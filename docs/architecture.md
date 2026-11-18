# Architecture

## Principles

1. **Agents propose, the engine decides.** LLM agents produce structured JSON (strict schemas). A
   deterministic engine validates every output and alone performs state transitions, git operations,
   test execution, integration and approvals. No agent can override an engine policy.
2. **Documents are data.** Ingested documents and repository content are wrapped in labelled
   untrusted-data delimiters and scanned for instruction-like content, which becomes an issue for the human.
3. **Nothing is invented.** Tasks must cite approved requirement keys; plans that cover unapproved or
   unknown requirements are rejected. Ambiguities are surfaced, never silently resolved.
4. **Everything is durable.** All state lives in SQLite (WAL, Alembic migrations). Each pipeline step is
   idempotent and resumable, so a crash never duplicates completed work.
5. **Boring infrastructure.** Python + asyncio + SQLite + git worktrees. No Redis, no queues, no containers.

## Components

| Package | Responsibility |
|---|---|
| `workforce.config` | Pydantic configuration, official role → model assignments, limits, policies |
| `workforce.db` | SQLAlchemy models (projects, documents, requirements, issues, decisions, tasks, events, executions, test runs, reviews, commits, approvals, audit log, role status, provider state); Alembic migrations in `workforce/migrations` |
| `workforce.providers` | Shared execution contract; Claude Code, Codex and OpenCode adapters; scripted adapter for tests; registry with concurrency limits, hourly budgets, retries, persistent circuit breakers, role availability; live model probe |
| `workforce.docs` | Parsers (md/txt/pdf/docx/json/yaml/OpenAPI), requirement extraction with stable IDs, ambiguity/contradiction/gap detection, ingestion with preserved read-only originals, approvals, traceability matrix |
| `workforce.agents` | Role prompts and strict JSON schemas for the Architect, Manager, Developers, Tester, Reviewer, DevOps and Worker |
| `workforce.engine` | State machine, dependency graph and scheduler, planning gate, task pipeline, orchestrator loop, recovery, control plane, test execution |
| `workforce.gitops` | Worktrees, branches, engine-performed commits with role attribution, merges, conflict detection, revert-based rollback |
| `workforce.security` | Command allowlist, path scoping, protected branches and paths, secret redaction, prompt-injection scanning |
| `workforce.api` | FastAPI control API (loopback only) that also serves the static dashboard |
| `dashboard/` | Next.js + TypeScript + Tailwind dashboard (static export), polling the live API |
| `workforce.daemon` | launchd LaunchAgent and the safety check that gates unattended mode |

## Multiple projects

The engine is installed once. The central database's `projects` table is the registry. Each project
registered with `workforce project register /abs/path` gets an isolated state home (`.workforce/homes/<name>/`)
with its own database, logs, worktrees and artifacts, plus a `.workforce.yaml` marker in its repository for
discovery from any subdirectory (`workforce/projects.py`).

`workforce run --all-projects` and the launchd daemon run a `Supervisor` (`engine/supervisor.py`): one
orchestrator per project in a single asyncio loop. A `SharedRuntime` (`engine/shared.py`) gives them global
provider semaphores, hourly budgets counted across every project database, central role availability and
circuit breakers, a global developer-concurrency cap, one-task-at-a-time developer roles, and engine-wide
pause/stop. Registry rows that predate multi-project support (`home_path` NULL) keep their state in the
central database.

## Task state machine

```
BACKLOG → READY → ASSIGNED → IMPLEMENTING → TESTING → REVIEWING → INTEGRATING → COMPLETED
                                  ▲             │          │            │
                                  └── NEEDS_FIX ◄┴──────────┴────────────┘
Any active state → BLOCKED | PAUSED | CANCELLED | FAILED;  BLOCKED → READY (human retry)
COMPLETED → BLOCKED only through an explicit human rollback.
```

Every transition is validated against `engine/state.py:TRANSITIONS` and persisted as a `TaskEvent` plus
an audit-log entry.

## Pipeline (per task)

1. **ASSIGNED.** The engine creates `wf/<project>/task/<key>` from the integration head in its own worktree.
   *Before any implementation exists*, the Tester writes acceptance tests from the requirements and the
   interface contract in a separate worktree (`wf/<project>/accept/<key>`). The engine accepts only files
   under `acceptance_tests/<key>/` and commits them as the tester.
