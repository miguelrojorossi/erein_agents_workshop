import asyncio
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

try:
    from fastmcp import FastMCP
except ImportError:
    class FastMCP:  # Fallback stub when running unit tests without fastmcp installed
        def __init__(self, name: str):
            self.name = name

        def tool(self, **_kwargs):
            def decorator(fn):
                return fn
            return decorator

        async def run_async(self, **_kwargs):
            raise RuntimeError("Install fastmcp to run the HTTP server.")

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

mcp = FastMCP("EreinOperacionesMCP")

# ==============================================================================
# Configuration & Paths
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent
REPO_DATA_DIR = BASE_DIR.parent / "EREIN_CdU_Revision_Operativa_Agentica"
BUNDLED_DATA_DIR = BASE_DIR / "data"
DATA_DIR = BUNDLED_DATA_DIR if (BUNDLED_DATA_DIR / "01_Datos").exists() else REPO_DATA_DIR
RUNTIME_STATE_PATH = BUNDLED_DATA_DIR / "runtime_state.json"

PROJECT_ID = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
BQ_DATASET = os.getenv("BQ_DATASET", "erein_operaciones")
GCS_BUCKET = os.getenv("GCS_BUCKET", f"{PROJECT_ID}-erein-docs" if PROJECT_ID else "")
USE_LOCAL_DATA = os.getenv("USE_LOCAL_DATA", "auto").lower()

SIMULATED_DATE = "2026-10-14"
SIMULATED_TIMESTAMP = "2026-10-14T12:00:00Z"

ALLOWED_ESTADOS = {
    "En revisión",
    "Pendiente de evidencia",
    "Escalado a Operaciones",
    "Resuelto",
    "Cerrado por evidencia incompleta",
}

FORBIDDEN_KEYWORDS_ESTADO = {
    "aprobado",
    "concedido",
    "denegado",
    "rechazado",
    "no elegible",
    "inadmisible",
}

CASE_ID_REGEX = re.compile(r"^SYN-\d{3}$")
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ==============================================================================
# Data Access Helpers (BigQuery/GCS with Automatic Local Fallback)
# ==============================================================================
def _should_use_cloud() -> bool:
    if USE_LOCAL_DATA in ("true", "1", "yes"):
        return False
    return bool(PROJECT_ID)


def _load_runtime_state() -> dict[str, Any]:
    if RUNTIME_STATE_PATH.exists():
        try:
            return json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"syn002_responded": False, "syn007_clarified": False, "acciones": []}


def _save_runtime_state(state: dict[str, Any]) -> None:
    RUNTIME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_csv(rel_path: str) -> list[dict[str, Any]]:
    csv_path = DATA_DIR / rel_path
    if not csv_path.exists():
        return []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            cleaned = {}
            for k, v in row.items():
                if v == "" or v is None:
                    cleaned[k] = None
                elif v in ("true", "TRUE"):
                    cleaned[k] = True
                elif v in ("false", "FALSE"):
                    cleaned[k] = False
                else:
                    cleaned[k] = v
            rows.append(cleaned)
        return rows


def _extract_pdf_text_bytes(pdf_bytes: bytes) -> list[dict[str, Any]]:
    if PdfReader is not None:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for idx, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            pages.append({"pagina": idx, "texto": text})
        return pages

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        res = subprocess.run(
            ["pdftotext", tmp.name, "-"],
            capture_output=True,
            text=True,
            check=True,
        )
        raw_pages = [p.strip() for p in res.stdout.split("\x0c") if p.strip()]
        return [
            {"pagina": idx, "texto": text}
            for idx, text in enumerate(raw_pages or [res.stdout.strip()], start=1)
        ]


def _read_pdf_pages(ruta_relativa: str) -> list[dict[str, Any]]:
    # Try Cloud Storage first when running in Cloud mode
    if _should_use_cloud() and GCS_BUCKET:
        try:
            from google.cloud import storage

            client = storage.Client(project=PROJECT_ID)
            bucket = client.bucket(GCS_BUCKET)
            blob = bucket.blob(ruta_relativa)
            if blob.exists():
                pdf_bytes = blob.download_as_bytes()
                return _extract_pdf_text_bytes(pdf_bytes)
        except Exception:
            pass

    # Fallback to local bundled PDF
    local_pdf = DATA_DIR / ruta_relativa
    if not local_pdf.exists():
        local_pdf = REPO_DATA_DIR / ruta_relativa
    if not local_pdf.exists():
        raise FileNotFoundError(f"No se encontró el archivo PDF en {ruta_relativa}")
    return _extract_pdf_text_bytes(local_pdf.read_bytes())


