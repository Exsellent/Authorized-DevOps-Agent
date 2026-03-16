"""
Risks Agent — Proactive security and design risk assessment.

PURPOSE:
  Pre-implementation risk assessment for the planning phase.
  Analyses WHAT COULD GO WRONG before the Code Execution Agent writes any code.

DISTINCT FROM a vulnerability scanner:
  - Vulnerability scanner: "This CVE was found in production — fix it"
  - Risks Agent (this): "We're planning X — what are the risks before we start?"

Consumed by:
  - Orchestrator (analyze_risks → issues_found, overall_risk_level)
  - Progress Agent (risk_level for escalation logic)
  - Digest Agent (executive_summary for the final report)

"""

import base64
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime
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

logger = logging.getLogger("risks_agent")


# ── Enums ─────────────────────────────────────────────────────────────────────

class RiskAnalysisMode(Enum):
    LLM = "llm"
    BASELINE = "baseline"
    HYBRID = "hybrid"


class RiskSeverity(Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class RiskCategory(Enum):
    SECURITY = "security"
    DESIGN_RISK = "design_risk"
    BUSINESS_IMPACT = "business_impact"
    TECHNICAL_DEBT = "technical_debt"
    INTEGRATION_RISK = "integration_risk"
    SCALABILITY = "scalability"
    COMPLIANCE = "compliance"
    OPERATIONAL = "operational"


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class RiskItem:
    risk_id: str
    title: str
    category: str
    severity: str
    description: str
    likelihood: str  # "high" | "medium" | "low"
    potential_impact: str
    mitigation_strategy: str
    priority: int  # 1–5, lower = more urgent
    timeline: str
    source: str = "baseline"  # "baseline" | "llm" | "hybrid"


@dataclass
class ExecutiveRiskSummary:
    overall_risk_level: str
    total_risks: int
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int
    top_concerns: List[str]
    mitigation_priorities: List[str]
    go_no_go_recommendation: str
    confidence_level: str
    timestamp: str


# ── Agent ─────────────────────────────────────────────────────────────────────

class RisksAgent(MCPAgent):
    """
    Proactive Risk Assessment Agent.

    MCP tools:
      - analyze_risks           : full LLM + baseline planning-phase analysis
      - assess_feature_risk     : quick baseline-only triage (no LLM call)
      - validate_patch_security : post-generation gate — scans patch files
                                  for forbidden patterns BEFORE GitHub commit

    Dual-pass pipeline:
      Orchestrator calls analyze_risks  BEFORE Code Execution (planning risk)
      Orchestrator calls validate_patch AFTER  Code Execution (code risk)
    """

    _VALID_CATEGORIES = {c.value for c in RiskCategory}
    _VALID_SEVERITIES = {s.value for s in RiskSeverity}

    def __init__(self):
        super().__init__("Risks")
        self.llm = LLMClient()

        self.register_tool("analyze_risks", self.analyze_risks)
        self.register_tool("assess_feature_risk", self.assess_feature_risk)
        self.register_tool("validate_patch_security", self.validate_patch_security)

        logger.info(
            "RisksAgent initialised — dual-pass risk assessment (planning + patch)"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _step(
            self,
            reasoning: list,
            description: str,
            input_data: Optional[Dict] = None,
            output_data: Optional[Dict] = None,
    ) -> None:
        next_step(reasoning, description, self.name, input_data, output_data)

    # ─────────────────────────────────────────────────────────────────────────
    # Baseline risk rules (deterministic fallback)
    # ─────────────────────────────────────────────────────────────────────────

    def _get_baseline_risks(
            self,
            feature: str,
            issue_number: Optional[int] = None,
    ) -> List[RiskItem]:
        """
        Pattern-based risk detection — used when LLM is unavailable or fails.
        Extended with GitHub-specific patterns (Actions, workflows, PRs).
        """
        lower = feature.lower()
        risks: List[RiskItem] = []

        # ── Security / Authentication ─────────────────────────────────────────
        if any(k in lower for k in [
            "oauth", "authentication", "jwt", "token", "session",
            "secret", "credential", "api key", "password",
        ]):
            risks.append(RiskItem(
                risk_id="SEC-001",
                title="Authentication / secrets handling risk",
                category=RiskCategory.SECURITY.value,
                severity=RiskSeverity.HIGH.value,
                likelihood="high",
                description=(
                    "Features involving authentication, tokens or secrets require "
                    "careful implementation to avoid leaks or privilege escalation."
                ),
                potential_impact="Unauthorized access, credential leak, data breach",
                mitigation_strategy=(
                    "Conduct security design review; use secret scanning "
                    "(GitHub Advanced Security); never commit credentials; "
                    "prefer short-lived tokens via Auth0 Token Vault."
                ),
                priority=1,
                timeline="Before any code is written",
                source="baseline",
            ))

        # ── GitHub Actions / CI/CD ────────────────────────────────────────────
        if any(k in lower for k in [
            "github actions", "workflow", "ci/cd", "pipeline",
            "runner", "deploy", "release",
        ]):
            risks.append(RiskItem(
                risk_id="OPS-001",
                title="CI/CD pipeline security risk",
                category=RiskCategory.OPERATIONAL.value,
                severity=RiskSeverity.HIGH.value,
                likelihood="medium",
                description=(
                    "Modifying GitHub Actions workflows can introduce supply-chain "
                    "risks or expose secrets to untrusted code."
                ),
                potential_impact="Supply-chain attack, secret exfiltration, broken deployments",
                mitigation_strategy=(
                    "Pin action versions to commit SHAs; restrict GITHUB_TOKEN permissions; "
                    "use environment protection rules for production deployments."
                ),
                priority=1,
                timeline="During workflow design",
                source="baseline",
            ))

        # ── Dependency / package updates ──────────────────────────────────────
        if any(k in lower for k in [
            "dependency", "package", "npm", "pip", "requirements",
            "library", "upgrade", "update", "cve", "vulnerability",
        ]):
            risks.append(RiskItem(
                risk_id="DEP-001",
                title="Dependency update regression risk",
                category=RiskCategory.INTEGRATION_RISK.value,
                severity=RiskSeverity.MEDIUM.value,
                likelihood="medium",
                description=(
                    "Dependency updates can introduce breaking API changes "
                    "or new transitive vulnerabilities."
                ),
                potential_impact="Runtime failures, new CVEs introduced, test breakage",
                mitigation_strategy=(
                    "Run full test suite after update; check changelogs for breaking changes; "
                    "use Dependabot alerts; pin to exact versions in production."
                ),
                priority=2,
                timeline="During implementation",
                source="baseline",
            ))

        # ── External API / integration ────────────────────────────────────────
        if any(k in lower for k in [
            "api", "integration", "third-party", "external", "webhook",
            "slack", "notion", "jira", "google",
        ]):
            risks.append(RiskItem(
                risk_id="INT-001",
                title="Third-party integration risk",
                category=RiskCategory.INTEGRATION_RISK.value,
                severity=RiskSeverity.MEDIUM.value,
                likelihood="medium",
                description=(
                    "External integrations introduce dependency and availability risks."
                ),
                potential_impact="Service disruption, data exposure, API rate limiting",
                mitigation_strategy=(
                    "Implement circuit breakers, retries with backoff, and rate-limit handling; "
                    "define SLOs for external dependencies."
                ),
                priority=2,
                timeline="During implementation",
                source="baseline",
            ))

        # ── Performance / scalability ─────────────────────────────────────────
        if any(k in lower for k in [
            "cache", "database", "performance", "scale", "bulk", "batch",
        ]):
            risks.append(RiskItem(
                risk_id="SCALE-001",
                title="Performance scalability concern",
                category=RiskCategory.SCALABILITY.value,
                severity=RiskSeverity.MEDIUM.value,
                likelihood="medium",
                description="Feature may not perform adequately under production load.",
                potential_impact="Performance degradation, system outage",
                mitigation_strategy="Conduct load testing before production deployment.",
                priority=3,
                timeline="Before production release",
                source="baseline",
            ))

        # ── Generic fallback ──────────────────────────────────────────────────
        if not risks:
            risks.append(RiskItem(
                risk_id="GEN-001",
                title="General implementation risk",
                category=RiskCategory.OPERATIONAL.value,
                severity=RiskSeverity.LOW.value,
                likelihood="low",
                description="Standard implementation risks apply.",
                potential_impact="Minor delays or quality issues",
                mitigation_strategy=(
                    "Follow standard development and review practices."
                ),
                priority=4,
                timeline="During development",
                source="baseline",
            ))

        return risks

    # ─────────────────────────────────────────────────────────────────────────
    # LLM response parser
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_llm_risks(
            self,
            llm_text: str,
            feature: str,
    ) -> Optional[List[RiskItem]]:
        """
        Parse LLM JSON response into RiskItem list.

        """
        try:
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", llm_text).strip()
            first, last = cleaned.find("{"), cleaned.rfind("}")
            if first == -1 or last == -1:
                return None
            cleaned = cleaned[first: last + 1]
            cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

            data = json.loads(cleaned)
            raw_risks = data.get("risks", data.get("risk_items", []))
            if not raw_risks or not isinstance(raw_risks, list):
                return None

            items: List[RiskItem] = []
            for i, r in enumerate(raw_risks[:10], start=1):
                if not isinstance(r, dict):
                    continue

                severity = r.get("severity", "medium").lower()
                if severity not in self._VALID_SEVERITIES:
                    severity = "medium"

                category = r.get("category", "operational").lower()
                if category not in self._VALID_CATEGORIES:
                    category = "operational"

                # Safely parse priority (protect against non‑integer values)
                try:
                    priority = int(r.get("priority", 3))
                except (ValueError, TypeError):
                    priority = 3

                items.append(RiskItem(
                    risk_id=r.get("risk_id", f"LLM-{i:03d}"),
                    title=str(r.get("title", f"Risk {i}"))[:100],
                    category=category,
                    severity=severity,
                    likelihood=r.get("likelihood", "medium").lower(),
                    description=str(r.get("description", ""))[:500],
                    potential_impact=str(r.get("potential_impact", ""))[:300],
                    mitigation_strategy=str(r.get("mitigation_strategy", ""))[:400],
                    priority=priority,
                    timeline=str(r.get("timeline", "During development"))[:100],
                    source="llm",
                ))

            return items if items else None

        except Exception as exc:
            logger.debug("LLM risk JSON parse failed: %s", exc)
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Risk scoring helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_overall_risk(self, risks: List[RiskItem]) -> str:
        if not risks:
            return RiskSeverity.LOW.value
        weights = {
            RiskSeverity.CRITICAL.value: 4,
            RiskSeverity.HIGH.value: 3,
            RiskSeverity.MEDIUM.value: 2,
            RiskSeverity.LOW.value: 1,
            RiskSeverity.INFO.value: 0,
        }
        avg = sum(weights.get(r.severity, 0) for r in risks) / len(risks)
        if avg >= 3.5:
            return RiskSeverity.CRITICAL.value
        if avg >= 2.5:
            return RiskSeverity.HIGH.value
        if avg >= 1.5:
            return RiskSeverity.MEDIUM.value
        return RiskSeverity.LOW.value

    def _calculate_risk_score(self, risks: List[RiskItem]) -> int:
        """
        Numeric risk score for UI display (e.g. "Risk Score: 18").
        critical×5 + high×3 + medium×2 + low×1
        """
        weights = {
            RiskSeverity.CRITICAL.value: 5,
            RiskSeverity.HIGH.value: 3,
            RiskSeverity.MEDIUM.value: 2,
            RiskSeverity.LOW.value: 1,
        }
        return sum(weights.get(r.severity, 0) for r in risks)

    def _issues_found_list(self, risks: List[RiskItem]) -> List[Dict]:
        """
        Flat list consumed by Orchestrator: risks_result.get("issues_found", [])
        Sorted by priority ascending (most urgent first).
        Severity is returned in UPPERCASE to match orchestrator expectations.
        """
        return [
            {
                "title": r.title,
                "severity": r.severity.upper(),   # ← UPPERCASE for orchestrator
                "category": r.category,
                "mitigation_strategy": r.mitigation_strategy,
                "priority": r.priority,
            }
            for r in sorted(risks, key=lambda x: x.priority)
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # MCP Tool: analyze_risks
    # ─────────────────────────────────────────────────────────────────────────

    @metric_counter("risks")
    @log_method
    async def analyze_risks(
            self,
            feature: str,
            issue_number: Optional[int] = None,
            title: Optional[str] = None,
            context: Optional[str] = None,
            classification: Optional[Dict] = None,
            file_tree: Optional[List[str]] = None,
            repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Full proactive risk assessment pipeline (Phase 1 — planning).

        Args:
            feature:        Feature description / goal string.
            issue_number:   GitHub issue number (optional, for audit trail).
            title:          GitHub issue title (optional).
            context:        Additional free-text context.
            classification: Output from Planner Agent (task_type, complexity).
            file_tree:      Repo file names from Orchestrator (for context).
            repo:           "owner/repo" string.
        """
        from shared.models import ReasoningStep  # type hint only
        reasoning: list[ReasoningStep] = []
        analysis_mode = RiskAnalysisMode.LLM

        # Sanitize user-provided inputs
        safe_feature = sanitize_user_input(feature)
        safe_context = sanitize_user_input(context) if context else None
        safe_repo = sanitize_user_input(repo) if repo else None
        safe_title = sanitize_user_input(title) if title else None

        issue_ref = (
            f"#{issue_number}: {safe_title}"
            if issue_number is not None and safe_title
            else "external feature request"
        )

        repo_line = f"Repository: {safe_repo}" if safe_repo else ""
        tree_line = "Key files: " + ", ".join(file_tree[:20]) if file_tree else ""
        task_type = (classification or {}).get("task_type", "unknown")
        complexity = (classification or {}).get("complexity", "medium")

        # ── Step 1: Intake ────────────────────────────────────────────────────
        self._step(
            reasoning,
            "Proactive risk assessment requested",
            input_data={
                "feature": safe_feature[:100],
                "issue_ref": issue_ref,
                "repo": safe_repo,
                "task_type": task_type,
                "file_count": len(file_tree) if file_tree else 0,
            },
        )

        # ── Step 2: LLM prompt ────────────────────────────────────────────────
        prompt = f"""You are a proactive risk assessment agent in a GitHub DevOps pipeline.

TASK REFERENCE: {issue_ref}
{repo_line}
{tree_line}

PLANNER CLASSIFICATION:
- Task type: {task_type}
- Complexity: {complexity}

FEATURE / GOAL TO ASSESS:
{safe_feature}

PROJECT CONTEXT: {safe_context or "GitHub repository — security and DevOps automation"}

Identify 3–6 PLANNING-PHASE risks before any code is written.
Focus on: design decisions, security gaps, integration fragility, scalability,
compliance, and CI/CD pipeline risks.

Do NOT analyse existing CVEs or scan results — those are handled separately.

For each risk return:
- risk_id: short unique ID (e.g. "SEC-001")
- title: concise title (max 80 chars)
- category: security|design_risk|business_impact|integration_risk|scalability|technical_debt|compliance|operational
- severity: critical|high|medium|low
- likelihood: high|medium|low
- description: clear explanation (1-2 sentences)
- potential_impact: business consequence if risk materialises
- mitigation_strategy: 1-2 concrete actionable steps
- priority: integer 1–5 (1 = most urgent)
- timeline: when risk might materialise

Return ONLY valid JSON (no markdown, no extra text):
{{
  "risks": [
    {{
      "risk_id": "SEC-001",
      "title": "...",
      "category": "security",
      "severity": "high",
      "likelihood": "medium",
      "description": "...",
      "potential_impact": "...",
      "mitigation_strategy": "...",
      "priority": 1,
      "timeline": "Before development starts"
    }}
  ]
}}
"""

        self._step(
            reasoning,
            "Risk assessment prompt generated",
            output_data={"prompt_length": len(prompt), "mode": "proactive_planning"},
        )

        # ── Step 3: LLM call + parse ──────────────────────────────────────────
        detected_risks: List[RiskItem] = []

        try:
            llm_text = await self.llm.chat(prompt)

            if is_invalid_response(llm_text):
                analysis_mode = RiskAnalysisMode.BASELINE
                detected_risks = self._get_baseline_risks(safe_feature, issue_number)
                self._step(
                    reasoning,
                    "LLM unavailable — using baseline risk model",
                    output_data={"risks_detected": len(detected_risks)},
                )
            else:
                parsed = self._parse_llm_risks(llm_text, safe_feature)

                if parsed:
                    detected_risks = parsed
                    analysis_mode = RiskAnalysisMode.LLM
                    self._step(
                        reasoning,
                        "LLM risk analysis parsed successfully",
                        output_data={
                            "risks_detected": len(detected_risks),
                            "analysis_mode": analysis_mode.value,
                        },
                    )
                else:
                    detected_risks = self._get_baseline_risks(safe_feature, issue_number)
                    analysis_mode = RiskAnalysisMode.HYBRID
                    self._step(
                        reasoning,
                        "LLM response not parseable — using baseline (hybrid mode)",
                        output_data={
                            "risks_detected": len(detected_risks),
                            "analysis_mode": analysis_mode.value,
                        },
                    )

        except Exception as exc:
            logger.error("Risk analysis failed: %s", exc)
            analysis_mode = RiskAnalysisMode.BASELINE
            detected_risks = self._get_baseline_risks(safe_feature, issue_number)
            self._step(
                reasoning,
                f"LLM call failed — using baseline: {exc}",
                output_data={"analysis_mode": analysis_mode.value},
            )

        # ── Step 4: Scoring and actions ───────────────────────────────────────
        overall_risk = self._calculate_overall_risk(detected_risks)
        overall_risk = overall_risk.upper()
        risk_score = self._calculate_risk_score(detected_risks)

        # Bump overall risk if Planner flagged high complexity
        if complexity == "high" and overall_risk == "MEDIUM":
            overall_risk = "HIGH"

        if overall_risk == "CRITICAL":
            auto_actions = ["block_pr", "require_architecture_review", "notify_leadership"]
            priority = "P0"
        elif overall_risk == "HIGH":
            auto_actions = ["require_senior_review", "require_security_review", "notify_pm"]
            priority = "P1"
        elif overall_risk == "MEDIUM":
            auto_actions = ["add_extra_testing", "request_design_review"]
            priority = "P2"
        else:
            auto_actions = ["standard_development"]
            priority = "P3"

        self._step(
            reasoning,
            "Overall risk scored and actions determined",
            output_data={
                "overall_risk": overall_risk,
                "risk_score": risk_score,
                "priority": priority,
                "auto_actions": auto_actions,
            },
        )

        # ── Step 5: Executive summary ─────────────────────────────────────────
        critical_count = sum(
            1 for r in detected_risks if r.severity == RiskSeverity.CRITICAL.value
        )
        high_count = sum(
            1 for r in detected_risks if r.severity == RiskSeverity.HIGH.value
        )
        medium_count = sum(
            1 for r in detected_risks if r.severity == RiskSeverity.MEDIUM.value
        )
        low_count = sum(
            1 for r in detected_risks if r.severity == RiskSeverity.LOW.value
        )

        top_concerns = [r.title for r in detected_risks[:3]]
        mitigation_priorities = [r.mitigation_strategy for r in detected_risks[:3]]

        if overall_risk == "CRITICAL":
            go_no_go = (
                "NO-GO: Requires risk mitigation and architecture review before proceeding"
            )
        elif overall_risk == "HIGH":
            go_no_go = (
                "CONDITIONAL: Proceed with senior oversight and a documented mitigation plan"
            )
        else:
            go_no_go = (
                "GO: Acceptable risk level — proceed with standard engineering practices"
            )

        executive_summary = ExecutiveRiskSummary(
            overall_risk_level=overall_risk,
            total_risks=len(detected_risks),
            critical_count=critical_count,
            high_count=high_count,
            medium_count=medium_count,
            low_count=low_count,
            top_concerns=top_concerns,
            mitigation_priorities=mitigation_priorities,
            go_no_go_recommendation=go_no_go,
            confidence_level="medium",
            timestamp=datetime.now().isoformat(),
        )

        self._step(
            reasoning,
            "Risk assessment complete",
            output_data={
                "overall_risk": overall_risk,
                "risk_score": risk_score,
                "total_risks": len(detected_risks),
                "go_no_go": go_no_go,
                "analysis_mode": analysis_mode.value,
            },
        )

        logger.info(
            "Risk assessment done — feature=%s overall=%s priority=%s score=%d mode=%s",
            safe_feature[:50], overall_risk, priority, risk_score, analysis_mode.value,
        )

        return {
            "feature": safe_feature,
            "repo": safe_repo,
            "issue_ref": issue_ref,
            "analysis_mode": analysis_mode.value,

            # ── Fields read by Orchestrator ───────────────────────────────────
            "overall_risk_level": overall_risk,
            "priority": priority,
            "risk_score": risk_score,

            # flat list consumed by Orchestrator + Code Execution
            "issues_found": self._issues_found_list(detected_risks),

            # full risk detail
            "risks": [asdict(r) for r in detected_risks],

            # consumed by Digest Agent
            "executive_summary": asdict(executive_summary),

            # actions for Orchestrator / Progress Agent
            "automated_actions": {
                "actions": auto_actions,
                "priority": priority,
                "block_pr": overall_risk == "CRITICAL",
                "require_review": overall_risk in ("CRITICAL", "HIGH"),
            },

            "reasoning": normalize_reasoning(reasoning),
            "metadata": {
                "agent": self.name,
                "focus": "proactive_planning_risks",
                "analysis_mode": analysis_mode.value,
                "auth0_token_vault": False,
                "timestamp": datetime.now().isoformat(),
            },
        }

    # ─────────────────────────────────────────────────────────────────────────
    # MCP Tool: assess_feature_risk (lightweight baseline-only triage)
    # ─────────────────────────────────────────────────────────────────────────

    @metric_counter("risks")
    @log_method
    async def assess_feature_risk(
            self,
            feature: str,
            complexity: str = "medium",
            repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Quick baseline-only risk assessment for fast triage.
        Does not call the LLM — suitable for high-volume batch processing.
        """
        from shared.models import ReasoningStep  # type hint only
        reasoning: list[ReasoningStep] = []

        safe_feature = sanitize_user_input(feature)

        self._step(
            reasoning,
            "Quick feature risk assessment (baseline only)",
            input_data={"feature": safe_feature[:100], "complexity": complexity, "repo": repo},
        )

        risks = self._get_baseline_risks(safe_feature)
        overall_risk = self._calculate_overall_risk(risks)
        risk_score = self._calculate_risk_score(risks)

        if complexity == "high" and overall_risk == RiskSeverity.MEDIUM.value:
            overall_risk = RiskSeverity.HIGH.value
        elif complexity == "low" and overall_risk == RiskSeverity.MEDIUM.value:
            overall_risk = RiskSeverity.LOW.value

        overall_risk = overall_risk.upper()

        self._step(
            reasoning,
            "Quick assessment completed",
            output_data={
                "overall_risk": overall_risk,
                "risks_count": len(risks),
                "risk_score": risk_score,
            },
        )

        return {
            "feature": safe_feature,
            "repo": repo,
            "complexity": complexity,
            "overall_risk": overall_risk,
            "risks_count": len(risks),
            "risk_score": risk_score,
            "issues_found": self._issues_found_list(risks),
            "reasoning": normalize_reasoning(reasoning),
            "timestamp": datetime.now().isoformat(),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # MCP Tool: validate_patch_security (post-generation security gate)
    # ─────────────────────────────────────────────────────────────────────────

    @metric_counter("risks")
    @log_method
    async def validate_patch_security(
            self,
            patch_files: List[Dict],
            repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Post-generation security gate — Phase 2 of dual-pass risk analysis.

        Called by Orchestrator AFTER Code Execution, BEFORE GitHub commit.
        Scans generated patch files for forbidden patterns that should never
        appear in AI-generated code pushed to a user's repository.

        Blocking policy  (security_passed=False): any critical violation.
        Warning policy   (security_passed=True):  high/medium violations reported.
        """
        from shared.models import ReasoningStep  # type hint only
        reasoning: list[ReasoningStep] = []
        violations: List[Dict] = []

        FORBIDDEN = [
            ("eval(", "code_injection", "critical"),
            ("exec(", "code_injection", "critical"),
            ("__import__(", "dynamic_import", "high"),
            ("os.system(", "shell_injection", "critical"),
            ("subprocess.Popen(shell=True", "shell_injection", "high"),
            ("pickle.loads(", "deserialization", "high"),
            ("marshal.loads(", "deserialization", "high"),
            ("password =", "secret_exposure", "high"),
            ("secret =", "secret_exposure", "high"),
            ("api_key =", "secret_exposure", "high"),
            ("curl http", "network_egress", "medium"),
            ("wget http", "network_egress", "medium"),
        ]

        SEVERITY_SCORE = {"critical": 5, "high": 3, "medium": 1}

        self._step(
            reasoning,
            "Post-patch security gate: scanning generated files",
            input_data={"patch_files_count": len(patch_files), "repo": repo},
        )

        risk_score = 0
        for pf in patch_files:
            # The orchestrator sends content_base64 (base64-encoded file content)
            encoded = pf.get("content_base64")
            if not encoded or not isinstance(encoded, str):
                # Fallback for compatibility (if raw content ever provided)
                encoded = pf.get("content", "")
            try:
                content = base64.b64decode(encoded).decode("utf-8", errors="replace")
            except Exception:
                content = ""   # treat as empty if decoding fails

            path = pf.get("path", "unknown")
            for pattern, category, severity in FORBIDDEN:
                if pattern.lower() in content.lower():
                    violations.append({
                        "file": path,
                        "pattern": pattern,
                        "category": category,
                        "severity": severity,
                    })
                    risk_score += SEVERITY_SCORE.get(severity, 1)

        security_passed = not any(
            v["severity"] == "critical" for v in violations
        )

        critical_count = sum(1 for v in violations if v["severity"] == "critical")
        high_count = sum(1 for v in violations if v["severity"] == "high")
        medium_count = sum(1 for v in violations if v["severity"] == "medium")

        self._step(
            reasoning,
            (
                    "Patch security gate "
                    + ("✓ PASSED" if security_passed
                       else "✗ BLOCKED — critical violations found")
            ),
            output_data={
                "security_passed": security_passed,
                "violations_count": len(violations),
                "risk_score": risk_score,
                "critical": critical_count,
                "high": high_count,
                "medium": medium_count,
            },
        )

        logger.info(
            "validate_patch_security — files=%d violations=%d "
            "risk_score=%d passed=%s",
            len(patch_files), len(violations), risk_score, security_passed,
        )

        return {
            "security_passed": security_passed,
            "violations": violations,
            "violations_count": len(violations),
            "critical_count": critical_count,
            "high_count": high_count,
            "medium_count": medium_count,
            "risk_score": risk_score,
            "policy": "patch_security_policy_v1",
            "reasoning": normalize_reasoning(reasoning),
            "metadata": {
                "agent": "Risks",
                "auth0_token_vault": False,
                "phase": "post_generation",
            },
        }