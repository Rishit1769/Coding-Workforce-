"""Provider registry: concurrency limits, hourly call budgets, retries,
persistent circuit breakers, role availability and execution records."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from workforce.config import Role, WorkforceConfig
from workforce.db.models import Execution, ProviderState, RoleStatus, utcnow
from workforce.db.session import Database
from workforce.engine.control import audit
from workforce.logging import get_logger
from workforce.providers.base import TRANSIENT, ErrorKind, ExecutionRequest, ExecutionResult, ProviderAdapter
from workforce.providers.claude_code import ClaudeCodeAdapter
from workforce.providers.codex import CodexAdapter
from workforce.providers.opencode import OpenCodeAdapter
from workforce.security.redaction import redact

log = get_logger("providers")

ROLE_DISABLING = {ErrorKind.MODEL_UNAVAILABLE, ErrorKind.AUTH, ErrorKind.NOT_INSTALLED}


def default_adapters(cfg: WorkforceConfig) -> dict[str, ProviderAdapter]:
    return {
        "claude_code": ClaudeCodeAdapter(cfg.providers["claude_code"].binary),
        "codex": CodexAdapter(cfg.providers["codex"].binary),
        "opencode": OpenCodeAdapter(cfg.providers["opencode"].binary),
    }


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class ProviderRegistry:
    def __init__(self, cfg: WorkforceConfig, db: Database, adapters: dict[str, ProviderAdapter] | None = None):
        self.cfg = cfg
        self.db = db
        self.adapters = adapters if adapters is not None else default_adapters(cfg)
        self._sems = {name: asyncio.Semaphore(pc.max_concurrency) for name, pc in cfg.providers.items()}
        self.running: dict[int, asyncio.Task] = {}

    # ---- availability -------------------------------------------------
    def role_available(self, role: Role | str) -> tuple[bool, str]:
        rc = self.cfg.role(role)
        if not rc.enabled:
            return False, "role disabled in configuration"
        if rc.provider not in self.adapters:
            return False, f"provider {rc.provider} not registered"
        with self.db.session() as s:
            st = s.get(RoleStatus, str(Role(role)))
            if st and st.model == rc.model and st.provider == rc.provider and not st.available:
                return False, st.detail
        ok, why = self._breaker_ok(rc.provider)
        if not ok:
            return False, why
        return True, ""

    def set_role_status(self, role: Role | str, available: bool, detail: str) -> None:
        rc = self.cfg.role(role)
        with self.db.session() as s:
            st = s.get(RoleStatus, str(Role(role)))
            if st is None:
                st = RoleStatus(role=str(Role(role)), provider=rc.provider, model=rc.model)
                s.add(st)
            st.provider, st.model = rc.provider, rc.model
            st.available, st.detail, st.verified_at = available, redact(detail)[:2000], utcnow()

    # ---- circuit breaker ----------------------------------------------
    def _breaker_ok(self, provider: str) -> tuple[bool, str]:
        with self.db.session() as s:
            ps = s.get(ProviderState, provider)
            if ps and ps.breaker == "open":
                until = _aware(ps.open_until)
                if until and until > datetime.now(UTC):
                    return False, f"circuit open until {until.isoformat()}: {ps.last_error or ''}"[:500]
        return True, ""

    def _record_outcome(self, provider: str, res: ExecutionResult) -> None:
        cb = self.cfg.providers[provider].circuit_breaker
        with self.db.session() as s:
            ps = s.get(ProviderState, provider) or ProviderState(provider=provider)
            s.add(ps)
            if res.ok:
                ps.consecutive_failures = 0
                ps.breaker = "closed"
                ps.open_until = None
                return
            if res.error_kind in (ErrorKind.CANCELLED, ErrorKind.INVALID_OUTPUT):
                return
            ps.consecutive_failures = (ps.consecutive_failures or 0) + 1
            ps.last_error = f"{res.error_kind}: {res.error[:300]}"
            if res.error_kind == ErrorKind.USAGE_LIMIT:
                ps.breaker, ps.open_until = "open", datetime.now(UTC) + timedelta(seconds=cb.usage_limit_cooldown_s)
            elif ps.consecutive_failures >= cb.failure_threshold:
                ps.breaker, ps.open_until = "open", datetime.now(UTC) + timedelta(seconds=cb.cooldown_s)
            if ps.breaker == "open":
                audit(s, "engine", "provider.circuit_open", {"provider": provider, "error": ps.last_error})

    def reset_breaker(self, provider: str) -> None:
        with self.db.session() as s:
            ps = s.get(ProviderState, provider)
            if ps:
                ps.breaker, ps.open_until, ps.consecutive_failures = "closed", None, 0

    def _calls_last_hour(self, provider: str) -> int:
        since = datetime.now(UTC) - timedelta(hours=1)
        with self.db.session() as s:
            return s.scalar(select(func.count()).select_from(Execution).where(
                Execution.provider == provider, Execution.started_at >= since)) or 0

    # ---- execution ----------------------------------------------------
    async def execute(self, role: Role | str, prompt: str, cwd, *, purpose: str, task_id: int | None = None,
                      project_id: int | None = None, access: str = "read_only", output_schema: dict | None = None,
                      allowed_commands: list[str] | None = None, system_prompt: str | None = None,
                      timeout_s: int | None = None) -> ExecutionResult:
        role = Role(role)
        rc = self.cfg.role(role)
        pc = self.cfg.providers[rc.provider]
        ok, why = self.role_available(role)
        if not ok:
            kind = ErrorKind.CIRCUIT_OPEN if why.startswith("circuit open") else ErrorKind.MODEL_UNAVAILABLE
            return ExecutionResult(ok=False, error_kind=kind, error=f"role {role} unavailable: {why}")
        if self._calls_last_hour(rc.provider) >= pc.max_calls_per_hour:
            return ExecutionResult(ok=False, error_kind=ErrorKind.BUDGET,
                                   error=f"{rc.provider} hourly call budget ({pc.max_calls_per_hour}) exhausted")
        req = ExecutionRequest(role=role.value, model=rc.model, prompt=prompt, cwd=cwd, purpose=purpose,
                               timeout_s=timeout_s or rc.timeout_s or pc.default_timeout_s,
                               reasoning_effort=rc.reasoning_effort, output_schema=output_schema, access=access,
                               allowed_commands=allowed_commands or [], system_prompt=system_prompt)
        adapter = self.adapters[rc.provider]
        attempts = 1 + max(0, pc.max_retries)
        res: ExecutionResult | None = None
        for attempt in range(attempts):
            res = await self._run_once(adapter, rc.provider, req, role, task_id, project_id)
            self._record_outcome(rc.provider, res)
            if res.ok or res.error_kind not in TRANSIENT or res.error_kind == ErrorKind.TIMEOUT:
                break
            await asyncio.sleep(min(30, 2 ** attempt))
        assert res is not None
        if res.error_kind in ROLE_DISABLING:
            self.set_role_status(role, False, f"{res.error_kind}: {res.error[:500]}")
        return res

    async def _run_once(self, adapter, provider, req, role, task_id, project_id) -> ExecutionResult:
        def on_start(pid: int) -> None:
            with self.db.session() as s2:
                s2.get(Execution, ex_id).pid = pid

        # The execution record starts when the provider slot is acquired, so queueing time is not run time.
        async with self._sems[provider]:
            with self.db.session() as s:
                ex = Execution(task_id=task_id, project_id=project_id, role=role.value, purpose=req.purpose,
                               provider=provider, model=req.model, cwd=str(req.cwd))
                s.add(ex)
                s.flush()
                ex_id = ex.id
            try:
                res = await adapter.run(req, on_start=on_start)
            except asyncio.CancelledError:
                res = ExecutionResult(ok=False, error_kind=ErrorKind.CANCELLED, error="cancelled")
                self._finish(ex_id, res)
                raise
            except Exception as e:  # adapter bug or forbidden flag
                log.exception("adapter crashed")
                res = ExecutionResult(ok=False, error_kind=ErrorKind.PROVIDER_ERROR, error=f"adapter error: {e}")
        self._finish(ex_id, res)
        log.info("execution finished", extra={"execution_id": ex_id, "role": role.value, "provider": provider,
                                               "ok": res.ok, "error_kind": res.error_kind})
        return res

    def _finish(self, ex_id: int, res: ExecutionResult) -> None:
        with self.db.session() as s:
            ex = s.get(Execution, ex_id)
            ex.status = ("succeeded" if res.ok else
                         "timeout" if res.error_kind == ErrorKind.TIMEOUT else
                         "cancelled" if res.error_kind == ErrorKind.CANCELLED else "failed")
            ex.error_kind = res.error_kind.value if res.error_kind else None
            ex.error = redact(res.error)[:8000] if res.error else None
            ex.exit_code = res.exit_code
            ex.usage = res.usage
            ex.session_id = res.session_id
            ex.output = redact(res.text)[:20000]
            ex.finished_at = utcnow()
            ex.duration_s = res.duration_s


def mark_interrupted_executions(db: Database) -> int:
    """On startup: executions left 'running' by a crashed engine are marked interrupted."""
    with db.session() as s:
        rows = s.scalars(select(Execution).where(Execution.status == "running")).all()
        for ex in rows:
            ex.status = "interrupted"
            ex.error_kind = "interrupted"
            ex.finished_at = utcnow()
        return len(rows)
