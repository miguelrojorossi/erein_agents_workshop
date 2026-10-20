"""Native ADK Agent Tool for querying EREIN DEMO-2 Policies via Vertex AI RAG Engine."""

import json
import os
import re
import subprocess
from typing import Any
import warnings

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

DEFAULT_RAG_CORPUS_NAME = ""
DEFAULT_PROJECT_ID = ""


def _get_gcp_credentials(project_id: str):
    """Returns Google Cloud credentials with cloud-platform scope (supports Cloud Workstations & Agent Runtime)."""
    import google.auth
    from google.auth import credentials as auth_credentials
    import google.auth.transport.requests
    from google.oauth2 import credentials as oauth2_credentials

    # In Cloud Run / Agent Runtime, fetch an unbound token ONLY for the RAG client
    # and restore os.environ immediately so Agent Runtime's native mTLS telemetry/trace exporter is unaffected.
    if os.getenv("K_SERVICE") or os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID"):
        prev_bound = os.environ.get("GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN")
        prev_share = os.environ.get(
            "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"
        )
        try:
            os.environ["GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN"] = "false"
            os.environ[
                "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"
            ] = "false"
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            creds.refresh(google.auth.transport.requests.Request())
            if getattr(creds, "token", None):
                return oauth2_credentials.Credentials(
                    token=creds.token, quota_project_id=project_id
                )
            return creds
        finally:
            if prev_bound is None:
                os.environ.pop("GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN", None)
            else:
                os.environ["GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN"] = prev_bound
            if prev_share is None:
                os.environ.pop(
                    "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES", None
                )
            else:
                os.environ[
                    "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"
                ] = prev_share

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

    if not project_id:
        try:
            import google.auth

            _, detected_project = google.auth.default()
            if detected_project:
                project_id = detected_project
        except Exception:
            pass

    if not project_id:
        return json.dumps(
            {
                "status": "RAG_ERROR",
                "message": "PROJECT_ID / GOOGLE_CLOUD_PROJECT no está configurado para Vertex AI RAG Engine.",
            },
            ensure_ascii=False,
            indent=2,
        )

    try:
        from google.auth.transport.requests import AuthorizedSession

        loc_match = re.search(
            r"^projects/([^/]+)/locations/([^/]+)/ragCorpora/", rag_corpus
        )
        corpus_project = loc_match.group(1) if loc_match else project_id
        corpus_location = loc_match.group(2) if loc_match else region
        parent = f"projects/{corpus_project}/locations/{corpus_location}"
        base_url = f"https://{corpus_location}-aiplatform.googleapis.com/v1"

        creds = _get_gcp_credentials(project_id)
        authed_session = AuthorizedSession(creds)

        if not rag_corpus:
            list_resp = authed_session.get(
                f"{base_url}/{parent}/ragCorpora", timeout=30
            )
            list_resp.raise_for_status()
            for c in list_resp.json().get("ragCorpora", []):
                if c.get("displayName") == "erein-demo2-rag-corpus" or not rag_corpus:
                    rag_corpus = c.get("name", "")
                    if c.get("displayName") == "erein-demo2-rag-corpus":
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

        payload = {
            "vertexRagStore": {
                "ragResources": [{"ragCorpus": rag_corpus}],
            },
            "query": {
                "text": (
                    f"{consulta} Política de revisión operativa DEMO-2 "
                    "Procedimiento operativo DEMO-2"
                ),
                "ragRetrievalConfig": {"topK": 10},
            },
        }
        retrieve_resp = authed_session.post(
            f"{base_url}/{parent}:retrieveContexts",
            json=payload,
            timeout=30,
        )
        retrieve_resp.raise_for_status()
        contexts = (
            retrieve_resp.json().get("contexts", {}).get("contexts", [])
        )
        fragments: list[dict[str, Any]] = []
        for ctx in contexts:
            fragments.append(
                {
                    "source_uri": ctx.get("sourceUri")
                    or ctx.get("source_uri", ""),
                    "source_display_name": ctx.get("sourceDisplayName")
                    or ctx.get("source_display_name", "03_Conocimiento/DEMO-2"),
                    "score": (
                        ctx.get("score")
                        if "score" in ctx
                        else ctx.get("distance")
                    ),
                    "texto": ctx.get("text", ""),
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
