"""Structured logging for Google Cloud Run / Cloud Logging.

Cloud Logging automatically parses JSON written to stdout and promotes the
``severity`` and ``message`` fields. Emitting one JSON object per line gives us
correctly-levelled, searchable logs without any agent sidecar.

Falls back to plain text locally (set ``LOG_FORMAT=text``) for readable dev output.
"""
from __future__ import annotations

import json
import logging
import os
import sys

# Python logging level name -> Cloud Logging severity.
_LEVEL_TO_SEVERITY = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


class CloudLoggingFormatter(logging.Formatter):
    """Render log records as single-line JSON understood by Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": _LEVEL_TO_SEVERITY.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    """Install the appropriate handler on the root logger (idempotent)."""
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    use_text = os.getenv("LOG_FORMAT", "json").lower() == "text"

    handler = logging.StreamHandler(sys.stdout)
    if use_text:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    else:
        handler.setFormatter(CloudLoggingFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn ships its own handlers; route them through ours for consistent output.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
