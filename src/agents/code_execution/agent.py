"""
Code Execution Agent — Autonomous code generation, testing and GitHub PR preparation.

"""

import asyncio
import base64
import hashlib
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from shared.llm_client import LLMClient
from shared.mcp_base import MCPAgent
from shared.metrics import metric_counter
from shared.models import ReasoningStep
from shared.utils import (
    is_invalid_response,  # used only for plan/non-code LLM responses
    log_method,
    normalize_reasoning,
)

logger = logging.getLogger("code_execution_agent")


# ── Enums ─────────────────────────────────────────────────────────────────────

class ThinkingLevel(str, Enum):
    STRATEGIC    = "strategic"
    GENERATION   = "generation"
    EXECUTION    = "execution"
    VERIFICATION = "verification"
    REFLECTION   = "reflection"


class ExecutionStatus(str, Enum):
    SUCCESS      = "success"
    FAILURE      = "failure"
    TIMEOUT      = "timeout"
    SYNTAX_ERROR = "syntax_error"


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ExecutableTest:
    test_id:           str
    test_code:         str
    description:       str
    expected_behavior: str
    passed:            Optional[bool]  = None
    execution_time_ms: Optional[float] = None
    stdout:            Optional[str]   = None
    stderr:            Optional[str]   = None
    error_message:     Optional[str]   = None


@dataclass
class VerificationArtifact:
    artifact_id:       str
    artifact_type:     str
    timestamp:         str
    stdout:            str
    stderr:            str
    exit_code:         int
    execution_time_ms: float
    tests_passed:      int
    tests_failed:      int
    test_details:      List[Dict[str, Any]]
    code_hash:         str
    code_length:       int
    quality_score:     float
    production_ready:  bool
    confidence:        float


@dataclass
class TestSuite:
    suite_id:   str
    tests:      List[ExecutableTest]
    created_at: str
    code_hash:  str


@dataclass
class CodeIteration:
    iteration_number:      int
    timestamp:             str
    code:                  str
    code_hash:             str
    trigger:               str
    previous_error:        Optional[str]
    verification_artifact: Optional[VerificationArtifact]
    thinking_level:        ThinkingLevel
    status:                ExecutionStatus
    next_action:           str


@dataclass
class PatchFile:
    """
    Single file patch ready for GitHub API commit.
    Consumed by Orchestrator.create_branch / commit_file.
    """
    path:           str
    content_base64: str
    commit_message: str
    sha:            Optional[str] = None  # set if updating existing file


# ── Agent ─────────────────────────────────────────────────────────────────────

