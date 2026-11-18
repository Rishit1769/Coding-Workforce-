"""OpenCode adapter: `opencode run --format json` with an inline permission policy."""

from __future__ import annotations

import json
from pathlib import Path

from workforce.providers.base import ExecutionRequest, ExecutionResult, ProviderAdapter, classify_error


def permission_policy(req: ExecutionRequest) -> dict:
    bash: dict[str, str] = {"*": "deny"}
    for c in req.allowed_commands:
        bash[c] = "allow"
        bash[f"{c} *"] = "allow"
    return {
        "permission": {
            "edit": "allow" if req.access == "workspace_write" else "deny",
            "bash": bash,
            "webfetch": "deny",
            "external_directory": "deny",
        }
    }


class OpenCodeAdapter(ProviderAdapter):
    name = "opencode"

    def build_command(self, req: ExecutionRequest, ctx: dict):
        prompt = req.prompt if not req.system_prompt else f"{req.system_prompt}\n\n{req.prompt}"
        if req.output_schema:
            prompt += ("\n\nRespond with ONLY a single JSON object (no prose, no code fences) that validates "
                       f"against this JSON Schema:\n{json.dumps(req.output_schema)}")
        argv = [self.binary, "run", "--format", "json", "-m", req.model, "--dir", str(req.cwd)]
        if req.reasoning_effort:
            argv += ["--variant", req.reasoning_effort]
        if req.session_id:
            argv += ["-s", req.session_id]
        argv += ["--", prompt]
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps(permission_policy(req))}
        return argv, None, env

    def parse(self, req, stdout, stderr, exit_code, ctx) -> ExecutionResult:
        texts: list[str] = []
        errors: list[str] = []
        usage = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0}
        session_id = None
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = ev.get("sessionID") or session_id
            t = ev.get("type")
            part = ev.get("part") or {}
            if t == "text":
                texts.append(part.get("text", ""))
            elif t == "step_finish":
                tok = part.get("tokens") or {}
                usage["input_tokens"] += tok.get("input", 0) or 0
                usage["output_tokens"] += tok.get("output", 0) or 0
                usage["reasoning_tokens"] += tok.get("reasoning", 0) or 0
                usage["cost_usd"] += part.get("cost", 0) or 0
            elif t == "error":
                err = ev.get("error") or {}
                errors.append(((err.get("data") or {}).get("message") or err.get("name") or json.dumps(err))[:2000])
        text = "".join(texts).strip()
        if errors or exit_code != 0 or (not text and not usage["output_tokens"]):
            blob = "\n".join(errors) + "\n" + stderr[-3000:]
            return ExecutionResult(ok=False, text=text, error_kind=classify_error(blob),
                                   error=blob.strip() or f"exit {exit_code}, no output", usage=usage,
                                   session_id=session_id)
        return ExecutionResult(ok=True, text=text, usage=usage, session_id=session_id)
