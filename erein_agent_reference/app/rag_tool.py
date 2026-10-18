"""Native ADK Agent Tool for querying EREIN DEMO-2 Policies via Vertex AI RAG Engine."""

import base64
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
import warnings
import zlib

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Disable certificate-bound (CAA) tokens on Agent Runtime so regional Vertex AI
# RAG Engine endpoints (europe-west4-aiplatform.googleapis.com) accept the Agent Identity token.
os.environ["GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN"] = "false"
os.environ["GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"] = "false"
os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"
os.environ["GOOGLE_API_USE_MTLS_ENDPOINT"] = "never"

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONOCIMIENTO_DIR = (
    REPO_ROOT / "EREIN_CdU_Revision_Operativa_Agentica" / "03_Conocimiento"
)

EMBEDDED_CONOCIMIENTO_DOCS = [
    {
        "documento": "Politica_Revision_Operativa_DEMO2.pdf",
        "pagina": 1,
        "texto": (
            "EREIN | EJERCICIO SINTETICO - SIN VALIDEZ REAL\n"
            "Política de revisión operativa DEMO-2\n"
            "P01 - Ingreso recurrente: Dos nóminas recientes y sus abonos coincidentes acreditan un ingreso recurrente estable.\n"
            "P02 - Pagos no recurrentes: Excluir bonus, ventas y transferencias entre cuentas propias del ingreso recurrente.\n"
            "P03 - Cambio o variabilidad: Si el nuevo salario o las comisiones no cumplen P01, escalar el cálculo a Operaciones.\n"
            "P04 - Situación laboral: Usar evidencia administrativa reciente y documentación laboral coherente para actualizar la situación.\n"
            "P05 - Obligaciones: Contrato financiero vigente y cargos coincidentes acreditan la cuota mensual.\n"
            "P06 - Calidad de evidencia: Evidencia vigente y legible prevalece sobre fuente desactualizada o extracción errónea.\n"
            "P07 - Alerta sensible: Escalar apuestas reales para valoración humana. No inferir rechazo por el porcentaje de gasto ni diagnósticos. Antes, verificar si la categoría bancaria es correcta.\n"
            "P08 - Evidencia pendiente: Pedir solo lo necesario. Recordatorios a los 3 y 7 días. Tras 10 días sin respuesta, cerrar por evidencia incompleta.\n"
            "P09 - Autoridad: Resolver y registrar cambios en datos de prueba cuando la evidencia cumpla la política. Solicitar evidencia pendiente. Escalar contradicciones, alertas sensibles y excepciones. No conceder ni denegar préstamos.\n"
            "P10 - Clasificación de transacciones: Ticket coherente en titular, fecha, importe y referencia acredita compra comercial. Puede descartarse la alerta de apuestas mal clasificada conservando la categoría original y registrando la corrección.\n"
            "P11 - Alquiler no declarado: Contrato vigente y tres abonos coincidentes acreditan renta bruta observada. Registrar la renta bruta observada y escalar su computabilidad; no sumar al salario ni fijar ingreso neto admisible sin criterio de gastos y cargas.\n"
            "P12 - Continuidad intragrupo: Pertenecer al mismo grupo no basta. Historial continuo y certificado empresarial con antigüedad reconocida permiten conservar fecha reconocida para este ejercicio. Si falta evidencia, solicitarla o escalar.\n"
            "P13 - Contexto de apuestas: Calcular gasto apuestas / nómina neta por mes usando movimientos y nómina. Registrar saldos e incidencias observadas. Ausencia de incidencias no autoriza cerrar la alerta: escalar por P07."
        ),
    },
    {
        "documento": "Procedimiento_Revision_Operativa_DEMO2.pdf",
        "pagina": 1,
        "texto": (
            "EREIN | EJERCICIO SINTETICO - SIN VALIDEZ REAL\n"
            "Procedimiento operativo DEMO-2\n"
            "Fecha simulada fija: 14/10/2026. Todos los datos y reglas son ficticios.\n"
            "Recibir caso; decidir qué consultar; consultar evidencias y política; analizar conjuntamente alertas y discrepancias.\n"
            "Una categoría del proveedor es una señal, no una conclusión. Contrastar con documentación y movimientos.\n"
            "Si falta evidencia imprescindible: solicitar solo lo necesario y persistir el caso. Recordatorios a los días 3 y 7; cierre por evidencia incompleta transcurridos 10 días sin respuesta.\n"
            "Las respuestas se incorporan por evento al mismo caso. El operador puede resolver o solicitar nuevas evidencias. No confundir un escalado con un caso resuelto.\n"
            "Validar autoridad y campos antes de escribir. Usar accion_id único para evitar duplicados. Guardar política DEMO-2, evidencias, documento/página y resultado efectivo.\n"
            "Las fuentes originales no se sobrescriben. Registrar cambios propuestos y aceptados por separado. No conceder ni denegar financiación.\n"
            "Estado permitido: En revisión, Pendiente de evidencia, Escalado a Operaciones, Resuelto, Cerrado por evidencia incompleta. No elegible requiere política explícita adicional y no se evalúa en este paquete."
        ),
    },
]


