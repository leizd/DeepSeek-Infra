from __future__ import annotations

import json
from pathlib import Path

from deepseek_infra.infra.diagnostics.evidence_manifest import (
    build_evidence_manifest,
    validate_evidence_manifest,
    write_evidence_manifest,
)


VERSION = "4.3.0"
REVISION = "candidate123"
EVIDENCE = "docs/evidence/sample-v4.3.0.json"


def _source_context() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "version": VERSION,
        "testedRevision": REVISION,
        "sourceTreeDirty": False,
        "capturedAt": "2026-07-22T10:00:00Z",
        "generator": "scripts/generate_release_evidence.py",
    }


def _write_evidence(root: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "version": VERSION,
        "status": "PASS",
        "testedRevision": REVISION,
        "sourceRevision": REVISION,
        "sourceTreeDirty": False,
        "releaseRevision": None,
        "ciRevision": None,
        "sourceContext": _source_context(),
    }
    payload.update(overrides)
    path = root / EVIDENCE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _write_manifest(root: Path, artifact_paths: list[str] | None = None) -> Path:
    manifest = build_evidence_manifest(
        root,
        version=VERSION,
        tested_revision=REVISION,
        artifact_paths=artifact_paths or [EVIDENCE],
        source_context=_source_context(),
    )
    path = root / "docs" / "evidence" / f"evidence-manifest-v{VERSION}.json"
    write_evidence_manifest(path, manifest)
    return path


def _validate(root: Path, *, github_sha: str | None = None) -> list[str]:
    return validate_evidence_manifest(
        root,
        version=VERSION,
        expected_revision=REVISION,
        required_paths=[EVIDENCE],
        github_sha=github_sha,
    )


def test_strict_provenance_accepts_one_clean_revision(tmp_path: Path) -> None:
    _write_evidence(tmp_path)
    _write_manifest(tmp_path)
    assert _validate(tmp_path) == []


def test_strict_provenance_rejects_modified_evidence_checksum(tmp_path: Path) -> None:
    path = _write_evidence(tmp_path)
    _write_manifest(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert any("checksum mismatch" in error for error in _validate(tmp_path))


def test_strict_provenance_rejects_revision_mismatches_and_unknown(tmp_path: Path) -> None:
    _write_evidence(tmp_path, sourceRevision="different")
    _write_manifest(tmp_path)
    assert any("sourceRevision mismatch" in error for error in _validate(tmp_path))

    _write_evidence(tmp_path, testedRevision="unknown", sourceRevision="unknown")
    _write_manifest(tmp_path)
    assert any("testedRevision is unknown" in error for error in _validate(tmp_path))


def test_strict_provenance_binds_ci_revision_to_github_sha(tmp_path: Path) -> None:
    _write_evidence(tmp_path, ciRevision="wrong")
    _write_manifest(tmp_path)
    assert any("ciRevision does not match" in error for error in _validate(tmp_path, github_sha=REVISION))


def test_strict_provenance_rejects_missing_and_duplicate_entries(tmp_path: Path) -> None:
    _write_evidence(tmp_path)
    path = _write_manifest(tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["artifacts"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert any("required evidence missing" in error for error in _validate(tmp_path))

    manifest = build_evidence_manifest(
        tmp_path,
        version=VERSION,
        tested_revision=REVISION,
        artifact_paths=[EVIDENCE, EVIDENCE],
        source_context=_source_context(),
    )
    write_evidence_manifest(path, manifest)
    assert any("duplicate evidence manifest path" in error for error in _validate(tmp_path))
