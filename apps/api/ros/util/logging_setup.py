"""Structured (JSON) logging (A/C5).

Opt-in via `ROS_LOG_JSON`. When ON, the root logger emits one JSON object per line carrying
`ts/level/logger/msg` plus any structured `extra=` fields, so a log pipeline (Datadog / Loki /
ELK) can index them without regex. When OFF (the default), this is a NO-OP: local dev keeps
human-readable logs and pytest/uvicorn keep their own handlers untouched.
"""

from __future__ import annotations

import json
import logging
import sys

# LogRecord's built-in attributes - everything else on the record was passed via `extra=`
# and is worth emitting as a structured field.
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
}


def _jsonable(value: object) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object. Unknown/extra attributes are included
    verbatim when JSON-serializable, else stringified, so a bad `extra=` value can never crash
    the logging call."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value if _jsonable(value) else str(value)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Install JSON logging on the root logger when `ROS_LOG_JSON` is set; otherwise no-op.

    Idempotent: replaces the root handlers with a single stdout JSON handler, so calling it
    repeatedly (e.g. per `create_app()` in tests) does not stack duplicate handlers.
    """
    from ros.config import settings

    if not settings.log_json:
        return
    root = logging.getLogger()
    root.setLevel(getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
