# Workspace Instructions for Antigravity (`agy`) — EREIN Agents Workshop

When asked to create or scaffold the `erein-operational-reviewer` agent using `agents-cli`:

1. **Reuse Reference RAG Tool (`consultar_politica`)**:
   - Copy `erein_agent_reference/app/rag_tool.py` into the agent directory (`rag_tool.py`) and import `consultar_politica` in `agent.py`.
   - Do **NOT** set `GOOGLE_API_USE_MTLS_ENDPOINT="never"` or `GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN="false"` globally at the top of `agent.py`, as global overrides interfere with Agent Runtime's built-in OpenTelemetry / Cloud Trace exporter (`rag_tool.py` already scopes unbound token creation strictly inside `_get_gcp_credentials()` and restores `os.environ` immediately).

2. **Do NOT Hardcode Policy or Procedure in Agent Instructions**:
   - **Never** paste or summarize the Policy rules (`P01`–`P13`), the Operational Procedure (`DEMO-2` states, deadlines, authority limits), or case-specific answers (`SYN-001`–`SYN-010`) into the agent's `instruction` / system prompt.
   - Instead, instruct the agent (as in `erein_agent_reference/app/agent.py`) that it does **not** have the Policy or Procedure in its prompt and **must always call `consultar_politica` first** to retrieve both the Policy and Procedure dynamically from Vertex AI RAG Engine.

3. **Bake Environment Defaults for Agent Runtime Deployment**:
   - Read the current values of `$MCP_URL`, `$RAG_CORPUS_NAME`, and `$PROJECT_ID` from the shell environment or `.env.workshop` and use them as the fallback defaults in `os.environ.get(...)` inside `erein-operational-reviewer/app/agent.py` and `erein-operational-reviewer/app/rag_tool.py` so the deployed Agent Runtime container connects to the attendee's Cloud Run MCP server and `europe-west4` RAG corpus out of the box.

4. **Dependencies (`pyproject.toml` / `requirements.txt`) — Do NOT Manually Declare `google-cloud-aiplatform` or `opentelemetry-*`**:
   - **Do NOT** declare `google-cloud-aiplatform` or explicit OpenTelemetry libraries (`opentelemetry-*`) directly in the agent's `pyproject.toml` or `requirements.txt`.
   - `agents-cli` (`scaffold create`, `scaffold enhance`, and `deploy`) handles declaring and matching these dependencies automatically. Declaring them manually can cause package version mismatches between the Vertex SDK and OpenTelemetry that silently break trace instrumentation in Agent Runtime (`erein_agent_reference/app/rag_tool.py` calls the Vertex AI RAG Engine `:retrieveContexts` API via `google.auth.transport.requests.AuthorizedSession`, requiring no extra packages).

5. **Export `app` and Prepare `05_Evaluacion` Evaluation Suite (`tests/eval/`)**:
   - Ensure `erein-operational-reviewer/app/agent.py` defines `app = App(root_agent=root_agent, name="app")` (from `google.adk.apps import App`) and `app/__init__.py` exports `from .agent import app, root_agent`.
   - Run `python3 scripts/prepare_eval_suite.py` so `erein-operational-reviewer/tests/eval/` contains the evaluation datasets built from `EREIN_CdU_Revision_Operativa_Agentica/05_Evaluacion/evaluacion_formador.csv` (`datasets/erein-smoke-dataset.json`, `datasets/erein-evaluacion-formador.json`), `eval_config.yaml`, `eval_cloud_config.yaml`, and `erein_ground_truth_metric.py`.

6. **When Asked to Run Evaluations (`run evaluations` or `agents-cli eval`)**:
   - First ensure `python3 scripts/prepare_eval_suite.py` has been run so `erein-operational-reviewer/tests/eval/` is populated from `EREIN_CdU_Revision_Operativa_Agentica/05_Evaluacion/evaluacion_formador.csv`.
   - Run `agents-cli eval run --dataset tests/eval/datasets/erein-smoke-dataset.json --config tests/eval/eval_config.yaml --project $PROJECT_ID --region europe-west1` inside `erein-operational-reviewer/` (or `--dataset tests/eval/datasets/erein-evaluacion-formador.json` if asked for the full 10-case benchmark).
