# API Reference — Authorized DevOps Agent

All agents expose two endpoints:
- `GET /health` — liveness check
- `POST /mcp` — MCP tool invocation

---

## Orchestrator (port 8600)

### `run_secure_devops_flow`

Main pipeline entry point. Called by the UI.

**Request:**
```http
POST http://localhost:8600/mcp
Content-Type: application/json
```
```json
{
  "method": "tools/run_secure_devops_flow",
  "params": {
    "repo": "owner/repository",
    "goal": "Fix JWT token validation vulnerability and update dependencies",
    "auth0_refresh_token": "<auth0_refresh_token>"
  }
}
```

**Parameters:**

| Field                  | Type   | Required | Description                                                      |
|------------------------|--------|----------|------------------------------------------------------------------|
| `repo`                 | string | ✅        | GitHub repository in `owner/repo` format                         |
| `goal`                 | string | ✅        | Natural language goal for the agent                              |
| `auth0_refresh_token`  | string | ❌        | Auth0 refresh token (falls back to `AUTH0_REFRESH_TOKEN` env)    |
| `auth0_access_token`   | string | ❌        | Auth0 access token (fallback if refresh unavailable)             |
| `base_branch`          | string | ❌        | Target branch for PR (default `"main"`)                          |
| `slack_notify`         | bool   | ❌        | Send Slack summary when done (default `true`)                    |

**Response:**
```json
{
  "status": "success",
  "pr_url": "https://github.com/owner/repo/pull/42",
  "pr_number": 42,
  "risk_level": "HIGH",
  "issues_found": [{"title": "Hardcoded JWT secret", "severity": "HIGH", "category": "security", "priority": 1}],
  "summary": "## 📋 Summary\n\nThe AI agent fixed JWT validation...\n\n## 🔐 Security & Risk\n\n...",
  "token_vault_used": true,
  "timestamp": "2026-03-01T14:22:11.000Z",
  "reasoning": [
    {
      "step_number": 1,
      "description": "Token Vault: obtaining scoped GitHub token via RFC 8693",
      "timestamp": "2026-03-01T14:22:00.123Z",
      "input_data": {"connection": "github", "scopes": ["repo"]},
      "output": {"token_type": "bearer", "scope": "repo read:user"},
      "agent": "Orchestrator"
    }
  ],
  "agent_errors": []
}
```

**Response fields:**

| Field              | Type         | Description                                        |
|--------------------|--------------|----------------------------------------------------|
| `status`           | string       | `success` / `partial` / `error`                    |
| `pr_url`           | string\|null | GitHub PR URL (null if no PR created)              |
| `pr_number`        | int\|null    | GitHub PR number                                   |
| `risk_level`       | string       | `CRITICAL` / `HIGH` / `MEDIUM` / `LOW`             |
| `issues_found`     | array        | List of issue dicts from Risks Agent               |
| `summary`          | string       | Markdown executive summary from Digest Agent       |
| `token_vault_used` | bool         | Always `true` in live mode — proof for judges      |
| `reasoning`        | array        | Full step trail from all agents                    |
| `agent_errors`     | array        | List of agent error strings (empty if all healthy) |

---

## Planner (port 8601)

### `plan_with_reasoning`

Classify a goal and decompose into subtasks.

**Request:**
```json
{
  "method": "tools/plan_with_reasoning",
  "params": {
    "description": "Fix JWT secret hardcoded in source. The application has SECRET_KEY hardcoded as a string literal",
    "context": "GitHub repository — security and DevOps automation",
    "title": "Fix JWT secret hardcoded in source",
    "issue_number": 17,
    "repo": "owner/repo",
    "file_tree": ["src/auth.py", "src/config.py", "requirements.txt"]
  }
}
```

**Response:**
```json
{
  "classification": {"task_type": "security_fix", "complexity": "high", "priority_hint": "P0"},
  "complexity": "high",
  "estimated_hours": 4,
  "priority": "P0",
  "subtasks": [
    {
      "id": 1,
      "title": "Move SECRET_KEY to environment variable",
      "priority": "critical",
      "estimated_hours": 1
    },
    {
      "id": 2,
      "title": "Rotate existing exposed secret",
      "priority": "critical",
      "estimated_hours": 0.5
    }
  ],
  "reasoning": [...]
}
```

### `risk_aware_planning`

Planning with risk integration from Risks Agent output.
```json
{
  "method": "tools/risk_aware_planning",
  "params": {
    "description": "Add OAuth2 authentication — Implement GitHub OAuth2 login flow",
    "context": "Enterprise SaaS platform",
    "repo": "owner/repo"
  }
}
```

---

## Risks (port 8603)

### `analyze_risks`

Full proactive risk assessment with LLM analysis.

