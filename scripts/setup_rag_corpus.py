#!/usr/bin/env python3
"""Creates and populates the Vertex AI RAG Engine corpus for EREIN DEMO-2 policies.

Indexes ONLY the two policy/procedure documents in 03_Conocimiento:
  - Politica_Revision_Operativa_DEMO2.pdf
  - Procedimiento_Revision_Operativa_DEMO2.pdf
"""

import argparse
import base64
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import warnings
import zlib

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONOCIMIENTO_DIR = (
    REPO_ROOT / "EREIN_CdU_Revision_Operativa_Agentica" / "03_Conocimiento"
)


def _get_gcloud_credentials(project_id: str):
    """Builds refreshable OAuth2 credentials from `gcloud auth print-access-token`."""
    from google.auth import credentials as auth_credentials

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
        import google.auth

        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            quota_project_id=project_id,
        )
        return creds


def _extract_pdf_text(pdf_path: Path) -> str:
    """Extracts plain text from a local PDF file for fallback RAG upload."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        return "\n\n".join(
            f"[Documento: {pdf_path.name} - Página {idx}]\n{(page.extract_text() or '').strip()}"
            for idx, page in enumerate(reader.pages, start=1)
        )
    except Exception:
        pdf_bytes = pdf_path.read_bytes()
        pages = []
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
            parts = []
            for sm in re.finditer(rb"\((?:\\.|[^\\()])*\)", dec):
                s = sm.group(0)[1:-1]
                s = re.sub(rb"\\([0-7]{1,3})", lambda om: bytes([int(om.group(1), 8)]), s)
                s = s.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
                parts.append(s.decode("cp1252", errors="ignore"))
            if parts:
                pages.append(
                    f"[Documento: {pdf_path.name} - Página {len(pages) + 1}]\n"
                    + "\n".join(parts)
                )
        return "\n\n".join(pages)


def setup_rag_corpus(
    project_id: str,
    region: str,
    gcs_bucket: str,
    env_file: str,
    corpus_display_name: str = "erein-demo2-rag-corpus",
) -> str:
    try:
        import vertexai
        try:
            from vertexai import rag
        except ImportError:
            from vertexai.preview import rag
    except ImportError:
        print(
            "WARNING: google-cloud-aiplatform is not installed in the current Python env.",
            file=sys.stderr,
        )
        return ""

    creds = _get_gcloud_credentials(project_id)

    # New GCP sandbox projects restrict RAG Engine in europe-west1 to allowlisted projects,
    # whereas europe-west4 and europe-west3 are open in EU. Check europe-west4 first if region is europe-west1.
    candidate_regions = []
    preferred_order = (
        ("europe-west4", "europe-west3", region)
        if region == "europe-west1"
        else (region, "europe-west4", "europe-west3")
    )
    for loc in preferred_order:
        if loc and loc not in candidate_regions:
            candidate_regions.append(loc)

    existing_corpus = None
    active_region = candidate_regions[0]

    for loc in candidate_regions:
        try:
            print(
                f"Initializing Vertex AI RAG Engine in project={project_id}, location={loc}..."
            )
            vertexai.init(project=project_id, location=loc, credentials=creds)
            for c in rag.list_corpora():
                if c.display_name == corpus_display_name:
                    existing_corpus = c
                    active_region = loc
                    print(f"Found existing RAG corpus in {loc}: {c.name}")
                    break
            if existing_corpus is not None:
                break

            existing_corpus = rag.create_corpus(
                display_name=corpus_display_name,
                description=(
                    "EREIN DEMO-2 Operational Review Policy (P01-P13) and Procedure "
                    "(03_Conocimiento only)"
                ),
            )
            active_region = loc
            print(f"Created new RAG corpus in {loc}: {existing_corpus.name}")
            break
        except Exception as exc:
            print(
                f"  -> Notice: Could not provision RAG corpus in {loc} ({exc}). Trying next EU region..."
            )
            continue

    if existing_corpus is None:
        raise RuntimeError(
            "Could not create or list Vertex AI RAG corpus in any candidate EU region."
        )

    transformation_config = rag.TransformationConfig(
        chunking_config=rag.ChunkingConfig(
            chunk_size=512,
            chunk_overlap=100,
        )
    )

    # Check if files are already uploaded to avoid duplicate indexing on re-runs
    existing_files = list(rag.list_files(corpus_name=existing_corpus.name))
    if len(existing_files) >= 2:
        print(
            f"RAG corpus already contains {len(existing_files)} files; skipping re-upload."
        )
    else:
        pdf_names = [
            "Politica_Revision_Operativa_DEMO2.pdf",
            "Procedimiento_Revision_Operativa_DEMO2.pdf",
        ]
        gcs_uris = [f"gs://{gcs_bucket}/03_Conocimiento/{name}" for name in pdf_names]
        imported = False

        # 1. Try importing from Cloud Storage via rag.import_files
        try:
            print(
                f"Importing 03_Conocimiento PDFs from gs://{gcs_bucket}/03_Conocimiento/ into {existing_corpus.name}..."
            )
            import_resp = rag.import_files(
                corpus_name=existing_corpus.name,
                paths=gcs_uris,
                transformation_config=transformation_config,
            )
            imported_count = getattr(import_resp, "imported_rag_files_count", 0)
            print(f"  -> Imported {imported_count} file(s) from GCS.")
            if imported_count > 0 or len(list(rag.list_files(corpus_name=existing_corpus.name))) >= 2:
                imported = True
        except Exception as exc:
            print(f"  -> GCS import notice ({exc}); falling back to direct upload...")

        # 2. Fallback to direct upload with explicit transformation_config
        if not imported:
            for pdf_name in pdf_names:
                local_pdf = CONOCIMIENTO_DIR / pdf_name
                try:
                    print(f"  -> Uploading {pdf_name} to {existing_corpus.name}...")
                    rag.upload_file(
                        corpus_name=existing_corpus.name,
                        path=str(local_pdf),
                        display_name=pdf_name,
                        description=f"EREIN DEMO-2 {pdf_name}",
                        transformation_config=transformation_config,
                    )
                except Exception:
                    # Fallback to uploading extracted plain text if PDF direct upload fails
                    txt_name = pdf_name.replace(".pdf", ".txt")
                    print(
                        f"  -> Uploading extracted text {txt_name} to {existing_corpus.name}..."
                    )
                    text_content = _extract_pdf_text(local_pdf)
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".txt", delete=False, encoding="utf-8"
                    ) as tmp_f:
                        tmp_f.write(text_content)
                        tmp_path = tmp_f.name
                    try:
                        rag.upload_file(
                            corpus_name=existing_corpus.name,
                            path=tmp_path,
                            display_name=pdf_name,
                            description=f"EREIN DEMO-2 {pdf_name}",
                            transformation_config=transformation_config,
                        )
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)

    print("Verifying RAG retrieval with test query ('P01 ingreso recurrente')...")
    try:
        response = rag.retrieval_query(
            rag_resources=[rag.RagResource(rag_corpus=existing_corpus.name)],
            text="P01 ingreso recurrente y P02 pagos no recurrentes bonus",
        )
        contexts = getattr(getattr(response, "contexts", None), "contexts", [])
        print(f"Retrieved {len(contexts)} context chunk(s) from Vertex AI RAG Engine.")
    except Exception as exc:
        print(f"  -> Note on test query: {exc}")

    # Persist environment variables (keep primary workshop REGION as europe-west1 for Cloud Run/BQ/GCS)
    primary_region = os.environ.get("REGION", "europe-west1")
    os.makedirs(os.path.dirname(os.path.abspath(env_file)), exist_ok=True)
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(f'export PROJECT_ID="{project_id}"\n')
        f.write(f'export REGION="{primary_region}"\n')
        f.write(f'export RAG_LOCATION="{active_region}"\n')
        f.write('export BQ_DATASET="erein_operaciones"\n')
        f.write(f'export GCS_BUCKET="{gcs_bucket}"\n')
        f.write(f'export RAG_CORPUS_NAME="{existing_corpus.name}"\n')

    print(f"Saved RAG_CORPUS_NAME={existing_corpus.name} to {env_file}")
    return existing_corpus.name


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision Vertex AI RAG Engine corpus for EREIN DEMO-2."
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("PROJECT_ID", ""),
        help="Google Cloud Project ID",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("RAG_LOCATION", "europe-west4"),
        help="Google Cloud region for RAG Engine (default: europe-west4)",
    )
    parser.add_argument(
        "--gcs-bucket",
        default=os.environ.get("GCS_BUCKET", ""),
        help="GCS bucket holding 03_Conocimiento PDFs",
    )
    parser.add_argument(
        "--env-file",
        default=".env.workshop",
        help="Path to write workshop env vars",
    )
    args = parser.parse_args()

    if not args.project_id:
        print("ERROR: --project-id or PROJECT_ID env var is required.", file=sys.stderr)
        sys.exit(1)
    bucket = args.gcs_bucket or f"{args.project_id}-erein-docs"
    setup_rag_corpus(
        project_id=args.project_id,
        region=args.region,
        gcs_bucket=bucket,
        env_file=args.env_file,
    )


if __name__ == "__main__":
    main()
