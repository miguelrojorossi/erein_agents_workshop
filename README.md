# EREIN Agent Platform Workshop (`Revisión Operativa de Operaciones de Activo`)

Hands-on 4-hour workshop repository for **EREIN** on **Google Cloud Agent Platform**, combining:
- **Agent Development Kit (ADK)**, **Agents CLI (`google-agents-cli`)**, and **Antigravity CLI (`agy`)**
- **Vertex AI RAG Engine** (`consultar_politica` native ADK tool over `03_Conocimiento` DEMO-2 policies)
- **FastMCP Server on Cloud Run (`mcp_erein_operaciones`)** connected to **BigQuery** (`01_Datos`) and **Cloud Storage** (`02_Documentos`)
- **Agent Runtime** with **SPIFFE Agent Identity**
- **Agent Gateway (`AGENT_TO_ANYWHERE` PSC Egress)** + **Model Armor** Guardrails
- **Gemini Enterprise** (Enterprise Agent Gallery + No-Code Agent Builder)
- **End-to-End Observability & Automated 10-Case Evaluation** (`05_Evaluacion`)

## Repository Structure

| Path | Description |
| :--- | :--- |
| [`Workshop_Guide_EREIN.md`](Workshop_Guide_EREIN.md) | Complete 4-hour step-by-step hands-on workshop guide (Tasks 1–7, English instructions with Spanish prompts). |
| [`EREIN_CdU_Revision_Operativa_Agentica/`](EREIN_CdU_Revision_Operativa_Agentica/README.md) | EREIN synthetic dataset: 10 cases (`SYN-001`–`SYN-010`), 63 initial evidences, 203 transactions, PDF payslips/receipts, `DEMO-2` policies, simulated customer responses, and evaluator ground truth. |
| [`mcp_erein_operaciones/`](mcp_erein_operaciones/server.py) | Cloud Run FastMCP server exposing the 5 governed operational tools (`consultar_expediente`, `leer_documento`, `consultar_transacciones`, `consultar_historial_laboral`, `registrar_accion_expediente`). |
| [`erein_agent_reference/`](erein_agent_reference/app/agent.py) | Reference ADK agent implementation combining the native Vertex AI RAG Engine tool ([`rag_tool.py`](erein_agent_reference/app/rag_tool.py)) with `MCPToolset`, plus ready-to-use prompts for Task 3 and Task 6. |
| [`scripts/bootstrap_erein_env.sh`](scripts/bootstrap_erein_env.sh) | One-command sandbox bootstrap script that creates the `erein_operaciones` BigQuery dataset, uploads `02_Documentos` and `03_Conocimiento` to Cloud Storage, and provisions the Vertex AI RAG Engine corpus. |
| [`scripts/simulate_customer_response.py`](scripts/simulate_customer_response.py) | Customer event simulator for Task 5 (`SYN-002` Phase 2 September payslip submission and `SYN-007` rental clarification). |
| [`scripts/test_mcp_tools.py`](scripts/test_mcp_tools.py) | Automated verification suite for the 5 MCP tools, ADK RAG tool, `caso_id` isolation, `P09` authority limits, and `accion_id` idempotency. |
| [`scripts/evaluate_agent.py`](scripts/evaluate_agent.py) | Automated 10-scenario evaluation script comparing agent outcomes against `05_Evaluacion/evaluacion_formador.csv`. |

## Quick Verification

```bash
python3 scripts/test_mcp_tools.py
python3 scripts/evaluate_agent.py --verify-ground-truth
```
