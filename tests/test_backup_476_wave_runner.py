"""Production Wave Runner and crash-takeover contracts for 4.7.6 Gates B-C."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_replication,
    resilience_action_journal,
    resilience_effect_reconciler,
    resilience_fresh_state,
    resilience_risk_engine,
    resilience_scheduler_service,
    resilience_wave_executor,
)


RISK_DIGEST = "1" * 64
AUTHORITY_DIGEST = "2" * 64


def _schedule(schedule_id: str = "runner-schedule", *, waves: int = 1) -> dict[str, Any]:
    execution_waves: list[dict[str, Any]] = []
    for wave_index in range(waves):
        execution_waves.append(
            {
                "waveIndex": wave_index,
                "actions": [
                    {
                        "actionId": f"{schedule_id}-repair-{wave_index}",
                        "type": "CREATE_REPAIR_JOB",
                        "parameters": {
                            "policyId": "policy-a",
                            "backupId": f"backup-{wave_index}",
                            "sourceTargetId": "target-a",
                            "destTargetId": "target-b",
                        },
                    }
                ],
            }
        )
    return {
        "scheduleId": schedule_id,
        "riskDigest": RISK_DIGEST,
        "authorityHeadDigest": AUTHORITY_DIGEST,
        "executionWaves": execution_waves,
    }


def _fresh_bundle() -> dict[str, Any]:
    return {
        "authorityHeadDigest": AUTHORITY_DIGEST,
        "riskDigest": RISK_DIGEST,
        "authorityState": {"workersAllowed": True, "mutationsAllowed": True},
        "maintenanceDecisions": [{"actionId": "runner", "allowed": True}],
        "budgets": {"admitted": True},
        "blastSimulation": {"passed": True},
        "freshStateBundleDigest": "3" * 64,
    }


def _install_fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resilience_fresh_state,
        "build_fresh_state_bundle",
        lambda schedule, wave_actions, *, now=None: _fresh_bundle(),
    )
    monkeypatch.setattr(
        resilience_scheduler_service,
        "settle_action_from_effect",
        lambda action_id, *, consumed_at=None: {"actionId": action_id, "status": "CONSUMED"},
    )


def _successful_action(action_id: str, *, epoch: int = 1) -> dict[str, Any]:
    return {
        "actionId": action_id,
        "state": "SUCCEEDED",
        "executionEpoch": epoch,
        "effectHandle": {"kind": "repair", "repairId": f"repair-{action_id}"},
        "executionResult": {
            "repairId": f"repair-{action_id}",
            "repairResult": {"status": "COMPLETED", "bytesTransferred": 4096, "durationMs": 1200},
        },
        "verificationResult": {"verified": True},
    }


def test_production_wave_runner_executes_action_journal_effect(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fresh_state(monkeypatch)
    schedule = _schedule()
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)
    executed: list[tuple[str, str, int]] = []

    def execute(action_id: str, *, instance_id: str, lease_seconds: int) -> dict[str, Any]:
        materialized = resilience_action_journal.get_action(action_id)
        assert materialized is not None and materialized["state"] == "PENDING"
        executed.append((action_id, instance_id, lease_seconds))
        return _successful_action(action_id)

    monkeypatch.setattr(resilience_action_journal, "execute_autonomous_action", execute)

    result = resilience_wave_executor.run_next_wave(
        str(schedule["scheduleId"]),
        instance_id="wave-worker-a",
        lease_seconds=90,
    )

    action_id = f"{schedule['scheduleId']}-repair-0"
    assert executed == [(action_id, "wave-worker-a", 90)]
    assert result["status"] == "COMPLETED"
    assert result["waveIndex"] == 0
    assert resilience_wave_executor.list_waves(str(schedule["scheduleId"]))[0]["status"] == "COMPLETED"
    action = resilience_wave_executor.list_wave_actions(str(schedule["scheduleId"]))[0]
    assert action["status"] == "VERIFIED_SUCCESS"
    assert action["journalExecutionEpoch"] == 1
    assert action["effectHandle"]["repairId"] == f"repair-{action_id}"


def test_existing_475_wave_database_is_migrated_with_zero_epochs(tmp_settings: Path) -> None:
    resilience_wave_executor.WAVE_EXECUTOR_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(resilience_wave_executor.WAVE_EXECUTOR_DB) as conn:
        conn.executescript(
            """
            CREATE TABLE resilience_wave_schedules (
                schedule_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                risk_digest TEXT NOT NULL,
                authority_head_digest TEXT,
                schedule_json TEXT NOT NULL,
                stale_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO resilience_wave_schedules VALUES (
                'legacy-475', 'PLANNED', 'legacy-risk', 'legacy-authority',
                '{"scheduleId":"legacy-475","executionWaves":[]}', NULL,
                '2026-08-29T00:00:00Z', '2026-08-29T00:00:00Z'
            );
            """
        )

    migrated = resilience_wave_executor.get_schedule("legacy-475")

    assert migrated is not None
    assert migrated["scheduleExecutionEpoch"] == 0
    assert migrated["ownerInstanceId"] is None
    assert migrated["leaseUntil"] is None


def test_wave_one_waits_for_real_wave_zero_outcome(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fresh_state(monkeypatch)
    schedule = _schedule("ordered-runner", waves=2)
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)
    executed: list[str] = []

    def execute(action_id: str, *, instance_id: str, lease_seconds: int) -> dict[str, Any]:
        del instance_id, lease_seconds
        executed.append(action_id)
        return _successful_action(action_id)

    monkeypatch.setattr(resilience_action_journal, "execute_autonomous_action", execute)

    first = resilience_wave_executor.run_next_wave(str(schedule["scheduleId"]), instance_id="worker-a")
    assert first["waveIndex"] == 0
    assert executed == ["ordered-runner-repair-0"]
    assert resilience_wave_executor.list_waves(str(schedule["scheduleId"]))[1]["status"] == "PENDING"

    second = resilience_wave_executor.run_next_wave(str(schedule["scheduleId"]), instance_id="worker-b")
    assert second["waveIndex"] == 1
    assert executed == ["ordered-runner-repair-0", "ordered-runner-repair-1"]
    assert resilience_wave_executor.get_schedule(str(schedule["scheduleId"]))["status"] == "COMPLETED"  # type: ignore[index]


def test_crash_takeover_uses_higher_epochs_and_reconciles_existing_effect(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fresh_state(monkeypatch)
    now = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
    schedule = _schedule("takeover-runner")
    schedule_id = str(schedule["scheduleId"])
    action_id = f"{schedule_id}-repair-0"
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST, now=now)

    calls: list[str] = []

    def crash_after_remote_effect(action: str, *, instance_id: str, lease_seconds: int) -> dict[str, Any]:
        del instance_id, lease_seconds
        calls.append(action)
        raise SystemExit("simulated SIGKILL after remote effect")

    monkeypatch.setattr(resilience_action_journal, "execute_autonomous_action", crash_after_remote_effect)
    with pytest.raises(SystemExit):
        resilience_wave_executor.run_next_wave(
            schedule_id,
            instance_id="wave-worker-a",
            lease_seconds=30,
            now=now,
        )

    before_schedule = resilience_wave_executor.get_schedule(schedule_id)
    before_wave = resilience_wave_executor.list_waves(schedule_id)[0]
    before_action = resilience_wave_executor.list_wave_actions(schedule_id)[0]
    assert before_wave["status"] == "EXECUTING"
    assert before_action["status"] == "EXECUTING"

    expired_at = now + timedelta(seconds=31)
    with sqlite3.connect(resilience_wave_executor.WAVE_EXECUTOR_DB) as conn:
        conn.execute(
            "UPDATE resilience_wave_schedules SET lease_until = ? WHERE schedule_id = ?",
            ((now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), schedule_id),
        )
        conn.execute(
            "UPDATE resilience_wave_states SET lease_until = ? WHERE schedule_id = ? AND wave_index = 0",
            ((now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), schedule_id),
        )
        conn.execute(
            "UPDATE resilience_wave_actions SET lease_until = ? WHERE schedule_id = ? AND action_id = ?",
            ((now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), schedule_id, action_id),
        )

    monkeypatch.setattr(
        resilience_action_journal,
        "execute_autonomous_action",
        lambda action, *, instance_id, lease_seconds: _successful_action(action, epoch=2),
    )

    recovered = resilience_wave_executor.run_next_wave(
        schedule_id,
        instance_id="wave-worker-b",
        lease_seconds=30,
        now=expired_at,
    )

    assert recovered["status"] == "COMPLETED"
    after_schedule = resilience_wave_executor.get_schedule(schedule_id)
    after_wave = resilience_wave_executor.list_waves(schedule_id)[0]
    after_action = resilience_wave_executor.list_wave_actions(schedule_id)[0]
    assert after_schedule is not None and before_schedule is not None
    assert after_schedule["scheduleExecutionEpoch"] > before_schedule["scheduleExecutionEpoch"]
    assert after_wave["waveExecutionEpoch"] > before_wave["waveExecutionEpoch"]
    assert after_action["actionExecutionEpoch"] > before_action["actionExecutionEpoch"]
    assert after_action["journalExecutionEpoch"] == 2
    assert calls == [action_id]


def test_terminal_journal_replay_does_not_execute_remote_effect_twice(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fresh_state(monkeypatch)
    schedule = _schedule("terminal-replay")
    schedule_id = str(schedule["scheduleId"])
    action_id = f"{schedule_id}-repair-0"
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)
    resilience_action_journal.record_action_intent(
        schedule["executionWaves"][0]["actions"][0],
        created_by="resilience-wave-runner",
        plan_id=schedule_id,
        input_risk_digest=RISK_DIGEST,
    )
    terminal = _successful_action(action_id, epoch=4)
    monkeypatch.setattr(resilience_action_journal, "get_action", lambda requested: terminal if requested == action_id else None)

    def forbidden_execute(*args: object, **kwargs: object) -> dict[str, Any]:
        raise AssertionError("terminal remote effect must not be created or executed again")

    monkeypatch.setattr(resilience_action_journal, "execute_autonomous_action", forbidden_execute)

    result = resilience_wave_executor.run_next_wave(schedule_id, instance_id="replay-worker")

    assert result["status"] == "COMPLETED"
    action = resilience_wave_executor.list_wave_actions(schedule_id)[0]
    assert action["status"] == "VERIFIED_SUCCESS"
    assert action["journalExecutionEpoch"] == 4


def test_wave_takeover_resumes_existing_action_journal_effect_without_recreate(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fresh_state(monkeypatch)
    schedule = _schedule("journal-takeover")
    schedule_id = str(schedule["scheduleId"])
    action_payload = schedule["executionWaves"][0]["actions"][0]
    action_id = str(action_payload["actionId"])
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)
    resilience_action_journal.record_action_intent(
        action_payload,
        created_by="resilience-wave-runner",
        plan_id=schedule_id,
        input_risk_digest=RISK_DIGEST,
    )
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    claimed, action, _reason = resilience_action_journal.admit_and_claim_action(
        action_id,
        owner_instance_id="killed-worker",
        lease_seconds=1,
        now=old,
        enforce_budgets=False,
    )
    assert claimed is True and action is not None
    resilience_action_journal.update_action_state(
        action_id,
        "EXECUTING",
        execution_epoch=int(action["executionEpoch"]),
        claim_token=str(action["claimToken"]),
        effect_class="CANCELABLE",
        effect_handle={"kind": "repair", "repairId": "repair-existing"},
        lease_until="2000-01-01T00:00:00Z",
        now=old,
    )

    create_calls: list[dict[str, object]] = []
    monkeypatch.setattr(autonomous_action_policy, "validate_action_admission", lambda value: (True, "admitted"))
    monkeypatch.setattr(
        resilience_effect_reconciler,
        "reconcile_action_effect",
        lambda value, **kwargs: (
            "RESUME_EXECUTION",
            {
                "repairId": "repair-existing",
                "job": {
                    "repairId": "repair-existing",
                    "policyId": "policy-a",
                    "backupId": "backup-0",
                    "resilienceActionId": action_id,
                },
            },
        ),
    )
    monkeypatch.setattr(resilience_action_journal, "check_action_freshness", lambda value, snapshot: (True, "fresh"))
    monkeypatch.setattr(resilience_action_journal, "simulate_action", lambda value: (True, {"simulated": True}))
    monkeypatch.setattr(
        resilience_risk_engine,
        "assess_risks",
        lambda **kwargs: {"riskDigest": "4" * 64, "overallRisk": "warning", "risks": []},
    )
    def unexpected_create(**kwargs: Any) -> dict[str, str]:
        create_calls.append(kwargs)
        return {"repairId": "duplicate"}

    monkeypatch.setattr(backup_replication, "create_repair_job", unexpected_create)
    monkeypatch.setattr(
        backup_replication,
        "read_repair_job",
        lambda repair_id: {
            "repairId": repair_id,
            "policyId": "policy-a",
            "backupId": "backup-0",
            "resilienceActionId": action_id,
        },
    )
    monkeypatch.setattr(
        backup_replication,
        "execute_replica_repair",
        lambda **kwargs: {"status": "SUCCESS", "bytesTransferred": 4096, "durationMs": 900},
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "verify_action_outcome",
        lambda value, result: (True, {"verified": True}),
    )
    monkeypatch.setattr(
        resilience_action_journal,
        "verify_scoped_risk_reduction",
        lambda value, before, after: (True, {"effectObserved": True, "severityBefore": "warning", "severityAfter": "healthy"}),
    )

    result = resilience_wave_executor.run_next_wave(schedule_id, instance_id="takeover-worker")

    assert result["status"] == "COMPLETED"
    assert create_calls == []
    terminal = resilience_action_journal.get_action(action_id)
    assert terminal is not None and terminal["state"] == "SUCCEEDED"
    assert terminal["executionEpoch"] == 2
    assert terminal["effectHandle"] == {"kind": "repair", "repairId": "repair-existing"}
    assert any(event["eventType"] == "ACTION_TAKEOVER" for event in resilience_action_journal.list_action_events(action_id))


def test_stale_runner_cannot_commit_after_higher_epoch_takeover(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fresh_state(monkeypatch)
    schedule = _schedule("fenced-runner")
    schedule_id = str(schedule["scheduleId"])
    action_id = f"{schedule_id}-repair-0"
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)
    assert resilience_wave_executor.admit_wave(schedule_id, 0)["admitted"] is True
    claim = resilience_wave_executor._claim_runner_epochs(  # noqa: SLF001 - explicit fencing contract
        schedule_id,
        0,
        instance_id="worker-a",
        lease_seconds=30,
        now=datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc),
    )
    assert claim["claimed"] is True
    action_claim = resilience_wave_executor._claim_wave_action(  # noqa: SLF001 - explicit fencing contract
        schedule_id,
        0,
        action_id,
        instance_id="worker-a",
        lease_until=claim["leaseUntil"],
        schedule_execution_epoch=claim["scheduleExecutionEpoch"],
        wave_execution_epoch=claim["waveExecutionEpoch"],
        runner_token=claim["runnerToken"],
        now=datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc),
    )
    assert action_claim["claimed"] is True
    with sqlite3.connect(resilience_wave_executor.WAVE_EXECUTOR_DB) as conn:
        conn.execute(
            "UPDATE resilience_wave_schedules SET lease_until = ? WHERE schedule_id = ?",
            ("2026-08-30T08:59:59Z", schedule_id),
        )
        conn.execute(
            "UPDATE resilience_wave_states SET lease_until = ? WHERE schedule_id = ? AND wave_index = 0",
            ("2026-08-30T08:59:59Z", schedule_id),
        )
    takeover = resilience_wave_executor._claim_runner_epochs(  # noqa: SLF001 - explicit fencing contract
        schedule_id,
        0,
        instance_id="worker-b",
        lease_seconds=30,
        now=datetime(2026, 8, 30, 9, 1, tzinfo=timezone.utc),
    )
    assert takeover["claimed"] is True

    action_committed = resilience_wave_executor._set_action_state_with_fence(  # noqa: SLF001 - explicit fencing contract
        schedule_id,
        0,
        action_id,
        status="VERIFIED_SUCCESS",
        action_execution_epoch=action_claim["actionExecutionEpoch"],
        schedule_execution_epoch=claim["scheduleExecutionEpoch"],
        wave_execution_epoch=claim["waveExecutionEpoch"],
        runner_token=claim["runnerToken"],
        journal_action=_successful_action(action_id),
        terminal=True,
    )
    assert action_committed is False

    committed = resilience_wave_executor._complete_wave_with_fence(  # noqa: SLF001 - explicit fencing contract
        schedule_id,
        0,
        schedule_execution_epoch=claim["scheduleExecutionEpoch"],
        wave_execution_epoch=claim["waveExecutionEpoch"],
        runner_token=claim["runnerToken"],
    )

    assert committed is False
    assert resilience_wave_executor.list_waves(schedule_id)[0]["status"] != "COMPLETED"
