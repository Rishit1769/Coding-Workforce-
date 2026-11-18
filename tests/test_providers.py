"""Adapter contract tests using fake CLI binaries (no subscription usage)."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from workforce.config import Role
from workforce.providers.base import ErrorKind, ExecutionRequest, classify_error, extract_json
from workforce.providers.claude_code import ClaudeCodeAdapter
from workforce.providers.codex import CodexAdapter
from workforce.providers.opencode import OpenCodeAdapter, permission_policy
from workforce.providers.registry import ProviderRegistry
from workforce.providers.scripted import ScriptedAdapter


def fake_bin(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def req(tmp_path, **kw):
    base = dict(role="developer_1", model="m", prompt="do it", cwd=tmp_path)
    base.update(kw)
    return ExecutionRequest(**base)


async def test_claude_success_and_structured(tmp_path):
    out = {"result": '{"a":1}', "is_error": False, "structured_output": {"a": 1}, "session_id": "s1",
           "usage": {"input_tokens": 3, "output_tokens": 4}, "total_cost_usd": 0.01}
    b = fake_bin(tmp_path, "claude", f"cat >/dev/null; echo '{json.dumps(out)}'")
    res = await ClaudeCodeAdapter(b).run(req(tmp_path, output_schema={"type": "object"}))
    assert res.ok and res.structured == {"a": 1} and res.session_id == "s1"
    assert res.usage["estimated_list_cost_usd"] == 0.01


async def test_claude_flags_are_safe(tmp_path):
    argv, stdin, _ = ClaudeCodeAdapter("claude").build_command(
        req(tmp_path, access="workspace_write", allowed_commands=["pytest"]), {})
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    tools = argv[argv.index("--allowedTools") + 1]
    assert "Edit" in tools and "Bash(pytest *)" in tools and "Bash(*)" not in tools
    argv_ro, _, _ = ClaudeCodeAdapter("claude").build_command(req(tmp_path), {})
    assert "Edit" not in argv_ro[argv_ro.index("--allowedTools") + 1]
    assert stdin == "do it"


async def test_claude_auth_error(tmp_path):
    out = {"result": "Not logged in. Please run /login", "is_error": True}
    b = fake_bin(tmp_path, "claude", f"cat >/dev/null; echo '{json.dumps(out)}'; exit 1")
    res = await ClaudeCodeAdapter(b).run(req(tmp_path))
    assert not res.ok and res.error_kind == ErrorKind.AUTH


async def test_codex_jsonl_parsing(tmp_path):
    events = [{"type": "thread.started", "thread_id": "th"},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}},
              {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}]
    lines = "\n".join(json.dumps(e) for e in events)
    # emulate `-o <file>` by writing the last message to the path after -o
    body = f'''cat >/dev/null
while [ $# -gt 0 ]; do if [ "$1" = "-o" ]; then echo -n hello > "$2"; fi; shift; done
cat <<'EOF'
{lines}
EOF
'''
    b = fake_bin(tmp_path, "codex", body)
    res = await CodexAdapter(b).run(req(tmp_path, reasoning_effort="medium"))
    assert res.ok and res.text == "hello" and res.session_id == "th" and res.usage["input_tokens"] == 10


def test_codex_sandbox_flags(tmp_path):
    argv, _, _ = CodexAdapter("codex").build_command(req(tmp_path, access="workspace_write", reasoning_effort="medium"), {})
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert "model_reasoning_effort=medium" in argv
    assert not any("danger" in a for a in argv)
    argv, _, _ = CodexAdapter("codex").build_command(req(tmp_path), {})
    assert argv[argv.index("-s") + 1] == "read-only"


async def test_opencode_region_error_is_model_unavailable(tmp_path):
    ev = {"type": "error", "sessionID": "x", "error": {"name": "APIError", "data": {
        "message": "This OpenCode Go model is hosted outside Europe and the United States."}}}
    b = fake_bin(tmp_path, "opencode", f"echo '{json.dumps(ev)}'; exit 1")
    res = await OpenCodeAdapter(b).run(req(tmp_path))
    assert not res.ok and res.error_kind == ErrorKind.MODEL_UNAVAILABLE


async def test_opencode_text_and_usage(tmp_path):
    evs = [{"type": "text", "sessionID": "s", "part": {"text": "PONG"}},
           {"type": "step_finish", "sessionID": "s", "part": {"tokens": {"input": 5, "output": 1}, "cost": 0}}]
    b = fake_bin(tmp_path, "opencode", "cat <<'EOF'\n" + "\n".join(json.dumps(e) for e in evs) + "\nEOF\n")
    res = await OpenCodeAdapter(b).run(req(tmp_path))
    assert res.ok and res.text == "PONG" and res.usage["input_tokens"] == 5 and res.session_id == "s"


def test_opencode_permission_policy(tmp_path):
    pol = permission_policy(req(tmp_path, allowed_commands=["pytest"]))
    assert pol["permission"]["edit"] == "deny"
    assert pol["permission"]["bash"]["*"] == "deny" and pol["permission"]["bash"]["pytest *"] == "allow"


async def test_timeout_kills_process_group(tmp_path):
    marker = tmp_path / "child.pid"
    b = fake_bin(tmp_path, "claude", f"sleep 30 & echo $! > {marker}; wait")
    t0 = time.monotonic()
    res = await ClaudeCodeAdapter(b).run(req(tmp_path, timeout_s=1))
    assert res.error_kind == ErrorKind.TIMEOUT and time.monotonic() - t0 < 10
    pid = int(marker.read_text())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_missing_binary(tmp_path):
    res = await CodexAdapter(str(tmp_path / "nope")).run(req(tmp_path))
    assert res.error_kind == ErrorKind.NOT_INSTALLED


def test_classify_error():
    assert classify_error("Error: 429 Too Many Requests") == ErrorKind.USAGE_LIMIT
    assert classify_error("getaddrinfo ENOTFOUND api") == ErrorKind.NETWORK
    assert classify_error("model gpt-x does not exist") == ErrorKind.MODEL_UNAVAILABLE
    assert classify_error("weird") == ErrorKind.PROVIDER_ERROR


def test_extract_json():
    assert extract_json('blah ```json\n{"a": 1}\n``` tail') == {"a": 1}
    assert extract_json('prefix {"a": {"b": 2}} suffix') == {"a": {"b": 2}}
    assert extract_json("nothing") is None


async def test_registry_breaker_and_role_disable(wf, tmp_path):
    from workforce.providers.base import ExecutionResult
    sc = ScriptedAdapter()
    calls = {"n": 0}

    def flaky(r):
        calls["n"] += 1
        return ExecutionResult(ok=False, error_kind=ErrorKind.NETWORK, error="ECONNRESET")

    sc.on("*", flaky)
    for role in Role:
        wf.cfg.roles[role].provider = "scripted"
    wf.cfg.providers["scripted"].max_retries = 0
    reg = ProviderRegistry(wf.cfg, wf.db, adapters={"scripted": sc})
    for _ in range(3):
        await reg.execute(Role.WORKER, "x", tmp_path, purpose="t")
    res = await reg.execute(Role.WORKER, "x", tmp_path, purpose="t")
    assert res.error_kind == ErrorKind.CIRCUIT_OPEN and calls["n"] == 3

    reg.reset_breaker("scripted")
    sc.on("*", lambda r: ExecutionResult(ok=False, error_kind=ErrorKind.MODEL_UNAVAILABLE, error="no such model"))
    await reg.execute(Role.TESTER, "x", tmp_path, purpose="t")
    ok, why = reg.role_available(Role.TESTER)
    assert not ok and "model_unavailable" in why


async def test_registry_budget(wf, tmp_path):
    from workforce.providers.base import ExecutionResult
    sc = ScriptedAdapter({"*": lambda r: ExecutionResult(ok=True, text="ok")})
    wf.cfg.roles[Role.WORKER].provider = "scripted"
    wf.cfg.providers["scripted"].max_calls_per_hour = 2
    reg = ProviderRegistry(wf.cfg, wf.db, adapters={"scripted": sc})
    assert (await reg.execute(Role.WORKER, "x", tmp_path, purpose="t")).ok
    assert (await reg.execute(Role.WORKER, "x", tmp_path, purpose="t")).ok
    assert (await reg.execute(Role.WORKER, "x", tmp_path, purpose="t")).error_kind == ErrorKind.BUDGET
