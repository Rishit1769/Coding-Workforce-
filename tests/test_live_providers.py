"""Live compatibility tests against the real, authenticated CLIs.

Opt-in: WORKFORCE_LIVE=1 pytest -m live tests/test_live_providers.py
Each test sends one tiny prompt and therefore consumes a little subscription usage."""

from __future__ import annotations

import os

import pytest

from workforce.providers.base import ExecutionRequest
from workforce.providers.claude_code import ClaudeCodeAdapter
from workforce.providers.codex import CodexAdapter
from workforce.providers.opencode import OpenCodeAdapter

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("WORKFORCE_LIVE") != "1",
                                                   reason="set WORKFORCE_LIVE=1 to run live provider tests")]

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
          "additionalProperties": False}


def _req(tmp_path, model, **kw):
    return ExecutionRequest(role="probe", model=model, prompt="Set answer to the word PONG.", cwd=tmp_path,
                            timeout_s=240, output_schema=SCHEMA, **kw)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5"])
async def test_claude_live(tmp_path, model):
    res = await ClaudeCodeAdapter("claude").run(_req(tmp_path, model))
    assert res.ok, res.error
    assert res.structured["answer"].upper() == "PONG"


@pytest.mark.parametrize("model,effort", [("gpt-6.1-sol", "medium"), ("gpt-5.6-sol", "low"), ("gpt-6-sol", "low")])
async def test_codex_live(tmp_path, model, effort):
    res = await CodexAdapter("codex").run(_req(tmp_path, model, reasoning_effort=effort))
    assert res.ok, res.error
    assert res.structured["answer"].upper() == "PONG"


async def test_opencode_go_deepseek_live(tmp_path):
    res = await OpenCodeAdapter("opencode").run(_req(tmp_path, "opencode-go/deepseek-v4.1-flash"))
    if not res.ok:
        pytest.xfail(f"OpenCode Go DeepSeek V4.1 Flash unavailable for this account: {res.error_kind}: {res.error[:200]}")
    assert "PONG" in res.text.upper()
