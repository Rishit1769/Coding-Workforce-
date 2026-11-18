"""Secret redaction applied to every log line, stored output and API response."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), "[REDACTED:anthropic-key]"),
    (re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), "[REDACTED:openai-key]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws-key]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED:slack-token]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED:jwt]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED:private-key]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9_\-\.=]{16,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|(?:access|refresh|auth|id)?[_-]?token|client[_-]?secret|secret|password|passwd|session[_-]?cookie)\s*[\"']?\s*[:=]\s*[\"']?)([^\s\"',;]{6,})"),
     r"\1[REDACTED]"),
]


def redact(text: str | None) -> str:
    if not text:
        return text or ""
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact_obj(obj):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj
