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
    assert evidence["status"] == "DELIVERED"

    # Invariants verification
    invariants = evidence["invariants"]
    assert invariants["pythonProductionHttpRequests"] == 0
    assert invariants["pythonProductionMutations"] == 0
    assert invariants["pythonProductionSchedulerRuns"] == 0
    assert invariants["pythonProductionStorageWrites"] == 0
    assert invariants["pythonProductionFederationSigns"] == 0
    assert invariants["sharedDatabaseWrites"] == 0
    assert invariants["crossLanguageCgoAllocations"] == 0

    # Topology verification
    topology = evidence["topology"]
    assert topology["public_listener"]["runtime"] == "rust"
    assert topology["control_plane"]["runtime"] == "go"
    assert topology["control_plane"]["cgo_enabled"] is False
    assert topology["worker_plane"]["runtime"] == "rust"

    # Contract coverage verification
    contracts = evidence["contracts"]
    assert contracts["frozen_corpora"] == 10
    assert contracts["command_codes"] == 8
    assert contracts["domains"] == 43
    assert contracts["mechanical_denial_enforced"] is True
