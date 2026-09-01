"""Real-process admission and crash-fencing contracts for the current release."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_writer_lease,
    evidence_proof,
    resilience_action_journal,
    resilience_resource_locks,
    resilience_wave_executor,
)


class _FailingCommitConnection:
    def __init__(self, message: str) -> None:
        self.message = message

    def execute(self, _statement: str) -> None:
        raise sqlite3.OperationalError(self.message)


def test_resource_lock_schema_initialization_preserves_caller_transaction() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute("CREATE TABLE transaction_probe (value TEXT NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO transaction_probe (value) VALUES ('pending')")

        resilience_resource_locks.ensure_locks_schema(conn)

        assert conn.in_transaction is True
        conn.execute("ROLLBACK")
        assert conn.execute("SELECT COUNT(*) FROM transaction_probe").fetchone() == (0,)
    finally:
        conn.close()


def _limits(**overrides: int) -> dict[str, int]:
    limits = {
        "maxConcurrentActions": 8,
        "maxActionsPerHour": 1000,
        "maxConcurrentPerTarget": 8,
        "maxConcurrentPerPolicy": 8,
        "maxSimultaneousFailureDomainsTouched": 8,
        "maxRebalancesPerTargetPerHour": 500,
        "maxDrillsPerPolicyPerDay": 500,
    }
    limits.update(overrides)
    return limits


def _run_process_race(tmp_settings: Path, action_ids: tuple[str, str]) -> list[dict[str, Any]]:
    nonce = uuid.uuid4().hex[:8]
    ready_dir = tmp_settings / f"process-ready-{nonce}"
    ready_dir.mkdir(parents=True, exist_ok=True)
    start_file = tmp_settings / f"process-start-{nonce}"
    script = textwrap.dedent(
        """
        import json, os, sys, time
        from pathlib import Path
        from deepseek_infra.infra.workspace import resilience_action_journal

        action_id = sys.argv[1]
        ready_dir = Path(sys.argv[2])
        start_file = Path(sys.argv[3])
        (ready_dir / f"{os.getpid()}.ready").write_text(action_id, encoding="utf-8")
        deadline = time.monotonic() + 20
        while not start_file.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("process-race-start-timeout")
            time.sleep(0.005)
        admitted, action, reason = resilience_action_journal.admit_and_claim_action(
            action_id,
            owner_instance_id=f"process-{os.getpid()}",
            lease_seconds=60,
        )
        print(json.dumps({
            "pid": os.getpid(),
            "actionId": action_id,
            "admitted": admitted,
            "reason": reason,
            "state": (action or {}).get("state"),
            "executionEpoch": (action or {}).get("executionEpoch"),
        }))
        """
    )
    environment = os.environ.copy()
    environment["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    environment["DEEPSEEK_CONTROL_AUTHORITY_MODE"] = "local-only"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, action_id, str(ready_dir), str(start_file)],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for action_id in action_ids
    ]
    deadline = time.monotonic() + 20
    while len(list(ready_dir.glob("*.ready"))) != 2:
        if time.monotonic() >= deadline:
            for process in processes:
                process.kill()
            pytest.fail("independent admission processes did not become ready")
        time.sleep(0.01)
    start_file.write_text("go", encoding="utf-8")

    results: list[dict[str, Any]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout.strip().splitlines()[-1]))
    return results


@pytest.mark.parametrize(
    ("scope", "limits", "actions", "reason_fragment"),
    [
        (
            "global",
            _limits(maxConcurrentActions=1),
            (
                {"actionId": "process-global-a", "type": "START_DR_DRILL", "parameters": {"policyId": "p-ga", "backupId": "b-ga"}},
                {"actionId": "process-global-b", "type": "START_DR_DRILL", "parameters": {"policyId": "p-gb", "backupId": "b-gb"}},
            ),
            "max-concurrent-actions-exceeded",
        ),
        (
            "target",
            _limits(maxConcurrentPerTarget=1),
            (
                {"actionId": "process-target-a", "type": "CREATE_REBALANCE_JOB", "parameters": {"policyId": "p-ta", "backupId": "b-ta", "sourceTargetId": "s-ta", "destTargetId": "shared-target"}},
                {"actionId": "process-target-b", "type": "CREATE_REBALANCE_JOB", "parameters": {"policyId": "p-tb", "backupId": "b-tb", "sourceTargetId": "s-tb", "destTargetId": "shared-target"}},
            ),
            "max-per-target-concurrent-actions-exceeded",
        ),
        (
            "policy",
            _limits(maxConcurrentPerPolicy=1),
            (
                {"actionId": "process-policy-a", "type": "START_DR_DRILL", "parameters": {"policyId": "shared-policy", "backupId": "b-pa"}},
                {"actionId": "process-policy-b", "type": "START_DR_DRILL", "parameters": {"policyId": "shared-policy", "backupId": "b-pb"}},
            ),
            "max-per-policy-concurrent-actions-exceeded",
        ),
        (
            "failure-domain",
            _limits(maxSimultaneousFailureDomainsTouched=1),
            (
                {"actionId": "process-domain-a", "type": "START_DR_DRILL", "riskSubject": {"type": "DR_STALENESS", "failureDomain": "zone-a"}, "parameters": {"policyId": "p-da", "backupId": "b-da"}},
                {"actionId": "process-domain-b", "type": "START_DR_DRILL", "riskSubject": {"type": "DR_STALENESS", "failureDomain": "zone-b"}, "parameters": {"policyId": "p-db", "backupId": "b-db"}},
            ),
            "max-failure-domains-touched-exceeded",
        ),
    ],
)
def test_independent_processes_cannot_oversubscribe_atomic_budgets(
    tmp_settings: Path,
    scope: str,
    limits: dict[str, int],
    actions: tuple[dict[str, Any], dict[str, Any]],
    reason_fragment: str,
) -> None:
    autonomous_action_policy.set_action_rate_limits(limits)
    for action in actions:
        resilience_action_journal.record_action_intent(action)

    results = _run_process_race(tmp_settings, (str(actions[0]["actionId"]), str(actions[1]["actionId"])))

    assert len({int(result["pid"]) for result in results}) == 2
    assert sum(result["admitted"] is True for result in results) == 1
    assert sum(result["admitted"] is False for result in results) == 1
    assert any(reason_fragment in str(result["reason"]) for result in results if not result["admitted"])
    check_name = {
        "global": "twoProcessesCannotOversubscribeGlobalBudget",
        "target": "twoProcessesCannotOversubscribeTargetBudget",
        "policy": "twoProcessesCannotOversubscribePolicyBudget",
        "failure-domain": "twoProcessesCannotOversubscribeFailureDomainBudget",
    }[scope]
    assert evidence_proof.validate_check(
        check_name,
        {
            "status": "PASS",
            "evidence": {
                "scope": scope,
                "processResults": results,
                "admittedCount": 1,
                "rejectedCount": 1,
            },
        },
    ) == []
    for action in actions:
        resilience_action_journal.update_action_state(str(action["actionId"]), "PREEMPTED", error="evidence-race-cleanup")


@pytest.mark.parametrize("legacy_schema", [False, True], ids=["fresh-database", "4.7.5-migration"])
def test_independent_processes_cannot_rebind_one_wave_schedule_id(
    tmp_settings: Path,
    legacy_schema: bool,
) -> None:
    nonce = uuid.uuid4().hex[:8]
    if legacy_schema:
        resilience_wave_executor.WAVE_EXECUTOR_DIR.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(resilience_wave_executor.WAVE_EXECUTOR_DB) as conn:
            conn.execute(
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
                )
                """
            )
    ready_dir = tmp_settings / f"schedule-identity-ready-{nonce}"
    ready_dir.mkdir(parents=True, exist_ok=True)
    start_file = tmp_settings / f"schedule-identity-start-{nonce}"
    script = textwrap.dedent(
        """
        import json, os, sys, time
        from pathlib import Path
        from deepseek_infra.infra.workspace import resilience_wave_executor

        backup_id = sys.argv[1]
        ready_dir = Path(sys.argv[2])
        start_file = Path(sys.argv[3])
        (ready_dir / f"{os.getpid()}.ready").write_text(backup_id, encoding="utf-8")
        deadline = time.monotonic() + 20
        while not start_file.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("schedule-identity-start-timeout")
            time.sleep(0.005)
        schedule = {
            "scheduleId": "shared-process-schedule",
            "riskDigest": "1" * 64,
            "authorityHeadDigest": "2" * 64,
            "executionWaves": [{
                "waveIndex": 0,
                "actions": [{
                    "actionId": "shared-process-action",
                    "type": "CREATE_REPAIR_JOB",
                    "parameters": {"backupId": backup_id, "policyId": "policy-a"},
                }],
            }],
        }
        try:
            record = resilience_wave_executor.persist_planned_schedule(
                schedule,
                authority_head_digest="2" * 64,
            )
            result = {
                "pid": os.getpid(),
                "status": "PERSISTED",
                "backupId": backup_id,
                "scheduleDigest": record["scheduleDigest"],
            }
        except resilience_wave_executor.ScheduleIdentityConflictError as exc:
            result = {
                "pid": os.getpid(),
                "status": exc.code,
                "backupId": backup_id,
                "existingDigest": exc.existing_digest,
                "incomingDigest": exc.incoming_digest,
            }
        print(json.dumps(result))
        """
    )
    environment = os.environ.copy()
    environment["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, backup_id, str(ready_dir), str(start_file)],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for backup_id in ("backup-a", "backup-b")
    ]
    deadline = time.monotonic() + 20
    while len(list(ready_dir.glob("*.ready"))) != 2:
        if time.monotonic() >= deadline:
            for process in processes:
                process.kill()
            pytest.fail("independent schedule processes did not become ready")
        time.sleep(0.01)
    start_file.write_text("go", encoding="utf-8")

    results: list[dict[str, Any]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout.strip().splitlines()[-1]))

    assert len({int(result["pid"]) for result in results}) == 2
    assert sorted(result["status"] for result in results) == [
        "PERSISTED",
        "SCHEDULE_IDENTITY_CONFLICT",
    ]
    winner = next(result for result in results if result["status"] == "PERSISTED")
    conflict = next(result for result in results if result["status"] == "SCHEDULE_IDENTITY_CONFLICT")
    assert conflict["existingDigest"] == winner["scheduleDigest"]
    assert conflict["incomingDigest"] != winner["scheduleDigest"]
    persisted = resilience_wave_executor.get_schedule("shared-process-schedule")
    assert persisted is not None
    assert persisted["scheduleDigest"] == winner["scheduleDigest"]
    assert persisted["schedule"]["executionWaves"][0]["actions"][0]["parameters"]["backupId"] == winner["backupId"]


