from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from deepseek_infra.infra.diagnostics import release_manifest
from deepseek_infra.infra.diagnostics.evidence_assembly import (
    _validate_producer_directory,
    assemble_evidence,
    prepare_producer_artifact,
    validate_evidence_payload,
)
from deepseek_infra.infra.diagnostics.evidence_inventory import (
    evidence_paths,
    evidence_paths_for_producer,
    evidence_producers,
    evidence_spec_by_path,
    evidence_specs,
)
from deepseek_infra.infra.diagnostics.evidence_manifest import sha256_of, validate_manifest_checksum
from scripts import generate_release_evidence
from scripts.verify_release_package import verify_release_package

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
REVISION = "a" * 40


def _context() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "version": VERSION,
        "testedRevision": REVISION,
        "sourceTreeDirty": False,
        "capturedAt": "2026-07-22T12:00:00Z",
        "generator": "scripts/capture_evidence_context.py",
        "repository": "leizd/DeepSeek-Infra",
        "workflowRunId": "123",
        "workflowAttempt": 1,
        "eventName": "push",
        "ref": "refs/heads/main",
    }


def _payload(*, revision: str = REVISION, ci_revision: str = REVISION) -> dict[str, object]:
    return {
        "version": VERSION,
        "status": "PASS",
        "testedRevision": revision,
        "sourceRevision": revision,
        "sourceTreeDirty": False,
        "releaseRevision": None,
        "ciRevision": ci_revision,
        "sourceContext": _context(),
        "generatedAt": "2026-07-22T12:01:00Z",
    }


