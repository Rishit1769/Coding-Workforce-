# Coding Workforce

A locally hosted, documentation-driven, multi-agent software engineering workforce. You (the Product
Owner / System Architect) supply documents; the workforce extracts requirements, asks you about
ambiguities, designs the architecture, splits the work into tasks, develops in parallel isolated git
worktrees, independently tests and reviews every change, corrects failures in a bounded loop, integrates
approved work and reports progress. It stops when no approved, actionable work remains.

It orchestrates the CLIs you already have authenticated — **Claude Code**, **Codex CLI** and **OpenCode** —
through their official non-interactive modes. It never bypasses permissions, never reads credentials and
never deploys to production.

```
docs ──► ingestion ──► requirements ──► (human approval) ──► Architect ──► (human approval)
                                                                 │
              ┌──────────────── tasks (dependency graph, file ownership) ◄──┘
              ▼
  Tester writes acceptance tests ──► Developer (isolated worktree) ──► engine runs tests
              ▲                                                         │
              └─── NEEDS_FIX (≤3 corrections, early stop on repeats) ◄── Reviewer verdict
                                                                         │ APPROVED
                                  DevOps: merge → regression → integration branch → COMPLETED
```

## Workforce roles

| Role | Desired model | Provider | Model ID used | Verified on this machine |
|---|---|---|---|---|
| Manager | DeepSeek V4.1 Flash | OpenCode Go | `opencode-go/deepseek-v4.1-flash` | **unavailable** (see below) |
| Architect | Claude Opus 5.5 | Claude Code | `claude-opus-5-5` | yes |
| Developer 1 | GPT-6.1 Sol Medium | Codex CLI | `gpt-6.1-sol` + `model_reasoning_effort=medium` | yes |
| Developer 2 | Claude Sonnet 5.5 | Claude Code | `claude-sonnet-5-5` | yes |
| Tester | DeepSeek V4.1 Flash | OpenCode Go | `opencode-go/deepseek-v4.1-flash` | **unavailable** |
| Reviewer | GPT-5.6 Sol | Codex CLI | `gpt-5.6-sol` | yes |
| DevOps | GPT-6 Sol | Codex CLI | `gpt-6-sol` | yes |
| Lightweight Worker | DeepSeek V4.1 Flash | OpenCode Go | `opencode-go/deepseek-v4.1-flash` | **unavailable** |

The OpenCode Go models are refused for this account: OpenCode returns *"An active OpenCode Go subscription
is required"* and, for DeepSeek, *"hosted outside Europe and the United States — select Global"*. The
workforce marks those roles unavailable and never substitutes another model on its own. See
[docs/model-compatibility.md](docs/model-compatibility.md).

## Requirements

- macOS on Apple Silicon (any POSIX system works except the launchd integration)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Node.js 20+ (dashboard only)
- `claude`, `codex` and `opencode` installed and logged in

## Install

The engine stays in its installation directory (this repository). Install it once:

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
```

```bash
cd dashboard && npm ci && npm run build && cd ..
```

Make `workforce` available everywhere, without activating the virtualenv. This symlinks
`~/.local/bin/workforce` to the engine's entry point:

```bash
./scripts/install_global.sh
```

```bash
workforce init
```

```bash
workforce models verify
```

`init` writes the central `workforce.yaml` (official role assignments and safe defaults; see
`workforce.example.yaml`) in the engine directory and creates the central state in `.workforce/`. The
central config is always resolved from the engine directory, never from your current directory; override it
with `--config` or `$WORKFORCE_CONFIG`. `models verify` sends one tiny prompt per distinct model and records
which roles are available, using a small amount of subscription quota.

## Running projects from any directory

Register each existing git repository once, by absolute path:

```bash
workforce project register "$HOME/Code/ANVIKRA" --docs "$HOME/Code/ANVIKRA/docs/specs" --test-command "python -m pytest -q"
```

```bash
workforce project register "$HOME/Code/YANTRA" --name yantra --test-command "npm test"
```

```bash
workforce project register /Volumes/Work/client-portal --name client-portal --docs /Volumes/Work/client-portal/requirements
```

Registration:

- writes a `.workforce.yaml` marker in the repository root (excluded from git via `.git/info/exclude`);
- creates a separate state directory, `<engine>/.workforce/homes/<name>/`, holding that project's database,
  task history, executions, approvals, logs, worktrees, preserved documents and reports;
- namespaces all of its branches as `wf/<name>/…`.

Projects never share documentation, worktrees, branches, task queues or execution history.

After that, run commands from anywhere inside a repository. The project is found by searching upward for
`.workforce.yaml`:

```bash
cd "$HOME/Code/ANVIKRA/backend/services" && workforce req list && workforce plan && workforce run
```

You can also select a project explicitly from any directory:

```bash
workforce --project "$HOME/Code/YANTRA" project status
```

Or set a current project for commands run outside any repository:

```bash
workforce project use anvikra
```

The active project is resolved in this order:

1. a project argument to the command (name or absolute path);
2. `--project /abs/path` or `$WORKFORCE_PROJECT`;
3. the nearest `.workforce.yaml` above the current directory;
4. a registered repository that contains the current directory;
5. `workforce project use`;
6. the only registered project, if there is just one.

Useful commands:

| Command | Purpose |
|---|---|
| `workforce project list` | All registered projects (`*` marks the current one), status, task progress and state directory |
| `workforce project status [NAME\|PATH]` | One project in detail: requirements, tasks, blockers, pending approvals, branches |
| `workforce project use NAME\|PATH` / `--clear` | Switch the current project |
| `workforce status` | Engine-wide view of the daemon and every project |
| `workforce run --all-projects` | One engine process schedules every registered project |
| `workforce pause --all` / `resume --all` / `stop --all` | Control the multi-project engine; without `--all` they act on the active project |

The daemon (`workforce daemon install`) runs `workforce run --all-projects`. All projects share:

- each provider's concurrency limit and hourly call budget;
- role availability and circuit breakers;
- a global developer cap, `limits.max_total_dev_concurrency` (default 2), with each developer role working
  on one task at a time across all projects.

Per-project overrides go in the project's `.workforce.yaml`:

```yaml
project: yantra
repo: /Users/you/Code/YANTRA
overrides:
  target_branch: develop
  max_dev_concurrency: 1
  require_integration_approval: true
  extra_allowed_commands: ["npm test"]
