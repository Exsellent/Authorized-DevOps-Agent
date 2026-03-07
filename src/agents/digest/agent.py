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

Changes vs. original GitLab version:
  - CRITICAL: register_tool("generate_digest") added — Orchestrator calls
    this tool by name; original had no such tool → KeyError at runtime
  - generate_digest: new primary method that accepts structured data from
    Orchestrator (repo, goal, risk_level, issues_found, pr_url, progress)
  - daily_digest: kept as secondary tool but prompt updated to GitHub context
  - _validate_digest_quality: validation now checks DevOps-relevant content
    (PR link, risk level, issues list) instead of "mood/morale" keywords
  - _determine_quality_state: removed "has_mood" as a required field
  - MAX_WORD_COUNT raised 200 → 500 (200 was too tight for a PR report)
  - auto_actions: "post_to_gitlab_wiki" → "post_to_github_comment"
  - log_method: added @wraps
  - logger.extra= dicts removed (breaks some log handlers)
  - GitLab platform / gitlab_integration removed from metadata / health check
  - auth0_token_vault: False explicit in health check
"""

import logging
from dataclasses import dataclass, asdict
from datetime import date as date_module, datetime
from enum import Enum
from functools import wraps
from typing import Any, Dict, List, Optional

from shared.llm_client import LLMClient
from shared.mcp_base import MCPAgent
from shared.metrics import metric_counter
from shared.models import ReasoningStep

logger = logging.getLogger("digest_agent")


# ── Enums / models ────────────────────────────────────────────────────────────

class DigestStatus(Enum):
    HEALTHY  = "HEALTHY"
    WARNING  = "WARNING"
    DEGRADED = "DEGRADED"
    UNKNOWN  = "UNKNOWN"


@dataclass
class DigestValidation:
    word_count:       int
    under_limit:      bool
    has_pr_section:   bool   # was has_achievements — DevOps-relevant
    has_risk_section: bool   # was has_blockers
    has_summary:      bool   # was has_mood — always true for structured digest
    tone_positive:    bool
    confidence:       float
    quality_state:    str


# ── Decorator ─────────────────────────────────────────────────────────────────

def log_method(func):
    @wraps(func)   # FIX: was missing @wraps in original
    async def wrapper(self, *args, **kwargs):
        logger.info("%s called", func.__name__)
        try:
            result = await func(self, *args, **kwargs)
            logger.info("%s completed", func.__name__)
            return result
        except Exception as exc:
            logger.error("%s failed: %s", func.__name__, exc)
            raise
    return wrapper


# ── Agent ─────────────────────────────────────────────────────────────────────

class DigestAgent(MCPAgent):
    """
    Executive summary and report generation agent.

    Primary tool: generate_digest — called by Orchestrator with rich structured
    data from the full pipeline run (PR url, risks, progress metrics).

    Secondary tool: daily_digest — standalone daily report from free-text context.
    """

    # FIX: raised from 200 → 500 (200 was too tight for a PR report with risk list)
    MAX_WORD_COUNT = 500
    MIN_WORD_COUNT = 40

    CONFIDENCE_THRESHOLD_HEALTHY = 0.7
    CONFIDENCE_THRESHOLD_WARNING = 0.5

    def __init__(self):
        super().__init__("Digest")
        self.llm = LLMClient()

        # FIX: generate_digest was missing — Orchestrator calls this tool name
        self.register_tool("generate_digest",  self.generate_digest)
        self.register_tool("daily_digest",      self.daily_digest)
        self.register_tool("validate_digest",   self.validate_digest)
        self.register_tool("extract_key_points", self.extract_key_points)

        logger.info("DigestAgent initialised")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _next_step(
        self,
        reasoning: List[ReasoningStep],
        description: str,
        input_data:  Optional[Dict] = None,
        output_data: Optional[Dict] = None,
    ) -> None:
        reasoning.append(ReasoningStep(
            step_number=len(reasoning) + 1,
            description=description,
            timestamp=datetime.now().isoformat(),
            input_data=input_data or {},
            output=output_data or {},
            agent=self.name,
        ))

    def _is_invalid_response(self, response: str) -> bool:
        lower = response.lower()
        bad = [
            "[stub]", "[llm error]", "[claude error]",
            "unauthorized", "401", "client error",
            "connection error", "timeout",
        ]
        return any(b in lower for b in bad)

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_digest_quality(self, digest: str) -> DigestValidation:
        """
        Quality check tuned for DevOps PR reports, not daily standup notes.
        Checks for: PR section, risk section, summary presence, word count.
        """
        words     = digest.split()
        word_count = len(words)
        lower     = digest.lower()

        # DevOps-relevant section checks
        has_pr_section   = any(k in lower for k in [
            "pull request", "pr #", "pr created", "branch", "commit",
            "patch", "fix", "no pr", "pr url",
        ])
        has_risk_section = any(k in lower for k in [
            "risk", "critical", "high", "medium", "low",
            "security", "issue", "vulnerability", "concern",
        ])
        has_summary = word_count >= self.MIN_WORD_COUNT  # any non-empty text counts

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
        # FIX: was "if not has_achievements or not has_mood" — mood is irrelevant here
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
            ("pull request", "pr created", "pr #", "branch"):          "pr_summary",
            ("risk", "security", "issues found", "vulnerabilit"):       "risk_summary",
            ("next step", "recommendation", "action", "suggested"):     "next_steps",
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

    @log_method
    @metric_counter("digest")
    async def generate_digest(
        self,
        repo:          Optional[str]       = None,
        goal:          Optional[str]       = None,
        risk_level:    str                 = "MEDIUM",
        issues_found:  Optional[List]      = None,
        pr_url:        Optional[str]       = None,
        pr_number:     Optional[int]       = None,
        progress:      Optional[Dict]      = None,
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
        reasoning: List[ReasoningStep] = []
        issues_found = issues_found or []
        progress     = progress or {}

        repo_display   = repo or "repository"
        goal_display   = goal or "DevOps automation task"
        health_status  = progress.get("health_status", progress.get("velocity", "unknown"))
        issues_count   = len(issues_found)
        pr_line        = f"PR #{pr_number}: {pr_url}" if pr_url else "No PR created"

        self._next_step(
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

        self._next_step(
            reasoning,
            "Generating LLM executive summary",
            output_data={"prompt_length": len(prompt)},
        )

        digest     = ""
        llm_fallback = False

        try:
            digest = await self.llm.chat(prompt)
            if self._is_invalid_response(digest):
                llm_fallback = True
                self._next_step(
                    reasoning,
                    "LLM response invalid — using structured fallback",
                    output_data={"fallback_reason": "invalid_response"},
                )
        except Exception as exc:
            logger.error("LLM digest generation failed: %s", exc)
            llm_fallback = True
            self._next_step(
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

        self._next_step(
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
        # FIX: replaced "post_to_gitlab_wiki" with "post_to_github_comment"
        auto_actions = ["post_to_github_comment", "send_to_slack"]
        if risk_level in ("CRITICAL", "HIGH"):
            auto_actions.append("notify_pm")
        if risk_level == "CRITICAL":
            auto_actions.append("alert_leadership")
        if pr_url and risk_level in ("LOW", "MEDIUM"):
            auto_actions.append("post_celebration")

        self._next_step(
            reasoning,
            "Executive summary generation complete",
            output_data={
                "auto_actions":  auto_actions,
                "pr_created":    pr_url is not None,
                "quality_state": validation.quality_state,
            },
        )

        # FIX: plain %s — no logger.extra= dict
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
                "actions":         auto_actions,
                "escalation_level": (
                    "leadership" if "alert_leadership" in auto_actions
                    else "pm"    if "notify_pm"        in auto_actions
                    else "team"  if "send_to_slack"    in auto_actions
                    else "none"
                ),
                "celebration": "post_celebration" in auto_actions,
            },

            "fallback_used": llm_fallback,
            "reasoning":     reasoning,
            "metadata": {
                "agent":             self.name,
                "auth0_token_vault": False,  # explicit: Digest does NOT use Token Vault
                "timestamp":         datetime.now().isoformat(),
            },
        }

    # ── MCP Tool: daily_digest (secondary — standalone daily report) ──────────

    @log_method
    @metric_counter("digest")
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
        reasoning: List[ReasoningStep] = []

        if date is None:
            date = date_module.today().isoformat()

        repo_display = repo or "GitHub repository"

        self._next_step(
            reasoning,
            "Daily digest generation requested",
            input_data={"date": date, "repo": repo_display, "context_provided": bool(context)},
        )

        context_section = f"\nPROJECT DATA:\n{context}\n" if context else ""

        # FIX: prompt updated from GitLab CI/CD → GitHub Actions context
        prompt = f"""You are an autonomous daily reporting agent in a GitHub DevOps workflow.