def _producer_downloads(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    downloads = tmp_path / "downloads"
    for spec in evidence_specs():
        path = source / spec.path(VERSION)
        path.parent.mkdir(parents=True, exist_ok=True)
        if spec.payload_kind == "proof":
            payload: dict[str, object] = {
                "schema": "evidence-proof-v2",
                "scenario": spec.scenario,
                "checks": {"assemblyExactProof": {"status": "PASS", "evidence": {"source": "test"}}},
                "meta": {"producer": spec.producer},
            }
        else:
            payload = _payload()
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    storage_specs = [spec for spec in evidence_specs() if spec.producer == "storage-control-plane-minio-e2e"]
    proof_spec = next(spec for spec in storage_specs if spec.payload_kind == "proof")
    report_spec = next(spec for spec in storage_specs if spec.payload_kind == "report")
    proof_path = source / proof_spec.path(VERSION)
    report_path = source / report_spec.path(VERSION)
    report = _payload()
    report["proofArtifact"] = {
        "path": proof_spec.path(VERSION),
        "sha256": sha256_of(proof_path),
        "bytes": proof_path.stat().st_size,
        "scenario": proof_spec.scenario,
    }
    report["requiredProofChecks"] = {proof_spec.scenario: ["assemblyExactProof"]}
    report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
    for producer in evidence_producers():
        prepare_producer_artifact(
            source,
            downloads / f"evidence-producer-{producer}",
            producer=producer,
            version=VERSION,
            context=_context(),
            github_sha=REVISION,
        )
    return source, downloads


def _assembled(tmp_path: Path) -> Path:
    _, downloads = _producer_downloads(tmp_path)
    output = tmp_path / "release-evidence"
    assemble_evidence(downloads, output, version=VERSION, context=_context(), github_sha=REVISION)
    return output


def test_inventory_has_unique_paths_and_release_manifest_uses_it() -> None:
    paths = evidence_paths(VERSION)
    assert len(paths) == len(set(paths))
    expected = (
        *paths,
        f"docs/evidence/evidence-source-context-v{VERSION}.json",
        f"docs/evidence/evidence-manifest-v{VERSION}.json",
        f"docs/evidence/evidence-manifest-v{VERSION}.json.sha256",
    )
    assert release_manifest.DEFAULT_EVIDENCE_PATHS == expected


def test_inventory_exposes_optional_evidence_without_promoting_it_to_ga() -> None:
    optional_path = f"docs/evidence/python-coverage-stability-v{VERSION}.json"
    assert optional_path not in evidence_paths(VERSION)
    assert optional_path in evidence_paths(VERSION, required_only=False)
    assert evidence_paths_for_producer("test", VERSION, required_only=False) == (optional_path,)
    assert evidence_spec_by_path(VERSION, required_only=False)[optional_path].tier == "optional"


def test_generator_has_no_post_hoc_provenance_stamping(tmp_path: Path) -> None:
    assert not hasattr(generate_release_evidence, "stamp_generated_evidence")
    owned = tuple(spec.path(VERSION) for spec in evidence_specs() if spec.producer == "release-readiness")
    for path_name in owned:
        path = tmp_path / path_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_payload()) + "\n", encoding="utf-8")
    rel = next(path for path in owned if path.startswith("docs/evidence/ga-"))
    stale = tmp_path / rel
    stale.write_text(json.dumps({"version": VERSION, "status": "PASS"}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Evidence"):
        generate_release_evidence.validate_generated_evidence(tmp_path, VERSION, _context())
    assert "testedRevision" not in json.loads(stale.read_text(encoding="utf-8"))


def test_producer_must_write_its_own_provenance(tmp_path: Path) -> None:
    spec = next(spec for spec in evidence_specs() if spec.producer == "frontend")
    path = tmp_path / spec.path(VERSION)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": VERSION, "status": "PASS"}), encoding="utf-8")
    with pytest.raises(ValueError, match="testedRevision"):
        prepare_producer_artifact(
            tmp_path,
            tmp_path / "staged",
            producer="frontend",
            version=VERSION,
            context=_context(),
            github_sha=REVISION,
        )


def test_assembly_rejects_missing_producer_and_artifact_collision(tmp_path: Path) -> None:
    _, downloads = _producer_downloads(tmp_path)
    missing_dir = downloads / "evidence-producer-rust-coverage"
    renamed = downloads / "held-rust-coverage"
    missing_dir.rename(renamed)
    with pytest.raises(ValueError, match="missing required producer Artifact"):
        assemble_evidence(downloads, tmp_path / "missing-output", version=VERSION, context=_context(), github_sha=REVISION)
    renamed.rename(missing_dir)

    foreign = next(spec for spec in evidence_specs() if spec.producer == "frontend-browser")
    target = downloads / "evidence-producer-frontend" / foreign.path(VERSION)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_payload()), encoding="utf-8")
    with pytest.raises(ValueError, match="ownership mismatch"):
        assemble_evidence(downloads, tmp_path / "collision-output", version=VERSION, context=_context(), github_sha=REVISION)


def test_storage_control_producer_rejects_missing_or_tampered_exact_proof(tmp_path: Path) -> None:
    _, downloads = _producer_downloads(tmp_path / "missing")
    producer = "storage-control-plane-minio-e2e"
    directory = downloads / f"evidence-producer-{producer}"
    proof_rel = f"docs/evidence/storage-control-plane-autonomous-proof-v{VERSION}.json"
    (directory / proof_rel).unlink()
    _, missing_errors = _validate_producer_directory(
        directory,
        producer=producer,
        version=VERSION,
        context=_context(),
        github_sha=REVISION,
    )
    assert any("producer artifact ownership mismatch" in error for error in missing_errors)
    assert any("producer exact proof pair missing" in error for error in missing_errors)

    _, tampered_downloads = _producer_downloads(tmp_path / "tampered")
    tampered_directory = tampered_downloads / f"evidence-producer-{producer}"
    report_path = tampered_directory / f"docs/evidence/storage-control-plane-minio-v{VERSION}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["proofArtifact"]["sha256"] = "0" * 64
    report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
    _, tampered_errors = _validate_producer_directory(
        tampered_directory,
        producer=producer,
        version=VERSION,
        context=_context(),
        github_sha=REVISION,
    )
    assert any("producer report proof checksum mismatch" in error for error in tampered_errors)


