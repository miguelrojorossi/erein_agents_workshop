#!/usr/bin/env python3
"""BigQuery-backed 10-Scenario Evaluation Verifier for EREIN Operational Review Agent.

Verifies the actions persisted in BigQuery (`erein_operaciones.acciones`) across
SYN-001 to SYN-010 against the trainer ground truth in `05_Evaluacion/evaluacion_formador.csv`.
"""

import argparse
import csv
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from google.cloud import bigquery

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_CSV_PATH = (
    REPO_ROOT
    / "EREIN_CdU_Revision_Operativa_Agentica"
    / "05_Evaluacion"
    / "evaluacion_formador.csv"
)
OUTPUT_CSV_PATH = (
    REPO_ROOT
    / "EREIN_CdU_Revision_Operativa_Agentica"
    / "05_Evaluacion"
    / "resultados_evaluacion_agente.csv"
)


def _get_project_id(explicit_project_id: str = "") -> str:
    project_id = (
        explicit_project_id
        or os.environ.get("PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    )
    if not project_id:
        try:
            project_id = subprocess.check_output(
                ["gcloud", "config", "get-value", "project"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if project_id == "(unset)":
                project_id = ""
        except Exception:
            project_id = ""
    if not project_id:
        raise RuntimeError(
            "PROJECT_ID is required. Set PROJECT_ID or pass --project-id."
        )
    return project_id


def fetch_bigquery_actions(project_id: str, dataset_id: str) -> list[dict[str, Any]]:
    bq = bigquery.Client(project=project_id)
    sql = f"""
    SELECT
      caso_id,
      accion_id,
      tipo_accion,
      estado,
      CAST(ingreso_actualizado_eur_mes AS FLOAT64) AS ingreso_actualizado_eur_mes,
      CAST(cuota_actualizada_eur_mes AS FLOAT64) AS cuota_actualizada_eur_mes,
      situacion_laboral_actualizada,
      evidencias_ids,
      reglas_ids,
      justificacion,
      mensaje_cliente,
      motivo_escalado,
      CAST(fecha_accion AS STRING) AS fecha_accion,
      CAST(antiguedad_reconocida_desde AS STRING) AS antiguedad_reconocida_desde,
      CAST(renta_bruta_observada_eur_mes AS FLOAT64) AS renta_bruta_observada_eur_mes,
      tratamiento_alerta,
      version_politica
    FROM `{project_id}.{dataset_id}.acciones`
    ORDER BY fecha_accion ASC, accion_id ASC
    """
    return [dict(r.items()) for r in bq.query(sql).result()]


def evaluate_recorded_actions(project_id: str, dataset_id: str) -> bool:
    """Compares actions recorded in BigQuery against 05_Evaluacion/evaluacion_formador.csv."""
    with open(EVAL_CSV_PATH, "r", encoding="utf-8-sig") as f:
        expected_rows = list(csv.DictReader(f))

    acciones = fetch_bigquery_actions(project_id, dataset_id)

    by_case: dict[str, list[dict[str, Any]]] = {}
    for act in acciones:
        by_case.setdefault(act["caso_id"], []).append(act)

    output_rows = []
    all_passed = True

    print(
        "\n===================================================================================================="
    )
    print(
        f"{'CASO':<9} | {'ESTADO ESPERADO':<31} | {'ESTADO BIGQUERY':<31} | {'INGRESO':<8} | {'CUOTA':<6} | {'REGLAS':<15} | {'CUMPLE'}"
    )
    print(
        "----------------------------------------------------------------------------------------------------"
    )

    for exp in expected_rows:
        cid = exp["caso_id"]
        case_actions = by_case.get(cid, [])
        if not case_actions:
            print(
                f"{cid:<9} | {exp['estado_esperado']:<31} | {'(SIN ACCIÓN EN BQ)':<31} | {'-':<8} | {'-':<6} | {'-':<15} | NO"
            )
            all_passed = False
            continue

        first_act = case_actions[0]
        last_act = case_actions[-1]

        if cid == "SYN-002":
            state_ok = last_act["estado"] in (
                "Pendiente de evidencia",
                "Escalado a Operaciones",
            )
            estado_display = (
                f"{first_act['estado']} -> {last_act['estado']}"
                if first_act["accion_id"] != last_act["accion_id"]
                else last_act["estado"]
            )
        else:
            state_ok = last_act["estado"] == exp["estado_esperado"]
            estado_display = last_act["estado"]

        exp_ing = (
            float(exp["ingreso_esperado_eur_mes"])
            if exp["ingreso_esperado_eur_mes"]
            else None
        )
        act_ing = (
            float(last_act["ingreso_actualizado_eur_mes"])
            if last_act.get("ingreso_actualizado_eur_mes") is not None
            else None
        )
        ing_ok = exp_ing is None or exp_ing == act_ing

        exp_cuota = (
            float(exp["cuota_esperada_eur_mes"])
            if exp["cuota_esperada_eur_mes"]
            else None
        )
        act_cuota = (
            float(last_act["cuota_actualizada_eur_mes"])
            if last_act.get("cuota_actualizada_eur_mes") is not None
            else None
        )
        cuota_ok = exp_cuota is None or exp_cuota == act_cuota

        expected_rules = set(exp["reglas"].split())
        cited_rules = set()
        for a in case_actions:
            cited_rules.update(a.get("reglas_ids", "").replace(",", " ").split())
        rules_ok = len(expected_rules.intersection(cited_rules)) >= max(
            1, len(expected_rules) - 1
        )

        meta_ok = all(
            a.get("version_politica") == "DEMO-2" and bool(a.get("justificacion"))
            for a in case_actions
        )

        cumple = state_ok and ing_ok and cuota_ok and rules_ok and meta_ok
        if not cumple:
            all_passed = False

        ing_str = f"{int(act_ing)}" if act_ing is not None else "-"
        cuota_str = f"{int(act_cuota)}" if act_cuota is not None else "-"
        cumple_str = "SI" if cumple else "NO"

        print(
            f"{cid:<9} | {exp['estado_esperado']:<31} | {estado_display[:31]:<31} | "
            f"{ing_str:<8} | {cuota_str:<6} | {last_act['reglas_ids']:<15} | {cumple_str}"
        )

        row_out = dict(exp)
        row_out["resultado_agente"] = (
            f"Estado: {estado_display} | Ingreso: {ing_str} | Cuota: {cuota_str} | "
            f"Acción: {last_act['accion_id']}"
        )
        row_out["cumple"] = cumple_str
        row_out["observaciones"] = last_act["justificacion"]
        output_rows.append(row_out)

    print(
        "====================================================================================================\n"
    )

    if output_rows:
        with open(OUTPUT_CSV_PATH, "w", encoding="utf-8", newline="") as f:
            fieldnames = list(expected_rows[0].keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)
        print(f"Saved detailed evaluation results to: {OUTPUT_CSV_PATH}")

    return all_passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify BigQuery acciones against 05_Evaluacion/evaluacion_formador.csv"
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("PROJECT_ID", ""),
        help="Google Cloud Project ID",
    )
    parser.add_argument(
        "--dataset",
        default=os.environ.get("BQ_DATASET", "erein_operaciones"),
        help="BigQuery dataset name",
    )
    parser.add_argument(
        "--verify-ground-truth",
        action="store_true",
        help="Compare BigQuery acciones rows against 05_Evaluacion/evaluacion_formador.csv",
    )
    args = parser.parse_args()

    project_id = _get_project_id(args.project_id)
    ok = evaluate_recorded_actions(project_id, args.dataset)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
