#!/usr/bin/env bash
# ==============================================================================
# EREIN Agent Platform Workshop - Sandbox Environment Bootstrap Script
# ==============================================================================
# Prepares the Google Cloud Sandbox Project for the 4-hour Hands-On Workshop:
#   1. Enables required Google Cloud APIs
#   2. Creates the BigQuery dataset (erein_operaciones) & loads 01_Datos CSVs
#   3. Creates the Cloud Storage bucket & uploads 02_Documentos and 03_Conocimiento
#   4. Creates the Vertex AI RAG Engine corpus (03_Conocimiento PDFs)
#   5. Writes environment variables to .env.workshop
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DATA_ROOT="${REPO_ROOT}/EREIN_CdU_Revision_Operativa_Agentica"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "ERROR: PROJECT_ID is not set. Run: export PROJECT_ID=\"your-project-id\""
  exit 1
fi

REGION="${REGION:-europe-west1}"
BQ_LOCATION="${BQ_LOCATION:-europe-west1}"
BQ_DATASET="${BQ_DATASET:-erein_operaciones}"
GCS_BUCKET="${GCS_BUCKET:-${PROJECT_ID}-erein-docs}"

echo "=============================================================================="
echo " Bootstrapping EREIN Operational Review Workshop Environment"
echo "------------------------------------------------------------------------------"
echo " Project ID : ${PROJECT_ID}"
echo " Region     : ${REGION}"
echo " BQ Dataset : ${BQ_DATASET} (${BQ_LOCATION})"
echo " GCS Bucket : gs://${GCS_BUCKET}"
echo "=============================================================================="

# 1. Enable Required APIs
echo "[1/5] Enabling Google Cloud APIs..."
gcloud services enable \
  aiplatform.googleapis.com \
  bigquery.googleapis.com \
  storage.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  compute.googleapis.com \
  dns.googleapis.com \
  networkservices.googleapis.com \
  networksecurity.googleapis.com \
  modelarmor.googleapis.com \
  discoveryengine.googleapis.com \
  --project="${PROJECT_ID}"

# 2. Create BigQuery Dataset and Load 01_Datos Tables
echo "[2/5] Creating BigQuery dataset '${BQ_DATASET}' and loading 01_Datos..."
if ! bq --project_id="${PROJECT_ID}" ls --dataset_id="${BQ_DATASET}" >/dev/null 2>&1; then
  bq --project_id="${PROJECT_ID}" --location="${BQ_LOCATION}" mk --dataset \
    --description="EREIN Operational Review Synthetic Dataset (DEMO-2)" \
    "${PROJECT_ID}:${BQ_DATASET}"
fi

TABLES=(
  "expedientes"
  "evidencias"
  "documentos"
  "transacciones"
  "resumen_cuentas"
  "historial_laboral"
  "politica"
)

for TABLE in "${TABLES[@]}"; do
  CSV_FILE="${DATA_ROOT}/01_Datos/${TABLE}.csv"
  SCHEMA_FILE="${DATA_ROOT}/01_Datos/${TABLE}.schema.json"
  echo "  -> Loading table ${BQ_DATASET}.${TABLE}..."
  bq --project_id="${PROJECT_ID}" --location="${BQ_LOCATION}" load \
    --replace \
    --source_format=CSV \
    --skip_leading_rows=1 \
    --encoding=UTF-8 \
    --null_marker="" \
    --allow_quoted_newlines \
    "${PROJECT_ID}:${BQ_DATASET}.${TABLE}" \
    "${CSV_FILE}" \
    "${SCHEMA_FILE}"
done

echo "  -> Creating empty table ${BQ_DATASET}.acciones..."
bq --project_id="${PROJECT_ID}" --location="${BQ_LOCATION}" rm -f -t "${PROJECT_ID}:${BQ_DATASET}.acciones" >/dev/null 2>&1 || true
bq --project_id="${PROJECT_ID}" --location="${BQ_LOCATION}" mk --table \
  --schema="${DATA_ROOT}/01_Datos/acciones.schema.json" \
  --description="Operational review actions recorded by the agent" \
  "${PROJECT_ID}:${BQ_DATASET}.acciones"

# 3. Create Cloud Storage Bucket and Upload Authorized PDFs
echo "[3/5] Creating Cloud Storage bucket 'gs://${GCS_BUCKET}' and uploading PDFs..."
if ! gcloud storage buckets describe "gs://${GCS_BUCKET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${GCS_BUCKET}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
fi

echo "  -> Uploading 02_Documentos (preserving relative paths)..."
gcloud storage cp -r "${DATA_ROOT}/02_Documentos" "gs://${GCS_BUCKET}/" --project="${PROJECT_ID}"

echo "  -> Uploading 03_Conocimiento (policy and procedure PDFs for RAG)..."
gcloud storage cp -r "${DATA_ROOT}/03_Conocimiento" "gs://${GCS_BUCKET}/" --project="${PROJECT_ID}"

echo "  -> Note: 04_Simulacion_Cliente and 05_Evaluacion are intentionally kept separate."

# 4. Provision Vertex AI RAG Engine Corpus for 03_Conocimiento
echo "[4/5] Setting up Vertex AI RAG Engine corpus for 03_Conocimiento..."
if command -v uv >/dev/null 2>&1; then
  uv run --with google-cloud-aiplatform python3 "${SCRIPT_DIR}/setup_rag_corpus.py" \
    --project-id="${PROJECT_ID}" \
    --region="${REGION}" \
    --gcs-bucket="${GCS_BUCKET}" \
    --env-file="${REPO_ROOT}/.env.workshop"
else
  python3 "${SCRIPT_DIR}/setup_rag_corpus.py" \
    --project-id="${PROJECT_ID}" \
    --region="${REGION}" \
    --gcs-bucket="${GCS_BUCKET}" \
    --env-file="${REPO_ROOT}/.env.workshop"
fi

# 5. Write Workshop Environment Configuration
echo "[5/5] Finalizing ${REPO_ROOT}/.env.workshop..."
grep -q "^export PROJECT_ID=" "${REPO_ROOT}/.env.workshop" 2>/dev/null || {
  cat >> "${REPO_ROOT}/.env.workshop" <<EOF
export PROJECT_ID="${PROJECT_ID}"
export REGION="${REGION}"
export BQ_DATASET="${BQ_DATASET}"
export GCS_BUCKET="${GCS_BUCKET}"
EOF
}

echo "=============================================================================="
echo " Bootstrap Complete!"
echo " Load your environment variables with:"
echo "   source ${REPO_ROOT}/.env.workshop"
echo "=============================================================================="
