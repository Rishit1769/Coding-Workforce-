"""Shared provider execution contract.

Every adapter turns an ExecutionRequest into a non-interactive CLI invocation
(no shell, isolated cwd, own process group) and returns an ExecutionResult with
a classified error kind. Adapters never pass permission-bypass flags."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from workforce.security.redaction import redact

FORBIDDEN_FLAGS = (
    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "bypassPermissions",
    "--dangerously-bypass-approvals-and-sandbox", "danger-full-access", "--auto", "--approve-for-me",
    "--dangerously-bypass-hook-trust",
)


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"
    AUTH = "auth"
    USAGE_LIMIT = "usage_limit"
    NETWORK = "network"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROVIDER_ERROR = "provider_error"
    CANCELLED = "cancelled"
    INVALID_OUTPUT = "invalid_output"
    CIRCUIT_OPEN = "circuit_open"
    BUDGET = "budget"
    NOT_INSTALLED = "not_installed"


TRANSIENT = {ErrorKind.NETWORK, ErrorKind.PROVIDER_ERROR, ErrorKind.TIMEOUT, ErrorKind.INVALID_OUTPUT}


class ExecutionRequest(BaseModel):
    role: str
    model: str
    prompt: str
    cwd: Path
    timeout_s: int = 1800
    reasoning_effort: str | None = None
    system_prompt: str | None = None
    output_schema: dict[str, Any] | None = None
    access: Literal["read_only", "workspace_write"] = "read_only"
    # Shell commands the agent may run itself (provider-level permission rules).
    allowed_commands: list[str] = Field(default_factory=list)
    session_id: str | None = None
    purpose: str = ""


class ExecutionResult(BaseModel):
    ok: bool
    text: str = ""
    structured: dict[str, Any] | None = None
    error_kind: ErrorKind | None = None
    error: str = ""
    exit_code: int | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    duration_s: float = 0.0
    command: list[str] = Field(default_factory=list)


_CLASSIFIERS: list[tuple[ErrorKind, re.Pattern[str]]] = [
    (ErrorKind.MODEL_UNAVAILABLE, re.compile(
        r"model[^\n]{0,40}(not found|does not exist|not supported|unavailable|not available)|invalid model|unknown model|"
        r"hosted outside|ProviderModelNotFound|model_not_found", re.I)),
    (ErrorKind.USAGE_LIMIT, re.compile(
        r"usage limit|rate[ _-]?limit|quota|too many requests|\b429\b|limit reached|credit balance|"
        r"out of credits|try again (at|in)|exceeded your", re.I)),
    (ErrorKind.AUTH, re.compile(
        r"not logged in|please (run )?/?login|authenticat(e|ion) (failed|required|error)|unauthori[sz]ed|\b401\b|"
        r"invalid api key|token (has )?expired|login required|sign in", re.I)),
    (ErrorKind.NETWORK, re.compile(
        r"ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|getaddrinfo|network (error|is unreachable)|"
        r"connection (reset|refused|error)|failed to connect|stream disconnected|\b50[234]\b", re.I)),
]


def classify_error(text: str) -> ErrorKind:
    for kind, pat in _CLASSIFIERS:
        if pat.search(text or ""):
            return kind
    return ErrorKind.PROVIDER_ERROR


def extract_json(text: str) -> dict | None:
    """Best-effort extraction of a JSON object from model text."""
    if not text:
        return None
    text = text.strip()
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    for m in re.finditer(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start: i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


class ProviderAdapter(ABC):
    name: str = "base"

    def __init__(self, binary: str):
        self.binary = binary

    @abstractmethod
    def build_command(self, req: ExecutionRequest, ctx: dict) -> tuple[list[str], str | None, dict[str, str]]:
        """Return (argv, stdin_text, extra_env). `ctx` is per-call scratch state shared with parse()."""

    @abstractmethod
    def parse(self, req: ExecutionRequest, stdout: str, stderr: str, exit_code: int, ctx: dict) -> ExecutionResult:
        ...

    async def version(self) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(self.binary, "--version", stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
            return out.decode().strip().splitlines()[0] if out else None
        except (FileNotFoundError, asyncio.TimeoutError):
            return None

    async def run(self, req: ExecutionRequest, on_start: Callable[[int], None] | None = None) -> ExecutionResult:
        ctx: dict = {}
        argv, stdin_text, extra_env = self.build_command(req, ctx)
        for flag in FORBIDDEN_FLAGS:
            if any(a == flag or a.endswith(f"={flag}") for a in argv):
                raise RuntimeError(f"adapter attempted to use forbidden flag {flag}")
        env = {**os.environ, **extra_env}
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(req.cwd), env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError as e:
            return ExecutionResult(ok=False, error_kind=ErrorKind.NOT_INSTALLED, error=str(e), command=_safe_argv(argv))
        if on_start:
            on_start(proc.pid)
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin_text.encode() if stdin_text is not None else None), req.timeout_s)
        except asyncio.TimeoutError:
            await _kill_group(proc)
            return ExecutionResult(ok=False, error_kind=ErrorKind.TIMEOUT, error=f"timed out after {req.timeout_s}s",
                                   duration_s=time.monotonic() - t0, command=_safe_argv(argv))
        except asyncio.CancelledError:
            await _kill_group(proc)
            raise
        try:
            res = self.parse(req, out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode, ctx)
        finally:
            for d in ctx.get("cleanup", []):
                shutil.rmtree(d, ignore_errors=True)
        res.duration_s = time.monotonic() - t0
        res.exit_code = proc.returncode
        res.command = _safe_argv(argv)
        res.text = redact(res.text)
        res.error = redact(res.error)
        if res.ok and req.output_schema is not None and res.structured is None:
            res.structured = extract_json(res.text)
            if res.structured is None:
                res.ok = False
                res.error_kind = ErrorKind.INVALID_OUTPUT
                res.error = "expected structured JSON output but none could be parsed"
        return res


def _safe_argv(argv: list[str]) -> list[str]:
    return [a if len(a) < 200 else a[:80] + f"...<{len(a)} chars>" for a in argv]


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 5)
            return
        except asyncio.TimeoutError:
            continue
