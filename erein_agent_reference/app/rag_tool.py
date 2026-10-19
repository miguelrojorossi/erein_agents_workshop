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

DEFAULT_RAG_CORPUS_NAME = ""
DEFAULT_PROJECT_ID = ""


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


def consultar_politica(consulta: str) -> str:
    """Recupera las reglas de la Política de Revisión Operativa DEMO-2 (P01 a P13) y el Procedimiento Operativo DEMO-2 desde el corpus de Vertex AI RAG Engine.

    Args:
        consulta: Términos de búsqueda o descripción de la discrepancia, alerta o procedimiento a consultar en Vertex AI RAG Engine (ej. 'reglas ingreso recurrente nóminas bonus procedimiento estados permitidos').
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
    use_local = os.getenv("USE_LOCAL_DATA", "").lower() in ("true", "1", "yes")

    if project_id and not use_local:
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
            if not rag_corpus:
                return json.dumps(
                    {
                        "status": "RAG_ERROR",
                        "message": (
                            f"No se encontró el corpus 'erein-demo2-rag-corpus' en {corpus_location} "
                            f"(proyecto {project_id}). Define RAG_CORPUS_NAME o ejecuta setup_rag_corpus.py."
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            retrieval_cfg = rag.RagRetrievalConfig(top_k=10)
            response = rag.retrieval_query(
                rag_resources=[rag.RagResource(rag_corpus=rag_corpus)],
                text=f"{consulta} Política de revisión operativa DEMO-2 Procedimiento operativo DEMO-2",
                rag_retrieval_config=retrieval_cfg,
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
        except Exception as exc:
            return json.dumps(
                {
                    "status": "RAG_ERROR",
                    "engine": "vertex_ai_rag_engine",
                    "rag_corpus": rag_corpus,
                    "consulta": consulta,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )

    # Local offline mode ONLY for automated unit test suite (scripts/test_mcp_tools.py with USE_LOCAL_DATA=true)
    if use_local:
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
        return json.dumps(
            {
                "status": "SUCCESS",
                "engine": "local_unit_test_03_conocimiento",
                "version_politica": "DEMO-2",
                "consulta": consulta,
                "fragmentos_recuperados": docs_output,
            },
            ensure_ascii=False,
            indent=2,
        )

    return json.dumps(
        {
            "status": "RAG_ERROR",
            "message": "PROJECT_ID / GOOGLE_CLOUD_PROJECT is not configured for Vertex AI RAG Engine.",
        },
        ensure_ascii=False,
        indent=2,
    )
