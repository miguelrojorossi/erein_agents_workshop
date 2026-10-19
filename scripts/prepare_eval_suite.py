#!/usr/bin/env python3
"""Generates agents-cli eval datasets and configuration from 05_Evaluacion/evaluacion_formador.csv.

Reads:
  - EREIN_CdU_Revision_Operativa_Agentica/05_Evaluacion/evaluacion_formador.csv
  - EREIN_CdU_Revision_Operativa_Agentica/05_Evaluacion/evaluacion_formador.schema.json
  - EREIN_CdU_Revision_Operativa_Agentica/05_Evaluacion/resultados_evaluacion_agente.csv

Generates in both `erein_agent_reference/tests/eval/` and `erein-operational-reviewer/tests/eval/` (if present):
  - datasets/erein-smoke-dataset.json        (Task 3 Step 4: fast 2-case pre-deployment evaluation)
  - datasets/erein-evaluacion-formador.json  (Task 5: full 10-case evaluation suite SYN-001..SYN-010)
  - eval_config.yaml                         (Multi-turn Vertex AI GenAI Eval metrics + custom ground-truth metric)
  - erein_ground_truth_metric.py             (Custom metric scoring against evaluacion_formador.csv & updating resultados_evaluacion_agente.csv)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import shutil
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "EREIN_CdU_Revision_Operativa_Agentica" / "05_Evaluacion"
FORMADOR_CSV = EVAL_DIR / "evaluacion_formador.csv"
RESULTADOS_CSV = EVAL_DIR / "resultados_evaluacion_agente.csv"
REF_EVAL_DIR = REPO_ROOT / "erein_agent_reference" / "tests" / "eval"


def _load_reference_observations() -> dict[str, dict[str, str]]:
    if not RESULTADOS_CSV.exists():
        return {}
    with open(RESULTADOS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return {row["caso_id"].strip(): row for row in reader if row.get("caso_id")}


def _build_prompt(caso_id: str) -> str:
    if caso_id == "SYN-002":
        return (
            "Revisa el expediente SYN-002. Primero verifica si falta la nómina de septiembre "
            "(solicítala dejando constancia en Pendiente de evidencia si aún no se ha incorporado "
            "la respuesta simulada); a continuación incorpora la respuesta simulada del cliente "
            "(incorporar_respuesta_simulada para SYN-002), analiza la nueva nómina DOC-SYN-002-E05 "
            "conforme a la política DEMO-2 consultada en Vertex AI RAG Engine y registra la acción "
            "operativa final citando las reglas aplicables."
        )
    return (
        f"Revisa íntegramente el expediente {caso_id}, consulta la política y procedimiento "
        "aplicable en Vertex AI RAG Engine, contrasta los documentos PDF y datos del expediente, "
        "registra la acción operativa correspondiente y emite el dictamen estructurado citando "
        "las reglas DEMO-2 aplicables."
    )


def _build_reference_text(row: dict[str, str], ref_obs: dict[str, str]) -> str:
    caso_id = row["caso_id"].strip()
    estado = row["estado_esperado"].strip()
    ingreso = row["ingreso_esperado_eur_mes"].strip()
    cuota = row["cuota_esperada_eur_mes"].strip()
    accion = row["accion_esperada"].strip()
    reglas = row["reglas"].strip()
    obs = (ref_obs.get("observaciones") or "").strip()

    lines = [
        f"Expediente: {caso_id}",
        f"Estado operativo esperado: {estado}",
        f"Ingreso neto mensual computable esperado (EUR/mes): {ingreso if ingreso else 'N/A (sin cambio / escalado)'}",
        f"Cuota mensual no declarada esperada (EUR/mes): {cuota if cuota else 'N/A'}",
        f"Acción esperada: {accion}",
        f"Reglas DEMO-2 aplicables: {reglas}",
    ]
    if obs:
        lines.append(f"Justificación de referencia: {obs}")
    return "\n".join(lines)


def build_eval_cases() -> list[dict[str, Any]]:
    ref_map = _load_reference_observations()
    eval_cases: list[dict[str, Any]] = []

    with open(FORMADOR_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            caso_id = (row.get("caso_id") or "").strip()
            if not caso_id:
                continue
            estado = (row.get("estado_esperado") or "").strip()
            ingreso_raw = (row.get("ingreso_esperado_eur_mes") or "").strip()
            cuota_raw = (row.get("cuota_esperada_eur_mes") or "").strip()
            accion = (row.get("accion_esperada") or "").strip()
            reglas_raw = (row.get("reglas") or "").strip()
            reglas_list = [r.strip() for r in reglas_raw.split() if r.strip()]

            prompt_text = _build_prompt(caso_id)
            reference_text = _build_reference_text(row, ref_map.get(caso_id, {}))

            eval_case: dict[str, Any] = {
                "eval_case_id": caso_id,
                "prompt": {
                    "role": "user",
                    "parts": [{"text": prompt_text}],
                },
                "reference": {
                    "response": {
                        "role": "model",
                        "parts": [{"text": reference_text}],
                    }
                },
                "caso_id": caso_id,
                "expected_state": estado,
                "expected_income_eur_mes": float(ingreso_raw) if ingreso_raw else None,
                "expected_installment_eur_mes": float(cuota_raw) if cuota_raw else None,
                "expected_action": accion,
                "expected_rules": reglas_list,
            }
            eval_cases.append(eval_case)

    return eval_cases


def write_eval_targets(eval_cases: list[dict[str, Any]]) -> None:
    smoke_ids = {"SYN-001", "SYN-003"}
    smoke_cases = [c for c in eval_cases if c["eval_case_id"] in smoke_ids]

    target_agent_dirs = [REPO_ROOT / "erein_agent_reference"]
    workshop_agent_dir = REPO_ROOT / "erein-operational-reviewer"
    if workshop_agent_dir.exists():
        target_agent_dirs.append(workshop_agent_dir)

    for agent_dir in target_agent_dirs:
        eval_dir = agent_dir / "tests" / "eval"
        datasets_dir = eval_dir / "datasets"
        datasets_dir.mkdir(parents=True, exist_ok=True)

        smoke_path = datasets_dir / "erein-smoke-dataset.json"
        full_path = datasets_dir / "erein-evaluacion-formador.json"

        with open(smoke_path, "w", encoding="utf-8") as f:
            json.dump({"eval_cases": smoke_cases}, f, ensure_ascii=False, indent=2)
            f.write("\n")

        with open(full_path, "w", encoding="utf-8") as f:
            json.dump({"eval_cases": eval_cases}, f, ensure_ascii=False, indent=2)
            f.write("\n")

        if eval_dir != REF_EVAL_DIR:
            shutil.copy2(REF_EVAL_DIR / "eval_config.yaml", eval_dir / "eval_config.yaml")
            shutil.copy2(
                REF_EVAL_DIR / "eval_cloud_config.yaml", eval_dir / "eval_cloud_config.yaml"
            )
            shutil.copy2(
                REF_EVAL_DIR / "erein_ground_truth_metric.py",
                eval_dir / "erein_ground_truth_metric.py",
            )

        print(f"[OK] Prepared evaluation suite in: {eval_dir}")
        print(f"     - Smoke dataset (2 cases) : {smoke_path}")
        print(f"     - Full dataset (10 cases) : {full_path}")
        print(f"     - Local eval config       : {eval_dir / 'eval_config.yaml'}")
        print(f"     - Cloud eval config       : {eval_dir / 'eval_cloud_config.yaml'}")
        print(f"     - Ground-truth metric     : {eval_dir / 'erein_ground_truth_metric.py'}")


def main() -> None:
    if not FORMADOR_CSV.exists():
        raise FileNotFoundError(f"Ground truth CSV not found: {FORMADOR_CSV}")
    eval_cases = build_eval_cases()
    write_eval_targets(eval_cases)


if __name__ == "__main__":
    main()
