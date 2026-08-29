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
)


class _FailingCommitConnection:
    def __init__(self, message: str) -> None:
        self.message = message

    def execute(self, _statement: str) -> None:
        raise sqlite3.OperationalError(self.message)


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

    actual_crash = {
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
                "eventType": "STATE_TRANSITION",
                "state": "EXECUTING",
                "executionEpoch": 1,
                "ownerInstanceId": "worker-101",
                "effectHandle": {"kind": "repair", "repairId": "repair-live"},
                "createdAt": "2026-08-28T12:00:01Z",
            },
            {
                "eventType": "ACTION_TAKEOVER",
                "state": "RECONCILING",
                "executionEpoch": 2,
                "ownerInstanceId": "worker-202",
                "effectHandle": {"kind": "repair", "repairId": "repair-live"},
                "createdAt": "2026-08-28T12:00:11Z",
            },
        ],
    }
    assert evidence_proof.validate_check(
        "takeoverDoesNotCreateSecondRepairJob",
        {"status": "PASS", "evidence": actual_crash},
    ) == []


def test_action_journal_commit_only_ignores_absent_transaction() -> None:
    resilience_action_journal._commit(_FailingCommitConnection("cannot commit - no transaction is active"))  # type: ignore[arg-type]  # noqa: SLF001
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        resilience_action_journal._commit(_FailingCommitConnection("disk I/O error"))  # type: ignore[arg-type]  # noqa: SLF001
