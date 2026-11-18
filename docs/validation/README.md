# Validation evidence

Two complementary levels of end-to-end validation were run.

## 1. Deterministic end-to-end suite (`tests/test_e2e_pipeline.py`, `tests/test_lifecycle.py`, `tests/test_api.py`)

These tests run the **real orchestration engine**: real SQLite state, real git worktrees, branches and merges,
real pytest execution of acceptance and regression suites, the real policy engine and the real CLI. Only
the language-model calls are replaced, by deterministic scripted agents (`workforce/selftest/scenario.py`),
so that each failure can be injected reproducibly. The sample project is `textstats`
(`examples/textstats/requirements/prd.md`).

| Mandatory scenario | Test | What is verified |
|---|---|---|
| Full pipeline: documentation → planning → development → testing → review → integration → completion | `test_happy_path_full_pipeline` | Requirements ingested and approved; the architecture approval gate; the tester writes acceptance tests **before** any developer call; two developers work in parallel in separate worktrees; ADRs, implementation and acceptance tests land on `wf/textstats/integration` with role-attributed commits; `main` is untouched; final regression and readiness report; `promote` approval requested; every FR traced to `verified` with commits, tests and reviews. |
| Requirement violation | `test_requirement_violation_caught_by_reviewer` | Tests pass, but the Reviewer issues `CHANGES_REQUESTED` citing FR-3 with evidence; the developer gets the structured finding; the second review is `APPROVED`. |
| Failing test | `test_failing_test_triggers_correction` | Hidden acceptance tests fail; failure names and output go back to the developer; one correction; the acceptance runs are recorded as `[fail, pass]`. |
| Architecture violation | `test_architecture_violation_rejected_by_policy` | A developer edits `README.md` outside its owned paths; the engine discards the change, records `architecture_violation`, and the correction succeeds; `README.md` on integration is unchanged. |
| Merge conflict | `test_merge_conflict_resolved_by_devops`, `test_merge_conflict_unsafe_goes_back_to_developer` | Add/add conflict on `tests/test_unit.py`. In the first test DevOps resolves only the conflicted file and regression passes. In the second DevOps declines (unsafe), the merge is aborted, the developer's worktree is updated with the conflict markers, the developer resolves them and integration succeeds. |
| Agent timeout | `test_agent_timeout_is_bounded_and_retried` | A developer call exceeds its 1 s timeout; the process is killed and the execution is recorded as `timeout`; the timeout becomes structured feedback and the retry succeeds. |
| Provider failure | `test_provider_failure_circuit_breaker_and_recovery` | The reviewer provider fails 3× with network errors; the circuit breaker opens (audit `provider.circuit_open`); the task waits without consuming correction attempts and completes once the provider recovers. |
| Unavailable model (real-world blocker) | `test_unavailable_tester_blocks_instead_of_substituting` | Tester model unavailable: tasks are `BLOCKED` with a clear reason, **no development happens without independent tests**, no model is substituted, a `role_unavailable` notice is raised and the engine stops with "no actionable work". |
| Process interruption (in-process crash) | `test_process_interruption_recovery` | The engine is cancelled mid-implementation and garbage is left in the worktree. On restart: the execution is marked `interrupted`, partial edits are discarded, each task is integrated exactly once and the garbage is never committed. |
| Process interruption (real `SIGKILL`) | `test_sigkill_engine_process_and_resume` | `workforce run` is started as a separate OS process and killed with `SIGKILL` while a task is `IMPLEMENTING`; a new `workforce run` recovers and completes every task. |
| Bounded correction loop | `test_bounded_correction_loop_blocks`, `test_correction_attempt_limit` | Repeated identical failures stop early; otherwise the task is blocked after 3 corrections. |
| Approval gates, pause/resume/stop | `test_integration_approval_gate_and_pause_resume`, `test_pause_resume_stop_flags` | Integration waits for human approval; pause prevents scheduling; stop is graceful. |
| Task budget | `test_task_wall_clock_budget` | Tasks exceeding `task_wall_clock_s` are blocked. |
| CLI lifecycle | `test_cli_retry_cancel_report_promote`, `test_rollback_reverts_integration` | retry, cancel and report; promote is refused until approved, then fast-forwards `main` with no push; rollback via revert. |
| Dashboard / API | `tests/test_api.py`, `dashboard/e2e/dashboard.spec.ts` (Playwright) | The API reflects real state; controls and approvals change real state; CSRF and host guards hold; the UI renders the agents, tasks, approvals, traceability and git from a server seeded by a real engine run. |

Latest run: `59 passed, 6 skipped` (the skipped tests are the opt-in live-provider tests) and Playwright
`3 passed`.

## 2. Live end-to-end run with the real CLIs (`scripts/live_e2e.py`)

See [live-e2e-report.md](live-e2e-report.md), generated by the script from the live state database.
