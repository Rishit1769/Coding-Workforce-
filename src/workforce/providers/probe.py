"""Model compatibility verification: probes every configured role with a
minimal prompt through its real CLI and records availability."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from workforce.config import Role
from workforce.providers.registry import ProviderRegistry

PROBE_PROMPT = "Reply with exactly the single word: PONG"


async def verify_roles(reg: ProviderRegistry, roles: list[Role] | None = None) -> list[dict]:
    roles = roles or list(Role)
    versions = {name: await a.version() for name, a in reg.adapters.items()}
    cache: dict[tuple[str, str, str | None], tuple[bool, str, dict]] = {}
    report = []
    scratch = Path(tempfile.mkdtemp(prefix="wf-probe-"))

    async def probe(role: Role):
        rc = reg.cfg.role(role)
        key = (rc.provider, rc.model, rc.reasoning_effort)
        if not rc.enabled:
            return role, False, "role disabled in configuration", {}
        if key not in cache:
            reg.reset_breaker(rc.provider)
            # Clear any previous "unavailable" verdict so the probe actually runs.
            reg.set_role_status(role, True, "probing")
            res = await reg.execute(role, PROBE_PROMPT, scratch, purpose="probe", timeout_s=180)
            ok = res.ok and "PONG" in res.text.upper()
            detail = "verified" if ok else f"{res.error_kind}: {res.error[:400] or res.text[:200]}"
            cache[key] = (ok, detail, res.usage)
        ok, detail, usage = cache[key]
        return role, ok, detail, usage

    results = await asyncio.gather(*(probe(r) for r in roles))
    for role, ok, detail, usage in results:
        rc = reg.cfg.role(role)
        reg.set_role_status(role, ok, detail)
        report.append({
            "role": role.value, "desired_model": rc.desired_model, "plan": rc.plan, "provider": rc.provider,
            "model_id": rc.model, "reasoning_effort": rc.reasoning_effort, "available": ok, "detail": detail,
            "cli_version": versions.get(rc.provider), "override_reason": rc.override_reason,
        })
    out = reg.cfg.artifact_dir / "model_compatibility.json"
    out.write_text(json.dumps(report, indent=2))
    return report
