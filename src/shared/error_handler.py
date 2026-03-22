import logging
from collections import defaultdict
from datetime import datetime
from functools import wraps
from typing import Dict, List, Optional

logger = logging.getLogger("error_handler")


class ErrorHandler:
    """
    Centralized error tracking and recovery system
    Tracks failures across all agents and provides health status
    """

    def __init__(self):
        self.errors: Dict[str, List[Dict]] = defaultdict(list)
        self.circuit_breakers: Dict[str, Dict] = {}
        self.max_errors_per_agent = 5
        self.circuit_open_duration = 60  # seconds

    def record_error(self, agent_name: str, error: Exception, context: Optional[Dict] = None):
        """Record an error for an agent"""
        error_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": context or {}
        }

        self.errors[agent_name].append(error_record)

        # Keep only last 10 errors per agent
        if len(self.errors[agent_name]) > 10:
            self.errors[agent_name] = self.errors[agent_name][-10:]

        # Check if circuit breaker should open
        recent_errors = self._get_recent_errors(agent_name, window_seconds=30)
        if len(recent_errors) >= 3:
            self._open_circuit(agent_name)
            logger.warning(f"Circuit breaker OPENED for {agent_name} - too many errors")

    def _get_recent_errors(self, agent_name: str, window_seconds: int = 30) -> List[Dict]:
        """Get errors within time window"""
        if agent_name not in self.errors:
            return []

        cutoff = (datetime.utcnow().timestamp() - window_seconds)
        recent = []

        for error in self.errors[agent_name]:
            error_time = datetime.fromisoformat(error["timestamp"]).timestamp()
            if error_time > cutoff:
                recent.append(error)

        return recent

    def _open_circuit(self, agent_name: str):
        """Open circuit breaker for agent"""
        self.circuit_breakers[agent_name] = {
            "opened_at": datetime.utcnow().isoformat(),
            "status": "open"
        }

    def _close_circuit(self, agent_name: str):
        """Close circuit breaker for agent"""
        if agent_name in self.circuit_breakers:
            self.circuit_breakers[agent_name]["status"] = "closed"
            self.circuit_breakers[agent_name]["closed_at"] = datetime.utcnow().isoformat()

    def is_circuit_open(self, agent_name: str) -> bool:
        """Check if circuit breaker is open for agent"""
        if agent_name not in self.circuit_breakers:
            return False

        breaker = self.circuit_breakers[agent_name]
        if breaker["status"] != "open":
            return False

        # Auto-close after duration
        opened_at = datetime.fromisoformat(breaker["opened_at"])
        elapsed = (datetime.utcnow() - opened_at).total_seconds()

        if elapsed > self.circuit_open_duration:
            self._close_circuit(agent_name)
            logger.info(f"Circuit breaker AUTO-CLOSED for {agent_name}")
            return False

        return True

    def get_health_report(self) -> Dict:
        """Generate health report for all agents"""
        report = {
            "timestamp": datetime.utcnow().isoformat(),
            "agents": {}
        }

        all_agents = set(self.errors.keys()) | set(self.circuit_breakers.keys())

        for agent in all_agents:
            recent_errors = self._get_recent_errors(agent, window_seconds=60)
            circuit_open = self.is_circuit_open(agent)

            health_status = "healthy"
            if circuit_open:
                health_status = "circuit_open"
            elif len(recent_errors) >= 2:
                health_status = "degraded"
            elif len(recent_errors) >= 1:
                health_status = "warning"

            report["agents"][agent] = {
                "status": health_status,
                "recent_errors_count": len(recent_errors),
                "total_errors_count": len(self.errors.get(agent, [])),
                "circuit_breaker": "open" if circuit_open else "closed",
                "last_error": self.errors[agent][-1] if agent in self.errors and self.errors[agent] else None
            }

        return report

    def clear_errors(self, agent_name: Optional[str] = None):
        """Clear error history"""
        if agent_name:
            self.errors[agent_name] = []
            if agent_name in self.circuit_breakers:
                self._close_circuit(agent_name)
        else:
            self.errors.clear()
            self.circuit_breakers.clear()


# Global singleton instance
_error_handler = ErrorHandler()


def get_error_handler() -> ErrorHandler:
    """Get global error handler instance"""
    return _error_handler


# Decorator for automatic error handling
def handle_agent_errors(agent_name: str):
    """Decorator to automatically catch and log agent errors"""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            handler = get_error_handler()

            # Check circuit breaker
            if handler.is_circuit_open(agent_name):
                logger.error(f"Circuit breaker OPEN for {agent_name} - request rejected")
                return {
                    "error": f"Service temporarily unavailable - {agent_name} circuit breaker is open",
                    "circuit_breaker": "open",
                    "retry_after": 60
                }

            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {agent_name}.{func.__name__}", extra={
                    "error": str(e),
                    "error_type": type(e).__name__
                })

                # Record error
                handler.record_error(agent_name, e, context={
                    "function": func.__name__,
                    "args": str(args)[:100]
                })

                return {
                    "error": f"Agent execution failed: {str(e)}",
                    "agent": agent_name,
                    "function": func.__name__,
                    "error_type": type(e).__name__
                }

        return wrapper

    return decorator
