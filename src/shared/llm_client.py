"""
Claude API Client for Authorized DevOps Agent System.

Public interface: Anthropic Claude (claude-3-5-sonnet)
Backend routing: configured via environment (see .env)
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("llm_client")


class LLMClient:
    """
    Anthropic Claude client for multi-agent DevOps pipeline.

    Uses Claude 3.5 Sonnet for all agent reasoning:
    - Planner: task classification and decomposition
    - Risks: proactive security assessment
    - Code Execution: fix generation and test writing
    - Progress: velocity analysis
    - Digest: executive summary generation

    Configuration via environment variables::

        ANTHROPIC_API_KEY   — Claude API key
        ANTHROPIC_BASE_URL  — API endpoint (default: Anthropic direct)
        ANTHROPIC_MODEL     — Model name (default: claude-3-5-sonnet-20241022)
    """

    # Shown in logs and UI — always Claude branding
    _PROVIDER_DISPLAY = "Anthropic Claude"

    def __init__(self):
        self.api_key   = os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url  = os.getenv(
            "ANTHROPIC_BASE_URL",
            "https://api.anthropic.com/v1/messages"   # direct Anthropic default
        )
        self.model     = os.getenv(
            "ANTHROPIC_MODEL",
            "claude-3-5-sonnet-20241022"
        )
        self.timeout   = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
        self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "2048"))

        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required. "
                "Set it in .env (see .env.example)."
            )

        # Detect if routing through a proxy/gateway
        self._via_gateway = "anthropic.com" not in self.base_url

        logger.info(
            "LLMClient ready — provider=%s model=%s gateway=%s",
            self._PROVIDER_DISPLAY, self.model, self._via_gateway,
        )

    # ── Internal: build request depending on endpoint type ────────────────────

    def _build_request(self, prompt: str) -> tuple[dict, dict]:
        """
        Returns (headers, payload) adapted to the endpoint.

        Anthropic native endpoint uses a different schema than
        OpenAI-compatible endpoints (OpenRouter, etc.).
        """
        if self._via_gateway:
            # OpenAI-compatible schema (OpenRouter / proxy)
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://devpost.com",
                "X-Title":       "Authorized DevOps Agent — Auth0 Hackathon",
            }
            payload = {
                "model":       self.model,
                "messages":    [{"role": "user", "content": prompt}],
                "max_tokens":  self.max_tokens,
                "temperature": 0.7,
            }
        else:
            # Native Anthropic Messages API
            headers = {
                "x-api-key":         self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type":      "application/json",
            }
            payload = {
                "model":       self.model,
                "max_tokens":  self.max_tokens,
                "messages":    [{"role": "user", "content": prompt}],
            }

        return headers, payload

    def _parse_response(self, data: dict) -> str:
        """
        Extract text from response, handling both API schemas.
        """
        # Native Anthropic → data["content"][0]["text"]
        if "content" in data and isinstance(data["content"], list):
            return data["content"][0].get("text", "").strip()

        # OpenAI-compatible → data["choices"][0]["message"]["content"]
        if "choices" in data:
            return data["choices"][0]["message"]["content"].strip()

        raise ValueError(f"Unrecognised response shape: {list(data.keys())}")

    # ── Public API ────────────────────────────────────────────────────────────

    async def chat(self, prompt: str, max_retries: int = 3) -> str:
        """
        Send a prompt to Claude and return the text response.

        Retries on 429 (rate limit) with exponential backoff.
        Returns an error-marker string on terminal failure so callers
        can detect it via _is_invalid_response() without crashing.
        """
        headers, payload = self._build_request(prompt)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(max_retries):
                try:
                    resp = await client.post(
                        self.base_url, json=payload, headers=headers
                    )

                    if resp.status_code == 429:
                        wait = 2 ** attempt
                        logger.warning(
                            "Rate limit hit (attempt %d/%d) — retrying in %ds",
                            attempt + 1, max_retries, wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                    resp.raise_for_status()
                    return self._parse_response(resp.json())

                except httpx.TimeoutException:
                    logger.warning(
                        "LLM request timed out (attempt %d/%d)",
                        attempt + 1, max_retries,
                    )
                    if attempt == max_retries - 1:
                        return "[claude error] Request timed out"
                    await asyncio.sleep(2 ** attempt)

                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "LLM HTTP error %d (attempt %d/%d): %s",
                        exc.response.status_code, attempt + 1, max_retries, exc,
                    )
                    if attempt == max_retries - 1:
                        return f"[claude error] HTTP {exc.response.status_code}"
                    await asyncio.sleep(2 ** attempt)

                except Exception as exc:
                    logger.error(
                        "LLM call failed (attempt %d/%d): %s",
                        attempt + 1, max_retries, exc,
                    )
                    if attempt == max_retries - 1:
                        return f"[claude error] {exc}"
                    await asyncio.sleep(2 ** attempt)

        return "[claude error] Rate limit exceeded after all retries"

    async def chat_structured(
        self,
        prompt:      str,
        system:      Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Extended call returning provider metadata alongside raw text.
        Useful for debugging and agent Observatory UI.
        """
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        text = await self.chat(full_prompt)
        return {
            "raw":      text,
            "model":    self.model,
            "provider": self._PROVIDER_DISPLAY,
        }