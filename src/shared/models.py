from datetime import datetime
from typing import Any, Dict

from pydantic import BaseModel, Field


class ReasoningStep(BaseModel):
    step_number: int
    description: str
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    input_data: Dict[str, Any] = Field(default_factory=dict)
    output: Dict[str, Any] = Field(default_factory=dict)
    agent: str | None = None
