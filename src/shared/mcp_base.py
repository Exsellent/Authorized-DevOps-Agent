import inspect
import json
import logging
import os
from typing import Any, Dict

from fastapi import FastAPI, Request
from pydantic import BaseModel, ValidationError

from shared.cors import setup_cors

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)


def finalize_output(obj: Any, agent_name: str) -> Any:
    """
    Recursively clean and normalize output for MCP/UI compatibility:
    - Convert Pydantic models using .model_dump(exclude_none=True)
    - Remove all None values
    - Add agent name to ReasoningStep-like dicts if missing
    """
    if isinstance(obj, BaseModel):
        return finalize_output(obj.model_dump(exclude_none=True), agent_name)

    if isinstance(obj, dict):
        new_dict = {}
        for k, v in obj.items():
            if k == "agent" and v is None:
                new_dict[k] = agent_name
                continue
            if v is not None:
                new_dict[k] = finalize_output(v, agent_name)
        if "step_number" in new_dict and "description" in new_dict:
            new_dict.setdefault("agent", agent_name)
        return new_dict

    if isinstance(obj, list):
        return [finalize_output(item, agent_name) for item in obj if item is not None]

    return obj


class MCPRequest(BaseModel):
    method: str
    params: Dict[str, Any]
    id: int | None = None


class MCPAgent:
    def __init__(self, name: str):
        self.name = name
        self.app = FastAPI()
        self.tools: Dict[str, Any] = {}

        # CORS -- single source of truth in shared/cors.py
        setup_cors(self.app)

        @self.app.get("/health")
        def health():
            return {"status": "ok", "agent": self.name}

        @self.app.post("/mcp")
        async def mcp(request: Request):
            try:
                body = await request.body()
                data = json.loads(body.decode("utf-8"))
                req = MCPRequest(**data)
            except json.JSONDecodeError as e:
                return {
                    "error": "Invalid JSON",
                    "details": str(e),
                    "hint": "Send valid JSON with 'method' and 'params'",
                }
            except ValidationError as e:
                return {
                    "error": "Invalid MCP request format",
                    "details": str(e),
                    "hint": "Required fields: method (str), params (dict)",
                }
            except Exception as e:
                return {
                    "error": "Request processing failed",
                    "details": str(e),
                }

            tool_name = req.method.replace("tools/", "")
            handler = self.tools.get(tool_name)

            if not handler:
                return {
                    "error": f"Unknown tool: {tool_name}",
                    "available_tools": list(self.tools.keys()),
                }

            try:
                if inspect.iscoroutinefunction(handler):
                    result = await handler(**req.params)
                else:
                    result = handler(**req.params)

                return finalize_output(result, self.name)

            except TypeError as e:
                sig = inspect.signature(handler)
                expected_params = [p for p in sig.parameters.keys() if p != "self"]
                return {
                    "error": f"Invalid parameters for tool '{tool_name}'",
                    "details": str(e),
                    "received_params": list(req.params.keys()),
                    "expected_params": expected_params,
                }
            except Exception as e:
                return {
                    "error": f"Tool execution failed: {tool_name}",
                    "details": str(e),
                }

    def register_tool(self, name: str, func):
        """Register a tool function (sync or async)."""
        self.tools[name] = func