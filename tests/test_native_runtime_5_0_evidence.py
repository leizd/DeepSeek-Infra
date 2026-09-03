from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "release" / "native_runtime_5_0_evidence_v1.json"


def test_5_0_evidence_invariants() -> None:
    assert EVIDENCE_PATH.exists()
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))

    assert evidence["schema_version"] == 1
    assert evidence["release"] == "5.0.0"
    assert evidence["status"] == "NOT_READY"
    assert evidence["blockers"]
    assert evidence["measurements"] == {}
    assert evidence["gates"] == {}

    # These are release requirements, not measurements or delivery claims.
    assert "invariants" not in evidence
    invariants = evidence["required_invariants"]
    assert invariants["pythonProductionHttpRequests"] == 0
    assert invariants["pythonProductionMutations"] == 0
    assert invariants["pythonProductionSchedulerRuns"] == 0
    assert invariants["pythonProductionStorageWrites"] == 0
    assert invariants["pythonProductionFederationSigns"] == 0
    assert invariants["sharedDatabaseWrites"] == 0
    assert invariants["crossLanguageCgoAllocations"] == 0

    # The topology is explicitly a target until provider-backed gates pass.
    assert "topology" not in evidence
    topology = evidence["target_topology"]
    assert topology["public_listener"]["runtime"] == "rust"
    assert topology["control_plane"]["runtime"] == "go"
    assert topology["control_plane"]["cgo_enabled"] is False
    assert topology["worker_plane"]["runtime"] == "rust"
