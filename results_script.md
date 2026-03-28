```bash
 chmod +x test_all_agents.sh
./test_all_agents.sh
=== TEST 1: Planner (8601) ===
{
  "task": "Test issue: something is broken",
  "executive_summary": {
    "overview": "Bug task — medium complexity, priority P1, assigned to backend team.",
    "main_risks": [
      "Standard delivery risk for this complexity level"
    ],
    "critical_path": [
      "Investigate the logs to identify the source of the error",
      "Reproduce the issue in a local development environment",
      "Review recent code changes that could have affected the functionality"
    ],
    "recommended_focus": "Investigate the logs to identify the source of the error",
    "confidence_label": "low",
    "estimated_duration": "4.1 day(s) (32.7 h)",
    "complexity_assessment": "Medium complexity, low confidence estimate"
  },
  "classification": {
    "task_type": "bug",
    "complexity": "medium",
    "technical_uncertainty": "medium",
    "priority_hint": "P1",
    "auto_labels": [
      "type::bug",
      "complexity::medium"
    ],
    "suggested_assignee_team": "backend",
    "reasoning": "The description indicates that something is broken, which suggests a bug that may require urgent attention."
  },
  "subtasks": [
    "Investigate the logs to identify the source of the error",
    "Reproduce the issue in a local development environment",
    "Review recent code changes that could have affected the functionality",
    "Write unit tests to cover the affected functionality",
    "Fix the identified bug in the codebase",
    "Update documentation to reflect any changes made during the bug fix",
    "Deploy the fix to a staging environment for verification",
    "Monitor the staging environment for any recurring issues after the deployment"
  ],
  "predictive_estimate": {
    "base_estimate_hours": 32.72727272727273,
    "confidence_interval_low": 24.545454545454547,
    "confidence_interval_high": 45.81818181818181,
    "confidence_level": 0.08000000000000002,
    "similar_tasks_analyzed": 2,
    "accuracy_factors": {
      "historical_data_quality": 0.4,
      "variance_penalty": 0.7856742013183862,
      "match_type": "broad"
    },
    "confidence_label": "low"
  },
  "estimated_days": 4.1,
  "fallback_used": false,
  "timestamp": "2026-03-20T12:36:04.500518"
}


=== TEST 2: Risks (8603) ===
{
  "feature": "Bug in system",
  "issue_ref": "#123: Test issue: something is broken",
  "analysis_mode": "baseline",
  "overall_risk_level": "MEDIUM",
  "priority": "P2",
  "risk_score": 1,
  "issues_found": [
    {
      "title": "General implementation risk",
      "severity": "LOW",
      "category": "operational",
      "mitigation_strategy": "Follow standard development and review practices.",
      "priority": 4
    }
  ],
  "risks": [
    {
      "risk_id": "GEN-001",
      "title": "General implementation risk",
      "category": "operational",
      "severity": "low",
      "description": "Standard implementation risks apply.",
      "likelihood": "low",
      "potential_impact": "Minor delays or quality issues",
      "mitigation_strategy": "Follow standard development and review practices.",
      "priority": 4,
      "timeline": "During development",
      "source": "baseline"
    }
  ],
  "executive_summary": {
    "overall_risk_level": "MEDIUM",
    "total_risks": 1,
    "critical_count": 0,
    "high_count": 0,
    "medium_count": 0,
    "low_count": 1,
    "top_concerns": [
      "General implementation risk"
    ],
    "mitigation_priorities": [
      "Follow standard development and review practices."
    ],
    "go_no_go_recommendation": "GO: Acceptable risk level — proceed with standard engineering practices",
    "confidence_level": "medium",
    "timestamp": "2026-03-20T12:36:05.852400"
  },
  "automated_actions": {
    "actions": [
      "add_extra_testing",
      "request_design_review"
    ],
    "priority": "P2",
    "block_pr": false,
    "require_review": false
  },
  "reasoning": [
    {
      "step_number": 1,
      "description": "Proactive risk assessment requested",
      "timestamp": "2026-03-20T12:36:04.610293",
      "input_data": {
        "feature": "Bug in system",
        "issue_ref": "#123: Test issue: something is broken",
        "task_type": "unknown",
        "file_count": 0
      },
      "output": {},
      "agent": "Risks"
    },
    {
      "step_number": 2,
      "description": "Dummy issue guardrail triggered — using baseline risks",
      "timestamp": "2026-03-20T12:36:04.612880",
      "input_data": {},
      "output": {
        "risks_detected": 1
      },
      "agent": "Risks"
    },
    {
      "step_number": 3,
      "description": "Risk assessment prompt generated",
      "timestamp": "2026-03-20T12:36:04.613082",
      "input_data": {},
      "output": {
        "prompt_length": 1858,
        "mode": "proactive_planning"
      },
      "agent": "Risks"
    },
    {
      "step_number": 4,
      "description": "LLM response not parseable — using only baseline",
      "timestamp": "2026-03-20T12:36:05.850773",
      "input_data": {},
      "output": {
        "risks_detected": 1
      },
      "agent": "Risks"
    },
    {
      "step_number": 5,
      "description": "Overall risk scored and actions determined",
      "timestamp": "2026-03-20T12:36:05.852269",
      "input_data": {},
      "output": {
        "overall_risk": "MEDIUM",
        "risk_score": 1,
        "priority": "P2",
        "auto_actions": [
          "add_extra_testing",
          "request_design_review"
        ]
      },
      "agent": "Risks"
    },
    {
      "step_number": 6,
      "description": "Risk assessment complete",
      "timestamp": "2026-03-20T12:36:05.852573",
      "input_data": {},
      "output": {
        "overall_risk": "MEDIUM",
        "risk_score": 1,
        "total_risks": 1,
        "go_no_go": "GO: Acceptable risk level — proceed with standard engineering practices",
        "analysis_mode": "baseline"
      },
      "agent": "Risks"
    }
  ],
  "metadata": {
    "agent": "Risks",
    "focus": "proactive_planning_risks",
    "analysis_mode": "baseline",
    "auth0_token_vault": false,
    "timestamp": "2026-03-20T12:36:05.854857"
  }
}


=== TEST 3: Progress (8602) — analyze_progress ===
{
  "repo": "GitHub repository",
  "commits_count": 3,
  "summary": "Three commits made focusing on bug fixes, refactoring, and improvements in logging.",
  "velocity_signal": "STEADY",
  "auto_actions": [
    "update_dashboard"
  ],
  "fallback_used": false,
  "reasoning": [
    {
      "step_number": 1,
      "description": "Received commits for progress analysis",
      "timestamp": "2026-03-20T12:36:05.997245",
      "input_data": {
        "commits_count": 3,
        "repo": "GitHub repository"
      },
      "output": {},
      "agent": "Progress"
    },
    {
      "step_number": 2,
      "description": "LLM commit analysis complete",
      "timestamp": "2026-03-20T12:36:07.873441",
      "input_data": {},
      "output": {
        "summary_length": 83,
        "fallback_used": false
      },
      "agent": "Progress"
    },
    {
      "step_number": 3,
      "description": "Velocity signal and actions determined",
      "timestamp": "2026-03-20T12:36:07.874292",
      "input_data": {},
      "output": {
        "velocity_signal": "STEADY",
        "auto_actions": [
          "update_dashboard"
        ]
      },
      "agent": "Progress"
    }
  ],
  "timestamp": "2026-03-20T12:36:07.876111"
}


=== TEST 4: Digest (8604) — daily_digest ===
{
  "date": "2026-03-16",
  "repo": "GitHub repository",
  "summary": "## 📅 Daily Digest - 2026-03-16\n\n### 📊 Progress\nStable progress with 5 commits made today.\n\n### ⚠️ Blockers\n1 medium risk detected on PR #14 related to auth bug in issue #12.\n\n### 👥 Team Health\nTeam is maintaining a stable velocity.\n\n### ▶️ Next Steps\nAddress the medium risk on PR #14 and continue monitoring the auth bug issue.\n",
  "sections": {
    "progress": "Stable progress with 5 commits made today.",
    "risk_summary": "1 medium risk detected on PR #14 related to auth bug in issue #12.",
    "team_health": "Team is maintaining a stable velocity.",
    "next_steps": "Address the medium risk on PR #14 and continue monitoring the auth bug issue.",
    "full_text": "## 📅 Daily Digest - 2026-03-16\n\n### 📊 Progress\nStable progress with 5 commits made today.\n\n### ⚠️ Blockers\n1 medium risk detected on PR #14 related to auth bug in issue #12.\n\n### 👥 Team Health\nTeam is maintaining a stable velocity.\n\n### ▶️ Next Steps\nAddress the medium risk on PR #14 and continue monitoring the auth bug issue.\n"
  },
  "validation": {
    "word_count": 61,
    "under_limit": true,
    "has_pr_section": true,
    "has_risk_section": true,
    "has_summary": true,
    "tone_positive": false,
    "confidence": 0.95,
    "quality_state": "HEALTHY"
  },
  "quality_state": "HEALTHY",
  "automated_actions": {
    "actions": [
      "send_to_slack",
      "post_to_github_comment",
      "notify_pm"
    ],
    "escalation_level": "pm"
  },
  "fallback_used": false,
  "reasoning": [
    {
      "step_number": 1,
      "description": "Daily digest generation requested",
      "timestamp": "2026-03-20T12:36:07.946110",
      "input_data": {
        "date": "2026-03-16",
        "repo": "GitHub repository",
        "context_provided": true
      },
      "output": {},
      "agent": "Digest"
    },
    {
      "step_number": 2,
      "description": "LLM daily digest generated",
      "timestamp": "2026-03-20T12:36:10.252970",
      "input_data": {},
      "output": {
        "word_count": 61,
        "fallback": false
      },
      "agent": "Digest"
    },
    {
      "step_number": 3,
      "description": "Daily digest complete",
      "timestamp": "2026-03-20T12:36:10.254017",
      "input_data": {},
      "output": {
        "quality_state": "HEALTHY",
        "auto_actions": [
          "send_to_slack",
          "post_to_github_comment",
          "notify_pm"
        ]
      },
      "agent": "Digest"
    }
  ],
  "metadata": {
    "agent": "Digest",
    "auth0_token_vault": false,
    "timestamp": "2026-03-20T12:36:10.258951"
  }
}


=== TEST 5: Orchestrator (8600) — triage_single ===
{
  "issue_number": 123,
  "title": "Test issue: something is broken",
  "classification": {
    "task_type": "other",
    "complexity": "low",
    "technical_uncertainty": "low",
    "priority_hint": "P3",
    "auto_labels": [],
    "suggested_assignee_team": "qa",
    "reasoning": "This is a test issue for the triage pipeline, indicating no real problem to address."
  },
  "risk_level": "MEDIUM",
  "priority": "P3",
  "labels": [
    "type::other",
    "priority::P3",
    "complexity::low",
    "ai-triage"
  ],
  "html_url": "",
  "agents_used": [
    "planner",
    "risks"
  ]
}


=== TEST 6: CodeExecution (8605) — generate_fix_and_create_pr ===
{
  "patch_files": [
    {
      "path": "src/security/fix_auth_fix.py",
      "commit_message": "fix(security_fix): fix auth"
    }
  ],
  "code_diff": "--- a/src/security/fix_auth_fix.py\n+++ b/src/security/fix_auth_fix.py\n@@ Generated fix @@\n+import hashlib\n+import hmac\n+import json\n+\n+class Auth:\n+    def __init__(self):\n+        self.users = {}\n+        self.failed_attempts = {}\n+        self.lockout_until = {}\n+        self.mock_time = 0\n+        self.max_attempts = 3\n+\n+    def hash_password(self, password):\n+        return hashlib.sha256(password.encode()).hexdigest()\n+\n+    def register(self, username, password):\n+        if username in self.users:\n+            raise Exception(\"User already exists\")\n+        if len(password) < 6:\n+            raise Exception(\"Password too weak\")\n+        self.users[username] = self.hash_password(password)\n+\n+    def login(self, username, password):\n+        if username in self.lockout_until:\n+            if self.mock_time < self.lockout_until[username]:\n+                raise Exception(\"Account locked due to too many failed attempts\")\n+            else:\n+                del self.lockout_until[username]\n+                self.failed_attempts[username] = 0\n+        if username not in self.users or self.users[username] != self.hash_password(password):\n+            self.increment_failed_attempts(username)\n+            raise Exception(\"Invalid credentials\")\n+        self.failed_attempts[username] = 0\n+        return \"Login successful\"\n+\n+    def increment_failed_attempts(self, username):\n+        self.failed_attempts[username] = self.failed_attempts.get(username, 0) + 1\n+        if self.failed_attempts[username] >= self.max_attempts:\n+            self.lockout_until[username] = self.mock_time + 5\n+            raise Exception(\"Account locked due to too many failed attempts\")",
  "tests_passed": true,
  "quality_score": 0.9999999999999999,
  "verification_artifact": {
    "artifact_id": "artifact_session_20260320_123649_611484_iter0",
    "artifact_type": "test_report",
    "timestamp": "2026-03-20T12:36:49.969504+00:00",
    "stdout": "",
    "stderr": "",
    "exit_code": 0,
    "execution_time_ms": 320.75800000000004,
    "tests_passed": 5,
    "tests_failed": 0,
    "test_details": [
      {
        "test_id": "test_1",
        "passed": true,
        "execution_time_ms": 184.472,
        "output": "PASSED: test_register_success\nPASSED: test_register_success\n",
        "error": ""
      },
      {
        "test_id": "test_2",
        "passed": true,
        "execution_time_ms": 32.304,
        "output": "PASSED: test_login_success\nPASSED: test_login_success\n",
        "error": ""
      },
      {
        "test_id": "test_3",
        "passed": true,
        "execution_time_ms": 33.8,
        "output": "PASSED: test_invalid_credentials\nPASSED: test_invalid_credentials\n",
        "error": ""
      },
      {
        "test_id": "test_4",
        "passed": true,
        "execution_time_ms": 36.932,
        "output": "PASSED: test_lockout\nPASSED: test_lockout\n",
        "error": ""
      },
      {
        "test_id": "test_5",
        "passed": true,
        "execution_time_ms": 33.25,
        "output": "PASSED: test_lockout_release\nPASSED: test_lockout_release\n",
        "error": ""
      }
    ],
    "code_hash": "2ae117428a3dcd7a",
    "code_length": 3323,
    "quality_score": 0.9999999999999999,
    "production_ready": true,
    "confidence": 1.0
  },
  "session_id": "session_20260320_123649_611484",
  "code_hash": "2ae117428a3dcd7a",
  "test_suite": {
    "suite_id": "suite_session_20260320_123649_611484",
    "tests_count": 5,
    "tests_passed": 5,
    "tests_failed": 0
  },
  "quality_metrics": {
    "quality_score": 0.9999999999999999,
    "production_ready": true,
    "confidence": 1.0
  },
  "fallback_used": false,
  "auth0_token_vault": true
}

=== ALL TESTS COMPLETED ===
```