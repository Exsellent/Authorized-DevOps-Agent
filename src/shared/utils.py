"""
Shared utilities to eliminate code duplication across agents.
"""

import json
import logging
import re
from datetime import datetime
from functools import wraps
from typing import List, Dict, Optional, Any

from shared.models import ReasoningStep

logger = logging.getLogger("shared.utils")

# ---------------------------------------------------------------------------
# LLM response validation
# ---------------------------------------------------------------------------

_INVALID_RESPONSE_INDICATORS = [
    "[stub]", "[llm error]", "[claude error]", "[gitlab duo error]",
    "client error",
    "for more information check",
    "connection error",
]


def is_invalid_response(response: str) -> bool:
    """
    Check if an LLM response is a stub, error, or otherwise unusable.
    """
    text = response.lower()
    return any(indicator in text for indicator in _INVALID_RESPONSE_INDICATORS)


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def log_method(func):
    """
    Decorator for logging async method calls with proper function introspection.
    """

    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        logger.info(f"{func.__name__} called")
        try:
            result = await func(self, *args, **kwargs)
            logger.info(f"{func.__name__} completed successfully")
            return result
        except Exception as e:
            logger.error(f"{func.__name__} failed: {str(e)}")
            raise

    return wrapper


# ---------------------------------------------------------------------------
# Reasoning trail helpers
# ---------------------------------------------------------------------------

def next_step(
        reasoning: List[ReasoningStep],
        description: str,
        agent_name: str,
        input_data: Optional[Dict] = None,
        output_data: Optional[Dict] = None,
):
    """
    Append a sequential reasoning step.
    """
    reasoning.append(ReasoningStep(
        step_number=len(reasoning) + 1,
        description=description,
        timestamp=datetime.now().isoformat(),
        input_data=input_data or {},
        output=output_data or {},  # field renamed to 'output' in models.py
        agent=agent_name,
    ))


def normalize_reasoning(reasoning: List[ReasoningStep]) -> List[Dict[str, Any]]:
    """
    Convert ReasoningStep Pydantic models to dicts for JSON serialization.
    """
    return [step.model_dump(exclude_none=True) for step in reasoning]


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def safe_parse_json(text: str, fallback: Optional[Dict] = None) -> Dict:
    """
    Safely parse JSON from LLM output using multiple extraction strategies.
    """

    # Strategy 1: direct parse
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Strategy 2: extract JSON array
    json_array_match = re.search(r'\[[\s\S]*\]', text)
    if json_array_match:
        try:
            return json.loads(json_array_match.group())
        except json.JSONDecodeError:
            pass

    # Strategy 3: extract JSON object (supports nested structures)
    json_match = re.search(
        r'\{[^{}]*(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}[^{}]*)*\}',
        text,
    )
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    logger.warning(f"Failed to parse JSON, using fallback: {text[:100]}")
    return fallback or {}


# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    r'ignore\s+(all\s+)?previous\s+instructions',
    r'disregard\s+(all\s+)?previous',
    r'forget\s+(all\s+)?previous',
    r'new\s+instructions?\s*:',
    r'system\s*:',
    r'<\|.*?\|>',
]

_INJECTION_RE = re.compile('|'.join(_INJECTION_PATTERNS), flags=re.IGNORECASE)


def sanitize_user_input(text: str, max_length: int = 10000) -> str:
    """
    Sanitize user input to mitigate prompt injection.
    """
    if not text:
        return ""

    text = text[:max_length]
    text = _INJECTION_RE.sub('', text)
    return text.strip()
