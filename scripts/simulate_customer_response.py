#!/usr/bin/env python3
"""Simulates customer document submission / clarification for EREIN PoC cases.

Specifically implements Section 4 ('Simulación de SYN-002') of Guia_Implementacion_PoC.pdf:
  - Keeps 04_Simulacion_Cliente isolated from initial agent access.
  - After the agent requests the missing September payslip for SYN-002 (setting state
    to 'Pendiente de evidencia'), this script:
      1. Uploads 04_Simulacion_Cliente/SYN-002_septiembre.pdf to Cloud Storage (and/or
         enables it in the local runtime state).
      2. Incorporates the response evidence SYN-002-E05 into `evidencias` without
         duplicating keys.
      3. Incorporates the document reference DOC-SYN-002-E05 (and updates DOC-SYN-002-E04)
         in `documentos` so the agent can read the September payslip upon re-analysis.
  - Also supports `--case SYN-007` (customer clarification message on rental income)
    and `--reset` to restore the initial Phase 1 state.
"""

import argparse
import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = REPO_ROOT / "EREIN_CdU_Revision_Operativa_Agentica"
LOCAL_STATE_FILE = REPO_ROOT / "mcp_erein_operaciones" / "data" / "runtime_state.json"


def update_local_state(case_id: str, reset: bool = False) -> None:
    """Updates local runtime state file so local/fallback MCP mode reflects the event."""
    LOCAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {"syn002_responded": False, "syn007_clarified": False, "acciones": []}
    if LOCAL_STATE_FILE.exists():
        try:
            state = json.loads(LOCAL_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    if reset:
        if case_id in ("SYN-002", "ALL"):
            state["syn002_responded"] = False
        if case_id in ("SYN-007", "ALL"):
            state["syn007_clarified"] = False
        if case_id == "ALL":
            state["acciones"] = []
        else:
            state["acciones"] = [
                a for a in state.get("acciones", []) if a.get("caso_id") != case_id
            ]
        LOCAL_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[Local State] Reset simulation state for {case_id}.")
        return

    if case_id == "SYN-002":
        state["syn002_responded"] = True
        print(
            "[Local State] Activated SYN-002 Phase 2: "
            "SYN-002-E05 and DOC-SYN-002-E05 (SYN-002_septiembre.pdf) are now available."
        )
    elif case_id == "SYN-007":
        state["syn007_clarified"] = True
        print(
            "[Local State] Activated SYN-007 clarification event RESP-SYN-007-01."
        )

    LOCAL_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def update_cloud_state(
    project_id: str,
    dataset_id: str,
    gcs_bucket: str,
    case_id: str,
    reset: bool = False,
) -> None:
    """Uploads the reserved PDF to GCS and updates BigQuery tables idempotently."""
    try:
        from google.cloud import bigquery
        from google.cloud import storage
    except ImportError:
        print(
            "[Cloud State] google-cloud-bigquery / google-cloud-storage not installed; "
            "skipping cloud update (local state updated)."
        )
        return

    bq = bigquery.Client(project=project_id)
    table_evidencias = f"`{project_id}.{dataset_id}.evidencias`"
    table_documentos = f"`{project_id}.{dataset_id}.documentos`"

    if reset:
        if case_id in ("SYN-002", "ALL"):
            bq.query(
                f"DELETE FROM {table_evidencias} WHERE evidencia_id = 'SYN-002-E05'"
            ).result()
            bq.query(
                f"DELETE FROM {table_documentos} WHERE documento_id = 'DOC-SYN-002-E05'"
            ).result()
            bq.query(
                f"""
                UPDATE {table_documentos}
                SET ruta_relativa = NULL, disponibilidad = 'Ausente', disponible_inicial = FALSE
                WHERE documento_id = 'DOC-SYN-002-E04'
                """
            ).result()
            print("[BigQuery] Restored SYN-002 to initial Phase 1 state.")
        return

    if case_id == "SYN-002":
        # 1. Upload reserved PDF to GCS
        pdf_local = DATA_ROOT / "04_Simulacion_Cliente" / "SYN-002_septiembre.pdf"
        gcs_rel_path = "04_Simulacion_Cliente/SYN-002_septiembre.pdf"
        storage_client = storage.Client(project=project_id)
        bucket = storage_client.bucket(gcs_bucket)
        blob = bucket.blob(gcs_rel_path)
        blob.upload_from_filename(str(pdf_local), content_type="application/pdf")
        print(f"[Cloud Storage] Uploaded gs://{gcs_bucket}/{gcs_rel_path}")

        # 2. Idempotently insert SYN-002-E05 in evidencias
        merge_evidencia_sql = f"""
        MERGE {table_evidencias} T
        USING (
          SELECT
            'SYN-002-E05' AS evidencia_id,
            'SYN-002' AS caso_id,
            'Respuesta simulada' AS fase,
            'Nómina' AS tipo_fuente,
            DATE '2026-09-30' AS fecha_evidencia,
            'Septiembre' AS periodo,
            CAST(2500 AS NUMERIC) AS importe_eur,
            'Disponible' AS disponibilidad,
            'Nuevo salario neto ordinario de 2500 EUR. Aportar solo después de la solicitud del agente.' AS contenido
        ) S
        ON T.evidencia_id = S.evidencia_id
        WHEN NOT MATCHED THEN
          INSERT (evidencia_id, caso_id, fase, tipo_fuente, fecha_evidencia, periodo, importe_eur, disponibilidad, contenido)
          VALUES (S.evidencia_id, S.caso_id, S.fase, S.tipo_fuente, S.fecha_evidencia, S.periodo, S.importe_eur, S.disponibilidad, S.contenido)
        """
        bq.query(merge_evidencia_sql).result()
        print("[BigQuery] Upserted evidence SYN-002-E05 into evidencias (no duplicate keys).")

        # 3. Idempotently insert DOC-SYN-002-E05 and update missing reference DOC-SYN-002-E04
        merge_doc_sql = f"""
        MERGE {table_documentos} T
        USING (
          SELECT
            'DOC-SYN-002-E05' AS documento_id,
            'SYN-002' AS caso_id,
            'SYN-002-E05' AS evidencia_id,
            'Respuesta simulada' AS fase,
            'Nómina' AS tipo_documento,
            'Septiembre' AS periodo,
            '04_Simulacion_Cliente/SYN-002_septiembre.pdf' AS ruta_relativa,
            'Disponible' AS disponibilidad,
            1 AS pagina,
            TRUE AS disponible_inicial
        ) S
        ON T.documento_id = S.documento_id
        WHEN NOT MATCHED THEN
          INSERT (documento_id, caso_id, evidencia_id, fase, tipo_documento, periodo, ruta_relativa, disponibilidad, pagina, disponible_inicial)
          VALUES (S.documento_id, S.caso_id, S.evidencia_id, S.fase, S.tipo_documento, S.periodo, S.ruta_relativa, S.disponibilidad, S.pagina, S.disponible_inicial)
        """
        bq.query(merge_doc_sql).result()

        update_absent_doc_sql = f"""
        UPDATE {table_documentos}
        SET
          ruta_relativa = '04_Simulacion_Cliente/SYN-002_septiembre.pdf',
          disponibilidad = 'Disponible',
          disponible_inicial = TRUE
        WHERE documento_id = 'DOC-SYN-002-E04'
        """
        bq.query(update_absent_doc_sql).result()
        print(
            "[BigQuery] Upserted DOC-SYN-002-E05 and updated DOC-SYN-002-E04 in documentos."
        )

    elif case_id == "SYN-007":
        merge_clarification_sql = f"""
        MERGE {table_evidencias} T
        USING (
          SELECT
            'RESP-SYN-007-01' AS evidencia_id,
            'SYN-007' AS caso_id,
            'Respuesta simulada' AS fase,
            'Interacción' AS tipo_fuente,
            DATE '2026-10-14' AS fecha_evidencia,
            'Octubre' AS periodo,
            CAST(NULL AS NUMERIC) AS importe_eur,
            'Disponible' AS disponibilidad,
            'Sí, esos abonos son del piso que tengo alquilado. No lo incluí en el ingreso de la solicitud.' AS contenido
        ) S
        ON T.evidencia_id = S.evidencia_id
        WHEN NOT MATCHED THEN
          INSERT (evidencia_id, caso_id, fase, tipo_fuente, fecha_evidencia, periodo, importe_eur, disponibilidad, contenido)
          VALUES (S.evidencia_id, S.caso_id, S.fase, S.tipo_fuente, S.fecha_evidencia, S.periodo, S.importe_eur, S.disponibilidad, S.contenido)
        """
        bq.query(merge_clarification_sql).result()
        print("[BigQuery] Incorporated customer clarification RESP-SYN-007-01 for SYN-007.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate customer response for SYN-002 (or SYN-007)."
    )
    parser.add_argument(
        "--case",
        default="SYN-002",
        choices=["SYN-002", "SYN-007", "ALL"],
        help="Case ID to simulate response for (default: SYN-002)",
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("PROJECT_ID", ""),
        help="Google Cloud Project ID (optional for local mode)",
    )
    parser.add_argument(
        "--dataset",
        default=os.environ.get("BQ_DATASET", "erein_operaciones"),
        help="BigQuery dataset name",
    )
    parser.add_argument(
        "--gcs-bucket",
        default=os.environ.get("GCS_BUCKET", ""),
        help="GCS bucket name",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset the simulated customer response back to initial Phase 1 state",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Only update local state without calling BigQuery/GCS",
    )
    args = parser.parse_args()

    update_local_state(args.case, reset=args.reset)

    if not args.local_only and args.project_id:
        bucket = args.gcs_bucket or f"{args.project_id}-erein-docs"
        update_cloud_state(
            project_id=args.project_id,
            dataset_id=args.dataset,
            gcs_bucket=bucket,
            case_id=args.case,
            reset=args.reset,
        )

    if not args.reset and args.case == "SYN-002":
        print(
            "\nNext step: Resume case SYN-002 with the agent using:\n"
            '  "El cliente ha aportado la nómina de septiembre pendiente para el expediente SYN-002. '
            'Reanuda la revisión operativa del expediente SYN-002 con la nueva evidencia disponible."'
        )


if __name__ == "__main__":
    main()
