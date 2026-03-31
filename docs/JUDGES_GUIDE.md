# [Authorized DevOps Agent — Judges Guide]()

**Hackathon:** [Authorized to Act — Auth0 × Devpost](https://authorizedtoact.devpost.com/)  
**Live Demo:** [https://authorized-devops-agent.netlify.app](https://authorized-devops-agent.netlify.app)  
**Repository:
** [https://github.com/Exsellent/Authorized-DevOps-Agent](https://github.com/Exsellent/Authorized-DevOps-Agent)

---

# ⚡ Quick Start

👉 **Open the live demo and click "Run Demo"**  
No setup required.

You will see a **full AI DevOps pipeline execute in real time**:

- Auth0 Token Exchange (RFC 8693)
- Repository analysis
- Security risk detection
- Code generation + sandbox testing
- Pull Request creation

⏱ Takes ~30–40 seconds

---

# 🧠 What to Look For (Key Innovation)

While watching the demo, pay attention to:

### 🔐 1. Secure Token Usage

- Token is obtained via **Auth0 Token Vault**
- Never exposed to agents or UI
- Short-lived (~60 seconds), garbage-collected after use

Look for in every response:

```json
"auth0_token_vault": true
```

---

### 🤖 2. Multi-Agent Pipeline

6 agents collaborate in sequence:

- **Planner** → breaks down the task
- **Risks** → detects security issues
- **Code Execution** → generates and tests the fix
- **Orchestrator** → creates the PR with vault-authorized GitHub calls

---

### 🔁 3. Self-Debugging Loop

Watch the Code Execution agent:

```
ITER 0 → 4/5 tests pass
REFLECTION → root cause identified, fix applied
ITER 1 → 5/5 tests pass, quality_score = 1.00
```

The AI improves its own code automatically.

---

### 🛡 4. Dual-Pass Security Gate

- **Phase 1 (before codegen)** — Risks agent assesses design, CVEs, integration fragility
- **Phase 2 (after codegen)** — patch scan for `eval(`, `exec(`, hardcoded secrets; blocks PR on critical violations

---

### 📊 5. Full Observability

- Every agent step is visible in the Observatory UI
- Reasoning trail included in every API response
- Nothing happens in a black box

---

# 🧪 Option 2 — Run Locally (Docker)

### Requirements

- Docker + Docker Compose
- Auth0 account (free tier)
- GitHub personal access token (`repo` scope)
- Anthropic API key

### Setup

```bash
git clone https://github.com/Exsellent/Authorized-DevOps-Agent.git
cd Authorized-DevOps-Agent
cp env.example .env
```

Fill `.env` (template included in this archive as `env.example`):

```env
# Auth0 Token Vault
AUTH0_DOMAIN=your-tenant.auth0.com
AUTH0_CLIENT_ID=your_client_id
AUTH0_CLIENT_SECRET=your_client_secret
AUTH0_AUDIENCE=https://api.github.com
AUTH0_REFRESH_TOKEN=your_refresh_token

# GitHub
GITHUB_TOKEN=ghp_your_token
GITHUB_REPO=owner/repo

# LLM
ANTHROPIC_API_KEY=sk-ant-api03-your_key
ANTHROPIC_BASE_URL=https://api.anthropic.com/v1/messages
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022

# Agent Settings
MAX_DEBUG_ITERATIONS=2
EXECUTION_TIMEOUT=60
```

### Run

```bash
docker compose up --build
```

Open: **http://localhost:8000**

Startup takes ~30–40 seconds. All 6 agents start in parallel, then the Orchestrator comes online.

---

# ⚙️ Option 3 — Test Agents via Script

Run the included test script:

```bash
chmod +x test_all_agents.sh
./test_all_agents.sh
```

Full output is in **`results_script.md`** in this archive.

### What this validates

| Test | Agent                | What to check                                       |
|------|----------------------|-----------------------------------------------------|
| 1    | Planner :8601        | Task classification, subtask decomposition          |
| 2    | Risks :8603          | Security analysis, risk scoring                     |
| 3    | Progress :8602       | Velocity signal, LLM commit summary                 |
| 4    | Digest :8604         | `quality_state=HEALTHY`, summary generation         |
| 5    | Orchestrator :8600   | `triage_single` — classification + priority routing |
| 6    | Code Execution :8605 | Full fix + test pipeline, `auth0_token_vault=true`  |

### ✅ Expected result — Code Execution

```json
{
  "tests_passed": true,
  "quality_score": 0.9999999999999999,
  "production_ready": true,
  "confidence": 1.0,
  "auth0_token_vault": true
}
```

`code_hash` in `patch_files` matches `verification_artifact.code_hash` — the patch committed to GitHub is byte-for-byte
identical to the code that passed the sandbox.

---

# 🔐 Auth0 Integration

This project implements **real RFC 8693 Token Exchange** as the entry point to every pipeline run.

| Property           | Implementation                                                               |
|--------------------|------------------------------------------------------------------------------|
| Least Privilege    | Only Orchestrator accesses the vault; 5 sub-agents receive no tokens         |
| Audience scoping   | `AUTH0_AUDIENCE=https://api.github.com`, scope: `repo`                       |
| Short-lived tokens | `VaultToken` frozen dataclass, in-memory only, GC'd after each request       |
| 401 retry          | `_github_call_with_retry` — on HTTP 401, fresh RFC 8693 exchange, retry once |

### Why this matters

- The AI **never sees credentials** — they never leave Auth0
- Tokens cannot be reused or escalated
- Full **least-privilege, zero-trust** agent architecture
- Confirmed as "textbook Least Privilege design" by Auth0/Okta

---

# 📦 Project Structure

```
6 independent agents (FastAPI, Python 3.12)
MCP protocol (JSON-RPC over HTTP)
Docker Compose deployment
Observatory UI (Netlify, no credentials required)
```

---

# 🧾 Archive Contents

``` 
JUDGES_GUIDE.md       ← this file
env.example           ← environment variables template
test_all_agents.sh    ← agent test script (also in repository)
results_script.md     ← full test output with expected results
```

---

# 🏁 Summary

**Authorized DevOps Agent = secure execution layer for AI**

It allows an AI model to:

- act on real systems (GitHub repository → code fix → pull request)
- without ever holding credentials
- using delegated, scoped, short-lived access via Auth0

👉 This is what **zero-trust AI agents** look like in practice.
