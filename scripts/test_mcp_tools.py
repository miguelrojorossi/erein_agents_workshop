#!/usr/bin/env python3
"""Automated test suite for EREIN MCP Server tools and ADK RAG tool."""

import json
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ["USE_LOCAL_DATA"] = "true"

from erein_agent_reference.app.rag_tool import consultar_politica
from mcp_erein_operaciones.server import (
    consultar_expediente,
    consultar_historial_laboral,
    consultar_transacciones,
    leer_documento,
    registrar_accion_expediente,
)
from scripts.simulate_customer_response import update_local_state


def _call_tool(fn, *args, **kwargs):
    target = getattr(fn, "fn", fn)
    return json.loads(target(*args, **kwargs))


def run_tests() -> None:
    print("==============================================================================")
    print(" Running EREIN MCP & ADK Tool Verification Suite")
    print("==============================================================================")

    # Reset state before testing
    update_local_state("ALL", reset=True)

    # 1. Test ADK RAG Tool (consultar_politica)
    rag_res = json.loads(consultar_politica("P01 ingreso recurrente P02 bonus"))
    assert rag_res["status"] == "SUCCESS"
    assert rag_res["version_politica"] == "DEMO-2"
    print("[PASS] 1. ADK RAG Tool (consultar_politica) retrieves DEMO-2 policy.")

    # 2. Test consultar_expediente masks Nómina amounts
    exp_001 = _call_tool(consultar_expediente, "SYN-001")
    assert exp_001["status"] == "SUCCESS"
    nominas_001 = [
        e for e in exp_001["expediente"]["evidencias"] if e["tipo_fuente"] == "Nómina"
    ]
    assert all(n["importe_eur"] is None for n in nominas_001), "Nómina amounts must be masked!"
    print("[PASS] 2. consultar_expediente masks Nómina amounts to enforce PDF reading.")

    # 3. Test leer_documento extracts text and page number
    doc_001 = _call_tool(leer_documento, "SYN-001", "DOC-SYN-001-E04")
    assert doc_001["status"] == "SUCCESS"
    assert "2100" in doc_001["paginas"][0]["texto"]
    print("[PASS] 3. leer_documento extracts text & page citation from SYN-001 payslip.")

    # 4. Test strict per-case isolation on leer_documento
    cross_doc = _call_tool(leer_documento, "SYN-001", "DOC-SYN-008-E06")
    assert cross_doc["status"] == "ACCESS_DENIED"
    print("[PASS] 4. leer_documento enforces strict per-case isolation (ACCESS_DENIED).")

    # 5. Test SYN-008 PlayStation receipt vs GAMBLING transaction
    doc_008 = _call_tool(leer_documento, "SYN-008", "DOC-SYN-008-E06")
    assert "PlayStation 5" in doc_008["paginas"][0]["texto"]
    assert "TX-SYN-008-09-GAME" in doc_008["paginas"][0]["texto"]
    print("[PASS] 5. leer_documento verifies SYN-008 PlayStation receipt (TX-SYN-008-09-GAME).")

    # 6. Test consultar_transacciones calculates SYN-006 15% gambling metrics
    tx_006 = _call_tool(consultar_transacciones, "SYN-006", "2026-07-01", "2026-09-30")
    assert tx_006["status"] == "SUCCESS"
    gamb_ctx = tx_006["resumen_analitico"]["contexto_categoria_gambling"]
    assert gamb_ctx["trimestre_porcentaje_sobre_nomina"] == 15.0
    assert tx_006["resumen_analitico"]["total_incidencias_periodo"]["descubiertos"] == 0
    print("[PASS] 6. consultar_transacciones computes 15% gambling ratio & 0 incidents for SYN-006.")

    # 7. Test consultar_historial_laboral for SYN-004
    hist_004 = _call_tool(consultar_historial_laboral, "SYN-004")
    assert len(hist_004["historial_laboral"]) == 2
    assert hist_004["historial_laboral"][0]["fecha_alta"] == "2021-10-01"
    print("[PASS] 7. consultar_historial_laboral retrieves intra-group history for SYN-004.")

    # 8. Test P09 Authority Guardrail blocks loan approval/rejection or "No elegible"
    bad_auth = _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-010",
        accion_id="ACC-BAD-01",
        tipo_accion="RESOLUCION",
        estado="No elegible",
        evidencias_ids="SYN-010-E04",
        reglas_ids="P08",
        justificacion="Intento inválido",
    )
    assert bad_auth["status"] == "AUTHORITY_ERROR_P09"
    print("[PASS] 8. registrar_accion_expediente blocks 'No elegible' / loan approval per P09.")

    # 9. Test Idempotency on registrar_accion_expediente
    ok_act = _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-001",
        accion_id="ACC-SYN-001-TEST",
        tipo_accion="RESOLUCION",
        estado="Resuelto",
        ingreso_actualizado_eur_mes=2100,
        evidencias_ids="SYN-001-E01 SYN-001-E02 SYN-001-E03 SYN-001-E04 SYN-001-E05 SYN-001-E06 DOC-SYN-001-E02 DOC-SYN-001-E04",
        reglas_ids="P01 P02 P09",
        justificacion="Excluido bonus extraordinario de 400 EUR.",
    )
    assert ok_act["status"] == "SUCCESS"
    dup_act = _call_tool(
        registrar_accion_expediente,
        caso_id="SYN-001",
        accion_id="ACC-SYN-001-TEST",
        tipo_accion="RESOLUCION",
        estado="Resuelto",
        ingreso_actualizado_eur_mes=2100,
        evidencias_ids="SYN-001-E01",
        reglas_ids="P01 P02 P09",
        justificacion="Duplicado",
    )
    assert dup_act["status"] == "IDEMPOTENT_OK"
    print("[PASS] 9. registrar_accion_expediente enforces accion_id idempotency.")

    # 10. Test SYN-002 Two-Phase Simulation (Absent -> Simulate Response -> Available)
    doc_002_p1 = _call_tool(leer_documento, "SYN-002", "DOC-SYN-002-E04")
    assert doc_002_p1["status"] == "DOCUMENT_ABSENT"
    update_local_state("SYN-002", reset=False)
    doc_002_p2 = _call_tool(leer_documento, "SYN-002", "DOC-SYN-002-E05")
    assert doc_002_p2["status"] == "SUCCESS"
    assert "2500" in doc_002_p2["paginas"][0]["texto"]
    print("[PASS] 10. SYN-002 Phase 1 (DOCUMENT_ABSENT) -> Phase 2 (2500 EUR PDF) works cleanly.")

    # Clean up test state
    update_local_state("ALL", reset=True)
    print("==============================================================================")
    print(" All 10 Verification Checks Passed Successfully!")
    print("==============================================================================")


if __name__ == "__main__":
    run_tests()
