#!/usr/bin/env python3
"""Creates and populates the Vertex AI RAG Engine corpus for EREIN DEMO-2 policies.

Indexes ONLY the two policy/procedure documents in 03_Conocimiento:
  - Politica_Revision_Operativa_DEMO2.pdf
  - Procedimiento_Revision_Operativa_DEMO2.pdf
"""

import argparse
import os
import sys


def setup_rag_corpus(
    project_id: str,
    region: str,
    gcs_bucket: str,
    env_file: str,
    corpus_display_name: str = "erein-demo2-rag-corpus",
) -> str:
    try:
        import vertexai
        from vertexai.preview import rag
    except ImportError:
        print(
            "WARNING: google-cloud-aiplatform is not installed in the current Python env.",
            file=sys.stderr,
        )
        return ""

    print(f"Initializing Vertex AI in project={project_id}, location={region}...")
    vertexai.init(project=project_id, location=region)

    existing_corpus = None
    for c in rag.list_corpora():
        if c.display_name == corpus_display_name:
            existing_corpus = c
            print(f"Found existing RAG corpus: {c.name}")
            break

    if existing_corpus is None:
        embedding_model_config = rag.EmbeddingModelConfig(
            publisher_model="publishers/google/models/text-multilingual-embedding-002"
        )
        existing_corpus = rag.create_corpus(
            display_name=corpus_display_name,
            description=(
                "EREIN DEMO-2 Operational Review Policy (P01-P13) and Procedure "
                "(03_Conocimiento only)"
            ),
            embedding_model_config=existing_model_config
            if (existing_model_config := embedding_model_config)
            else None,
        )
        print(f"Created new RAG corpus: {existing_corpus.name}")

    gcs_uris = [
        f"gs://{gcs_bucket}/03_Conocimiento/Politica_Revision_Operativa_DEMO2.pdf",
        f"gs://{gcs_bucket}/03_Conocimiento/Procedimiento_Revision_Operativa_DEMO2.pdf",
    ]
    print(f"Importing 03_Conocimiento PDFs into {existing_corpus.name}...")
    rag.import_files(
        existing_corpus.name,
        gcs_uris,
        chunk_size=512,
        chunk_overlap=100,
    )

    print("Verifying RAG retrieval with test query ('P01 ingreso recurrente')...")
    response = rag.retrieval_query(
        rag_resources=[rag.RagResource(rag_corpus=existing_corpus.name)],
        text="P01 ingreso recurrente y P02 pagos no recurrentes bonus",
        similarity_top_k=2,
    )
    contexts = getattr(getattr(response, "contexts", None), "contexts", [])
    print(f"Retrieved {len(contexts)} context chunk(s) from Vertex AI RAG Engine.")

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