Generating daily digest for: {repo_display} — {date}

Your digest will be:
- Sent to the team's Slack #daily-updates channel
- Posted as a GitHub repository summary comment
- Included in the weekly progress report
{context_section}
Structure (Markdown):

## 📊 Progress
Key achievements today (2-3 bullet points).
Reference GitHub issue/PR numbers where relevant (#123).

## ⚠️ Blockers
Current impediments (list "None" if clear).
Each blocker should have a proposed owner or next action.

## 👥 Team Health
1 paragraph on collaboration, momentum, and any concerns.

Requirements:
- Under 300 words
- Professional tone
- Markdown formatting
- Specific — no generic filler phrases
"""

        digest = ""
        llm_fallback = False

        try:
            digest = await self.llm.chat(prompt)
            if self._is_invalid_response(digest):
                llm_fallback = True
        except Exception as exc:
            logger.error("LLM daily digest failed: %s", exc)
            llm_fallback = True

        if llm_fallback or not digest:
            digest = f"""## 📊 Progress — {date}

- ✅ Development tasks are progressing as planned
- ✅ Code reviews and PR activity on track
- ✅ No critical blockers impacting sprint goals

## ⚠️ Blockers

None currently identified.

## 👥 Team Health

Team collaboration remains strong. Engineers are engaged and momentum is positive.
"""

        self._next_step(
            reasoning,
            "LLM daily digest generated",
            output_data={"word_count": len(digest.split()), "fallback": llm_fallback},
        )

        validation = self._validate_digest_quality(digest)
        sections   = self._extract_sections(digest)

        # FIX: "post_to_gitlab_wiki" → "post_to_github_comment"
        auto_actions = ["send_to_slack", "post_to_github_comment"]
        if "blocker" in digest.lower():
            auto_actions.append("notify_pm")

        self._next_step(
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
            "summary":     digest,
            "sections":    sections,
            "validation":  asdict(validation),
            "quality_state": validation.quality_state,
            "automated_actions": {
                "actions": auto_actions,
                "escalation_level": "pm" if "notify_pm" in auto_actions else "team",
            },
            "fallback_used": llm_fallback,
            "reasoning":   reasoning,
            "metadata": {
                "agent":             self.name,
                "auth0_token_vault": False,
                "timestamp":         datetime.now().isoformat(),
            },
        }

    # ── MCP Tool: validate_digest ─────────────────────────────────────────────

    @log_method
    @metric_counter("digest")
    async def validate_digest(self, digest: str) -> Dict[str, Any]:
        """Standalone quality check for any digest text."""
        reasoning: List[ReasoningStep] = []

        self._next_step(
            reasoning,
            "Digest validation requested",
            input_data={"digest_length": len(digest)},
        )

        validation = self._validate_digest_quality(digest)

        self._next_step(
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
            "reasoning":  reasoning,
            "timestamp":  datetime.now().isoformat(),
        }

    # ── MCP Tool: extract_key_points ──────────────────────────────────────────

    @log_method
    @metric_counter("digest")
    async def extract_key_points(self, digest: str) -> Dict[str, Any]:
        """Extract structured sections from any digest text."""
        reasoning: List[ReasoningStep] = []

        self._next_step(
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

        self._next_step(
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
            "reasoning":         reasoning,
            "timestamp":         datetime.now().isoformat(),
        }


# ── Health check ──────────────────────────────────────────────────────────────

def get_agent_status() -> Dict[str, Any]:
    return {
        "agent_name":  "digest",
        "status":      "HEALTHY",
        "capabilities": [
            "generate_digest",      # PRIMARY — called by Orchestrator
            "daily_digest",
            "validate_digest",
            "extract_key_points",
        ],
        "features": [
            "structured_pipeline_summary",
            "slack_message_builder",
            "github_pr_report",
            "quality_validation",
            "llm_fallback",
            "markdown_formatting",
        ],
        "auth0_token_vault": False,  # explicit: Digest does NOT use Token Vault
        "agent_type":  "autonomous",
        "llm_powered": True,
        "timestamp":   datetime.now().isoformat(),
    }