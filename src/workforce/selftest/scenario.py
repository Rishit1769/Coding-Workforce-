"""Self-test scenario: a small documented sample project and deterministic
scripted agents with injectable faults. Used by the E2E test-suite and by
`workforce selftest`. Scripted agents are never used unless a role is
explicitly configured with `provider: scripted`."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

from workforce.providers.base import ErrorKind, ExecutionRequest, ExecutionResult
from workforce.providers.scripted import ScriptedAdapter

PRD = """# textstats - Product Requirements

## Overview
textstats is a tiny Python library for text statistics.

## Functional Requirements
- FR-1: `textstats.counting.word_count(text)` must return the number of whitespace-separated words.
  - an empty or whitespace-only string returns 0
- FR-2: `textstats.counting.char_count(text, include_spaces=True)` must return the number of characters; when include_spaces is False, whitespace characters are not counted.
- FR-3: `textstats.ranking.top_words(text, n)` must return the n most frequent lowercase words as (word, count) tuples ordered by count descending then word ascending.
  - n must be a positive integer; otherwise ValueError is raised
"""

PLAN = {
    "summary": "Two independent modules: counting (FR-1, FR-2) and ranking (FR-3).",
    "decisions": [
        {"key": "ADR-001", "title": "Pure-function stdlib modules", "context": "Small library, no I/O.",
         "decision": "Implement textstats.counting and textstats.ranking as pure functions using only the stdlib.",
         "consequences": "No dependencies; trivially testable.", "requirement_keys": ["FR-1", "FR-2", "FR-3"]},
    ],
    "tasks": [
        {"key": "T-001", "title": "Counting functions",
         "description": "Create textstats/counting.py with word_count(text: str) -> int and "
                        "char_count(text: str, include_spaces: bool = True) -> int.",
         "requirement_keys": ["FR-1", "FR-2"], "decision_keys": ["ADR-001"], "depends_on": [],
         "owned_paths": ["textstats/counting.py"], "acceptance_criteria": ["word_count('') == 0"],
         "preferred_role": "developer_1"},
        {"key": "T-002", "title": "Ranking function",
         "description": "Create textstats/ranking.py with top_words(text: str, n: int) -> list[tuple[str, int]].",
         "requirement_keys": ["FR-3"], "decision_keys": ["ADR-001"], "depends_on": [],
         "owned_paths": ["textstats/ranking.py"], "acceptance_criteria": ["n <= 0 raises ValueError"],
         "preferred_role": "developer_2"},
    ],
    "issues": [], "risks": ["none significant"],
}

ACCEPT = {
    "T-001": ("test_t_001_counting.py", '''from textstats.counting import char_count, word_count


def test_word_count_basic():
    """Verifies FR-1."""
    assert word_count("a quick  brown fox") == 4


def test_word_count_empty_boundary():
    """Verifies FR-1 (boundary: empty/whitespace)."""
    assert word_count("") == 0
    assert word_count("   \\n\\t") == 0


def test_char_count():
    """Verifies FR-2."""
    assert char_count("a b") == 3
    assert char_count("a b", include_spaces=False) == 2
'''),
    "T-002": ("test_t_002_ranking.py", '''from textstats.ranking import top_words


def test_top_words_order():
    """Verifies FR-3."""
    assert top_words("b a B c a b", 2) == [("b", 3), ("a", 2)]
'''),
}

IMPL = {
    "T-001": ("textstats/counting.py", '''def word_count(text: str) -> int:
    return len(text.split())


def char_count(text: str, include_spaces: bool = True) -> int:
    if include_spaces:
        return len(text)
    return sum(1 for c in text if not c.isspace())
'''),
    "T-002": ("textstats/ranking.py", '''from collections import Counter


def top_words(text: str, n: int) -> list[tuple[str, int]]:
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        raise ValueError("n must be a positive integer")
    counts = Counter(w.lower() for w in text.split())
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
'''),
}

BUGGY_T001 = '''def word_count(text: str) -> int:
    return len(text.split(" "))


def char_count(text: str, include_spaces: bool = True) -> int:
    return len(text)
'''

NO_VALIDATION_T002 = '''from collections import Counter


def top_words(text, n):
    counts = Counter(w.lower() for w in text.split())
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
'''


def create_sample_project(root: Path) -> tuple[Path, Path]:
    repo = root / "textstats-repo"
    repo.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    run("config", "user.email", "owner@example.invalid")
    run("config", "user.name", "Sample Owner")
    (repo / "textstats").mkdir()
    (repo / "textstats" / "__init__.py").write_text('"""textstats sample library."""\n')
    (repo / "tests").mkdir()
    (repo / "tests" / "test_package.py").write_text("import textstats\n\n\ndef test_importable():\n    assert textstats\n")
    (repo / "requirements").mkdir()
    (repo / "requirements" / "prd.md").write_text(PRD)
    (repo / "README.md").write_text("# textstats\nSample project for the Coding Workforce self-test.\n")
    run("add", ".")
    run("commit", "-qm", "chore: initial sample project")
    return repo, repo / "requirements"


def _ok(data: dict) -> ExecutionResult:
    return ExecutionResult(ok=True, text=json.dumps(data), structured=data)


def _task_key(req: ExecutionRequest) -> str:
    for k in ("T-001", "T-002"):
        if f"task {k}" in req.prompt or f"Implement task {k}" in req.prompt:
            return k
    return "T-001"


def build_adapter(faults: dict | None = None) -> ScriptedAdapter:
    """faults keys:
      buggy_first:      T-001 first implementation fails acceptance tests
      violate_req:      T-002 first implementation omits FR-3 validation (reviewer must catch)
      arch_violation:   T-001 first implementation edits a file outside its owned paths
      conflict:         both developers add a conflicting tests/test_unit.py
      devops_unsafe:    DevOps declines to resolve conflicts (forces developer correction)
      dev_timeout:      first developer call hangs past its timeout
      reviewer_down:    reviewer fails with network errors N times before recovering
      tester_unavailable: tester model unavailable (like an un-entitled model)
      always_buggy:     T-001 is always buggy (bounded correction loop -> BLOCKED)
      slow_dev:         developer sleeps this many seconds (for interruption tests)
    """
    f = dict(faults or {})
    state = {"dev_calls": {}, "reviewer_failures": 0, "timeouts": 0}
    a = ScriptedAdapter()

    a.on("architect", lambda r: _ok(PLAN))
    a.on("worker:explore", lambda r: _ok({"summary": "python package textstats", "languages": ["python"],
                                          "entry_points": [], "test_layout": "pytest", "dependencies": []}))
    a.on("worker:classify_log", lambda r: _ok({"category": "assertion_error", "summary": "assertion failed"}))
    a.on("manager", lambda r: _ok({"summary": "start both", "assignments": [
        {"task_key": "T-001", "role": "developer_1", "rationale": "preferred"},
        {"task_key": "T-002", "role": "developer_2", "rationale": "preferred"}]}))

    def tester(r: ExecutionRequest):
        if f.get("tester_unavailable"):
            return ExecutionResult(ok=False, error_kind=ErrorKind.MODEL_UNAVAILABLE,
                                   error="model is not available for this account")
        key = _task_key(r)
        name, body = ACCEPT[key]
        d = r.cwd / "acceptance_tests" / key.lower().replace("-", "_")
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body)
        return _ok({"summary": f"acceptance tests for {key}", "tests": [
            {"file": f"{d.relative_to(r.cwd)}/{name}", "test_name": "all", "requirement_keys": ["FR-1"], "kind": "conformance"}],
            "untestable_requirements": []})
    a.on("tester", tester)

    async def developer(r: ExecutionRequest):
        key = _task_key(r)
        n = state["dev_calls"][key] = state["dev_calls"].get(key, 0) + 1
        if f.get("slow_dev"):
            await asyncio.sleep(float(f["slow_dev"]))
        if f.get("dev_timeout") and state["timeouts"] == 0:
            state["timeouts"] += 1
            await asyncio.sleep(r.timeout_s + 5)
        path, body = IMPL[key]
        if key == "T-001" and (f.get("always_buggy") or (f.get("buggy_first") and n == 1)):
            body = BUGGY_T001
        if key == "T-002" and f.get("violate_req") and n == 1:
            body = NO_VALIDATION_T002
        (r.cwd / path).parent.mkdir(parents=True, exist_ok=True)
        (r.cwd / path).write_text(body)
        if key == "T-001" and f.get("arch_violation") and n == 1:
            (r.cwd / "README.md").write_text("# hijacked by developer\n")
        if f.get("conflict"):
            tf = r.cwd / "tests" / "test_unit.py"
            if tf.exists() and "<<<<<<<" in tf.read_text():
                tf.write_text(_merged_unit_tests())
            elif not tf.exists() or n == 1:
                tf.write_text(_unit_test(key))
        return _ok({"summary": f"implemented {key}", "files_changed": [path], "requirement_keys_addressed": [],
                    "tests_added": [], "notes": ""})
    a.on("developer_1", developer)
    a.on("developer_2", developer)

    def reviewer(r: ExecutionRequest):
        if f.get("reviewer_down") and state["reviewer_failures"] < int(f["reviewer_down"]):
            state["reviewer_failures"] += 1
            return ExecutionResult(ok=False, error_kind=ErrorKind.NETWORK, error="ECONNRESET")
        key = _task_key(r)
        if key == "T-002":
            impl = (r.cwd / "textstats" / "ranking.py")
            if impl.exists() and "ValueError" not in impl.read_text():
                return _ok({"verdict": "CHANGES_REQUESTED", "summary": "FR-3 requires ValueError for n <= 0",
                            "findings": [{"severity": "major", "category": "acceptance_criteria",
                                          "requirement_keys": ["FR-3"], "file": "textstats/ranking.py", "line": 4,
                                          "evidence": "top_words('a', 0) returns [] instead of raising ValueError",
                                          "recommendation": "validate n and raise ValueError"}],
                            "requirement_coverage": [{"requirement_key": "FR-3", "satisfied": False,
                                                      "evidence": "no validation of n"}]})
        keys = ["FR-1", "FR-2"] if key == "T-001" else ["FR-3"]
        return _ok({"verdict": "APPROVED", "summary": f"{key} satisfies {keys}", "findings": [],
                    "requirement_coverage": [{"requirement_key": k, "satisfied": True, "evidence": "tests + code"}
                                             for k in keys]})
    a.on("reviewer", reviewer)

    def devops(r: ExecutionRequest):
        if r.purpose == "readiness":
            return _ok({"summary": "ready", "ready": True, "risks": [], "recommendations": []})
        if f.get("devops_unsafe"):
            return _ok({"summary": "semantic conflict, not resolving", "resolved_files": [], "safe": False, "notes": ""})
        tf = r.cwd / "tests" / "test_unit.py"
        if tf.exists():
            tf.write_text(_merged_unit_tests())
        return _ok({"summary": "combined both unit test files", "resolved_files": ["tests/test_unit.py"], "safe": True,
                    "notes": ""})
    a.on("devops", devops)
    return a


def _unit_test(key: str) -> str:
    if key == "T-001":
        return "from textstats.counting import word_count\n\n\ndef test_wc():\n    assert word_count('a b') == 2\n"
    return "from textstats.ranking import top_words\n\n\ndef test_tw():\n    assert top_words('a a b', 1) == [('a', 2)]\n"


def _merged_unit_tests() -> str:
    return ("import importlib.util\n\n\ndef test_wc():\n    from textstats.counting import word_count\n"
            "    assert word_count('a b') == 2\n\n\ndef test_tw():\n"
            "    if importlib.util.find_spec('textstats.ranking') is None:\n        return\n"
            "    from textstats.ranking import top_words\n    assert top_words('a a b', 1) == [('a', 2)]\n")


def factory_from_env() -> ScriptedAdapter:
    """Entry point used by the CLI when roles are configured with provider: scripted."""
    return build_adapter(json.loads(os.environ.get("WORKFORCE_SELFTEST_FAULTS", "{}")))
