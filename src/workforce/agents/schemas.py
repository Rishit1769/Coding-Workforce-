"""Strict JSON schemas for agent outputs (OpenAI strict-mode compatible:
every object lists all properties as required and forbids extras)."""

from __future__ import annotations


def obj(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


STR = {"type": "string"}
BOOL = {"type": "boolean"}
INT = {"type": "integer"}
STRS = arr(STR)


def enum(*vals: str) -> dict:
    return {"type": "string", "enum": list(vals)}


ARCHITECT = obj(
    summary=STR,
    decisions=arr(obj(key=STR, title=STR, context=STR, decision=STR, consequences=STR, requirement_keys=STRS)),
    tasks=arr(obj(key=STR, title=STR, description=STR, requirement_keys=STRS, decision_keys=STRS, depends_on=STRS,
                  owned_paths=STRS, acceptance_criteria=STRS, preferred_role=enum("developer_1", "developer_2"))),
    issues=arr(obj(kind=enum("ambiguity", "contradiction", "gap", "risk"), severity=enum("major", "minor"),
                   requirement_keys=STRS, description=STR)),
    risks=STRS,
)

MANAGER = obj(
    summary=STR,
    assignments=arr(obj(task_key=STR, role=enum("developer_1", "developer_2"), rationale=STR)),
)

DEVELOPER = obj(
    summary=STR,
    files_changed=STRS,
    requirement_keys_addressed=STRS,
    tests_added=STRS,
    notes=STR,
)

TESTER = obj(
    summary=STR,
    tests=arr(obj(file=STR, test_name=STR, requirement_keys=STRS,
                  kind=enum("unit", "integration", "e2e", "api_contract", "negative", "boundary", "security",
                            "regression", "migration", "conformance"))),
    untestable_requirements=arr(obj(requirement_key=STR, reason=STR)),
)

REVIEWER = obj(
    verdict=enum("APPROVED", "CHANGES_REQUESTED", "BLOCKED"),
    summary=STR,
    findings=arr(obj(
        severity=enum("critical", "major", "minor"),
        category=enum("correctness", "security", "maintainability", "architecture", "api_contract", "schema",
                      "traceability", "error_handling", "tests", "undocumented_behavior", "complexity",
                      "acceptance_criteria"),
        requirement_keys=STRS, file=STR, line=INT, evidence=STR, recommendation=STR)),
    requirement_coverage=arr(obj(requirement_key=STR, satisfied=BOOL, evidence=STR)),
)

DEVOPS_MERGE = obj(summary=STR, resolved_files=STRS, safe=BOOL, notes=STR)

DEVOPS_READINESS = obj(summary=STR, ready=BOOL, risks=STRS, recommendations=STRS)

WORKER_EXPLORE = obj(summary=STR, languages=STRS, entry_points=STRS, test_layout=STR, dependencies=STRS)

WORKER_CLASSIFY = obj(
    category=enum("test_failure", "assertion_error", "syntax_error", "import_error", "timeout", "environment",
                  "flaky", "conflict", "unknown"),
    summary=STR,
)