**Request:**
```json
{
  "method": "tools/analyze_risks",
  "params": {
    "feature": "Implement OAuth2 authentication with GitHub",
    "repo": "owner/repo",
    "issue_number": 17,
    "title": "Add GitHub OAuth login",
    "context": "Enterprise SaaS platform, 10k users",
    "classification": {
      "task_type": "security_fix",
      "complexity": "high"
    },
    "file_tree": ["src/auth.py", "src/config.py"]
  }
}
```

**Parameters:**

| Field            | Type   | Required | Description                          |
|------------------|--------|----------|--------------------------------------|
| `feature`        | string | ✅        | Feature description to assess        |
| `repo`           | string | ❌        | Repository context                   |
| `issue_number`   | int    | ❌        | GitHub issue number for traceability |
| `title`          | string | ❌        | Issue title                          |
| `context`        | string | ❌        | Project context                      |
| `classification` | dict   | ❌        | Output from Planner Agent            |
| `file_tree`      | list   | ❌        | Repo file names for context          |

**Response:**
```json
{
  "feature": "Implement OAuth2 authentication with GitHub",
  "overall_risk_level": "HIGH",
  "priority": "P1",
  "analysis_mode": "llm",
  "issues_found": [
    {
      "title": "Authentication design complexity",
      "severity": "high",
      "category": "security",
      "mitigation_strategy": "Conduct security design review before implementation",
      "priority": 1
    },
    {
      "title": "Third-party integration risk",
      "severity": "medium",
      "category": "integration_risk",
      "mitigation_strategy": "Implement circuit breakers and rate limiting",
      "priority": 2
    }
  ],
  "executive_summary": {
    "overall_risk_level": "HIGH",
    "total_risks": 2,
    "critical_count": 0,
    "high_count": 1,
    "medium_count": 1,
    "go_no_go_recommendation": "CONDITIONAL: Proceed with senior oversight",
    "confidence_level": "high"
  },
  "automated_actions": {
    "actions": ["require_senior_review", "require_security_review", "notify_pm"],
    "block_pr": false,
    "require_review": true
  },
  "reasoning": [...]
}
```

### `assess_feature_risk`

Quick baseline-only assessment (no LLM call).
```json
{
  "method": "tools/assess_feature_risk",
  "params": {
    "feature": "Add rate limiting middleware",
    "complexity": "low",
    "repo": "owner/repo"
  }
}
```

---

## Code Execution (port 8605)

### `generate_fix_and_create_pr`

Primary tool — generates fix, tests, debug loop, returns patch files.

**Request:**
```json
{
  "method": "tools/generate_fix_and_create_pr",
  "params": {
    "repo": "owner/repo",
    "goal": "Fix hardcoded JWT secret — move to environment variable",
    "risks": [
      {"severity": "high", "title": "Hardcoded secret in source"}
    ],
    "classification": {
      "task_type": "security_fix",
      "complexity": "medium"
    },
    "file_tree": ["src/auth.py", "src/config.py", "requirements.txt"],
    "max_debug_iter": 2
  }
}
```

> **Note:** The Orchestrator does **not** pass `github_token` to Code Execution.
> Code Execution generates patch files; the Orchestrator handles all GitHub API calls itself.

**Response:**
```json
{
  "patch_files": [
    {
      "path": "src/security/auth_fix.py",
      "content_base64": "aW1wb3J0IG9zCmltcG9ydCBqd3Q...",
      "commit_message": "fix(security): move JWT secret to environment variable",
      "sha": null
    }
  ],
  "code_diff": "--- a/src/security/auth_fix.py\n+++ b/src/security/auth_fix.py\n...",
  "tests_passed": true,
  "quality_score": 0.87,
  "session_id": "session_20260301_142200",
  "code": "import os\nimport jwt\n...",
  "quality_metrics": {
    "quality_score": 0.87,
    "production_ready": true,
    "confidence": 0.9
  },
  "verification_artifact": {
    "artifact_id": "artifact_session_20260301_142200_iter1",
    "artifact_type": "test_report",
    "tests_passed": 5,
    "tests_failed": 0,
    "exit_code": 0,
    "execution_time_ms": 312.4,
    "quality_score": 0.87,
    "production_ready": true,
    "test_details": [
      {"test_id": "test_1", "passed": true, "execution_time_ms": 45.2}
    ]
  },
  "reasoning": [...]
}
```

### `generate_and_test_code`

Standalone code generation and test cycle (not called by Orchestrator).
```json
{
  "method": "tools/generate_and_test_code",
  "params": {
    "requirement": "Write a JWT token generator and validator with refresh support",
    "context": "Python 3.11, PyJWT library",
    "language": "python"
  }
}
```

### `autonomous_debug_loop`

