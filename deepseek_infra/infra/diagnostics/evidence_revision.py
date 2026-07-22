"""Shared revision metadata for generated evidence files.

Locally generated evidence can only describe the source tree it was produced
from — never the release commit it will ship in, because that commit does not
exist yet when the generator runs (the classic self-photograph problem). To
keep the semantics honest, evidence files carry:

- ``sourceRevision``: the working tree HEAD the generator ran against;
- ``sourceTreeDirty``: whether the tree had uncommitted changes at that time;
- ``releaseRevision``: reserved for an external publication record, ``None``
  for producer Evidence;
- ``ciRevision``: the ``GITHUB_SHA`` of the CI run, ``None`` outside Actions.

The workflow captures one schema-v2 context before any producer starts and
sets ``DEEPSEEK_EVIDENCE_SOURCE_CONTEXT`` in every producer job. Each report
writes that immutable provenance itself; assembly only validates and copies
bytes and never stamps or rewrites producer Evidence.
"""

from __future__ import annotations

import json
import os
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


def validate_source_context(
    data: dict[str, Any],
    *,
    version: str | None = None,
    expected_revision: str | None = None,
) -> list[str]:
    errors: list[str] = []
    schema_version = data.get("schemaVersion")
    required = ("version", "testedRevision", "sourceTreeDirty", "capturedAt", "generator")
    for key in required:
        if key not in data:
            errors.append(f"source context missing {key}")
    if schema_version not in (1, 2):
        errors.append("source context schemaVersion must be 1 or 2")
    if data.get("sourceTreeDirty") is not False:
        errors.append("source context must describe a clean tree")
    revision = data.get("testedRevision")
    if not isinstance(revision, str) or not revision or revision == "unknown":
        errors.append("source context testedRevision must be known")
    if version is not None and data.get("version") != version:
        errors.append("source context version mismatch")
    if expected_revision is not None and revision != expected_revision:
        errors.append("source context testedRevision mismatch")
    if schema_version == 2:
        for key in ("repository", "workflowRunId", "workflowAttempt", "eventName", "ref"):
            if not data.get(key):
                errors.append(f"source context missing {key}")
    return errors


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
    errors = validate_source_context(data)
    if errors:
        raise ValueError(f"invalid evidence source context {target}: {'; '.join(errors)}")
    return data


def capture_source_context(
    root: Path,
    version: str,
    *,
    generator: str,
    schema_version: int = 2,
) -> dict[str, Any]:
    repo = root.resolve()
    head_revision = _git(repo, "rev-parse", "HEAD") or "unknown"
    ci_revision = os.environ.get("GITHUB_SHA", "").strip()
    if ci_revision and head_revision != ci_revision:
        raise ValueError("GITHUB_SHA does not match the checked-out Git HEAD")
    revision = ci_revision or head_revision
    dirty = bool(_git(repo, "status", "--porcelain"))
    if dirty:
        raise ValueError("release evidence requires a clean source tree")
    if revision == "unknown":
        raise ValueError("release evidence requires a known Git HEAD")
    context: dict[str, Any] = {
        "schemaVersion": schema_version,
        "version": version,
        "testedRevision": revision,
        "sourceTreeDirty": False,
        "capturedAt": _utc_now(),
        "generator": generator,
    }
    if schema_version == 2:
        context.update(
            repository=os.environ.get("GITHUB_REPOSITORY") or "local",
            workflowRunId=os.environ.get("GITHUB_RUN_ID") or "local",
            workflowAttempt=int(os.environ.get("GITHUB_RUN_ATTEMPT") or "1"),
            eventName=os.environ.get("GITHUB_EVENT_NAME") or "local",
            ref=os.environ.get("GITHUB_REF") or _git(repo, "symbolic-ref", "-q", "HEAD") or "local",
        )
    errors = validate_source_context(context, version=version, expected_revision=revision)
    if errors:
        raise ValueError("invalid captured evidence source context: " + "; ".join(errors))
    return context


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