# ==============================================================================
# MCP Tools
# ==============================================================================
@mcp.tool(annotations={"readOnlyHint": True})
def consultar_expediente(caso_id: str) -> str:
    """Consulta los datos declarados del expediente, las evidencias disponibles (ocultando el importe directo de las nóminas para obligar a su verificación documental en PDF), las referencias documentales del caso y las acciones previas registradas.

    Args:
        caso_id: Identificador del expediente (ej. 'SYN-001' a 'SYN-010').
    """
    caso_id = caso_id.strip().upper()
    if not CASE_ID_REGEX.match(caso_id):
        return json.dumps(
            {"status": "ERROR", "message": f"Formato de caso_id inválido: '{caso_id}'."},
            ensure_ascii=False,
        )

    # Try BigQuery first if configured
    if _should_use_cloud():
        try:
            from google.cloud import bigquery

            bq = bigquery.Client(project=PROJECT_ID)
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("caso_id", "STRING", caso_id)
                ]
            )
            sql = f"""
            SELECT
              e.*,
              ARRAY(
                SELECT AS STRUCT
                  v.evidencia_id,
                  v.caso_id,
                  v.fase,
                  v.tipo_fuente,
                  CAST(v.fecha_evidencia AS STRING) AS fecha_evidencia,
                  v.periodo,
                  v.disponibilidad,
                  IF(v.tipo_fuente = 'Nómina', NULL, v.importe_eur) AS importe_eur,
                  IF(v.tipo_fuente = 'Nómina', 'Leer el PDF vinculado o solicitarlo si está ausente', v.contenido) AS contenido
                FROM `{PROJECT_ID}.{BQ_DATASET}.evidencias` v
                WHERE v.caso_id = e.caso_id
                ORDER BY v.evidencia_id
              ) AS evidencias,
              ARRAY(
                SELECT AS STRUCT
                  x.documento_id,
                  x.caso_id,
                  x.evidencia_id,
                  x.fase,
                  x.tipo_documento,
                  x.periodo,
                  x.ruta_relativa,
                  x.disponibilidad,
                  x.pagina,
                  x.disponible_inicial
                FROM `{PROJECT_ID}.{BQ_DATASET}.documentos` x
                WHERE x.caso_id = e.caso_id
                ORDER BY x.documento_id
              ) AS documentos,
              ARRAY(
                SELECT AS STRUCT
                  a.accion_id,
                  a.tipo_accion,
                  a.estado,
                  a.ingreso_actualizado_eur_mes,
                  a.cuota_actualizada_eur_mes,
                  a.situacion_laboral_actualizada,
                  a.evidencias_ids,
                  a.reglas_ids,
                  a.justificacion,
                  a.mensaje_cliente,
                  a.motivo_escalado,
                  CAST(a.fecha_accion AS STRING) AS fecha_accion,
                  CAST(a.antiguedad_reconocida_desde AS STRING) AS antiguedad_reconocida_desde,
                  a.renta_bruta_observada_eur_mes,
                  a.tratamiento_alerta,
                  a.version_politica
                FROM `{PROJECT_ID}.{BQ_DATASET}.acciones` a
                WHERE a.caso_id = e.caso_id
                ORDER BY a.fecha_accion ASC
              ) AS acciones_previas
            FROM `{PROJECT_ID}.{BQ_DATASET}.expedientes` e
            WHERE e.caso_id = @caso_id
            """
            rows = list(bq.query(sql, job_config=job_config).result())
            if rows:
                record = dict(rows[0].items())
                return json.dumps(
                    {
                        "status": "SUCCESS",
                        "source": "bigquery",
                        "fecha_simulada_ejercicio": SIMULATED_DATE,
                        "expediente": record,
                    },
                    default=str,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass

    # Local fallback using 01_Datos CSVs + runtime_state.json
    expedientes = [
        r for r in _read_csv("01_Datos/expedientes.csv") if r["caso_id"] == caso_id
    ]
    if not expedientes:
        return json.dumps(
            {"status": "NOT_FOUND", "message": f"No existe el expediente {caso_id}."},
            ensure_ascii=False,
        )

    exp = dict(expedientes[0])
    state = _load_runtime_state()

    evidencias_raw = [
        r for r in _read_csv("01_Datos/evidencias.csv") if r["caso_id"] == caso_id
    ]
    documentos_raw = [
        r for r in _read_csv("01_Datos/documentos.csv") if r["caso_id"] == caso_id
    ]

    # Incorporate simulated customer responses if enabled in runtime_state
    if caso_id == "SYN-002" and state.get("syn002_responded"):
        resp_ev = _read_csv("04_Simulacion_Cliente/respuesta_simulada.csv")
        resp_doc = _read_csv("04_Simulacion_Cliente/documentos_respuesta.csv")
        existing_ev_ids = {e["evidencia_id"] for e in evidencias_raw}
        for e in resp_ev:
            if e["caso_id"] == "SYN-002" and e["evidencia_id"] not in existing_ev_ids:
                evidencias_raw.append(e)
        existing_doc_ids = {d["documento_id"] for d in documentos_raw}
        for d in resp_doc:
            if d["caso_id"] == "SYN-002" and d["documento_id"] not in existing_doc_ids:
                d_copy = dict(d)
                d_copy["disponible_inicial"] = True
                documentos_raw.append(d_copy)
        for d in documentos_raw:
            if d["documento_id"] == "DOC-SYN-002-E04":
                d["ruta_relativa"] = "04_Simulacion_Cliente/SYN-002_septiembre.pdf"
                d["disponibilidad"] = "Disponible"
                d["disponible_inicial"] = True

    if caso_id == "SYN-007" and state.get("syn007_clarified"):
        aclaracion = _read_csv("04_Simulacion_Cliente/aclaracion_alquiler.csv")
        for a in aclaracion:
            if a["caso_id"] == "SYN-007":
                evidencias_raw.append(
                    {
                        "evidencia_id": a["evento_id"],
                        "caso_id": "SYN-007",
                        "fase": "Respuesta simulada",
                        "tipo_fuente": "Interacción",
                        "fecha_evidencia": a["fecha"],
                        "periodo": "Octubre",
                        "importe_eur": None,
                        "disponibilidad": "Disponible",
                        "contenido": a["mensaje"],
                    }
                )

    # Mask Nómina amounts per consultar_expediente.sql
    evidencias_masked = []
    for v in evidencias_raw:
        es_nomina = v.get("tipo_fuente") == "Nómina"
        evidencias_masked.append(
            {
                "evidencia_id": v.get("evidencia_id"),
                "caso_id": v.get("caso_id"),
                "fase": v.get("fase"),
                "tipo_fuente": v.get("tipo_fuente"),
                "fecha_evidencia": v.get("fecha_evidencia"),
                "periodo": v.get("periodo"),
                "disponibilidad": v.get("disponibilidad"),
                "importe_eur": None if es_nomina else v.get("importe_eur"),
                "contenido": (
                    "Leer el PDF vinculado o solicitarlo si está ausente"
                    if es_nomina
                    else v.get("contenido")
                ),
            }
        )

    acciones_previas = [
        a for a in state.get("acciones", []) if a.get("caso_id") == caso_id
    ]

    exp["evidencias"] = evidencias_masked
    exp["documentos"] = documentos_raw
    exp["acciones_previas"] = acciones_previas

    return json.dumps(
        {
            "status": "SUCCESS",
            "source": "local_dataset",
            "fecha_simulada_ejercicio": SIMULATED_DATE,
            "expediente": exp,
        },
        default=str,
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(annotations={"readOnlyHint": True})
def leer_documento(caso_id: str, documento_id: str) -> str:
    """Lee un documento PDF (nómina, contrato, certificado o justificante) asociado al expediente autorizado y devuelve el documento_id, número de página y fragmento de texto extraído.

    Args:
        caso_id: Identificador del expediente autorizado (ej. 'SYN-001').
        documento_id: Identificador del documento dentro del expediente (ej. 'DOC-SYN-001-E04').
    """
    caso_id = caso_id.strip().upper()
    documento_id = documento_id.strip().upper()

    if not CASE_ID_REGEX.match(caso_id):
        return json.dumps(
            {"status": "ERROR", "message": f"Formato de caso_id inválido: '{caso_id}'."},
            ensure_ascii=False,
        )

    doc_meta: dict[str, Any] | None = None

    # Check BigQuery metadata if in cloud mode
    if _should_use_cloud():
        try:
            from google.cloud import bigquery

            bq = bigquery.Client(project=PROJECT_ID)
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("documento_id", "STRING", documento_id)
                ]
            )
            sql = f"""
            SELECT *
            FROM `{PROJECT_ID}.{BQ_DATASET}.documentos`
            WHERE documento_id = @documento_id
            LIMIT 1
            """
            rows = list(bq.query(sql, job_config=job_config).result())
            if rows:
                doc_meta = dict(rows[0].items())
        except Exception:
            pass

    if doc_meta is None:
        state = _load_runtime_state()
        docs = _read_csv("01_Datos/documentos.csv")
        if caso_id == "SYN-002" and state.get("syn002_responded"):
            docs.extend(_read_csv("04_Simulacion_Cliente/documentos_respuesta.csv"))
            for d in docs:
                if d.get("documento_id") == "DOC-SYN-002-E04":
                    d["ruta_relativa"] = "04_Simulacion_Cliente/SYN-002_septiembre.pdf"
                    d["disponibilidad"] = "Disponible"
                    d["disponible_inicial"] = True
        for d in docs:
            if d.get("documento_id") == documento_id:
                doc_meta = d
                break

    if doc_meta is None:
        return json.dumps(
            {
                "status": "NOT_FOUND",
                "message": f"El documento '{documento_id}' no existe en el catálogo.",
            },
            ensure_ascii=False,
        )

    # Strict per-case isolation check
    if doc_meta.get("caso_id") != caso_id:
        return json.dumps(
            {
                "status": "ACCESS_DENIED",
                "message": (
                    f"Violación de aislamiento: El documento '{documento_id}' no pertenece "
                    f"al expediente autorizado '{caso_id}'."
                ),
            },
            ensure_ascii=False,
        )

    disponibilidad = doc_meta.get("disponibilidad")
    ruta_relativa = doc_meta.get("ruta_relativa")

    if disponibilidad == "Ausente" or not ruta_relativa:
        return json.dumps(
            {
                "status": "DOCUMENT_ABSENT",
                "caso_id": caso_id,
                "documento_id": documento_id,
                "evidencia_id": doc_meta.get("evidencia_id"),
                "tipo_documento": doc_meta.get("tipo_documento"),
                "periodo": doc_meta.get("periodo"),
                "disponibilidad": "Ausente",
                "message": (
                    "El documento figura como AUSENTE (no aportado por el cliente). "
                    "Si resulta imprescindible según la política DEMO-2, debe solicitarse al cliente."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    try:
        pages = _read_pdf_pages(ruta_relativa)
    except Exception as exc:
        return json.dumps(
            {
                "status": "ERROR",
                "caso_id": caso_id,
                "documento_id": documento_id,
                "message": f"Error al leer el PDF '{ruta_relativa}': {exc}",
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "status": "SUCCESS",
            "caso_id": caso_id,
            "documento_id": documento_id,
            "evidencia_id": doc_meta.get("evidencia_id"),
            "tipo_documento": doc_meta.get("tipo_documento"),
            "periodo": doc_meta.get("periodo"),
            "disponibilidad": disponibilidad,
            "ruta_relativa": ruta_relativa,
            "paginas": pages,
            "citacion_sugerida": f"{documento_id} (pág. {pages[0]['pagina'] if pages else 1})",
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(annotations={"readOnlyHint": True})
def consultar_transacciones(
    caso_id: str,
    desde: str = "2026-07-01",
    hasta: str = "2026-09-30",
) -> str:
    """Consulta los movimientos bancarios (transacciones) en el rango de fechas indicado y los resúmenes mensuales de cuenta (saldos, descubiertos, devoluciones e impagos) para un expediente.

    Args:
        caso_id: Identificador del expediente (ej. 'SYN-006').
        desde: Fecha inicial en formato YYYY-MM-DD (por defecto '2026-07-01').
        hasta: Fecha final en formato YYYY-MM-DD (por defecto '2026-09-30').
    """
    caso_id = caso_id.strip().upper()
    desde = desde.strip()
    hasta = hasta.strip()

    if not CASE_ID_REGEX.match(caso_id):
        return json.dumps(
            {"status": "ERROR", "message": f"Formato de caso_id inválido: '{caso_id}'."},
            ensure_ascii=False,
        )
    if not (DATE_REGEX.match(desde) and DATE_REGEX.match(hasta)):
        return json.dumps(
            {
                "status": "ERROR",
                "message": "Las fechas 'desde' y 'hasta' deben tener formato YYYY-MM-DD.",
            },
            ensure_ascii=False,
        )

    transacciones: list[dict[str, Any]] = []
    resumenes: list[dict[str, Any]] = []

    if _should_use_cloud():
        try:
            from google.cloud import bigquery

            bq = bigquery.Client(project=PROJECT_ID)
            tx_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("caso_id", "STRING", caso_id),
                    bigquery.ScalarQueryParameter("desde", "DATE", desde),
                    bigquery.ScalarQueryParameter("hasta", "DATE", hasta),
                ]
            )
            tx_sql = f"""
            SELECT
              transaccion_id,
              caso_id,
              cuenta_id,
              CAST(fecha AS STRING) AS fecha,
              CAST(importe_eur AS FLOAT64) AS importe_eur,
              moneda,
              concepto,
              contraparte,
              categoria_proveedor,
              CAST(saldo_tras_movimiento_eur AS FLOAT64) AS saldo_tras_movimiento_eur,
              estado
            FROM `{PROJECT_ID}.{BQ_DATASET}.transacciones`
            WHERE caso_id = @caso_id AND fecha BETWEEN @desde AND @hasta
            ORDER BY fecha, transaccion_id
            """
            transacciones = [dict(r.items()) for r in bq.query(tx_sql, job_config=tx_config).result()]

            res_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("caso_id", "STRING", caso_id)
                ]
            )
            res_sql = f"""
            SELECT
              caso_id,
              mes,
              CAST(saldo_inicial_eur AS FLOAT64) AS saldo_inicial_eur,
              CAST(saldo_final_eur AS FLOAT64) AS saldo_final_eur,
              descubiertos,
              devoluciones,
              impagos,
              cobertura
            FROM `{PROJECT_ID}.{BQ_DATASET}.resumen_cuentas`
            WHERE caso_id = @caso_id
            ORDER BY mes
            """
            resumenes = [dict(r.items()) for r in bq.query(res_sql, job_config=res_config).result()]
        except Exception:
            transacciones = []
            resumenes = []

    if not transacciones and not resumenes:
        all_tx = _read_csv("01_Datos/transacciones.csv")
        for t in all_tx:
            if t["caso_id"] == caso_id and desde <= str(t["fecha"]) <= hasta:
                t_copy = dict(t)
                t_copy["importe_eur"] = float(t_copy["importe_eur"])
                t_copy["saldo_tras_movimiento_eur"] = float(t_copy["saldo_tras_movimiento_eur"])
                transacciones.append(t_copy)
        transacciones.sort(key=lambda x: (str(x["fecha"]), str(x["transaccion_id"])))

        all_res = _read_csv("01_Datos/resumen_cuentas.csv")
        for r in all_res:
            if r["caso_id"] == caso_id:
                r_copy = dict(r)
                r_copy["saldo_inicial_eur"] = float(r_copy["saldo_inicial_eur"])
                r_copy["saldo_final_eur"] = float(r_copy["saldo_final_eur"])
                r_copy["descubiertos"] = int(r_copy["descubiertos"])
                r_copy["devoluciones"] = int(r_copy["devoluciones"])
                r_copy["impagos"] = int(r_copy["impagos"])
                resumenes.append(r_copy)
        resumenes.sort(key=lambda x: str(x["mes"]))

    # Compute analytical breakdown by month and category to assist P02, P05, P07, P10, P11, P13
    por_mes: dict[str, dict[str, float]] = {}
    for tx in transacciones:
        mes = str(tx["fecha"])[:7]
        cat = str(tx["categoria_proveedor"])
        imp = float(tx["importe_eur"])
        por_mes.setdefault(mes, {})
        por_mes[mes][cat] = round(por_mes[mes].get(cat, 0.0) + imp, 2)

    metricas_gambling_por_mes = {}
    total_salary = 0.0
    total_gambling_abs = 0.0
    for mes, cats in sorted(por_mes.items()):
        sal = cats.get("SALARY", 0.0)
        gamb = abs(cats.get("GAMBLING", 0.0))
        total_salary += sal
        total_gambling_abs += gamb
        if gamb > 0 and sal > 0:
            metricas_gambling_por_mes[mes] = {
                "gasto_gambling_eur": gamb,
                "nomina_abono_eur": sal,
                "porcentaje_sobre_nomina": round((gamb / sal) * 100.0, 2),
            }

    resumen_analitico = {
        "totales_por_mes_y_categoria_eur": por_mes,
        "total_incidencias_periodo": {
            "descubiertos": sum(int(r["descubiertos"]) for r in resumenes),
            "devoluciones": sum(int(r["devoluciones"]) for r in resumenes),
            "impagos": sum(int(r["impagos"]) for r in resumenes),
        },
    }
    if metricas_gambling_por_mes:
        resumen_analitico["contexto_categoria_gambling"] = {
            "por_mes": metricas_gambling_por_mes,
            "trimestre_total_gambling_eur": round(total_gambling_abs, 2),
            "trimestre_total_nomina_eur": round(total_salary, 2),
            "trimestre_porcentaje_sobre_nomina": (
                round((total_gambling_abs / total_salary) * 100.0, 2)
                if total_salary > 0
                else None
            ),
        }

    return json.dumps(
        {
            "status": "SUCCESS",
            "caso_id": caso_id,
            "rango_consultado": {"desde": desde, "hasta": hasta},
            "resumen_cuentas": resumenes,
            "resumen_analitico": resumen_analitico,
            "transacciones": transacciones,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(annotations={"readOnlyHint": True})
def consultar_historial_laboral(caso_id: str) -> str:
    """Consulta los registros de historial laboral (empresas, grupo empresarial, fechas de alta y baja, documento acreditativo) asociados al expediente.

    Args:
        caso_id: Identificador del expediente (ej. 'SYN-004').
    """
    caso_id = caso_id.strip().upper()
    if not CASE_ID_REGEX.match(caso_id):
        return json.dumps(
            {"status": "ERROR", "message": f"Formato de caso_id inválido: '{caso_id}'."},
            ensure_ascii=False,
        )

    registros: list[dict[str, Any]] = []
    if _should_use_cloud():
        try:
            from google.cloud import bigquery

            bq = bigquery.Client(project=PROJECT_ID)
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("caso_id", "STRING", caso_id)
                ]
            )
            sql = f"""
            SELECT
              caso_id,
              empresa_id,
              empresa,
              grupo,
              CAST(fecha_alta AS STRING) AS fecha_alta,
              CAST(fecha_baja AS STRING) AS fecha_baja,
              CAST(fecha_consulta AS STRING) AS fecha_consulta,
              documento_id
            FROM `{PROJECT_ID}.{BQ_DATASET}.historial_laboral`
            WHERE caso_id = @caso_id
            ORDER BY fecha_alta
            """
            registros = [dict(r.items()) for r in bq.query(sql, job_config=job_config).result()]
        except Exception:
            registros = []

    if not registros:
        registros = [
            r
            for r in _read_csv("01_Datos/historial_laboral.csv")
            if r["caso_id"] == caso_id
        ]
        registros.sort(key=lambda x: str(x.get("fecha_alta") or ""))

    return json.dumps(
        {
            "status": "SUCCESS",
            "caso_id": caso_id,
            "historial_laboral": registros,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def registrar_accion_expediente(
    caso_id: str,
    accion_id: str,
    tipo_accion: str,
    estado: str,
    evidencias_ids: str,
    reglas_ids: str,
    justificacion: str,
    ingreso_actualizado_eur_mes: float | None = None,
    cuota_actualizada_eur_mes: float | None = None,
    situacion_laboral_actualizada: str | None = None,
    antiguedad_reconocida_desde: str | None = None,
    renta_bruta_observada_eur_mes: float | None = None,
    tratamiento_alerta: str | None = None,
    mensaje_cliente: str | None = None,
    motivo_escalado: str | None = None,
    version_politica: str = "DEMO-2",
) -> str:
    """Registra la resolución, solicitud de evidencia, escalado a Operaciones o cierre por evidencia incompleta en la tabla 'acciones', validando autoridad (P09) e idempotencia por accion_id.

    Args:
        caso_id: Identificador del expediente ('SYN-001' a 'SYN-010').
        accion_id: Identificador único de la acción para evitar duplicados (ej. 'ACC-SYN-001-01').
        tipo_accion: Tipo de actuación ('RESOLUCION', 'SOLICITUD_EVIDENCIA', 'ESCALADO_OPERACIONES', 'CIERRE_EVIDENCIA_INCOMPLETA').
        estado: Estado resultante permitido ('En revisión', 'Pendiente de evidencia', 'Escalado a Operaciones', 'Resuelto', 'Cerrado por evidencia incompleta').
        evidencias_ids: Lista separada por espacios o comas de los IDs de evidencias y documentos utilizados (ej. 'SYN-001-E01 SYN-001-E02 DOC-SYN-001-E04').
        reglas_ids: Lista separada por espacios de las reglas de la política DEMO-2 aplicadas (ej. 'P01 P02 P09').
        justificacion: Explicación detallada del análisis, documentos/páginas consultados y fundamento de la actuación.
        ingreso_actualizado_eur_mes: Nuevo ingreso neto mensual computable en EUR (si procede actualizar o confirmar).
        cuota_actualizada_eur_mes: Nueva cuota mensual de obligaciones financieras en EUR (si procede actualizar).
        situacion_laboral_actualizada: Situación laboral verificada (si procede actualizar).
        antiguedad_reconocida_desde: Fecha de antigüedad laboral reconocida en formato YYYY-MM-DD (ej. '2021-10-01' para SYN-004).
        renta_bruta_observada_eur_mes: Renta bruta mensual de alquiler observada por separado (ej. 700 para SYN-007; nunca sumada al salario).
        tratamiento_alerta: Resultado del análisis de alertas (ej. reclasificación de compra comercial o contexto de 15% de apuestas).
        mensaje_cliente: Mensaje concreto para el cliente cuando se solicita evidencia pendiente.
        motivo_escalado: Motivo detallado cuando el expediente se escala a Operaciones.
        version_politica: Versión de la política utilizada (por defecto 'DEMO-2').
    """
    caso_id = caso_id.strip().upper()
    accion_id = accion_id.strip()
    estado = estado.strip()
    tipo_accion = tipo_accion.strip().upper()

    if not CASE_ID_REGEX.match(caso_id):
        return json.dumps(
            {"status": "ERROR", "message": f"Formato de caso_id inválido: '{caso_id}'."},
            ensure_ascii=False,
        )

    # Validate Authority (P09) & Allowed States
    estado_lower = estado.lower()
    for forbidden in FORBIDDEN_KEYWORDS_ESTADO:
        if forbidden in estado_lower or forbidden in tipo_accion.lower():
            return json.dumps(
                {
                    "status": "AUTHORITY_ERROR_P09",
                    "message": (
                        f"Acción rechazada por control de autoridad (P09): '{estado}' / '{tipo_accion}'. "
                        "El agente de revisión operativa NO tiene autoridad para conceder, aprobar, "
                        "denegar financiación ni etiquetar un expediente como 'No elegible'. "
                        f"Estados permitidos: {sorted(ALLOWED_ESTADOS)}."
                    ),
                },
                ensure_ascii=False,
            )

    if estado not in ALLOWED_ESTADOS:
        return json.dumps(
            {
                "status": "INVALID_STATE",
                "message": (
                    f"Estado '{estado}' no permitido por el Procedimiento DEMO-2. "
                    f"Debe ser exactamente uno de: {sorted(ALLOWED_ESTADOS)}."
                ),
            },
            ensure_ascii=False,
        )

    if not evidencias_ids.strip() or not reglas_ids.strip() or not justificacion.strip():
        return json.dumps(
            {
                "status": "VALIDATION_ERROR",
                "message": (
                    "Los campos 'evidencias_ids', 'reglas_ids' y 'justificacion' son "
                    "obligatorios para garantizar la trazabilidad operativa."
                ),
            },
            ensure_ascii=False,
        )

    record = {
        "caso_id": caso_id,
        "accion_id": accion_id,
        "tipo_accion": tipo_accion,
        "estado": estado,
        "ingreso_actualizado_eur_mes": ingreso_actualizado_eur_mes,
        "cuota_actualizada_eur_mes": cuota_actualizada_eur_mes,
        "situacion_laboral_actualizada": situacion_laboral_actualizada,
        "evidencias_ids": evidencias_ids.strip(),
        "reglas_ids": reglas_ids.strip(),
        "justificacion": justificacion.strip(),
        "mensaje_cliente": mensaje_cliente,
        "motivo_escalado": motivo_escalado,
        "fecha_accion": SIMULATED_TIMESTAMP,
        "antiguedad_reconocida_desde": antiguedad_reconocida_desde,
        "renta_bruta_observada_eur_mes": renta_bruta_observada_eur_mes,
        "tratamiento_alerta": tratamiento_alerta,
        "version_politica": version_politica or "DEMO-2",
    }

    # Persist in local runtime state (with idempotency check)
    state = _load_runtime_state()
    for existing in state.get("acciones", []):
        if existing.get("caso_id") == caso_id and existing.get("accion_id") == accion_id:
            return json.dumps(
                {
                    "status": "IDEMPOTENT_OK",
                    "message": (
                        f"La acción '{accion_id}' para '{caso_id}' ya estaba registrada. "
                        "Se evita duplicar la escritura."
                    ),
                    "accion_registrada": existing,
                },
                ensure_ascii=False,
                indent=2,
            )

    # Persist in BigQuery if configured
    persisted_in_bq = False
    if _should_use_cloud():
        try:
            from google.cloud import bigquery

            bq = bigquery.Client(project=PROJECT_ID)
            table_acciones = f"`{PROJECT_ID}.{BQ_DATASET}.acciones`"

            # Check idempotency in BigQuery
            check_cfg = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("caso_id", "STRING", caso_id),
                    bigquery.ScalarQueryParameter("accion_id", "STRING", accion_id),
                ]
            )
            dup_rows = list(
                bq.query(
                    f"SELECT accion_id FROM {table_acciones} WHERE caso_id=@caso_id AND accion_id=@accion_id LIMIT 1",
                    job_config=check_cfg,
                ).result()
            )
            if dup_rows:
                return json.dumps(
                    {
                        "status": "IDEMPOTENT_OK",
                        "source": "bigquery",
                        "message": (
                            f"La acción '{accion_id}' para '{caso_id}' ya consta en BigQuery. "
                            "No se duplica el registro."
                        ),
                        "accion_registrada": record,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            insert_cfg = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("caso_id", "STRING", record["caso_id"]),
                    bigquery.ScalarQueryParameter("accion_id", "STRING", record["accion_id"]),
                    bigquery.ScalarQueryParameter("tipo_accion", "STRING", record["tipo_accion"]),
                    bigquery.ScalarQueryParameter("estado", "STRING", record["estado"]),
                    bigquery.ScalarQueryParameter(
                        "ingreso_actualizado_eur_mes",
                        "NUMERIC",
                        record["ingreso_actualizado_eur_mes"],
                    ),
                    bigquery.ScalarQueryParameter(
                        "cuota_actualizada_eur_mes",
                        "NUMERIC",
                        record["cuota_actualizada_eur_mes"],
                    ),
                    bigquery.ScalarQueryParameter(
                        "situacion_laboral_actualizada",
                        "STRING",
                        record["situacion_laboral_actualizada"],
                    ),
                    bigquery.ScalarQueryParameter(
                        "evidencias_ids", "STRING", record["evidencias_ids"]
                    ),
                    bigquery.ScalarQueryParameter("reglas_ids", "STRING", record["reglas_ids"]),
                    bigquery.ScalarQueryParameter(
                        "justificacion", "STRING", record["justificacion"]
                    ),
                    bigquery.ScalarQueryParameter(
                        "mensaje_cliente", "STRING", record["mensaje_cliente"]
                    ),
                    bigquery.ScalarQueryParameter(
                        "motivo_escalado", "STRING", record["motivo_escalado"]
                    ),
                    bigquery.ScalarQueryParameter(
                        "fecha_accion", "TIMESTAMP", record["fecha_accion"]
                    ),
                    bigquery.ScalarQueryParameter(
                        "antiguedad_reconocida_desde",
                        "DATE",
                        record["antiguedad_reconocida_desde"],
                    ),
                    bigquery.ScalarQueryParameter(
                        "renta_bruta_observada_eur_mes",
                        "NUMERIC",
                        record["renta_bruta_observada_eur_mes"],
                    ),
                    bigquery.ScalarQueryParameter(
                        "tratamiento_alerta", "STRING", record["tratamiento_alerta"]
                    ),
                    bigquery.ScalarQueryParameter(
                        "version_politica", "STRING", record["version_politica"]
                    ),
                ]
            )
            insert_sql = f"""
            INSERT INTO {table_acciones} (
              caso_id, accion_id, tipo_accion, estado,
              ingreso_actualizado_eur_mes, cuota_actualizada_eur_mes,
              situacion_laboral_actualizada, evidencias_ids, reglas_ids,
              justificacion, mensaje_cliente, motivo_escalado,
              fecha_accion, antiguedad_reconocida_desde,
              renta_bruta_observada_eur_mes, tratamiento_alerta, version_politica
            )
            VALUES (
              @caso_id, @accion_id, @tipo_accion, @estado,
              @ingreso_actualizado_eur_mes, @cuota_actualizada_eur_mes,
              @situacion_laboral_actualizada, @evidencias_ids, @reglas_ids,
              @justificacion, @mensaje_cliente, @motivo_escalado,
              @fecha_accion, @antiguedad_reconocida_desde,
              @renta_bruta_observada_eur_mes, @tratamiento_alerta, @version_politica
            )
            """
            bq.query(insert_sql, job_config=insert_cfg).result()
            persisted_in_bq = True
        except Exception:
            persisted_in_bq = False

    state.setdefault("acciones", []).append(record)
    _save_runtime_state(state)

    return json.dumps(
        {
            "status": "SUCCESS",
            "persisted_in_bigquery": persisted_in_bq,
            "accion_registrada": record,
        },
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    asyncio.run(
        mcp.run_async(
            transport="streamable-http",
            host="0.0.0.0",
            port=port,
        )
    )
