# Architecture — Authorized DevOps Agent

## Overview

Authorized DevOps Agent is a 6-agent AI system built on the MCP (Model Context Protocol)
architecture. The system acts on GitHub repositories on behalf of authenticated users,
using Auth0 Token Vault as the single security perimeter for credential management.
```
User → Auth0 Login → Connect GitHub → Token Vault → Orchestrator → 6 Agents → GitHub PR
```

---

## System Diagram
```
┌─────────────────────────────────────────────────────────────────┐
│                     Frontend  (port 8000)                       │
│               Observatory UI — Real-time reasoning trail        │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP POST /mcp
┌────────────────────────────▼────────────────────────────────────┐
│                   Orchestrator  (port 8600)                     │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  Auth0 Token Vault (RFC 8693)                           │    │
│  │  POST /oauth/token → scoped GitHub token (in-memory)    │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  GitHub API Client                                      │    │
│  │  get_repo_info · list_files · create_branch             │    │
│  │  commit_file · create_pull_request · list_issues        │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                 │
│  MCP Coordinator → calls sub-agents in sequence                 │
└────┬──────┬───────┬───────────┬─────────────────────────────────┘
     │      │       │           │                    │
   MCP    MCP     MCP         MCP                  MCP
     │      │       │           │                    │
┌────▼──┐ ┌─▼───┐ ┌─▼────────┐ ┌▼───────┐ ┌──────▼───┐
│Planner│ │Risks│ │  Code    │ │Progress│ │  Digest  │
│ :8601 │ │:8603│ │Execution │ │  :8602 │ │  :8604   │
│       │ │     │ │  :8605   │ │        │ │          │
└───────┘ └─────┘ └──────────┘ └────────┘ └──────────┘
```

---

## Auth0 Token Vault — RFC 8693

### Why Token Vault?

Traditional AI DevOps tools store GitHub tokens in environment variables, databases,
or pass them through every layer of the system. Token Vault eliminates this:

| Approach        | Token Storage   | Credential Exposure                   |
|-----------------|-----------------|---------------------------------------|
| Traditional     | env var / DB    | All agents + logs                     |
| **This system** | In-memory only  | Orchestrator only, gc'd after request |

### Token Exchange Flow
```
User Browser                Auth0                      Orchestrator
    │                         │                             │
    ├── Login ───────────────►│                             │
    │◄── access_token ────────┤                             │
    │    refresh_token        │                             │
    │                         │                             │
    ├── Connect GitHub ──────►│                             │
    │   Connected Accounts    │  Auth0 stores               │
    │   flow                  │  GitHub token in Vault      │
    │                         │                             │
    ├── Set goal ──────────────────────────────────────────►│
    │                         │                             │
    │                         │◄── RFC 8693 exchange ───────┤
    │                         │    POST /oauth/token        │
    │                         │    grant_type=token-exchange│
    │                         │    connection=github        │
    │                         │                             │
    │                         ├── scoped GitHub token ─────►│
    │                         │   VaultToken (in-memory)    │
    │                         │                             │
    │                         │      ┌── GitHub API ───────►│
    │                         │      │   branch + commit    │
    │                         │      │   pull request       │
    │                         │      └── token goes gc      │
    │◄── PR created ────────────────────────────────────────┤
```

### RFC 8693 HTTP Request
```http
POST https://YOUR_TENANT.auth0.com/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=urn:ietf:params:oauth:grant-type:token-exchange
&client_id=YOUR_CLIENT_ID
&client_secret=YOUR_CLIENT_SECRET
&subject_token=USER_REFRESH_TOKEN
&subject_token_type=urn:ietf:params:oauth:token-type:refresh_token
&requested_token_type=http://auth0.com/oauth/token-type/federated-connection-access-token
&connection=github
&scope=repo read:user
```

### VaultToken — Security by Design
```python
@dataclass(frozen=True)          # immutable — cannot be accidentally mutated
class VaultToken:
    access_token: str
    token_type: str = "bearer"
    scope: str = ""

    def auth_header(self) -> str:
        return f"Bearer {self.access_token}"

    def __repr__(self) -> str:
        # token value NEVER appears in logs, tracebacks, or repr
        return f"VaultToken(token_type={self.token_type!r}, access_token=***)"
```

### Log Redaction (Deep)

