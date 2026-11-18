# Model compatibility report

Verified on 2026-11-18 on the target MacBook (Apple Silicon) with the installed, authenticated CLIs:

| CLI | Version | Non-interactive mode used |
|---|---|---|
| Claude Code | 2.1.326 | `claude -p --output-format json --json-schema … --permission-mode dontAsk --allowedTools … --strict-mcp-config --no-session-persistence` |
| Codex CLI | 0.167.2 | `codex exec - --json --ephemeral -s read-only\|workspace-write -C <dir> -m <model> -c model_reasoning_effort=<e> --output-schema <file> -o <file>` |
| OpenCode | 1.19.0 | `opencode run --format json -m <provider/model> --dir <dir>` with `OPENCODE_CONFIG_CONTENT` permission policy |

Model IDs were discovered from each CLI's own catalogue (`opencode models`, Codex's model cache, live
Claude probes) and then confirmed with a real structured-output call (`workforce models verify` and
`WORKFORCE_LIVE=1 pytest -m live`).

| Role | Desired | Exact model ID | Result |
|---|---|---|---|
| Manager | DeepSeek V4.1 Flash (OpenCode Go) | `opencode-go/deepseek-v4.1-flash` | **Unavailable**: `model_unavailable` |
| Architect | Claude Opus 5.5 (Claude Pro) | `claude-opus-5-5` | Verified (structured output) |
| Developer 1 | GPT-6.1 Sol Medium (ChatGPT Plus / Codex) | `gpt-6.1-sol`, `model_reasoning_effort=medium` | Verified |
| Developer 2 | Claude Sonnet 5.5 (Claude Pro) | `claude-sonnet-5-5` | Verified |
| Tester | DeepSeek V4.1 Flash (OpenCode Go) | `opencode-go/deepseek-v4.1-flash` | **Unavailable**: `model_unavailable` |
| Reviewer | GPT-5.6 Sol (ChatGPT Plus / Codex) | `gpt-5.6-sol` | Verified |
| DevOps | GPT-6 Sol (ChatGPT Plus / Codex) | `gpt-6-sol` | Verified |
| Lightweight Worker | DeepSeek V4.1 Flash (OpenCode Go) | `opencode-go/deepseek-v4.1-flash` | **Unavailable**: `model_unavailable` |

## Why the OpenCode Go roles are unavailable

The model appears in `opencode models` as `opencode-go/deepseek-v4.1-flash`, but every call is refused by
the OpenCode service:

- For `opencode-go/deepseek-v4.1-flash`: *"This OpenCode Go model is hosted outside Europe and the United
  States. Select Global at https://opencode.ai/console/…/settings/privacy#regions to use it."* (HTTP 400)
- For every other `opencode-go/*` model tried: *"An active OpenCode Go subscription is required to use
  OpenCode Go models."* (HTTP 403)

So the account has no active OpenCode Go subscription, and DeepSeek additionally requires the
**Global** region privacy setting. Both are account decisions for the owner. The workforce does not change
them and does not substitute models.

The same model is reachable through OpenCode Zen's free tier as `opencode/deepseek-v4.1-flash-free`. That
is a different plan, whose data-handling terms may differ from Go, so it is **not** used by default. The live
end-to-end validation used it for the Tester only, as an explicit `override_reason`-labelled substitution on
synthetic sample data. That override is visible in reports and on the dashboard.

## Behaviour while roles are unavailable

| Role | Effect |
|---|---|
| Manager | The deterministic scheduling policy assigns work (dependency- and ownership-aware); the Manager's suggestions are optional. |
| Tester | Tasks are `BLOCKED` with reason "tester unavailable". Development never starts without independent acceptance tests. |
| Worker | Repository exploration summaries and failure-log classification are skipped. |

## To restore the official assignment

1. In the OpenCode console, activate an OpenCode Go subscription and set the privacy region to **Global**.
2. Re-verify the roles:

   ```bash
   .venv/bin/workforce models verify
   ```

3. Retry blocked tasks: `workforce task retry <KEY>`.

## Notes

- Claude Code reports `total_cost_usd` at list price even on subscription plans. The workforce records it as
  `estimated_list_cost_usd` and never presents it as a charge.
- Running several `opencode run` processes in parallel fails with "database is locked" (OpenCode's local
  SQLite). The OpenCode provider therefore defaults to `max_concurrency: 1`.
