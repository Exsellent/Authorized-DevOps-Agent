"""
Planner Agent — strategic planning component of the multi-agent DevOps system.

The Planner analyses GitHub issues or user goals and produces a structured
execution plan used by other agents.

Responsibilities:
    - Classify tasks by type, complexity and priority
    - Decompose tasks into executable subtasks
    - Generate predictive effort estimates
    - Produce executive summaries for UI and reporting

Design constraints:
    - Pure analysis agent (no side effects)
    - Does not access external APIs
    - Does not receive or store authentication tokens
    - Outputs structured data consumed by Orchestrator and other agents
"""

import json
import logging
import re
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.llm_client import LLMClient
from shared.mcp_base import MCPAgent
from shared.metrics import metric_counter
from shared.models import ReasoningStep
from shared.utils import (
    is_invalid_response,
    log_method,
    next_step,
    normalize_reasoning,
    safe_parse_json,
    sanitize_user_input,
)

logger = logging.getLogger("planner_agent")


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class HistoricalTask:
    """Historical task data for predictive estimation."""
    task_type: str
    estimated_hours: float
    actual_hours: float
    complexity: str
    blockers_encountered: List[str]
    team_size: int
    success: bool


@dataclass
class PredictiveEstimate:
    """Statistics-based effort estimate derived from historical tasks."""
    base_estimate_hours: float
    confidence_interval_low: float
    confidence_interval_high: float
    confidence_level: float
    similar_tasks_analyzed: int
    accuracy_factors: Dict[str, float]


@dataclass
class ExecutiveSummary:
    """Human-readable summary consumed by Digest Agent and UI."""
    overview: str
    main_risks: List[str]
    critical_path: List[str]
    recommended_focus: str
    confidence_label: str
    estimated_duration: str
    complexity_assessment: str


# ── Agent ─────────────────────────────────────────────────────────────────────

