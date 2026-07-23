"""Checksummed release-evidence manifest helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Sequence

from deepseek_infra.infra.diagnostics.evidence_inventory import (
    evidence_paths,
    evidence_spec_by_path,
)
from deepseek_infra.infra.diagnostics.evidence_revision import validate_source_context


def required_evidence_paths(version: str) -> tuple[str, ...]:
    """Compatibility name backed by the centralized Evidence inventory."""
    return evidence_paths(version)


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
    specs = evidence_spec_by_path(version)
    for rel in artifact_paths:
        path = root / rel
        data = _load_object(path)
        entry = {
            "path": rel,
            "sha256": sha256_of(path),
            "bytes": path.stat().st_size,
            "status": data.get("status"),
        }
        spec = specs.get(rel)
        if spec is not None:
            entry.update(producer=spec.producer, tier=spec.tier)
        artifacts.append(entry)
    manifest = {
        "schemaVersion": 2,
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


def manifest_checksum_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


def write_manifest_checksum(path: Path) -> Path:
    target = manifest_checksum_path(path)
    target.write_text(f"{sha256_of(path)}  {path.name}\n", encoding="utf-8")
    return target


def validate_manifest_checksum(path: Path, checksum_path: Path | None = None) -> list[str]:
    target = checksum_path or manifest_checksum_path(path)
    if not path.is_file():
        return [f"missing evidence manifest: {path}"]
    if not target.is_file():
        return [f"missing detached evidence manifest checksum: {target}"]
    fields = target.read_text(encoding="utf-8").strip().split()
    if len(fields) < 2 or fields[1] != path.name:
        return ["invalid detached evidence manifest checksum format"]
    if fields[0].lower() != sha256_of(path).lower():
        return ["evidence manifest detached checksum mismatch"]
    return []


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
    if manifest.get("schemaVersion") not in (1, 2):
        errors.append("evidence manifest schemaVersion must be 1 or 2")
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
        errors.extend(validate_source_context(source_context, version=version, expected_revision=expected_revision))

    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list):
        return [*errors, "evidence manifest artifacts must be a list"]
    entries: dict[str, dict[str, Any]] = {}
    specs = evidence_spec_by_path(version)
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
        spec = specs.get(rel)
        if spec is not None:
            if entry.get("producer") != spec.producer:
                errors.append(f"evidence producer mismatch: {rel}")
            if entry.get("tier") != spec.tier:
                errors.append(f"evidence tier mismatch: {rel}")
        if github_sha and data.get("ciRevision") != github_sha:
            errors.append(f"evidence ciRevision does not match GITHUB_SHA: {rel}")
    return errors
