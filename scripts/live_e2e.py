"""Live end-to-end validation against the real, authenticated provider CLIs.

Copies examples/textstats into a fresh git repository and drives the full
pipeline through the `workforce` CLI:

    Documentation -> Planning -> Development -> Testing -> Review -> Integration -> Completion

Consumes real subscription usage. Usage:

    .venv/bin/python scripts/live_e2e.py --root /path/to/scratch [--tester-override MODEL]

Any role override is explicit, recorded in the config with `override_reason`
and shown in every report - the workforce never substitutes models silently.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def sh(cmd: list[str], cwd: Path | None = None, env: dict | None = None, check: bool = True) -> str:
    print("$", " ".join(cmd), flush=True)
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    out = p.stdout + p.stderr
    print(out[-4000:], flush=True)
    if check and p.returncode != 0:
        raise SystemExit(f"command failed ({p.returncode}): {' '.join(cmd)}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--tester-override", default="opencode/deepseek-v4.1-flash-free")
    ap.add_argument("--report", type=Path, default=ROOT / "docs" / "validation" / "live-e2e-report.md")
    args = ap.parse_args()
    root = args.root.resolve()
    if root.exists():
        shutil.rmtree(root)
    repo = root / "textstats"
    shutil.copytree(ROOT / "examples" / "textstats", repo)
    for c in (["init", "-q", "-b", "main"], ["config", "user.email", "owner@example.invalid"],
              ["config", "user.name", "Sample Owner"], ["add", "."], ["commit", "-qm", "chore: sample project"]):
        sh(["git", *c], cwd=repo)
    cfg = {
        "home": str(root / ".wf"),
        "require_architecture_approval": False,
        "require_integration_approval": False,
        "limits": {"command_timeout_s": 600, "poll_interval_s": 2.0},
        "roles": {},
    }
    if args.tester_override:
        cfg["roles"]["tester"] = {
            "provider": "opencode", "model": args.tester_override,
            "override_reason": "validation-only: OpenCode Go DeepSeek V4.1 Flash is not usable on this account "
                               "(no active Go subscription / region restricted); same model via OpenCode Zen free tier",
        }
    cfg_path = root / "workforce.live.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))
    wf = [sys.executable, "-m", "workforce.cli"]
    c = ["--config", str(cfg_path)]
    t0 = time.time()
    log: list[tuple[str, str]] = []

    def step(name, *cmd, check=True):
        out = sh([*wf, *cmd, *c], check=check)
        log.append((name, out))
        return out

    step("verify models", "models", "verify")
    step("create project + ingest docs", "project", "create", "textstats", "--repo", str(repo),
         "--docs", str(repo / "requirements"))
    step("requirements", "req", "list", "textstats")
    step("approve requirements", "req", "approve", "textstats")
    step("architecture (Claude Opus)", "plan", "textstats")
    step("run engine until idle", "run", "--until-idle", "--max-seconds", "5400")
    step("tasks", "task", "list", "textstats")
    trace = step("traceability", "req", "trace", "textstats")
    report = step("progress report", "report", "textstats")
    integ = sh(["git", "log", "--format=%h %an | %s", "wf/textstats/integration"], cwd=repo)
    files = sh(["git", "ls-tree", "-r", "--name-only", "wf/textstats/integration"], cwd=repo)
    elapsed = time.time() - t0
    md = [
        "# Live end-to-end validation report", "",
        f"- Duration: {elapsed / 60:.1f} minutes",
        f"- Tester override: `{args.tester_override}` (explicit, validation-only; see reason in config)",
        "- Manager and Worker keep their official assignment (OpenCode Go DeepSeek V4.1 Flash), which is "
        "unavailable on this account: scheduling falls back to the deterministic engine policy and repository "
        "exploration/log classification are skipped.", "",
        "## Progress report (generated from the live state database)", "", report.split("written to")[0], "",
        "## Traceability matrix", "", trace, "",
        "## Integration branch history", "", "```", integ.strip(), "```", "",
        "## Files on the integration branch", "", "```", files.strip(), "```", "",
        "## Command transcript (tail of each step)", "",
    ]
    for name, out in log:
        md += [f"### {name}", "```", out.strip()[-2500:], "```", ""]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(md))
    print(f"report written to {args.report}")


if __name__ == "__main__":
    main()
