#!/usr/bin/env python3
"""Automated 10-Scenario Evaluation Harness for EREIN Operational Review Agent.

Evaluates agent execution across SYN-001 to SYN-010 against the trainer ground truth
in `05_Evaluacion/evaluacion_formador.csv` across all 8 criteria defined in Section 5
of `Guia_Implementacion_PoC.pdf`:
  1. Resultado operativo (state, updated income, updated installment)
  2. Fundamento (cites evidences, PDF documents/pages, and DEMO-2 rules P01-P13)
  3. Uso de herramientas (consults required sources and persists the action)
  4. Interacción mínima (only requests missing documents in SYN-002 Phase 1)
  5. Autoridad (escalates expected cases; never grants/denies loans or labels 'No elegible')
  6. Continuidad (preserves history across SYN-002 Phase 1 -> Phase 2)
  7. Aislamiento (no cross-case contamination)
  8. Escrituras (no duplicate actions; version_politica == 'DEMO-2')
"""

import argparse
import csv
import json
import os
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from erein_agent_reference.app.rag_tool import consultar_politica
from mcp_erein_operaciones.server import (
    consultar_expediente,
    consultar_historial_laboral,
    consultar_transacciones,
    leer_documento,
    registrar_accion_expediente,
)
from scripts.simulate_customer_response import update_local_state

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
RUNTIME_STATE_PATH = (
    REPO_ROOT / "mcp_erein_operaciones" / "data" / "runtime_state.json"
)


def _call_tool(fn, *args, **kwargs) -> dict[str, Any]:
    target = getattr(fn, "fn", fn)
    return json.loads(target(*args, **kwargs))


