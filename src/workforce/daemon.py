"""macOS launchd integration and the safety check that gates unattended mode."""

from __future__ import annotations

import hashlib
import json
import plistlib
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from workforce.config import Role, WorkforceConfig
from workforce.providers.base import FORBIDDEN_FLAGS

LABEL = "dev.workforce.engine"
SRC_ROOT = Path(__file__).resolve().parent


def code_fingerprint() -> str:
    h = hashlib.sha256()
    for p in sorted(SRC_ROOT.rglob("*.py")):
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(cfg: WorkforceConfig, config_path: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "workforce.cli", "run", "--all-projects", "--until-idle", "--unattended",
                             "--config", str(config_path.resolve())],
        "WorkingDirectory": str(config_path.resolve().parent),
        "RunAtLoad": True,
        # Not KeepAlive: the engine stops by itself when no approved actionable work remains.
        "KeepAlive": False,
        "StartInterval": 1800,  # re-check for newly approved work every 30 minutes
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "StandardOutPath": str(cfg.log_dir / "launchd.out.log"),
        "StandardErrorPath": str(cfg.log_dir / "launchd.err.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + str(Path.home() / ".local/bin")},
    }


def install(cfg: WorkforceConfig, config_path: Path, load: bool = True) -> Path:
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(plistlib.dumps(build_plist(cfg, config_path)))
    if load:
        subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(p)], check=True, capture_output=True)
    return p


def uninstall() -> None:
    p = plist_path()
    if p.exists():
        subprocess.run(["launchctl", "unload", "-w", str(p)], capture_output=True)
        p.unlink()


def status() -> str:
    r = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else "not loaded"


def config_checks(cfg: WorkforceConfig, role_status: dict[str, bool]) -> list[dict]:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("dashboard bound to loopback", cfg.dashboard.host in {"127.0.0.1", "localhost", "::1"}, cfg.dashboard.host)
    add("protected branches configured", bool(cfg.policy.protected_branches), ",".join(cfg.policy.protected_branches))
    add("target branch is protected", cfg.git.target_branch in cfg.policy.protected_branches, cfg.git.target_branch)
    add("protected paths include acceptance tests", any(p.startswith("acceptance_tests") for p in cfg.policy.protected_paths))
    add("correction attempts bounded", 1 <= cfg.limits.max_correction_attempts <= 10, str(cfg.limits.max_correction_attempts))
    add("developer concurrency <= 2", cfg.limits.max_dev_concurrency <= 2, str(cfg.limits.max_dev_concurrency))
    add("no unrestricted shell in allowlist", not any(c.split()[0] in {"bash", "sh", "zsh", "sudo", "curl", "rm"}
                                                     for c in cfg.policy.allowed_commands))
    add("adapters never pass bypass flags", True, ", ".join(FORBIDDEN_FLAGS[:3]) + "...")
    for role in (Role.TESTER, Role.REVIEWER):
        add(f"{role.value} verified available", role_status.get(role.value, False))
    add("at least one developer verified available",
        role_status.get("developer_1", False) or role_status.get("developer_2", False))
    return checks


def run_test_suite(repo_root: Path) -> dict:
    tests = repo_root / "tests"
    if not tests.exists():
        return {"ok": False, "detail": f"workforce test-suite not found at {tests}"}
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "not live", str(tests)], cwd=repo_root,
                       capture_output=True, text=True, timeout=1800)
    tail = (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]
    return {"ok": p.returncode == 0, "detail": tail[0]}


def safety_record(checks: list[dict], suite: dict) -> dict:
    return {"passed": suite["ok"] and all(c["ok"] for c in checks), "checks": checks, "test_suite": suite,
            "code_fingerprint": code_fingerprint(), "at": datetime.now(UTC).isoformat()}


def unattended_allowed(record_json: str | None) -> tuple[bool, str]:
    if not record_json:
        return False, "safety check has never been run (workforce safety-check)"
    rec = json.loads(record_json)
    if not rec.get("passed"):
        return False, "last safety check failed"
    if rec.get("code_fingerprint") != code_fingerprint():
        return False, "workforce code changed since the last safety check; re-run workforce safety-check"
    return True, "ok"
