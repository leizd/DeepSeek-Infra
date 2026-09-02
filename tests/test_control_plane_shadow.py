from __future__ import annotations

from pathlib import Path

from scripts.control_plane_shadow import check_fixture, evaluate


ROOT = Path(__file__).resolve().parents[1]


def test_python_shadow_oracle_matches_frozen_digests() -> None:
    report = check_fixture()
    assert report["ok"] is True
    assert report["passed"] == report["total"]
    assert report["total"] >= 8


def test_shadow_kernel_never_authorizes_mutation() -> None:
    decision = evaluate({"actions": [], "capacityTargets": [], "federationTransitions": []})
    assert decision["mutationDenied"] is True
    assert decision["digest"]


def test_stale_epoch_is_rejected_before_admit() -> None:
    decision = evaluate(
        {
            "nowUnix": 10,
            "nowMinute": 0,
            "liveEpochs": {"act-1": 4},
            "actions": [{"actionId": "act-1", "executionEpoch": 3, "type": "CREATE_REPAIR_JOB", "severity": "warning", "createdAtUnix": 10}],
            "capacityTargets": [],
            "federationTransitions": [],
        }
    )
    assert decision["scheduler"]["admissions"][0]["reason"] == "STALE_EXECUTION_EPOCH"
    assert decision["scheduler"]["orderedActionIds"] == []


def test_pending_to_active_is_not_tofu() -> None:
    decision = evaluate(
        {
            "localFleetId": "fleet-a",
            "federationTransitions": [
                {
                    "peerFleetId": "fleet-b",
                    "from": "PENDING",
                    "to": "ACTIVE",
                    "metadata": {"provider": "minio", "region": "us", "jurisdiction": "us", "siteClass": "region"},
                }
            ],
        }
    )
    assert decision["federation"]["transitions"][0]["code"] == "FEDERATION_PEER_NOT_VERIFIED"
