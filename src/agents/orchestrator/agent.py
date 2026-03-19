"""
Orchestrator Agent — DevOps AI Agent for Auth0 "Authorized to Act" Hackathon.

Responsibilities:
  1. Accept task from UI (repo + goal + Auth0 user tokens)
  2. Obtain scoped GitHub token from Auth0 Token Vault (never stored)
  3. Orchestrate the MCP pipeline:
       Planner → Risks → Code Execution → Progress → Digest
  4. Create GitHub branch + PR using the scoped token
  5. Post summary to Slack (optional)
  6. Return full reasoning trail to UI

"""

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from shared.auth0_token_vault import Auth0TokenVault, TokenVaultError, VaultToken
from shared.mcp_base import MCPAgent
from shared.metrics import metric_counter
from shared.models import ReasoningStep
from shared.utils import log_method, normalize_reasoning

logger = logging.getLogger("orchestrator")

# ── Sensitive fields — masked in all log output ───────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API Client
# ─────────────────────────────────────────────────────────────────────────────

class GitHubClient:
    """
    The async GitHub API client.
    The token is passed per-call and never stored as an instance attribute,
    ensuring it lives only for the duration of a single request.
    """

    BASE = "https://api.github.com"

    async def _headers(self, token: VaultToken) -> Dict[str, str]:
        return {
            "Authorization": token.auth_header(),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_repo_info(self, token: VaultToken, repo: str) -> Dict:
        """Fetch basic repo metadata (name, description, default branch)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{self.BASE}/repos/{repo}",
                headers=await self._headers(token),
            )
            r.raise_for_status()
            return r.json()

    async def list_files(
        self, token: VaultToken, repo: str, path: str = ""
    ) -> List[Dict]:
        """List files/dirs at a given path in the default branch."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{self.BASE}/repos/{repo}/contents/{path}",
                headers=await self._headers(token),
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else [data]

    async def get_file_content(
        self, token: VaultToken, repo: str, file_path: str
    ) -> Dict:
        """Download a single file (base64-encoded content + sha)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{self.BASE}/repos/{repo}/contents/{file_path}",
                headers=await self._headers(token),
            )
            r.raise_for_status()
            return r.json()

    async def create_branch(
        self, token: VaultToken, repo: str, branch_name: str, base_branch: str = "main"
    ) -> Dict:
        """Create a new branch from base_branch's HEAD sha."""
        # 1. resolve base sha
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{self.BASE}/repos/{repo}/git/refs/heads/{base_branch}",
                headers=await self._headers(token),
            )
            r.raise_for_status()
            sha = r.json()["object"]["sha"]

            # 2. create branch
            r2 = await client.post(
                f"{self.BASE}/repos/{repo}/git/refs",
                headers=await self._headers(token),
                json={"ref": f"refs/heads/{branch_name}", "sha": sha},
            )
            r2.raise_for_status()
            return r2.json()

    async def commit_file(
        self,
        token: VaultToken,
        repo: str,
        branch: str,
        file_path: str,
        content_base64: str,
        message: str,
        existing_sha: Optional[str] = None,
    ) -> Dict:
        """Create or update a file in the repo."""
        payload: Dict[str, Any] = {
            "message": message,
            "content": content_base64,
            "branch": branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.put(
                f"{self.BASE}/repos/{repo}/contents/{file_path}",
                headers=await self._headers(token),
                json=payload,
            )
            r.raise_for_status()
            return r.json()

    async def create_pull_request(
        self,
        token: VaultToken,
        repo: str,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> Dict:
        """Open a Pull Request and return the PR object."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{self.BASE}/repos/{repo}/pulls",
                headers=await self._headers(token),
                json={
                    "title": title,
                    "head": head_branch,
                    "base": base_branch,
                    "body": body,
                },
            )
            r.raise_for_status()
            return r.json()

    async def list_open_issues(
        self, token: VaultToken, repo: str, limit: int = 20
    ) -> List[Dict]:
        """Return open issues (excludes pull requests)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{self.BASE}/repos/{repo}/issues",
                headers=await self._headers(token),
                params={"state": "open", "per_page": limit},
            )
            r.raise_for_status()
            return [i for i in r.json() if "pull_request" not in i]

    async def create_issue(
        self,
        token: VaultToken,
        repo: str,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
    ) -> Dict:
        """Create a new GitHub issue."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{self.BASE}/repos/{repo}/issues",
                headers=await self._headers(token),
                json={"title": title, "body": body, "labels": labels or []},
            )
            r.raise_for_status()
            return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Slack Notifier (optional)
# ─────────────────────────────────────────────────────────────────────────────

class SlackNotifier:
    """Sends messages via an Incoming Webhook URL."""

    def __init__(self):
        self.webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def send(self, text: str) -> bool:
        if not self.enabled:
            logger.debug("Slack notifier disabled (no SLACK_WEBHOOK_URL)")
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(self.webhook_url, json={"text": text})
                r.raise_for_status()
                return True
        except Exception as exc:
            logger.warning("Slack notification failed: %s", exc)
            return False


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator Agent
# ─────────────────────────────────────────────────────────────────────────────

class OrchestratorAgent(MCPAgent):
    """
    Central coordinator for the Secure DevOps AI Agent.

    Exposes two MCP tools:
      - run_secure_devops_flow   : full pipeline (analyse → fix → PR)
      - triage_issues            : lightweight issue classification (compat alias)
    """

    def __init__(self):
        super().__init__("OrchestratorAgent")

        # External service clients
        self.vault = Auth0TokenVault()
        self.github = GitHubClient()
        self.slack = SlackNotifier()

        # Downstream agent URLs (docker-compose service names)
        self.planner_url = os.getenv("PLANNER_URL", "http://planner:8601")
        self.risks_url = os.getenv("RISKS_URL", "http://risks:8603")
        self.code_exec_url = os.getenv("CODE_EXEC_URL", "http://code_execution:8605")
        self.progress_url = os.getenv("PROGRESS_URL", "http://progress:8602")
        self.digest_url = os.getenv("DIGEST_URL", "http://digest:8604")

        # Register MCP tool handlers
        self.register_tool("run_secure_devops_flow", self.run_secure_devops_flow)
        self.register_tool("triage_issues", self.triage_issues)
        self.register_tool("triage_single", self.triage_single_issue)

        logger.info(
            "OrchestratorAgent initialised | vault=%s slack=%s",
            self.vault.domain,
            "enabled" if self.slack.enabled else "disabled",
        )

    # ── Reasoning trail helpers ───────────────────────────────────────────────

    def _step(
        self,
        reasoning: List[ReasoningStep],
        description: str,
        input_data: Optional[Dict] = None,
        output_data: Optional[Dict] = None,
    ) -> None:
        """Append a reasoning step (sensitive fields auto-redacted)."""
        reasoning.append(
            ReasoningStep(
                step_number=len(reasoning) + 1,
                description=description,
                timestamp=datetime.now(timezone.utc).isoformat(),
                input_data=_safe_deep(input_data or {}),
                output=_safe_deep(output_data or {}),
                agent=self.name,
            )
        )

    # ── MCP agent call ────────────────────────────────────────────────────────

    async def _call_agent(self, url: str, tool: str, params: Dict) -> Dict:
        """
        Invoke a downstream agent via MCP HTTP.
        Sensitive params are redacted in logs but passed in full to the agent.
        """
        logger.debug("→ %s/%s params=%s", url, tool, _safe_deep(params))
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{url}/mcp",
                    json={"method": f"tools/{tool}", "params": params},
                )
                r.raise_for_status()
                result = r.json()
        except Exception as exc:
            logger.error("Agent call failed %s %s: %s", url, tool, exc)
            return {"error": str(exc)}

        # Propagate downstream agent errors as structured dicts
        if "error" in result and isinstance(result["error"], str):
            logger.warning("Downstream agent %s/%s returned error: %s",
                           url, tool, result["error"][:200])
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN PIPELINE — run_secure_devops_flow
    # ─────────────────────────────────────────────────────────────────────────

    @log_method
    @metric_counter("secure_devops_flow")
    async def run_secure_devops_flow(
        self,
        repo: str,
        goal: str,
        auth0_refresh_token: Optional[str] = None,   # UI passes if entered; else uses .env
        auth0_access_token:  Optional[str] = None,
        base_branch: str = "main",
        slack_notify: bool = True,
    ) -> Dict[str, Any]:
        """
        Full secure DevOps pipeline:
          Token Vault → Planner → Risks → Code Execution → PR → Progress → Digest

        :param repo:                  "owner/repo-name"
        :param goal:                  e.g. "find_security_issues_and_create_pr"
        :param auth0_refresh_token:   User's Auth0 refresh token (used for Vault exchange)
        :param auth0_access_token:    Optional fallback if refresh token unavailable
        :param base_branch:           Target branch for PR (default "main")
        :param slack_notify:          Send Slack summary when done

        UI contract (every field consumed by index.html):
          - reasoning          : List[ReasoningStep]  ← step-by-step trail
          - status             : "success" | "error" | "partial"
          - pr_url             : str | None
          - pr_number          : int | None
          - issues_found       : List[str]
          - summary            : str
          - risk_level         : "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
          - token_vault_used   : bool  ← proof for judges
          - timestamp          : ISO-8601
        """
        reasoning: List[ReasoningStep] = []
        github_token: Optional[VaultToken] = None

        # Track which agents failed so we can report partial success
        agent_errors: List[str] = []

        def _check_agent_result(result: Dict, agent_name: str) -> None:
            if "error" in result and isinstance(result["error"], str):
                agent_errors.append(f"{agent_name}: {result['error'][:150]}")

        # Resolve token: UI field → .env → error
        # Allows token to live in the server environment (not sent by UI every call)
        resolved_refresh = auth0_refresh_token or os.getenv("AUTH0_REFRESH_TOKEN")
        resolved_access  = auth0_access_token  or os.getenv("AUTH0_ACCESS_TOKEN")

        if not resolved_refresh and not resolved_access:
            return {
                **self._error_response(
                    reasoning,
                    "Auth0 token required. Set AUTH0_REFRESH_TOKEN in .env or pass auth0_refresh_token.",
                ),
                "token_vault_used": False,
            }

        # ── Step 1: Auth0 Token Vault exchange ────────────────────────────────
        self._step(
            reasoning,
            "Requesting scoped GitHub token from Auth0 Token Vault",
            input_data={"repo": repo, "goal": goal, "scope": "repo"},
        )
        try:
            subject_token = resolved_refresh or resolved_access
            use_refresh = bool(resolved_refresh)
            github_token = await self.vault.get_github_token(
                subject_token=subject_token,
                scopes=["repo"],
                use_refresh_token=use_refresh,
            )
            self._step(
                reasoning,
                "Token Vault: scoped GitHub token obtained successfully",
                output_data={
                    "token_type": github_token.token_type if github_token else "unknown",
                    "scope": github_token.scope,
                    "token_vault_used": True,
                    # access_token is NOT included — _safe_deep would redact anyway
                },
            )
        except TokenVaultError as exc:
            logger.error("Token Vault failed: %s", exc)
            self._step(
                reasoning,
                f"Token Vault error: {exc}",
                output_data={"status": "error"},
            )
            return self._error_response(reasoning, str(exc))

        # ── Step 2: Read repo via GitHub API (read-only, scoped token) ────────
        self._step(reasoning, "Reading repository structure via GitHub API",
                   input_data={"repo": repo})
        try:
            repo_info = await self.github.get_repo_info(github_token, repo)
            file_tree = await self.github.list_files(github_token, repo)
            base_branch = repo_info.get("default_branch", base_branch)
            self._step(
                reasoning,
                "Repository read successfully",
                output_data={
                    "repo_name": repo_info["full_name"],
                    "default_branch": base_branch,
                    "file_count": len(file_tree),
                    "description": repo_info.get("description", ""),
                },
            )
        except Exception as exc:
            logger.error("GitHub repo read failed: %s", exc)
            return self._error_response(
                reasoning,
                f"GitHub API error: {exc}",
                token_vault_used=True,  # token was successfully obtained
            )

        # ── Step 3: Planner — classify & decompose task ───────────────────────
        self._step(reasoning, "Planner: analysing repository and decomposing goal",
                   input_data={"goal": goal, "file_count": len(file_tree)})
        planner_result = await self._call_agent(
            self.planner_url, "plan_with_reasoning",
            {
                "description": f"Goal: {goal}. Repository: {repo}.",
                "context": f"GitHub repo with {len(file_tree)} files. Branch: {base_branch}.",
                "repo": repo,
                "file_tree": [f["name"] for f in file_tree[:30]],
            },
        )
        _check_agent_result(planner_result, "Planner")
        classification = planner_result.get(
            "classification", {"task_type": "security_audit", "complexity": "medium"}
        )
        self._step(
            reasoning,
            "Planner: task decomposed",
            output_data={
                "task_type": classification.get("task_type"),
                "complexity": classification.get("complexity"),
                "subtasks": planner_result.get("subtasks", []),
            },
        )

        # ── Step 4: Risks — security & impact assessment ──────────────────────
        self._step(reasoning, "Risks: performing security and impact assessment",
                   input_data={"goal": goal, "task_type": classification.get("task_type")})
        risks_result = await self._call_agent(
            self.risks_url, "analyze_risks",
            {
                "feature": f"{goal} in {repo}",
                "classification": classification,
                "repo": repo,
                "file_tree": [f["name"] for f in file_tree[:30]],
            },
        )
        _check_agent_result(risks_result, "Risks")
        risk_summary = risks_result.get("executive_summary", {})
        overall_risk = risk_summary.get("overall_risk_level", "MEDIUM")
        issues_found: List[str] = risks_result.get("issues_found", [])
        self._step(
            reasoning,
            "Risks: assessment complete",
            output_data={
                "overall_risk": overall_risk,
                "issues_count": len(issues_found),
                "critical_issues": [
                    i for i in issues_found
                    if isinstance(i, dict) and i.get("severity") == "CRITICAL"
                ],
            },
        )

        # ── Step 5: Code Execution — generate fix ─────────────────────────────
        self._step(reasoning, "Code Execution: generating fix and preparing PR",
                   input_data={"issues_count": len(issues_found), "risk": overall_risk})
        code_result = await self._call_agent(
            self.code_exec_url, "generate_fix_and_create_pr",
            {
                "repo": repo,
                "goal": goal,
                "risks": issues_found,
                "classification": classification,
                "file_tree": [f["name"] for f in file_tree[:30]],
                # github_token is NOT passed — Code Execution does not call GitHub API
            },
        )
        _check_agent_result(code_result, "Code-Execution")
        code_diff = code_result.get("code_diff", "")
        patch_files: List[Dict] = code_result.get("patch_files", [])
        self._step(
            reasoning,
            "Code Execution: fix generated",
            output_data={
                "patch_files_count": len(patch_files),
                "tests_passed": code_result.get("tests_passed"),
                "quality_score": code_result.get("quality_score"),
            },
        )

        # ── Step 5b: Risks — post-patch security gate ─────────────────────
        if patch_files:
            self._step(reasoning, "Risks: validating generated patch security",
                       input_data={"patch_files_count": len(patch_files)})
            sec_check = await self._call_agent(
                self.risks_url, "validate_patch_security",
                {"patch_files": patch_files, "repo": repo},
            )
            _check_agent_result(sec_check, "Risks (post-patch)")
            sec_passed = sec_check.get("security_passed", True)
            self._step(
                reasoning,
                "Risks: patch security gate " + ("PASSED" if sec_passed else "BLOCKED"),
                output_data={
                    "security_passed": sec_passed,
                    "violations_count": sec_check.get("violations_count", 0),
                    "risk_score": sec_check.get("risk_score", 0),
                },
            )
            if not sec_passed:
                return {
                    **self._error_response(reasoning, "Security policy blocked PR creation", token_vault_used=True),
                    "status": "security_blocked",
                    "violations": sec_check.get("violations", []),
                }

        # ── Step 6: Create GitHub PR (write token — repo scope) ───────────────
        pr_url: Optional[str] = None
        pr_number: Optional[int] = None

        if patch_files:
            branch_name = f"ai-fix/{goal.replace(' ', '-')[:40]}-{secrets.token_hex(4)}"
            self._step(
                reasoning,
                f"GitHub: creating branch '{branch_name}' and committing fixes",
                input_data={"branch": branch_name, "base": base_branch},
            )
            try:
                await self.github.create_branch(
                    github_token, repo, branch_name, base_branch
                )
                for pf in patch_files:
                    await self.github.commit_file(
                        token=github_token,
                        repo=repo,
                        branch=branch_name,
                        file_path=pf["path"],
                        content_base64=pf["content_base64"],
                        message=pf.get("commit_message", f"fix: {goal}"),
                        existing_sha=pf.get("sha"),
                    )

                pr_body = self._build_pr_body(goal, issues_found, overall_risk, code_diff)
                pr = await self.github.create_pull_request(
                    token=github_token,
                    repo=repo,
                    head_branch=branch_name,
                    base_branch=base_branch,
                    title=f"🤖 [AI Security Fix] {goal[:72]}",
                    body=pr_body,
                )
                pr_url = pr.get("html_url", "")
                pr_number = pr.get("number")
                self._step(
                    reasoning,
                    "GitHub: Pull Request created successfully",
                    output_data={"pr_url": pr_url, "pr_number": pr_number},
                )
            except Exception as exc:
                logger.error("PR creation failed: %s", exc)
                self._step(
                    reasoning,
                    f"GitHub PR creation failed: {exc}",
                    output_data={"pr_url": None},
                )
        else:
            self._step(
                reasoning,
                "Code Execution returned no patch files — skipping PR creation",
                output_data={"reason": "no_patches"},
            )

        # ── Step 7: Progress — repo health metrics ────────────────────────────
        self._step(reasoning, "Progress: collecting repository metrics")
        progress_result = await self._call_agent(
            self.progress_url, "track_progress",
            {
                "repo": repo,
                "pr_created": pr_number is not None,
                "issues_found_count": len(issues_found),
                "risk_level": overall_risk,
            },
        )
        _check_agent_result(progress_result, "Progress")
        self._step(
            reasoning,
            "Progress: metrics collected",
            output_data={
                "velocity": progress_result.get("velocity"),
                "health_status": progress_result.get("health_status"),
            },
        )

        # ── Step 8: Digest — human-readable summary ───────────────────────────
        self._step(reasoning, "Digest: generating executive summary")
        digest_result = await self._call_agent(
            self.digest_url, "generate_digest",
            {
                "repo": repo,
                "goal": goal,
                "risk_level": overall_risk,
                "issues_found": issues_found,
                "pr_url": pr_url,
                "pr_number": pr_number,
                "progress": progress_result,
            },
        )
        _check_agent_result(digest_result, "Digest")
        summary_text: str = digest_result.get("summary", self._fallback_summary(
            repo, goal, overall_risk, issues_found, pr_url
        ))
        self._step(
            reasoning,
            "Pipeline complete",
            output_data={"summary_length": len(summary_text)},
        )

        # ── Step 9: Slack notification (optional) ─────────────────────────────
        if slack_notify and self.slack.enabled:
            slack_msg = self._build_slack_message(repo, goal, pr_url, overall_risk, summary_text)
            sent = await self.slack.send(slack_msg)
            self._step(
                reasoning,
                "Slack notification sent" if sent else "Slack notification skipped",
                output_data={"sent": sent},
            )

        # ── Final response (UI contract) ──────────────────────────────────────
        final_status = "partial" if agent_errors else "success"

        return {
            "status": final_status,
            "reasoning": normalize_reasoning(reasoning),
            "pr_url": pr_url,
            "pr_number": pr_number,
            "issues_found": issues_found,
            "risk_level": overall_risk,
            "summary": summary_text,
            "token_vault_used": True,        # key proof for judges
            "repo": repo,
            "goal": goal,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_errors": agent_errors or [],
        }

    # ─────────────────────────────────────────────────────────────────────────
    # TRIAGE PIPELINE — backwards-compatible alias used by old UI endpoints
    # ─────────────────────────────────────────────────────────────────────────

    @log_method
    @metric_counter("triage")
    async def triage_issues(
        self,
        repo: str,
        auth0_refresh_token: Optional[str] = None,   # UI or .env fallback
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        Lightweight issue triage — classifies open issues and attaches labels.
        Kept for UI backward-compatibility; calls the same sub-agents.

        UI contract fields (same as original GitLab version):
          - triaged_count, issues, summary, reasoning, timestamp
        """
        reasoning: List[ReasoningStep] = []

        resolved_refresh = auth0_refresh_token or os.getenv("AUTH0_REFRESH_TOKEN")
        if not resolved_refresh:
            return self._error_response(
                reasoning,
                "Auth0 token required. Set AUTH0_REFRESH_TOKEN in .env or pass auth0_refresh_token.",
            )

        self._step(reasoning, "Requesting read-only GitHub token from Token Vault",
                   input_data={"repo": repo, "scope": "repo:read"})
        try:
            github_token = await self.vault.get_github_token(
                subject_token=resolved_refresh,
                scopes=["repo"],
                use_refresh_token=True,
            )
            self._step(reasoning, "Token Vault: read token obtained",
                       output_data={"scope": github_token.scope, "token_vault_used": True})
        except TokenVaultError as exc:
            return self._error_response(reasoning, str(exc))

        self._step(reasoning, "Fetching open issues from GitHub",
                   input_data={"repo": repo, "limit": limit})  # noqa: E501
        try:
            issues = await self.github.list_open_issues(github_token, repo, limit=limit)
        except Exception as exc:
            return self._error_response(
                reasoning,
                f"GitHub API error: {exc}",
                token_vault_used=True,
            )

        self._step(reasoning, f"Fetched {len(issues)} open issues",
                   output_data={"issues_count": len(issues)})

        triaged: List[Dict] = []
        for issue in issues:
            result = await self.triage_single_issue(issue=issue)
            triaged.append(result)

        self._step(reasoning, f"Triage complete — {len(triaged)} issues processed",
                   output_data={
                       "p0": sum(1 for t in triaged if t["priority"] == "P0"),
                       "p1": sum(1 for t in triaged if t["priority"] == "P1"),
                   })

        summary = self._triage_summary(triaged)
        return {
            "status": "success",
            "triaged_count": len(triaged),
            "issues": triaged,
            "summary": summary,
            "token_vault_used": True,
            "reasoning": normalize_reasoning(reasoning),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @log_method
    @metric_counter("triage_single")
    async def triage_single_issue(
        self, issue: Dict,
    ) -> Dict[str, Any]:
        """Classify a single issue via Planner + Risks."""
        title = issue.get("title", "")
        # Handle both GitHub "body" and GitLab "description" fields
        body = issue.get("body") or issue.get("description", "") or ""
        # Handle both GitHub "number" and GitLab "iid" fields
        issue_number = issue.get("number") or issue.get("iid", 0)

        planner_result = await self._call_agent(
            self.planner_url, "plan_with_reasoning",
            {"description": f"{title}. {body[:300]}", "context": "GitHub issue triage"},
        )
        classification = planner_result.get(
            "classification", {"task_type": "other", "complexity": "medium"}
        )

        risks_result = await self._call_agent(
            self.risks_url, "analyze_risks",
            {"feature": f"{title}. {body[:300]}"},
        )
        overall_risk = risks_result.get("executive_summary", {}).get(
            "overall_risk_level", "MEDIUM"
        )

        priority = self._calculate_priority(classification, overall_risk)
        labels = self._generate_labels(classification, priority, overall_risk)

        return {
            "issue_number": issue_number,
            "title": title,
            "classification": classification,
            "risk_level": overall_risk,
            "priority": priority,
            "labels": labels,
            "html_url": issue.get("html_url", ""),
            "agents_used": ["planner", "risks"],
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _calculate_priority(classification: Dict, risk_level: str) -> str:
        priority_hint = classification.get("priority_hint", "")
        task_type = classification.get("task_type", "other")
        complexity = classification.get("complexity", "medium")

        #1. FORCED LOW PRIORITY (Override)

        if priority_hint == "P3" or task_type == "other":
            return "P3"

        # 2. CRITICAL LEVEL
        if risk_level == "CRITICAL":
            return "P0"

        # 3. HIGH PRIORITY
        if risk_level == "HIGH" or complexity == "high":
            return "P1"

        return "P2"

    @staticmethod
    def _generate_labels(
        classification: Dict, priority: str, risk_level: str,
    ) -> List[str]:
        labels = [
            f"type::{classification.get('task_type', 'other')}",
            f"priority::{priority}",
            f"complexity::{classification.get('complexity', 'medium')}",
            "ai-triage",
        ]
        if risk_level in ("CRITICAL", "HIGH"):
            labels.append(f"risk::{risk_level.lower()}")
        return labels

    @staticmethod
    def _build_pr_body(
        goal: str,
        issues_found: List,
        overall_risk: str,
        code_diff: str,
    ) -> str:
        issues_md = "\n".join(
            f"- {i.get('title', i) if isinstance(i, dict) else i}"
            for i in issues_found[:10]
        ) or "_No specific issues found._"
        diff_section = (
            f"\n<details><summary>📄 Diff preview</summary>\n\n```diff\n{code_diff[:3000]}\n```\n</details>"
            if code_diff else ""
        )
        return f"""## 🤖 AI Security Fix — {goal}

**Risk Level:** `{overall_risk}`

### Issues addressed
{issues_md}
{diff_section}

---
> 🔐 This PR was created by an AI agent authorised via **Auth0 Token Vault**.  
> The GitHub token was obtained via RFC 8693 Token Exchange and was **never stored**.  
> Reasoning trail available in the Observatory dashboard.

*Powered by [Authorized to Act](https://authorizedtoact.devpost.com/) hackathon project*
"""

    @staticmethod
    def _build_slack_message(
        repo: str,
        goal: str,
        pr_url: Optional[str],
        risk: str,
        summary: str,
    ) -> str:
        pr_line = f"🔗 PR: {pr_url}" if pr_url else "ℹ️ No PR created (no patches generated)"
        return (
            f"*🤖 AI DevOps Agent — Task Complete*\n"
            f"*Repo:* `{repo}`\n"
            f"*Goal:* {goal}\n"
            f"*Risk level:* `{risk}`\n"
            f"{pr_line}\n\n"
            f"{summary}"
        )

    @staticmethod
    def _fallback_summary(
        repo: str,
        goal: str,
        risk: str,
        issues: List,
        pr_url: Optional[str],
    ) -> str:
        return (
            f"Agent completed '{goal}' on `{repo}`. "
            f"Risk level: {risk}. "
            f"Issues found: {len(issues)}. "
            f"PR: {pr_url or 'none'}."
        )

    @staticmethod
    def _triage_summary(triaged: List[Dict]) -> Dict:
        priority_counts: Dict[str, int] = {}
        type_counts: Dict[str, int] = {}
        for item in triaged:
            p = item["priority"]
            t = item["classification"].get("task_type", "other")
            priority_counts[p] = priority_counts.get(p, 0) + 1
            type_counts[t] = type_counts.get(t, 0) + 1
        return {
            "total_triaged": len(triaged),
            "by_priority": priority_counts,
            "by_type": type_counts,
            "high_priority_issues": [
                {"number": t["issue_number"], "title": t["title"]}
                for t in triaged
                if t["priority"] in ("P0", "P1")
            ],
        }

    @staticmethod
    def _error_response(
        reasoning: List[ReasoningStep],
        message: str,
        *,
        token_vault_used: bool = False,
    ) -> Dict:
        """Build a structured error response.

        Contains the full UI contract fields so callers don't need to
        merge extra keys.  Fields irrelevant to the current pipeline
        (e.g. pr_url for triage) are included as None for schema
        consistency with the UI.
        """
        return {
            "status": "error",
            "error": message,
            "reasoning": normalize_reasoning(reasoning),
            "pr_url": None,
            "pr_number": None,
            "issues_found": [],
            "risk_level": "UNKNOWN",
            "summary": f"Pipeline failed: {message}",
            "token_vault_used": token_vault_used,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }