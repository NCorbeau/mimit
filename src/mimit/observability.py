"""Explicit structured diagnostics with bounded, allowlisted operational fields.

Importing this module never configures logging. Runtime entry points install the
JSON handler; library callers and pytest can retain their own handlers.
"""

import json
import logging
import math
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from time import perf_counter
from typing import TextIO
from uuid import UUID

_IDS = {
    "job_id",
    "notification_id",
    "consumable_id",
    "household_id",
    "offer_source_id",
    "observation_id",
    "lease_owner",
}
_INTEGERS = {"update_id", "attempt", "attempts", "http_status", "body_bytes", "retry_after"}
_CODES = {"outcome", "error_code", "stage", "state", "previous_state", "worker"}
_CODE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{0,79}\Z")
_TELEGRAM_URL = re.compile(r"https?://api\.telegram\.org/bot[^/\s\"']+/?", re.IGNORECASE)
_DATABASE_URL = re.compile(r"postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^\s\"']+", re.IGNORECASE)
_TOKEN = re.compile(r"\b[0-9]+:[A-Za-z0-9_-]+\b")
_secrets: set[str] = set()


def _redact(value: str) -> str:
    value = _TELEGRAM_URL.sub("https://api.telegram.org/bot<redacted>/", value)
    value = _DATABASE_URL.sub("<database-url-redacted>", value)
    value = _TOKEN.sub("<token-redacted>", value)
    for secret in sorted(_secrets, key=len, reverse=True):
        value = value.replace(secret, "<redacted>")
    return value


def _safe_fields(fields: dict[str, object]) -> dict[str, str | int | float | bool | None]:
    safe: dict[str, str | int | float | bool | None] = {}
    for key, value in fields.items():
        if key in _IDS:
            if isinstance(value, UUID):
                safe[key] = str(value)
            elif isinstance(value, str):
                try:
                    safe[key] = str(UUID(value))
                except ValueError:
                    pass
        elif key in _INTEGERS and type(value) is int and abs(value) < 2**63:
            safe[key] = value
        elif key in _CODES:
            if value is None:
                safe[key] = None
            elif isinstance(value, str) and _CODE.fullmatch(value) and _redact(value) == value:
                safe[key] = value
        elif key == "duration_ms" and type(value) in (int, float):
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                safe[key] = round(value, 3)
        elif key == "acknowledged" and type(value) is bool:
            safe[key] = value
    return safe


def elapsed_ms(started: float) -> float:
    """Measure operation duration without using the injected domain clock."""
    return max(0.0, (perf_counter() - started) * 1000)


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Emit one bounded event; discard unrecognized fields and unsafe values.

    Fields never include message text, URLs, settings, payloads, arbitrary metadata or
    exception strings. IDs are UUIDs (update_id is an integer); codes are short enums.
    """
    if not _CODE.fullmatch(event) or _redact(event) != event:
        event = "invalid_operational_event"
    logging.getLogger("mimit.events").log(
        level, event, extra={"event_fields": _safe_fields(fields)}
    )


class SafeLogFilter(logging.Filter):
    """Protect ordinary library records before other handlers, including caplog."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(record.getMessage())
        record.args = ()
        # Exception/stack rendering may contain SQL values, URLs or message bodies.
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class JsonFormatter(logging.Formatter):
    """Never serialize arbitrary library messages or traceback contents."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "event_fields", None)
        event = record.getMessage() if isinstance(fields, dict) else "runtime_log"
        if not _CODE.fullmatch(event) or _redact(event) != event:
            event = "invalid_operational_event"
        result: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": event,
            "logger": record.name
            if _CODE.fullmatch(record.name) and _redact(record.name) == record.name
            else "runtime",
        }
        if isinstance(fields, dict):
            result.update(_safe_fields(fields))
        return json.dumps(result, ensure_ascii=True, allow_nan=False)


class _OperationalHandler(logging.StreamHandler[TextIO]):
    """Marker for idempotent runtime setup without replacing caller handlers."""


def install_log_redaction(*, secrets: Iterable[str] = ()) -> None:
    """Install redaction at dependency loggers without creating output handlers."""
    _secrets.update(secret for secret in secrets if secret)
    # Parent logger filters do not apply to propagated child records.
    for name in (
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "sqlalchemy.engine.Engine",
        "sqlalchemy.pool.impl.AsyncAdaptedQueuePool",
    ):
        logger = logging.getLogger(name)
        if not any(isinstance(item, SafeLogFilter) for item in logger.filters):
            logger.addFilter(SafeLogFilter())


def configure_logging(*, secrets: Iterable[str] = (), level: int = logging.INFO) -> None:
    """Add one JSON stderr handler; leave caller/test handlers available."""
    install_log_redaction(secrets=secrets)
    root = logging.getLogger()
    if not any(isinstance(handler, _OperationalHandler) for handler in root.handlers):
        handler = _OperationalHandler()
        handler.setFormatter(JsonFormatter())
        handler.addFilter(SafeLogFilter())
        root.addHandler(handler)
    # Uvicorn configures nonpropagating handlers before invoking the factory.
    # Its access/error messages can contain arbitrary paths and exception text.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for server_handler in logging.getLogger(name).handlers:
            server_handler.setFormatter(JsonFormatter())
            if not any(isinstance(item, SafeLogFilter) for item in server_handler.filters):
                server_handler.addFilter(SafeLogFilter())
    root.setLevel(level)
