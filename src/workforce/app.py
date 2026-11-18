"""Application container wiring configuration, database and logging."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from workforce.config import WorkforceConfig, ensure_dirs, load_config
from workforce.db.session import Database
from workforce.logging import setup_logging


@dataclass
class App:
    cfg: WorkforceConfig
    db: Database


def build_app(config_path: str | Path | None = None, cfg: WorkforceConfig | None = None, stderr_logs: bool = True,
              configure_logging: bool = True) -> App:
    """`configure_logging=False` is used for per-project apps: logging is process-wide and owned by the
    central app; project records are routed to the project's own log directory."""
    cfg = cfg or load_config(config_path)
    ensure_dirs(cfg)
    if configure_logging:
        setup_logging(cfg.log_dir, stderr=stderr_logs)
    return App(cfg=cfg, db=Database(cfg.db_path))
