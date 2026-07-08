"""Observability layer: structured logging + request correlation.

See app/observability/logger.py. Metric aggregation lives in
app/services/metrics_service.py; persisted event tables (usage_events,
error_events) live in app/models.py.
"""
from app.observability.logger import (
    configure_logging,
    get_logger,
    new_request_id,
    set_request_id,
    reset_request_id,
    current_request_id,
)

__all__ = [
    "configure_logging",
    "get_logger",
    "new_request_id",
    "set_request_id",
    "reset_request_id",
    "current_request_id",
]
