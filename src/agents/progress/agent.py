"""
Progress Agent — Repository health metrics and sprint velocity tracking.

Responsibilities:
  - Analyse GitHub commit activity for velocity signals
  - Calculate issue completion rate from data passed by Orchestrator
  - Produce health status and escalation recommendations
  - Feed structured metrics to Digest Agent
"""

import json
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


class ProgressAgent(MCPAgent):
    """
    Progress and velocity tracking agent.
    """

    _THRESHOLDS = {
        "excellent": 75,
        "on_track": 50,
        "at_risk": 25,
    }

    def __init__(self):
        super().__init__("Progress")
        self.llm = LLMClient()

        self.register_tool("analyze_progress", self.analyze_progress)
        self.register_tool("track_progress", self.track_progress)
        self.register_tool("gitlab_velocity", self.track_progress)

        logger.info("ProgressAgent initialised")

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
            "on_track": f"On track — {completion_rate}% complete",
            "at_risk": f"At risk — only {completion_rate}% complete",
            "critical": f"Critical status — {completion_rate}% complete",
        }
        return labels.get(velocity_status, f"{completion_rate}% complete")

    def _baseline_analysis(self, completion_rate: float, velocity_status: str, total: int, done: int) -> str:
        msgs = {
            "excellent": f"Excellent velocity: {completion_rate}% done ({done}/{total} issues).",
            "on_track": f"Healthy velocity: {completion_rate}% done ({done}/{total} issues).",
            "at_risk": f"Velocity below target: {completion_rate}% done ({done}/{total} issues).",
            "critical": f"Critical velocity alert: {completion_rate}% done ({done}/{total} issues).",
        }
        return msgs.get(velocity_status, f"Completion: {completion_rate}%")

    def _determine_actions(self, velocity_status: str, risk_level: str) -> tuple[list, str]:
        actions: List[str] = ["update_dashboard"]
        urgency = "medium"

        if velocity_status == "critical" or risk_level == "CRITICAL":
            actions = ["alert_leadership", "schedule_standup", "notify_pm", "flag_blockers"]
            urgency = "immediate"
        elif velocity_status == "at_risk" or risk_level == "HIGH":
            actions = ["notify_pm", "suggest_scope_reduction", "flag_blockers"]
            urgency = "high"
        elif velocity_status == "excellent":
            actions = ["update_dashboard", "celebrate_team"]
            urgency = "low"

        return actions, urgency

    def _step(self, reasoning: list, description: str, input_data: Optional[Dict] = None,
              output_data: Optional[Dict] = None):
        next_step(reasoning, description, self.name, input_data, output_data)

    # ── FINAL VERSION: analyze_progress with safe JSON parsing ────────────────

    @metric_counter("progress")
    @log_method
    async def analyze_progress(
            self,
            commits: List[str],
            project_name: Optional[str] = None,
            repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        from shared.models import ReasoningStep

        reasoning: List[ReasoningStep] = []
        fallback_used = False

        safe_commits = [sanitize_user_input(c) for c in commits]
        safe_project = sanitize_user_input(project_name) if project_name else None
        safe_repo = sanitize_user_input(repo) if repo else None
        repo_display = safe_repo or safe_project or "GitHub repository"

        valid_commits = [c for c in safe_commits if c and c.strip()]

        self._step(
            reasoning,
            "Received commits for progress analysis",
            input_data={"commits_count": len(safe_commits), "repo": repo_display},
        )

        prompt = f"""You are an autonomous progress monitoring agent in a GitHub Actions workflow.

Analysing commits for: {repo_display}

COMMITS ({len(valid_commits)} valid messages provided):
{chr(10).join(f"- {c}" for c in valid_commits[:30]) if valid_commits else "NO COMMIT MESSAGES PROVIDED."}

Return STRICT JSON only (no text outside JSON):
{{
  "summary": "string",
  "velocity": "ACCELERATING|STEADY|SLOWING|BLOCKED",
  "concerns": []
}}
NO TEXT OUTSIDE JSON.
"""

        raw = await self.llm.chat(prompt)

        if is_invalid_response(raw):
            fallback_used = True
            summary = (
                f"Processed {len(valid_commits)} commit(s) for {repo_display}. "
                "Velocity signal: STEADY (LLM unavailable — baseline applied)."
            )
            velocity_signal = "STEADY"
        else:
            try:
                parsed = json.loads(raw)
                summary = parsed.get("summary", "")
                velocity_signal = parsed.get("velocity", "STEADY")
            except Exception:
                fallback_used = True
                summary = (
                    f"Processed {len(valid_commits)} commit(s) for {repo_display}. "
                    "Velocity signal: STEADY (JSON parse failed)."
                )
                velocity_signal = "STEADY"

        self._step(
            reasoning,
            "LLM commit analysis complete",
            output_data={"summary_length": len(summary), "fallback_used": fallback_used},
        )

        auto_actions = ["update_dashboard"]
        if velocity_signal in ("BLOCKED", "SLOWING"):
            auto_actions.append("notify_pm")
        if velocity_signal == "BLOCKED":
            auto_actions.append("flag_blockers")
        if velocity_signal == "ACCELERATING":
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
            "commits_count": len(valid_commits),
            "summary": summary,
            "velocity_signal": velocity_signal,
            "auto_actions": auto_actions,
            "fallback_used": fallback_used,
            "reasoning": normalize_reasoning(reasoning),
            "timestamp": datetime.now().isoformat(),
        }

    # ── track_progress (без изменений) ───────────────────────────────────────

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
            project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        from shared.models import ReasoningStep

        reasoning: List[ReasoningStep] = []

        safe_repo = sanitize_user_input(repo) if repo else None
        repo_display = safe_repo or "repository"

        self._step(
            reasoning,
            "Starting repository health assessment",
            input_data={"repo": repo_display, "pr_created": pr_created, "issues_found": issues_found_count,
                        "risk_level": risk_level},
        )

        total = max(total_issues_count, issues_found_count, 1)
        done = issues_resolved_count if issues_resolved_count else (1 if pr_created else 0)
        completion_rate = round((done / total * 100) if total > 0 else 0.0, 1)

        if pr_created and completion_rate < 10:
            completion_rate = 10.0

        velocity_status = self._classify_velocity(completion_rate)

        if risk_level == "CRITICAL" and velocity_status == "excellent":
            velocity_status = "at_risk"

        llm_analysis = self._baseline_analysis(completion_rate, velocity_status, total, done)

        auto_actions, urgency = self._determine_actions(velocity_status, risk_level)

        confidence = "high" if total_issues_count > 0 or issues_resolved_count > 0 else "medium"

        return {
            "repo": repo_display,
            "health_status": velocity_status,
            "velocity": velocity_status,
            "urgency": urgency,
            "executive_summary": {
                "status": velocity_status.upper(),
                "completion_rate": f"{completion_rate}%",
                "headline": self._generate_headline(velocity_status, completion_rate),
                "interpretation": llm_analysis,
                "auto_actions": auto_actions,
                "urgency": urgency,
                "confidence": confidence,
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
            },
            "reasoning": normalize_reasoning(reasoning),
            "metadata": {
                "agent": self.name,
                "timestamp": datetime.now().isoformat(),
            },
        }
