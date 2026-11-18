"""Claude Code adapter: `claude -p` with JSON output and scoped permissions."""

from __future__ import annotations

import json
from pathlib import Path

from workforce.providers.base import ErrorKind, ExecutionRequest, ExecutionResult, ProviderAdapter, classify_error

READ_TOOLS = ["Read", "Glob", "Grep"]
WRITE_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write"]


class ClaudeCodeAdapter(ProviderAdapter):
    name = "claude_code"

    def build_command(self, req: ExecutionRequest, ctx: dict):
        tools = list(WRITE_TOOLS if req.access == "workspace_write" else READ_TOOLS)
        tools += [f"Bash({c} *)" for c in req.allowed_commands] + [f"Bash({c})" for c in req.allowed_commands]
        argv = [
            self.binary, "-p",
            "--model", req.model,
            "--output-format", "json",
            "--no-session-persistence",
            # dontAsk: anything not explicitly allowed is denied (never bypassed).
            "--permission-mode", "dontAsk",
            "--allowedTools", ",".join(tools),
            "--disallowedTools", "WebFetch,WebSearch",
            "--strict-mcp-config",
            "--disable-slash-commands",
        ]
        if req.reasoning_effort:
            argv += ["--effort", req.reasoning_effort]
        if req.system_prompt:
            argv += ["--append-system-prompt", req.system_prompt]
        if req.output_schema:
            argv += ["--json-schema", json.dumps(req.output_schema)]
        return argv, req.prompt, {}

    def parse(self, req, stdout, stderr, exit_code, ctx) -> ExecutionResult:
        data = None
        try:
            data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
        except (json.JSONDecodeError, IndexError):
            data = None
        if not isinstance(data, dict):
            blob = (stdout + "\n" + stderr)[-4000:]
            return ExecutionResult(ok=False, error_kind=classify_error(blob), error=blob.strip() or f"exit {exit_code}")
        usage = {
            "input_tokens": (data.get("usage") or {}).get("input_tokens"),
            "output_tokens": (data.get("usage") or {}).get("output_tokens"),
            "cache_read_input_tokens": (data.get("usage") or {}).get("cache_read_input_tokens"),
            "cache_creation_input_tokens": (data.get("usage") or {}).get("cache_creation_input_tokens"),
            # Claude Code reports a list-price estimate; subscription plans are not billed per call.
            "estimated_list_cost_usd": data.get("total_cost_usd"),
            "num_turns": data.get("num_turns"),
            "permission_denials": len(data.get("permission_denials") or []),
        }
        text = data.get("result") or ""
        if data.get("is_error") or exit_code != 0:
            blob = f"{text}\n{stderr}"[-4000:]
            return ExecutionResult(ok=False, text=text, error_kind=classify_error(blob), error=blob.strip(),
                                   usage=usage, session_id=data.get("session_id"))
        return ExecutionResult(ok=True, text=text, structured=data.get("structured_output"), usage=usage,
                               session_id=data.get("session_id"))


__all__ = ["ClaudeCodeAdapter", "ErrorKind"]
