"""Structured logging.

One JSON object per line on stdout, every record carrying the correlation ID of
the request that produced it. A single `grep <correlation_id>` reconstructs a
whole request across routing, retrieval, the model call, and persistence.

Secrets are redacted by key name in the formatter itself rather than at call
sites, because the call site you forget is exactly the one that leaks the key.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

# Propagated through the whole request, including background tasks.
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)
session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None
)

# Substring match against the lowercased key name.
_REDACT_HINTS = ("key", "secret", "token", "password", "authorization", "credential")
_REDACTED = "***redacted***"

# Attributes present on every LogRecord; anything else is treated as structured extra.
_STD_ATTRS = frozenset(
    """name msg args levelname levelno pathname filename module exc_info exc_text
    stack_info lineno funcName created msecs relativeCreated thread threadName
    processName process taskName getMessage message asctime""".split()
)


def _should_redact(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _REDACT_HINTS)


def _scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secret-looking keys and make values JSON-safe."""
    if _depth > 6:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _should_redact(str(k)) else _scrub(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_scrub(v, _depth + 1) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }

        session_id = session_id_var.get()
        if session_id:
            payload["session_id"] = session_id

        for key, value in record.__dict__.items():
            if key in _STD_ATTRS or key.startswith("_"):
                continue
            payload[key] = _REDACTED if _should_redact(key) else _scrub(value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for local development (LOG_FORMAT=console)."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STD_ATTRS and not k.startswith("_")
        }
        tail = ""
        if extras:
            tail = "  " + " ".join(
                f"{k}={_REDACTED if _should_redact(k) else _scrub(v)}"
                for k, v in extras.items()
            )
        cid = correlation_id_var.get()
        base = (
            f"{time.strftime('%H:%M:%S', time.localtime(record.created))} "
            f"{record.levelname:<7} [{cid[:8]}] {record.name}: {record.getMessage()}{tail}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install the root handler. Idempotent, so tests may call it freely."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root.addHandler(handler)
    root.setLevel(level)

    # These are noisy at INFO and duplicate our own request logging.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def set_correlation_id(value: str | None = None) -> str:
    cid = value or new_correlation_id()
    correlation_id_var.set(cid)
    return cid


def set_session_id(value: str | None) -> None:
    session_id_var.set(value)


@contextmanager
def timed(logger: logging.Logger, event: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a block and log its duration, whether it succeeds or raises.

        with timed(log, "rag.query", query=q) as span:
            span["hits"] = len(results)

    Failures are logged with the same duration field, so a slow failure is as
    visible in the logs as a slow success.
    """
    span: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        yield span
    except Exception as exc:
        logger.error(
            event,
            extra={
                **fields,
                **span,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "outcome": "error",
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        raise
    else:
        logger.info(
            event,
            extra={
                **fields,
                **span,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "outcome": "ok",
            },
        )
