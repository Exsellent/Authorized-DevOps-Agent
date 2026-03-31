# Agent Comparison — Authorized DevOps Agent

## Overview

The system uses 6 specialized sub-agents coordinated by an Orchestrator.
Each agent has a distinct responsibility and does NOT import `auth0_token_vault`.

### Side-by-Side Comparison

```
| Property            | Orchestrator | Planner |   Risks     | Code Execution | Progress | Digest     |
|---------------------|:------------:|:-------:|:-----------:|:--------------:|:--------:|:----------:|
| Port                | 8600         | 8601    | 8603        | 8605           | 8602     | 8604       |
| Auth0 Token Vault   | ✅           | ❌      |     ❌     | ❌             | ❌       | ❌         |
| GitHub API          | ✅           | ❌      |     ❌     | ❌             | ❌       | ❌         |
| Claude LLM          | ✅           | ✅      |     ✅     | ✅             | ✅       | ✅         |
| Calls other agents  | ✅ all 5     | ❌      |     ❌     | ❌             | ❌       | ❌         |
| Fallback strategy   | partial      | baseline| baseline    | baseline       | baseline | structured |
| Has debug loop      | ❌           | ❌      |     ❌     | ✅             | ❌       | ❌         |
| Returns patch_files | ❌           | ❌      |     ❌     | ✅             | ❌       | ❌         |
| Returns Slack msg   | ❌           | ❌      |     ❌     | ❌             | ❌       | ✅         |
```

## Orchestrator

**Role:** Central coordinator. Entry point for every pipeline run.

**Unique responsibilities:**

- Auth0 Token Vault → scoped GitHub token (only agent with `AUTH0_CLIENT_SECRET`)
- GitHub API: read repo, create branch, commit files, create PR
- Sequence and error-handle all 5 sub-agents
- Return `token_vault_used: true` as proof for judges

**Primary tool:** `run_secure_devops_flow`

**Does NOT:**

- Generate code
- Assess risks directly
- Store the GitHub token beyond the request

---

## Planner

**Role:** Understand the goal and create an actionable execution plan.

**Unique responsibilities:**

- Classify goal into `task_type` (security_fix, dependency_update, api_development, bug_fix, feature)
- Decompose into subtasks with priority ordering
- Estimate effort using historical velocity data (ML-based)
- Assess complexity: `low` / `medium` / `high`

**Primary tool:** `plan_with_reasoning`

**Output consumed by:**

- Orchestrator (classification → passed to Risks and Code Execution)
- Risks Agent (`complexity` field affects overall risk level)
- Code Execution Agent (`task_type` → fix strategy and filename)

**Fallback:** JSON parse failure → `_safe_parse_json` extracts partial data.

---

## Risks

**Role:** Pre-implementation risk assessment. NOT a vulnerability scanner.

**Distinction:**

| Risks Agent (this)             | Vulnerability Scanner             |
|--------------------------------|-----------------------------------|
| Assesses PLANNING risks        | Analyses EXISTING vulnerabilities |
| Pre-implementation             | Post-scan / post-deployment       |
| Design decisions, architecture | CVEs, SAST/DAST findings          |
| Feeds go/no-go decision        | Feeds patch prioritisation        |

**Risk categories:**

| Category           | Examples                                         |
|--------------------|--------------------------------------------------|
| `security`         | Auth, secrets, token handling                    |
| `design_risk`      | Architectural complexity, unclear requirements   |
| `business_impact`  | Revenue risk, customer-facing failure            |
| `integration_risk` | API compatibility, third-party dependencies      |
| `scalability`      | Performance under load, DB scaling               |
| `technical_debt`   | Maintenance burden, code complexity              |
| `compliance`       | GDPR, SOC2, audit requirements                   |
| `operational`      | CI/CD pipeline, deployment risks, GitHub Actions |

**Analysis modes:**

| Mode       | Trigger                        | Data source                       |
|------------|--------------------------------|-----------------------------------|
| `llm`      | LLM returns valid JSON         | Claude 3.5 Sonnet (full analysis) |
| `hybrid`   | LLM responded but JSON invalid | Baseline supplemented             |
| `baseline` | LLM unavailable / timeout      | Deterministic pattern matching    |

**Bug fixed vs. original:** Original code always fell back to baseline even when
LLM returned valid JSON. Fixed with `_parse_llm_risks()` that actually parses
the Claude JSON response.

**Primary tool:** `analyze_risks`

---

## Code Execution

**Role:** Generate working code fixes, verify through execution, prepare for GitHub commit.

**Unique responsibilities:**

