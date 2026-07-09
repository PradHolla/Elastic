from __future__ import annotations

import json
import logging
import os
import socket
import sys
import uuid
from datetime import datetime, timezone

# Attributes present on every LogRecord; anything else was passed via extra={}
# and belongs in the structured payload.
_STANDARD_LOG_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_ATTRS
        }
        suffix = " ".join(f"{key}={value}" for key, value in extras.items())
        base = f"[{record.name}] {record.getMessage()}"
        if record.exc_info:
            suffix = f"{suffix} {self.formatException(record.exc_info)}".strip()
        return f"{base} {suffix}".rstrip()


def setup_logging(*, json_logs: bool = False, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(getattr(handler, "_elastic_handler", False) for handler in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter() if json_logs else ConsoleLogFormatter())
    handler._elastic_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def default_worker_id() -> str:
    # In Kubernetes ELASTIC_WORKER_ID is the pod name; locally we fall back to
    # hostname plus a random suffix so two local workers never share a lease id.
    configured = os.environ.get("ELASTIC_WORKER_ID")
    if configured:
        return configured
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
