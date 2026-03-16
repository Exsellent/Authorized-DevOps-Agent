"""
Progress Agent — Repository health metrics and sprint velocity tracking.

Responsibilities:
  - Analyse GitHub commit activity for velocity signals
  - Calculate issue completion rate from data passed by Orchestrator
  - Produce health status and escalation recommendations
  - Feed structured metrics to Digest Agent

"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.llm_client import LLMClient
from shared.mcp_base import MCPAgent
from shared.metrics import metric_counter
from shared.utils import (
    is_invalid_response,
    log_method,
    next_step,
    normalize_reasoning,
    sanitize_user_input,
)

logger = logging.getLogger("progress_agent")


# ── Agent ─────────────────────────────────────────────────────────────────────

class ProgressAgent(MCPAgent):
    """
    Progress and velocity tracking agent.

    Consumes structured data from the Orchestrator (no direct GitHub API calls —
    the Orchestrator already holds the scoped token and passes results in).
    Does NOT import auth0_token_vault — token management is Orchestrator's concern.
    """

    # Velocity thresholds (completion % of issues in the current batch)
    _THRESHOLDS = {
        "excellent": 75,
        "on_track":  50,
        "at_risk":   25,
        # below 25 → "critical"
    }

    def __init__(self):
        super().__init__("Progress")
        self.llm = LLMClient()

        # Tools registered with MCP
        self.register_tool("analyze_progress", self.analyze_progress)
        self.register_tool("track_progress", self.track_progress)   # ← called by Orchestrator
        # Keep old name as alias so existing UI endpoints don't break
        self.register_tool("gitlab_velocity", self.track_progress)

        logger.info("ProgressAgent initialised")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _classify_velocity(self, completion_rate: float) -> str:
        if completion_rate >= self._THRESHOLDS["excellent"]:
            return "excellent"
        if completion_rate >= self._THRESHOLDS["on_track"]:
            return "on_track"
        if completion_rate >= self._THRESHOLDS["at_risk"]:
            return "at_risk"
        return "critical"

    def _generate_headline(self, velocity_status: str, completion_rate: float) -> str:
        labels = {
            "excellent": f"Excellent progress — {completion_rate}% complete",
            "on_track":  f"On track — {completion_rate}% complete",
            "at_risk":   f"At risk — only {completion_rate}% complete",
            "critical":  f"Critical status — {completion_rate}% complete",
        }
        return labels.get(velocity_status, f"{completion_rate}% complete")

    def _baseline_analysis(
        self, completion_rate: float, velocity_status: str, total: int, done: int
    ) -> str:
        """Deterministic fallback when LLM is unavailable."""
        msgs = {
            "excellent": (
                f"Excellent velocity: {completion_rate}% done ({done}/{total} issues). "
                "Team is executing well — maintain momentum."
            ),
            "on_track": (
                f"Healthy velocity: {completion_rate}% done ({done}/{total} issues). "
                "Monitor closely to maintain current pace."
            ),
            "at_risk": (
                f"Velocity below target: {completion_rate}% done ({done}/{total} issues). "
                "Recommended actions: prioritise in-progress items and address blockers."
            ),
            "critical": (
                f"Critical velocity alert: {completion_rate}% done ({done}/{total} issues). "
                "Immediate escalation required: emergency standup, scope review, resource reallocation."
            ),
        }
        return msgs.get(velocity_status, f"Completion: {completion_rate}%")

    def _determine_actions(
        self, velocity_status: str, risk_level: str
    ) -> tuple[list, str]:
        """
        Map velocity + risk level → recommended auto-actions and urgency.
        Risk level comes from Risks Agent via Orchestrator.
        """
        actions: List[str] = ["update_dashboard"]
        urgency = "medium"

        if velocity_status == "critical" or risk_level == "CRITICAL":
            actions = ["alert_leadership", "schedule_standup", "notify_pm", "flag_blockers"]
            urgency = "immediate"
        elif velocity_status == "at_risk" or risk_level == "HIGH":
            actions = ["notify_pm", "suggest_scope_reduction", "flag_blockers"]
            urgency = "high"
        elif velocity_status == "excellent" and risk_level not in ("HIGH", "CRITICAL"):
            actions = ["update_dashboard", "celebrate_team"]
            urgency = "low"

        return actions, urgency

    def _step(
            self,
            reasoning: list,
            description: str,
            input_data: Optional[Dict] = None,
            output_data: Optional[Dict] = None,
    ) -> None:
        next_step(reasoning, description, self.name, input_data, output_data)

    # ── MCP Tool: analyze_progress ────────────────────────────────────────────

    @metric_counter("progress")
    @log_method
    async def analyze_progress(
        self,
        commits: List[str],
        project_name: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Analyse a list of commit messages and return a velocity signal.

        Called by Orchestrator when it has commit data from GitHub API.

        Args:
            commits:      List of commit message strings.
            project_name: Human-readable project label (display only).
            repo:         "owner/repo" string (optional, for context in prompt).
        """
        # Local import for type hint only (aligns with planner pattern)
        from shared.models import ReasoningStep

        reasoning: List[ReasoningStep] = []
        fallback_used = False

        # Sanitize all user‑provided inputs that go into prompts
        safe_commits = [sanitize_user_input(c) for c in commits]
        safe_project = sanitize_user_input(project_name) if project_name else None
        safe_repo = sanitize_user_input(repo) if repo else None
        repo_display = safe_repo or safe_project or "GitHub repository"

        self._step(
            reasoning,
            "Received commits for progress analysis",
            input_data={"commits_count": len(safe_commits), "repo": repo_display},
        )

        prompt = f"""You are an autonomous progress monitoring agent in a GitHub Actions workflow.

Analysing commits for: {repo_display}

Your analysis will:
- Update repository health dashboards
- Trigger Slack notifications if velocity drops
- Feed into the sprint burn-down report

COMMITS ({len(safe_commits)} total):
{chr(10).join(f"- {c}" for c in safe_commits[:30])}

Provide:
1. Summary: 2-3 sentences describing what was accomplished
2. Velocity signal: ACCELERATING | STEADY | SLOWING | BLOCKED
3. Concerns: List any blockers visible in commit messages (empty list if none)

Keep response under 150 words. Be specific and data-driven.
"""

        try:
            summary = await self.llm.chat(prompt)

            if is_invalid_response(summary):
                fallback_used = True
                summary = (
                    f"Processed {len(safe_commits)} commit(s) for {repo_display}. "
                    "Velocity signal: STEADY (LLM unavailable — baseline applied)."
                )
        except Exception as exc:
            logger.error("LLM commit analysis failed: %s", exc)
            fallback_used = True
            summary = (
                f"Progress analysis failed for {repo_display}. "
                f"Processed {len(safe_commits)} commit(s). Velocity: STEADY (fallback)."
            )

        self._step(
            reasoning,
            "LLM commit analysis complete",
            output_data={"summary_length": len(summary), "fallback_used": fallback_used},
        )

        # Extract velocity signal from LLM text only if not fallback
        if fallback_used:
            velocity_signal = "STEADY"
        else:
            lower = summary.lower()
            if "blocked" in lower:
                velocity_signal = "BLOCKED"
            elif any(w in lower for w in ("accelerat", "excellent", "ahead")):
                velocity_signal = "ACCELERATING"
            elif any(w in lower for w in ("slow", "behind", "delayed")):
                velocity_signal = "SLOWING"
            else:
                velocity_signal = "STEADY"

        auto_actions = ["update_dashboard"]
        if velocity_signal == "BLOCKED":
            auto_actions.extend(["notify_pm", "flag_blockers"])
        elif velocity_signal == "SLOWING":
            auto_actions.append("notify_pm")
        elif velocity_signal == "ACCELERATING":
            auto_actions.append("celebrate_milestone")

        self._step(
            reasoning,
            "Velocity signal and actions determined",
            output_data={"velocity_signal": velocity_signal, "auto_actions": auto_actions},
        )

        logger.info(
            "analyze_progress done — repo=%s velocity=%s fallback=%s",
            repo_display, velocity_signal, fallback_used,
        )

        return {
            "repo": repo_display,
            "commits_count": len(safe_commits),
            "summary": summary,
            "velocity_signal": velocity_signal,
            "auto_actions": auto_actions,
            "fallback_used": fallback_used,
            "reasoning": normalize_reasoning(reasoning),
            "timestamp": datetime.now().isoformat(),
        }

    # ── MCP Tool: track_progress ──────────────────────────────────────────────

    @metric_counter("progress")
    @log_method
    async def track_progress(
        self,
        repo: Optional[str] = None,
        pr_created: bool = False,
        issues_found_count: int = 0,
        risk_level: str = "MEDIUM",
        issues_resolved_count: int = 0,
        total_issues_count: int = 0,
        project_id: Optional[str] = None,    # legacy alias (ignored)
    ) -> Dict[str, Any]:
        """
        Calculate repository health metrics from data supplied by the Orchestrator.

        This tool does NOT call GitHub API directly — the Orchestrator already holds
        the scoped token and passes in the relevant numbers.

        Args:
            repo:                   "owner/repo"
            pr_created:             Whether the Code Execution Agent created a PR.
            issues_found_count:     Security / bug issues identified by Risks Agent.
            risk_level:             Overall risk from Risks Agent.
            issues_resolved_count:  Issues already closed (for completion rate).
            total_issues_count:     Total open issues in the repo.
        """
        # Local import for type hint only
        from shared.models import ReasoningStep

        reasoning: List[ReasoningStep] = []

        # Sanitize repo string before prompt interpolation
        safe_repo = sanitize_user_input(repo) if repo else None
        repo_display = safe_repo or "repository"

        self._step(
            reasoning,
            "Starting repository health assessment",
            input_data={
                "repo": repo_display,
                "pr_created": pr_created,
                "issues_found": issues_found_count,
                "risk_level": risk_level,
            },
        )

        # ── Deterministic metrics ─────────────────────────────────────────────
        total = max(total_issues_count, issues_found_count, 1)
        done = issues_resolved_count if issues_resolved_count else (1 if pr_created else 0)
        completion_rate = round((done / total * 100) if total > 0 else 0.0, 1)

        # Boost effective completion if a PR was just created
        if pr_created and completion_rate < 10:
            completion_rate = 10.0   # Minimal positive signal

        velocity_status = self._classify_velocity(completion_rate)

        # Override velocity if risk is very high, even with good completion
        if risk_level == "CRITICAL" and velocity_status == "excellent":
            velocity_status = "at_risk"

        self._step(
            reasoning,
            "Deterministic metrics calculated",
            output_data={
                "total": total,
                "done": done,
                "completion_rate": completion_rate,
                "velocity_status": velocity_status,
            },
        )

        # ── LLM-enhanced interpretation ───────────────────────────────────────
        llm_prompt = f"""You are an autonomous sprint health monitoring agent in a GitHub DevOps workflow.

Repository: {repo_display}

PIPELINE RESULTS:
- PR created by AI agent: {pr_created}
- Issues / risks found: {issues_found_count}
- Overall risk level: {risk_level}
- Issue completion rate: {completion_rate}%
- Velocity status: {velocity_status.upper()}

Your interpretation will be:
- Posted to the team's Slack #sprint-health channel
- Included in the executive summary for stakeholders
- Used to decide whether to escalate to leadership

Provide:
1. A 2-3 sentence interpretation for the team
2. Whether this warrants immediate attention (yes/no and why)

Be concise (under 100 words), data-driven, and specific to the numbers above.
"""

        llm_fallback = False
        try:
            llm_analysis = await self.llm.chat(llm_prompt)
            if is_invalid_response(llm_analysis):
                llm_fallback = True
                llm_analysis = self._baseline_analysis(
                    completion_rate, velocity_status, total, done
                )
        except Exception as exc:
            logger.error("LLM velocity interpretation failed: %s", exc)
            llm_fallback = True
            llm_analysis = self._baseline_analysis(
                completion_rate, velocity_status, total, done
            )

        self._step(
            reasoning,
            "LLM interpretation complete",
            output_data={"llm_fallback": llm_fallback, "length": len(llm_analysis)},
        )

        # ── Actions and urgency ───────────────────────────────────────────────
        auto_actions, urgency = self._determine_actions(velocity_status, risk_level)

        self._step(
            reasoning,
            "Escalation actions determined",
            output_data={"auto_actions": auto_actions, "urgency": urgency},
        )

        # ── Confidence — real logic, not hardcoded "mock" ─────────────────────
        if total_issues_count > 0 or issues_resolved_count > 0:
            confidence = "high"
        elif pr_created or issues_found_count > 0:
            confidence = "medium"
        else:
            confidence = "low"

        logger.info(
            "track_progress done — repo=%s velocity=%s risk=%s urgency=%s",
            repo_display, velocity_status, risk_level, urgency,
        )

        return {
            "repo": repo_display,

            # Primary health fields consumed by Orchestrator and Digest Agent
            "health_status": velocity_status,          # key field for Orchestrator
            "velocity": velocity_status,               # alias for backward compat
            "urgency": urgency,

            "executive_summary": {
                "status": velocity_status.upper(),
                "completion_rate": f"{completion_rate}%",
                "headline": self._generate_headline(velocity_status, completion_rate),
                "interpretation": llm_analysis,
                "auto_actions": auto_actions,
                "urgency": urgency,
                "confidence": confidence,
                "llm_enhanced": not llm_fallback,
            },

            "metrics": {
                "total_issues": total,
                "done": done,
                "completion_rate": completion_rate,
                "velocity_status": velocity_status,
                "pr_created": pr_created,
                "issues_found": issues_found_count,
                "risk_level": risk_level,
            },

            "automated_actions": {
                "actions": auto_actions,
                "urgency": urgency,
                "triggers": {
                    "notify_pm":        velocity_status in ("critical", "at_risk"),
                    "alert_leadership": velocity_status == "critical" or risk_level == "CRITICAL",
                    "celebrate":        velocity_status == "excellent",
                    "slack_notify":     urgency in ("immediate", "high"),
                },
            },

            "reasoning": normalize_reasoning(reasoning),
            "metadata": {
                "agent": self.name,
                "llm_fallback": llm_fallback,
                "timestamp": datetime.now().isoformat(),
            },
        }