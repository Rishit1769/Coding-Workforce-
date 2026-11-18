"""Structured JSON logging with mandatory secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from workforce.security.redaction import redact, redact_obj

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = redact_obj(v) if isinstance(v, (str, dict, list)) else v
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def setup_logging(log_dir: Path | None = None, level: str = "INFO", stderr: bool = True) -> None:
    root = logging.getLogger("workforce")
    root.setLevel(level)
    root.handlers.clear()
    root.propagate = False
    fmt = JsonFormatter()
    if stderr:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        h.setLevel("WARNING")
        root.addHandler(h)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "workforce.jsonl")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"workforce.{name}")
