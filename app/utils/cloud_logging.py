"""Structured logging for Google Cloud Run.

Cloud Logging parses JSON written to stdout/stderr: a `severity` field
sets the log level and `message` becomes the entry text. Locally (no
K_SERVICE env var) we keep human-readable plain logs.
"""
import json
import logging
import os
import sys


class CloudRunJsonFormatter(logging.Formatter):
    """Formats records as single-line JSON that Cloud Logging understands."""

    LEVEL_TO_SEVERITY = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": self.LEVEL_TO_SEVERITY.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logging.googleapis.com/sourceLocation": {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
            "logger": record.name,
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def running_on_cloud_run() -> bool:
    return bool(os.getenv("K_SERVICE"))


def setup_logging() -> None:
    """Install JSON logging on Cloud Run, plain logging elsewhere."""
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

    handler = logging.StreamHandler(sys.stdout)
    if running_on_cloud_run() or os.getenv("LOG_FORMAT", "").lower() == "json":
        handler.setFormatter(CloudRunJsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(levelname)s:%(name)s:%(message)s")
        )

    root.handlers.clear()
    root.addHandler(handler)

    # uvicorn installs its own handlers; route them through ours so
    # access/error logs are also structured in Cloud Logging.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
