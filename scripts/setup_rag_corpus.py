#!/usr/bin/env python3
"""Creates and populates the Vertex AI RAG Engine corpus for EREIN DEMO-2 policies.

Indexes ONLY the two policy/procedure documents in 03_Conocimiento:
  - Politica_Revision_Operativa_DEMO2.pdf
  - Procedimiento_Revision_Operativa_DEMO2.pdf
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import warnings

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONOCIMIENTO_DIR = (
    REPO_ROOT / "EREIN_CdU_Revision_Operativa_Agentica" / "03_Conocimiento"
)


def _get_gcloud_credentials(project_id: str):
    """Builds refreshable OAuth2 credentials from `gcloud auth print-access-token`.

    On Cloud Workstations, `google.auth.default()` falls back to the VM's GCE
    metadata server (which lacks `cloud-platform` scope and raises
    ACCESS_TOKEN_SCOPE_INSUFFICIENT) when only `gcloud auth login` was run.
    Using `gcloud auth print-access-token` uses the user's CLI session directly.
    """
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

    # Candidate regions in order of preference (some new projects restrict RagManagedDb in europe-west1)
    candidate_regions = []
    for loc in (region, "europe-west4", "europe-west3", "us-central1"):
        if loc and loc not in candidate_regions:
            candidate_regions.append(loc)

    existing_corpus = None
    active_region = region

    for loc in candidate_regions:
        try:
            print(f"Initializing Vertex AI RAG Engine in project={project_id}, location={loc}...")
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
            print(f"  -> Notice: Could not provision RAG corpus in {loc} ({exc}). Trying fallback region...")
            continue

    if existing_corpus is None:
        raise RuntimeError("Could not create or list Vertex AI RAG corpus in any candidate region.")

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
        if active_region == region:
            try:
                print(f"Importing 03_Conocimiento PDFs from GCS into {existing_corpus.name}...")
                transformation_config = rag.TransformationConfig(
                    chunking_config=rag.ChunkingConfig(
                        chunk_size=512,
                        chunk_overlap=100,
                    )
                )
                rag.import_files(
                    corpus_name=existing_corpus.name,
                    paths=gcs_uris,
                    transformation_config=transformation_config,
                )
                imported = True
            except Exception as exc:
                print(
                    f"  -> GCS import fallback ({exc}); uploading local PDFs directly via rag.upload_file..."
                )

        if not imported:
            for pdf_name in pdf_names:
                local_pdf = CONOCIMIENTO_DIR / pdf_name
                print(f"  -> Uploading {pdf_name} to {existing_corpus.name}...")
                rag.upload_file(
                    corpus_name=existing_corpus.name,
                    path=str(local_pdf),
                    display_name=pdf_name,
                    description=f"EREIN DEMO-2 {pdf_name}",
                )

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

    # Persist environment variables
    os.makedirs(os.path.dirname(os.path.abspath(env_file)), exist_ok=True)
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(f'export PROJECT_ID="{project_id}"\n')
        f.write(f'export REGION="{region}"\n')
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
        default=os.environ.get("REGION", "europe-west1"),
        help="Google Cloud region (default: europe-west1)",
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
