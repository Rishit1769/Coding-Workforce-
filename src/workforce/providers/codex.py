"""Codex CLI adapter: `codex exec --json` with sandbox and reasoning effort."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from workforce.providers.base import ExecutionRequest, ExecutionResult, ProviderAdapter, classify_error


class CodexAdapter(ProviderAdapter):
    name = "codex"

    def build_command(self, req: ExecutionRequest, ctx: dict):
        tmp = Path(tempfile.mkdtemp(prefix="wf-codex-"))
        last = tmp / "last.txt"
        argv = [
            self.binary, "exec", "-",
            "-m", req.model,
            "-C", str(req.cwd),
            # The engine performs git commits itself; agents get a workspace sandbox only.
            "-s", "workspace-write" if req.access == "workspace_write" else "read-only",
            "--json", "--ephemeral", "--skip-git-repo-check", "--color", "never",
            "-o", str(last),
        ]
        if req.reasoning_effort:
            argv += ["-c", f"model_reasoning_effort={req.reasoning_effort}"]
        if req.output_schema:
            schema = tmp / "schema.json"
            schema.write_text(json.dumps(req.output_schema))
            argv += ["--output-schema", str(schema)]
        prompt = req.prompt if not req.system_prompt else f"{req.system_prompt}\n\n{req.prompt}"
        ctx["last"] = last
        ctx["cleanup"] = [tmp]
        return argv, prompt, {}

    def parse(self, req, stdout, stderr, exit_code, ctx) -> ExecutionResult:
        texts: list[str] = []
        errors: list[str] = []
        usage: dict = {}
        session_id = None
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "thread.started":
                session_id = ev.get("thread_id")
            elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                texts.append(ev["item"].get("text", ""))
            elif t == "turn.completed":
                u = ev.get("usage") or {}
                for k, v in u.items():
                    if isinstance(v, (int, float)):
                        usage[k] = usage.get(k, 0) + v
            elif t in ("error", "turn.failed"):
                errors.append(json.dumps(ev.get("error") or ev.get("message") or ev)[:2000])
        last = ctx.get("last")
        text = last.read_text() if last and last.exists() else (texts[-1] if texts else "")
        if exit_code != 0 or errors:
            blob = "\n".join(errors) + "\n" + stderr[-3000:]
            return ExecutionResult(ok=False, text=text, error_kind=classify_error(blob), error=blob.strip(),
                                   usage=usage, session_id=session_id)
        structured = None
        if req.output_schema:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                structured = None
        return ExecutionResult(ok=True, text=text, structured=structured, usage=usage, session_id=session_id)
