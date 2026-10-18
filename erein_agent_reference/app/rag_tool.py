"""Native ADK Agent Tool for querying EREIN DEMO-2 Policies via Vertex AI RAG Engine."""

import json
import os
from pathlib import Path
import subprocess
from typing import Any

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONOCIMIENTO_DIR = (
    REPO_ROOT / "EREIN_CdU_Revision_Operativa_Agentica" / "03_Conocimiento"
)


def consultar_politica(consulta: str) -> str:
    """Recupera las reglas de la Política de Revisión Operativa DEMO-2 (P01 a P13) y el Procedimiento Operativo DEMO-2 mediante Vertex AI RAG Engine.

    Args:
        consulta: Términos de búsqueda o descripción de la discrepancia/alerta a consultar en la política (ej. 'ingreso recurrente nóminas bonus P01 P02', 'apuestas categoría GAMBLING P07 P10 P13', 'alquiler no declarado P11', 'antigüedad grupo empresarial P12', 'documento ilegible plazo P08').
    """
    project_id = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
    region = os.getenv("REGION", "europe-west1")
    rag_corpus = os.getenv("RAG_CORPUS_NAME", "")

    if project_id and rag_corpus:
        try:
            import vertexai
            from vertexai.preview import rag

            vertexai.init(project=project_id, location=region)
            response = rag.retrieval_query(
                rag_resources=[rag.RagResource(rag_corpus=rag_corpus)],
                text=consulta,
                similarity_top_k=5,
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
        except Exception as exc:
            # Fall back cleanly to reading the 03_Conocimiento PDFs directly if RAG Engine is unreachable
            fallback_reason = str(exc)
    else:
        fallback_reason = "RAG_CORPUS_NAME not set in environment"

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
                res = subprocess.run(
                    ["pdftotext", str(pdf_path), "-"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                docs_output.append(
                    {
                        "documento": pdf_name,
                        "pagina": 1,
                        "texto": res.stdout.strip(),
                    }
                )

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
