"""Production Wave Runner and crash-takeover contracts for release Gates B-C."""

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


def test_wave_revalidation_and_runner_claims_fail_closed(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = _fresh_bundle()
    denied["authorityState"] = {"workersAllowed": True, "mutationsAllowed": False}
    plan = _schedule("claim-guards")
    plan["riskDigest"] = ""
    plan["authorityHeadDigest"] = ""

    revalidation = resilience_wave_executor.revalidate_wave(plan, denied)

    assert set(revalidation["reasons"]) >= {
        "PLANNED_RISK_BINDING_MISSING",
        "PLANNED_AUTHORITY_BINDING_MISSING",
        "AUTHORITY_MUTATIONS_BLOCKED",
    }
    with pytest.raises(ValueError, match="lease_seconds must be positive"):
        resilience_wave_executor._claim_runner_epochs(  # noqa: SLF001 - fencing contract
            "missing",
            0,
            instance_id="worker",
            lease_seconds=0,
        )
    with pytest.raises(ValueError, match="unknown schedule"):
        resilience_wave_executor._claim_runner_epochs(  # noqa: SLF001 - fencing contract
            "missing",
            0,
            instance_id="worker",
            lease_seconds=30,
        )

    _install_fresh_state(monkeypatch)
    persisted = _schedule("claim-helper")
    schedule_id = str(persisted["scheduleId"])
    resilience_wave_executor.persist_planned_schedule(persisted, authority_head_digest=AUTHORITY_DIGEST)
    with pytest.raises(ValueError, match="unknown wave"):
        resilience_wave_executor._claim_runner_epochs(  # noqa: SLF001 - fencing contract
            schedule_id,
            9,
            instance_id="worker",
            lease_seconds=30,
        )
    not_runnable = resilience_wave_executor._claim_runner_epochs(  # noqa: SLF001 - fencing contract
        schedule_id,
        0,
        instance_id="worker",
        lease_seconds=30,
    )
    assert not_runnable == {"claimed": False, "reason": "PENDING"}

    assert resilience_wave_executor.admit_wave(schedule_id, 0)["admitted"] is True
    claimed = resilience_wave_executor._claim_runner_epochs(  # noqa: SLF001 - fencing contract
        schedule_id,
        0,
        instance_id="worker-a",
        lease_seconds=30,
    )
    assert claimed["claimed"] is True
    held = resilience_wave_executor._claim_runner_epochs(  # noqa: SLF001 - fencing contract
        schedule_id,
        0,
        instance_id="worker-b",
        lease_seconds=30,
    )
    assert held == {"claimed": False, "reason": "SCHEDULE_RUNNER_LEASE_HELD"}

    with sqlite3.connect(resilience_wave_executor.WAVE_EXECUTOR_DB) as conn:
        conn.execute(
            "UPDATE resilience_wave_schedules SET runner_token = NULL, lease_until = NULL WHERE schedule_id = ?",
            (schedule_id,),
        )
    wave_held = resilience_wave_executor._claim_runner_epochs(  # noqa: SLF001 - fencing contract
        schedule_id,
        0,
        instance_id="worker-b",
        lease_seconds=30,
    )
    assert wave_held == {"claimed": False, "reason": "WAVE_RUNNER_LEASE_HELD"}


def test_wave_action_claim_guards_terminal_and_leased_actions(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fresh_state(monkeypatch)
    schedule = _schedule("action-claim-guards")
    schedule_id = str(schedule["scheduleId"])
    action_id = str(schedule["executionWaves"][0]["actions"][0]["actionId"])
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)

    with pytest.raises(ValueError, match="unknown wave action"):
        resilience_wave_executor._claim_wave_action(  # noqa: SLF001 - fencing contract
            schedule_id,
            0,
            "missing-action",
            instance_id="worker",
            lease_until="2099-01-01T00:00:00Z",
            schedule_execution_epoch=1,
            wave_execution_epoch=1,
            runner_token="token",
        )

    for terminal_status in (
        resilience_wave_executor.ACTION_VERIFIED_SUCCESS,
        resilience_wave_executor.ACTION_FAILED,
        resilience_wave_executor.ACTION_PREEMPTED,
        resilience_wave_executor.ACTION_STALE,
    ):
        with sqlite3.connect(resilience_wave_executor.WAVE_EXECUTOR_DB) as conn:
            conn.execute(
                "UPDATE resilience_wave_actions SET status = ?, runner_token = NULL, lease_until = NULL WHERE action_id = ?",
                (terminal_status, action_id),
            )
        terminal = resilience_wave_executor._claim_wave_action(  # noqa: SLF001 - fencing contract
            schedule_id,
            0,
            action_id,
            instance_id="worker",
            lease_until="2099-01-01T00:00:00Z",
            schedule_execution_epoch=1,
            wave_execution_epoch=1,
            runner_token="token",
        )
        assert terminal == {"claimed": False, "terminal": True, "reason": terminal_status}

    with sqlite3.connect(resilience_wave_executor.WAVE_EXECUTOR_DB) as conn:
        conn.execute(
            """
            UPDATE resilience_wave_actions
            SET status = ?, runner_token = ?, lease_until = ?
            WHERE action_id = ?
            """,
            (resilience_wave_executor.ACTION_EXECUTING, "active-token", "2099-01-01T00:00:00Z", action_id),
        )
    leased = resilience_wave_executor._claim_wave_action(  # noqa: SLF001 - fencing contract
        schedule_id,
        0,
        action_id,
        instance_id="worker",
        lease_until="2099-01-01T00:00:00Z",
        schedule_execution_epoch=1,
        wave_execution_epoch=1,
        runner_token="token",
    )
    assert leased == {"claimed": False, "terminal": False, "reason": "ACTION_RUNNER_LEASE_HELD"}


def test_wave_cannot_complete_with_an_unverified_action(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fresh_state(monkeypatch)
    schedule = _schedule("incomplete-wave")
    schedule_id = str(schedule["scheduleId"])
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)
    assert resilience_wave_executor.admit_wave(schedule_id, 0)["admitted"] is True
    claim = resilience_wave_executor._claim_runner_epochs(  # noqa: SLF001 - fencing contract
        schedule_id,
        0,
        instance_id="worker",
        lease_seconds=30,
    )

    completed = resilience_wave_executor._complete_wave_with_fence(  # noqa: SLF001 - fencing contract
        schedule_id,
        0,
        schedule_execution_epoch=claim["scheduleExecutionEpoch"],
        wave_execution_epoch=claim["waveExecutionEpoch"],
        runner_token=claim["runnerToken"],
    )

    assert completed is False


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("journal-unavailable", "ACTION_JOURNAL_UNAVAILABLE"),
        ("journal-terminal-failure", "FAILED"),
        ("journal-active", "ACTION_EFFECT_IN_PROGRESS"),
    ],
)
def test_wave_runner_handles_journal_states_without_external_verification(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_status: str,
) -> None:
    _install_fresh_state(monkeypatch)
    schedule = _schedule(f"journal-state-{scenario}")
    schedule_id = str(schedule["scheduleId"])
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)

    if scenario == "journal-unavailable":
        def record_failure(*args: object, **kwargs: object) -> dict[str, Any]:
            raise OSError("journal offline")

        monkeypatch.setattr(resilience_action_journal, "record_action_intent", record_failure)
    elif scenario == "journal-terminal-failure":
        monkeypatch.setattr(
            resilience_action_journal,
            "record_action_intent",
            lambda *args, **kwargs: {"state": "FAILED_BEFORE_EFFECT", "executionEpoch": 1},
        )
    else:
        monkeypatch.setattr(
            resilience_action_journal,
            "record_action_intent",
            lambda *args, **kwargs: {
                "state": "EXECUTING",
                "executionEpoch": 1,
                "leaseUntil": "2099-01-01T00:00:00Z",
                "effectHandle": {"kind": "repair", "repairId": "existing"},
            },
        )
    monkeypatch.setattr(
        resilience_action_journal,
        "execute_autonomous_action",
        lambda *args, **kwargs: pytest.fail("journal guard must return before execution"),
    )

    result = resilience_wave_executor.run_next_wave(schedule_id)

    assert result["status"] == expected_status


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("observed-success", "COMPLETED"),
        ("observed-failure", "FAILED"),
        ("unknown-after-error", "ACTION_EXECUTION_RETRY_REQUIRED"),
        ("invalid-terminal", "FAILED"),
    ],
)
def test_wave_runner_reconciles_execution_errors_fail_closed(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_status: str,
) -> None:
    _install_fresh_state(monkeypatch)
    schedule = _schedule(f"execute-error-{scenario}")
    schedule_id = str(schedule["scheduleId"])
    action_id = str(schedule["executionWaves"][0]["actions"][0]["actionId"])
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)

    if scenario == "invalid-terminal":
        monkeypatch.setattr(
            resilience_action_journal,
            "execute_autonomous_action",
            lambda *args, **kwargs: {"actionId": action_id, "state": "UNVERIFIED"},
        )
    else:
        def execute_failure(*args: object, **kwargs: object) -> dict[str, Any]:
            raise TimeoutError("uncertain remote effect")

        monkeypatch.setattr(resilience_action_journal, "execute_autonomous_action", execute_failure)
        if scenario == "observed-success":
            observed = _successful_action(action_id, epoch=3)
        elif scenario == "observed-failure":
            observed = {"actionId": action_id, "state": "EFFECT_UNKNOWN", "executionEpoch": 3}
        else:
            observed = {"actionId": action_id, "state": "EXECUTING", "executionEpoch": 3}
        monkeypatch.setattr(resilience_action_journal, "get_action", lambda requested: observed if requested == action_id else None)

    result = resilience_wave_executor.run_next_wave(schedule_id)

    assert result["status"] == expected_status


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("execute-state-fence", "RUNNER_FENCED_OUT"),
        ("outcome-state-fence", "RUNNER_FENCED_OUT"),
        ("terminal-state-fence", "RUNNER_FENCED_OUT"),
        ("settlement-exception", "FAIR_SERVICE_SETTLEMENT_REQUIRED"),
        ("settlement-not-consumed", "FAIR_SERVICE_SETTLEMENT_REQUIRED"),
        ("final-wave-fence", "RUNNER_FENCED_OUT"),
        ("complete-fence", "RUNNER_FENCED_OUT"),
    ],
)
def test_wave_runner_fails_closed_at_each_commit_boundary(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_status: str,
) -> None:
    _install_fresh_state(monkeypatch)
    schedule = _schedule(f"commit-boundary-{scenario}")
    schedule_id = str(schedule["scheduleId"])
    resilience_wave_executor.persist_planned_schedule(schedule, authority_head_digest=AUTHORITY_DIGEST)
    monkeypatch.setattr(
        resilience_action_journal,
        "execute_autonomous_action",
        lambda action_id, **kwargs: _successful_action(action_id),
    )

    original_set_action = resilience_wave_executor._set_action_state_with_fence  # noqa: SLF001
    action_commits = 0

    def set_action(*args: Any, **kwargs: Any) -> bool:
        nonlocal action_commits
        action_commits += 1
        denied_at = {
            "execute-state-fence": 1,
            "outcome-state-fence": 2,
            "terminal-state-fence": 3,
        }.get(scenario)
        if denied_at == action_commits:
            return False
        return original_set_action(*args, **kwargs)

    monkeypatch.setattr(resilience_wave_executor, "_set_action_state_with_fence", set_action)

    original_set_wave = resilience_wave_executor._set_wave_state_with_fence  # noqa: SLF001
    wave_commits = 0

    def set_wave(*args: Any, **kwargs: Any) -> bool:
        nonlocal wave_commits
        wave_commits += 1
        if scenario == "final-wave-fence" and wave_commits == 2:
            return False
        return original_set_wave(*args, **kwargs)

    monkeypatch.setattr(resilience_wave_executor, "_set_wave_state_with_fence", set_wave)
    if scenario == "settlement-exception":
        def settlement_failure(*args: object, **kwargs: object) -> dict[str, Any]:
            raise OSError("telemetry unavailable")

        monkeypatch.setattr(resilience_scheduler_service, "settle_action_from_effect", settlement_failure)
    elif scenario == "settlement-not-consumed":
        monkeypatch.setattr(
            resilience_scheduler_service,
            "settle_action_from_effect",
            lambda *args, **kwargs: {"status": "RESERVED"},
        )
    if scenario == "complete-fence":
        monkeypatch.setattr(resilience_wave_executor, "_complete_wave_with_fence", lambda *args, **kwargs: False)

    result = resilience_wave_executor.run_next_wave(schedule_id)

    assert result["status"] == expected_status