def test_same_run_higher_fence_takes_over_active_writer_lease(tmp_path: Path) -> None:
    root = tmp_path / "writer-target"
    worker_a = backup_writer_lease.TargetWriterLease(
        root,
        target_id="target-b",
        owner_run_id="repair-live",
        owner_instance_id="worker-a",
        fencing_token=10,
        lease_seconds=300,
    )
    worker_a.acquire()

    worker_b = backup_writer_lease.TargetWriterLease(
        root,
        target_id="target-b",
        owner_run_id="repair-live",
        owner_instance_id="worker-b",
        fencing_token=11,
        lease_seconds=300,
    )
    worker_b.acquire()

    assert worker_b.acquired is True
    with pytest.raises(Exception, match="writer lease lost"):
        worker_a.assert_owned()
    worker_b.release()


def test_action_journal_persists_claim_and_reconciling_events(tmp_settings: Path) -> None:
    start = datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc)
    action_id = "action-event-takeover"
    resilience_action_journal.record_action_intent(
        {
            "actionId": action_id,
            "type": "CREATE_REPAIR_JOB",
            "parameters": {"policyId": "policy-a", "backupId": "backup-a", "destTargetId": "target-b"},
        }
    )
    admitted_a, action_a, _ = resilience_action_journal.admit_and_claim_action(
        action_id,
        owner_instance_id="worker-a-pid-101",
        lease_seconds=5,
        now=start,
        enforce_budgets=False,
    )
    assert admitted_a and action_a is not None
    resilience_action_journal.update_action_state(
        action_id,
        "EXECUTING",
        execution_epoch=1,
        claim_token=str(action_a["claimToken"]),
        effect_class="CANCELABLE",
        effect_handle={"kind": "repair", "repairId": "repair-live"},
    )
    admitted_b, action_b, _ = resilience_action_journal.admit_and_claim_action(
        action_id,
        owner_instance_id="worker-b-pid-202",
        lease_seconds=5,
        now=start + timedelta(seconds=6),
        enforce_budgets=False,
    )
    assert admitted_b and action_b is not None

    events = resilience_action_journal.list_action_events(action_id)

    assert [(event["state"], event["executionEpoch"]) for event in events][-3:] == [
        ("CLAIMED", 1),
        ("EXECUTING", 1),
        ("RECONCILING", 2),
    ]
    assert events[-2]["effectHandle"] == {"kind": "repair", "repairId": "repair-live"}
    assert events[-1]["ownerInstanceId"] == "worker-b-pid-202"
    assert all(event["actionId"] == action_id for event in events)


