"""Agent role implementations. Each method builds a role-specific prompt,
invokes the role's configured provider through the registry, and validates the
structured output. Agents propose; the engine decides."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from workforce.agents import schemas
from workforce.config import Role, WorkforceConfig
from workforce.providers.base import ExecutionResult
from workforce.providers.registry import ProviderRegistry
from workforce.security.injection import UNTRUSTED_PREAMBLE, wrap_untrusted

COMMON_RULES = """
Operating rules (enforced by the orchestration engine; violations are rejected automatically):
- Never run git commands, never commit, push, reset or delete branches. The engine owns version control.
- Never modify files under: {protected}.
- Never read, print or copy credentials, tokens, .env files or auth files.
- Never invent requirements. Work only from the approved requirements given below; if something is unclear, say so.
- Do not use network access or install packages unless the task explicitly requires it.
""".strip()


@dataclass
class AgentOutcome:
    ok: bool
    data: dict | None
    result: ExecutionResult
    error: str = ""


def _reqs_block(reqs: list[dict]) -> str:
    return wrap_untrusted("approved-requirements", json.dumps(reqs, indent=2))


class Agents:
    def __init__(self, cfg: WorkforceConfig, registry: ProviderRegistry):
        self.cfg = cfg
        self.reg = registry

    def _rules(self) -> str:
        return COMMON_RULES.format(protected=", ".join(self.cfg.policy.protected_paths))

    async def _call(self, role: Role, prompt: str, cwd: Path, schema: dict, purpose: str, *, access="read_only",
                    allowed_commands=None, task_id=None, project_id=None) -> AgentOutcome:
        system = f"You are the {role.value.replace('_', ' ')} in an autonomous software engineering workforce.\n" \
                 f"{UNTRUSTED_PREAMBLE}\n{self._rules()}"
        res = await self.reg.execute(role, prompt, cwd, purpose=purpose, task_id=task_id, project_id=project_id,
                                     access=access, output_schema=schema, allowed_commands=allowed_commands,
                                     system_prompt=system)
        if not res.ok:
            return AgentOutcome(False, None, res, f"{res.error_kind}: {res.error[:800]}")
        data = res.structured
        missing = [k for k in schema["required"] if not isinstance(data, dict) or k not in data]
        if missing:
            return AgentOutcome(False, data, res, f"invalid_output: missing fields {missing}")
        return AgentOutcome(True, data, res)

    # ---- Architect ------------------------------------------------------
    async def architect(self, project_id: int, repo: Path, reqs: list[dict], repo_summary: str,
                        feedback: str = "") -> AgentOutcome:
        prompt = f"""Design the technical architecture and implementation plan for the approved requirements.

{_reqs_block(reqs)}

Repository context (read-only exploration is allowed in the current directory):
{wrap_untrusted("repository-summary", repo_summary)}

Produce:
1. Architecture decision records (keys ADR-001, ADR-002, ...) covering module boundaries, API and database contracts,
   implementation patterns and dependencies. Each decision must cite the requirement keys it serves.
