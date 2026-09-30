"""Logging for every ChartLens process (API, pipeline jobs).

Locally: readable single-line text. With ``runtime.log_json = true`` (Cloud Run,
GitHub Actions jobs): one JSON object per line with a ``severity`` field, which
Cloud Logging parses into log levels without an agent.

Structured fields are passed with ``log_event``::

    log_event(log, "source.stored", trading_date="2024-01-10", source_hash=sha, bytes=n)

They appear as top-level keys in JSON mode and as ``key=value`` pairs in text mode.
Never pass whole datasets as fields — counts and identifiers only.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, override

from chartlens_core.config import RuntimeConfig

_FIELDS = "chartlens_fields"


class JsonFormatter(logging.Formatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "severity": record.levelname,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(getattr(record, _FIELDS, {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    @override
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields: dict[str, object] = getattr(record, _FIELDS, {})
        if not fields:
            return base
        return base + " " + " ".join(f"{k}={v}" for k, v in fields.items())


def configure_logging(runtime: RuntimeConfig) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if runtime.log_json else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(runtime.log_level)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log ``event`` with structured ``fields`` (see module docstring)."""
    logger.log(level, event, extra={_FIELDS: fields})