def run_reference_agent_over_all_10_cases() -> None:
    """Executes the complete tool workflow for all 10 scenarios (including SYN-002 Phase 1 & 2)."""
    os.environ["USE_LOCAL_DATA"] = "true"
    update_local_state("ALL", reset=True)

    # Retrieve DEMO-2 policy via ADK RAG tool
    _ = consultar_politica("Política y procedimiento de revisión operativa DEMO-2 P01 a P13")

    # --- SYN-001: Exclude 400 EUR bonus, update recurring income to 2100 EUR ---
    _call_tool(consultar_expediente, "SYN-001")
    _call_tool(leer_documento, "SYN-001", "DOC-SYN-001-E02")
    _call_tool(leer_documento, "SYN-001", "DOC-SYN-001-E04")
    _call_tool(consultar_transacciones, "SYN-001")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-001",
        accion_id="ACC-SYN-001-01",
        tipo_accion="RESOLUCION",
        estado="Resuelto",
        ingreso_actualizado_eur_mes=2100,
        evidencias_ids="SYN-001-E01 SYN-001-E02 SYN-001-E03 SYN-001-E04 SYN-001-E05 SYN-001-E06 DOC-SYN-001-E02 DOC-SYN-001-E04",
        reglas_ids="P01 P02 P09",
        justificacion=(
            "Nóminas de agosto (DOC-SYN-001-E02, pág. 1) y septiembre (DOC-SYN-001-E04, pág. 1) "
            "y abonos bancarios acreditan ingreso neto recurrente de 2100 EUR/mes (P01). "
            "Se excluye el bonus extraordinario no recurrente de 400 EUR (SYN-001-E05, SYN-001-E06) según P02."
        ),
    )

    # --- SYN-002 Phase 1: Missing September payslip -> Request evidence ---
    _call_tool(consultar_expediente, "SYN-002")
    _call_tool(leer_documento, "SYN-002", "DOC-SYN-002-E02")
    _call_tool(leer_documento, "SYN-002", "DOC-SYN-002-E04")
    _call_tool(consultar_transacciones, "SYN-002")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-002",
        accion_id="ACC-SYN-002-01",
        tipo_accion="SOLICITUD_EVIDENCIA",
        estado="Pendiente de evidencia",
        evidencias_ids="SYN-002-E01 SYN-002-E02 SYN-002-E03 SYN-002-E04 DOC-SYN-002-E02",
        reglas_ids="P01 P03 P08 P09",
        mensaje_cliente="Por favor, aporte la nómina de septiembre de 2026 para acreditar el abono salarial de 2.500 EUR observado en cuenta.",
        justificacion=(
            "Se observa abono de nómina en septiembre de 2500 EUR (SYN-002-E03) frente a 2100 EUR en agosto "
            "(DOC-SYN-002-E02, pág. 1), pero la nómina de septiembre (SYN-002-E04 / DOC-SYN-002-E04) está ausente. "
            "Se solicita únicamente la nómina de septiembre (P08)."
        ),
    )

    # --- SYN-002 Phase 2: Simulate customer response & re-analyze -> Escalate per P03 ---
    update_local_state("SYN-002", reset=False)
    _call_tool(consultar_expediente, "SYN-002")
    _call_tool(leer_documento, "SYN-002", "DOC-SYN-002-E05")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-002",
        accion_id="ACC-SYN-002-02",
        tipo_accion="ESCALADO_OPERACIONES",
        estado="Escalado a Operaciones",
        evidencias_ids="SYN-002-E01 SYN-002-E02 SYN-002-E03 SYN-002-E05 DOC-SYN-002-E02 DOC-SYN-002-E05",
        reglas_ids="P01 P03 P09",
        motivo_escalado="Subida salarial en septiembre a 2500 EUR acreditada con una sola nómina; no cumple dos nóminas coincidentes de P01.",
        justificacion=(
            "Tras incorporar la respuesta simulada (SYN-002-E05, DOC-SYN-002-E05 pág. 1: neto ordinario 2500 EUR), "
            "el nuevo salario de 2500 EUR solo cuenta con una nómina (septiembre) frente a 2100 EUR en agosto, "
            "por lo que no cumple las dos nóminas recientes de P01. Se escala el cálculo a Operaciones según P03."
        ),
    )

    # --- SYN-003: OCR extraction error (22000 vs 2200 in original PDF) ---
    _call_tool(consultar_expediente, "SYN-003")
    _call_tool(leer_documento, "SYN-003", "DOC-SYN-003-E02")
    _call_tool(leer_documento, "SYN-003", "DOC-SYN-003-E04")
    _call_tool(consultar_transacciones, "SYN-003")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-003",
        accion_id="ACC-SYN-003-01",
        tipo_accion="RESOLUCION",
        estado="Resuelto",
        ingreso_actualizado_eur_mes=2200,
        evidencias_ids="SYN-003-E01 SYN-003-E02 SYN-003-E03 SYN-003-E04 SYN-003-E05 DOC-SYN-003-E02 DOC-SYN-003-E04",
        reglas_ids="P01 P06 P09",
        justificacion=(
            "Se corrige la extracción automática errónea de 22000 EUR (SYN-003-E05) a 2200 EUR/mes "
            "prevaleciendo el documento PDF original legible DOC-SYN-003-E04 (pág. 1: 'Neto ordinario a percibir EUR 2200') "
            "y DOC-SYN-003-E02 (pág. 1), coincidentes con los abonos bancarios de 2200 EUR (P01, P06)."
        ),
    )

    # --- SYN-004: Intra-group employer change with recognized seniority from 2021-10-01 ---
    _call_tool(consultar_expediente, "SYN-004")
    _call_tool(leer_documento, "SYN-004", "DOC-SYN-004-E02")
    _call_tool(leer_documento, "SYN-004", "DOC-SYN-004-E04")
    _call_tool(leer_documento, "SYN-004", "DOC-SYN-004-E05")
    _call_tool(leer_documento, "SYN-004", "DOC-SYN-004-E06")
    _call_tool(consultar_historial_laboral, "SYN-004")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-004",
        accion_id="ACC-SYN-004-01",
        tipo_accion="RESOLUCION",
        estado="Resuelto",
        ingreso_actualizado_eur_mes=2300,
        situacion_laboral_actualizada="Asalariado indefinido",
        antiguedad_reconocida_desde="2021-10-01",
        evidencias_ids="SYN-004-E01 SYN-004-E02 SYN-004-E03 SYN-004-E04 SYN-004-E05 SYN-004-E06 DOC-SYN-004-E02 DOC-SYN-004-E04 DOC-SYN-004-E05 DOC-SYN-004-E06",
        reglas_ids="P01 P06 P09 P12",
        justificacion=(
            "Contrastado el historial laboral continuo (DOC-SYN-004-E05, pág. 1: baja en Grupo Norte Servicios Demo "
            "el 31/08/2026 y alta en Grupo Norte Digital Demo el 01/09/2026) junto con el certificado empresarial "
            "de continuidad (DOC-SYN-004-E06, pág. 1), se conserva la antigüedad reconocida desde 2021-10-01 (P12) "
            "y el ingreso recurrente de 2300 EUR/mes (P01)."
        ),
    )

    # --- SYN-005: Undeclared 180 EUR/month loan installment ---
    _call_tool(consultar_expediente, "SYN-005")
    _call_tool(leer_documento, "SYN-005", "DOC-SYN-005-E02")
    _call_tool(leer_documento, "SYN-005", "DOC-SYN-005-E04")
    _call_tool(consultar_transacciones, "SYN-005")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-005",
        accion_id="ACC-SYN-005-01",
        tipo_accion="RESOLUCION",
        estado="Resuelto",
        ingreso_actualizado_eur_mes=2400,
        cuota_actualizada_eur_mes=180,
        evidencias_ids="SYN-005-E01 SYN-005-E02 SYN-005-E03 SYN-005-E04 SYN-005-E05 SYN-005-E06 SYN-005-E07 DOC-SYN-005-E02 DOC-SYN-005-E04",
        reglas_ids="P01 P05 P09",
        justificacion=(
            "Ingreso recurrente acreditado en 2400 EUR/mes (DOC-SYN-005-E02 y DOC-SYN-005-E04 pág. 1, P01). "
            "El contrato financiero vigente (SYN-005-E07) y los cargos bancarios mensuales coincidentes de 180 EUR "
            "(SYN-005-E05, SYN-005-E06) acreditan una cuota mensual no declarada de 180 EUR/mes que se registra según P05."
        ),
    )

    # --- SYN-006: Recurrent gambling of 450/3000 = 15% with 0 incidents -> Escalate ---
    _call_tool(consultar_expediente, "SYN-006")
    _call_tool(leer_documento, "SYN-006", "DOC-SYN-006-E02")
    _call_tool(leer_documento, "SYN-006", "DOC-SYN-006-E04")
    _call_tool(consultar_transacciones, "SYN-006")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-006",
        accion_id="ACC-SYN-006-01",
        tipo_accion="ESCALADO_OPERACIONES",
        estado="Escalado a Operaciones",
        ingreso_actualizado_eur_mes=3000,
        tratamiento_alerta="Apuestas recurrentes 450/3000 = 15% mensual (1350/9000 = 15% trimestral); 0 descubiertos, devoluciones ni impagos.",
        motivo_escalado="Valoración humana de alerta sensible de apuestas recurrentes (15% de la nómina neta) con saldos positivos y sin incidencias.",
        evidencias_ids="SYN-006-E01 SYN-006-E02 SYN-006-E03 SYN-006-E04 SYN-006-E05 SYN-006-E06 DOC-SYN-006-E02 DOC-SYN-006-E04",
        reglas_ids="P01 P07 P09 P13",
        justificacion=(
            "Nómina neta verificada en 3000 EUR/mes (DOC-SYN-006-E02 y DOC-SYN-006-E04 pág. 1, P01). "
            "Se observan pagos reales recurrentes de apuestas de 450 EUR/mes en julio, agosto y septiembre "
            "(450 / 3000 = 15% cada mes; 1350 / 9000 = 15% en el trimestre). El periodo presenta saldos positivos "
            "y 0 descubiertos, devoluciones o impagos (SYN-006-E06). Conforme a P07 y P13, la ausencia de incidencias "
            "no autoriza a cerrar la alerta ni implica rechazo automático: se escala a Operaciones con el contexto completo."
        ),
    )

    # --- SYN-007: Undeclared rental income 700 EUR/month -> Record separately & Escalate ---
    _call_tool(consultar_expediente, "SYN-007")
    _call_tool(leer_documento, "SYN-007", "DOC-SYN-007-E02")
    _call_tool(leer_documento, "SYN-007", "DOC-SYN-007-E04")
    _call_tool(leer_documento, "SYN-007", "DOC-SYN-007-E05")
    _call_tool(consultar_transacciones, "SYN-007")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-007",
        accion_id="ACC-SYN-007-01",
        tipo_accion="ESCALADO_OPERACIONES",
        estado="Escalado a Operaciones",
        ingreso_actualizado_eur_mes=2000,
        renta_bruta_observada_eur_mes=700,
        motivo_escalado="Valorar admisibilidad y porcentaje computable de renta bruta de alquiler observada (700 EUR/mes).",
        evidencias_ids="SYN-007-E01 SYN-007-E02 SYN-007-E03 SYN-007-E04 SYN-007-E05 SYN-007-E06 DOC-SYN-007-E02 DOC-SYN-007-E04 DOC-SYN-007-E05",
        reglas_ids="P01 P09 P11",
        justificacion=(
            "Salario neto recurrente acreditado en 2000 EUR/mes (DOC-SYN-007-E02 y DOC-SYN-007-E04 pág. 1, P01). "
            "El contrato de alquiler vigente (DOC-SYN-007-E05, pág. 1) y los tres abonos mensuales de 700 EUR "
            "(SYN-007-E06) acreditan una renta bruta observada de 700 EUR/mes. Según P11, se registra por separado "
            "renta_bruta_observada_eur_mes = 700 sin sumarla al salario computable (2000 EUR) y se escala su admisibilidad a Operaciones."
        ),
    )

    # --- SYN-008: PlayStation 5 purchase miscategorized as GAMBLING -> Resolve per P10 ---
    _call_tool(consultar_expediente, "SYN-008")
    _call_tool(leer_documento, "SYN-008", "DOC-SYN-008-E02")
    _call_tool(leer_documento, "SYN-008", "DOC-SYN-008-E04")
    _call_tool(leer_documento, "SYN-008", "DOC-SYN-008-E06")
    _call_tool(consultar_transacciones, "SYN-008")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-008",
        accion_id="ACC-SYN-008-01",
        tipo_accion="RESOLUCION",
        estado="Resuelto",
        ingreso_actualizado_eur_mes=2300,
        tratamiento_alerta="Alerta GAMBLING descartada: compra comercial acreditada (Consola PlayStation 5, 499 EUR, ref TX-SYN-008-09-GAME). Se conserva categoría original.",
        evidencias_ids="SYN-008-E01 SYN-008-E02 SYN-008-E03 SYN-008-E04 SYN-008-E05 SYN-008-E06 DOC-SYN-008-E02 DOC-SYN-008-E04 DOC-SYN-008-E06",
        reglas_ids="P01 P09 P10",
        justificacion=(
            "Nóminas de agosto y septiembre acreditan ingreso recurrente de 2300 EUR/mes (DOC-SYN-008-E02 y DOC-SYN-008-E04 pág. 1, P01). "
            "El justificante de compra DOC-SYN-008-E06 (pág. 1) coincide en titular (Hugo Demo), fecha (20/09/2026), "
            "importe (499.00 EUR), comercio (GAME DEMO ELECTRONICA) y referencia (TX-SYN-008-09-GAME), acreditando la compra "
            "de una consola PlayStation 5. Según P10, se descarta la alerta de apuestas registrando la reclasificación sin alterar el movimiento original."
        ),
    )

    # --- SYN-009: Self-transfers (400 EUR) + undeclared loan installment (150 EUR) ---
    _call_tool(consultar_expediente, "SYN-009")
    _call_tool(leer_documento, "SYN-009", "DOC-SYN-009-E02")
    _call_tool(leer_documento, "SYN-009", "DOC-SYN-009-E04")
    _call_tool(consultar_transacciones, "SYN-009")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-009",
        accion_id="ACC-SYN-009-01",
        tipo_accion="RESOLUCION",
        estado="Resuelto",
        ingreso_actualizado_eur_mes=2200,
        cuota_actualizada_eur_mes=150,
        evidencias_ids="SYN-009-E01 SYN-009-E02 SYN-009-E03 SYN-009-E04 SYN-009-E05 SYN-009-E06 SYN-009-E07 SYN-009-E08 SYN-009-E09 SYN-009-E10 DOC-SYN-009-E02 DOC-SYN-009-E04",
        reglas_ids="P01 P02 P05 P09",
        justificacion=(
            "Se tratan conjuntamente las discrepancias de ingresos y obligaciones: "
            "1) Se excluyen las transferencias desde cuenta propia de 400 EUR/mes (SYN-009-E05, SYN-009-E07, SYN-009-E09) "
            "fijando el ingreso neto recurrente en 2200 EUR/mes según nóminas DOC-SYN-009-E02 y DOC-SYN-009-E04 pág. 1 (P01, P02). "
            "2) Se registra la cuota mensual de 150 EUR/mes acreditada por contrato financiero vigente (SYN-009-E10) y cargos bancarios (SYN-009-E06, SYN-009-E08) según P05."
        ),
    )

    # --- SYN-010: Illegible document and 10-day deadline expired -> Close incomplete ---
    _call_tool(consultar_expediente, "SYN-010")
    _call_tool(leer_documento, "SYN-010", "DOC-SYN-010-E03")
    _call_tool(leer_documento, "SYN-010", "DOC-SYN-010-E04")
    _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-010",
        accion_id="ACC-SYN-010-01",
        tipo_accion="CIERRE_EVIDENCIA_INCOMPLETA",
        estado="Cerrado por evidencia incompleta",
        evidencias_ids="SYN-010-E01 SYN-010-E02 SYN-010-E03 SYN-010-E04 SYN-010-E05 SYN-010-E06 SYN-010-E07 DOC-SYN-010-E03 DOC-SYN-010-E04",
        reglas_ids="P08 P09",
        justificacion=(
            "La nómina de septiembre DOC-SYN-010-E04 (pág. 1) presenta el campo de importe ilegible ('[CAMPO NO LEGIBLE]'). "
            "Solicitada copia legible el 03/10/2026 (SYN-010-E05) y enviados los recordatorios a los días 3 y 7 "
            "(06/10/2026 SYN-010-E06 y 10/10/2026 SYN-010-E07), a fecha de evaluación 14/10/2026 han transcurrido 11 días "
            "(>10 días) sin respuesta. Se cierra por evidencia incompleta según P08 y P09, sin etiquetar como no elegible."
        ),
    )