2. **IMPLEMENTING.** The assigned developer works in its worktree with a workspace-write sandbox and may
   run only the project's test command. Changes are attributed by snapshotting the worktree just before the
   agent runs. The policy engine rejects edits to protected or secret paths, edits outside the task's
   `owned_paths` (architecture violation) and leftover conflict markers. The engine then commits the change,
   authored as `workforce/<role> (<model>)`.
3. **TESTING.** A detached verification worktree combines the implementation with the hidden acceptance
   tests. The engine runs the acceptance suite, then the full regression suite, then migrations if
   `alembic.ini` exists. Commands, exit codes and outputs are recorded as `TestRun` evidence.
4. **REVIEWING.** The Reviewer gets the diff, requirements, ADRs, test evidence and policy observations, and
   returns `APPROVED`, `CHANGES_REQUESTED` or `BLOCKED`. Rejections without evidence and requirement IDs
   are invalid. An approval contradicted by unsatisfied requirement coverage or major findings is turned
   into a change request. Review never starts without passing test evidence for the current head.
5. **INTEGRATING.** If required, integration waits for an approval. Merges are serialized behind an
   integration lock: the task branch, then the acceptance branch, go into `wf/<project>/integration`.
   DevOps may resolve conflicts, but only in the conflicted files; anything else, an unsafe verdict or a
   failing regression aborts the merge. Then the full regression suite runs. A failure resets the
   engine-owned integration branch to its pre-merge commit and sends the task back.
6. **NEEDS_FIX.** Structured feedback is appended and a failure signature is computed. A repeated signature
   blocks the task early. After `max_correction_attempts` (default 3) corrections the task is `BLOCKED`.

When every task is complete, the engine runs a final regression, writes a deployment-readiness report and
asks for a `promote` approval. Promotion fast-forwards the protected target branch, never with a force.

## Scheduling

`engine/scheduler.py` promotes tasks whose dependencies are complete and assigns READY tasks to free
developers, at most `max_dev_concurrency` (2). Tasks whose `owned_paths` overlap any active or integrating
task are never run concurrently. When a real choice exists, the Manager agent proposes an ordering. The
engine sanitizes the proposal (unknown task keys and non-developer roles are dropped). If the Manager is
unavailable, the deterministic order is used and the fallback is visible in the task events.

## Recovery

On start the orchestrator takes an exclusive `flock`, marks executions left `running` as `interrupted`,
and reconciles every task:

- `INTEGRATING` tasks whose commits are already on the integration branch become `COMPLETED`, not merged twice.
- `IMPLEMENTING` worktrees are reset to the last engine commit, because partial agent edits are untrusted.
- Other active states resume their step.

SIGTERM/SIGINT trigger a graceful stop: steps finish within `shutdown_grace_s`, then are cancelled.

## Provider integration

| | Claude Code | Codex CLI | OpenCode |
|---|---|---|---|
| Invocation | `claude -p` (prompt on stdin) | `codex exec -` (prompt on stdin) | `opencode run --format json` |
| Model | `--model` | `-m`, `-c model_reasoning_effort=…` | `-m provider/model`, `--variant` |
| Isolation | cwd = worktree; `--permission-mode dontAsk` + explicit `--allowedTools`; `--strict-mcp-config` | `-C worktree`, `-s read-only` / `workspace-write` | `--dir worktree`, inline permission policy via `OPENCODE_CONFIG_CONTENT` (edit/bash deny by default) |
| Structured output | `--json-schema` → `structured_output` | `--output-schema` + `-o last-message` | schema in prompt + robust JSON extraction |
| Usage | tokens + list-price estimate | tokens from `turn.completed` | tokens + cost from `step_finish` |
| Sessions | `--no-session-persistence` | `--ephemeral` | session id captured; `-s` supported |

All adapters run without a shell, in their own process group (timeouts kill the whole group), with a
runtime guard that refuses any permission-bypass flag. Errors are classified as `timeout`, `auth`,
`usage_limit`, `network`, `model_unavailable`, `provider_error`, `invalid_output`, `circuit_open`, `budget`
or `not_installed`:

- Transient errors are retried once.
- Usage limits open the provider's circuit breaker for an hour.
- Auth and entitlement errors mark the role unavailable until it is re-verified.
- While a provider is unavailable, waiting tasks are not charged a correction attempt.

## Data model

See `src/workforce/db/models.py`. Traceability is a join, not duplicated data:
`Requirement.key ∈ Decision.requirement_keys ∈ Task.requirement_keys → Commit/TestRun/Review(task_id) →
final status` (`docs/traceability.py`).
