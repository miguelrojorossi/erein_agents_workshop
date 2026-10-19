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

4. **Dependencies (`pyproject.toml`)**:
   - **Do NOT** add a duplicate `google-cloud-aiplatform` line to `pyproject.toml` if `google-cloud-aiplatform` (such as `google-cloud-aiplatform[evaluation,agent-engines]>=...`) is already present, as duplicate entries break `uv add` during `agents-cli scaffold enhance`. Only add `pypdf>=5.0.0` if it is not already listed.