Continue debugging a previous session.
```json
{
  "method": "tools/autonomous_debug_loop",
  "params": {
    "session_id": "session_20260301_142200",
    "max_iterations": 3
  }
}
```

---

## Progress (port 8602)

### `track_progress`

Calculate repository health from pipeline results.

**Request:**
```json
{
  "method": "tools/track_progress",
  "params": {
    "repo": "owner/repo",
    "pr_created": true,
    "issues_found_count": 3,
    "risk_level": "HIGH",
    "issues_resolved_count": 1,
    "total_issues_count": 8
  }
}
```

**Response:**
```json
{
  "repo": "owner/repo",
  "health_status": "on_track",
  "velocity": "on_track",
  "urgency": "medium",
  "executive_summary": {
    "status": "ON_TRACK",
    "completion_rate": "12.5%",
    "headline": "On track — 12.5% complete",
    "interpretation": "The AI agent resolved 1 of 8 open issues...",
    "auto_actions": ["update_dashboard"],
    "urgency": "medium",
    "confidence": "medium",
    "llm_enhanced": true
  },
  "metrics": {
    "total_issues": 8,
    "done": 1,
    "completion_rate": 12.5,
    "pr_created": true,
    "issues_found": 3,
    "risk_level": "HIGH"
  },
  "automated_actions": {
    "actions": ["update_dashboard"],
    "triggers": {
      "notify_pm": false,
      "alert_leadership": false,
      "celebrate": false,
      "slack_notify": false
    }
  },
  "reasoning": [...]
}
```

### `analyze_progress`

Analyse commit messages for velocity signals.
```json
{
  "method": "tools/analyze_progress",
  "params": {
    "commits": [
      "fix(auth): remove hardcoded JWT secret",
      "feat: add environment variable validation",
      "test: add auth token integration tests"
    ],
    "repo": "owner/repo"
  }
}
```

---

## Digest (port 8604)

### `generate_digest`

Generate executive summary from full pipeline results.

**Request:**
```json
{
  "method": "tools/generate_digest",
  "params": {
    "repo": "owner/repo",
    "goal": "Fix JWT token validation vulnerability",
    "risk_level": "HIGH",
    "issues_found": [
      {"title": "Hardcoded JWT secret", "severity": "high"}
    ],
    "pr_url": "https://github.com/owner/repo/pull/42",
    "pr_number": 42,
    "progress": {
      "health_status": "on_track",
      "metrics": {"completion_rate": 12.5}
    }
  }
}
```

**Response:**
```json
{
  "summary": "## 📋 Summary\n\nThe AI agent addressed a high-severity JWT secret exposure in `owner/repo`. A pull request was created with the proposed fix...\n\n## 🔐 Security & Risk\n\n- Overall risk: **HIGH**\n- Issues identified: 1\n  - [HIGH] Hardcoded JWT secret\n\n## 🔗 Pull Request\n\nPR #42: https://github.com/owner/repo/pull/42\n\n## ✅ Next Steps\n\n- Review the PR and run the full test suite\n- Rotate the exposed JWT secret immediately\n- Re-run the agent after applying fixes",
  "slack_message": "*🤖 AI DevOps Agent — Pipeline Complete*\n*Repo:* `owner/repo`\n*Goal:* Fix JWT token validation vulnerability\n🟠 *Risk:* `HIGH`  |  ⚠️ *Health:* `on_track`  |  *Issues found:* 1\n🔗 <https://github.com/owner/repo/pull/42|View Pull Request>",
  "quality_state": "HEALTHY",
  "validation": {
    "word_count": 187,
    "under_limit": true,
    "has_pr_section": true,
    "has_risk_section": true,
    "confidence": 0.9
  },
  "automated_actions": {
    "actions": ["post_to_github_comment", "send_to_slack", "notify_pm"],
    "escalation_level": "pm",
    "celebration": false
  },
  "reasoning": [...]
}
```

---

## Health Checks
```bash
# All agents
curl http://localhost:8600/health   # Orchestrator
curl http://localhost:8601/health   # Planner
curl http://localhost:8602/health   # Progress
curl http://localhost:8603/health   # Risks
curl http://localhost:8604/health   # Digest
curl http://localhost:8605/health   # Code Execution
```

Expected response:
```json
{
  "status": "ok",
  "agent": "Risks"
}
```

---

## Error Responses

All agents return consistent error shapes:
```json
{
  "error": "Unknown tool: nonexistent_tool",
  "available_tools": ["analyze_risks", "assess_feature_risk"]
}
```
```json
{
  "error": "Invalid parameters for 'analyze_risks'",
  "details": "missing required argument: 'feature'",
  "received_params": [],
  "expected_params": ["feature", "issue_number", "title", "context", "classification", "file_tree", "repo"]
}
```