```

Agents are confined to their own project. They can only be started inside that project's worktrees or
repository, each CLI's sandbox limits writes to that directory, and the engine snapshots every registered
repository (and the engine itself) around each agent run. A change outside the agent's workspace rejects
that run (`policy.workspace_guard: block`, or `warn` to only report it). If you edit your own checkout
while agents are running, use `warn`.

## Submit a project

1. Put your documents (Markdown, text, PDF, DOCX, JSON, YAML, OpenAPI) in a folder, register the repository
   and ingest them:

   ```bash
   workforce project register "$HOME/Code/ANVIKRA" --docs "$HOME/Code/ANVIKRA/docs/specs"
   ```

2. From inside the repository, review the extracted requirements and the issues found (ambiguities,
   contradictions, gaps, prompt-injection attempts):

   ```bash
   cd "$HOME/Code/ANVIKRA" && workforce req list
   ```

3. Resolve every major issue, optionally amending a requirement's text, then approve:

   ```bash
   workforce req resolve 3 --resolution "max length is 100" --amend "REQ-PRD-9AF8E1=Titles must be at most 100 characters."
   ```

   ```bash
   workforce req approve
   ```

4. Ask the Architect for a plan, read it (the path is printed), then approve it:

   ```bash
   workforce plan
   ```

   ```bash
   workforce approvals
   ```

   ```bash
   workforce approve <id>
   ```

5. Start the workforce. It stops by itself when no approved actionable work remains:

   ```bash
   workforce run
   ```

6. Watch and steer it from the dashboard (its project selector lists every registered project) or the CLI:

   ```bash
   workforce serve
   ```

   Then open http://127.0.0.1:8765. Useful CLI commands: `project status`, `task list`,
   `task show T-001`, `report`, `req trace`, `pause`, `resume`, `stop`, `task retry T-003`,
   `task cancel T-004`.

7. When every task is complete, the workforce requests a `promote` approval. Approve it, then fast-forward
   your protected target branch:

   ```bash
   workforce integrate promote
   ```

## Operating modes

- **Supervised** (default): architecture, every integration and promotion need your approval.
- **Unattended**: integrations into the engine-owned integration branch happen automatically; architecture
  and promotion still need approval. It is refused until `workforce safety-check` passes for the current
  code version (configuration checks plus the full non-live test-suite). For background operation on
  macOS:

  ```bash
  .venv/bin/workforce safety-check && .venv/bin/workforce daemon install
  ```

## Documentation

- [docs/architecture.md](docs/architecture.md): components, state machine, pipeline, data model
- [docs/security.md](docs/security.md): execution boundaries and threat model
- [docs/operations.md](docs/operations.md): CLI reference, dashboard, recovery, launchd, troubleshooting
- [docs/model-compatibility.md](docs/model-compatibility.md): verified model IDs and CLI flags
- [docs/validation/](docs/validation/): end-to-end validation evidence
- [CHECKPOINT.md](CHECKPOINT.md): build status, known limitations, blockers and resume instructions

## Tests

```bash
.venv/bin/pytest
```

```bash
WORKFORCE_LIVE=1 .venv/bin/pytest -m live tests/test_live_providers.py
```

```bash
cd dashboard && npx playwright test
```

The default suite needs no network and uses deterministic scripted agents. It covers the full pipeline and
deliberate failures: a failing test, a requirement violation, an architecture violation, merge conflicts,
an agent timeout, provider failures, an unavailable model, and both in-process and real `SIGKILL`
interruption.