def _crash_takeover_proof() -> dict[str, Any]:
    return {
        "actionId": "action-live",
        "workerAPid": 101,
        "workerBPid": 202,
        "processAReturnCode": -9,
        "epochA": 1,
        "epochB": 2,
        "repairId": "repair-live",
        "repairPhaseAtCrash": "transferring-components",
        "reconciliationDirective": "RESUME_EXECUTION",
        "workerALeaseUntil": "2026-08-28T12:00:10Z",
        "remoteRepairJobCountBefore": 1,
        "remoteRepairJobCountAfter": 1,
        "remoteRepairJobIdsBefore": ["repair-live"],
        "remoteRepairJobIdsAfter": ["repair-live"],
        "journalEvents": [
            {
                "actionId": "action-live",
                "eventType": "STATE_TRANSITION",
                "state": "EXECUTING",
                "executionEpoch": 1,
                "ownerInstanceId": "crash-worker-a-101",
                "effectHandle": {"kind": "repair", "repairId": "repair-live"},
                "createdAt": "2026-08-28T12:00:01Z",
            },
            {
                "actionId": "action-live",
                "eventType": "ACTION_TAKEOVER",
                "state": "RECONCILING",
                "executionEpoch": 2,
                "ownerInstanceId": "takeover-worker-b-202",
                "effectHandle": {"kind": "repair", "repairId": "repair-live"},
                "createdAt": "2026-08-28T12:00:11Z",
            },
        ],
    }