All data in the Orchestrator passes through `_safe_deep()` before any log
statement or reasoning trail entry. This recursively redacts sensitive keys
in nested dicts and lists:
```python
_SENSITIVE = frozenset({
    "github_token", "access_token", "refresh_token",
    "auth0_access_token", "auth0_refresh_token",
})

def _safe_deep(data: Any) -> Any:
    """Recursively redact sensitive fields in nested dicts/lists (logging only)."""
    if isinstance(data, dict):
        return {
            k: "***REDACTED***" if k in _SENSITIVE else _safe_deep(v)
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [_safe_deep(item) for item in data]
    return data

# Every logger.* call involving sensitive data:
logger.debug("→ %s/%s params=%s", url, tool, _safe_deep(params))
```

---

## Secret Isolation — Docker Level

`AUTH0_CLIENT_SECRET` is only injected into the Orchestrator container.
Sub-agents structurally cannot access it, even if compromised:
```yaml
# docker-compose.yml
orchestrator:
  environment:
    - AUTH0_DOMAIN=${AUTH0_DOMAIN}
    - AUTH0_CLIENT_ID=${AUTH0_CLIENT_ID}
    - AUTH0_CLIENT_SECRET=${AUTH0_CLIENT_SECRET}   # ← only here

planner:
  env_file:
    - .env   # does NOT include AUTH0_* vars — only ANTHROPIC_API_KEY
```

### Access Matrix
```
| Component      | AUTH0_SECRET    | GitHub Token    | LLM API       |
|----------------|:--------------: |:--------------: | :-----------: |
| Orchestrator   |   ✅ env var    |  ✅ VaultToken  |      ✅      |
| Planner        |       ❌        |       ❌        |      ✅      |
| Risks          |       ❌        |       ❌        |      ✅      |
| Code Execution |       ❌        |       ❌        |      ✅      |
| Progress       |       ❌        |       ❌        |      ✅      |
| Digest         |       ❌        |       ❌        |      ✅      |
```
---

## MCP Protocol

Each sub-agent is a FastAPI service exposing two endpoints:
```
GET  /health   → {"status": "ok", "agent": "...", "available_tools": [...]}
POST /mcp      → JSON-RPC style: {"method": "tool_name", "params": {...}}
```

The Orchestrator calls agents via `_call_agent()`:
```python
async def _call_agent(self, url: str, tool: str, params: dict) -> dict:
    logger.debug("→ %s/%s params=%s", url, tool, _safe_deep(params))
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{url}/mcp",
            json={"method": f"tools/{tool}", "params": params},
        )
        resp.raise_for_status()
        return resp.json()
```

Tool names are registered per-agent:
```python
class PlannerAgent(MCPAgent):
    def __init__(self):
        super().__init__("Planner")
        self.register_tool("plan_with_reasoning", self.plan_with_reasoning)
        self.register_tool("risk_aware_planning", self.risk_aware_planning)
```

---

## Observable Reasoning Trail

Every agent appends `ReasoningStep` objects to a shared `reasoning` list.
The Orchestrator collects all steps from all agents and returns them in the
final response. The Observatory UI renders this as a real-time decision log.
```python
@dataclass
class ReasoningStep:
    step_number:  int
    description:  str
    timestamp:    str
    input_data:   dict
    output:       dict
    agent:        str
```

Example trail (abbreviated):
```json
[
  {"step_number": 1, "agent": "Orchestrator",    "description": "Token Vault: RFC 8693 exchange"},
  {"step_number": 2, "agent": "Orchestrator",    "description": "GitHub: reading repository structure"},
  {"step_number": 3, "agent": "Planner",         "description": "Classified as security_fix / high complexity"},
  {"step_number": 4, "agent": "Risks",           "description": "LLM risk analysis parsed — 3 issues found"},
  {"step_number": 5, "agent": "Code-Execution",  "description": "STRATEGIC: Planning fix approach"},
  {"step_number": 6, "agent": "Code-Execution",  "description": "EXECUTION iter 0: 4/5 tests passed"},
  {"step_number": 7, "agent": "Code-Execution",  "description": "REFLECTION: generating fix for 1 failure"},
  {"step_number": 8, "agent": "Code-Execution",  "description": "EXECUTION iter 1: 5/5 tests passed"},
  {"step_number": 9, "agent": "Orchestrator",    "description": "GitHub: PR #42 created"},
  {"step_number": 10,"agent": "Progress",        "description": "Velocity: on_track — urgency: medium"},
  {"step_number": 11,"agent": "Digest",          "description": "Executive summary generated — HEALTHY"}
]
```

---
