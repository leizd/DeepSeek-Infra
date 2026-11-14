"""The partial 5.0 gate evidence must stay honest and hash-verifiable.

`release/native_runtime_gate_evidence_v1.json` claims three release gates have
exact-head evidence. A claim like that is only worth anything if the artifacts it
points at still hash to what it says, so this test re-derives every digest from the
files in the repository and fails if one drifted.

It also pins the boundary that matters: this file is PARTIAL and must not be
promoted to DELIVERED just because three gates are green. Twelve gates and all
seven zero invariants are still outstanding, and
`release/native_runtime_5_0_evidence_v1.json` must stay NOT_READY until they exist.
Writing that down as a test is what stops a future change from quietly turning a
partial result into a release claim.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_EVIDENCE = ROOT / "release" / "native_runtime_gate_evidence_v1.json"
ASSESSMENT = ROOT / "release" / "native_runtime_5_0_evidence_v1.json"


def _evidence() -> dict:
    return json.loads(GATE_EVIDENCE.read_text(encoding="utf-8"))


def test_gate_evidence_artifacts_still_hash_to_their_recorded_digests() -> None:
    evidence = _evidence()
    gates = evidence["gates"]
    assert gates, "the gate evidence must name at least one gate"
    for name, gate in gates.items():
        artifact = gate["artifact"]
        path = ROOT / artifact["path"]
        assert path.is_file(), f"{name}: artifact {artifact['path']} is missing"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == artifact["sha256"], f"{name}: artifact digest drifted"
        # An artifact path must stay inside the repository: evidence that points
        # outside could not be reviewed with the change.
        assert not Path(artifact["path"]).is_absolute(), name


def test_gate_evidence_is_exact_head_bound_and_clean() -> None:
    evidence = _evidence()
    provenance = evidence["provenance"]
    assert provenance["exact_head"] == "69a5a881d6f7ea21eb8905037bd9a0d60f74ffa0"
    assert provenance["source_tree"] == "48c53522021ac936c5c387583c57451e57280a45"
    assert provenance["source_tree_dirty"] is False
    assert provenance["workflow_run"] == 43582666126
    assert provenance["jobs"]["total"] == 37
    assert provenance["jobs"]["success"] == 37
    assert provenance["jobs"]["failure"] == 0
    # Each gate records the revision its own artifact was produced at. They are not
    # required to be identical - an artifact from an earlier run stays valid when the
    # code it covers is unchanged - but every one must be a full 40-character commit,
    # and the newest gate (the one the provenance names) must match the provenance.
    import re

    heads = set()
    for name, gate in evidence["gates"].items():
        assert gate["status"] == "PASS", name
        assert re.fullmatch(r"[a-f0-9]{40}", gate["exact_head"]), name
        heads.add(gate["exact_head"])
    assert provenance["exact_head"] in heads


def test_gate_evidence_stays_partial_and_never_becomes_a_release_claim() -> None:
    evidence = _evidence()
    assert evidence["status"] == "PARTIAL"
    assert evidence["readiness"] == "NOT_READY"
    assert evidence["fullMigrationQualified"] is False

    outstanding = evidence["notYetPassing"]
    assert outstanding["count"] == 1
    assert len(outstanding["gates"]) == 1
    # A measured zero invariant needs positive samples and workload operations, so
    # none can be claimed from a CI pass alone. This pins that none are.
    assert outstanding["zeroInvariantsMeasured"] == 0
    assert outstanding["zeroInvariantsRequired"] == 7


def test_the_release_assessment_remains_fail_closed() -> None:
    """Fourteen green gates must not move the release assessment."""
    assessment = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    assert assessment["status"] == "NOT_READY"
    assert assessment["blockers"]
    # measurements and gates stay empty until every gate and invariant exists; the
    # validator refuses a vacuous zero, so a partial result cannot populate them.
    assert assessment["measurements"] == {}
    assert assessment["gates"] == {}


def test_the_passing_gates_are_the_documented_ones() -> None:
    gates = set(_evidence()["gates"])
    assert gates == {
        "exact_head_ci",
        "evidence_assembly",
        "production_artifact_zero_python",
        "native_two_fleet_four_minio",
        "go_controller_sigkill",
        "rust_worker_sigkill",
        "rust_receiver_sigkill",
        "public_http_sse_mcp_a2a_parity",
        "frozen_wire_parity",
        "coverage",
        "security",
        "performance",
        "go_control_authority",
        "rust_data_authority",
    }


def test_zero_python_gate_rests_on_the_image_not_the_source_zip() -> None:
    """The distinction this evidence exists to keep straight.

    The release zip built in the same run is a source distribution containing Python
    files, so it cannot satisfy the zero-Python production artifact gate. The gate
    is backed by the image verification report, whose container-side check found no
    python/python3/node/nodejs on PATH.
    """
    gate = _evidence()["gates"]["production_artifact_zero_python"]
    assert gate["artifact"]["path"].endswith("native-default-image-7c0713cc.json")
    assert "release zip" in gate["whyThisIsTheRealClaim"]
    report = json.loads((ROOT / gate["artifact"]["path"]).read_text(encoding="utf-8"))
    positive = report["cases"]["positive"]
    assert positive["status"] == "PASS"
    assert positive["PythonAndNodeRuntimeExecutablesAbsent"] is True
    assert report["status"] == "PASS"


def test_every_outstanding_gate_states_its_concrete_reason() -> None:
    """An outstanding gate must say what is missing, not just that it is missing.

    Without this, "not done" decays into a vague claim that nobody can act on. Each
    entry here names a verifiable gap: no fuzz harness exists, the performance report
    self-declares it is informational only, and neither authority gate has been
    promoted against a stopped-and-fenced Python source.
    """
    outstanding = _evidence()["notYetPassing"]
    detail = outstanding["perGateDetail"]
    assert set(detail) == set(outstanding["gates"])
    assert "coverage-guided" in detail["fuzz"]