def test_assembly_rejects_revision_and_ci_mismatch(tmp_path: Path) -> None:
    source, _ = _producer_downloads(tmp_path)
    spec = next(spec for spec in evidence_specs() if spec.producer == "frontend")
    path = source / spec.path(VERSION)
    path.write_text(json.dumps(_payload(revision="b" * 40, ci_revision="c" * 40)), encoding="utf-8")
    with pytest.raises(ValueError, match="testedRevision"):
        prepare_producer_artifact(
            source,
            tmp_path / "bad-revision",
            producer="frontend",
            version=VERSION,
            context=_context(),
            github_sha=REVISION,
        )

    path.write_text(json.dumps(_payload(ci_revision="c" * 40)), encoding="utf-8")
    with pytest.raises(ValueError, match="ciRevision"):
        prepare_producer_artifact(
            source,
            tmp_path / "bad-ci-revision",
            producer="frontend",
            version=VERSION,
            context=_context(),
            github_sha=REVISION,
        )


def test_producer_payload_and_staging_guards_fail_closed(tmp_path: Path) -> None:
    spec = next(spec for spec in evidence_specs() if spec.producer == "frontend")
    payload_path = tmp_path / "payload.json"
    payload_path.write_text("not-json", encoding="utf-8")
    assert "invalid Evidence" in validate_evidence_payload(
        payload_path,
        spec,
        version=VERSION,
        context=_context(),
        github_sha=REVISION,
    )[0]

    payload_path.write_text("{}", encoding="utf-8")
    errors = validate_evidence_payload(payload_path, spec, version=VERSION, context=_context(), github_sha=REVISION)
    for fragment in ("version mismatch", "status is not PASS", "generatedAt missing", "ciRevision"):
        assert any(fragment in error for error in errors), fragment

    with pytest.raises(ValueError, match="unknown or empty"):
        prepare_producer_artifact(tmp_path, tmp_path / "unknown", producer="not-a-producer", version=VERSION, context=_context(), github_sha=REVISION)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="output already exists"):
        prepare_producer_artifact(tmp_path, existing, producer="frontend", version=VERSION, context=_context(), github_sha=REVISION)
    with pytest.raises(ValueError, match="missing owned Evidence"):
        prepare_producer_artifact(tmp_path, tmp_path / "missing", producer="frontend", version=VERSION, context=_context(), github_sha=REVISION)


def test_producer_descriptor_validation_reports_metadata_and_ownership_errors(tmp_path: Path) -> None:
    producer = "frontend"
    directory = tmp_path / "producer"
    paths, errors = _validate_producer_directory(
        directory,
        producer=producer,
        version=VERSION,
        context=_context(),
        github_sha=REVISION,
    )
    assert paths == [] and errors == [f"missing producer descriptor: {producer}"]

    directory.mkdir()
    (directory / "producer.json").write_text("[]", encoding="utf-8")
    _, errors = _validate_producer_directory(directory, producer=producer, version=VERSION, context=_context(), github_sha=REVISION)
    assert "invalid producer descriptor" in errors[0]

    source = tmp_path / "source"
    for spec in evidence_specs():
        if spec.producer == producer:
            path = source / spec.path(VERSION)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(_payload()) + "\n", encoding="utf-8")
    valid = tmp_path / "valid"
    prepare_producer_artifact(source, valid, producer=producer, version=VERSION, context=_context(), github_sha=REVISION)
    descriptor_path = valid / "producer.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor.update(producer="other", version="wrong", testedRevision="wrong", paths=[], sha256={})
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    extra = valid / "extra.json"
    extra.write_text("{}", encoding="utf-8")
    _, errors = _validate_producer_directory(valid, producer=producer, version=VERSION, context=_context(), github_sha=REVISION)
    for fragment in ("ownership mismatch", "version mismatch", "revision mismatch", "path inventory mismatch", "artifact ownership mismatch", "checksum mismatch"):
        assert any(fragment in error for error in errors), fragment


