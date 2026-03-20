"""
Digest Agent — Executive summary and report generation.

Responsibilities:
  - Generate human-readable summary of the full agent pipeline run
  - Format structured data (PR link, risks, progress) into Markdown report
  - Build Slack notification message for Orchestrator
  - Validate report quality

Consumed by:
  - Orchestrator (generate_digest → summary, slack_message)
  - UI Observatory (reasoning trail + summary field)

"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date as date_module, datetime
from enum import Enum
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

logger = logging.getLogger("digest_agent")


# ── Enums / models ────────────────────────────────────────────────────────────

class DigestStatus(Enum):
    HEALTHY  = "HEALTHY"
    WARNING  = "WARNING"
    DEGRADED = "DEGRADED"


@dataclass
class DigestValidation:
    word_count:       int
    under_limit:      bool
    has_pr_section:   bool
    has_risk_section: bool
    has_summary:      bool
    tone_positive:    bool
    confidence:       float
    quality_state:    str


# ── Agent ─────────────────────────────────────────────────────────────────────

class DigestAgent(MCPAgent):
    """
    Executive summary and report generation agent.

    Primary tool: generate_digest — called by Orchestrator with rich structured
    data from the full pipeline run (PR url, risks, progress metrics).

    Secondary tool: daily_digest — standalone daily report from free-text context.
    """

    MAX_WORD_COUNT = 500
    MIN_WORD_COUNT = 40

    CONFIDENCE_THRESHOLD_HEALTHY = 0.7
    CONFIDENCE_THRESHOLD_WARNING = 0.5

    def __init__(self):
        super().__init__("Digest")
        self.llm = LLMClient()

        self.register_tool("generate_digest",    self.generate_digest)
        self.register_tool("daily_digest",       self.daily_digest)
        self.register_tool("validate_digest",    self.validate_digest)
        self.register_tool("extract_key_points", self.extract_key_points)

        logger.info("DigestAgent initialised")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _step(
            self,
            reasoning: list,
            description: str,
            input_data: Optional[Dict] = None,
            output_data: Optional[Dict] = None,
    ) -> None:
        next_step(reasoning, description, self.name, input_data, output_data)

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_digest_quality(self, digest: str) -> DigestValidation:
        """
        Quality check tuned for DevOps PR reports, not daily standup notes.
        Checks for: PR section, risk section, summary presence, word count.
        """
        words      = digest.split()
        word_count = len(words)
        lower      = digest.lower()

        has_pr_section = any(k in lower for k in [
            "pull request", "pr #", "pr created", "branch", "commit",
            "patch", "fix", "no pr", "pr url",
        ])
        has_risk_section = any(k in lower for k in [
            "risk", "critical", "high", "medium", "low",
            "security", "issue", "vulnerability", "concern",
        ])
        has_summary = word_count >= self.MIN_WORD_COUNT

        tone_positive = any(k in lower for k in [
            "success", "created", "fixed", "resolved", "improved",
            "complete", "good", "healthy", "on track",
        ])

        confidence = 0.5
        if self.MIN_WORD_COUNT <= word_count <= self.MAX_WORD_COUNT:
            confidence += 0.2
        if has_pr_section:
            confidence += 0.15
        if has_risk_section:
            confidence += 0.1
        if has_summary:
            confidence += 0.05

        quality_state = self._determine_quality_state(
            confidence, has_pr_section, has_risk_section
        )

        return DigestValidation(
            word_count=word_count,
            under_limit=word_count <= self.MAX_WORD_COUNT,
            has_pr_section=has_pr_section,
            has_risk_section=has_risk_section,
            has_summary=has_summary,
            tone_positive=tone_positive,
            confidence=min(confidence, 0.95),
            quality_state=quality_state,
        )

    def _determine_quality_state(
        self,
        confidence: float,
        has_pr_section: bool,
        has_risk_section: bool,
    ) -> str:
        if not has_pr_section and not has_risk_section:
            return DigestStatus.DEGRADED.value
        if confidence < self.CONFIDENCE_THRESHOLD_WARNING:
            return DigestStatus.WARNING.value
        if confidence < self.CONFIDENCE_THRESHOLD_HEALTHY:
            return DigestStatus.WARNING.value
        return DigestStatus.HEALTHY.value

    # ── Section extraction ────────────────────────────────────────────────────

    def _extract_sections(self, digest: str) -> Dict[str, str]:
        """
        Extract PR, risk, and summary sections from digest text.
        Updated to look for DevOps-relevant headings.
        """
        sections: Dict[str, str] = {
            "pr_summary":   "",
            "risk_summary": "",
            "next_steps":   "",
            "full_text":    digest,
        }

        lines = digest.split("\n")
        current: Optional[str] = None

        heading_map = {
            ("pull request", "pr created", "pr #", "branch"):      "pr_summary",
            ("risk", "security", "issues found", "vulnerabilit"):   "risk_summary",
            ("next step", "recommendation", "action", "suggested"): "next_steps",
        }

        for line in lines:
            ll = line.lower()
            for keywords, section in heading_map.items():
                if any(k in ll for k in keywords):
                    current = section
                    break

            if current and line.strip():
                sections[current] += line + "\n"

        # Fallback: if no sections detected, put everything in pr_summary
        if not any(v.strip() for k, v in sections.items() if k != "full_text"):
            sections["pr_summary"] = digest

        return sections

    # ── Slack message builder ─────────────────────────────────────────────────

    def _build_slack_message(
        self,
        repo:          str,
        goal:          str,
        risk_level:    str,
        pr_url:        Optional[str],
        issues_count:  int,
        health_status: str,
        summary_text:  str,
    ) -> str:
        """
        Compact Slack message consumed by Orchestrator's SlackNotifier.
        Kept short for Slack readability.
        """
        risk_emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}.get(
            risk_level.upper(), "⚪"
        )
        pr_line = f"🔗 <{pr_url}|View Pull Request>" if pr_url else "ℹ️ No PR created"
        health_emoji = "✅" if health_status in ("excellent", "on_track") else "⚠️"

        return (
            f"*🤖 AI DevOps Agent — Pipeline Complete*\n"
            f"*Repo:* `{repo}`\n"
            f"*Goal:* {goal}\n"
            f"{risk_emoji} *Risk:* `{risk_level}`  |  "
            f"{health_emoji} *Health:* `{health_status}`  |  "
            f"*Issues found:* {issues_count}\n"
            f"{pr_line}\n\n"
            f"{summary_text[:400]}"
        )

    # ── MCP Tool: generate_digest (PRIMARY — called by Orchestrator) ──────────

    @metric_counter("digest")
    @log_method
    async def generate_digest(
        self,
        repo:          Optional[str]  = None,
        goal:          Optional[str]  = None,
        risk_level:    str            = "MEDIUM",
        issues_found:  Optional[List] = None,
        pr_url:        Optional[str]  = None,
        pr_number:     Optional[int]  = None,
        progress:      Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Generate executive summary from the full pipeline run.

        Called by Orchestrator at the end of run_secure_devops_flow.
        All parameters come from upstream agent results.

        Args:
            repo:         "owner/repo"
            goal:         Original user goal string
            risk_level:   Overall risk from Risks Agent (CRITICAL/HIGH/MEDIUM/LOW)
            issues_found: List of issues/risks from Risks Agent
            pr_url:       GitHub PR URL (None if no PR was created)
            pr_number:    GitHub PR number
            progress:     Dict from Progress Agent (health_status, velocity, …)
        """
        from shared.models import ReasoningStep  # type hint only
        reasoning: list[ReasoningStep] = []
        issues_found = issues_found or []
        progress     = progress or {}

        # Sanitize user-provided inputs before prompt interpolation
        repo_display  = sanitize_user_input(repo)  if repo  else "repository"
        goal_display  = sanitize_user_input(goal)  if goal  else "DevOps automation task"

        health_status = progress.get("health_status", progress.get("velocity", "unknown"))
        issues_count  = len(issues_found)
        pr_line       = f"PR #{pr_number}: {pr_url}" if pr_url else "No PR created"

        self._step(
            reasoning,
            "Generating executive summary from pipeline results",
            input_data={
                "repo":          repo_display,
                "goal":          goal_display,
                "risk_level":    risk_level,
                "issues_count":  issues_count,
                "pr_created":    pr_url is not None,
                "health_status": health_status,
            },
        )

        # ── Format issues list for prompt ─────────────────────────────────────
        issues_md = "\n".join(
            f"  - [{i.get('severity', '?').upper()}] {i.get('title', str(i))}"
            if isinstance(i, dict) else f"  - {i}"
            for i in issues_found[:8]
        ) or "  - No specific issues identified"

        progress_summary = (
            f"Completion rate: {progress.get('metrics', {}).get('completion_rate', 'N/A')}%, "
            f"velocity: {health_status}"
            if progress else "Progress metrics not available"
        )

        # ── LLM prompt ────────────────────────────────────────────────────────
        prompt = f"""You are an executive report writer for an AI DevOps agent system.

The AI agent just completed an autonomous pipeline run. Write a concise executive summary.

PIPELINE RESULTS:
- Repository: {repo_display}
- Goal: {goal_display}
- Overall risk level: {risk_level}
- Pull request: {pr_line}
- Issues / risks found ({issues_count} total):
{issues_md}
- Repository health: {progress_summary}

Write the executive summary in Markdown with these sections:

## 📋 Summary
2-3 sentences: what the agent did and what the outcome was.

## 🔐 Security & Risk
Brief description of risk level and top issues found.

## 🔗 Pull Request
PR status and what was fixed (or why no PR was created).

## ✅ Next Steps
2-3 recommended actions for the engineering team.

Requirements:
- Under 400 words
- Professional tone for stakeholders
- Use Markdown formatting
- Be specific — reference the actual goal and repo
- Do NOT mention Auth0 internal implementation details
"""

        self._step(
            reasoning,
            "Generating LLM executive summary",
            output_data={"prompt_length": len(prompt)},
        )

        digest       = ""
        llm_fallback = False

        try:
            digest = await self.llm.chat(prompt)
            if is_invalid_response(digest):
                llm_fallback = True
                self._step(
                    reasoning,
                    "LLM response invalid — using structured fallback",
                    output_data={"fallback_reason": "invalid_response"},
                )
        except Exception as exc:
            logger.error("LLM digest generation failed: %s", exc)
            llm_fallback = True
            self._step(
                reasoning,
                f"LLM call failed — using structured fallback: {exc}",
                output_data={"fallback_reason": "exception"},
            )

        # ── Structured fallback — always correct for the hackathon demo ───────
        if llm_fallback or not digest:
            risk_emoji = {
                "CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"
            }.get(risk_level.upper(), "⚪")

            digest = f"""## 📋 Summary

The AI DevOps agent completed the goal **"{goal_display}"** on `{repo_display}`.
{"A pull request was created with the proposed fixes." if pr_url else "No pull request was created (no actionable patches generated)."}
Overall risk level assessed as **{risk_level}**.

## {risk_emoji} Security & Risk

- Overall risk: **{risk_level}**
- Issues identified: {issues_count}
{issues_md}

## 🔗 Pull Request

{pr_line}

## ✅ Next Steps

- Review the pull request and run the full test suite before merging
- Address any CRITICAL or HIGH severity issues before deployment
- Re-run the agent after applying fixes to verify risk reduction
"""

        # ── Validate quality ──────────────────────────────────────────────────
        validation = self._validate_digest_quality(digest)

        self._step(
            reasoning,
            "Digest quality validated",
            output_data={
                "word_count":       validation.word_count,
                "quality_state":    validation.quality_state,
                "confidence":       round(validation.confidence, 2),
                "has_pr_section":   validation.has_pr_section,
                "has_risk_section": validation.has_risk_section,
                "llm_fallback":     llm_fallback,
            },
        )

        # ── Auto actions ──────────────────────────────────────────────────────
        auto_actions = ["post_to_github_comment", "send_to_slack"]
        if risk_level in ("CRITICAL", "HIGH"):
            auto_actions.append("notify_pm")
        if risk_level == "CRITICAL":
            auto_actions.append("alert_leadership")
        if pr_url and risk_level in ("LOW", "MEDIUM"):
            auto_actions.append("post_celebration")

        self._step(
            reasoning,
            "Executive summary generation complete",
            output_data={
                "auto_actions":  auto_actions,
                "pr_created":    pr_url is not None,
                "quality_state": validation.quality_state,
            },
        )

        logger.info(
            "generate_digest done — repo=%s risk=%s pr=%s quality=%s fallback=%s",
            repo_display, risk_level, pr_number, validation.quality_state, llm_fallback,
        )

        # ── Build Slack message ───────────────────────────────────────────────
        slack_message = self._build_slack_message(
            repo=repo_display,
            goal=goal_display,
            risk_level=risk_level,
            pr_url=pr_url,
            issues_count=issues_count,
            health_status=health_status,
            summary_text=digest,
        )

        sections = self._extract_sections(digest)

        return {
            # ── Fields read by Orchestrator ───────────────────────────────────
            "summary":       digest,          # main text for UI + Orchestrator
            "slack_message": slack_message,   # compact Slack notification

            # ── Structured data ───────────────────────────────────────────────
            "sections":      sections,
            "validation":    asdict(validation),
            "quality_state": validation.quality_state,

            # ── Pipeline context (echoed for UI) ──────────────────────────────
            "repo":          repo_display,
            "goal":          goal_display,
            "pr_url":        pr_url,
            "pr_number":     pr_number,
            "risk_level":    risk_level,
            "issues_count":  issues_count,

            # ── Actions ───────────────────────────────────────────────────────
            "automated_actions": {
                "actions":          auto_actions,
                "escalation_level": (
                    "leadership" if "alert_leadership" in auto_actions
                    else "pm"    if "notify_pm"        in auto_actions
                    else "team"  if "send_to_slack"    in auto_actions
                    else "none"
                ),
                "celebration": "post_celebration" in auto_actions,
            },

            "fallback_used": llm_fallback,
            "reasoning":     normalize_reasoning(reasoning),
            "metadata": {
                "agent":             self.name,
                "auth0_token_vault": False,
                "timestamp":         datetime.now().isoformat(),
            },
        }

    # ── MCP Tool: daily_digest (secondary — standalone daily report) ──────────

    @metric_counter("digest")
    @log_method
    async def daily_digest(
        self,
        date:    Optional[str] = None,
        context: Optional[str] = None,
        repo:    Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Standalone daily project digest from free-text context.
        Not called by Orchestrator — useful for scheduled daily reports.

        Args:
            date:    Date string (defaults to today).
            context: Free-text project context / data.
            repo:    "owner/repo" (optional, for prompt context).
        """
        from shared.models import ReasoningStep  # type hint only
        reasoning: list[ReasoningStep] = []

        if date is None:
            date = date_module.today().isoformat()

        # Sanitize user-provided inputs before prompt interpolation
        safe_context = sanitize_user_input(context) if context else None
        repo_display = sanitize_user_input(repo) if repo else "GitHub repository"

        self._step(
            reasoning,
            "Daily digest generation requested",
            input_data={"date": date, "repo": repo_display, "context_provided": bool(context)},
        )

        context_section = f"\nPROJECT DATA:\n{safe_context}\n" if safe_context else ""

        # === MODIFIED: JSON prompt ===
        prompt = f"""You are a DevOps digest agent.

Generate a daily digest for {repo_display} on {date}.

{context_section}

Return STRICT JSON only:
{{
  "progress": "string",
  "risks": "string",
  "team_health": "string",
  "next_steps": "string"
}}

NO markdown. NO code blocks. NO extra text.
"""

        raw = ""
        llm_fallback = False
        data = {}

        try:
            raw = await self.llm.chat(prompt)
            if is_invalid_response(raw):
                llm_fallback = True
        except Exception as exc:
            logger.error("LLM daily digest failed: %s", exc)
            llm_fallback = True

        if not llm_fallback and raw:
            try:
                data = json.loads(raw)
                # Ensure all keys exist
                data = {
                    "progress": data.get("progress", ""),
                    "risks": data.get("risks", ""),
                    "team_health": data.get("team_health", ""),
                    "next_steps": data.get("next_steps", "")
                }
            except Exception:
                llm_fallback = True
                data = {}

        if llm_fallback or not data:
            data = {
                "progress": "No data available.",
                "risks": "No risks identified.",
                "team_health": "Unknown.",
                "next_steps": "Review pipeline."
            }

        # Build final summary Markdown from JSON fields
        summary = f"""## 📅 Daily Digest - {date}

### 📊 Progress
{data["progress"]}

### ⚠️ Blockers
{data["risks"]}

### 👥 Team Health
{data["team_health"]}

### ▶️ Next Steps
{data["next_steps"]}
"""

        # Update sections structure to match new fields
        sections = {
            "progress": data["progress"],
            "risk_summary": data["risks"],
            "team_health": data["team_health"],
            "next_steps": data["next_steps"],
            "full_text": summary,
        }

        # Override validation for daily digest: has_pr_section → has progress data, has_risk_section → has risks data
        word_count = len(summary.split())
        has_progress_data = bool(data["progress"] and data["progress"] != "No data available.")
        has_risks_data = bool(data["risks"] and data["risks"] != "No risks identified.")

        # Reuse same quality thresholds but with adapted semantics
        # We'll treat progress section as "pr_section" and risks as "risk_section" for compatibility
        validation = DigestValidation(
            word_count=word_count,
            under_limit=word_count < 200,
            has_pr_section=has_progress_data,
            has_risk_section=has_risks_data,
            has_summary=bool(summary),
            tone_positive=False,   # not calculated for daily
            confidence=0.95 if (has_progress_data or has_risks_data) else 0.7,
            quality_state=DigestStatus.HEALTHY.value if (has_progress_data and has_risks_data) else DigestStatus.WARNING.value,
        )

        self._step(
            reasoning,
            "LLM daily digest generated",
            output_data={"word_count": word_count, "fallback": llm_fallback},
        )

        # Auto‑actions
        auto_actions = ["send_to_slack", "post_to_github_comment"]
        if has_risks_data and "risk" in data["risks"].lower():
            auto_actions.append("notify_pm")

        self._step(
            reasoning,
            "Daily digest complete",
            output_data={"quality_state": validation.quality_state, "auto_actions": auto_actions},
        )

        logger.info(
            "daily_digest done — date=%s repo=%s quality=%s fallback=%s",
            date, repo_display, validation.quality_state, llm_fallback,
        )

        return {
            "date":        date,
            "repo":        repo_display,
            "summary":     summary,
            "sections":    sections,
            "validation":  asdict(validation),
            "quality_state": validation.quality_state,
            "automated_actions": {
                "actions":          auto_actions,
                "escalation_level": "pm" if "notify_pm" in auto_actions else "team",
            },
            "fallback_used": llm_fallback,
            "reasoning":     normalize_reasoning(reasoning),
            "metadata": {
                "agent":             self.name,
                "auth0_token_vault": False,
                "timestamp":         datetime.now().isoformat(),
            },
        }

    # ── MCP Tool: validate_digest ─────────────────────────────────────────────

    @metric_counter("digest")
    @log_method
    async def validate_digest(self, digest: str) -> Dict[str, Any]:
        """Standalone quality check for any digest text."""
        from shared.models import ReasoningStep  # type hint only
        reasoning: list[ReasoningStep] = []

        self._step(
            reasoning,
            "Digest validation requested",
            input_data={"digest_length": len(digest)},
        )

        validation = self._validate_digest_quality(digest)

        self._step(
            reasoning,
            "Validation completed",
            output_data={**asdict(validation), "quality_state": validation.quality_state},
        )

        return {
            "validation":    asdict(validation),
            "quality_state": validation.quality_state,
            "passed": (
                validation.under_limit
                and validation.confidence > 0.6
                and validation.has_summary
            ),
            "reasoning":  normalize_reasoning(reasoning),
            "timestamp":  datetime.now().isoformat(),
        }

    # ── MCP Tool: extract_key_points ──────────────────────────────────────────

    @metric_counter("digest")
    @log_method
    async def extract_key_points(self, digest: str) -> Dict[str, Any]:
        """Extract structured sections from any digest text."""
        from shared.models import ReasoningStep  # type hint only
        reasoning: list[ReasoningStep] = []

        self._step(
            reasoning,
            "Key points extraction requested",
            input_data={"digest_length": len(digest)},
        )

        sections   = self._extract_sections(digest)
        validation = self._validate_digest_quality(digest)

        if sections["pr_summary"].strip() and sections["risk_summary"].strip():
            method = "structured_complete"
        elif sections["pr_summary"].strip() or sections["risk_summary"].strip():
            method = "structured_partial"
        else:
            method = "fallback_full_text"

        self._step(
            reasoning,
            "Extraction complete",
            output_data={
                "method":        method,
                "quality_state": validation.quality_state,
                "sections_found": sum(
                    1 for k, v in sections.items()
                    if k != "full_text" and v.strip()
                ),
            },
        )

        return {
            "sections":          sections,
            "quality_state":     validation.quality_state,
            "extraction_method": method,
            "confidence":        validation.confidence,
            "reasoning":         normalize_reasoning(reasoning),
            "timestamp":         datetime.now().isoformat(),
        }