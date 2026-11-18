"""Workforce configuration (Pydantic models loaded from YAML).

The official role -> model assignments live in DEFAULT_ROLES. They are *desired*
assignments: availability is verified at runtime by `workforce models verify` and
recorded in the database. Nothing in this module substitutes models.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class Role(StrEnum):
    MANAGER = "manager"
    ARCHITECT = "architect"
    DEVELOPER_1 = "developer_1"
    DEVELOPER_2 = "developer_2"
    TESTER = "tester"
    REVIEWER = "reviewer"
    DEVOPS = "devops"
    WORKER = "worker"


DEVELOPER_ROLES = (Role.DEVELOPER_1, Role.DEVELOPER_2)

ProviderName = Literal["claude_code", "codex", "opencode", "scripted"]


class RoleConfig(BaseModel):
    provider: ProviderName
    model: str
    desired_model: str = Field(description="Human-readable model the user asked for")
    plan: str = ""
    reasoning_effort: str | None = None
    timeout_s: int | None = None
    enabled: bool = True
    # An explicit, user-visible substitution. Never set automatically.
    override_reason: str | None = None


DEFAULT_ROLES: dict[Role, RoleConfig] = {
    Role.MANAGER: RoleConfig(provider="opencode", model="opencode-go/deepseek-v4.1-flash",
                             desired_model="DeepSeek V4.1 Flash", plan="OpenCode Go"),
    Role.ARCHITECT: RoleConfig(provider="claude_code", model="claude-opus-5-5",
                               desired_model="Claude Opus 5.5", plan="Claude Pro"),
    Role.DEVELOPER_1: RoleConfig(provider="codex", model="gpt-6.1-sol", reasoning_effort="medium",
                                 desired_model="GPT-6.1 Sol Medium", plan="ChatGPT Plus / Codex"),
    Role.DEVELOPER_2: RoleConfig(provider="claude_code", model="claude-sonnet-5-5",
                                 desired_model="Claude Sonnet 5.5", plan="Claude Pro"),
    Role.TESTER: RoleConfig(provider="opencode", model="opencode-go/deepseek-v4.1-flash",
                            desired_model="DeepSeek V4.1 Flash", plan="OpenCode Go"),
    Role.REVIEWER: RoleConfig(provider="codex", model="gpt-5.6-sol", reasoning_effort="high",
                              desired_model="GPT-5.6 Sol", plan="ChatGPT Plus / Codex"),
    Role.DEVOPS: RoleConfig(provider="codex", model="gpt-6-sol", reasoning_effort="medium",
                            desired_model="GPT-6 Sol", plan="ChatGPT Plus / Codex"),
    Role.WORKER: RoleConfig(provider="opencode", model="opencode-go/deepseek-v4.1-flash",
                            desired_model="DeepSeek V4.1 Flash", plan="OpenCode Go"),
}


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 3
    cooldown_s: int = 300
    usage_limit_cooldown_s: int = 3600


class ProviderConfig(BaseModel):
    binary: str
    max_concurrency: int = 2
    default_timeout_s: int = 1800
    max_retries: int = 1
    max_calls_per_hour: int = 60
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()


def _default_providers() -> dict[str, ProviderConfig]:
    return {
        "claude_code": ProviderConfig(binary="claude", max_concurrency=2, max_calls_per_hour=40),
        "codex": ProviderConfig(binary="codex", max_concurrency=2, max_calls_per_hour=40),
        "opencode": ProviderConfig(binary="opencode", max_concurrency=2, max_calls_per_hour=80),
        "scripted": ProviderConfig(binary="", max_concurrency=4, max_calls_per_hour=100000),
    }


class LimitsConfig(BaseModel):
    max_dev_concurrency: int = 2
    max_correction_attempts: int = 3
    task_wall_clock_s: int = 4 * 3600
    command_timeout_s: int = 900
    poll_interval_s: float = 2.0
    provider_wait_s: float = 60.0
    shutdown_grace_s: float = 60.0


class PolicyConfig(BaseModel):
    # argv[0] (plus optional argv[1]) prefixes the engine itself may execute in a worktree.
    allowed_commands: list[str] = Field(default_factory=lambda: [
        "python -m pytest", "pytest", "python -m compileall", "uv run pytest",
        "npm test", "npm run test", "npm run build", "npm run lint", "npx playwright test",
        "make test", "go test", "cargo test", "alembic upgrade", "alembic downgrade",
    ])
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master", "production", "release"])
    # Paths developers may never modify (relative to the target repo).
    protected_paths: list[str] = Field(default_factory=lambda: [
        ".workforce/**", "requirements/**", "acceptance_tests/**", ".git/**", ".github/workflows/**",
    ])
    secret_file_globs: list[str] = Field(default_factory=lambda: [
        ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "auth.json", "credentials*",
    ])


class GitConfig(BaseModel):
    integration_branch: str = "wf/integration"
    target_branch: str = "main"
    branch_prefix: str = "wf/task"
    push_authorized: bool = False
    remote: str = "origin"


class DashboardConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765

    @field_validator("host")
    @classmethod
    def _local_only(cls, v: str) -> str:
        if v not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("control API must bind to a loopback address")
        return v


class WorkforceConfig(BaseModel):
    home: Path = Path(".workforce")
    mode: Literal["supervised", "unattended"] = "supervised"
    require_architecture_approval: bool = True
    require_integration_approval: bool = True
    roles: dict[Role, RoleConfig] = Field(default_factory=lambda: {k: v.model_copy() for k, v in DEFAULT_ROLES.items()})
    providers: dict[str, ProviderConfig] = Field(default_factory=_default_providers)
    limits: LimitsConfig = LimitsConfig()
    policy: PolicyConfig = PolicyConfig()
    git: GitConfig = GitConfig()
    dashboard: DashboardConfig = DashboardConfig()

    @property
    def db_path(self) -> Path:
        return self.home / "workforce.db"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def worktree_dir(self) -> Path:
        return self.home / "worktrees"

    @property
    def artifact_dir(self) -> Path:
        return self.home / "artifacts"

    def role(self, role: Role | str) -> RoleConfig:
        return self.roles[Role(role)]


CONFIG_ENV = "WORKFORCE_CONFIG"
HOME_ENV = "WORKFORCE_HOME"


def load_config(path: Path | str | None = None) -> WorkforceConfig:
    """Load configuration; partial YAML files are merged over defaults."""
    path = Path(path or os.environ.get(CONFIG_ENV, "workforce.yaml"))
    data: dict = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    roles_raw = data.pop("roles", {}) or {}
    cfg = WorkforceConfig.model_validate(data)
    for name, raw in roles_raw.items():
        role = Role(name)
        base = cfg.roles[role].model_dump()
        base.update(raw or {})
        cfg.roles[role] = RoleConfig.model_validate(base)
    if os.environ.get(HOME_ENV):
        cfg.home = Path(os.environ[HOME_ENV])
    if not cfg.home.is_absolute():
        cfg.home = (path.parent.resolve() if path.exists() else Path.cwd()) / cfg.home
    return cfg


def ensure_dirs(cfg: WorkforceConfig) -> None:
    for d in (cfg.home, cfg.log_dir, cfg.worktree_dir, cfg.artifact_dir):
        d.mkdir(parents=True, exist_ok=True)
