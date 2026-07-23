"""Validate producer ownership and assemble exact-merge release Evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from deepseek_infra.infra.diagnostics.evidence_inventory import (
    EvidenceSpec,
    evidence_paths,
    evidence_producers,
    evidence_specs_for_producer,
)
from deepseek_infra.infra.diagnostics.evidence_manifest import (
    build_evidence_manifest,
    sha256_of,
    write_evidence_manifest,
    write_manifest_checksum,
)
from deepseek_infra.infra.diagnostics.evidence_revision import validate_source_context

PRODUCER_DESCRIPTOR = "producer.json"


def _load_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def validate_evidence_payload(
    path: Path,
    spec: EvidenceSpec,
    *,
    version: str,
    context: dict[str, Any],
    github_sha: str | None,
) -> list[str]:
    rel = spec.path(version)
    try:
        data = _load_object(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"invalid Evidence {rel}: {exc}"]
    errors: list[str] = []
    revision = context.get("testedRevision")
    if data.get("version") != version:
        errors.append(f"Evidence version mismatch: {rel}")
    if data.get("status") != "PASS":
        errors.append(f"Evidence status is not PASS: {rel}")
    if data.get("testedRevision") != revision:
        errors.append(f"Evidence testedRevision mismatch: {rel}")
    if data.get("sourceRevision") != revision:
        errors.append(f"Evidence sourceRevision mismatch: {rel}")
    if data.get("sourceTreeDirty") is not False:
        errors.append(f"Evidence sourceTreeDirty is not false: {rel}")
    if data.get("sourceContext") != context:
        errors.append(f"Evidence sourceContext mismatch: {rel}")
    if not data.get("generatedAt"):
        errors.append(f"Evidence generatedAt missing: {rel}")
    if github_sha and data.get("ciRevision") != github_sha:
        errors.append(f"Evidence ciRevision does not match GITHUB_SHA: {rel}")
    return errors


def prepare_producer_artifact(
    source_root: Path,
    output_root: Path,
    *,
    producer: str,
    version: str,
    context: dict[str, Any],
    github_sha: str | None,
) -> tuple[str, ...]:
    specs = evidence_specs_for_producer(producer)
    if not specs:
        raise ValueError(f"unknown or empty Evidence producer: {producer}")
    if output_root.exists():
        raise ValueError(f"producer artifact output already exists: {output_root}")
    errors: list[str] = []
    for spec in specs:
        path = source_root / spec.path(version)
        if not path.is_file():
            errors.append(f"producer {producer} missing owned Evidence: {spec.path(version)}")
            continue
        errors.extend(validate_evidence_payload(path, spec, version=version, context=context, github_sha=github_sha))
    if errors:
        raise ValueError("; ".join(errors))

    paths: list[str] = []
    for spec in specs:
        rel = spec.path(version)
        source = source_root / rel
        destination = output_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        paths.append(rel)
    descriptor = {
        "schemaVersion": 1,
        "producer": producer,
        "version": version,
        "testedRevision": context["testedRevision"],
        "paths": paths,
        "sha256": {rel: sha256_of(output_root / rel) for rel in paths},
    }
    (output_root / PRODUCER_DESCRIPTOR).write_text(
        json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return tuple(paths)


def _validate_producer_directory(
    directory: Path,
    *,
    producer: str,
    version: str,
    context: dict[str, Any],
    github_sha: str,
) -> tuple[list[str], list[str]]:
    specs = evidence_specs_for_producer(producer)
    expected = {spec.path(version): spec for spec in specs}
    descriptor_path = directory / PRODUCER_DESCRIPTOR
    errors: list[str] = []
    if not descriptor_path.is_file():
        return [], [f"missing producer descriptor: {producer}"]
    try:
        descriptor = _load_object(descriptor_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [], [f"invalid producer descriptor {producer}: {exc}"]
    if descriptor.get("producer") != producer:
        errors.append(f"producer descriptor ownership mismatch: {producer}")
    if descriptor.get("version") != version:
        errors.append(f"producer descriptor version mismatch: {producer}")
    if descriptor.get("testedRevision") != context.get("testedRevision"):
        errors.append(f"producer descriptor revision mismatch: {producer}")
    declared = descriptor.get("paths")
    declared_paths = set(declared) if isinstance(declared, list) and all(isinstance(item, str) for item in declared) else set()
    if declared_paths != set(expected):
        errors.append(f"producer descriptor path inventory mismatch: {producer}")
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path.name != PRODUCER_DESCRIPTOR
    }
    if actual != set(expected):
        extra = sorted(actual - set(expected))
        missing = sorted(set(expected) - actual)
        errors.append(f"producer artifact ownership mismatch {producer}: extra={extra}, missing={missing}")
    hashes = descriptor.get("sha256")
    hash_map = hashes if isinstance(hashes, dict) else {}
    for rel, spec in expected.items():
        path = directory / rel
        if not path.is_file():
            continue
        if hash_map.get(rel) != sha256_of(path):
            errors.append(f"producer descriptor checksum mismatch: {rel}")
        errors.extend(validate_evidence_payload(path, spec, version=version, context=context, github_sha=github_sha))
    return sorted(actual), errors


def assemble_evidence(
    downloads_root: Path,
    output_root: Path,
    *,
    version: str,
    context: dict[str, Any],
    github_sha: str,
) -> Path:
    if output_root.exists():
        raise ValueError(f"Evidence assembly output already exists: {output_root}")
    if not github_sha or github_sha == "unknown":
        raise ValueError("exact-merge Evidence assembly requires GITHUB_SHA")
    context_errors = validate_source_context(context, version=version, expected_revision=github_sha)
    if context.get("schemaVersion") != 2:
        context_errors.append("exact-merge Evidence requires schema-v2 source context")
    if context_errors:
        raise ValueError("; ".join(context_errors))

    paths_to_source: dict[str, Path] = {}
    errors: list[str] = []
    for producer in evidence_producers():
        directory = downloads_root / f"evidence-producer-{producer}"
        if not directory.is_dir():
            errors.append(f"missing required producer Artifact: {producer}")
            continue
        paths, producer_errors = _validate_producer_directory(
            directory,
            producer=producer,
            version=version,
            context=context,
            github_sha=github_sha,
        )
        errors.extend(producer_errors)
        for rel in paths:
            if rel in paths_to_source:
                errors.append(f"duplicate Evidence path submitted by multiple producers: {rel}")
            else:
                paths_to_source[rel] = directory / rel
    required_paths = set(evidence_paths(version))
    missing = sorted(required_paths - set(paths_to_source))
    if missing:
        errors.extend(f"required Evidence missing from producer Artifacts: {rel}" for rel in missing)
    if errors:
        raise ValueError("; ".join(errors))

    for rel, source in paths_to_source.items():
        destination = output_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    context_rel = f"docs/evidence/evidence-source-context-v{version}.json"
    context_path = output_root / context_rel
    context_path.parent.mkdir(parents=True, exist_ok=True)
    rendered_context = json.dumps(context, ensure_ascii=False, indent=2) + "\n"
    context_path.write_text(rendered_context, encoding="utf-8")
    (output_root / f"evidence-source-context-v{version}.json").write_text(rendered_context, encoding="utf-8")

    manifest = build_evidence_manifest(
        output_root,
        version=version,
        tested_revision=github_sha,
        artifact_paths=evidence_paths(version),
        source_context=context,
    )
    manifest_path = output_root / "docs" / "evidence" / f"evidence-manifest-v{version}.json"
    write_evidence_manifest(manifest_path, manifest)
    checksum_path = write_manifest_checksum(manifest_path)
    shutil.copy2(manifest_path, output_root / manifest_path.name)
    shutil.copy2(checksum_path, output_root / checksum_path.name)

    checks = {
        "sharedCiEvidenceContext": "PASS",
        "producerOwnershipValidated": "PASS",
        "artifactCollisionRejected": "PASS",
        "exactMergeRevisionBound": "PASS",
        "completeEvidenceInventory": "PASS",
        "evidenceManifestChecksum": "PASS",
        "releasePackageEvidenceComplete": "PENDING",
        "releasePackageReverified": "PENDING",
    }
    preflight = {
        "schemaVersion": 1,
        "version": version,
        "testedRevision": github_sha,
        "status": "PASS",
        "checks": checks,
    }
    (output_root / f"preflight-v{version}.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path
