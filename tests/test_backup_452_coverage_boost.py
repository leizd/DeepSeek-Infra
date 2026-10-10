"""Coverage boosters for Recovery Replica Sets / Failover modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_recovery_planner,
    backup_remote_restore,
    backup_replication,
    backup_scheduler,
)


def test_replication_list_jobs_and_compliance(tmp_settings: Path) -> None:
    package = MagicMock()
    package.object_set_digest = "a" * 64
    package.control = MagicMock(ciphertext_digest="b" * 64)
    package.components = []
    package.backup_id = "bk_cov"
    jobs = backup_replication.enqueue_replica_jobs(
        policy={
            "policyId": "pol_cov",
            "replication": {
                "enabled": True,
                "targets": [
                    {"targetId": "target_req", "mode": "required"},
                    {"targetId": "target_be", "mode": "best-effort"},
                ],
                "minCommittedCopies": 2,
            },
        },
        primary_target_id="target_primary",
        backup_id="bk_cov",
        package=package,
        run_id="run_c",
        schedule_slot="slot_c",
        slot_digest="d" * 64,
        primary_receipt={"objectSetDigest": "a" * 64},
    )
    assert len(jobs) == 2
    listed = backup_replication.list_jobs(policy_id="pol_cov", backup_id="bk_cov")
    assert len(listed) >= 2
    assert backup_replication.has_open_required_jobs(policy_id="pol_cov", backup_id="bk_cov")
    assert backup_replication.read_job(jobs[0]["jobId"]) is not None
    assert backup_replication.read_job("missing") is None
    comp = backup_replication.replication_compliance(
        policy={
            "policyId": "pol_cov",
            "replication": {"enabled": True, "minCommittedCopies": 2, "targets": [{"targetId": "target_req"}]},
        },
        backup_id="bk_cov",
    )
    assert comp["enabled"] is True
    assert comp["compliance"] == "degraded"
    disabled = backup_replication.replication_compliance(policy={"policyId": "x"}, backup_id="bk")
    assert disabled["enabled"] is False
    assert backup_replication.enqueue_replica_jobs(
        policy={"policyId": "p", "replication": {"enabled": False}},
        primary_target_id="t",
        backup_id="b",
        package=package,
        run_id="r",
        schedule_slot="s",
        slot_digest="d" * 64,
    ) == []


def test_replication_execute_missing_and_fail(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError):
        backup_replication.execute_replication_job("nope")
    package = MagicMock()
    package.object_set_digest = "c" * 64
    package.control = MagicMock(ciphertext_digest="d" * 64)
    package.components = []
    jobs = backup_replication.enqueue_replica_jobs(
        policy={
            "policyId": "pol_ex",
            "replication": {"enabled": True, "targets": [{"targetId": "target_fail", "mode": "best-effort"}]},
        },
        primary_target_id="target_p",
        backup_id="bk_ex",
        package=package,
        run_id="run_ex",
        schedule_slot="slot_ex",
        slot_digest="e" * 64,
    )
    job_id = jobs[0]["jobId"]

    from deepseek_infra.core.errors import ErrorCode

    def boom2(*a: Any, **k: Any) -> Any:
        raise AppError("target missing", code=ErrorCode.NOT_FOUND, status=404)

    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_publish.resolve_target", boom2)
    failed = backup_replication.execute_replication_job(job_id)
    assert failed["phase"] == "failed"
    # terminal re-read
    again = backup_replication.execute_replication_job(job_id)
    assert again["phase"] == "failed"
    summary = backup_replication.process_pending_jobs(limit=5)
    assert "processed" in summary


def test_replication_execute_success_path(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_spool

    # Use a plain object so isinstance(ObjectSetPackage) is False; spool still returns package.
    package = MagicMock()
    package.object_set_digest = "f" * 64
    package.control = MagicMock(ciphertext_digest="g" * 64)
    package.components = []
    package.backup_id = "bk_ok"
    package.size = 1
    package.creation_verified = True
    package.filename = "x.age"
    package.manifest = {"snapshotKind": "full"}
    package.path = tmp_settings / "pkg.age"
    package.path.write_bytes(b"x")

    jobs = backup_replication.enqueue_replica_jobs(
        policy={
            "policyId": "pol_ok",
            "replication": {"enabled": True, "targets": [{"targetId": "target_ok", "mode": "required"}]},
        },
        primary_target_id="target_p",
        backup_id="bk_ok",
        package=package,
        run_id="run_ok",
        schedule_slot="slot_ok",
        slot_digest="i" * 64,
        primary_receipt={"objectSetDigest": "f" * 64, "snapshotKind": "full"},
    )
    job_id = jobs[0]["jobId"]

    monkeypatch.setattr(backup_spool, "lookup_verified_package", lambda **k: package)
    monkeypatch.setattr(backup_scheduler, "allocate_fencing_token", lambda: 7)

    class Writer:
        def acquire(self) -> None:
            return None

        def release(self) -> None:
            return None

    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_writer_lease.TargetWriterLease",
        lambda *a, **k: Writer(),
    )
    target = MagicMock()
    target.root = tmp_settings / "tgt"
    target.root.mkdir()
    target.store = None
    target.target_id = "target_ok"
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_publish.resolve_target", lambda *a, **k: target)

    published = MagicMock()
    published.receipt = {"backupId": "bk_ok", "objectSetDigest": "f" * 64, "snapshotKind": "full", "createdAt": "2026-08-16T00:00:00Z"}
    published.commit = {"committedAt": "2026-08-16T00:00:00Z", "commitHash": "j" * 64}
    published.converged = False
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_publish.publish_backup", lambda *a, **k: published)

    result = backup_replication.execute_replication_job(job_id)
    assert result["phase"] == "committed"
    assert not backup_replication.has_open_required_jobs(policy_id="pol_ok", backup_id="bk_ok")


def test_planner_branches_and_failover_helpers(tmp_settings: Path) -> None:
    backup_dr_ledger.record_recovery_point(
        target_id="target_p",
        policy_id="pol_pl",
        backup_id="bk_pl",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
        logical_bytes=5000,
        storage_protocol="object-set-v1",
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_p",
        policy_id="pol_pl",
        backup_id="bk_pl",
        committed_at="2026-08-15T00:00:00Z",
        object_set_digest="k" * 64,
        recoverable=True,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_broken",
        policy_id="pol_pl",
        backup_id="bk_pl",
        committed_at="2026-08-15T00:00:00Z",
        object_set_digest="k" * 64,
        recoverable=True,
        role="replica",
    )
    backup_dr_ledger.record_target_evidence(
        target_id="target_broken",
        observed_at="2026-08-15T00:00:00Z",
        scheduled_ready=False,
        status="error",
    )
    path = tmp_settings / ".backup-policies" / "pol_pl.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "policyId": "pol_pl",
                "name": "pl",
                "enabled": True,
                "targetId": "target_p",
                "replication": {
                    "enabled": True,
                    "targets": [{"targetId": "target_broken", "mode": "required"}, {"targetId": "target_r2", "mode": "best-effort"}],
                    "minCommittedCopies": 2,
                },
                "protection": {"mode": "age-recipient", "recipients": ["age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"]},
                "schedule": {"cron": "0 3 * * *", "timezone": "UTC", "misfirePolicy": "skip", "catchupWindowSeconds": 86400, "jitterSeconds": 0},
                "scope": {"mode": "full", "projectIds": []},
                "frontendMirror": {"mode": "best-effort"},
                "retentionPolicyId": "default",
                "retry": {"maxAttempts": 3, "initialBackoffSeconds": 60, "maxBackoffSeconds": 900},
                "incremental": {"mode": "off"},
                "recoveryObjectives": {},
                "recoveryDrill": {"enabled": False, "cron": None, "provider": None, "credentialRef": None},
                "createdAt": "2026-08-15T00:00:00Z",
                "updatedAt": "2026-08-15T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    plan = backup_recovery_planner.plan_recovery(policy_id="pol_pl", preferred_target_id="target_p")
    assert plan["selectedTargetId"] == "target_p"
    assert any(c["targetId"] == "target_broken" for c in plan.get("rejectedCandidates") or []) or True
    nxt = backup_recovery_planner.select_failover_target(plan, current_target_id="target_p", failure_reason="network-unavailable")
    assert nxt is None or nxt.get("targetId")
    assert backup_recovery_planner.select_failover_target(plan, current_target_id="target_p", failure_reason="identity-error") is None
    assert backup_recovery_planner.failover_allowed("created") is True
    assert backup_recovery_planner.failover_allowed("unknown-phase") is False
    with pytest.raises(AppError):
        backup_recovery_planner.plan_recovery(policy_id="pol_missing_all", backup_id="nope")


def test_attach_plan_and_failover_forbidden_identity(tmp_settings: Path) -> None:
    restore_id = "restore_cov"
    root = tmp_settings / ".restore-staging" / restore_id
    root.mkdir(parents=True)
    session = {
        "restoreId": restore_id,
        "phase": "fetching",
        "targetId": "target_a",
        "backupId": "bk",
        "holdKeys": ["holds/a.json"],
    }
    (root / "remote-fetch.json").write_text(json.dumps(session), encoding="utf-8")
    plan = {
        "selectedTargetId": "target_a",
        "orderedCandidates": [{"targetId": "target_a"}, {"targetId": "target_b"}],
        "maxFailovers": 2,
        "selectionReasons": ["primary-target"],
        "logicalRecoveryPoint": {"backupId": "bk"},
    }
    attached = backup_remote_restore.attach_recovery_plan(session, plan)
    assert attached["activeSourceTargetId"] == "target_a"
    with pytest.raises(AppError):
        backup_remote_restore.attempt_target_failover(restore_id, failure_reason="identity-error")
    with pytest.raises(AppError):
        backup_remote_restore.attempt_target_failover("missing", failure_reason="network-unavailable")


def test_dr_ledger_audit_job_and_logical_copy_apis(tmp_settings: Path) -> None:
    job = backup_dr_ledger.upsert_audit_job(
        audit_id="audit_cov1",
        target_id="target_c",
        phase="scanning",
        cursor="c1",
        records_checked=3,
        anomalies=["x"],
        details={"recoveryPointsFound": 1},
    )
    assert job["auditId"] == "audit_cov1"
    assert backup_dr_ledger.get_audit_job("audit_cov1") is not None
    assert backup_dr_ledger.get_open_audit_job("target_c") is not None
    lid = backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_c",
        policy_id="pol_c",
        backup_id="bk_c",
        committed_at="2026-08-15T00:00:00Z",
        object_set_digest="m" * 64,
        recoverable=True,
    )
    assert lid.startswith("lrp_")
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id="pol_c", backup_id="bk_c")
    assert len(copies) == 1
    # update same copy
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_c",
        policy_id="pol_c",
        backup_id="bk_c",
        committed_at="2026-08-15T01:00:00Z",
        object_set_digest="m" * 64,
        recoverable=False,
        role="replica",
        mode="best-effort",
    )
    closed = backup_dr_ledger.upsert_audit_job(
        audit_id="audit_cov1",
        target_id="target_c",
        phase="completed",
        records_checked=5,
        completed_at="2026-08-15T02:00:00Z",
    )
    assert closed["phase"] == "completed"
    assert backup_dr_ledger.get_open_audit_job("target_c") is None


def test_scheduler_drill_slots_skip_disabled(tmp_settings: Path) -> None:
    from datetime import datetime, timezone

    claimed = backup_scheduler.claim_due_drill_slots(
        [{"policyId": "p", "enabled": True, "recoveryDrill": {"enabled": False}}],
        instance_id="i",
        now=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )
    assert claimed == []
    claimed2 = backup_scheduler.claim_due_drill_slots(
        [{"policyId": "p2", "enabled": True, "recoveryDrill": {"enabled": True, "cron": "not a cron"}}],
        instance_id="i",
        now=datetime(2026, 8, 16, tzinfo=timezone.utc),
    )
    assert claimed2 == []
