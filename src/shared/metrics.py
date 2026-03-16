import logging
from functools import wraps

from prometheus_client import Counter, start_http_server

logger = logging.getLogger("metrics")

tasks_processed = Counter(
    'tasks_processed_total',
    'Total number of processed tasks by agent',
    ['agent']
)

errors_total = Counter(
    'errors_total',
    'Total number of errors by agent',
    ['agent']
)


def start_metrics_server(port=8000):
    """Start Prometheus HTTP server for metrics exposure"""
    try:
        start_http_server(port)
        logger.info(f"Metrics server started on port {port}")
    except Exception as e:
        logger.error(f"Failed to start metrics server: {e}")


def metric_counter(agent_name: str):
    """Decorator to count tasks and errors. Preserves function metadata."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            tasks_processed.labels(agent=agent_name).inc()
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                errors_total.labels(agent=agent_name).inc()
                raise

        return wrapper

    return decorator