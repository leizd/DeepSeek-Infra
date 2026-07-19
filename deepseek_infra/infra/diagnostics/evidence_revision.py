"""Shared revision metadata for generated evidence files.

Locally generated evidence can only describe the source tree it was produced
from — never the release commit it will ship in, because that commit does not
exist yet when the generator runs (the classic self-photograph problem). To
keep the semantics honest, evidence files carry:

- ``sourceRevision``: the working tree HEAD the generator ran against;
- ``sourceTreeDirty``: whether the tree had uncommitted changes at that time;
- ``releaseRevision``: stamped later by a post-release job, ``None`` locally;
- ``ciRevision``: the ``GITHUB_SHA`` of the CI run, ``None`` outside Actions.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def evidence_revision(root: Path | None = None) -> dict[str, Any]:
    """Return the revision block for a locally generated evidence payload."""
    repo = (root or ROOT).resolve()
    return {
        "sourceRevision": _git(repo, "rev-parse", "HEAD") or "unknown",
        "sourceTreeDirty": bool(_git(repo, "status", "--porcelain")),
        "releaseRevision": None,
        "ciRevision": os.environ.get("GITHUB_SHA") or None,
    }


def evidence_revision_present(data: dict[str, Any]) -> bool:
    """Accept both the new revision block and the legacy ``commit`` field."""
    return bool(data.get("sourceRevision") or data.get("commit"))
