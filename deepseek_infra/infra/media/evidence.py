"""Evidence metadata helpers for media smoke and eval reports."""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision, load_source_context
from deepseek_infra.infra.workspace.schema import utc_now


def evidence_metadata(version: str, *, status: str, checks: dict[str, str], details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": version,
        "commit": git_short_sha(),
        **evidence_revision(),
        "generatedAt": utc_now(),
        "environment": {"os": platform.platform(), "python": platform.python_version(), "ci": bool(os.environ.get("CI"))},
        "status": status,
        "checks": checks,
    }
    if details is not None:
        payload["details"] = details
    return payload


def git_short_sha() -> str:
    context = load_source_context()
    if context is not None:
        return str(context["testedRevision"])
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=Path(__file__).resolve().parents[3],
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"
