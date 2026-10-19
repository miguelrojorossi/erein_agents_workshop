"""Custom agents-cli evaluation metric against EREIN 05_Evaluacion/evaluacion_formador.csv ground truth."""

import csv
import json
from pathlib import Path
import re
from typing import Any

ALLOWED_STATES = {
    "En revisión",
    "Pendiente de evidencia",
    "Escalado a Operaciones",
    "Resuelto",
    "Cerrado por evidencia incompleta",
}


def _extract_tool_calls(agent_data: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for turn in (agent_data or {}).get("turns", []):
        for event in turn.get("events", []):
            content = event.get("content") or {}
            for part in content.get("parts", []):
                fc = part.get("function_call")
                if fc and isinstance(fc, dict):
                    calls.append(
                        {
                            "name": fc.get("name", ""),
                            "args": fc.get("args") or {},
                        }
                    )
    return calls


def _extract_response_text(instance: dict[str, Any]) -> str:
    resp = instance.get("response")
    if isinstance(resp, dict):
        parts = resp.get("parts") or (resp.get("response") or {}).get("parts") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
        if texts:
            return "\n".join(texts)
    if isinstance(resp, str):
        return resp
    # Fallback: last model text event in agent_data
    agent_data = instance.get("agent_data") or {}
    for turn in reversed(agent_data.get("turns", [])):
        for event in reversed(turn.get("events", [])):
            if event.get("author") not in ("user", "tool"):
                parts = (event.get("content") or {}).get("parts", [])
                texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
                if texts:
                    return "\n".join(texts)
    return ""


def _update_csv_row(
    caso_id: str,
    resultado_agente: str,
    cumple: str,
    observaciones: str,
) -> None:
    """Updates EREIN_CdU_Revision_Operativa_Agentica/05_Evaluacion/resultados_evaluacion_agente.csv if present."""
    try:
        for parent in Path(__file__).resolve().parents:
            eval_dir = parent / "EREIN_CdU_Revision_Operativa_Agentica" / "05_Evaluacion"
            if (eval_dir / "evaluacion_formador.csv").exists():
                formador_csv = eval_dir / "evaluacion_formador.csv"
                out_csv = eval_dir / "resultados_evaluacion_agente.csv"
                source_csv = out_csv if out_csv.exists() else formador_csv
                with open(source_csv, "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames or [
                        "caso_id",
                        "estado_esperado",
                        "ingreso_esperado_eur_mes",
                        "cuota_esperada_eur_mes",
                        "accion_esperada",
                        "reglas",
                        "resultado_agente",
                        "cumple",
                        "observaciones",
                    ]
                    rows = list(reader)
                for row in rows:
                    if row.get("caso_id") == caso_id:
                        row["resultado_agente"] = resultado_agente
                        row["cumple"] = cumple
                        row["observaciones"] = observaciones
                with open(out_csv, "w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                break
    except Exception:
        pass


def evaluate(instance: dict[str, Any]) -> dict[str, Any]:
    """Scores an agent trace against the expected ground truth from 05_Evaluacion/evaluacion_formador.csv."""
    caso_id = instance.get("eval_case_id") or instance.get("caso_id") or ""
    expected_state = (instance.get("expected_state") or "").strip()
    expected_action = (instance.get("expected_action") or "").strip()
    expected_income = instance.get("expected_income_eur_mes")
    expected_installment = instance.get("expected_installment_eur_mes")
    expected_rules = instance.get("expected_rules") or []

    agent_data = instance.get("agent_data") or {}
    tool_calls = _extract_tool_calls(agent_data)
    tool_names = [c["name"] for c in tool_calls]
    response_text = _extract_response_text(instance)

    checks: list[tuple[str, bool, str]] = []

    # 1. Required RAG + MCP Tool Usage
    called_expediente = "consultar_expediente" in tool_names
    called_rag = "consultar_politica" in tool_names
    called_registrar = "registrar_accion_expediente" in tool_names
    checks.append(
        (
            "uso_herramientas",
            called_expediente and called_rag and called_registrar,
            f"Herramientas invocadas: {tool_names} (requiere consultar_expediente, consultar_politica en RAG y registrar_accion_expediente)",
        )
    )

    # Extract last registrar_accion_expediente call arguments
    reg_calls = [c["args"] for c in tool_calls if c["name"] == "registrar_accion_expediente"]
    last_reg = reg_calls[-1] if reg_calls else {}
    recorded_state = str(last_reg.get("estado") or "").strip()
    recorded_income = last_reg.get("ingreso_actualizado_eur_mes")
    recorded_installment = last_reg.get("cuota_actualizada_eur_mes")
    recorded_rules_str = f"{last_reg.get('reglas_ids', '')} {response_text}"

    # 2. Operational State Match (estado_esperado)
    if expected_state:
        recorded_states = [str(r.get("estado") or "").strip().lower() for r in reg_calls if r.get("estado")]
        valid_expected_states = {expected_state.lower()}
        if "escalar" in expected_action.lower():
            valid_expected_states.add("escalado a operaciones")
        state_ok = any(s in valid_expected_states for s in recorded_states) or (
            not recorded_states and any(s in response_text.lower() for s in valid_expected_states)
        )
        checks.append(
            (
                "estado_operativo",
                state_ok,
                f"Estado registrado='{recorded_state}' vs esperado='{expected_state}'",
            )
        )

    # 3. Authority Compliance (P09)
    authority_ok = (not recorded_state or recorded_state in ALLOWED_STATES) and (
        "no elegible" not in recorded_state.lower()
    )
    checks.append(
        (
            "autoridad_P09",
            authority_ok,
            f"Cumplimiento de límites de autoridad P09 (estado='{recorded_state}')",
        )
    )

    # 4. Expected Monthly Income (ingreso_esperado_eur_mes)
    if expected_income is not None and str(expected_income).strip() != "":
        exp_inc_val = float(expected_income)
        inc_ok = False
        if recorded_income is not None and str(recorded_income).strip() != "":
            try:
                inc_ok = abs(float(recorded_income) - exp_inc_val) < 0.01
            except ValueError:
                pass
        if not inc_ok:
            inc_ok = str(int(exp_inc_val)) in response_text
        checks.append(
            (
                "ingreso_esperado",
                inc_ok,
                f"Ingreso actualizado='{recorded_income}' vs esperado='{int(exp_inc_val)} EUR/mes'",
            )
        )

    # 5. Expected Monthly Installment (cuota_esperada_eur_mes)
    if expected_installment is not None and str(expected_installment).strip() != "":
        exp_cuota_val = float(expected_installment)
        cuota_ok = False
        if recorded_installment is not None and str(recorded_installment).strip() != "":
            try:
                cuota_ok = abs(float(recorded_installment) - exp_cuota_val) < 0.01
            except ValueError:
                pass
        if not cuota_ok:
            cuota_ok = str(int(exp_cuota_val)) in response_text
        checks.append(
            (
                "cuota_esperada",
                cuota_ok,
                f"Cuota actualizada='{recorded_installment}' vs esperada='{int(exp_cuota_val)} EUR/mes'",
            )
        )

    # 6. Policy Rule Citations (reglas DEMO-2)
    if expected_rules:
        cited = [
            r
            for r in expected_rules
            if re.search(rf"\b{re.escape(r)}\b", recorded_rules_str, re.IGNORECASE)
        ]
        rules_ok = len(cited) >= max(1, len(expected_rules) - 1)
        checks.append(
            (
                "reglas_DEMO2",
                rules_ok,
                f"Reglas citadas={cited} vs esperadas={expected_rules}",
            )
        )

    passed_count = sum(1 for _, ok, _ in checks if ok)
    total_count = len(checks) if checks else 1
    score = round(passed_count / total_count, 4)
    explanation = " | ".join(
        f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}" for name, ok, detail in checks
    )

    if caso_id:
        res_summary = (
            f"Estado: {recorded_state or 'N/A'} | "
            f"Ingreso: {recorded_income if recorded_income is not None else '-'} | "
            f"Cuota: {recorded_installment if recorded_installment is not None else '-'}"
        )
        _update_csv_row(
            caso_id=caso_id,
            resultado_agente=res_summary,
            cumple="SI" if score >= 0.8 else "NO",
            observaciones=explanation,
        )

    return {
        "score": score,
        "explanation": explanation,
    }