- Generate Python fix code (STRATEGIC → GENERATION phases)
- Write executable `test_*` functions (not descriptions)
- Run tests in isolated subprocesses
- Self-correct through debug loop (REFLECTION phase)
- Return base64 patch files for GitHub API

**Thinking levels:**

```
STRATEGIC    → understand the goal, plan what to build
GENERATION   → write fix code + test suite
EXECUTION    → run subprocess, collect stdout/stderr/exit_code
VERIFICATION → calculate quality_score, production_ready
REFLECTION   → fix failures, re-run (up to max_debug_iter times)
```

**Bugs fixed vs. original Gemini agent:**

| Bug                                    | Root Cause                                                   | Fix                                                   |
|----------------------------------------|--------------------------------------------------------------|-------------------------------------------------------|
| `ModuleNotFoundError` — all tests fail | No pip install before subprocess                             | `_install_deps()` auto-installs before execution      |
| All 5 tests run identical code         | Regex found function names but used full file as `test_code` | `_split_test_functions()` isolates each `test_*` body |
| Golang requested → Python generated    | `language` param silently ignored                            | Explicit warning + enforced Python-only               |
| `ReasoningStep(output_data=...)`       | Wrong kwarg name vs dataclass field `output=`                | Fixed to `output=`                                    |

**Quality scoring (deterministic — agent decides, not LLM):**

```python
pass_rate = tests_passed / tests_total
perf_score = 1.0 if exec_ms < 1000 else 0.8
size_score = 1.0 if 20 <= code_len <= 2000 else 0.7
score = (pass_rate * 0.7) + (perf_score * 0.2) + (size_score * 0.1)
production_ready = (tests_passed == tests_total and exec_ms < 5000)
```

**Primary tool:** `generate_fix_and_create_pr`

---

## Progress

**Role:** Calculate repo health metrics from pipeline results.

**Unique responsibilities:**

- Classify velocity: `excellent` / `on_track` / `at_risk` / `critical`
- Determine urgency and escalation actions
- Factor in Risks Agent output (risk_level overrides velocity if CRITICAL)
- Produce confidence score based on data completeness

**Does NOT call GitHub API** — all data comes from Orchestrator parameters.

**Velocity thresholds:**

| Status      | Completion % |  Urgency  | Actions                            |
|-------------|:------------:|:---------:|------------------------------------|
| `excellent` |    ≥ 75%     |    low    | celebrate_team                     |
| `on_track`  |    ≥ 50%     |  medium   | update_dashboard                   |
| `at_risk`   |    ≥ 25%     |   high    | notify_pm, suggest_scope_reduction |
| `critical`  |    < 25%     | immediate | alert_leadership, schedule_standup |

**Override rule:** If `risk_level == CRITICAL` and `velocity == excellent`,
overrides to `at_risk` — risk trumps velocity.

**Primary tool:** `track_progress`

---

## Digest

**Role:** Generate executive summary and Slack notification from all pipeline results.

**Unique responsibilities:**

- Synthesize all agent outputs into human-readable Markdown
- Build compact Slack message for `_build_slack_message()`
- Validate report quality (`HEALTHY` / `WARNING` / `DEGRADED`)
- Return `summary` and `slack_message` fields consumed by Orchestrator

**Quality validation checks:**

| Check              | Keywords detected                               |
|--------------------|-------------------------------------------------|
| `has_pr_section`   | "pull request", "branch", "pr #", "commit"      |
| `has_risk_section` | "risk", "security", "vulnerability", "critical" |
| `has_summary`      | word_count ≥ 40                                 |

**Bug fixed vs. original:** Original validated `has_mood` ("morale", "atmosphere")
as a required field for a DevOps report. Replaced with `has_pr_section` and
`has_risk_section`. `MAX_WORD_COUNT` raised from 200 → 500 (200 was too tight
for a PR summary with a risk list).

**Primary tool:** `generate_digest`

---

## Fallback Strategy Matrix

All agents implement graceful degradation when LLM is unavailable:

| Agent          |        LLM Available        |              LLM Unavailable               |
|----------------|:---------------------------:|:------------------------------------------:|
| Planner        |       Full JSON plan        |   `_safe_parse_json` partial extraction    |
| Risks          |    Claude JSON analysis     | Deterministic pattern matching on keywords |
| Code Execution |    Generated fix + tests    |        Minimal stub + fallback test        |
| Progress       | LLM velocity interpretation |        `_baseline_analysis()` text         |
| Digest         |    LLM executive summary    |        Structured Markdown template        |

The Orchestrator continues the pipeline even if individual agents fall back —
partial results are better than no results for the demo.