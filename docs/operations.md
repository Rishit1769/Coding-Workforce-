# Operations

All commands accept `--config/-c PATH` (or `WORKFORCE_CONFIG`). Without it the central config is
`<engine root>/workforce.yaml`, whatever the current directory. Project commands act on the active project:
see "Running projects from any directory" in the README for resolution rules, and use
`workforce --project /abs/path <command>` to choose explicitly.

State layout:

```
<engine root>/.workforce/                 central: registry (projects table), role availability,
  workforce.db                              circuit breakers, engine run state, safety check
  logs/workforce.jsonl                      engine-level log
  homes/<project>/                          one per registered project (isolated):
    workforce.db                              requirements, tasks, executions, approvals, audit
    logs/workforce.jsonl                      this project's log records only
    worktrees/<project>/…                     task, verification and integration worktrees
    artifacts/, projects/<project>/           reports, preserved documents, architecture, readiness
    engine.lock                               one orchestrator per project at a time
```

Projects registered before multi-project support (`home_path` empty in the registry) keep their state in
the central database and keep working unchanged.

## CLI reference

| Command | Purpose |
|---|---|
| `init [PATH]` | Write a default `workforce.yaml` and create the database |
| `config` | Print the effective configuration |
| `models verify [--role R]` / `models list` | Probe role models through the real CLIs / show the last results |
| `project register /ABS/REPO [--name N] [--docs PATH] [--test-command CMD] [--use]` | Register an existing repository (isolated state, `.workforce.yaml` marker) |
| `project create NAME --repo /ABS/REPO --docs PATH` | Register and ingest in one step |
| `project list` / `project use NAME\|PATH` / `project status [NAME\|PATH]` | Registry, switching, per-project status |
| `project ingest [PROJECT] PATH` | Re-ingest; requirements whose text changed need re-approval |
| `req list NAME` / `req approve NAME [KEYS…]` / `req reject NAME KEY --reason` | Review and approve requirements |
| `req resolve ISSUE_ID --resolution TEXT [--amend KEY=TEXT]` | Resolve an ambiguity or contradiction |
| `req trace NAME [--format markdown\|csv\|json]` | Traceability matrix |
| `plan NAME [--feedback TEXT]` | Run the Architect; requests architecture approval |
| `approvals [--all]` / `approve ID [--comment]` / `reject ID` | Approval gates |
| `run [--all-projects] [--until-idle\|--forever] [--unattended] [--max-seconds N]` | Run the engine for the active project, or every project in one process |
| `force` | Start the engine in the background for the active project (default project: YANTRA); refuses duplicates |
| `force <command>` | Same as `workforce <command>` |
| `project default [NAME\|PATH] [--clear]` | Fallback project when nothing else selects one (built-in fallback: `yantra`) |
| `start [--unattended] [--all-projects]` | Start the engine in the background (`logs/engine.out.log`); refuses if one is already running |
| `pause` / `resume` / `stop` `[--all]` | Run-state control (graceful); `--all` targets the multi-project engine |
| `status [--json]` | Run state, heartbeat, task counts, roles, pending approvals |
| `task list [NAME]` / `task show KEY` | Task inspection |
| `task retry KEY` / `task cancel KEY` / `task pause KEY` / `task resume KEY` | Task control |
| `report [NAME]` | Markdown progress report from the live database |
| `integrate promote NAME` | Fast-forward the protected target branch (needs an approved `promote` request) |
| `integrate rollback NAME KEY` | Revert a task's merges on the integration branch |
| `safety-check [--skip-tests]` | Enable unattended mode for this code version |
| `daemon install\|uninstall\|status` | launchd LaunchAgent |
| `serve [--port]` | Control API and dashboard on 127.0.0.1 |

## Dashboard

```bash
cd dashboard && npm ci && npm run build
```

```bash
.venv/bin/workforce serve
```

Then open http://127.0.0.1:8765. During development, run `npm run dev` (port 3000); it proxies `/api` to
the running control API. Every panel reads the live database or git. When the API is unreachable, the UI
shows an error instead of stale or synthetic numbers.

## Recovery behaviour

- **Crash or kill.** Start the engine again. Interrupted executions are marked `interrupted`, and partial
  agent edits are discarded. Tasks that were merged before the crash are marked complete without
  re-merging.
- **Provider usage limit.** The provider's circuit opens for `usage_limit_cooldown_s` (default 1 h).
  Affected tasks wait without consuming correction attempts, and the engine keeps running other work.
- **Auth expired or model not entitled.** The role is marked unavailable. Developer tasks go back to the
  queue for the other developer. Tester or reviewer tasks become `BLOCKED` with the reason. Re-login, run
  `workforce models verify`, then `task retry`.
- **Network loss or sleep.** These are classified as network errors and retried. Tasks back off and resume
  once calls succeed again.
- **Stopping automatically.** When nothing is actionable (everything is complete, blocked, or waiting for
  an approval) the engine stops and records the reason in the audit log.

## launchd

`workforce daemon install` writes `~/Library/LaunchAgents/dev.workforce.engine.plist`. It runs
`workforce run --until-idle --unattended` at load and every 30 minutes, and is not kept alive: the engine
exits whenever there is no approved actionable work. Installing refuses unless the safety check passed for
the current code. Remove it with `workforce daemon uninstall`.

## Configuration highlights (`workforce.yaml`)

```yaml
mode: supervised
require_architecture_approval: true
require_integration_approval: true
roles:
  tester:                 # change a role explicitly; override_reason is shown everywhere
    provider: opencode
    model: opencode-go/deepseek-v4.1-flash
    override_reason: null
limits: {max_dev_concurrency: 2, max_correction_attempts: 3, command_timeout_s: 900}
providers:
  opencode: {max_concurrency: 1}   # OpenCode's local DB does not tolerate parallel runs
git: {target_branch: main, push_authorized: false, remote: origin}
```

## Troubleshooting

- `another orchestrator instance is running`: only one engine per home directory is allowed. Use `stop`, or
  check `<home>/engine.lock`.
- A task is `BLOCKED` with `tester unavailable`: the tester's model is not usable. See
  `docs/model-compatibility.md`.
- `database is locked` from OpenCode: keep `providers.opencode.max_concurrency: 1`.
