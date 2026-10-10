from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.native_runtime_evidence import (
    EvidenceError,
    REQUIRED_GATES,
    REQUIRED_ZERO_INVARIANTS,
    validate_native_runtime_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "release" / "native_runtime_5_0_evidence_v1.json"


def _evidence() -> dict[str, object]:
    return json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))


def test_current_native_runtime_assessment_is_fail_closed_not_delivered() -> None:
    evidence = _evidence()

    assert evidence["status"] == "NOT_READY"
    assert evidence["blockers"]
    validate_native_runtime_evidence(evidence, root=ROOT)


def test_delivered_evidence_requires_matching_release_version(tmp_path: Path) -> None:
    evidence = _evidence()
    evidence["status"] = "DELIVERED"
    evidence["blockers"] = []
    (tmp_path / "VERSION").write_text("4.8.0\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="VERSION_MISMATCH"):
        validate_native_runtime_evidence(evidence, root=tmp_path, current_head="head-1")


def test_delivered_evidence_must_match_the_observed_exact_head(tmp_path: Path) -> None:
    evidence = _evidence()
    evidence["status"] = "DELIVERED"
    evidence["blockers"] = []
    evidence["provenance"] = {"exact_head": "head-1"}
    (tmp_path / "VERSION").write_text("5.0.0\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="EXACT_HEAD_MISMATCH"):
        validate_native_runtime_evidence(evidence, root=tmp_path, current_head="head-2")


def test_delivered_evidence_rejects_literal_zero_without_measurement(tmp_path: Path) -> None:
    evidence = _evidence()
    evidence["status"] = "DELIVERED"
    evidence["blockers"] = []
    evidence["provenance"] = {"exact_head": "head-1"}
    evidence["measurements"] = {
        name: {"value": 0, "samples": 0, "workload_operations": 0}
        for name in REQUIRED_ZERO_INVARIANTS
    }
    (tmp_path / "VERSION").write_text("5.0.0\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="VACUOUS_ZERO_MEASUREMENT"):
        validate_native_runtime_evidence(evidence, root=tmp_path, current_head="head-1")


def test_delivered_evidence_requires_every_hashed_gate_artifact(tmp_path: Path) -> None:
    evidence = _evidence()
    evidence["status"] = "DELIVERED"
    evidence["blockers"] = []
    evidence["provenance"] = {"exact_head": "head-1"}
    evidence["measurements"] = {
        name: {
            "value": 0,
            "samples": 1,
            "workload_operations": 1,
            "artifact": {"path": "measurements.json", "sha256": "0" * 64},
        }
        for name in REQUIRED_ZERO_INVARIANTS
    }
    evidence["gates"] = {}
    (tmp_path / "VERSION").write_text("5.0.0\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="MISSING_GATE") as exc_info:
        validate_native_runtime_evidence(evidence, root=tmp_path, current_head="head-1")

    assert any(gate in str(exc_info.value) for gate in REQUIRED_GATES)


def test_delivered_evidence_rejects_artifact_digest_mismatch(tmp_path: Path) -> None:
    artifact = tmp_path / "proof.json"
    artifact.write_text('{"ok":true}\n', encoding="utf-8")
    evidence = _evidence()
    evidence["status"] = "DELIVERED"
    evidence["blockers"] = []
    evidence["provenance"] = {"exact_head": "head-1"}
    evidence["measurements"] = {
        name: {
            "value": 0,
            "samples": 1,
            "workload_operations": 1,
            "artifact": {"path": "proof.json", "sha256": "0" * 64},
        }
        for name in REQUIRED_ZERO_INVARIANTS
    }
    evidence["gates"] = {
        gate: {
            "status": "PASS",
            "exact_head": "head-1",
            "artifact": {"path": "proof.json", "sha256": "0" * 64},
        }
        for gate in REQUIRED_GATES
    }
    (tmp_path / "VERSION").write_text("5.0.0\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="ARTIFACT_DIGEST_MISMATCH"):
        validate_native_runtime_evidence(evidence, root=tmp_path, current_head="head-1")


def test_delivered_evidence_rejects_artifact_path_escape(tmp_path: Path) -> None:
    evidence = _evidence()
    evidence["status"] = "DELIVERED"
    evidence["blockers"] = []
    evidence["provenance"] = {"exact_head": "head-1"}
    evidence["measurements"] = {
        name: {
            "value": 0,
            "samples": 1,
            "workload_operations": 1,
            "artifact": {"path": "../outside.json", "sha256": "0" * 64},
        }
        for name in REQUIRED_ZERO_INVARIANTS
    }
    evidence["gates"] = {
        gate: {
            "status": "PASS",
            "exact_head": "head-1",
            "artifact": {"path": "../outside.json", "sha256": "0" * 64},
        }
        for gate in REQUIRED_GATES
    }
    (tmp_path / "VERSION").write_text("5.0.0\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="ARTIFACT_PATH_ESCAPE"):
        validate_native_runtime_evidence(evidence, root=tmp_path, current_head="head-1")


def test_validation_does_not_mutate_evidence() -> None:
    evidence = _evidence()
    before = copy.deepcopy(evidence)

    validate_native_runtime_evidence(evidence, root=ROOT)

    assert evidence == before
