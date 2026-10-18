# Workspace Instructions for Antigravity (`agy`) — EREIN Agents Workshop

When asked to create or scaffold the `erein-operational-reviewer` agent using `agents-cli`:

1. **Reuse Reference RAG Tool (`consultar_politica`)**:
   - Copy `erein_agent_reference/app/rag_tool.py` into `erein-operational-reviewer/app/rag_tool.py` and import `consultar_politica` from `.rag_tool` in `erein-operational-reviewer/app/agent.py`.
   - Ensure the following environment variables are set at the top of `agent.py` and `rag_tool.py` before any Vertex AI / Google Auth imports so that **Agent Runtime SPIFFE Agent Identity** (`--agent-identity`) requests unbound OAuth2 tokens compatible with regional Vertex AI RAG Engine endpoints (`europe-west4-aiplatform.googleapis.com`):
     ```python
     import os
     os.environ["GOOGLE_API_ENABLE_RUNTIME_BOUND_TOKEN"] = "false"
     os.environ["GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"] = "false"
     os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"
     os.environ["GOOGLE_API_USE_MTLS_ENDPOINT"] = "never"
     ```

2. **Bake Environment Defaults for Agent Runtime Deployment**:
   - Read the current values of `$MCP_URL`, `$RAG_CORPUS_NAME`, and `$PROJECT_ID` from the shell environment or `.env.workshop` and use them as the fallback defaults in `os.environ.get(...)` inside `erein-operational-reviewer/app/agent.py` and `erein-operational-reviewer/app/rag_tool.py` so the deployed Agent Runtime container connects to the attendee's Cloud Run MCP server and `europe-west4` RAG corpus out of the box.

3. **Dependencies (`pyproject.toml` / `requirements.txt`)**:
   - Ensure `google-cloud-aiplatform>=1.70.0` and `pypdf>=5.0.0` are included in the agent's dependencies alongside `google-adk`.
