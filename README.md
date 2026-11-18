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

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
```

```bash
cd dashboard && npm ci && npm run build && cd ..
```

```bash
.venv/bin/workforce init
```

```bash
.venv/bin/workforce models verify
```

`init` writes `workforce.yaml` (official role assignments and safe defaults; see
`workforce.example.yaml`) and creates the state database in `.workforce/`. `models verify` sends one tiny
prompt per distinct model and records which roles are available. It uses a small amount of subscription
quota.

## Submit a project

1. Put your documents (Markdown, text, PDF, DOCX, JSON, YAML, OpenAPI) in a folder. The target repository must be a git repository.
2. Register the project and ingest the documents:

   ```bash
   .venv/bin/workforce project create myapp --repo ~/code/myapp --docs ~/specs/myapp --test-command "python -m pytest -q"
   ```

3. Review the extracted requirements and the issues found (ambiguities, contradictions, gaps, prompt-injection attempts):

   ```bash
   .venv/bin/workforce req list myapp
   ```

4. Resolve every major issue, optionally amending a requirement's text, then approve:

   ```bash
   .venv/bin/workforce req resolve 3 --resolution "max length is 100" --amend "REQ-PRD-9AF8E1=Titles must be at most 100 characters."
   ```

   ```bash
   .venv/bin/workforce req approve myapp
   ```

5. Ask the Architect for a plan, read it (in `.workforce/projects/myapp/architecture.md`), then approve it:

   ```bash
   .venv/bin/workforce plan myapp
   ```

   ```bash
   .venv/bin/workforce approvals
   ```

   ```bash
   .venv/bin/workforce approve <id>
   ```

6. Start the workforce. It stops by itself when no approved actionable work remains:

   ```bash
   .venv/bin/workforce run
   ```

7. Watch and steer it from the dashboard or the CLI:

   ```bash
   .venv/bin/workforce serve
   ```

   Then open http://127.0.0.1:8765. Useful CLI commands: `status`, `task list`, `task show T-001`,
   `report myapp`, `req trace myapp`, `pause`, `resume`, `stop`, `task retry T-003`, `task cancel T-004`.

8. When every task is complete, the workforce requests a `promote` approval. Approve it, then fast-forward
   your protected target branch:

   ```bash
   .venv/bin/workforce integrate promote myapp
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
