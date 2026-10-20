import asyncio
import base64
import io
import json
import os
import re
from typing import Any
import zlib

from fastmcp import FastMCP
from google.cloud import bigquery
from google.cloud import storage
from pypdf import PdfReader

mcp = FastMCP("EreinOperacionesMCP")

# ==============================================================================
# Cloud Configuration (BigQuery + Cloud Storage Exclusively — No Local Fallbacks)
# ==============================================================================
BQ_DATASET = os.getenv("BQ_DATASET", "erein_operaciones")

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


def _get_project_id() -> str:
    project_id = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
    if not project_id:
        import google.auth

        _, detected = google.auth.default()
        project_id = detected or ""
    if not project_id:
        raise RuntimeError(
            "PROJECT_ID / GOOGLE_CLOUD_PROJECT no está configurado en el servidor MCP."
        )
    return project_id


def _get_gcs_bucket(project_id: str) -> str:
    return os.getenv("GCS_BUCKET") or f"{project_id}-erein-docs"


def _stdlib_extract_pdf_pages(pdf_bytes: bytes) -> list[dict[str, Any]]:
    """Fallback PDF stream parser for ReportLab ASCII85+Flate streams if PdfReader returns empty text."""
    pages: list[dict[str, Any]] = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\n?endstream", pdf_bytes, re.DOTALL):
        raw = m.group(1).strip()
        dec = None
        for fn in (
            lambda b: zlib.decompress(base64.a85decode(b, adobe=True)),
            lambda b: zlib.decompress(base64.a85decode(b)),
            lambda b: zlib.decompress(b),
        ):
            try:
                dec = fn(raw)
                break
            except Exception:
                continue
        if dec is None or (b"BT" not in dec and b"Tj" not in dec and b"TJ" not in dec):
            continue
        parts: list[str] = []
        for sm in re.finditer(rb"\((?:\\.|[^\\()])*\)", dec):
            s = sm.group(0)[1:-1]
            s = re.sub(rb"\\([0-7]{1,3})", lambda om: bytes([int(om.group(1), 8)]), s)
            s = s.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
            parts.append(s.decode("cp1252", errors="ignore"))
        if parts:
            pages.append({"pagina": len(pages) + 1, "texto": "\n".join(parts)})
    return pages


def _extract_pdf_text_bytes(pdf_bytes: bytes) -> list[dict[str, Any]]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for idx, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        pages.append({"pagina": idx, "texto": text})
    if any(p["texto"] for p in pages):
        return pages
    fallback_pages = _stdlib_extract_pdf_pages(pdf_bytes)
    return fallback_pages if fallback_pages else pages


def _read_pdf_pages_from_gcs(project_id: str, ruta_relativa: str) -> list[dict[str, Any]]:
    bucket_name = _get_gcs_bucket(project_id)
    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(ruta_relativa)
    if not blob.exists():
        raise FileNotFoundError(
            f"El objeto gs://{bucket_name}/{ruta_relativa} no existe en Cloud Storage."
        )
    pdf_bytes = blob.download_as_bytes()
    return _extract_pdf_text_bytes(pdf_bytes)