2. Implementation tasks (keys T-001, T-002, ...). Every task must:
   - cite at least one requirement key from the list above (never invent requirements),
   - declare `owned_paths`: repo-relative glob patterns of the files it may create/modify (keep tasks that run in
     parallel on disjoint paths; never include acceptance_tests/** or requirements/**),
   - define the exact public interface (module paths, function/class signatures, HTTP routes, schemas) in its
     description so independent testers can write acceptance tests before implementation exists,
   - list `depends_on` task keys and concrete acceptance criteria,
   - set preferred_role developer_1 or developer_2 to balance work.
   Every approved requirement must be covered by at least one task. Prefer few, cohesive tasks; avoid unnecessary
   complexity.
3. Issues: ambiguities, contradictions or gaps you find that need a human decision (severity major blocks work).
4. Technical risks.
{f"Previous plan was rejected by the engine for these reasons; fix them:{chr(10)}{feedback}" if feedback else ""}"""
        return await self._call(Role.ARCHITECT, prompt, repo, schemas.ARCHITECT, "architecture", project_id=project_id)

    # ---- Manager --------------------------------------------------------
    async def manager(self, project_id: int, cwd: Path, ready: list[dict], free_roles: list[str],
                      active: list[dict]) -> AgentOutcome:
        prompt = f"""Choose which READY tasks to start next and which developer should take each.
Free developer roles: {free_roles}. Tasks already in progress (do not overlap their owned paths): {json.dumps(active)}
READY tasks: {json.dumps(ready)}
Developer profiles: developer_1 = {self.cfg.role(Role.DEVELOPER_1).desired_model}; developer_2 = {self.cfg.role(Role.DEVELOPER_2).desired_model}.
Prefer tasks that unblock the most downstream work. Return at most {len(free_roles)} assignments."""
        return await self._call(Role.MANAGER, prompt, cwd, schemas.MANAGER, "scheduling", project_id=project_id)

    # ---- Developer ------------------------------------------------------
    async def developer(self, role: Role, task: dict, reqs: list[dict], decisions: list[dict], worktree: Path,
                        feedback: list[dict], test_command: str, task_id: int, project_id: int) -> AgentOutcome:
        fb = ""
        if feedback:
            fb = "This is a correction attempt. Fix every item of structured feedback below (evidence is from the " \
                 "independent tester/reviewer/policy engine):\n" + wrap_untrusted("verification-feedback", json.dumps(feedback[-3:], indent=2))
        prompt = f"""Implement task {task['key']}: {task['title']}

Task description and interface contract:
{task['description']}

Acceptance criteria: {json.dumps(task['acceptance_criteria'])}
You may only create or modify files matching: {json.dumps(task['owned_paths'])} (plus unit tests for them under tests/).
Architecture decisions to follow:
{wrap_untrusted("architecture-decisions", json.dumps(decisions, indent=2))}
{_reqs_block(reqs)}
{fb}

Work in the current directory. Write clean, minimal, well-structured code with unit tests. You may run the test
suite with `{test_command}`. Hidden acceptance tests written independently from the requirements will be run
against your implementation, so implement the documented behaviour exactly (including error cases and limits).
When done, report a summary and the files you changed."""
        allowed = _test_prefixes(test_command)
        return await self._call(role, prompt, worktree, schemas.DEVELOPER, "implement", access="workspace_write",
                                allowed_commands=allowed, task_id=task_id, project_id=project_id)

    # ---- Tester ---------------------------------------------------------
    async def tester(self, task: dict, reqs: list[dict], decisions: list[dict], worktree: Path, test_dir: str,
                     task_id: int, project_id: int) -> AgentOutcome:
        prompt = f"""Write independent acceptance tests for task {task['key']}: {task['title']}.

The implementation does NOT exist yet. Derive tests ONLY from the documented requirements, acceptance criteria and
the interface contract below - never from implementation details.

Interface contract / task description:
{task['description']}
Acceptance criteria: {json.dumps(task['acceptance_criteria'])}
{wrap_untrusted("architecture-decisions", json.dumps(decisions, indent=2))}
{_reqs_block(reqs)}

Write pytest tests ONLY under `{test_dir}/` (create the directory). Name every file
`test_{test_dir.rsplit('/', 1)[-1]}_<topic>.py` so module names are unique across the whole repository, and do
not add __init__.py files; imports resolve from the repository root. Cover: positive behaviour, negative cases, boundary values,
API contract details (status codes, fields), security-relevant behaviour and error handling, as applicable.
Put the requirement key(s) each test verifies in the test's docstring, e.g. \"\"\"Verifies FR-1.\"\"\".
Tests must be deterministic and must not use the network. Report every test you wrote."""
        return await self._call(Role.TESTER, prompt, worktree, schemas.TESTER, "acceptance_tests",
                                access="workspace_write", allowed_commands=["python -m pytest --collect-only"],
                                task_id=task_id, project_id=project_id)

    # ---- Reviewer -------------------------------------------------------
    async def reviewer(self, task: dict, reqs: list[dict], decisions: list[dict], worktree: Path, diff: str,
                       evidence: dict, policy_findings: dict, task_id: int, project_id: int) -> AgentOutcome:
        prompt = f"""Independently review the implementation of task {task['key']}: {task['title']}.

Task contract: {task['description']}
Acceptance criteria: {json.dumps(task['acceptance_criteria'])}
Owned paths: {json.dumps(task['owned_paths'])}
{wrap_untrusted("architecture-decisions", json.dumps(decisions, indent=2))}
{_reqs_block(reqs)}

Diff under review:
{wrap_untrusted("diff", diff)}

Verification evidence (independent acceptance tests + regression suite):
{wrap_untrusted("test-evidence", json.dumps(evidence, indent=2))}
Policy engine observations: {json.dumps(policy_findings)}

Verify correctness, security, maintainability, architecture compliance, API contract and database schema
compliance, requirement traceability, error handling, test completeness, undocumented behaviour, unnecessary
complexity and missing acceptance criteria. Passing tests alone are NOT sufficient for approval.
Verdict: APPROVED only if every cited requirement is satisfied; CHANGES_REQUESTED with actionable findings
(each with evidence and requirement keys) if fixes are needed; BLOCKED if the work cannot proceed without a human
decision. Fill requirement_coverage for every requirement key the task cites. Use line 0 when not applicable.
You may read files in the current directory to inspect the full implementation."""
        return await self._call(Role.REVIEWER, prompt, worktree, schemas.REVIEWER, "review",
                                task_id=task_id, project_id=project_id)

    # ---- DevOps ---------------------------------------------------------
    async def devops_merge(self, worktree: Path, conflicts: list[str], ours: str, theirs: str, task_id: int,
                           project_id: int) -> AgentOutcome:
        prompt = f"""A merge in the current directory stopped with conflicts in: {json.dumps(conflicts)}.
Side A (integration branch) context: {ours}
Side B (incoming task) context: {theirs}
Resolve the conflict markers ONLY in the listed files so that BOTH sides' intended behaviour is preserved.
Do not modify any other file. If the conflict is semantic (the two sides cannot both be right), do not guess:
leave the files unchanged and return safe=false with an explanation."""
        return await self._call(Role.DEVOPS, prompt, worktree, schemas.DEVOPS_MERGE, "merge_conflict",
                                access="workspace_write", task_id=task_id, project_id=project_id)

    async def devops_readiness(self, worktree: Path, evidence: dict, project_id: int) -> AgentOutcome:
        prompt = f"""Prepare a deployment readiness assessment for the integration branch in the current directory.
Evidence gathered by the engine:
{wrap_untrusted("readiness-evidence", json.dumps(evidence, indent=2))}
Assess build validity, regression results, migrations and environment configuration. This is a LOCAL staging
assessment only; nothing will be deployed to production."""
        return await self._call(Role.DEVOPS, prompt, worktree, schemas.DEVOPS_READINESS, "readiness",
                                project_id=project_id)

    # ---- Lightweight worker --------------------------------------------
    async def worker_explore(self, repo: Path, project_id: int) -> AgentOutcome:
        prompt = ("Explore the repository in the current directory (read-only) and summarise its structure, "
                  "languages, entry points, test layout and dependencies. Do not make architectural decisions.")
        return await self._call(Role.WORKER, prompt, repo, schemas.WORKER_EXPLORE, "explore", project_id=project_id)

    async def worker_classify(self, cwd: Path, log_text: str, task_id: int, project_id: int) -> AgentOutcome:
        prompt = "Classify this failure log and summarise the root cause in one or two sentences.\n" + \
                 wrap_untrusted("failure-log", log_text[-6000:])
        return await self._call(Role.WORKER, prompt, cwd, schemas.WORKER_CLASSIFY, "classify_log",
                                task_id=task_id, project_id=project_id)


def _test_prefixes(test_command: str) -> list[str]:
    parts = test_command.split()
    prefixes = {" ".join(parts[:3]) if parts[:2] == ["python", "-m"] else parts[0]}
    prefixes |= {"python -m pytest", "pytest"}
    return sorted(prefixes)