def _get_gcp_credentials(project_id: str):
    """Returns Google Cloud credentials with cloud-platform scope (supports Cloud Workstations & Agent Runtime)."""
    import google.auth
    from google.auth import credentials as auth_credentials
    import google.auth.transport.requests

    os.environ["GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN"] = "false"
    os.environ["GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"] = "false"
    os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"
    os.environ["GOOGLE_API_USE_MTLS_ENDPOINT"] = "never"

    # In Cloud Run / Agent Runtime, use standard ADC credentials with unbound token
    if os.getenv("K_SERVICE") or os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID"):
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        try:
            creds.refresh(google.auth.transport.requests.Request())
        except Exception:
            pass
        return creds

    # On Cloud Workstations where only `gcloud auth login` was run, use gcloud CLI token
    class _GcloudCliCredentials(auth_credentials.Credentials):
        def __init__(self, quota_project_id: str) -> None:
            super().__init__()
            self._quota_project_id = quota_project_id
            self.refresh(None)

        @property
        def quota_project_id(self) -> str:
            return self._quota_project_id

        def refresh(self, request) -> None:
            token = subprocess.check_output(
                ["gcloud", "auth", "print-access-token"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if not token:
                raise RuntimeError("Empty token from gcloud auth print-access-token")
            self.token = token

    try:
        return _GcloudCliCredentials(quota_project_id=project_id)
    except Exception:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return creds


DEFAULT_RAG_CORPUS_NAME = ""
DEFAULT_PROJECT_ID = ""


def consultar_politica(consulta: str) -> str:
    """Recupera las reglas de la Política de Revisión Operativa DEMO-2 (P01 a P13) y el Procedimiento Operativo DEMO-2 mediante Vertex AI RAG Engine.

    Args:
        consulta: Términos de búsqueda o descripción de la discrepancia/alerta a consultar en la política (ej. 'ingreso recurrente nóminas bonus P01 P02', 'apuestas categoría GAMBLING P07 P10 P13', 'alquiler no declarado P11', 'antigüedad grupo empresarial P12', 'documento ilegible plazo P08').
    """
    project_id = (
        os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or DEFAULT_PROJECT_ID
    )
    region = os.getenv("RAG_LOCATION") or os.getenv("REGION", "europe-west4")
    if region == "europe-west1":
        region = "europe-west4"
    rag_corpus = os.getenv("RAG_CORPUS_NAME") or DEFAULT_RAG_CORPUS_NAME

    if project_id:
        try:
            import vertexai
            try:
                from vertexai import rag
            except ImportError:
                from vertexai.preview import rag

            loc_match = re.search(r"/locations/([^/]+)/ragCorpora/", rag_corpus)
            corpus_location = loc_match.group(1) if loc_match else region
            creds = _get_gcp_credentials(project_id)
            vertexai.init(
                project=project_id, location=corpus_location, credentials=creds
            )
            if not rag_corpus:
                for c in rag.list_corpora():
                    if c.display_name == "erein-demo2-rag-corpus" or not rag_corpus:
                        rag_corpus = c.name
                        if c.display_name == "erein-demo2-rag-corpus":
                            break
            if rag_corpus:
                response = rag.retrieval_query(
                    rag_resources=[rag.RagResource(rag_corpus=rag_corpus)],
                    text=consulta,
                )
                contexts = getattr(getattr(response, "contexts", None), "contexts", [])
                fragments: list[dict[str, Any]] = []
                for ctx in contexts:
                    fragments.append(
                        {
                            "source_uri": getattr(ctx, "source_uri", ""),
                            "source_display_name": getattr(
                                ctx, "source_display_name", "03_Conocimiento/DEMO-2"
                            ),
                            "score": getattr(ctx, "score", None),
                            "texto": getattr(ctx, "text", ""),
                        }
                    )
                if fragments:
                    return json.dumps(
                        {
                            "status": "SUCCESS",
                            "engine": "vertex_ai_rag_engine",
                            "rag_corpus": rag_corpus,
                            "version_politica": "DEMO-2",
                            "consulta": consulta,
                            "fragmentos_recuperados": fragments,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
            fallback_reason = "No matching fragments returned from RAG corpus"
        except Exception as exc:
            # Fall back cleanly to reading the 03_Conocimiento PDFs directly if RAG Engine is unreachable
            fallback_reason = str(exc)
    else:
        fallback_reason = "PROJECT_ID / GOOGLE_CLOUD_PROJECT not set in environment"

    # Local fallback over the 2 PDFs in 03_Conocimiento
    docs_output = []
    for pdf_name in (
        "Politica_Revision_Operativa_DEMO2.pdf",
        "Procedimiento_Revision_Operativa_DEMO2.pdf",
    ):
        pdf_path = CONOCIMIENTO_DIR / pdf_name
        if pdf_path.exists():
            if PdfReader is not None:
                reader = PdfReader(str(pdf_path))
                for idx, page in enumerate(reader.pages, start=1):
                    docs_output.append(
                        {
                            "documento": pdf_name,
                            "pagina": idx,
                            "texto": (page.extract_text() or "").strip(),
                        }
                    )
            else:
                pdf_bytes = pdf_path.read_bytes()
                page_idx = 0
                for m in re.finditer(
                    rb"stream\r?\n(.*?)\r?\n?endstream", pdf_bytes, re.DOTALL
                ):
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
                    if dec is None or (
                        b"BT" not in dec and b"Tj" not in dec and b"TJ" not in dec
                    ):
                        continue
                    parts = []
                    for sm in re.finditer(rb"\((?:\\.|[^\\()])*\)", dec):
                        s = sm.group(0)[1:-1]
                        s = re.sub(
                            rb"\\([0-7]{1,3})",
                            lambda om: bytes([int(om.group(1), 8)]),
                            s,
                        )
                        s = (
                            s.replace(b"\\(", b"(")
                            .replace(b"\\)", b")")
                            .replace(b"\\\\", b"\\")
                        )
                        parts.append(s.decode("cp1252", errors="ignore"))
                    if parts:
                        page_idx += 1
                        docs_output.append(
                            {
                                "documento": pdf_name,
                                "pagina": page_idx,
                                "texto": "\n".join(parts),
                            }
                        )

    if not docs_output:
        docs_output = list(EMBEDDED_CONOCIMIENTO_DOCS)

    return json.dumps(
        {
            "status": "SUCCESS",
            "engine": "local_03_conocimiento_pdf_fallback",
            "fallback_note": fallback_reason,
            "version_politica": "DEMO-2",
            "consulta": consulta,
            "documentos_conocimiento": docs_output,
        },
        ensure_ascii=False,
        indent=2,
    )