# ==============================================================================
# MCP Tools (BigQuery & Cloud Storage Exclusively)
# ==============================================================================
@mcp.tool(annotations={"readOnlyHint": True})
def consultar_expediente(caso_id: str) -> str:
    """Consulta en BigQuery los datos declarados del expediente, las evidencias disponibles (ocultando el importe directo de las nóminas para obligar a su verificación documental en PDF), las referencias documentales del caso y las acciones previas registradas.

    Args:
        caso_id: Identificador del expediente (ej. 'SYN-001' a 'SYN-010').
    """
    caso_id = caso_id.strip().upper()
    if not CASE_ID_REGEX.match(caso_id):
        return json.dumps(
            {"status": "ERROR", "message": f"Formato de caso_id inválido: '{caso_id}'."},
            ensure_ascii=False,
        )

    try:
        project_id = _get_project_id()
        bq = bigquery.Client(project=project_id)
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
            FROM `{project_id}.{BQ_DATASET}.evidencias` v
            WHERE v.caso_id = @caso_id
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
            FROM `{project_id}.{BQ_DATASET}.documentos` x
            WHERE x.caso_id = @caso_id
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
            FROM `{project_id}.{BQ_DATASET}.acciones` a
            WHERE a.caso_id = @caso_id
            ORDER BY a.fecha_accion ASC
          ) AS acciones_previas
        FROM `{project_id}.{BQ_DATASET}.expedientes` e
        WHERE e.caso_id = @caso_id
        """
        rows = list(bq.query(sql, job_config=job_config).result())
        if not rows:
            return json.dumps(
                {
                    "status": "NOT_FOUND",
                    "source": "bigquery",
                    "message": f"No existe el expediente {caso_id} en BigQuery ({project_id}.{BQ_DATASET}.expedientes).",
                },
                ensure_ascii=False,
            )

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
    except Exception as exc:
        return json.dumps(
            {
                "status": "CLOUD_ERROR",
                "source": "bigquery",
                "caso_id": caso_id,
                "message": f"Error al consultar BigQuery para el expediente '{caso_id}': {exc}",
            },
            ensure_ascii=False,
            indent=2,
        )


@mcp.tool(annotations={"readOnlyHint": True})
def leer_documento(caso_id: str, documento_id: str) -> str:
    """Lee desde Cloud Storage un documento PDF (nómina, contrato, certificado o justificante) asociado al expediente autorizado en BigQuery y devuelve el documento_id, número de página y fragmento de texto extraído.

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

    try:
        project_id = _get_project_id()
        bq = bigquery.Client(project=project_id)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("documento_id", "STRING", documento_id)
            ]
        )
        sql = f"""
        SELECT *
        FROM `{project_id}.{BQ_DATASET}.documentos`
        WHERE documento_id = @documento_id
        LIMIT 1
        """
        rows = list(bq.query(sql, job_config=job_config).result())
    except Exception as exc:
        return json.dumps(
            {
                "status": "CLOUD_ERROR",
                "source": "bigquery",
                "caso_id": caso_id,
                "documento_id": documento_id,
                "message": f"Error al consultar el catálogo de documentos en BigQuery: {exc}",
            },
            ensure_ascii=False,
            indent=2,
        )

    if not rows:
        return json.dumps(
            {
                "status": "NOT_FOUND",
                "source": "bigquery",
                "message": f"El documento '{documento_id}' no existe en la tabla BigQuery '{project_id}.{BQ_DATASET}.documentos'.",
            },
            ensure_ascii=False,
        )

    doc_meta = dict(rows[0].items())

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
        pages = _read_pdf_pages_from_gcs(project_id, ruta_relativa)
    except Exception as exc:
        return json.dumps(
            {
                "status": "CLOUD_ERROR",
                "source": "cloud_storage",
                "caso_id": caso_id,
                "documento_id": documento_id,
                "message": f"Error al leer el PDF desde Cloud Storage ('{ruta_relativa}'): {exc}",
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "status": "SUCCESS",
            "source": "cloud_storage",
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
    """Consulta en BigQuery los movimientos bancarios (transacciones) en el rango de fechas indicado y los resúmenes mensuales de cuenta (saldos, descubiertos, devoluciones e impagos) para un expediente.

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

    try:
        project_id = _get_project_id()
        bq = bigquery.Client(project=project_id)
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
        FROM `{project_id}.{BQ_DATASET}.transacciones`
        WHERE caso_id = @caso_id AND fecha BETWEEN @desde AND @hasta
        ORDER BY fecha, transaccion_id
        """
        transacciones = [
            dict(r.items()) for r in bq.query(tx_sql, job_config=tx_config).result()
        ]

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
        FROM `{project_id}.{BQ_DATASET}.resumen_cuentas`
        WHERE caso_id = @caso_id
        ORDER BY mes
        """
        resumenes = [
            dict(r.items()) for r in bq.query(res_sql, job_config=res_config).result()
        ]
    except Exception as exc:
        return json.dumps(
            {
                "status": "CLOUD_ERROR",
                "source": "bigquery",
                "caso_id": caso_id,
                "message": f"Error al consultar transacciones/resumen_cuentas en BigQuery: {exc}",
            },
            ensure_ascii=False,
            indent=2,
        )

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
            "source": "bigquery",
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
    """Consulta en BigQuery los registros de historial laboral (empresas, grupo empresarial, fechas de alta y baja, documento acreditativo) asociados al expediente.

    Args:
        caso_id: Identificador del expediente (ej. 'SYN-004').
    """
    caso_id = caso_id.strip().upper()
    if not CASE_ID_REGEX.match(caso_id):
        return json.dumps(
            {"status": "ERROR", "message": f"Formato de caso_id inválido: '{caso_id}'."},
            ensure_ascii=False,
        )

    try:
        project_id = _get_project_id()
        bq = bigquery.Client(project=project_id)
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
        FROM `{project_id}.{BQ_DATASET}.historial_laboral`
        WHERE caso_id = @caso_id
        ORDER BY fecha_alta
        """
        registros = [
            dict(r.items()) for r in bq.query(sql, job_config=job_config).result()
        ]
    except Exception as exc:
        return json.dumps(
            {
                "status": "CLOUD_ERROR",
                "source": "bigquery",
                "caso_id": caso_id,
                "message": f"Error al consultar historial_laboral en BigQuery: {exc}",
            },
            ensure_ascii=False,
            indent=2,
        )

    return json.dumps(
        {
            "status": "SUCCESS",
            "source": "bigquery",
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
    """Registra la resolución, solicitud de evidencia, escalado a Operaciones o cierre por evidencia incompleta en la tabla BigQuery 'acciones', validando autoridad (P09) e idempotencia por accion_id.

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

    try:
        project_id = _get_project_id()
        bq = bigquery.Client(project=project_id)
        table_acciones = f"`{project_id}.{BQ_DATASET}.acciones`"

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
    except Exception as exc:
        return json.dumps(
            {
                "status": "CLOUD_ERROR",
                "source": "bigquery",
                "caso_id": caso_id,
                "accion_id": accion_id,
                "message": f"Error al registrar la acción en BigQuery: {exc}",
            },
            ensure_ascii=False,
            indent=2,
        )

    return json.dumps(
        {
            "status": "SUCCESS",
            "source": "bigquery",
            "persisted_in_bigquery": True,
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
