# Security and execution boundaries

## Threat model

| Threat | Control |
|---|---|
| Prompt injection in requirement documents or repository content | Documents are wrapped in random-tagged `UNTRUSTED` delimiters, with a system notice that they are data. Ingestion scans for instruction-like content (override instructions, role hijack, pipe-to-shell, bypass flags, exfiltration, status manipulation) and records major issues that block approval of affected work. Agents cannot change state: they only return JSON that the engine validates. |
| An agent running arbitrary commands | Claude Code: `--permission-mode dontAsk` with an explicit tool allowlist (read tools; edit tools only for writers; `Bash(<test command> *)` only). Codex: OS sandbox `read-only` / `workspace-write`. OpenCode: permission policy denying bash except allowlisted test commands and denying edits for read-only roles. The engine's own command runner executes only allowlisted argv prefixes, with no shell and with secret-like environment variables stripped. |
| Unsafe execution flags | `FORBIDDEN_FLAGS` (`--dangerously-skip-permissions`, `bypassPermissions`, `--dangerously-bypass-approvals-and-sandbox`, `danger-full-access`, `--auto`, …) are refused at runtime by the adapter base class and asserted in tests. |
| Cross-project interference | Every project has its own state database, worktrees, logs and `wf/<name>/` branch namespace. An agent may only be started inside its own project's worktree directory or repository (`check_cwd`). Every registered repository and the engine installation are snapshotted around each agent run, and changes outside the agent's workspace reject the run (`policy.workspace_guard: block`) or are reported (`warn`), with an audit entry and a `workspace_violation` notice. `.workforce.yaml` is a protected path. |
| Filesystem escape | Agents run with the task worktree as cwd. Owned paths containing `..` or absolute paths are rejected at planning time. `ensure_within` guards engine path handling. |
| Modification of requirements or acceptance tests by developers | `protected_paths` (`requirements/**`, `acceptance_tests/**`, `.workforce/**`, `.git/**`, `.github/workflows/**`): developer edits there are discarded and fed back as a policy failure. Tester output outside its own acceptance directory is rejected. |
| Architecture drift | Each task declares `owned_paths`. Edits outside them (other than `tests/**`) are rejected as architecture violations. |
| Secret leakage | Secret-file globs (`.env*`, `*.pem`, `*.key`, `auth.json`, …) are blocked from commits. Regex redaction covers Anthropic/OpenAI/GitHub/AWS/Slack keys, JWTs, private keys, bearer tokens and `key=value` secrets in every log line, stored execution output, test output and API response. The workforce never reads CLI credential files: it relies on each CLI's own authenticated session. |
| Protected branches | `main`, `master`, `production`, `release` cannot be written by the engine. Work lands on `wf/<project>/integration`. Promotion is a separate fast-forward-only command that requires an approved `promote` request. Pushing happens only if `git.push_authorized: true`, and never with force. |
| Destructive git operations | `push --force`, `reset --hard`, `clean -fd`, `branch -D` and `push --delete` are refused unless the call targets an engine-owned `wf/` worktree (reset of agent scratch state). Rollback uses `git revert`. |
| Runaway loops and cost | At most 3 correction attempts with early stop on repeated identical failures, per-call timeouts with process-group kill, per-provider concurrency limits and hourly call budgets, and persistent circuit breakers (an hour-long cooldown on usage-limit errors). |
| Production deployment | Not implemented by design. DevOps only prepares local readiness reports. |
| Dashboard CSRF / DNS rebinding | Binds to loopback only (validated in config and at serve time). The `Host` header must be loopback. State-changing requests need the `X-Workforce-Client` header, a JSON content type and a loopback `Origin`. No CORS is granted, so cross-site requests fail preflight. |
| Unattended operation | Refused until `workforce safety-check` passes for the exact code fingerprint (configuration checks plus the full non-live test-suite). |

## Audit

Every transition, approval, control action, provider circuit change, merge resolution, push, promotion
and rollback is written to `audit_log`, together with who did it (`engine`, `human`, `human(dashboard)` or
a role).
