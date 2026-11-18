# Build checkpoint

**Status (2026-11-18): all ten implementation phases are complete and validated.** Remaining items are
external blockers (listed below), not unfinished code.

## Phases

| Phase | Status | Evidence |
|---|---|---|
| 1 Foundation (config, SQLite/Alembic, JSON logging, CLI) | done | `tests/test_foundation.py` |
| 2 Provider adapters + compatibility | done | `tests/test_providers.py`; live: `WORKFORCE_LIVE=1 pytest -m live` (5 pass, 1 xfail = OpenCode Go) |
| 3 Core orchestration (state machine, scheduler, persistence, recovery) | done | `tests/test_orchestration_core.py` |
| 4 Documentation engine | done | `tests/test_docs_engine.py` |
| 5 Agent roles | done | exercised in E2E and the live run |
| 6 Parallel execution (worktrees, ownership, conflicts) | done | E2E conflict and parallel tests |
| 7 Verification (acceptance tests, review, correction loop, gates) | done | `tests/test_e2e_pipeline.py` |
| 8 Continuous operation (lifecycle, launchd, safety gate, limits) | done | `tests/test_lifecycle.py` |
| 9 Dashboard (API + Next.js) | done | `tests/test_api.py`, `dashboard/e2e` (Playwright, 3 pass) |
| 10 End-to-end validation | done | `docs/validation/README.md`, `docs/validation/live-e2e-report.md` |

## Multi-project extension (2026-11-18)

| Feature | Evidence |
|---|---|
| Engine stays in its install directory; central config resolved from it, never from the cwd | `config.engine_root()`; `test_global_command_shim_and_engine_root` |
| Global `workforce` command without activating the venv | `scripts/install_global.sh` → `~/.local/bin/workforce` |
| Register existing repositories by absolute path; `.workforce.yaml` upward discovery; `--project /abs/path` | `projects.py`; `test_register_validates_paths_and_writes_marker`, `test_discovery_explicit_selection_and_switching`, `test_cli_from_any_directory` |
| Isolated per-project state (DB, logs, worktrees, branches, task queues) | `test_two_projects_isolated_under_one_daemon` |
| One daemon for all projects with shared provider limits, budgets and a global developer cap | `engine/supervisor.py`, `engine/shared.py`; same test plus `test_global_pause_applies_to_all_projects` |
| `project list`, `project use`, `project status` | CLI tests above |
| Agents confined to their project's workspaces | `security/workspace.py`; `test_agents_cannot_touch_other_workspaces` |
| Existing databases, projects and configuration preserved | Migration 0002 (additive); `test_existing_database_and_projects_preserved`; central DB upgraded in place with role-verification data intact (backup: `.workforce/workforce.pre-0002.backup.db`) |
| Multi-project API and dashboard | `test_api_serves_each_project_from_its_own_state`; Playwright |

## External blockers

1. **OpenCode Go is not usable on this account.** Every `opencode-go/*` model returns "An active OpenCode
   Go subscription is required", and `opencode-go/deepseek-v4.1-flash` additionally requires the **Global**
   privacy region. Manager, Tester and Lightweight Worker are therefore unavailable with their official
   assignment. With the Tester unavailable, tasks block before development, by design. The live E2E run
   used an explicit, labelled override of the Tester to `opencode/deepseek-v4.1-flash-free` (OpenCode Zen
   free tier, synthetic data only). That override exists only in the live-run config, not in the defaults.
2. **GitHub publishing.** Remote `origin` is `https://github.com/Rishit1769/Coding-Workforce-.git`. The
   engine itself never pushes unless `git.push_authorized: true`.

## Known limitations

- Requirement extraction is deterministic and heuristic: modal sentences, explicit IDs, bullets and
  acceptance-criteria sections. The Architect adds a semantic review, which caught an extractor bug in the
  live run (now fixed). Free-form prose without modal verbs may be missed and should be phrased as
  requirements.
- Contradiction detection is heuristic (negation plus token overlap, and numeric conflicts on the same subject).
- The engine runs tests through the project's own test command. Migration validation is automatic only
  when `alembic.ini` exists. Other stacks need their command added to `policy.allowed_commands`.
- Subscription quotas are not observable: usage shows CLI-reported tokens, and costs are list-price estimates.
- System sleep is handled as an interruption: in-flight calls fail or time out and are retried or
  recovered. The engine does not prevent sleep.
- The dashboard polls every 2.5–10 s. There is no push channel.
- One orchestrator per project state directory, and one supervisor per engine installation. Developer
  concurrency is capped at 2 across all projects (`limits.max_total_dev_concurrency`).
- The workspace guard compares repository working trees before and after each agent run. With
  `workspace_guard: block`, your own edits to a registered checkout made during an agent run are
  indistinguishable from an agent's and reject that run. Use `warn` if you work in the same checkout.

## Resume instructions

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
```

```bash
.venv/bin/pytest
```

```bash
cd dashboard && npm ci && npm run build && npx playwright test
```

```bash
.venv/bin/workforce models verify
```

```bash
.venv/bin/python scripts/live_e2e.py --root /tmp/wf-live
```

Once OpenCode Go is active with the Global region, re-run `workforce models verify`. Manager, Tester and
Worker should then show `available`, with no config change needed.
