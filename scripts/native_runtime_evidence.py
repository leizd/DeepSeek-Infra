"""Fail-closed validation for the native Rust/Go runtime release evidence.

The checked-in assessment may describe an incomplete migration, but it must not
claim that 5.0 is delivered without exact-head, non-vacuous, content-addressed
evidence for every release gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REQUIRED_ZERO_INVARIANTS = (
    "pythonProductionHttpRequests",
    "pythonProductionMutations",
    "pythonProductionSchedulerRuns",
    "pythonProductionStorageWrites",
    "pythonProductionFederationSigns",
    "sharedDatabaseWrites",
    "crossLanguageCgoAllocations",
)

REQUIRED_GATES = (
    "exact_head_ci",
    "evidence_assembly",
    "public_http_sse_mcp_a2a_parity",
    "frozen_wire_parity",
    "go_control_authority",
    "rust_data_authority",
    "native_two_fleet_four_minio",
    "go_controller_sigkill",
    "rust_worker_sigkill",
    "rust_receiver_sigkill",
    "production_artifact_zero_python",
    "security",
    "coverage",
    "fuzz",
    "performance",
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class EvidenceError(RuntimeError):
    """Raised when native release evidence is incomplete or inconsistent."""


def _mapping(value: object, *, code: str, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{code}: {field} must be an object")
    return value


def _non_empty_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and bool(value)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _artifact_reference(value: object, *, owner: str) -> tuple[str, str]:
    artifact = _mapping(value, code="INVALID_ARTIFACT", field=f"{owner}.artifact")
    path = artifact.get("path")
    digest = artifact.get("sha256")
    if not isinstance(path, str) or not path.strip():
        raise EvidenceError(f"INVALID_ARTIFACT: {owner}.artifact.path must be non-empty")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise EvidenceError(f"INVALID_ARTIFACT_DIGEST: {owner}.artifact.sha256 must be lowercase SHA-256")
    return path, digest


def _resolve_artifact(root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise EvidenceError(f"ARTIFACT_PATH_ESCAPE: {relative_path}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise EvidenceError(f"ARTIFACT_PATH_ESCAPE: {relative_path}") from exc
    return resolved


def _verify_artifact(root: Path, relative_path: str, expected_digest: str) -> None:
    artifact = _resolve_artifact(root, relative_path)
    if not artifact.is_file():
        raise EvidenceError(f"ARTIFACT_MISSING: {relative_path}")
    actual_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if actual_digest != expected_digest:
        raise EvidenceError(
            f"ARTIFACT_DIGEST_MISMATCH: {relative_path} expected {expected_digest}, got {actual_digest}"
        )


def _repository_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    head = result.stdout.strip()
    if result.returncode != 0 or not head:
        detail = result.stderr.strip() or "git did not return a commit"
        raise EvidenceError(f"HEAD_UNAVAILABLE: {detail}")
    return head


def validate_native_runtime_evidence(
    evidence: Mapping[str, object],
    *,
    root: Path,
    current_head: str | None = None,
) -> None:
    """Validate an assessment without mutating it.

    ``NOT_READY`` and ``CANDIDATE`` are safe, non-release states and must carry
    explicit blockers. ``DELIVERED`` is accepted only when every measurement
    and release gate is exact-head bound and backed by a verified artifact.
    """

    if evidence.get("schema_version") != 1:
        raise EvidenceError("SCHEMA_MISMATCH: schema_version must be 1")

    release = evidence.get("release")
    if not isinstance(release, str) or not release.strip():
        raise EvidenceError("INVALID_RELEASE: release must be non-empty")

    status = evidence.get("status")
    if status not in {"NOT_READY", "CANDIDATE", "DELIVERED"}:
        raise EvidenceError(f"INVALID_STATUS: {status!r}")

    blockers = evidence.get("blockers")
    if status != "DELIVERED":
        if not _non_empty_sequence(blockers):
            raise EvidenceError(f"BLOCKERS_REQUIRED: {status} assessments must name blockers")
        return

    if blockers not in (None, []):
        raise EvidenceError("DELIVERED_WITH_BLOCKERS: delivered evidence cannot retain blockers")

    version_path = root / "VERSION"
    if not version_path.is_file():
        raise EvidenceError(f"VERSION_MISSING: {version_path}")
    actual_version = version_path.read_text(encoding="utf-8").strip()
    if actual_version != release:
        raise EvidenceError(f"VERSION_MISMATCH: evidence={release}, repository={actual_version}")

    provenance = _mapping(evidence.get("provenance"), code="PROVENANCE_REQUIRED", field="provenance")
    exact_head = provenance.get("exact_head")
    if not isinstance(exact_head, str) or not exact_head.strip():
        raise EvidenceError("EXACT_HEAD_REQUIRED: provenance.exact_head must be non-empty")
    observed_head = current_head if current_head is not None else _repository_head(root)
    if exact_head != observed_head:
        raise EvidenceError(f"EXACT_HEAD_MISMATCH: evidence={exact_head}, repository={observed_head}")

    measurements = _mapping(evidence.get("measurements"), code="MEASUREMENTS_REQUIRED", field="measurements")
    artifacts: list[tuple[str, str]] = []
    for name in REQUIRED_ZERO_INVARIANTS:
        if name not in measurements:
            raise EvidenceError(f"MISSING_MEASUREMENT: {name}")
        measurement = _mapping(
            measurements[name], code="INVALID_MEASUREMENT", field=f"measurements.{name}"
        )
        value = measurement.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != 0:
            raise EvidenceError(f"ZERO_INVARIANT_FAILED: {name}={value!r}")
        if not _positive_int(measurement.get("samples")) or not _positive_int(
            measurement.get("workload_operations")
        ):
            raise EvidenceError(
                f"VACUOUS_ZERO_MEASUREMENT: {name} requires positive samples and workload_operations"
            )
        artifacts.append(_artifact_reference(measurement.get("artifact"), owner=f"measurements.{name}"))

    gates = _mapping(evidence.get("gates"), code="GATES_REQUIRED", field="gates")
    for name in REQUIRED_GATES:
        if name not in gates:
            raise EvidenceError(f"MISSING_GATE: {name}")
        gate = _mapping(gates[name], code="INVALID_GATE", field=f"gates.{name}")
        if gate.get("status") != "PASS":
            raise EvidenceError(f"GATE_NOT_PASS: {name}={gate.get('status')!r}")
        if gate.get("exact_head") != exact_head:
            raise EvidenceError(f"GATE_HEAD_MISMATCH: {name}")
        artifacts.append(_artifact_reference(gate.get("artifact"), owner=f"gates.{name}"))

    verified: set[tuple[str, str]] = set()
    for artifact_reference in artifacts:
        if artifact_reference in verified:
            continue
        _verify_artifact(root, *artifact_reference)
        verified.add(artifact_reference)


def _load_evidence(path: Path) -> Mapping[str, object]:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"EVIDENCE_READ_FAILED: {path}: {exc}") from exc
    return _mapping(value, code="INVALID_EVIDENCE", field="evidence")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate native Rust/Go runtime release evidence")
    default_root = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "evidence",
        nargs="?",
        type=Path,
        default=default_root / "release" / "native_runtime_5_0_evidence_v1.json",
    )
    parser.add_argument("--root", type=Path, default=default_root)
    args = parser.parse_args(argv)

    try:
        evidence = _load_evidence(args.evidence)
        validate_native_runtime_evidence(evidence, root=args.root)
    except EvidenceError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "status": evidence["status"], "release": evidence["release"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