def test_process_and_crash_proof_validators_reject_self_reported_flags() -> None:
    old_atomic = evidence_proof.validate_check(
        "twoProcessesCannotOversubscribeGlobalBudget",
        {
            "status": "PASS",
            "evidence": {"atomicAdmissionVerified": True, "actionId": "a", "executionEpoch": 1},
        },
    )
    assert "missing-field:processResults" in old_atomic

    old_crash = evidence_proof.validate_check(
        "takeoverEntersReconcilingBeforeMutation",
        {
            "status": "PASS",
            "evidence": {
                "actionId": "a",
                "oldEpoch": 1,
                "newEpoch": 2,
                "reconciliationDirective": "ADVANCE_TO_VERIFYING",
            },
        },
    )
    assert "missing-field:journalEvents" in old_crash

    actual_crash = _crash_takeover_proof()
    assert evidence_proof.validate_check(
        "takeoverDoesNotCreateSecondRepairJob",
        {"status": "PASS", "evidence": actual_crash},
    ) == []


def _wave_crash_takeover_proof() -> dict[str, Any]:
    proof = _crash_takeover_proof()
    proof.update(
        {
            "scheduleId": "schedule-live",
            "waveIndex": 0,
            "scheduleEpochA": 1,
            "scheduleEpochB": 2,
            "waveEpochA": 1,
            "waveEpochB": 2,
            "waveActionEpochA": 1,
            "waveActionEpochB": 2,
            "workerAScheduleLeaseUntil": "2026-08-28T12:00:10Z",
            "workerAWaveLeaseUntil": "2026-08-28T12:00:10Z",
            "workerAWaveActionLeaseUntil": "2026-08-28T12:00:05Z",
            "firstRunnerLeaseUntil": "2026-08-28T12:00:05Z",
            "renewedRunnerLeaseUntil": "2026-08-28T12:00:10Z",
            "runnerLeaseObservations": [
                {
                    "schedule": {
                        "scheduleId": "schedule-live",
                        "status": "RUNNING",
                        "scheduleExecutionEpoch": 1,
                        "ownerInstanceId": "crash-worker-a-101",
                        "leaseUntil": "2026-08-28T12:00:05Z",
                        "updatedAt": "2026-08-28T12:00:02Z",
                    },
                    "wave": {
                        "scheduleId": "schedule-live",
                        "waveIndex": 0,
                        "status": "EXECUTING",
                        "waveExecutionEpoch": 1,
                        "ownerInstanceId": "crash-worker-a-101",
                        "leaseUntil": "2026-08-28T12:00:05Z",
                        "updatedAt": "2026-08-28T12:00:02Z",
                    },
                },
                {
                    "schedule": {
                        "scheduleId": "schedule-live",
                        "status": "RUNNING",
                        "scheduleExecutionEpoch": 1,
                        "ownerInstanceId": "crash-worker-a-101",
                        "leaseUntil": "2026-08-28T12:00:10Z",
                        "updatedAt": "2026-08-28T12:00:07Z",
                    },
                    "wave": {
                        "scheduleId": "schedule-live",
                        "waveIndex": 0,
                        "status": "EXECUTING",
                        "waveExecutionEpoch": 1,
                        "ownerInstanceId": "crash-worker-a-101",
                        "leaseUntil": "2026-08-28T12:00:10Z",
                        "updatedAt": "2026-08-28T12:00:07Z",
                    },
                },
            ],
            "journalStateAtCrash": {
                "actionId": "action-live",
                "state": "EXECUTING",
                "executionEpoch": 1,
                "ownerInstanceId": "crash-worker-a-101",
                "leaseUntil": "2026-08-28T12:00:10Z",
                "effectHandle": {"kind": "repair", "repairId": "repair-live"},
            },
            "runnerStateAtCrash": {
                "schedule": {
                    "scheduleId": "schedule-live",
                    "status": "RUNNING",
                    "scheduleExecutionEpoch": 1,
                    "ownerInstanceId": "crash-worker-a-101",
                    "leaseUntil": "2026-08-28T12:00:10Z",
                },
                "wave": {
                    "scheduleId": "schedule-live",
                    "waveIndex": 0,
                    "status": "EXECUTING",
                    "waveExecutionEpoch": 1,
                    "ownerInstanceId": "crash-worker-a-101",
                    "leaseUntil": "2026-08-28T12:00:10Z",
                },
                "waveAction": {
                    "scheduleId": "schedule-live",
                    "waveIndex": 0,
                    "actionId": "action-live",
                    "status": "EXECUTING",
                    "actionExecutionEpoch": 1,
                    "scheduleExecutionEpoch": 1,
                    "waveExecutionEpoch": 1,
                    "ownerInstanceId": "crash-worker-a-101",
                    "leaseUntil": "2026-08-28T12:00:05Z",
                },
            },
            "runnerStateAtTakeoverClaim": {
                "schedule": {
                    "scheduleId": "schedule-live",
                    "status": "RUNNING",
                    "scheduleExecutionEpoch": 2,
                    "ownerInstanceId": "takeover-worker-b-202",
                    "leaseUntil": "2026-08-28T12:00:41Z",
                    "updatedAt": "2026-08-28T12:00:11Z",
                },
                "wave": {
                    "scheduleId": "schedule-live",
                    "waveIndex": 0,
                    "status": "EXECUTING",
                    "waveExecutionEpoch": 2,
                    "ownerInstanceId": "takeover-worker-b-202",
                    "leaseUntil": "2026-08-28T12:00:41Z",
                    "updatedAt": "2026-08-28T12:00:11Z",
                },
                "waveAction": {
                    "scheduleId": "schedule-live",
                    "waveIndex": 0,
                    "actionId": "action-live",
                    "status": "CLAIMED",
                    "actionExecutionEpoch": 2,
                    "scheduleExecutionEpoch": 2,
                    "waveExecutionEpoch": 2,
                    "ownerInstanceId": "takeover-worker-b-202",
                    "leaseUntil": "2026-08-28T12:00:41Z",
                    "updatedAt": "2026-08-28T12:00:11Z",
                },
            },
            "runnerStateAfterTakeover": {
                "schedule": {
                    "scheduleId": "schedule-live",
                    "status": "COMPLETED",
                    "scheduleExecutionEpoch": 2,
                    "updatedAt": "2026-08-28T12:00:20Z",
                },
                "wave": {
                    "scheduleId": "schedule-live",
                    "waveIndex": 0,
                    "status": "COMPLETED",
                    "waveExecutionEpoch": 2,
                    "updatedAt": "2026-08-28T12:00:20Z",
                },
                "waveAction": {
                    "scheduleId": "schedule-live",
                    "waveIndex": 0,
                    "actionId": "action-live",
                    "status": "VERIFIED_SUCCESS",
                    "actionExecutionEpoch": 2,
                    "scheduleExecutionEpoch": 2,
                    "waveExecutionEpoch": 2,
                    "journalExecutionEpoch": 2,
                    "effectHandle": {"kind": "repair", "repairId": "repair-live"},
                    "updatedAt": "2026-08-28T12:00:20Z",
                },
            },
            "settlementEvents": [
                {
                    "actionId": "action-live",
                    "toStatus": "CONSUMING",
                    "executionEpoch": 2,
                    "effectHandle": {"kind": "repair", "repairId": "repair-live"},
                    "createdAt": "2026-08-28T12:00:20Z",
                },
                {
                    "actionId": "action-live",
                    "toStatus": "CONSUMED",
                    "executionEpoch": 2,
                    "effectHandle": {"kind": "repair", "repairId": "repair-live"},
                    "createdAt": "2026-08-28T12:00:21Z",
                },
            ],
        }
    )
    return proof


