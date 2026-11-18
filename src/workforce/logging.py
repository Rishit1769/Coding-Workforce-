"""Structured JSON logging with mandatory secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from workforce.security.redaction import redact, redact_obj

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


# The project a log record belongs to (set per orchestrator / asyncio task context).
_current_project: contextvars.ContextVar[str | None] = contextvars.ContextVar("workforce_project", default=None)
_project_log_dirs: dict[str, Path] = {}


def set_log_project(name: str | None) -> contextvars.Token:
    return _current_project.set(name)


def reset_log_project(token: contextvars.Token) -> None:
    _current_project.reset(token)


def register_project_logs(name: str, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    _project_log_dirs[name] = log_dir


class _ProjectFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.project = _current_project.get()
        return True


class _CentralOnly(logging.Filter):
    """Records of a registered project go to that project's log, not the central one."""

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "project", None) not in _project_log_dirs


class ProjectLogRouter(logging.Handler):
    """Appends each project's records to <project home>/logs/workforce.jsonl."""

    def emit(self, record: logging.LogRecord) -> None:
        name = getattr(record, "project", None)
        d = _project_log_dirs.get(name) if name else None
        if d is None:
            return
        try:
            with (d / "workforce.jsonl").open("a") as fh:
                fh.write(self.format(record) + "\n")
        except Exception:  # pragma: no cover - logging must never crash the engine
            self.handleError(record)


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
    root.filters.clear()
    root.propagate = False
    fmt = JsonFormatter()
    tag = _ProjectFilter()  # handler-level: logger filters do not apply to child-logger records
    if stderr:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        h.setLevel("WARNING")
        h.addFilter(tag)
        root.addHandler(h)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "workforce.jsonl")
        fh.setFormatter(fmt)
        fh.addFilter(tag)
        fh.addFilter(_CentralOnly())
        root.addHandler(fh)
    router = ProjectLogRouter()
    router.setFormatter(fmt)
    router.addFilter(tag)
    root.addHandler(router)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"workforce.{name}")
