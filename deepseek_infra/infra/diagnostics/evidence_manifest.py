"""Checksummed release-evidence manifest helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Sequence


REQUIRED_EVIDENCE_TEMPLATES = (
    "docs/evidence/headless-mcp-bridge.json",
    "docs/evidence/a2a-external-peer.json",
    "docs/evidence/ga-v{version}.json",
    "docs/evidence/workspace-v{version}.json",
    "docs/evidence/edge-router-v{version}.json",
    "docs/evidence/media-v{version}.json",
    "docs/evidence/browser-v{version}.json",
    "docs/evidence/frontend-browser-v{version}.json",
    "docs/evidence/frontend-bundle-v{version}.json",
    "docs/evidence/automation-v{version}.json",
    "docs/evidence/skills-v{version}.json",
    "docs/evidence/skills-ui-v{version}.json",
    "docs/evidence/skill-builder-v{version}.json",
    "docs/evidence/skill-packs-v{version}.json",
    "docs/evidence/skill-eval-dashboard-v{version}.json",
    "docs/evidence/skill-versioning-v{version}.json",
    "docs/evidence/skill-analytics-v{version}.json",
    "docs/evidence/skill-security-v{version}.json",
    "docs/evidence/skill-catalog-v{version}.json",
    "docs/evidence/context-taint-v{version}.json",
    "docs/evidence/upgrade-rollback-v{version}.json",
    "docs/evidence/protocol-contract-v{version}.json",
    "evals/reports/latest.json",
    "evals/reports/agent-latest.json",
    "evals/reports/baseline-compare-latest.json",
    "evals/reports/security-latest.json",
    "evals/reports/media-v{version}.json",
    "evals/reports/browser-v{version}.json",
    "evals/reports/automation-v{version}.json",
    "evals/reports/skills-v{version}.json",
)


def required_evidence_paths(version: str) -> tuple[str, ...]:
    return tuple(template.format(version=version) for template in REQUIRED_EVIDENCE_TEMPLATES)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def build_evidence_manifest(
    root: Path,
    *,
    version: str,
    tested_revision: str,
    artifact_paths: Sequence[str],
    source_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    for rel in artifact_paths:
        path = root / rel
        data = _load_object(path)
        artifacts.append(
            {
                "path": rel,
                "sha256": sha256_of(path),
                "bytes": path.stat().st_size,
                "status": data.get("status"),
            }
        )
    manifest = {
        "schemaVersion": 1,
        "version": version,
        "testedRevision": tested_revision,
        "sourceTreeDirty": False,
        "artifacts": artifacts,
    }
    if source_context is not None:
        manifest["sourceContext"] = dict(source_context)
    return manifest


def write_evidence_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_evidence_manifest(
    root: Path,
    *,
    version: str,
    expected_revision: str,
    required_paths: Iterable[str] | None = None,
    github_sha: str | None = None,
) -> list[str]:
    manifest_path = root / "docs" / "evidence" / f"evidence-manifest-v{version}.json"
    if not manifest_path.is_file():
        return [f"missing evidence manifest: {manifest_path.relative_to(root).as_posix()}"]
    try:
        manifest = _load_object(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"invalid evidence manifest: {exc}"]

    errors: list[str] = []
    if manifest.get("schemaVersion") != 1:
        errors.append("evidence manifest schemaVersion must be 1")
    if manifest.get("version") != version:
        errors.append(f"evidence manifest version {manifest.get('version')!r} does not match {version!r}")
    if expected_revision == "unknown" or not expected_revision:
        errors.append("expected candidate revision must be known")
    if manifest.get("testedRevision") != expected_revision:
        errors.append("evidence manifest testedRevision does not match the expected candidate revision")
    if manifest.get("sourceTreeDirty") is not False:
        errors.append("evidence manifest sourceTreeDirty must be false")
    source_context = manifest.get("sourceContext")
    if not isinstance(source_context, dict):
        errors.append("evidence manifest sourceContext must be present")
        source_context = {}
    else:
        if source_context.get("version") != version:
            errors.append("evidence manifest sourceContext version mismatch")
        if source_context.get("testedRevision") != expected_revision:
            errors.append("evidence manifest sourceContext revision mismatch")
        if source_context.get("sourceTreeDirty") is not False:
            errors.append("evidence manifest sourceContext must describe a clean tree")
        for key in ("capturedAt", "generator"):
            if not source_context.get(key):
                errors.append(f"evidence manifest sourceContext missing {key}")

    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list):
        return [*errors, "evidence manifest artifacts must be a list"]
    entries: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_artifacts):
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            errors.append(f"evidence manifest artifact {index} is invalid")
            continue
        rel = str(raw["path"])
        if rel in entries:
            errors.append(f"duplicate evidence manifest path: {rel}")
            continue
        entries[rel] = raw

    for rel in required_paths or required_evidence_paths(version):
        if rel not in entries:
            errors.append(f"required evidence missing from manifest: {rel}")

    for rel, entry in entries.items():
        path = root / rel
        if not path.is_file():
            errors.append(f"evidence manifest references missing file: {rel}")
            continue
        try:
            data = _load_object(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"invalid evidence file {rel}: {exc}")
            continue
        if entry.get("sha256") != sha256_of(path):
            errors.append(f"evidence checksum mismatch: {rel}")
        if entry.get("bytes") != path.stat().st_size:
            errors.append(f"evidence byte size mismatch: {rel}")
        if entry.get("status") != "PASS" or data.get("status") != "PASS":
            errors.append(f"evidence status is not PASS: {rel}")
        if data.get("version") != version:
            errors.append(f"evidence version mismatch: {rel}")
        tested_revision = data.get("testedRevision")
        if not tested_revision or tested_revision == "unknown":
            errors.append(f"evidence testedRevision is unknown: {rel}")
        elif tested_revision != expected_revision:
            errors.append(f"evidence testedRevision mismatch: {rel}")
        source_revision = data.get("sourceRevision")
        if source_revision is not None and source_revision != tested_revision:
            errors.append(f"evidence sourceRevision mismatch: {rel}")
        if data.get("sourceTreeDirty") is not False:
            errors.append(f"evidence sourceTreeDirty is not false: {rel}")
        if data.get("sourceContext") != source_context:
            errors.append(f"evidence sourceContext mismatch: {rel}")
        if github_sha and data.get("ciRevision") != github_sha:
            errors.append(f"evidence ciRevision does not match GITHUB_SHA: {rel}")
    return errors