def test_wave_crash_proof_semantically_binds_outer_leases_epochs_effect_and_settlement() -> None:
    check_names = {
        "longRunningWaveRenewsScheduleLease",
        "longRunningWaveRenewsWaveLease",
        "realProcessWaveSigkillTakeoverUsesHigherEpoch",
        "realProcessWaveSigkillDoesNotDuplicateEffect",
        "realProcessWaveSigkillSettlesExactlyOnce",
    }
    proof = _wave_crash_takeover_proof()
    for check_name in check_names:
        assert evidence_proof.validate_check(check_name, {"status": "PASS", "evidence": proof}) == []

    missing_outer = evidence_proof.validate_check(
        "realProcessWaveSigkillTakeoverUsesHigherEpoch",
        {"status": "PASS", "evidence": _crash_takeover_proof()},
    )
    assert "missing-field:scheduleId" in missing_outer

    stale_schedule = {**proof, "scheduleEpochB": 1}
    assert "schedule-execution-epoch-not-increased" in evidence_proof.validate_check(
        "realProcessWaveSigkillTakeoverUsesHigherEpoch",
        {"status": "PASS", "evidence": stale_schedule},
    )
    divergent_lease = {**proof, "workerAWaveLeaseUntil": "2026-08-28T12:00:09Z"}
    assert "schedule-wave-lease-diverged" in evidence_proof.validate_check(
        "longRunningWaveRenewsWaveLease",
        {"status": "PASS", "evidence": divergent_lease},
    )
    duplicate_settlement = {**proof, "settlementEvents": [*proof["settlementEvents"], proof["settlementEvents"][-1]]}
    assert "settlement-consumed-count-not-exactly-one" in evidence_proof.validate_check(
        "realProcessWaveSigkillSettlesExactlyOnce",
        {"status": "PASS", "evidence": duplicate_settlement},
    )
    crash_state = proof["runnerStateAtCrash"]
    tampered_runner_state = {
        **proof,
        "runnerStateAtCrash": {
            **crash_state,
            "schedule": {**crash_state["schedule"], "scheduleExecutionEpoch": 9},
        },
    }
    assert "runner-state-schedule-epoch-binding-mismatch" in evidence_proof.validate_check(
        "realProcessWaveSigkillTakeoverUsesHigherEpoch",
        {"status": "PASS", "evidence": tampered_runner_state},
    )


