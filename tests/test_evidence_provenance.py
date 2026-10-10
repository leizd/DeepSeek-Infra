from __future__ import annotations

import json
from pathlib import Path

from deepseek_infra.infra.diagnostics.evidence_manifest import (
    build_evidence_manifest,
    manifest_checksum_path,
    validate_evidence_manifest,
    validate_manifest_checksum,
    write_evidence_manifest,
    write_manifest_checksum,
)


VERSION = "4.3.6"
REVISION = "candidate123"
EVIDENCE = "docs/evidence/sample-v4.3.6.json"


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


def test_detached_manifest_checksum_fails_closed(tmp_path: Path) -> None:
    manifest = tmp_path / "evidence-manifest.json"
    assert validate_manifest_checksum(manifest) == [f"missing evidence manifest: {manifest}"]

    manifest.write_text("{}\n", encoding="utf-8")
    checksum = manifest_checksum_path(manifest)
    assert validate_manifest_checksum(manifest) == [f"missing detached evidence manifest checksum: {checksum}"]

    checksum.write_text("invalid\n", encoding="utf-8")
    assert validate_manifest_checksum(manifest) == ["invalid detached evidence manifest checksum format"]
    checksum.write_text(f"{'0' * 64}  {manifest.name}\n", encoding="utf-8")
    assert validate_manifest_checksum(manifest) == ["evidence manifest detached checksum mismatch"]
    assert write_manifest_checksum(manifest) == checksum
    assert validate_manifest_checksum(manifest) == []


def test_manifest_validation_rejects_invalid_top_level_state(tmp_path: Path) -> None:
    missing = validate_evidence_manifest(
        tmp_path,
        version=VERSION,
        expected_revision=REVISION,
        required_paths=[EVIDENCE],
    )
    assert missing and "missing evidence manifest" in missing[0]

    path = tmp_path / "docs" / "evidence" / f"evidence-manifest-v{VERSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json", encoding="utf-8")
    assert "invalid evidence manifest" in _validate(tmp_path)[0]

    path.write_text("[]", encoding="utf-8")
    assert "must contain a JSON object" in _validate(tmp_path)[0]

    path.write_text(
        json.dumps(
            {
                "schemaVersion": 99,
                "version": "wrong",
                "testedRevision": "wrong",
                "sourceTreeDirty": True,
                "artifacts": "invalid",
            }
        ),
        encoding="utf-8",
    )
    errors = validate_evidence_manifest(
        tmp_path,
        version=VERSION,
        expected_revision="unknown",
        required_paths=[EVIDENCE],
    )
    assert any("schemaVersion" in error for error in errors)
    assert any("version" in error for error in errors)
    assert any("candidate revision must be known" in error for error in errors)
    assert any("testedRevision" in error for error in errors)
    assert any("sourceTreeDirty" in error for error in errors)
    assert any("sourceContext" in error for error in errors)
    assert any("artifacts must be a list" in error for error in errors)


def test_manifest_validation_reports_every_artifact_integrity_failure(tmp_path: Path) -> None:
    evidence = _write_evidence(tmp_path)
    manifest_path = _write_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"].insert(0, "invalid")
    manifest["artifacts"].append({"path": "docs/evidence/missing.json", "sha256": "x", "bytes": 1, "status": "PASS"})
    entry = manifest["artifacts"][1]
    entry.update(sha256="wrong", bytes=-1, status="FAIL")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    evidence.write_text(
        json.dumps(
            {
                "version": "wrong",
                "status": "FAIL",
                "testedRevision": "different",
                "sourceRevision": "other",
                "sourceTreeDirty": True,
                "sourceContext": {"different": True},
            }
        ),
        encoding="utf-8",
    )

    errors = _validate(tmp_path, github_sha=REVISION)
    expected_fragments = (
        "artifact 0 is invalid",
        "references missing file",
        "checksum mismatch",
        "byte size mismatch",
        "status is not PASS",
        "version mismatch",
        "testedRevision mismatch",
        "sourceRevision mismatch",
        "sourceTreeDirty is not false",
        "sourceContext mismatch",
        "ciRevision does not match",
    )
    for fragment in expected_fragments:
        assert any(fragment in error for error in errors), fragment


def test_manifest_validation_rejects_unreadable_evidence_payload(tmp_path: Path) -> None:
    evidence = _write_evidence(tmp_path)
    _write_manifest(tmp_path)
    evidence.write_text("[]", encoding="utf-8")
    assert any("invalid evidence file" in error for error in _validate(tmp_path))
