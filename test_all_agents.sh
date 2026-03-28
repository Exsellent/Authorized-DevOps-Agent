#!/bin/bash

set -e

BASE="http://localhost"

echo "=== TEST 1: Planner (8601) ==="
curl -s --max-time 30 -X POST $BASE:8601/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "plan",
    "params": {
      "description": "Test issue: something is broken",
      "context": "This is a test issue for Planner agent"
    }
  }' | jq

echo
echo


echo "=== TEST 2: Risks (8603) ==="
curl -s --max-time 30 -X POST $BASE:8603/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "analyze_risks",
    "params": {
      "feature": "Bug in system",
      "issue_number": 123,
      "title": "Test issue: something is broken",
      "context": "This is a test issue for Risks agent"
    }
  }' | jq

echo
echo


echo "=== TEST 3: Progress (8602) — analyze_progress ==="
curl -s --max-time 30 -X POST $BASE:8602/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "analyze_progress",
    "params": {
      "commits": [
        "Fix API bug in auth module",
        "Refactor database connector",
        "Improve logging in orchestrator"
      ]
    }
  }' | jq

echo
echo


echo "=== TEST 4: Digest (8604) — daily_digest ==="
curl -s --max-time 30 -X POST http://localhost:8604/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "daily_digest",
    "params": {
      "date": "2026-03-16",
      "context": "Planner classified issue #12 (auth bug). Risks detected 1 medium risk on PR #14. Progress velocity stable at 5 commits today."
    }
   }' | jq

echo
echo


echo "=== TEST 5: Orchestrator (8600) — triage_single ==="
curl -s --max-time 30 -X POST $BASE:8600/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 5,
    "method": "triage_single",
    "params": {
      "issue": {
        "id": 999999,
        "iid": 123,
        "title": "Test issue: something is broken",
        "description": "This is a test issue for full triage pipeline"
      }
    }
  }' | jq

echo
echo


echo "=== TEST 6: CodeExecution (8605) — generate_fix_and_create_pr ==="
curl -s -X POST http://localhost:8605/mcp \
-H "Content-Type: application/json" \
-d '{
  "jsonrpc": "2.0",
  "method": "generate_fix_and_create_pr",
  "params": {
     "repo": "test/test",
     "goal": "fix auth"
  }
}' | jq 'del(.patch_files[].content_base64)'

echo
echo "=== ALL TESTS COMPLETED ==="
