"""Deterministic in-process provider used for tests, fault injection and the
self-test pipeline. It is never selected unless a role is explicitly
configured with provider: scripted."""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from workforce.providers.base import ErrorKind, ExecutionRequest, ExecutionResult, ProviderAdapter

Handler = Callable[[ExecutionRequest], "ExecutionResult | Awaitable[ExecutionResult]"]


class ScriptedAdapter(ProviderAdapter):
    name = "scripted"

    def __init__(self, handlers: dict[str, Handler] | None = None):
        super().__init__(binary="")
        # Keys: "<role>:<purpose>", "<role>", or "*"
        self.handlers: dict[str, Handler] = dict(handlers or {})
        self.calls: list[ExecutionRequest] = []

    def on(self, key: str, handler: Handler) -> None:
        self.handlers[key] = handler

    def build_command(self, req, ctx):  # pragma: no cover - not used
        return [], None, {}

    def parse(self, req, stdout, stderr, exit_code, ctx):  # pragma: no cover - not used
        raise NotImplementedError

    async def version(self) -> str | None:
        return "scripted-1"

    async def run(self, req: ExecutionRequest, on_start=None) -> ExecutionResult:
        self.calls.append(req)
        handler = (self.handlers.get(f"{req.role}:{req.purpose}") or self.handlers.get(req.role)
                   or self.handlers.get("*"))
        t0 = time.monotonic()
        if handler is None:
            return ExecutionResult(ok=False, error_kind=ErrorKind.PROVIDER_ERROR,
                                   error=f"no scripted handler for {req.role}:{req.purpose}")
        try:
            out = handler(req)
            if asyncio.iscoroutine(out):
                out = await asyncio.wait_for(out, req.timeout_s)
        except asyncio.TimeoutError:
            return ExecutionResult(ok=False, error_kind=ErrorKind.TIMEOUT, error=f"timed out after {req.timeout_s}s",
                                   duration_s=time.monotonic() - t0)
        out.duration_s = time.monotonic() - t0
        return out