def test_wave_runner_reports_non_runnable_and_denied_work(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="unknown schedule"):
        resilience_wave_executor.run_next_wave("missing-schedule")

    empty = _schedule("empty-schedule")
    empty["executionWaves"] = []
    resilience_wave_executor.persist_planned_schedule(empty, authority_head_digest=AUTHORITY_DIGEST)
    assert resilience_wave_executor.run_next_wave("empty-schedule")["reason"] == "NO_RUNNABLE_WAVE"

    terminal = _schedule("terminal-schedule")
    resilience_wave_executor.persist_planned_schedule(terminal, authority_head_digest=AUTHORITY_DIGEST)
    with sqlite3.connect(resilience_wave_executor.WAVE_EXECUTOR_DB) as conn:
        conn.execute(
            "UPDATE resilience_wave_schedules SET status = ? WHERE schedule_id = ?",
            (resilience_wave_executor.SCHEDULE_FAILED, "terminal-schedule"),
        )
    assert resilience_wave_executor.run_next_wave("terminal-schedule")["reason"] == resilience_wave_executor.SCHEDULE_FAILED

    denied = _schedule("admission-denied")
    resilience_wave_executor.persist_planned_schedule(denied, authority_head_digest=AUTHORITY_DIGEST)
    monkeypatch.setattr(
        resilience_wave_executor,
        "admit_wave",
        lambda *args, **kwargs: {"admitted": False, "status": "WAVE_NOT_ADMITTED", "reason": "SOURCE_UNAVAILABLE"},
    )
    assert resilience_wave_executor.run_next_wave("admission-denied")["status"] == "WAVE_NOT_ADMITTED"

    active = _schedule("claim-denied")
    resilience_wave_executor.persist_planned_schedule(active, authority_head_digest=AUTHORITY_DIGEST)
    with sqlite3.connect(resilience_wave_executor.WAVE_EXECUTOR_DB) as conn:
        conn.execute(
            "UPDATE resilience_wave_states SET status = ? WHERE schedule_id = ?",
            (resilience_wave_executor.WAVE_CLAIMING, "claim-denied"),
        )
    monkeypatch.setattr(
        resilience_wave_executor,
        "_claim_runner_epochs",
        lambda *args, **kwargs: {"claimed": False, "reason": "LEASE_HELD"},
    )
    claim_result = resilience_wave_executor.run_next_wave("claim-denied")
    assert claim_result["status"] == "WAVE_NOT_CLAIMED"


def test_wave_runner_reports_action_claim_and_initial_fence_conflicts(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fresh_state(monkeypatch)
    action_denied = _schedule("action-denied")
    resilience_wave_executor.persist_planned_schedule(action_denied, authority_head_digest=AUTHORITY_DIGEST)
    monkeypatch.setattr(
        resilience_wave_executor,
        "_claim_wave_action",
        lambda *args, **kwargs: {"claimed": False, "reason": "ACTION_RUNNER_LEASE_HELD"},
    )
    denied_result = resilience_wave_executor.run_next_wave("action-denied")
    assert denied_result["status"] == "ACTION_NOT_CLAIMED"

    fenced = _schedule("initial-fence")
    resilience_wave_executor.persist_planned_schedule(fenced, authority_head_digest=AUTHORITY_DIGEST)
    monkeypatch.setattr(resilience_wave_executor, "_set_wave_state_with_fence", lambda *args, **kwargs: False)
    fenced_result = resilience_wave_executor.run_next_wave("initial-fence")
    assert fenced_result["status"] == "RUNNER_FENCED_OUT"
