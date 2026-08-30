"""Production fresh-state admission contracts for 4.7.6 Gates A-B."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import resilience_fleet_scheduler, resilience_fresh_state, resilience_wave_executor


RISK_DIGEST = "a" * 64
AUTHORITY_DIGEST = "b" * 64


def _schedule(schedule_id: str = "fresh-state-schedule") -> dict[str, Any]:
    return {
        "scheduleId": schedule_id,
        "riskDigest": RISK_DIGEST,
        "authorityHeadDigest": AUTHORITY_DIGEST,
        "executionWaves": [
            {
                "waveIndex": 0,
                "actions": [
                    {
                        "actionId": f"{schedule_id}-repair",
                        "type": "CREATE_REPAIR_JOB",
                        "parameters": {
                            "policyId": "policy-a",
                            "backupId": "backup-a",
                            "sourceTargetId": "target-a",
                            "destTargetId": "target-b",
                        },
                    }
                ],
            }
        ],
    }


def _install_available_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resilience_fresh_state,
        "_read_authority_state",
        lambda: {
            "canonicalDigest": AUTHORITY_DIGEST,
            "canonicalGeneration": 7,
            "workersAllowed": True,
            "mutationsAllowed": True,
        },
    )
    monkeypatch.setattr(
        resilience_fresh_state,
        "_read_risk_snapshot",
        lambda *, now: {
            "riskDigest": RISK_DIGEST,
            "riskSnapshotVersion": 1,
            "overallRisk": "warning",
            "coverage": {"CAPACITY_EXHAUSTION": {"complete": True}},
            "generatedAt": "2026-08-30T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        resilience_fresh_state,
        "_read_capacity_snapshot",
        lambda actions, *, now: {
            "targets": [
                {
                    "targetId": "target-a",
                    "usedBytes": 100,
                    "freeBytes": 900,
                    "totalBytes": 1000,
                    "observedAt": "2026-08-30T00:00:00Z",
                    "source": "minio-probe",
                },
                {
                    "targetId": "target-b",
                    "usedBytes": 200,
                    "freeBytes": 800,
                    "totalBytes": 1000,
                    "observedAt": "2026-08-30T00:00:00Z",
                    "source": "minio-probe",
                },
            ]
        },
    )
    monkeypatch.setattr(resilience_fresh_state, "_read_running_effects", lambda: [])
    monkeypatch.setattr(
        resilience_fresh_state,
        "_read_budget_snapshot",
        lambda actions, running_effects: {
            "admitted": True,
            "actionLimits": {"maxConcurrentActions": 3},
            "transferBudget": {"globalBytesPerSecond": 1024},
        },
    )
    monkeypatch.setattr(
        resilience_fresh_state,
        "_read_maintenance_decisions",
        lambda actions, *, now: [{"actionId": str(actions[0]["actionId"]), "allowed": True, "reason": "WITHIN_WINDOW"}],
    )
    monkeypatch.setattr(
        resilience_fresh_state,
        "_read_blast_simulation",
        lambda actions, running_effects: {"passed": True, "simulatedActions": len(actions)},
    )


def test_wave_admission_cannot_accept_caller_supplied_freshness_flags() -> None:
    parameters = inspect.signature(resilience_wave_executor.admit_wave).parameters
    assert "authority_head_digest" not in parameters
    assert "risk_snapshot" not in parameters
    assert "capacity_snapshot" not in parameters
    assert "running_effects" not in parameters
    assert "budgets" not in parameters
    assert "maintenance_windows_ok" not in parameters
    assert "blast_simulation" not in parameters


@pytest.mark.parametrize(
    ("reader_name", "expected_reason"),
    [
        ("_read_authority_state", "AUTHORITY_OBSERVATION_UNAVAILABLE"),
        ("_read_risk_snapshot", "RISK_SNAPSHOT_UNAVAILABLE"),
        ("_read_capacity_snapshot", "CAPACITY_SNAPSHOT_UNAVAILABLE"),
        ("_read_running_effects", "RUNNING_EFFECTS_UNAVAILABLE"),
        ("_read_budget_snapshot", "BUDGET_SNAPSHOT_UNAVAILABLE"),
        ("_read_maintenance_decisions", "MAINTENANCE_DECISION_UNAVAILABLE"),
        ("_read_blast_simulation", "BLAST_SIMULATION_UNAVAILABLE"),
    ],
)
def test_missing_production_source_blocks_wave_admission(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_name: str,
    expected_reason: str,
) -> None:
    _install_available_sources(monkeypatch)
    monkeypatch.setattr(resilience_fresh_state, reader_name, lambda *args, **kwargs: None)
    schedule = _schedule(f"missing-{reader_name.removeprefix('_read_')}")
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)

    result = resilience_wave_executor.admit_wave(str(schedule["scheduleId"]), 0)

    assert result["admitted"] is False
    assert result["status"] == "WAVE_NOT_ADMITTED"
    assert result["reason"] == expected_reason
    assert resilience_wave_executor.list_waves(str(schedule["scheduleId"]))[0]["status"] == "PENDING"


def test_fresh_state_bundle_is_digest_bound_and_admits_wave(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_available_sources(monkeypatch)
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    schedule = _schedule()
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST, now=now)

    result = resilience_wave_executor.admit_wave(str(schedule["scheduleId"]), 0, now=now)

    assert result["admitted"] is True
    bundle = result["revalidation"]["freshStateBundle"]
    assert bundle["authorityHeadDigest"] == AUTHORITY_DIGEST
    assert bundle["riskDigest"] == RISK_DIGEST
    assert len(bundle["capacitySnapshotDigest"]) == 64
    assert len(bundle["runningEffectsDigest"]) == 64
    assert len(bundle["budgetRevision"]) == 64
    assert len(bundle["maintenanceDecisionDigest"]) == 64
    assert len(bundle["blastSimulationDigest"]) == 64
    assert len(bundle["freshStateBundleDigest"]) == 64
    assert bundle["observedAt"] == "2026-08-30T00:00:00Z"


def test_stale_production_risk_pauses_for_replan(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_available_sources(monkeypatch)
    monkeypatch.setattr(
        resilience_fresh_state,
        "_read_risk_snapshot",
        lambda *, now: {
            "riskDigest": "c" * 64,
            "riskSnapshotVersion": 1,
            "overallRisk": "critical",
            "coverage": {"CAPACITY_EXHAUSTION": {"complete": True}},
        },
    )
    schedule = _schedule("stale-production-risk")
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)

    result = resilience_wave_executor.admit_wave(str(schedule["scheduleId"]), 0)

    assert result["admitted"] is False
    assert result["status"] == "PAUSED_REPLAN"
    assert "RISK_SNAPSHOT_STALE" in result["revalidation"]["reasons"]


def test_unexpected_fresh_state_failure_does_not_strand_admitting_wave(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = _schedule("unexpected-fresh-state-failure")
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)

    def fail_bundle(*args: object, **kwargs: object) -> dict[str, Any]:
        raise RuntimeError("source serialization failed")

    monkeypatch.setattr(resilience_fresh_state, "build_fresh_state_bundle", fail_bundle)

    result = resilience_wave_executor.admit_wave(str(schedule["scheduleId"]), 0)

    assert result["admitted"] is False
    assert result["status"] == "WAVE_NOT_ADMITTED"
    assert result["reason"] == "FRESH_STATE_BUILD_FAILED"
    assert resilience_wave_executor.list_waves(str(schedule["scheduleId"]))[0]["status"] == "PENDING"


def test_planned_schedule_binds_current_authority_head(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        resilience_fleet_scheduler.backup_control_recovery,
        "authority_health_snapshot",
        lambda: {"canonicalDigest": AUTHORITY_DIGEST},
    )

    schedule = resilience_fleet_scheduler.schedule_fleet_resilience(
        {"riskDigest": RISK_DIGEST, "risks": []},
        candidate_actions=_schedule("planner-binding")["executionWaves"][0]["actions"],
        now=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )

    assert schedule["authorityHeadDigest"] == AUTHORITY_DIGEST
    persisted = resilience_wave_executor.get_schedule(str(schedule["scheduleId"]))
    assert persisted is not None
    assert persisted["authorityHeadDigest"] == AUTHORITY_DIGEST