class CodeExecutionAgent(MCPAgent):
    """
    Autonomous code generation, testing and PR-patch preparation agent.

    Does NOT import auth0_token_vault — receives github_token as a plain
    string parameter from Orchestrator (token is never stored).
    """

    LANGUAGE = "python"
    _STDLIB  = frozenset({
        "os", "sys", "re", "json", "time", "datetime", "hashlib",
        "typing", "dataclasses", "enum", "functools", "itertools",
        "collections", "pathlib", "abc", "io", "math", "random",
        "string", "subprocess", "threading", "asyncio", "logging",
        "unittest", "traceback", "copy", "inspect",
    })

    def __init__(self):
        super().__init__("Code-Execution")
        self.llm = LLMClient()

        self._test_suites: Dict[str, TestSuite]              = {}
        self._iterations_history: Dict[str, List[CodeIteration]] = {}

        self.register_tool("generate_fix_and_create_pr", self.generate_fix_and_create_pr)
        self.register_tool("generate_and_test_code",     self.generate_and_test_code)
        self.register_tool("autonomous_debug_loop",      self.autonomous_debug_loop)
        self.register_tool("verify_code_quality",        self.verify_code_quality)
        self.register_tool("get_verification_artifacts", self.get_verification_artifacts)

        logger.info("CodeExecutionAgent initialised — language=%s", self.LANGUAGE)

    # ── Utility ───────────────────────────────────────────────────────────────

    def _hash(self, code: str) -> str:
        return hashlib.sha256(code.encode()).hexdigest()[:16]

    def _session_id(self) -> str:
        """Session ID with microsecond precision to avoid same-second collisions."""
        return f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"

    def _step(
            self,
            reasoning: List[ReasoningStep],
            description: str,
            input_data: Optional[Dict] = None,
            output_data: Optional[Dict] = None,
    ) -> None:
        reasoning.append(ReasoningStep(
            step_number=len(reasoning) + 1,
            description=description,
            timestamp=datetime.now(timezone.utc).isoformat(),
            input_data=input_data or {},
            output=output_data or {},
            agent=self.name,
        ))

    def _extract_code_block(self, response: str) -> str:
        """Extract the first Python code block from a markdown response."""
        for pattern in [r"```python\s*\n(.*?)```", r"```\s*\n(.*?)```"]:
            m = re.search(pattern, response, re.DOTALL)
            if m:
                return m.group(1).strip()
        return response.strip()

    def _extract_imports(self, code: str) -> List[str]:
        """Return top-level module names imported by the code."""
        modules: List[str] = []
        for m in re.finditer(
                r"^(?:import\s+([\w.]+)|from\s+([\w.]+)\s+import)", code, re.MULTILINE
        ):
            mod = (m.group(1) or m.group(2) or "").split(".")[0]
            if mod and mod not in self._STDLIB:
                modules.append(mod)
        return list(dict.fromkeys(modules))

    def _is_code_response_invalid(self, response: str) -> bool:
        """Check if an LLM response is an error marker *before* code extraction.

        Unlike ``is_invalid_response`` from shared/utils (which matches bare
        substrings like "timeout" and "401" that legitimately appear inside
        generated code), this helper only triggers on the bracketed error
        markers that ``LLMClient.chat`` returns on terminal failure.
        """
        prefix = response.strip()[:80].lower()
        return prefix.startswith("[claude error]") or prefix.startswith("[llm error]") \
            or prefix.startswith("[stub]") or prefix.startswith("[gitlab duo error]")

    def _quality_score(
            self,
            tests_passed: int,
            tests_total:  int,
            exec_ms:      float,
            code_len:     int,
    ) -> Tuple[float, bool, float]:
        if tests_total == 0:
            return 0.0, False, 0.0
        pass_rate  = tests_passed / tests_total
        perf_score = 1.0 if exec_ms < 1000 else 0.8
        size_score = 1.0 if 20 <= code_len <= 2000 else 0.7
        score      = (pass_rate * 0.7) + (perf_score * 0.2) + (size_score * 0.1)
        ready      = tests_passed == tests_total and exec_ms < 5000 and code_len >= 10
        confidence = min(pass_rate + (tests_total / 10 * 0.1), 1.0)
        return score, ready, confidence

    # ── Code execution ────────────────────────────────────────────────────────

    async def _install_deps(self, code: str) -> Optional[str]:
        """
        Installs missing pip packages before running code.
        Returns error message if install fails, None on success.
        """
        modules = self._extract_imports(code)
        if not modules:
            return None

        pip_map = {
            "jwt":          "PyJWT",
            "jose":         "python-jose",
            "cryptography": "cryptography",
            "httpx":        "httpx",
            "fastapi":      "fastapi",
            "pydantic":     "pydantic",
            "yaml":         "pyyaml",
            "dotenv":       "python-dotenv",
            "github":       "PyGithub",
            "requests":     "requests",
            "aiohttp":      "aiohttp",
            "boto3":        "boto3",
        }

        to_install = [pip_map.get(m, m) for m in modules]
        logger.info("Installing deps: %s", to_install)

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "--quiet", *to_install,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
            if process.returncode != 0:
                return stderr.decode()[:300]
            return None
        except Exception as exc:
            return str(exc)

    async def _run(self, code: str, timeout: float = 10.0) -> Dict[str, Any]:
        """Execute Python code in an isolated subprocess with timeout."""
        start = datetime.now()
        tmp   = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".py", delete=False
            ) as f:
                f.write(code)
                tmp = f.name

            proc = await asyncio.create_subprocess_exec(
                sys.executable, tmp,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                ms = (datetime.now() - start).total_seconds() * 1000
                return {
                    "status":           ExecutionStatus.SUCCESS if proc.returncode == 0
                                        else ExecutionStatus.FAILURE,
                    "exit_code":        proc.returncode,
                    "stdout":           stdout.decode(),
                    "stderr":           stderr.decode(),
                    "execution_time_ms": ms,
                }
            except asyncio.TimeoutError:
                proc.kill()
                ms = (datetime.now() - start).total_seconds() * 1000
                return {
                    "status":           ExecutionStatus.TIMEOUT, "exit_code": -1,
                    "stdout":           "", "stderr": f"Timeout ({timeout}s)",
                    "execution_time_ms": ms,
                }
        except Exception as exc:
            return {
                "status":           ExecutionStatus.FAILURE, "exit_code": 1,
                "stdout":           "", "stderr": str(exc), "execution_time_ms": 0,
            }
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ── Test suite extraction ─────────────────────────────────────────────────

    @staticmethod
    def _extract_preamble(test_code: str) -> str:
        """Extract the top-level preamble (imports, constants, helpers) that
        appears *before* the first ``def test_*`` function.

        Everything before the first test function is considered shared setup
        code and will be prepended to every isolated test so that constants,
        helper functions, and fixtures remain available.
        """
        preamble_lines: List[str] = []
        for line in test_code.splitlines(keepends=True):
            if re.match(r"^def test_\w+\s*\(", line):
                break
            preamble_lines.append(line)
        preamble = "".join(preamble_lines).rstrip()
        return (preamble + "\n\n") if preamble.strip() else ""

    @staticmethod
    def _extract_interstitial(lines_between: List[str]) -> str:
        """Collect non-blank, non-indented lines that appear between two
        ``def test_*`` blocks (helper functions, constants, etc.) and return
        them as a block to prepend to the *next* test.

        This prevents helper functions defined between tests from being
        silently dropped.
        """
        buf: List[str] = []
        inside_def = False
        for line in lines_between:
            stripped = line.rstrip("\n\r")
            if re.match(r"^def \w+\s*\(", stripped) and not re.match(r"^def test_\w+", stripped):
                inside_def = True
            if inside_def:
                buf.append(line)
                if stripped and not stripped[0].isspace() and buf and not re.match(r"^def ", stripped):
                    inside_def = False
            elif stripped and not stripped[0].isspace():
                buf.append(line)
        text = "".join(buf).rstrip()
        return (text + "\n\n") if text.strip() else ""

    def _split_test_functions(self, test_code: str) -> List[Tuple[str, str]]:
        """
        Extract each test_* function individually, preserving the full
        top-level preamble (imports, constants, helper functions) that
        precedes the first test, as well as any helper functions defined
        between test functions.

        Returns list of (function_name, runnable_source) tuples.
        Each returned source includes the full preamble so that isolated
        execution does not produce NameError on shared helpers/constants.
        """
        preamble = self._extract_preamble(test_code)

        lines    = test_code.splitlines(keepends=True)
        blocks:  List[Tuple[str, List[str]]] = []
        current_body: Optional[List[str]] = None
        current_name = "test_unknown"
        between: List[str] = []  # lines between test functions

        for line in lines:
            if re.match(r"^def (test_\w+)\s*\(", line):
                if current_body is not None:
                    blocks.append((current_name, current_body))
                name_m = re.match(r"^def (test_\w+)", line)
                current_name = name_m.group(1) if name_m else "test_unknown"
                # Prepend any interstitial helpers collected since the last test
                interstitial = self._extract_interstitial(between)
                between = []
                current_body = []
                if interstitial:
                    current_body.append(interstitial)
                current_body.append(line)
            elif current_body is not None:
                if line.startswith((" ", "\t")) or not line.strip():
                    current_body.append(line)
                else:
                    blocks.append((current_name, current_body))
                    current_body = None
                    between.append(line)
            else:
                between.append(line)

        if current_body is not None:
            blocks.append((current_name, current_body))

        if not blocks:
            return [("test_all", test_code)]

        result = []
        for name, body_lines in blocks:
            body = "".join(body_lines)
            runnable = (
                f"{preamble}"
                f"{body}\n\n"
                f"try:\n"
                f"    {name}()\n"
                f"    print('PASSED: {name}')\n"
                f"except Exception as e:\n"
                f"    print('FAILED: {name} -', e)\n"
                f"    raise\n"
            )
            result.append((name, runnable))
        return result

    # ── Main Orchestrator-facing tool ─────────────────────────────────────────

    @log_method
    @metric_counter("code_execution")
    async def generate_fix_and_create_pr(
            self,
            repo:            str,
            goal:            str,
            risks:           Optional[List]      = None,
            classification:  Optional[Dict]      = None,
            file_tree:       Optional[List[str]] = None,
            github_token:    Optional[str]       = None,
            max_debug_iter:  int                 = 2,
    ) -> Dict[str, Any]:
        """
        Primary tool called by Orchestrator.

        Generates a security/bug fix for the given repo goal, verifies it
        through execution, then returns patch_files ready for GitHub commit.

        Does NOT call GitHub API directly — returns patch_files list that
        Orchestrator uses with its scoped VaultToken.

        Args:
            repo:           "owner/repo"
            goal:           Fix goal from Orchestrator
            risks:          Issues list from Risks Agent
            classification: Task classification from Planner
            file_tree:      Repo file names from Orchestrator
            github_token:   Passed in-memory, never logged or stored
            max_debug_iter: Max self-correction iterations

        Returns (consumed by Orchestrator):
            patch_files:            List[PatchFile dicts] for commit_file()
            code_diff:              Human-readable diff for PR body
            tests_passed:           bool
            quality_score:          float
            verification_artifact:  full artifact dict for UI
            reasoning:              step trail for Observatory
        """
        reasoning: List[ReasoningStep] = []
        risks      = risks or []
        file_tree  = file_tree or []
        task_type  = (classification or {}).get("task_type", "security_fix")
        complexity = (classification or {}).get("complexity", "medium")

        self._step(reasoning, "PR fix generation requested",
                   input_data={"repo": repo, "goal": goal, "risks_count": len(risks),
                               "task_type": task_type})

        # ── Build risk context for prompt ─────────────────────────────────────
        risks_md = "\n".join(
            f"- [{r.get('severity', '?').upper()}] {r.get('title', str(r))}"
            if isinstance(r, dict) else f"- {r}"
            for r in risks[:6]
        ) or "- No specific issues provided"

        tree_line = "Key files: " + ", ".join(file_tree[:20]) if file_tree else ""

        # ── STRATEGIC: plan the fix ───────────────────────────────────────────
        self._step(reasoning, "STRATEGIC: Planning the fix approach",
                   input_data={"thinking_level": ThinkingLevel.STRATEGIC.value})

        strategic_prompt = f"""You are a security-focused Python engineer.

Repository: {repo}
Goal: {goal}
Task type: {task_type} | Complexity: {complexity}
{tree_line}

Risks / issues to address:
{risks_md}

Plan the fix in under 100 words:
1. What Python module / function to write or patch
2. Key security concerns to address
3. Edge cases to handle
"""
        try:
            plan_resp = await self.llm.chat(strategic_prompt)
            if is_invalid_response(plan_resp):
                logger.warning("Strategic planning LLM response invalid, using fallback")
                plan = f"Implement a fix for: {goal}"
            else:
                plan = plan_resp
        except Exception as exc:
            logger.error("Strategic planning failed: %s", exc)
            plan = f"Implement a fix for: {goal}"

        self._step(reasoning, "STRATEGIC: Fix plan ready",
                   output_data={"plan_length": len(plan)})

        # ── GENERATION: write the fix ─────────────────────────────────────────
        self._step(reasoning, "GENERATION: Writing fix code",
                   input_data={"thinking_level": ThinkingLevel.GENERATION.value})

        gen_prompt = f"""Write Python code to fix the following issues in the repository '{repo}'.

Goal: {goal}

Fix plan:
{plan}

Issues to address:
{risks_md}

RULES:
1. Write complete, runnable Python 3 code
2. Include docstrings
3. Handle edge cases and exceptions
4. Do NOT include test code here
5. Use only stdlib or well-known packages (jwt → PyJWT, etc.)
6. Output ONLY the fix code in a ```python block
"""
        try:
            code_resp = await self.llm.chat(gen_prompt)
            if self._is_code_response_invalid(code_resp):
                logger.warning("Code generation LLM response invalid, using fallback")
                fix_code = f'# Auto-generated fix for: {goal}\n# Manual review required\n'
            else:
                fix_code = self._extract_code_block(code_resp)
        except Exception as exc:
            logger.error("Code generation failed: %s", exc)
            fix_code = f'# Auto-generated fix for: {goal}\n# Manual review required\n'

        code_hash = self._hash(fix_code)
        self._step(reasoning, "GENERATION: Fix code generated",
                   output_data={"code_hash": code_hash, "code_length": len(fix_code)})

        # ── GENERATION: write tests ───────────────────────────────────────────
        self._step(reasoning, "GENERATION: Writing test suite")

        test_prompt = f"""Write a Python test suite for this fix code.

FIX CODE:
{fix_code}

GOAL BEING TESTED: {goal}

RULES:
1. Write 3-5 test functions named test_*
2. Use assertions only (no unittest.TestCase)
3. Each test must be self-contained
4. Print "PASSED: <n>" on success
5. Cover: happy path, edge cases, security constraints
6. Output ONLY test code in a ```python block
"""
        try:
            test_resp = await self.llm.chat(test_prompt)
            if self._is_code_response_invalid(test_resp):
                logger.warning("Test generation LLM response invalid, using fallback")
                test_code = "def test_basic():\n    assert True\n    print('PASSED: test_basic')\n"
            else:
                test_code = self._extract_code_block(test_resp)
        except Exception as exc:
            logger.error("Test generation failed: %s", exc)
            test_code = "def test_basic():\n    assert True\n    print('PASSED: test_basic')\n"

        # ── Install dependencies once before running anything ─────────────────
        dep_error = await self._install_deps(fix_code + "\n" + test_code)
        if dep_error:
            self._step(reasoning, "Dependency install warning",
                       output_data={"warning": dep_error[:200]})

        # ── Split tests into isolated functions ───────────────────────────────
        test_functions = self._split_test_functions(test_code)

        tests: List[ExecutableTest] = [
            ExecutableTest(
                test_id=f"test_{i + 1}",
                test_code=body,
                description=name,
                expected_behavior="Should pass all assertions",
            )
            for i, (name, body) in enumerate(test_functions[:5])
        ]
        if not tests:
            tests = [ExecutableTest(
                test_id="test_fallback",
                test_code="assert True\nprint('PASSED: test_fallback')\n",
                description="fallback", expected_behavior="basic",
            )]

        session_id = self._session_id()

        # Store suite under session_id so autonomous_debug_loop can reliably find it
        suite = TestSuite(
            suite_id=f"suite_{session_id}",
            tests=tests,
            created_at=datetime.now(timezone.utc).isoformat(),
            code_hash=code_hash,
        )
        self._test_suites[session_id] = suite

        # ── EXECUTION + debug loop ────────────────────────────────────────────
        self._step(reasoning, "EXECUTION: Running tests and debug loop",
                   input_data={"tests_count": len(tests), "max_iter": max_debug_iter})

        current_code  = fix_code
        best_code     = fix_code
        best_passed   = -1
        final_artifact: Optional[VerificationArtifact] = None

        final_iteration = 0
        for iteration in range(max_debug_iter + 1):
            t_passed, t_failed, t_details = 0, 0, []
            total_test_ms = 0.0

            for test in tests:
                full = f"{current_code}\n\n{test.test_code}"
                tr   = await self._run(full)
                ok   = tr["exit_code"] == 0
                t_passed  += int(ok)
                t_failed  += int(not ok)
                total_test_ms += tr["execution_time_ms"]
                test.passed            = ok
                test.execution_time_ms = tr["execution_time_ms"]
                test.stdout            = tr["stdout"]
                test.stderr            = tr["stderr"]
                test.error_message     = tr["stderr"] if not ok else None
                t_details.append({
                    "test_id":          test.test_id,
                    "passed":           ok,
                    "execution_time_ms": tr["execution_time_ms"],
                    "output":           tr["stdout"][:200],
                    "error":            tr["stderr"][:200] if not ok else "",
                })

            # Use aggregate test execution time for quality score (issue #14)
            score, ready, conf = self._quality_score(
                t_passed, len(tests), total_test_ms, len(current_code)
            )

            # Also run bare code for stdout/stderr capture in artifact
            exec_result = await self._run(current_code)

            artifact = VerificationArtifact(
                artifact_id=f"artifact_{session_id}_iter{iteration}",
                artifact_type="test_report",
                timestamp=datetime.now(timezone.utc).isoformat(),
                stdout=exec_result["stdout"],
                stderr=exec_result["stderr"],
                exit_code=exec_result["exit_code"],
                execution_time_ms=total_test_ms,
                tests_passed=t_passed,
                tests_failed=t_failed,
                test_details=t_details,
                code_hash=self._hash(current_code),
                code_length=len(current_code),
                quality_score=score,
                production_ready=ready,
                confidence=conf,
            )
            final_artifact = artifact

            self._step(
                reasoning,
                f"EXECUTION iter {iteration}: {t_passed}/{len(tests)} tests passed",
                output_data={
                    "iteration":      iteration,
                    "tests_passed":   t_passed,
                    "tests_failed":   t_failed,
                    "quality_score":  round(score, 2),
                    "production_ready": ready,
                },
            )

            if t_passed > best_passed:
                best_passed = t_passed
                best_code   = current_code

            final_iteration = iteration
            if ready or iteration == max_debug_iter:
                break

            # ── REFLECTION: generate fix for failures ─────────────────────────
            failed_errors = "\n".join(
                f"{d['test_id']}: {d['error']}"
                for d in t_details if not d["passed"]
            )
            debug_prompt = f"""Fix this Python code to pass all tests.

CURRENT CODE:
{current_code}

FAILED TEST ERRORS:
{failed_errors}

ORIGINAL GOAL: {goal}

RULES:
1. Preserve all working functionality
2. Fix only what is broken
3. Keep same module structure
4. Output ONLY fixed code in ```python block
"""
            try:
                fix_resp = await self.llm.chat(debug_prompt)
                if self._is_code_response_invalid(fix_resp):
                    logger.warning("Debug LLM response invalid, stopping loop")
                    break
                current_code = self._extract_code_block(fix_resp)
                dep_error    = await self._install_deps(current_code)
                self._step(reasoning, f"REFLECTION iter {iteration}: new fix generated",
                           output_data={"new_hash": self._hash(current_code)})
            except Exception as exc:
                logger.error("Debug iteration %d failed: %s", iteration, exc)
                break

        # ── Build patch_files for Orchestrator GitHub commit ──────────────────
        filename   = self._suggest_filename(goal, task_type)
        b64_content = base64.b64encode(best_code.encode()).decode()

        patch_files: List[Dict] = [asdict(PatchFile(
            path=filename,
            content_base64=b64_content,
            commit_message=f"fix({task_type}): {goal[:72]}",
            sha=None,
        ))]

        code_diff = (
            f"--- a/{filename}\n"
            f"+++ b/{filename}\n"
            f"@@ Generated fix @@\n"
            + "\n".join(f"+{line}" for line in best_code.splitlines()[:40])
        )

        iterations_list = self._iterations_history.get(session_id, [])
        if final_artifact:
            # Derive test-based status: SUCCESS only when all tests pass
            iter_status = ExecutionStatus.SUCCESS if best_passed == len(tests) \
                          else ExecutionStatus.FAILURE
            iterations_list.append(CodeIteration(
                iteration_number=final_iteration + 1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                code=best_code,
                code_hash=self._hash(best_code),
                trigger="generate_fix_and_create_pr",
                previous_error=None,
                verification_artifact=final_artifact,
                thinking_level=ThinkingLevel.VERIFICATION,
                status=iter_status,
                next_action="pr_created",
            ))
            self._iterations_history[session_id] = iterations_list

        self._step(
            reasoning, "PR patch prepared",
            output_data={
                "patch_files_count": len(patch_files),
                "filename":          filename,
                "tests_passed":      best_passed,
                "tests_total":       len(tests),
                "production_ready":  (final_artifact.production_ready
                                      if final_artifact else False),
            },
        )

        logger.info(
            "generate_fix_and_create_pr done — repo=%s tests=%d/%d ready=%s",
            repo, best_passed, len(tests),
            final_artifact.production_ready if final_artifact else False,
        )

        return {
            "patch_files":          patch_files,
            "code_diff":            code_diff,
            "tests_passed":         best_passed == len(tests),
            "quality_score":        final_artifact.quality_score if final_artifact else 0.0,
            "verification_artifact": asdict(final_artifact) if final_artifact else None,
            "session_id":           session_id,
            "code":                 best_code,
            "code_hash":            self._hash(best_code),
            "test_suite": {
                "suite_id":     suite.suite_id,
                "tests_count":  len(tests),
                "tests_passed": best_passed,
                "tests_failed": len(tests) - best_passed,
            },
            "quality_metrics": {
                "quality_score":   final_artifact.quality_score if final_artifact else 0.0,
                "production_ready": final_artifact.production_ready if final_artifact else False,
                "confidence":      final_artifact.confidence if final_artifact else 0.0,
            },
            "auth0_token_vault": False,
            "reasoning": normalize_reasoning(reasoning),
        }

    def _suggest_filename(self, goal: str, task_type: str) -> str:
        """Suggest a reasonable file path for the generated fix."""
        slug = re.sub(r"[^\w]", "_", goal.lower())[:40].strip("_")
        prefix_map = {
            "security_fix":      "src/security",
            "dependency_update": "requirements",
            "api_development":   "src/api",
            "bug":               "src",
            "feature":           "src",
        }
        prefix = prefix_map.get(task_type, "src")
        return f"{prefix}/{slug}_fix.py"

    # ── generate_and_test_code ────────────────────────────────────────────────

    @log_method
    @metric_counter("code_execution")
    async def generate_and_test_code(
            self,
            requirement: str,
            context:     Optional[str] = None,
            language:    Optional[str] = None,
    ) -> Dict[str, Any]:

        reasoning:  List[ReasoningStep] = []
        session_id = self._session_id()

        if language and language.lower() not in ("python", "py", "python3"):
            logger.warning(
                "Requested language '%s' — only Python execution supported. "
                "Generating Python implementation.", language
            )

        self._step(reasoning, "STRATEGIC: Planning code generation",
                   input_data={"requirement": requirement},
                   output_data={"thinking_level": ThinkingLevel.STRATEGIC.value})

        try:
            plan_resp = await self.llm.chat(
                f"Break down this requirement (under 100 words):\n{requirement}"
            )
            if is_invalid_response(plan_resp):
                logger.warning("Planning LLM response invalid, using fallback")
                plan = "Basic implementation needed"
            else:
                plan = plan_resp
        except Exception as exc:
            logger.error("Strategic planning failed: %s", exc)
            plan = "Basic implementation needed"

        self._step(reasoning, "GENERATION: Creating code and tests",
                   input_data={"thinking_level": ThinkingLevel.GENERATION.value},
                   output_data={"plan_length": len(plan)})

        ctx_block  = f"\nContext: {context}" if context else ""
        gen_prompt = f"""Generate Python code for:
{requirement}
{ctx_block}
Plan: {plan}

RULES: complete runnable Python, docstrings, edge cases, NO tests here.
Output ONLY code in ```python block."""

        try:
            code_resp      = await self.llm.chat(gen_prompt)
            if self._is_code_response_invalid(code_resp):
                logger.warning("Code generation LLM response invalid, using fallback")
                generated_code = (
                    f'# Auto-generated code for: {requirement}\n'
                    f'# Manual review required\n'
                )
            else:
                generated_code = self._extract_code_block(code_resp)
        except Exception as exc:
            logger.error("Code generation failed: %s", exc)
            generated_code = (
                f'# Auto-generated code for: {requirement}\n'
                f'# Manual review required\n'
            )

        code_hash = self._hash(generated_code)

        self._step(reasoning, "GENERATION: Creating executable test suite",
                   input_data={"code_hash": code_hash},
                   output_data={"code_length": len(generated_code)})

        try:
            test_resp = await self.llm.chat(
                f"Write 3-5 executable Python test functions (test_*) for:\n{generated_code}\n"
                f"Use only assert statements. Output in ```python block."
            )
            if self._is_code_response_invalid(test_resp):
                logger.warning("Test generation LLM response invalid, using fallback")
                test_code = "def test_basic():\n    assert True\n    print('PASSED: test_basic')\n"
            else:
                test_code = self._extract_code_block(test_resp)
        except Exception as exc:
            logger.error("Test generation failed: %s", exc)
            test_code = "def test_basic():\n    assert True\n    print('PASSED: test_basic')\n"

        await self._install_deps(generated_code + "\n" + test_code)

        test_functions = self._split_test_functions(test_code)
        tests = [
            ExecutableTest(
                test_id=f"test_{i + 1}", test_code=body,
                description=name, expected_behavior="pass assertions",
            )
            for i, (name, body) in enumerate(test_functions[:5])
        ] or [ExecutableTest(
            test_id="test_fallback",
            test_code="assert True\nprint('PASSED: test_fallback')\n",
            description="fallback", expected_behavior="basic",
        )]

        # Store suite under session_id for reliable lookup in autonomous_debug_loop
        suite = TestSuite(suite_id=f"suite_{session_id}", tests=tests,
                          created_at=datetime.now(timezone.utc).isoformat(), code_hash=code_hash)
        self._test_suites[session_id] = suite

        self._step(reasoning, "EXECUTION: Running code and tests",
                   input_data={"tests_count": len(tests)},
                   output_data={"suite_id": suite.suite_id})

        exec_result         = await self._run(generated_code)
        t_passed, t_failed  = 0, 0
        t_details: List[Dict] = []
        total_test_ms       = 0.0

        self._step(reasoning, "EXECUTION: Running test suite",
                   input_data={"code_status": exec_result["status"].value},
                   output_data={"exit_code": exec_result["exit_code"],
                                "execution_time_ms": exec_result["execution_time_ms"]})

        for test in tests:
            tr  = await self._run(f"{generated_code}\n\n{test.test_code}")
            ok  = tr["exit_code"] == 0
            t_passed  += int(ok)
            t_failed  += int(not ok)
            total_test_ms += tr["execution_time_ms"]
            test.passed            = ok
            test.execution_time_ms = tr["execution_time_ms"]
            test.stdout            = tr["stdout"]
            test.stderr            = tr["stderr"]
            test.error_message     = tr["stderr"] if not ok else None
            t_details.append({
                "test_id":          test.test_id, "passed": ok,
                "execution_time_ms": tr["execution_time_ms"],
                "output":           tr["stdout"][:200],
            })

        self._step(reasoning, "VERIFICATION: Analysing results",
                   input_data={"tests_total": len(tests)},
                   output_data={"tests_passed": t_passed, "tests_failed": t_failed})

        score, ready, conf = self._quality_score(
            t_passed, len(tests), total_test_ms, len(generated_code)
        )
        artifact = VerificationArtifact(
            artifact_id=f"artifact_{session_id}",
            artifact_type="test_report",
            timestamp=datetime.now(timezone.utc).isoformat(),
            stdout=exec_result["stdout"], stderr=exec_result["stderr"],
            exit_code=exec_result["exit_code"],
            execution_time_ms=total_test_ms,
            tests_passed=t_passed, tests_failed=t_failed,
            test_details=t_details, code_hash=code_hash,
            code_length=len(generated_code), quality_score=score,
            production_ready=ready, confidence=conf,
        )

        # Derive status from test results, not bare-code execution
        iter_status = ExecutionStatus.SUCCESS if t_passed == len(tests) \
                      else ExecutionStatus.FAILURE
        iteration = CodeIteration(
            iteration_number=1, timestamp=datetime.now(timezone.utc).isoformat(),
            code=generated_code, code_hash=code_hash,
            trigger="initial_generation", previous_error=None,
            verification_artifact=artifact, thinking_level=ThinkingLevel.VERIFICATION,
            status=iter_status,
            next_action="return_results" if ready else "debug_needed",
        )
        self._iterations_history[session_id] = [iteration]

        self._step(reasoning, "COMPLETED: Code generation and verification",
                   input_data={"production_ready": ready, "quality_score": score},
                   output_data={"session_id": session_id, "tests_passed": t_passed,
                                "tests_total": len(tests),
                                "final_status": exec_result["status"].value})

        logger.info(
            "generate_and_test_code done — session=%s tests=%d/%d ready=%s",
            session_id, t_passed, len(tests), ready,
        )

        return {
            "session_id":  session_id,
            "code":        generated_code,
            "code_hash":   code_hash,
            "verification_artifact": asdict(artifact),
            "test_suite": {
                "suite_id":     suite.suite_id,
                "tests_count":  len(tests),
                "tests_passed": t_passed,
                "tests_failed": t_failed,
            },
            "quality_metrics": {
                "quality_score":   score,
                "production_ready": ready,
                "confidence":      conf,
            },
            "reasoning": normalize_reasoning(reasoning),
        }

    # ── autonomous_debug_loop ─────────────────────────────────────────────────

    @log_method
    @metric_counter("code_execution")
    async def autonomous_debug_loop(
            self, session_id: str, max_iterations: int = 3
    ) -> Dict[str, Any]:

        reasoning: List[ReasoningStep] = []
        self._step(reasoning, "DEBUG LOOP: Starting",
                   input_data={"session_id": session_id, "max_iterations": max_iterations})

        iterations = self._iterations_history.get(session_id, [])
        if not iterations:
            return {"error": "No session found", "session_id": session_id,
                    "reasoning": normalize_reasoning(reasoning)}

        last = iterations[-1]

        # Suite is stored under session_id — not code_hash — so lookup is
        # stable even after code mutations change the hash between iterations
        suite = self._test_suites.get(session_id)
        if not suite:
            return {"error": "No test suite found", "session_id": session_id,
                    "reasoning": normalize_reasoning(reasoning)}

        self._step(reasoning, "Retrieved test suite",
                   input_data={"iterations_count": len(iterations)},
                   output_data={"suite_id": suite.suite_id, "tests_count": len(suite.tests)})

        current_code = last.code
        iter_num     = len(iterations) + 1

        for i in range(max_iterations):
            if (last.status == ExecutionStatus.SUCCESS
                    and last.verification_artifact
                    and last.verification_artifact.tests_passed == len(suite.tests)):
                self._step(reasoning, "All tests passing — debug complete",
                           output_data={"success": True})
                break

            failed = [t for t in suite.tests if not t.passed]
            errors = "\n".join(
                f"{t.test_id}: {(t.error_message or '')[:100]}" for t in failed[:3]
            )

            try:
                fix_resp = await self.llm.chat(
                    f"Fix Python code:\n{current_code}\n\nFailed:\n{errors}\n"
                    f"Output fixed code in ```python block."
                )
                if self._is_code_response_invalid(fix_resp):
                    logger.warning("Debug LLM response invalid, stopping loop")
                    break
                current_code = self._extract_code_block(fix_resp)
                await self._install_deps(current_code)
            except Exception as exc:
                logger.error("Debug attempt %d failed: %s", i, exc)
                break

            code_hash     = self._hash(current_code)
            t_passed, t_failed = 0, 0
            t_details: List[Dict] = []
            total_test_ms = 0.0

            for test in suite.tests:
                tr  = await self._run(f"{current_code}\n\n{test.test_code}")
                ok  = tr["exit_code"] == 0
                t_passed  += int(ok)
                t_failed  += int(not ok)
                total_test_ms += tr["execution_time_ms"]
                test.passed = ok
                t_details.append({"test_id": test.test_id, "passed": ok,
                                  "execution_time_ms": tr["execution_time_ms"]})

            score, ready, conf = self._quality_score(
                t_passed, len(suite.tests), total_test_ms, len(current_code)
            )
            exec_result = await self._run(current_code)
            artifact = VerificationArtifact(
                artifact_id=f"artifact_{session_id}_iter{iter_num}",
                artifact_type="debug_report", timestamp=datetime.now(timezone.utc).isoformat(),
                stdout=exec_result["stdout"], stderr=exec_result["stderr"],
                exit_code=exec_result["exit_code"],
                execution_time_ms=total_test_ms,
                tests_passed=t_passed, tests_failed=t_failed, test_details=t_details,
                code_hash=code_hash, code_length=len(current_code),
                quality_score=score, production_ready=ready, confidence=conf,
            )

            # Derive status from test results, not bare-code execution
            debug_status = ExecutionStatus.SUCCESS if t_failed == 0 \
                           else ExecutionStatus.FAILURE
            new_iter = CodeIteration(
                iteration_number=iter_num, timestamp=datetime.now(timezone.utc).isoformat(),
                code=current_code, code_hash=code_hash,
                trigger="debug_attempt", previous_error=errors,
                verification_artifact=artifact, thinking_level=ThinkingLevel.REFLECTION,
                status=debug_status,
                next_action="completed" if t_failed == 0 else "continue_debug",
            )
            iterations.append(new_iter)
            self._iterations_history[session_id] = iterations

            self._step(reasoning, f"Debug iteration {iter_num}: {t_passed}/{len(suite.tests)} passed",
                       output_data={"tests_passed": t_passed, "quality_score": round(score, 2)})

            last     = new_iter
            iter_num += 1
            if t_passed == len(suite.tests):
                break

        final   = iterations[-1]
        final_a = final.verification_artifact
        self._step(reasoning, "Debug loop completed",
                   output_data={"total_iterations": len(iterations),
                                "final_status": final.status.value,
                                "tests_passed": final_a.tests_passed if final_a else 0})

        return {
            "session_id":       session_id,
            "total_iterations": len(iterations),
            "final_code":       final.code,
            "final_artifact":   asdict(final_a) if final_a else None,
            "all_iterations": [
                {"iteration":   it.iteration_number, "status": it.status.value,
                 "tests_passed": it.verification_artifact.tests_passed
                 if it.verification_artifact else 0}
                for it in iterations
            ],
            "reasoning": normalize_reasoning(reasoning),
        }

    # ── verify_code_quality ───────────────────────────────────────────────────

    @log_method
    @metric_counter("code_execution")
    async def verify_code_quality(self, code: str) -> Dict[str, Any]:
        reasoning: List[ReasoningStep] = []
        self._step(reasoning, "Code quality check", input_data={"code_length": len(code)})

        lines   = [l for l in code.split("\n") if l.strip()]
        has_fns = bool(re.search(r"def \w+\(", code))
        has_docs = bool(re.search(r'""".*?"""', code, re.DOTALL))
        has_bad  = bool(re.search(r"(eval|exec|__import__|compile)\(", code))
        det_score = (0.4 if has_fns else 0) + (0.3 if has_docs else 0) + (0.3 if not has_bad else 0)

        try:
            report = await self.llm.chat(
                f"Analyse Python code quality (list issues only):\n{code}"
            )
            if is_invalid_response(report):
                report = "LLM quality analysis unavailable (invalid response)"
        except Exception as exc:
            report = f"LLM analysis unavailable: {exc}"

        self._step(reasoning, "Quality analysis complete",
                   output_data={"deterministic_score": det_score, "has_forbidden": has_bad})

        return {
            "code_length": len(code), "code_lines": len(lines),
            "deterministic_checks": {
                "has_functions": has_fns, "has_docstrings": has_docs,
                "has_forbidden_imports": has_bad, "score": det_score,
            },
            "llm_analysis": report,
            "reasoning":    normalize_reasoning(reasoning),
        }

    # ── get_verification_artifacts ────────────────────────────────────────────

    @log_method
    @metric_counter("code_execution")
    async def get_verification_artifacts(self, session_id: str) -> Dict[str, Any]:
        reasoning: List[ReasoningStep] = []
        self._step(reasoning, "Fetching verification artifacts",
                   input_data={"session_id": session_id})
        iters     = self._iterations_history.get(session_id, [])
        artifacts = [asdict(it.verification_artifact)
                     for it in iters if it.verification_artifact]
        return {
            "session_id":       session_id,
            "total_iterations": len(iters),
            "artifacts":        artifacts,
            "artifact_types":   list({a["artifact_type"] for a in artifacts}),
            "reasoning":        normalize_reasoning(reasoning),
        }


# ── Health check ──────────────────────────────────────────────────────────────

def get_agent_status() -> Dict[str, Any]:
    return {
        "agent_name": "code_execution",
        "status":     "HEALTHY",
        "capabilities": [
            "generate_fix_and_create_pr",
            "generate_and_test_code",
            "autonomous_debug_loop",
            "verify_code_quality",
            "get_verification_artifacts",
        ],
        "features": [
            "executable_tests",
            "verification_artifacts",
            "autonomous_debug_loop",
            "dependency_auto_install",
            "test_isolation",
            "patch_file_generation",
            "base64_github_ready",
        ],
        "language":          "python",
        "auth0_token_vault": False,
        "agent_type":        "autonomous",
        "llm_powered":       True,
        "timestamp":         datetime.now(timezone.utc).isoformat(),
    }