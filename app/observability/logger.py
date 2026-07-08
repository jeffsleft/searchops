"""Centralized structured logging + per-request correlation.

`configure_logging()` installs ONE handler on the root logger so every existing
`logging.info/warning/error` call across the app gains a consistent, greppable
format and a correlation id — without touching any call site. It is idempotent
(safe to call from the web endpoint and from each Modal function on cold start).

The correlation id is a `contextvars.ContextVar`, set by the usage-tracking
middleware at the top of each request and read by a logging filter, so every log
line emitted while handling a request carries the same `req=<id>` — the thread
that ties a stdout line to its `usage_events` / `error_events` row.

Design mirrors `log_task_event` in app/models.py: logging must never break a
caller, so failures here are swallowed.
"""
import contextvars
import logging
import sys
import uuid

# Set by UsageTrackingMiddleware per request; read by _RequestIdFilter.
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# Marker so reconfigure removes only OUR handler, never Modal's or a test's.
_HANDLER_FLAG = "_searchops_handler"
_configured = False


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def configure_logging(level: str | None = None, structured: bool | None = None) -> None:
    """Install the structured root handler. Idempotent."""
    global _configured
    try:
        from app import config
        lvl = (level or getattr(config, "LOG_LEVEL", "INFO") or "INFO").upper()
        use_structured = getattr(config, "LOG_STRUCTURED", True) if structured is None else structured

        root = logging.getLogger()
        root.setLevel(getattr(logging, lvl, logging.INFO))

        # Drop a prior SearchOps handler on reconfigure; leave foreign handlers alone.
        for h in list(root.handlers):
            if getattr(h, _HANDLER_FLAG, False):
                root.removeHandler(h)

        handler = logging.StreamHandler(sys.stdout)
        setattr(handler, _HANDLER_FLAG, True)
        handler.addFilter(_RequestIdFilter())
        if use_structured:
            fmt = "%(asctime)s %(levelname)s %(name)s req=%(request_id)s %(message)s"
        else:
            fmt = "%(levelname)s %(name)s %(message)s"
        handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%dT%H:%M:%S"))
        root.addHandler(handler)

        # Quiet chatty third-party INFO logs that would otherwise flood stdout now
        # that the root logger emits at INFO (httpx logs every request, etc.).
        for noisy in ("httpx", "httpcore", "urllib3", "google", "google_genai", "PIL"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _configured = True
    except Exception:  # never let logging setup break startup
        pass


def get_logger(name: str) -> logging.Logger:
    if not _configured:
        configure_logging()
    return logging.getLogger(name)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def set_request_id(rid: str):
    """Set the correlation id for the current context; returns a reset token."""
    return _request_id.set(rid)


def reset_request_id(token) -> None:
    try:
        _request_id.reset(token)
    except (ValueError, LookupError):
        pass


def current_request_id() -> str:
    return _request_id.get()
