"""Logging setup.

Locally: readable single-line text. On Cloud Run (``runtime.log_json = true``):
one JSON object per line with a ``severity`` field, which Cloud Logging parses
into proper log levels without an agent.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import override

from chartlens_core.config import RuntimeConfig


class JsonFormatter(logging.Formatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "severity": record.levelname,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(runtime: RuntimeConfig) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if runtime.log_json
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(runtime.log_level)
