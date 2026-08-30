"""Source-of-truth fair-service settlement contracts for 4.7.6 Gate D."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_readiness,
    backup_replication,
    resilience_action_journal,
    resilience_fresh_state,
    resilience_scheduler_service,
    resilience_wave_executor,
)


def _action(action_id: str) -> dict[str, Any]:
    return {
        "actionId": action_id,
        "type": "CREATE_REPAIR_JOB",
        "waveIndex": 0,
        "parameters": {
            "policyId": "policy-fair",
            "backupId": "backup-fair",
            "sourceTargetId": "target-a",
            "destTargetId": "target-b",
            "estimatedBytes": 999_999,
        },
    }


def _terminal_repair(action_id: str, *, actual_bytes: int = 4096, duration_ms: float = 1250.0) -> dict[str, Any]:
    payload = _action(action_id)
    resilience_action_journal.record_action_intent(payload, plan_id="schedule-fair")
    claimed, action, reason = resilience_action_journal.admit_and_claim_action(
        action_id,
        owner_instance_id="fair-worker",
        enforce_budgets=False,
    )
    assert claimed is True and action is not None, reason
    job = backup_replication.create_repair_job(
        policy_id="policy-fair",
        backup_id="backup-fair",
        source_target_id="target-a",
        dest_target_id="target-b",
        resilience_action_id=action_id,
    )
    job = backup_replication._set_repair_phase(  # noqa: SLF001 - durable effect fixture
        job,
        "healthy",
        bytesRepaired=actual_bytes,
        durationMs=duration_ms,
    )
    terminal = resilience_action_journal.update_action_state(
        action_id,
        "SUCCEEDED",
        execution_epoch=int(action["executionEpoch"]),
        claim_token=str(action["claimToken"]),
        effect_class="CANCELABLE",
        effect_handle={"kind": "repair", "repairId": job["repairId"]},
        result={
            "repairId": job["repairId"],
            "repairResult": {
                "status": "success",
                "bytesRepaired": actual_bytes,
                "durationMs": duration_ms,
            },
        },
        verification={"verified": True},
    )
    return terminal


def _reserve(action_id: str) -> None:
    action = _action(action_id)
    resilience_scheduler_service.record_schedule_result(
        {"scheduleId": "schedule-fair", "executionWaves": [{"waveIndex": 0, "actions": [action]}]},
        [action],
    )


def _reserve_payload(action: dict[str, Any], *, schedule_id: str) -> None:
    resilience_scheduler_service.record_schedule_result(
        {"scheduleId": schedule_id, "executionWaves": [{"waveIndex": 0, "actions": [action]}]},
        [action],
    )


def _terminalize(
    action: dict[str, Any],
    *,
    effect_handle: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    action_id = str(action["actionId"])
    resilience_action_journal.record_action_intent(action, plan_id="effect-telemetry")
    claimed, record, reason = resilience_action_journal.admit_and_claim_action(
        action_id,
        owner_instance_id="effect-worker",
        enforce_budgets=False,
    )
    assert claimed is True and record is not None, reason
    return resilience_action_journal.update_action_state(
        action_id,
        "SUCCEEDED",
        execution_epoch=int(record["executionEpoch"]),
        claim_token=str(record["claimToken"]),
        effect_class="CANCELABLE",
        effect_handle=effect_handle,
        result=result,
        verification={"verified": True},
    )


def test_fair_service_settlement_has_no_caller_reported_actuals() -> None:
    parameters = inspect.signature(resilience_scheduler_service.settle_action_from_effect).parameters
    assert set(parameters) == {"action_id", "consumed_at"}
    assert not hasattr(resilience_scheduler_service, "consume_action_service")


def test_existing_475_reservation_database_is_migrated(tmp_settings: Path) -> None:
    resilience_scheduler_service.SCHEDULER_SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(resilience_scheduler_service.SCHEDULER_SERVICE_DB) as conn:
        conn.executescript(
            """
            CREATE TABLE resilience_service_reservations (
                action_id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                wave_index INTEGER NOT NULL,
                execution_epoch INTEGER NOT NULL,
                policy_id TEXT NOT NULL,
                estimated_bytes INTEGER NOT NULL,
                reserved_units REAL NOT NULL,
                status TEXT NOT NULL,
                actual_bytes INTEGER,
                actual_duration_ms REAL,
                actual_traffic_class TEXT,
                outcome TEXT,
                release_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO resilience_service_reservations VALUES (
                'legacy-fair', 'legacy-schedule', 0, 0, 'legacy-policy',
                10, 1.0, 'RESERVED', NULL, NULL, NULL, NULL, NULL,
                '2026-08-29T00:00:00Z', '2026-08-29T00:00:00Z'
            );
            """
        )

    migrated = resilience_scheduler_service.get_reservation("legacy-fair")

    assert migrated is not None and migrated["status"] == "RESERVED"
    assert migrated["effectHandle"] is None
    assert migrated["transferId"] is None
    assert migrated["telemetryDigest"] is None


def test_fair_service_uses_durable_repair_telemetry_not_estimate(tmp_settings: Path) -> None:
    action_id = "fair-real-repair"
    _reserve(action_id)
    terminal = _terminal_repair(action_id, actual_bytes=12_345, duration_ms=678.5)

    settled = resilience_scheduler_service.settle_action_from_effect(action_id)

    assert settled is not None and settled["status"] == "CONSUMED"
    assert settled["actualBytes"] == 12_345
    assert settled["actualDurationMs"] == 678.5
    assert settled["actualTrafficClass"] == "P2_REQUIRED_REPAIR"
    assert settled["executionEpoch"] == terminal["executionEpoch"]
    assert settled["effectHandle"] == terminal["effectHandle"]
    assert settled["transferId"] == terminal["effectHandle"]["repairId"]
    assert len(settled["telemetryDigest"]) == 64
    policy = resilience_scheduler_service.get_policy_service("policy-fair")
    assert policy is not None and policy["bytesServed"] == 12_345


def test_terminal_replay_does_not_double_charge_fair_service(tmp_settings: Path) -> None:
    action_id = "fair-terminal-replay"
    _reserve(action_id)
    terminal = _terminal_repair(action_id, actual_bytes=2048)
    first = resilience_scheduler_service.settle_action_from_effect(action_id)
    job = backup_replication.read_repair_job(str(terminal["effectHandle"]["repairId"]))
    assert job is not None
    backup_replication._set_repair_phase(job, "healthy", bytesRepaired=999_999, durationMs=9999)  # noqa: SLF001

    second = resilience_scheduler_service.settle_action_from_effect(action_id)

    assert first is not None and second is not None
    assert first["actualBytes"] == second["actualBytes"] == 2048
    assert resilience_scheduler_service.get_policy_service("policy-fair")["actionsServed"] == 1  # type: ignore[index]
    assert [event["toStatus"] for event in resilience_scheduler_service.list_settlement_events(action_id)] == [
        "CONSUMING",
        "CONSUMED",
    ]


def test_consuming_settlement_resumes_exactly_once_after_crash(tmp_settings: Path) -> None:
    action_id = "fair-consuming-recovery"
    _reserve(action_id)
    _terminal_repair(action_id, actual_bytes=321)
    telemetry = resilience_scheduler_service._read_terminal_effect_telemetry(action_id)  # noqa: SLF001

    consuming = resilience_scheduler_service._begin_effect_settlement(action_id, telemetry)  # noqa: SLF001
    assert consuming is not None and consuming["status"] == "CONSUMING"

    consumed = resilience_scheduler_service.settle_action_from_effect(action_id)

    assert consumed is not None and consumed["status"] == "CONSUMED"
    assert consumed["actualBytes"] == 321
    assert resilience_scheduler_service.get_policy_service("policy-fair")["actionsServed"] == 1  # type: ignore[index]


def test_failed_effect_releases_reserved_service(tmp_settings: Path) -> None:
    action_id = "fair-failed-effect"
    _reserve(action_id)
    resilience_action_journal.record_action_intent(_action(action_id), plan_id="schedule-fair")
    resilience_action_journal.update_action_state(action_id, "BLOCKED", error="production admission denied")

    released = resilience_scheduler_service.settle_action_from_effect(action_id)

    assert released is not None and released["status"] == "RELEASED"
    assert released["releaseReason"] == "FAILED"
    assert resilience_scheduler_service.get_policy_service("policy-fair") is None


def test_rebalance_service_uses_durable_job_telemetry(tmp_settings: Path) -> None:
    action = {
        "actionId": "fair-rebalance",
        "type": "CREATE_REBALANCE_JOB",
        "parameters": {
            "policyId": "policy-rebalance",
            "backupId": "backup-rebalance",
            "sourceTargetId": "target-a",
            "destTargetId": "target-b",
            "estimatedBytes": 9_999,
        },
    }
    _reserve_payload(action, schedule_id="schedule-rebalance")
    job = backup_replication.create_rebalance_job(
        policy_id="policy-rebalance",
        backup_id="backup-rebalance",
        source_target_id="target-a",
        dest_target_id="target-b",
        resilience_action_id=str(action["actionId"]),
    )
    job = backup_replication._set_rebalance_phase(  # noqa: SLF001 - durable effect fixture
        job,
        "complete",
        bytesTransferred=4567,
        durationMs=234.0,
    )
    _terminalize(
        action,
        effect_handle={"kind": "rebalance", "jobId": job["jobId"]},
        result={"jobId": job["jobId"], "job": job, "executionStatus": "success"},
    )

    settled = resilience_scheduler_service.settle_action_from_effect(str(action["actionId"]))

    assert settled is not None and settled["actualBytes"] == 4567
    assert settled["actualDurationMs"] == 234.0
    assert settled["actualTrafficClass"] == "P5_REBALANCE_DRAIN"


def test_drill_service_uses_durable_restore_accounting_without_changing_proof_v1(tmp_settings: Path) -> None:
    action = {
        "actionId": "fair-drill",
        "type": "START_DR_DRILL",
        "parameters": {"policyId": "policy-drill", "backupId": "backup-drill", "targetId": "target-drill"},
    }
    _reserve_payload(action, schedule_id="schedule-drill")
    drill = backup_dr_readiness.run_dr_drill(
        backup_id="backup-drill",
        target_id="target-drill",
        resilience_action_id=str(action["actionId"]),
    )
    _terminalize(
        action,
        effect_handle={"kind": "drill", "resilienceActionId": action["actionId"], "drillId": drill["drillId"]},
        result=drill,
    )

    settled = resilience_scheduler_service.settle_action_from_effect(str(action["actionId"]))

    assert settled is not None and settled["actualBytes"] == drill["bytesRestored"]
    assert settled["actualDurationMs"] == drill["durationMs"]
    assert settled["actualTrafficClass"] == "P4_SCRUB_DRILL"
    assert drill["proof"]["schema"] == "dr-readiness-proof-v1"
    assert "bytesRestored" not in drill["proof"]


def test_wave_runner_settles_terminal_action_from_effect_source(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_id = "fair-wave-runner"
    action = _action(action_id)
    schedule = {
        "scheduleId": "schedule-fair",
        "riskDigest": "5" * 64,
        "authorityHeadDigest": "6" * 64,
        "executionWaves": [{"waveIndex": 0, "actions": [action]}],
    }
    resilience_scheduler_service.record_schedule_result(schedule, [action])
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest="6" * 64)
    _terminal_repair(action_id, actual_bytes=777, duration_ms=88)
    monkeypatch.setattr(
        resilience_fresh_state,
        "build_fresh_state_bundle",
        lambda planned, actions, *, now=None: {
            "authorityHeadDigest": "6" * 64,
            "riskDigest": "5" * 64,
            "authorityState": {"workersAllowed": True, "mutationsAllowed": True},
            "maintenanceDecisions": [{"allowed": True}],
            "budgets": {"admitted": True},
            "blastSimulation": {"passed": True},
        },
    )

    def no_second_execution(*args: object, **kwargs: object) -> dict[str, Any]:
        raise AssertionError("terminal Action must be reconciled, not executed again")

    monkeypatch.setattr(resilience_action_journal, "execute_autonomous_action", no_second_execution)

    result = resilience_wave_executor.run_next_wave("schedule-fair", instance_id="fair-wave-worker")

    assert result["status"] == "COMPLETED"
    settlement = result["actions"][0]["fairServiceSettlement"]
    assert settlement["status"] == "CONSUMED"
    assert settlement["actualBytes"] == 777
