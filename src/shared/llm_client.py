"""
Claude API Client for Authorized DevOps Agent System.

Public interface: Anthropic Claude (claude-3-5-sonnet)
Backend routing: configured via environment (see .env)
"""

import asyncio
import logging
import os
from typing import Dict, Any, Optional

import httpx

logger = logging.getLogger("llm_client")


class LLMClient:
    """
    Anthropic Claude client for multi-agent DevOps system.

    Features:
    - Retry with exponential backoff on 429 and transient errors
    - Persistent HTTP connection pool
    - UTF-8 safe for Docker slim images
    """

    DEFAULT_TEMPERATURE = 0.7
    DEFAULT_MAX_TOKENS = 2048
    MAX_RETRIES = 3

    def __init__(self):
        # Anthropic API config
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.base_url = os.getenv(
            "ANTHROPIC_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"
        )
        self.model = os.getenv("ANTHROPIC_MODEL", "anthropic/claude-3-5-sonnet-20241022")
        self.enabled = bool(self.api_key)

        if not self.enabled:
            logger.warning(
                "ANTHROPIC_API_KEY not set. LLM client disabled; chat calls return error messages."
            )
        else:
            logger.info("LLM Client initialized", extra={"model": self.model})

        # Safe headers: no non-ASCII characters
        self._headers = {
            "Authorization": f"Bearer {self.api_key}" if self.enabled else "",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv("LLM_REFERER", "https://devpost.com"),
            "X-Title": "Authorized DevOps Agent - Auth0 Hackathon",  # only ASCII!
        }

        # Persistent client
        self._client: Optional[httpx.AsyncClient] = None
        # Force UTF-8 in Python runtime
        os.environ["PYTHONIOENCODING"] = "utf-8"
        os.environ["LANG"] = "C.UTF-8"

    async def _get_client(self) -> httpx.AsyncClient:
        if not self.enabled:
            return httpx.AsyncClient()

        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def chat(
        self,
        prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> str:
        if not self.enabled:
            return "[LLM error] ANTHROPIC_API_KEY not set"

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens or self.DEFAULT_MAX_TOKENS,
            "temperature": temperature if temperature is not None else self.DEFAULT_TEMPERATURE,
        }

        client = await self._get_client()
        retries = max_retries or self.MAX_RETRIES
        last_error: Optional[Exception] = None

        for attempt in range(retries):
            try:
                resp = await client.post(self.base_url, json=payload, headers=self._headers)

                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning(f"Rate limited (429), retrying in {wait}s (attempt {attempt+1})")
                    await asyncio.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()
                # OpenRouter / Anthropic compatible parsing
                if "choices" in data:
                    return data["choices"][0]["message"]["content"].strip()
                if "content" in data:
                    return data["content"][0].get("text", "").strip()
                return "[LLM error] Unrecognized response"

            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_error = e
                logger.warning(f"Transient error on attempt {attempt+1}: {e}")
                await asyncio.sleep(2 ** attempt)
                continue

            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(f"HTTP {e.response.status_code} on attempt {attempt+1}: {e}")
                if e.response.status_code >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                break

            except Exception as e:
                last_error = e
                logger.error(f"Unexpected LLM error on attempt {attempt+1}: {e}")
                break

        error_msg = str(last_error)[:200] if last_error else "Unknown error"
        logger.error(f"LLM call failed after {retries} attempts: {error_msg}")
        return f"[LLM error] {error_msg}"

    async def chat_structured(self, prompt: str, **kwargs) -> Dict[str, Any]:
        return {
            "raw": await self.chat(prompt, **kwargs),
            "model": self.model,
            "provider": "Anthropic Claude",
        }