def test_assembly_writes_detached_checksum_and_rejects_tampering(tmp_path: Path) -> None:
    output = _assembled(tmp_path)
    preflight = json.loads((output / f"preflight-v{VERSION}.json").read_text(encoding="utf-8"))
    assert preflight["checks"]["assembledEvidenceIncludesExactAutonomousProof"] == "PASS"
    assert preflight["checks"]["missingAutonomousProofFailsAssembly"] == "PASS"
    manifest = output / "docs" / "evidence" / f"evidence-manifest-v{VERSION}.json"
    assert validate_manifest_checksum(manifest) == []
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert "detached checksum mismatch" in validate_manifest_checksum(manifest)[0]


def test_package_validation_rejects_evidence_modified_after_manifest(tmp_path: Path) -> None:
    output = _assembled(tmp_path)
    archive = tmp_path / "dist" / f"deepseek-infra-{VERSION}.zip"
    release_path = tmp_path / "release-manifest.json"
    _write_release_zip(output, archive)
    _release_manifest(output, archive, release_path)
    evidence = output / evidence_paths(VERSION)[0]
    evidence.write_text(evidence.read_text(encoding="utf-8") + " ", encoding="utf-8")

    errors = verify_release_package(archive, release_path, output, version=VERSION, expected_revision=REVISION)

    assert any("evidence checksum mismatch" in error for error in errors)


def _write_release_zip(output: Path, archive: Path, *, omit: str = "") -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for path in output.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(output).as_posix()
            if rel != omit:
                package.write(path, rel)


def _release_manifest(output: Path, archive: Path, target: Path) -> None:
    evidence_manifest = output / "docs" / "evidence" / f"evidence-manifest-v{VERSION}.json"
    evidence_data = json.loads(evidence_manifest.read_text(encoding="utf-8"))
    payload = release_manifest.build_manifest(
        version=VERSION,
        commit=REVISION,
        python_version="3.12",
        coverage_gate="95%",
        eval_report="evals/reports/latest.json",
        agent_report="evals/reports/agent-latest.json",
        artifact=archive,
        sha256=sha256_of(archive),
        evidence_manifest={
            "path": f"docs/evidence/evidence-manifest-v{VERSION}.json",
            "sha256": sha256_of(evidence_manifest),
            "artifactCount": len(evidence_data["artifacts"]),
            "testedRevision": REVISION,
        },
    )
    target.write_text(json.dumps(payload), encoding="utf-8")


def test_release_package_requires_complete_byte_identical_evidence(tmp_path: Path) -> None:
    output = _assembled(tmp_path)
    archive = tmp_path / "dist" / f"deepseek-infra-{VERSION}.zip"
    release_path = tmp_path / "release-manifest.json"
    _write_release_zip(output, archive)
    _release_manifest(output, archive, release_path)
    assert verify_release_package(
        archive,
        release_path,
        output,
        version=VERSION,
        expected_revision=REVISION,
    ) == []

    omitted = evidence_paths(VERSION)[0]
    incomplete = tmp_path / "dist" / "incomplete.zip"
    _write_release_zip(output, incomplete, omit=omitted)
    _release_manifest(output, incomplete, release_path)
    errors = verify_release_package(incomplete, release_path, output, version=VERSION, expected_revision=REVISION)
    assert any("missing required Evidence" in error for error in errors)

    external = output / evidence_paths(VERSION)[1]
    external.write_text(external.read_text(encoding="utf-8") + " ", encoding="utf-8")
    _release_manifest(output, archive, release_path)
    errors = verify_release_package(archive, release_path, output, version=VERSION, expected_revision=REVISION)
    assert any("differs from assembled Artifact" in error for error in errors)


def test_release_package_rejects_release_manifest_inventory_drift(tmp_path: Path) -> None:
    output = _assembled(tmp_path)
    archive = tmp_path / "dist" / f"deepseek-infra-{VERSION}.zip"
    release_path = tmp_path / "release-manifest.json"
    _write_release_zip(output, archive)
    _release_manifest(output, archive, release_path)
    manifest = json.loads(release_path.read_text(encoding="utf-8"))
    manifest["evidence"] = manifest["evidence"][:-1]
    release_path.write_text(json.dumps(manifest), encoding="utf-8")

    errors = verify_release_package(archive, release_path, output, version=VERSION, expected_revision=REVISION)

    assert "release manifest Evidence inventory mismatch" in errors
