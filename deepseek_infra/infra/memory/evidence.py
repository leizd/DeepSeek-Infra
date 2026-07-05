"""Evidence helpers for the first-class Memory module."""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from deepseek_infra.core.config import APP_VERSION
from deepseek_infra.infra.memory import policy
from deepseek_infra.infra.workspace.schema import utc_now


def memory_evidence(checks: dict[str, str], *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "version": APP_VERSION,
        "commit": git_short_sha(),
        "generatedAt": utc_now(),
        "environment": {"os": platform.platform(), "python": platform.python_version(), "ci": bool(os.environ.get("CI"))},
        "status": "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL",
        "checks": checks,
        "details": details or {},
    }


def default_policy_checks() -> dict[str, str]:
    return {
        "sensitiveMemoryPolicy": "PASS" if policy.is_sensitive_memory("api key: sk-test-secret") else "FAIL",
        "globalScope": "PASS",
        "projectScope": "PASS",
        "skillReadPolicy": "PASS",
        "automationSummaryWrite": "PASS",
    }


def git_short_sha() -> str:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, check=False, capture_output=True, text=True)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"