def _set_substring_impostor_owner(proof: dict[str, Any]) -> None:
    proof["runnerStateAtCrash"]["wave"]["ownerInstanceId"] = "impostor-crash-worker-a-1010"


def _set_negative_wave_index_everywhere(proof: dict[str, Any]) -> None:
    proof["waveIndex"] = -1
    for snapshot_name in ("runnerStateAtCrash", "runnerStateAtTakeoverClaim", "runnerStateAfterTakeover"):
        proof[snapshot_name]["wave"]["waveIndex"] = -1
        proof[snapshot_name]["waveAction"]["waveIndex"] = -1
    for observation in proof["runnerLeaseObservations"]:
        observation["wave"]["waveIndex"] = -1


def _move_takeover_claim_after_terminal_and_settlement(proof: dict[str, Any]) -> None:
    for record in proof["runnerStateAtTakeoverClaim"].values():
        record["updatedAt"] = "2026-08-28T12:00:30Z"
        record["leaseUntil"] = "2026-08-28T12:01:00Z"


def _rebind_journal_events_to_other_action(proof: dict[str, Any]) -> None:
    for event in proof["journalEvents"]:
        event["actionId"] = "other-action"


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda proof: proof.__setitem__("waveEpochB", 1), "wave-execution-epoch-not-increased"),
        (lambda proof: proof.__setitem__("waveActionEpochB", 1), "wave-action-execution-epoch-not-increased"),
        (lambda proof: proof.__setitem__("workerAScheduleLeaseUntil", "2026-08-28T12:00:10"), "invalid-workerAScheduleLeaseUntil"),
        (lambda proof: proof.__setitem__("renewedRunnerLeaseUntil", "2026-08-28T12:00:05Z"), "runner-lease-not-renewed"),
        (
            lambda proof: proof.__setitem__("workerAScheduleLeaseUntil", "2026-08-28T12:00:09Z"),
            "runner-state-schedule-lease-binding-mismatch",
        ),
        (
            lambda proof: proof.__setitem__("workerALeaseUntil", "2026-08-28T12:00:09Z"),
            "journal-state-at-crash-lease-binding-mismatch",
        ),
        (lambda proof: proof.__setitem__("runnerLeaseObservations", "invalid"), "runner-lease-observations-must-be-list"),
        (
            lambda proof: proof.__setitem__("firstRunnerLeaseUntil", "2026-08-28T12:00:01Z"),
            "first-runner-lease-not-bound-to-durable-observation",
        ),
        (
            lambda proof: proof["runnerLeaseObservations"][1]["schedule"].__setitem__(
                "leaseUntil", "2026-08-28T12:00:05Z"
            ),
            "runner-lease-observation-schedule-wave-lease-diverged",
        ),
        (lambda proof: proof.__setitem__("runnerStateAtCrash", "invalid"), "runner-state-at-crash-must-be-object"),
        (
            lambda proof: proof.__setitem__("runnerStateAtTakeoverClaim", "invalid"),
            "runner-state-at-takeover-claim-must-be-object",
        ),
        (lambda proof: proof.__setitem__("runnerStateAfterTakeover", "invalid"), "runner-state-after-takeover-must-be-object"),
        (
            lambda proof: proof["runnerStateAtCrash"].__setitem__("schedule", "invalid"),
            "runner-state-crash-schedule-must-be-object",
        ),
        (
            lambda proof: proof["runnerStateAtCrash"]["schedule"].__setitem__("scheduleId", "other"),
            "runner-state-schedule-id-binding-mismatch",
        ),
        (
            lambda proof: proof["runnerStateAtCrash"]["waveAction"].__setitem__("actionId", "other"),
            "runner-state-action-id-binding-mismatch",
        ),
        (
            lambda proof: proof["runnerStateAtCrash"]["schedule"].__setitem__("scheduleExecutionEpoch", "invalid"),
            "runner-state-crash-schedule-invalid-scheduleExecutionEpoch",
        ),
        (
            lambda proof: proof["runnerStateAtCrash"]["wave"].__setitem__("waveExecutionEpoch", 9),
            "runner-state-wave-epoch-binding-mismatch",
        ),
        (
            lambda proof: proof["runnerStateAtCrash"]["waveAction"].__setitem__("actionExecutionEpoch", 9),
            "runner-state-wave-action-epoch-binding-mismatch",
        ),
        (
            lambda proof: proof["runnerStateAtCrash"]["waveAction"].__setitem__("leaseUntil", "2026-08-28T12:00:04Z"),
            "runner-state-wave-action-lease-binding-mismatch",
        ),
        (
            lambda proof: proof["runnerStateAtCrash"]["wave"].__setitem__("ownerInstanceId", "other"),
            "runner-state-worker-a-owner-binding-mismatch",
        ),
        (_set_substring_impostor_owner, "runner-state-worker-a-owner-binding-mismatch"),
        (
            lambda proof: proof["runnerStateAtTakeoverClaim"]["wave"].__setitem__("ownerInstanceId", "other"),
            "runner-state-worker-b-owner-binding-mismatch",
        ),
        (
            lambda proof: proof["runnerStateAtTakeoverClaim"]["schedule"].__setitem__(
                "updatedAt", "2026-08-28T12:00:10Z"
            ),
            "takeover-claim-occurred-before-all-worker-a-leases-expired",
        ),
        (
            lambda proof: proof["journalEvents"][1].__setitem__("createdAt", "2026-08-28T12:00:10Z"),
            "journal-takeover-occurred-before-all-worker-a-leases-expired",
        ),
        (_rebind_journal_events_to_other_action, "missing-action-bound-journal-takeover-event"),
        (
            lambda proof: proof["runnerStateAtTakeoverClaim"]["wave"].__setitem__("waveIndex", 9),
            "runner-state-wave-index-binding-mismatch",
        ),
        (_set_negative_wave_index_everywhere, "negative-wave-index"),
        (_move_takeover_claim_after_terminal_and_settlement, "takeover-claim-not-before-terminal-runner-state"),
        (
            lambda proof: proof["runnerStateAtCrash"]["schedule"].__setitem__("status", "PAUSED_REPLAN"),
            "runner-state-crash-not-active",
        ),
        (
            lambda proof: proof["runnerStateAtCrash"]["waveAction"].__setitem__("status", "PENDING"),
            "runner-state-crash-action-not-executing",
        ),
        (
            lambda proof: proof["runnerStateAfterTakeover"]["wave"].__setitem__("status", "EXECUTING"),
            "runner-state-takeover-not-completed",
        ),
        (
            lambda proof: proof["runnerStateAfterTakeover"]["waveAction"].__setitem__("status", "EXECUTING"),
            "runner-state-takeover-action-not-verified",
        ),
        (
            lambda proof: proof["runnerStateAfterTakeover"]["waveAction"].__setitem__(
                "effectHandle", {"kind": "repair", "repairId": "other"}
            ),
            "runner-state-takeover-effect-binding-mismatch",
        ),
        (lambda proof: proof.__setitem__("settlementEvents", "invalid"), "settlement-events-must-be-list"),
        (
            lambda proof: proof.__setitem__("settlementEvents", [None, *proof["settlementEvents"]]),
            "settlement-event-not-object",
        ),
        (
            lambda proof: proof.__setitem__("settlementEvents", [{"toStatus": "RESERVED"}, proof["settlementEvents"][1]]),
            "settlement-consuming-count-not-exactly-one",
        ),
        (
            lambda proof: proof["settlementEvents"][0].__setitem__("executionEpoch", "invalid"),
            "invalid-settlement-execution-epoch",
        ),
        (
            lambda proof: proof["settlementEvents"][0].__setitem__("executionEpoch", 1),
            "settlement-execution-epoch-not-bound-to-takeover",
        ),
        (
            lambda proof: proof["settlementEvents"][0].__setitem__("effectHandle", {"kind": "repair", "repairId": "other"}),
            "settlement-effect-handle-not-bound-to-repair",
        ),
        (
            lambda proof: proof["settlementEvents"][0].__setitem__("actionId", "other"),
            "settlement-action-id-not-bound-to-action",
        ),
        (
            lambda proof: proof.__setitem__("settlementEvents", [proof["settlementEvents"][1]]),
            "settlement-consuming-count-not-exactly-one",
        ),
        (
            lambda proof: proof.__setitem__("settlementEvents", list(reversed(proof["settlementEvents"]))),
            "settlement-consumed-before-consuming",
        ),
    ],
    ids=(
        "wave-epoch",
        "wave-action-epoch",
        "naive-lease-time",
        "lease-not-renewed",
        "crash-lease-before-renewal",
        "journal-lease-not-bound",
        "lease-observations-not-list",
        "first-lease-not-bound",
        "observation-lease-divergence",
        "crash-state-not-object",
        "takeover-claim-state-not-object",
        "takeover-state-not-object",
        "crash-schedule-not-object",
        "runner-schedule-id",
        "runner-action-id",
        "runner-epoch-invalid",
        "runner-wave-epoch",
        "runner-action-epoch",
        "runner-action-lease",
        "runner-owner",
        "runner-owner-substring-impostor",
        "takeover-owner",
        "outer-takeover-before-expiry",
        "journal-takeover-before-outer-expiry",
        "journal-event-other-action",
        "wave-index",
        "negative-wave-index",
        "claim-after-terminal",
        "crash-not-active",
        "crash-action-not-active",
        "takeover-not-complete",
        "takeover-action-not-complete",
        "takeover-effect",
        "settlement-not-list",
        "settlement-event-not-object",
        "ignored-settlement-event",
        "settlement-epoch-invalid",
        "settlement-epoch-stale",
        "settlement-effect-mismatch",
        "settlement-action-mismatch",
        "missing-consuming",
        "settlement-order",
    ),
)
def test_wave_crash_proof_rejects_each_tampered_binding(mutation: Any, expected_error: str) -> None:
    proof = _wave_crash_takeover_proof()
    mutation(proof)
    errors = evidence_proof.validate_check(
        "realProcessWaveSigkillSettlesExactlyOnce",
        {"status": "PASS", "evidence": proof},
    )
    assert expected_error in errors


def test_action_journal_commit_only_ignores_absent_transaction() -> None:
    resilience_action_journal._commit(_FailingCommitConnection("cannot commit - no transaction is active"))  # type: ignore[arg-type]  # noqa: SLF001
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        resilience_action_journal._commit(_FailingCommitConnection("disk I/O error"))  # type: ignore[arg-type]  # noqa: SLF001
