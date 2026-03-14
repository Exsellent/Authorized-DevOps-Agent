"""
MCP Base — FastAPI server + tool registry for all agents.

Changes vs. original:
- logger.extra= dict in __init__ removed (breaks some handlers)
- finalize_output: handles Enum values → .value (prevents JSON serialisation errors)
- finalize_output: handles datetime → .isoformat()
- /health returns agent capabilities list (useful for Observatory UI)
- root() endpoint: message updated to match hackathon branding
"""

import inspect
import json
import logging
import os
from dataclasses import is_dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ValidationError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("mcp_base")


def finalize_output(obj: Any, agent_name: str) -> Any:
    """
    Recursively clean and normalise output for MCP / UI compatibility:

    - Pydantic BaseModel  → .model_dump(exclude_none=True)
    - dataclass           → asdict()
    - Enum                → .value  (NEW: prevents JSON serialisation errors)
    - datetime            → .isoformat() (NEW)
    - dict                → strip None values, rename output_data→output
    - list                → filter None, recurse
    - ReasoningStep dicts → ensure agent field is set
    """
    # Pydantic model
    if isinstance(obj, BaseModel):
        return finalize_output(obj.model_dump(exclude_none=True), agent_name)

    # Dataclass
    if is_dataclass(obj) and not isinstance(obj, type):
        return finalize_output(asdict(obj), agent_name)

    # Enum → use .value so JSON serialiser doesn't choke
    if isinstance(obj, Enum):
        return obj.value

    # datetime → ISO string
    if isinstance(obj, datetime):
        return obj.isoformat()

    # Dict
    if isinstance(obj, dict):
        new: Dict[str, Any] = {}
        for k, v in obj.items():
            if k == "output_data":
                # Rename output_data → output (ReasoningStep compat)
                new["output"] = finalize_output(v, agent_name)
                continue
            if k == "agent" and v is None:
                new[k] = agent_name
                continue
            if v is not None:
                new[k] = finalize_output(v, agent_name)
        # Auto-fill agent on ReasoningStep-shaped dicts
        if "step_number" in new and "description" in new:
            new.setdefault("agent", agent_name)
        return new

    # List
    if isinstance(obj, list):
        return [
            finalize_output(item, agent_name)
            for item in obj
            if item is not None
        ]

    # Primitives (str, int, float, bool) — pass through
    return obj


# ── MCP request schema ────────────────────────────────────────────────────────

class MCPRequest(BaseModel):
    method: str
    params: Dict[str, Any]
    id: int | None = None


# ── Base agent ────────────────────────────────────────────────────────────────

class MCPAgent:
    """
    Base class for all agents.

    Each agent subclass calls:
        super().__init__("AgentName")
        self.register_tool("tool_name", self.method)

    The /mcp endpoint routes incoming JSON-RPC calls to registered tools.
    """

    def __init__(self, name: str):
        self.name  = name
        self.app   = FastAPI(title=f"{name} Agent", version="1.0.0")
        self.tools: Dict[str, Any] = {}

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # ── Health endpoint ───────────────────────────────────────────────────
        @self.app.get("/health")
        def health():
            return {
                "status":          "ok",
                "agent":           self.name,
                "available_tools": list(self.tools.keys()),
            }

        # ── Root info endpoint ────────────────────────────────────────────────
        @self.app.get("/")
        def root():
            return {
                "agent":           self.name,
                "project":         "Authorized DevOps Agent — Auth0 Hackathon",
                "available_tools": list(self.tools.keys()),
                "docs":            "/docs",
            }

        # ── MCP dispatcher ────────────────────────────────────────────────────
        @self.app.post("/mcp")
        async def mcp(request: Request):
            # Parse request
            try:
                body = await request.body()
                data = json.loads(body.decode("utf-8"))
                req  = MCPRequest(**data)
            except json.JSONDecodeError as exc:
                return {"error": "Invalid JSON", "details": str(exc)}
            except ValidationError as exc:
                return {
                    "error":   "Invalid MCP request format",
                    "details": str(exc),
                    "hint":    "Required fields: method (str), params (dict)",
                }
            except Exception as exc:
                return {"error": "Request processing failed", "details": str(exc)}

            # Resolve tool
            tool_name = req.method.replace("tools/", "")
            handler   = self.tools.get(tool_name)

            if not handler:
                return {
                    "error":           f"Unknown tool: {tool_name}",
                    "available_tools": list(self.tools.keys()),
                }

            # Execute tool
            try:
                if inspect.iscoroutinefunction(handler):
                    result = await handler(**req.params)
                else:
                    result = handler(**req.params)

                return finalize_output(result, self.name)

            except TypeError as exc:
                sig     = inspect.signature(handler)
                expected = [p for p in sig.parameters if p != "self"]
                return {
                    "error":            f"Invalid parameters for '{tool_name}'",
                    "details":          str(exc),
                    "received_params":  list(req.params.keys()),
                    "expected_params":  expected,
                }
            except Exception as exc:
                logger.error("Tool '%s' failed: %s", tool_name, exc)
                return {
                    "error":   f"Tool execution failed: {tool_name}",
                    "details": str(exc),
                }

    def register_tool(self, name: str, func) -> None:
        """Register a sync or async callable as an MCP tool."""
        self.tools[name] = func