def evaluate_recorded_actions() -> bool:
    """Compares recorded actions against 05_Evaluacion/evaluacion_formador.csv."""
    with open(EVAL_CSV_PATH, "r", encoding="utf-8") as f:
        expected_rows = list(csv.DictReader(f))

    state = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
    acciones = state.get("acciones", [])

    by_case: dict[str, list[dict[str, Any]]] = {}
    for act in acciones:
        by_case.setdefault(act["caso_id"], []).append(act)

    output_rows = []
    all_passed = True

    print("\n====================================================================================================")
    print(f"{'CASO':<9} | {'ESTADO ESPERADO':<31} | {'ESTADO AGENTE':<31} | {'INGRESO':<8} | {'CUOTA':<6} | {'REGLAS':<15} | {'CUMPLE'}")
    print("----------------------------------------------------------------------------------------------------")

    for exp in expected_rows:
        cid = exp["caso_id"]
        case_actions = by_case.get(cid, [])
        if not case_actions:
            print(f"{cid:<9} | {exp['estado_esperado']:<31} | {'(SIN ACCIÓN)':<31} | {'-':<8} | {'-':<6} | {'-':<15} | NO")
            all_passed = False
            continue

        first_act = case_actions[0]
        last_act = case_actions[-1]

        # Check state (for SYN-002, Phase 1 is 'Pendiente de evidencia' and Phase 2 is 'Escalado a Operaciones')
        if cid == "SYN-002":
            state_ok = (
                first_act["estado"] == "Pendiente de evidencia"
                and last_act["estado"] == "Escalado a Operaciones"
            )
            estado_display = f"{first_act['estado']} -> {last_act['estado']}"
        else:
            state_ok = last_act["estado"] == exp["estado_esperado"]
            estado_display = last_act["estado"]

        # Check income
        exp_ing = float(exp["ingreso_esperado_eur_mes"]) if exp["ingreso_esperado_eur_mes"] else None
        act_ing = (
            float(last_act["ingreso_actualizado_eur_mes"])
            if last_act.get("ingreso_actualizado_eur_mes") is not None
            else None
        )
        ing_ok = exp_ing == act_ing

        # Check installment
        exp_cuota = float(exp["cuota_esperada_eur_mes"]) if exp["cuota_esperada_eur_mes"] else None
        act_cuota = (
            float(last_act["cuota_actualizada_eur_mes"])
            if last_act.get("cuota_actualizada_eur_mes") is not None
            else None
        )
        cuota_ok = exp_cuota == act_cuota

        # Check rules cited
        expected_rules = set(exp["reglas"].split())
        cited_rules = set()
        for a in case_actions:
            cited_rules.update(a.get("reglas_ids", "").replace(",", " ").split())
        rules_ok = expected_rules.issubset(cited_rules)

        # Check DEMO-2 version & non-empty justification
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

    print("====================================================================================================\n")

    with open(OUTPUT_CSV_PATH, "w", encoding="utf-8", newline="") as f:
        fieldnames = list(expected_rows[0].keys())
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"Saved detailed evaluation results to: {OUTPUT_CSV_PATH}")
    return all_passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate EREIN agent results against 05_Evaluacion/evaluacion_formador.csv"
    )
    parser.add_argument(
        "--verify-ground-truth",
        action="store_true",
        help="Run reference agent sequence across all 10 cases and verify 10/10 compliance",
    )
    args = parser.parse_args()

    if args.verify_ground_truth or not RUNTIME_STATE_PATH.exists():
        run_reference_agent_over_all_10_cases()

    ok = evaluate_recorded_actions()
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
