#!/usr/bin/env python3
"""Verify that an exact-merge Evidence set is byte-identical inside a release ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_inventory import evidence_paths  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_manifest import (  # noqa: E402
    sha256_of,
    validate_evidence_manifest,
    validate_manifest_checksum,
)

RUNTIME_PREFIXES = (
    ".file-cache/",
    ".projects/",
    ".local-rag/",
    ".traces/",
    ".semantic-cache/",
    ".request-queue/",
    ".generated/",
    ".tool-audit/",
    ".scheduler/",
    ".a2a/",
    ".budget/",
    ".memory/",
    ".reminders/",
    ".agent-runs/",
    ".search-cache/",
    ".auth-token/",
    ".media/",
    ".browser-audit/",
    ".browser-downloads/",
    ".browser-profiles/",
    ".automation/",
    ".skills/",
)
SECRET_NAMES = {".env", ".env.local", ".auth-token", "signing.properties", "keystore.properties"}


def _load_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _manifest_paths(evidence_root: Path, version: str) -> tuple[Path, Path]:
    nested = evidence_root / "docs" / "evidence" / f"evidence-manifest-v{version}.json"
    manifest = nested if nested.is_file() else evidence_root / f"evidence-manifest-v{version}.json"
    checksum = manifest.with_suffix(manifest.suffix + ".sha256")
    return manifest, checksum


def verify_release_package(
    archive: Path,
    release_manifest_path: Path,
    evidence_root: Path,
    *,
    version: str,
    expected_revision: str,
) -> list[str]:
    errors: list[str] = []
    evidence_manifest_path, checksum_path = _manifest_paths(evidence_root, version)
    errors.extend(validate_manifest_checksum(evidence_manifest_path, checksum_path))
    if errors:
        return errors
    errors.extend(
        validate_evidence_manifest(
            evidence_root,
            version=version,
            expected_revision=expected_revision,
            required_paths=evidence_paths(version),
            github_sha=expected_revision,
        )
    )
    try:
        evidence_manifest = _load_object(evidence_manifest_path)
        release_manifest = _load_object(release_manifest_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [f"invalid package manifest: {exc}"]
    if release_manifest.get("version") != version:
        errors.append("release manifest version mismatch")
    if release_manifest.get("schemaVersion") != "release-manifest.v3":
        errors.append("release manifest schema mismatch")
    if release_manifest.get("commit") != expected_revision:
        errors.append("release manifest commit does not match GITHUB_SHA")
    if release_manifest.get("sha256") != sha256_of(archive):
        errors.append("release ZIP checksum does not match release manifest")
    raw_entries = evidence_manifest.get("artifacts")
    entries = raw_entries if isinstance(raw_entries, list) else []
    entry_map = {
        str(entry["path"]): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    required = set(evidence_paths(version))
    expected_release_evidence = [
        *evidence_paths(version),
        f"docs/evidence/evidence-source-context-v{version}.json",
        f"docs/evidence/evidence-manifest-v{version}.json",
        f"docs/evidence/evidence-manifest-v{version}.json.sha256",
    ]
    if release_manifest.get("evidence") != expected_release_evidence:
        errors.append("release manifest Evidence inventory mismatch")
    if set(entry_map) != required:
        errors.append("Evidence manifest inventory is incomplete or contains unlisted paths")
    expected_manifest_summary = {
        "path": f"docs/evidence/evidence-manifest-v{version}.json",
        "sha256": sha256_of(evidence_manifest_path),
        "artifactCount": len(entries),
        "testedRevision": expected_revision,
    }
    if release_manifest.get("evidenceManifest") != expected_manifest_summary:
        errors.append("release manifest Evidence manifest summary mismatch")

    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
        for name in names:
            normalized = name.replace("\\", "/")
            basename = Path(normalized).name
            if normalized.startswith(RUNTIME_PREFIXES) or basename in SECRET_NAMES or basename.endswith((".jks", ".keystore")):
                errors.append(f"release ZIP contains excluded runtime or secret path: {normalized}")
            if re.fullmatch(r"(?:\.server.*|server.*)\.log", basename):
                errors.append(f"release ZIP contains log file: {normalized}")
        manifest_rel = f"docs/evidence/evidence-manifest-v{version}.json"
        checksum_rel = manifest_rel + ".sha256"
        for rel in (manifest_rel, checksum_rel):
            if rel not in names:
                errors.append(f"release ZIP missing Evidence manifest chain file: {rel}")
        if manifest_rel in names and package.read(manifest_rel) != evidence_manifest_path.read_bytes():
            errors.append("release ZIP Evidence manifest differs from assembled Artifact")
        if checksum_rel in names and package.read(checksum_rel) != checksum_path.read_bytes():
            errors.append("release ZIP detached Evidence checksum differs from assembled Artifact")
        context_rel = f"docs/evidence/evidence-source-context-v{version}.json"
        external_context = evidence_root / context_rel
        if context_rel not in names:
            errors.append(f"release ZIP missing Evidence source context: {context_rel}")
        elif not external_context.is_file() or package.read(context_rel) != external_context.read_bytes():
            errors.append("release ZIP Evidence source context differs from assembled Artifact")
        for rel, entry in entry_map.items():
            if rel not in names:
                errors.append(f"release ZIP missing required Evidence: {rel}")
                continue
            payload = package.read(rel)
            if _sha256_bytes(payload) != entry.get("sha256"):
                errors.append(f"release ZIP Evidence checksum mismatch: {rel}")
            external = evidence_root / rel
            if not external.is_file() or external.read_bytes() != payload:
                errors.append(f"release ZIP Evidence differs from assembled Artifact: {rel}")
        allowed_versioned = required | {
            context_rel,
            manifest_rel,
        }
        for name in names:
            if not name.endswith(".json") or not name.startswith(("docs/evidence/", "evals/reports/")) or name in allowed_versioned:
                continue
            current_version = name.endswith(f"v{version}.json")
            if not current_version:
                try:
                    value = json.loads(package.read(name))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    value = None
                current_version = isinstance(value, dict) and value.get("version") == version
            if current_version and name not in required:
                errors.append(f"release ZIP contains unlisted current Evidence: {name}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        errors = verify_release_package(
            args.archive.resolve(),
            args.release_manifest.resolve(),
            args.evidence_root.resolve(),
            version=args.version,
            expected_revision=args.expected_revision,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        errors = [str(exc)]
    checks = {
        "sharedCiEvidenceContext": "PASS" if not errors else "FAIL",
        "producerOwnershipValidated": "PASS" if not errors else "FAIL",
        "artifactCollisionRejected": "PASS" if not errors else "FAIL",
        "exactMergeRevisionBound": "PASS" if not errors else "FAIL",
        "completeEvidenceInventory": "PASS" if not errors else "FAIL",
        "evidenceManifestChecksum": "PASS" if not errors else "FAIL",
        "releasePackageEvidenceComplete": "PASS" if not errors else "FAIL",
        "releasePackageReverified": "PASS" if not errors else "FAIL",
    }
    report = {
        "schemaVersion": 1,
        "version": args.version,
        "testedRevision": args.expected_revision,
        "status": "PASS" if not errors else "FAIL",
        "checks": checks,
        "errors": errors,
    }
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
