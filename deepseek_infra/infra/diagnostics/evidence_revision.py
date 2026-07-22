"""Shared revision metadata for generated evidence files.

Locally generated evidence can only describe the source tree it was produced
from — never the release commit it will ship in, because that commit does not
exist yet when the generator runs (the classic self-photograph problem). To
keep the semantics honest, evidence files carry:

- ``sourceRevision``: the working tree HEAD the generator ran against;
- ``sourceTreeDirty``: whether the tree had uncommitted changes at that time;
- ``releaseRevision``: stamped later by a post-release job, ``None`` locally;
- ``ciRevision``: the ``GITHUB_SHA`` of the CI run, ``None`` outside Actions.

Release-candidate generation sets ``DEEPSEEK_EVIDENCE_SOURCE_CONTEXT``. Every
report then reads the same immutable source snapshot instead of inspecting a
working tree that becomes dirty as evidence files are written.
"""

from __future__ import annotations

import os
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_SOURCE_CONTEXT_ENV = "DEEPSEEK_EVIDENCE_SOURCE_CONTEXT"


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def source_context_path() -> Path | None:
    value = os.environ.get(EVIDENCE_SOURCE_CONTEXT_ENV, "").strip()
    return Path(value).resolve() if value else None


def load_source_context(path: Path | None = None) -> dict[str, Any] | None:
    target = path or source_context_path()
    if target is None:
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid evidence source context {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid evidence source context {target}: expected a JSON object")
    required = ("schemaVersion", "version", "testedRevision", "sourceTreeDirty", "capturedAt", "generator")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"invalid evidence source context {target}: missing {', '.join(missing)}")
    if data.get("schemaVersion") != 1 or data.get("sourceTreeDirty") is not False:
        raise ValueError(f"invalid evidence source context {target}: source snapshot must be clean schema v1")
    revision = data.get("testedRevision")
    if not isinstance(revision, str) or not revision or revision == "unknown":
        raise ValueError(f"invalid evidence source context {target}: testedRevision must be known")
    return data


def capture_source_context(root: Path, version: str, *, generator: str) -> dict[str, Any]:
    repo = root.resolve()
    revision = _git(repo, "rev-parse", "HEAD") or "unknown"
    dirty = bool(_git(repo, "status", "--porcelain"))
    if dirty:
        raise ValueError("release evidence requires a clean source tree")
    if revision == "unknown":
        raise ValueError("release evidence requires a known Git HEAD")
    return {
        "schemaVersion": 1,
        "version": version,
        "testedRevision": revision,
        "sourceTreeDirty": False,
        "capturedAt": _utc_now(),
        "generator": generator,
    }


def revision_from_context(context: dict[str, Any]) -> dict[str, Any]:
    revision = str(context["testedRevision"])
    return {
        "testedRevision": revision,
        "sourceRevision": revision,
        "sourceTreeDirty": False,
        "releaseRevision": None,
        "ciRevision": os.environ.get("GITHUB_SHA") or None,
        "sourceContext": dict(context),
    }


def evidence_revision(root: Path | None = None) -> dict[str, Any]:
    """Return revision metadata, preferring the shared release source context."""
    context = load_source_context()
    if context is not None:
        return revision_from_context(context)
    repo = (root or ROOT).resolve()
    revision = _git(repo, "rev-parse", "HEAD") or "unknown"
    return {
        "testedRevision": revision,
        "sourceRevision": revision,
        "sourceTreeDirty": bool(_git(repo, "status", "--porcelain")),
        "releaseRevision": None,
        "ciRevision": os.environ.get("GITHUB_SHA") or None,
    }


def evidence_revision_present(data: dict[str, Any]) -> bool:
    """Accept both the new revision block and the legacy ``commit`` field."""
    return bool(data.get("testedRevision") or data.get("sourceRevision") or data.get("commit"))
