# Deployment Guide — Authorized DevOps Agent

---

## Prerequisites

| Tool           | Version | Check                    |
|----------------|---------|--------------------------|
| Docker         | 24+     | `docker --version`       |
| Docker Compose | 2.20+   | `docker compose version` |
| Git            | any     | `git --version`          |

You also need:

- Auth0 account with Token Vault enabled
- GitHub repository to analyse
- OpenRouter or Anthropic API key

---

## Quick Start

```bash
git clone https://github.com/your-org/authorized-devops-agent
cd authorized-devops-agent
cp .env.example .env
# Edit .env — see Configuration section below
docker compose up --build
open http://localhost:8000
```

---

## Configuration

### Step 1 — Auth0 Setup

1. Log in to [auth0.com](https://auth0.com) → your tenant
2. **Applications** → **Create Application** → Machine to Machine
3. **APIs** → enable the Management API
4. **Token Vault** → enable in application settings
5. **Authentication** → **Social** → GitHub → enable
6. **Connected Accounts** → enable GitHub connection
7. Note your **Domain**, **Client ID**, **Client Secret**

Required Auth0 settings:

```
Allowed Callback URLs: http://localhost:8000/callback
Allowed Logout URLs:   http://localhost:8000
Token Endpoint Auth Method: Post
GitHub scopes: repo, read:user
Application scopes: openid profile email offline_access
```

### Step 2 — Configure `.env`

```bash
cp .env.example .env
```

#### Minimal configuration (demo mode)

```env
# Claude via OpenRouter (no rate limits for demo)
ANTHROPIC_API_KEY=sk-or-v1-your_openrouter_key
ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1/chat/completions
ANTHROPIC_MODEL=anthropic/claude-3-5-sonnet

# Auth0 (required — injected ONLY into orchestrator via docker-compose.yml)
# ⚠ Do NOT put AUTH0_CLIENT_SECRET here if you want container isolation.
# Instead, set these in the orchestrator's `environment:` block in docker-compose.yml.
# For demo convenience they can go here, but in production use docker-compose env only.
AUTH0_DOMAIN=your-tenant.auth0.com
AUTH0_CLIENT_ID=your_client_id_here
AUTH0_CLIENT_SECRET=your_client_secret_here

APP_MODE=demo
LOG_LEVEL=INFO
```

#### Production configuration

```env
# Auth0
AUTH0_DOMAIN=your-tenant.auth0.com
AUTH0_CLIENT_ID=your_client_id_here
AUTH0_CLIENT_SECRET=your_client_secret_here

# Claude direct
ANTHROPIC_API_KEY=sk-ant-api03-your_real_key
# ANTHROPIC_BASE_URL defaults to https://api.anthropic.com/v1/messages
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022

# Optional integrations
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../xxx
GITHUB_TOKEN=ghp_fallback_only

APP_MODE=live
LOG_LEVEL=INFO
```

### Step 3 — Start Services

```bash
# Start all services (foreground — see logs)
docker compose up --build

# Start detached
docker compose up --build -d

# Logs for a specific service
docker compose logs -f orchestrator
docker compose logs -f code_execution

# Restart single service without rebuild
docker compose restart risks
```

---

## Service Architecture

### Ports

| Service        | Container Port | Host Port | Description                       |
|----------------|:--------------:|:---------:|-----------------------------------|
| static         |       80       |   8000    | Observatory UI (nginx)            |
| orchestrator   |      8600      |   8600    | Main pipeline + Auth0 Token Vault |
| planner        |      8601      |   8601    | Goal classification               |
| progress       |      8602      |   8602    | Velocity tracking                 |
| risks          |      8603      |   8603    | Risk assessment                   |
| digest         |      8604      |   8604    | Report generation                 |
| code_execution |      8605      |   8605    | Fix generation + testing          |

### Startup Order

`depends_on: service_healthy` enforces this sequence:

```
Phase 1 (parallel):
  planner       → healthcheck passes (~10s)
  risks         → healthcheck passes (~10s)
  code_execution→ healthcheck passes (~10s)
  progress      → healthcheck passes (~10s)
  digest        → healthcheck passes (~10s)

Phase 2:
  orchestrator  → waits for all Phase 1 healthy (~15s)

Phase 3:
  static (nginx)→ waits for orchestrator healthy (~5s)

Total: ~30-40s from docker compose up to UI ready
```

### Network

All services share `agent-net` bridge network.
Internal communication uses Docker service names:

```
http://planner:8601/mcp
http://risks:8603/mcp
http://code_execution:8605/mcp
http://progress:8602/mcp
http://digest:8604/mcp
```

---

## Health Checks

### Check All Services

```bash
# Quick status table
docker compose ps

# Individual health
curl -s http://localhost:8600/health | python3 -m json.tool
curl -s http://localhost:8601/health | python3 -m json.tool
curl -s http://localhost:8602/health | python3 -m json.tool
curl -s http://localhost:8603/health | python3 -m json.tool
curl -s http://localhost:8604/health | python3 -m json.tool
curl -s http://localhost:8605/health | python3 -m json.tool
```

### Expected Response

```json
{
  "status": "ok",
  "agent": "Orchestrator",
  "available_tools": [
    "run_secure_devops_flow"
  ]
}
```

### Wait for All Services (CI/demo script)

```bash
#!/bin/bash
PORTS=(8600 8601 8602 8603 8604 8605)
for port in "${PORTS[@]}"; do
  echo -n "Waiting for :$port ... "
  until curl -sf "http://localhost:$port/health" > /dev/null; do
    sleep 2
  done
  echo "OK"
done
echo "All agents healthy — opening UI"
open http://localhost:8000
```

---

## Running a Pipeline

### Via UI

1. Open `http://localhost:8000`
2. Log in with Auth0
3. Connect your GitHub account (Connected Accounts)
4. Enter: repository (`owner/repo`) and goal
5. Click **Run Agent**
6. Watch the Observatory reasoning trail in real time

### Via API (curl)

```bash
curl -X POST http://localhost:8600/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "method": "run_secure_devops_flow",
    "params": {
      "repo": "owner/repository",
      "goal": "Fix JWT token validation vulnerability",
      "user_refresh_token": "YOUR_AUTH0_REFRESH_TOKEN"
    }
  }' | python3 -m json.tool
```

---

## Troubleshooting

### Services not starting

```bash
# Check logs
docker compose logs --tail=50 orchestrator

# Common causes:
# 1. .env missing required variables
# 2. Auth0 credentials incorrect
# 3. Port already in use
lsof -i :8600   # check port 8600
```

### `AUTH0_CLIENT_SECRET` errors

```
TokenVaultError: Auth0 Token Vault exchange failed (401)
```

Checklist:

1. `AUTH0_DOMAIN` format: `your-tenant.auth0.com` (no `https://`)
2. Application type: Machine to Machine (not SPA)
3. Token Vault enabled in Auth0 Dashboard
4. GitHub Connected Accounts enabled

### LLM rate limit errors

Switch to OpenRouter in `.env`:

```env
ANTHROPIC_API_KEY=sk-or-v1-your_openrouter_key
ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1/chat/completions
ANTHROPIC_MODEL=anthropic/claude-3-5-sonnet
```

### Code Execution: `ModuleNotFoundError`

The agent auto-installs pip dependencies before execution.
If a package fails to install:

```bash
# Check code_execution logs
docker compose logs --tail=100 code_execution | grep "Installing deps"

# Enter container to debug
docker compose exec code_execution bash
pip install PyJWT
```

### All tests fail with identical errors

Verify the test isolation fix is applied — each `test_*` function should
run independently. Check `_split_test_functions()` in `code_execution/agent.py`.

### GitHub API 404 errors

```
GitHubError: 404 - Not Found
```

Checklist:

1. Repository exists and is accessible
2. GitHub OAuth scopes include `repo` (not just `read:repo`)
3. Auth0 Token Vault GitHub connection has `repo` scope configured

---

## Resetting for Demo

```bash
# Full reset — rebuild everything
docker compose down -v
docker compose up --build

# Soft reset — restart without rebuild
docker compose restart

# View real-time logs during demo
docker compose logs -f --tail=0
```

---

## Environment Variables Reference

| Variable              | Required | Default                                 | Description              |
|-----------------------|:--------:|-----------------------------------------|--------------------------|
| `AUTH0_DOMAIN`        |    ✅     | —                                       | Auth0 tenant domain      |
| `AUTH0_CLIENT_ID`     |    ✅     | —                                       | Auth0 Client ID          |
| `AUTH0_CLIENT_SECRET` |    ✅     | —                                       | Auth0 Client Secret      |
| `ANTHROPIC_API_KEY`   |    ✅     | —                                       | Claude / OpenRouter key  |
| `ANTHROPIC_BASE_URL`  |    ❌     | `https://api.anthropic.com/v1/messages` | LLM endpoint             |
| `ANTHROPIC_MODEL`     |    ❌     | `claude-3-5-sonnet-20241022`            | Model name               |
| `SLACK_WEBHOOK_URL`   |    ❌     | —                                       | Slack Incoming Webhook   |
| `GITHUB_TOKEN`        |    ❌     | —                                       | Fallback PAT (demo only) |
| `APP_MODE`            |    ❌     | `demo`                                  | `demo` or `live`         |
| `LOG_LEVEL`           |    ❌     | `INFO`                                  | Python log level         |
| `LLM_MAX_TOKENS`      |    ❌     | `2048`                                  | Max tokens per LLM call  |
| `LLM_TIMEOUT_SECONDS` |    ❌     | `60`                                    | LLM HTTP timeout         |

```

---