class PlannerAgent(MCPAgent):
    """
    Strategic Planning Agent.

    Classifies GitHub issues/goals, decomposes them into subtasks,
    estimates effort and produces an executive summary.
    """

    # Valid task types used in classification prompt and routing logic.
    TASK_TYPES = (
        "security_fix",       # CVE patches, auth issues, secret leaks
        "dependency_update",  # package bumps, vulnerability updates
        "api_development",
        "database_migration",
        "performance",
        "bug",
        "feature",
        "infrastructure",
        "refactor",
        "other",
    )

    def __init__(self):
        super().__init__("Planner")
        self.llm = LLMClient()
        self.historical_tasks: List[HistoricalTask] = self._load_historical_data()

        self.register_tool("plan", self.plan)
        self.register_tool("plan_with_reasoning", self.plan_with_reasoning)
        self.register_tool("predictive_estimate", self.predictive_estimate)
        self.register_tool("risk_aware_planning", self.risk_aware_planning)

        logger.info("PlannerAgent initialised")

    # ── Historical data ───────────────────────────────────────────────────────

    def _load_historical_data(self) -> List[HistoricalTask]:
        """Seed historical tasks for effort estimation (expanded coverage)."""
        return [
            HistoricalTask("security_fix", 4, 6, "medium", ["regression_risk"], 1, True),
            HistoricalTask("security_fix", 8, 10, "high", ["breaking_change"], 2, True),
            HistoricalTask("dependency_update", 2, 3, "low", [], 1, True),
            HistoricalTask("dependency_update", 6, 9, "high", ["api_breaking_change"], 2, True),
            HistoricalTask("api_development", 8, 12, "medium", ["dependency_delay"], 2, True),
            HistoricalTask("api_development", 16, 18, "high", ["scope_creep"], 3, True),
            HistoricalTask("database_migration", 12, 20, "high", ["data_quality"], 2, True),
            HistoricalTask("bug", 3, 4, "low", [], 1, True),
            HistoricalTask("bug", 8, 14, "high", ["root_cause_unclear"], 2, False),
            HistoricalTask("infrastructure", 10, 15, "high", ["api_changes"], 3, False),
            HistoricalTask("feature", 6, 8, "medium", ["requirement_changes"], 2, True),
            HistoricalTask("feature", 12, 16, "high", ["integration_delay"], 3, True),
            HistoricalTask("performance", 5, 7, "medium", ["environment_mismatch"], 2, True),
            HistoricalTask("refactor", 4, 5, "low", [], 1, True),
            HistoricalTask("refactor", 10, 12, "medium", ["regression_tests"], 2, True),
            # Add a fallback entry for 'other' to avoid zero similar tasks
            HistoricalTask("other", 4, 6, "medium", [], 1, True),
        ]

    # ── JSON parsing ──────────────────────────────────────────────────────────

    def _safe_parse_json_array(self, response: str, fallback: List) -> List:
        """
        Extract a top-level JSON array from an LLM response.
        Kept local because shared.safe_parse_json returns Dict, not List.
        Adds markdown-fence stripping and trailing-comma removal on top of
        the shared object parser.
        """
        try:
            cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", response).strip()
            first = cleaned.find("[")
            last = cleaned.rfind("]")
            if first != -1 and last != -1:
                cleaned = cleaned[first: last + 1]
                cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
                return json.loads(cleaned)
        except Exception as exc:
            logger.debug("JSON array parsing failed: %s", exc)
        return fallback

    def _is_fallback_response(self, data: Dict) -> bool:
        """
        Return True when safe_parse_json returned the fallback dict.
        Detects this by checking whether the fallback sentinel phrases appear
        in the 'reasoning' field — phrases the LLM itself would never produce
        verbatim (e.g. "LLM response could not be parsed").
        """
        sentinel = "llm response could not be parsed"
        reasoning = data.get("reasoning", "")
        return isinstance(reasoning, str) and sentinel in reasoning.lower()

    def _validate_classification(self, data: Dict) -> bool:
        """
        Ensure classification dict contains required fields.
        If not, it should be replaced with fallback.
        """
        required = {"task_type", "complexity", "technical_uncertainty",
                    "priority_hint", "suggested_assignee_team"}
        if not isinstance(data, dict):
            return False
        return all(k in data for k in required)

    # ── Reasoning trail ───────────────────────────────────────────────────────

    def _next_step(
            self,
            reasoning: List[ReasoningStep],
            description: str,
            input_data: Optional[Dict] = None,
            output_data: Optional[Dict] = None,
    ) -> None:
        next_step(reasoning, description, self.name, input_data, output_data)

    def _normalize_reasoning(self, reasoning: List[ReasoningStep]) -> List[Dict]:
        return normalize_reasoning(reasoning)

    # ── Effort estimation ─────────────────────────────────────────────────────

    def _find_similar_tasks(
            self, task_type: str, complexity: str
    ) -> List[HistoricalTask]:
        """Find tasks with exact type and complexity match."""
        return [
            t for t in self.historical_tasks
            if t.task_type == task_type and t.complexity == complexity
        ]

    def _find_similar_tasks_by_type(
            self, task_type: str
    ) -> List[HistoricalTask]:
        """Find tasks with same type (any complexity) for broader matching."""
        return [t for t in self.historical_tasks if t.task_type == task_type]

    def _get_confidence_label(self, confidence: float) -> str:
        if confidence >= 0.7:
            return "high"
        if confidence >= 0.4:
            return "medium"
        return "low"

    async def _generate_predictive_estimate(
            self, task_type: str, complexity: str, subtasks_count: int
    ) -> PredictiveEstimate:
        """Statistics-based estimate from historical seeds, with fallback to broader matches."""
        # First try exact match
        similar = self._find_similar_tasks(task_type, complexity)

        # If none, try same type (any complexity)
        if not similar:
            similar = self._find_similar_tasks_by_type(task_type)
            match_type = "broad"
        else:
            match_type = "exact"

        if not similar:
            # No historical data at all → heuristic fallback
            base = subtasks_count * {"low": 3, "medium": 7, "high": 11}.get(complexity, 7)
            return PredictiveEstimate(
                base_estimate_hours=float(base),
                confidence_interval_low=base * 0.7,
                confidence_interval_high=base * 1.5,
                confidence_level=0.3,
                similar_tasks_analyzed=0,
                accuracy_factors={"fallback": 1.0, "method": "heuristic"},
            )

        # Use actual hours from similar tasks
        actual = [t.actual_hours for t in similar]
        mean = statistics.mean(actual)
        std = statistics.stdev(actual) if len(actual) > 1 else mean * 0.3

        # Adjust for subtask count difference
        avg_subtasks = sum(t.estimated_hours / 5 for t in similar)  # rough proxy
        subtask_ratio = subtasks_count / max(avg_subtasks, 1)
        base = mean * subtask_ratio

        # Confidence factors
        variance_penalty = min(std / mean, 1.0) if mean > 0 else 0.5
        data_quality = min(len(similar) / 5.0, 1.0)
        confidence = max(0.1, data_quality * (1 - variance_penalty))

        # Adjust confidence for broad match
        if match_type == "broad":
            confidence *= 0.8

        return PredictiveEstimate(
            base_estimate_hours=base,
            confidence_interval_low=base * 0.75,
            confidence_interval_high=base * 1.4,
            confidence_level=confidence,
            similar_tasks_analyzed=len(similar),
            accuracy_factors={
                "historical_data_quality": data_quality,
                "variance_penalty": variance_penalty,
                "match_type": match_type,
            },
        )

    # ── Executive summary ─────────────────────────────────────────────────────

    def _generate_executive_summary(
            self,
            task: str,
            classification: Dict,
            subtasks: List[str],
            estimate: PredictiveEstimate,
    ) -> ExecutiveSummary:
        """
        Build a human-readable summary from structured classification data.
        No hardcoded keyword matching — driven entirely by classification fields.
        """
        task_type = classification.get("task_type", "other")
        complexity = classification.get("complexity", "medium")
        priority = classification.get("priority_hint", "P2")
        team = classification.get("suggested_assignee_team", "backend")
        uncertainty = classification.get("technical_uncertainty", "medium")

        readable_type = task_type.replace("_", " ").title()

        overview = (
            f"{readable_type} task — {complexity} complexity, "
            f"priority {priority}, assigned to {team} team."
        )

        main_risks: List[str] = []
        if complexity == "high":
            main_risks.append("High complexity increases delivery risk and review overhead")
        if uncertainty == "high":
            main_risks.append("High technical uncertainty — early spike/prototype recommended")
        if task_type in ("security_fix", "dependency_update"):
            main_risks.append("Changes may introduce regressions — regression tests required")
        if task_type == "database_migration":
            main_risks.append("Data migration is irreversible — rollback plan mandatory")
        if not main_risks:
            main_risks.append("Standard delivery risk for this complexity level")

        critical_path = subtasks[:3] if subtasks else ["Define acceptance criteria", "Implement", "Test"]
        recommended_focus = critical_path[0] if critical_path else "Define requirements"

        confidence_label = self._get_confidence_label(estimate.confidence_level)

        # FIXED MATH: consistent rounding
        days = round(estimate.base_estimate_hours / 8, 1)
        estimated_duration = f"{days} day(s) ({estimate.base_estimate_hours:.1f} h)"

        if complexity == "high" and confidence_label == "low":
            complexity_assessment = "High complexity with low confidence — expect significant variance"
        else:
            complexity_assessment = f"{complexity.capitalize()} complexity, {confidence_label} confidence estimate"

        return ExecutiveSummary(
            overview=overview,
            main_risks=main_risks,
            critical_path=critical_path,
            recommended_focus=recommended_focus,
            confidence_label=confidence_label,
            estimated_duration=estimated_duration,
            complexity_assessment=complexity_assessment,
        )

    # ── Subtask extraction ────────────────────────────────────────────────────

    def _extract_subtasks(self, decomposition: str) -> List[str]:
        """
        Extract subtasks from LLM response.

        Tries in order:
        1. JSON array  (preferred — prompt asks for JSON)
        2. Numbered list  "1. ..."
        3. Bullet list    "- ..." / "• ..."
        4. Plain lines    (last resort)
        """
        # 1. JSON array
        parsed = self._safe_parse_json_array(decomposition, [])
        if parsed and all(isinstance(s, str) for s in parsed):
            return [s.strip() for s in parsed if s.strip()][:10]

        subtasks: List[str] = []

        # 2 & 3. Numbered / bullet patterns
        for pattern in [
            r"^\s*\d+[\.)]\s+(.+?)$",
            r"^\s*[-•*]\s+(.+?)$",
        ]:
            matches = re.findall(pattern, decomposition, re.MULTILINE)
            if matches:
                for m in matches:
                    clean = m.strip().split("\n")[0]
                    clean = re.sub(r"\*\*(.*?)\*\*", r"\1", clean)
                    if ":" in clean:
                        clean = clean.split(":")[0].strip()
                    if clean:
                        subtasks.append(clean)
                return subtasks[:10]

        # 4. Plain lines
        for line in decomposition.splitlines():
            line = line.strip().lstrip("0123456789.-•* ")
            line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
            if ":" in line:
                line = line.split(":")[0].strip()
            if 20 < len(line) < 200:
                subtasks.append(line)

        return subtasks[:10]

    # ── Pattern helpers ───────────────────────────────────────────────────────

    def _is_common_pattern(self, description: str) -> bool:
        patterns = [
            "oauth", "jwt", "authentication", "token", "secret",
            "api endpoint", "rest api", "graphql",
            "database migration", "sql",
            "docker", "kubernetes", "ci cd", "github actions",
            "dependency", "cve", "vulnerability", "patch",
        ]
        lower = description.lower()
        return any(p in lower for p in patterns)

    def _is_similar_task(self, description: str, historical_type: str) -> bool:
        if historical_type in (
                "security_fix", "dependency_update", "api_development", "infrastructure"
        ):
            return self._is_common_pattern(description)
        return False

    # ── Public MCP tools ──────────────────────────────────────────────────────

    @log_method
    @metric_counter("planner")
    async def plan(
            self, description: str, context: Optional[str] = None
    ) -> Dict[str, Any]:
        """Basic planning — same as plan_with_reasoning but reasoning stripped."""
        result = await self.plan_with_reasoning(description, context)
        result.pop("reasoning", None)
        return result

    @log_method
    @metric_counter("planner")
    async def plan_with_reasoning(
            self,
            description: str,
            context: Optional[str] = None,
            issue_number: Optional[int] = None,
            title: Optional[str] = None,
            repo: Optional[str] = None,
            file_tree: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Main planning pipeline:
          1. Classify task type, complexity, priority
          2. Decompose into executable subtasks
          3. Predictive effort estimate
          4. Executive summary

        Args:
            description:   Task / issue description or goal string.
            context:       Additional free-text context.
            issue_number:  GitHub issue number (optional).
            title:         GitHub issue title (optional).
            repo:          "owner/repo" string (optional).
            file_tree:     List of file names from the repo root (optional).
        """
        reasoning: List[ReasoningStep] = []

        # Sanitize user-provided inputs before prompt interpolation
        description = sanitize_user_input(description)
        context = sanitize_user_input(context) if context else None
        title = sanitize_user_input(title) if title else None

        # ── Build reference strings for prompts ───────────────────────────────
        issue_ref = (
            f"#{issue_number}: {title}"
            if issue_number is not None and title
            else "external task"
        )
        repo_context = f"Repository: {repo}" if repo else ""
        file_tree_str = (
            "Key files: " + ", ".join(file_tree[:20])
            if file_tree
            else "File tree not provided"
        )
        task_types_str = "|".join(self.TASK_TYPES)

        # ── Step 1: Classification ────────────────────────────────────────────
        self._next_step(
            reasoning,
            "Classifying task type, complexity and priority",
            input_data={
                "description": description,
                "context": context,
                "repo": repo,
                "file_count": len(file_tree) if file_tree else 0,
            },
        )

        classification_prompt = f"""You are an autonomous issue classification agent for a GitHub DevOps pipeline.

TASK REFERENCE: {issue_ref}
{repo_context}
{file_tree_str}

DESCRIPTION:
{description}

PROJECT CONTEXT: {context or "GitHub repository — security and DevOps automation"}

Your classification will be used to:
- Route the issue to the correct engineering team
- Set GitHub labels automatically
- Feed the Risks Agent and Code Execution Agent
- Calculate sprint effort estimates

Return ONLY valid JSON (no markdown, no extra text):
{{
  "task_type": "{task_types_str}",
  "complexity": "low|medium|high",
  "technical_uncertainty": "low|medium|high",
  "priority_hint": "P0|P1|P2|P3",
  "auto_labels": ["type::security_fix", "complexity::high"],
  "suggested_assignee_team": "backend|frontend|security|devops|qa",
  "reasoning": "One sentence explaining the classification for the audit trail"
}}

Priority guide:
- P0: production outage or critical security vulnerability
- P1: high-impact bug or security issue requiring urgent attention
- P2: important feature or moderate bug
- P3: low-priority improvement or housekeeping
"""

        classification_response = await self.llm.chat(classification_prompt)
        if is_invalid_response(classification_response):
            logger.warning("Classification LLM response invalid, using fallback")
            classification_response = ""

        classification = safe_parse_json(
            classification_response,
            fallback={
                "task_type": "other",
                "complexity": "medium",
                "technical_uncertainty": "medium",
                "priority_hint": "P2",
                "auto_labels": ["type::other", "complexity::medium"],
                "suggested_assignee_team": "backend",
                "reasoning": "Fallback classification — LLM response could not be parsed",
            },
        )

        # Validate classification structure; if invalid, force fallback
        if not self._validate_classification(classification):
            logger.warning("Classification missing required fields, forcing fallback")
            classification = {
                "task_type": "other",
                "complexity": "medium",
                "technical_uncertainty": "medium",
                "priority_hint": "P2",
                "auto_labels": ["type::other", "complexity::medium"],
                "suggested_assignee_team": "backend",
                "reasoning": "Fallback classification — invalid structure",
            }
            classification_fallback = True
        else:
            classification_fallback = self._is_fallback_response(classification)

        self._next_step(
            reasoning,
            "Task classified",
            output_data={
                "task_type": classification.get("task_type"),
                "complexity": classification.get("complexity"),
                "priority_hint": classification.get("priority_hint"),
                "team": classification.get("suggested_assignee_team"),
                "fallback_used": classification_fallback,
            },
        )

        # ── Step 2: Decomposition ─────────────────────────────────────────────
        self._next_step(reasoning, "Decomposing task into executable subtasks")

        decomposition_prompt = f"""You are an autonomous task decomposition agent for a GitHub-based DevOps workflow.

TASK REFERENCE: {issue_ref}
{repo_context}
{file_tree_str}

CLASSIFICATION:
- Type: {classification.get("task_type")}
- Complexity: {classification.get("complexity")}
- Priority: {classification.get("priority_hint", "P2")}
- Team: {classification.get("suggested_assignee_team")}

TASK TO DECOMPOSE:
{description}

Generate 1-8 EXECUTABLE subtasks based STRICTLY on the issue description.
Each subtask will be:
- Created as a GitHub issue or checklist item
- Assigned directly to engineers
- Used for sprint planning and time estimation

Requirements:
- Actionable and independent (no "see above" references)
- Technically specific (not generic "implement X")
- Completable by a single engineer in 1-3 days
- Testable / verifiable

CRITICAL: If the issue description is generic, a test, or lacks technical detail, output exactly 1 simple subtask (e.g., "Acknowledge test issue") and DO NOT invent architecture, features, or deployment plans.

Return ONLY a JSON array of strings:
["Specific technical subtask 1", "Specific technical subtask 2", ...]
"""

        try:
            decomp_response = await self.llm.chat(decomposition_prompt)
            if is_invalid_response(decomp_response):
                logger.warning("Decomposition LLM response invalid, using fallback")
                raise ValueError("LLM returned invalid response")

            subtasks = self._extract_subtasks(decomp_response)
            if not subtasks:
                raise ValueError("No subtasks extracted")
            decomp_fallback = False
        except Exception as exc:
            logger.warning("Decomposition failed: %s", exc)
            subtasks = [
                f"Analyse root cause of: {description[:80]}",
                "Implement fix with comprehensive error handling",
                "Write / update automated tests",
                "Open PR with description and link to this issue",
            ]
            decomp_fallback = True

        self._next_step(
            reasoning,
            "Task decomposed into subtasks",
            output_data={
                "subtasks_count": len(subtasks),
                "subtasks_preview": subtasks[:3],
                "fallback_used": decomp_fallback,
            },
        )

        # ── Step 3: Predictive estimate ───────────────────────────────────────
        estimate = await self._generate_predictive_estimate(
            task_type=classification.get("task_type", "other"),
            complexity=classification.get("complexity", "medium"),
            subtasks_count=len(subtasks),
        )

        self._next_step(
            reasoning,
            "Predictive effort estimation completed",
            output_data={
                "base_hours": round(estimate.base_estimate_hours, 1),
                "confidence": round(estimate.confidence_level, 2),
                "confidence_label": self._get_confidence_label(estimate.confidence_level),
                "similar_tasks_analyzed": estimate.similar_tasks_analyzed,
            },
        )

        # ── Step 4: Executive summary ─────────────────────────────────────────
        exec_summary = self._generate_executive_summary(
            task=description,
            classification=classification,
            subtasks=subtasks,
            estimate=estimate,
        )

        self._next_step(
            reasoning,
            "Executive summary generated",
            output_data={"overview": exec_summary.overview},
        )

        overall_fallback = classification_fallback or decomp_fallback
        logger.info(
            "Planning completed — task_type=%s complexity=%s subtasks=%d fallback=%s",
            classification.get("task_type"),
            classification.get("complexity"),
            len(subtasks),
            overall_fallback,
        )

        return {
            "task": description,
            "repo": repo,
            "executive_summary": asdict(exec_summary),
            "classification": classification,
            "subtasks": subtasks,
            "predictive_estimate": {
                **asdict(estimate),
                "confidence_label": exec_summary.confidence_label,
            },
            "estimated_days": round(estimate.base_estimate_hours / 8, 1),
            "fallback_used": overall_fallback,
            "reasoning": self._normalize_reasoning(reasoning),
            "timestamp": datetime.now().isoformat(),
        }

    @log_method
    @metric_counter("planner")
    async def predictive_estimate(
            self,
            description: str,
            classification: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Standalone effort estimation tool (called directly when only estimate is needed).
        Returns the PredictiveEstimate as a plain dict.
        """
        description = sanitize_user_input(description)

        similar = [
            t for t in self.historical_tasks
            if t.task_type == classification.get("task_type")
               or self._is_similar_task(description, t.task_type)
        ]

        if not similar:
            base = 12.0
            low, high, confidence = base * 0.8, base * 1.4, 0.4
            accuracy_factors: Dict[str, float] = {
                "historical_data_quality": 0.0,
                "pattern_baseline": 0.4,
            }
        else:
            actual = [t.actual_hours for t in similar]
            base = statistics.mean(actual)
            std = statistics.stdev(actual) if len(actual) > 1 else base * 0.2
            low = max(4.0, base - 1.5 * std)
            high = base + 1.5 * std
            data_quality = min(1.0, len(similar) / 10.0)
            variance_penalty = min(0.6, std / base)
            confidence = max(0.3, data_quality * (1 - variance_penalty))
            accuracy_factors = {
                "historical_data_quality": data_quality,
                "variance_penalty": variance_penalty,
                "data_volume_factor": len(similar) / 10.0,
            }

        if self._is_common_pattern(description):
            confidence = min(1.0, confidence + 0.2)
            accuracy_factors["pattern_bonus"] = 0.2

        return asdict(
            PredictiveEstimate(
                base_estimate_hours=base,
                confidence_interval_low=low,
                confidence_interval_high=high,
                confidence_level=confidence,
                similar_tasks_analyzed=len(similar),
                accuracy_factors=accuracy_factors,
            )
        )

    @log_method
    @metric_counter("planner")
    async def risk_aware_planning(
            self,
            description: str,
            context: Optional[str] = None,
            repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Risk-aware planning: identify risks first, then build mitigation subtasks.
        Used by Risks Agent as a secondary input.
        """
        reasoning: List[ReasoningStep] = []

        description = sanitize_user_input(description)
        context = sanitize_user_input(context) if context else None

        self._next_step(
            reasoning,
            "Starting risk-aware planning",
            input_data={"description": description, "context": context, "repo": repo},
        )

        repo_line = f"Repository: {repo}" if repo else ""
        risk_prompt = f"""You are a risk-aware engineering lead reviewing a GitHub task before implementation.

{repo_line}
Task: {description}
Context: {context or ""}

Identify 3-5 key risks and decompose into subtasks that directly mitigate them.
Focus on: security regressions, breaking changes, missing tests, deployment issues.

Return ONLY valid JSON:
{{
  "risks": [
    {{"risk": "description", "impact": "low|medium|high", "mitigation": "concrete action"}}
  ],
  "subtasks": [
    {{"task": "specific actionable step", "mitigates": "exact risk description"}}
  ]
}}
"""

        response = await self.llm.chat(risk_prompt)
        if is_invalid_response(response):
            logger.warning("risk_aware_planning LLM response invalid, using fallback")
            response = "{}"

        plan = safe_parse_json(
            response,
            fallback={
                "risks": [
                    {
                        "risk": "Unknown external dependencies",
                        "impact": "medium",
                        "mitigation": "Prototype and validate before full implementation",
                    }
                ],
                "subtasks": [
                    {
                        "task": "Validate all requirements and dependencies",
                        "mitigates": "Unknown external dependencies",
                    }
                ],
            },
        )

        self._next_step(
            reasoning,
            "Risk-aware plan generated",
            output_data={
                "risks_count": len(plan.get("risks", [])),
                "subtasks_count": len(plan.get("subtasks", [])),
            },
        )

        return {
            "plan": plan,
            "repo": repo,
            "reasoning": self._normalize_reasoning(reasoning),
            "timestamp": datetime.now().isoformat(),
        }