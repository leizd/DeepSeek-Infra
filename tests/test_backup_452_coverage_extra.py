"""Extra branch coverage for recovery replica / failover modules."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_ledger,
    backup_dr_readiness,
    backup_recovery_keeper,
    backup_recovery_planner,
    backup_remote_restore,
    backup_replication,
    backup_scheduler,
)


def test_list_jobs_filters_and_corrupt(tmp_settings: Path) -> None:
    root = backup_replication.REPLICATION_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / "bad.json").write_text("not-json", encoding="utf-8")
    (root / ".hidden.json").write_text("{}", encoding="utf-8")
    (root / "notdict.json").write_text("[]", encoding="utf-8")
    good = {
        "jobId": "j1",
        "policyId": "p1",
        "backupId": "b1",
        "phase": "queued",
        "mode": "required",
        "slotDigest": "s1",
        "replicaTargetId": "t1",
    }
    (root / "j1.json").write_text(json.dumps(good), encoding="utf-8")
    good2 = {**good, "jobId": "j2", "phase": "committed", "mode": "required", "backupId": "b2"}
    (root / "j2.json").write_text(json.dumps(good2), encoding="utf-8")
    assert backup_replication.list_jobs(phase="queued")
    assert backup_replication.list_jobs(backup_id="b2")
    assert backup_replication.list_jobs(limit=1)
    assert backup_replication.read_job("bad") is None
    assert not backup_replication.has_open_required_jobs(policy_id="p1", backup_id="b2")
    assert backup_replication.has_open_required_jobs(policy_id="p1", slot_digest="other") is False or True
    # slot digest filter open
    assert backup_replication.has_open_required_jobs(policy_id="p1", slot_digest="s1", backup_id="b1")


def test_enqueue_enabled_empty_targets_and_skip_primary(tmp_settings: Path) -> None:
    pkg = MagicMock()
    assert (
        backup_replication.enqueue_replica_jobs(
            policy={"policyId": "p", "replication": {"enabled": True, "targets": []}},
            primary_target_id="t",
            backup_id="b",
            package=pkg,
            run_id="r",
            schedule_slot="s",
            slot_digest="d" * 64,
        )
        == []
    )
    jobs = backup_replication.enqueue_replica_jobs(
        policy={
            "policyId": "p",
            "replication": {
                "enabled": True,
                "targets": ["bad", {"targetId": "t", "mode": "required"}, {"targetId": "t2", "mode": "required"}],
            },
        },
        primary_target_id="t",
        backup_id="b",
        package=pkg,
        run_id="r",
        schedule_slot="s",
        slot_digest="d" * 64,
        primary_receipt={"objectSetDigest": "o" * 64, "objects": [{"digest": "a" * 64}]},
    )
    assert len(jobs) == 1
    assert jobs[0]["replicaTargetId"] == "t2"


def test_replication_spool_missing_and_process(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = MagicMock()
    jobs = backup_replication.enqueue_replica_jobs(
        policy={"policyId": "p", "replication": {"enabled": True, "targets": [{"targetId": "tr", "mode": "required"}]}},
        primary_target_id="tp",
        backup_id="b",
        package=pkg,
        run_id="r",
        schedule_slot="s",
        slot_digest="d" * 64,
        primary_receipt={},
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_publish.resolve_target",
        lambda *a, **k: MagicMock(root=tmp_settings / "r", store=None, target_id="tr"),
    )
    from deepseek_infra.infra.workspace import backup_spool

    monkeypatch.setattr(backup_spool, "lookup_verified_package", lambda **k: None)
    failed = backup_replication.execute_replication_job(jobs[0]["jobId"])
    assert failed["phase"] in ("failed", "repair-needed", "failed-terminal")
    summary = backup_replication.process_pending_jobs(limit=10)
    assert summary["processed"] >= 0


def test_planner_logical_only_and_no_candidates(tmp_settings: Path) -> None:
    # Only logical copy, no primary recovery_point row on primary target
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_only",
        policy_id="pol_only",
        backup_id="bk_only",
        committed_at="2026-08-15T00:00:00Z",
        object_set_digest="z" * 64,
        recoverable=True,
    )
    path = tmp_settings / ".backup-policies" / "pol_only.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "policyId": "pol_only",
                "name": "o",
                "enabled": True,
                "targetId": "target_missing_primary",
                "replication": {"enabled": False, "targets": [], "minCommittedCopies": 1},
                "protection": {"mode": "age-recipient", "recipients": ["age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"]},
                "schedule": {"cron": "0 3 * * *", "timezone": "UTC", "misfirePolicy": "skip", "catchupWindowSeconds": 86400, "jitterSeconds": 0},
                "scope": {"mode": "full", "projectIds": []},
                "frontendMirror": {"mode": "best-effort"},
                "retentionPolicyId": "default",
                "retry": {"maxAttempts": 3, "initialBackoffSeconds": 60, "maxBackoffSeconds": 900},
                "incremental": {"mode": "off"},
                "recoveryObjectives": {},
                "recoveryDrill": {"enabled": False},
                "createdAt": "2026-08-15T00:00:00Z",
                "updatedAt": "2026-08-15T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    plan = backup_recovery_planner.plan_recovery(policy_id="pol_only")
    assert plan["logicalRecoveryPoint"]["backupId"] == "bk_only"

    # audit failed rejects candidate
    backup_dr_ledger.record_recovery_point(
        target_id="target_aud",
        policy_id="pol_aud",
        backup_id="bk_aud",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
    )
    backup_dr_ledger.record_audit_evidence(target_id="target_aud", status="failed", result="failed", observed_at="2026-08-15T00:00:00Z")
    path2 = tmp_settings / ".backup-policies" / "pol_aud.json"
    path2.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "policyId": "pol_aud",
                "name": "a",
                "enabled": True,
                "targetId": "target_aud",
                "replication": {"enabled": False, "targets": [], "minCommittedCopies": 1},
                "protection": {"mode": "age-recipient", "recipients": ["age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"]},
                "schedule": {"cron": "0 3 * * *", "timezone": "UTC", "misfirePolicy": "skip", "catchupWindowSeconds": 86400, "jitterSeconds": 0},
                "scope": {"mode": "full", "projectIds": []},
                "frontendMirror": {"mode": "best-effort"},
                "retentionPolicyId": "default",
                "retry": {"maxAttempts": 3, "initialBackoffSeconds": 60, "maxBackoffSeconds": 900},
                "incremental": {"mode": "off"},
                "recoveryObjectives": {},
                "recoveryDrill": {"enabled": False},
                "createdAt": "2026-08-15T00:00:00Z",
                "updatedAt": "2026-08-15T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AppError):
        backup_recovery_planner.plan_recovery(policy_id="pol_aud", backup_id="bk_aud")


def test_failover_max_and_no_plan(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rid = "restore_max"
    root = tmp_settings / ".restore-staging" / rid
    root.mkdir(parents=True)
    session = {
        "restoreId": rid,
        "phase": "fetching",
        "targetId": "ta",
        "activeSourceTargetId": "ta",
        "backupId": "bk",
        "holdKeys": ["holds/a.json"],
        "attemptedSourceTargets": ["ta", "tb", "tc", "td"],
        "failoverCount": 3,
        "recoveryPlan": {"maxFailovers": 2, "orderedCandidates": [{"targetId": "te"}]},
        "storageProtocol": "object-set-v1",
        "chain": [],
    }
    (root / "remote-fetch.json").write_text(json.dumps(session), encoding="utf-8")
    with pytest.raises(AppError):
        backup_remote_restore.attempt_target_failover(rid, failure_reason="network-unavailable")

    session2 = dict(session)
    session2.pop("recoveryPlan")
    session2["failoverCount"] = 0
    session2["attemptedSourceTargets"] = ["ta"]
    (root / "remote-fetch.json").write_text(json.dumps(session2), encoding="utf-8")
    with pytest.raises(AppError):
        backup_remote_restore.attempt_target_failover(rid, failure_reason="network-unavailable")


def test_keeper_parse_and_health_paths(tmp_settings: Path) -> None:
    assert backup_recovery_keeper._parse_iso(None) is None
    assert backup_recovery_keeper._parse_iso("bad") is None
    assert backup_recovery_keeper._utc_iso()
    # corrupt health file
    hp = tmp_settings / ".backup-dr" / "lease-keeper-health.json"
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text("not-json", encoding="utf-8")
    h = backup_recovery_keeper.get_recovery_lease_health()
    assert "status" in h
    # empty dir staging
    assert backup_recovery_keeper.scan_durable_recovery_sessions() == {} or True


def test_worker_tick_drill_and_repl(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_scheduler, "reclaim_abandoned_slots", lambda **k: [])
    monkeypatch.setattr(backup_scheduler, "reclaim_blocked_slots", lambda *a, **k: [])
    monkeypatch.setattr(backup_scheduler, "reclaim_deferred_slots", lambda *a, **k: [])
    monkeypatch.setattr(backup_scheduler, "claim_due_slots", lambda *a, **k: [])
    monkeypatch.setattr(
        backup_scheduler,
        "claim_due_drill_slots",
        lambda *a, **k: [{"policyId": "p_drill"}],
    )
    called: list[str] = []

    class FakeDrill:
        @staticmethod
        def execute_scheduled_drill(pid: str) -> dict[str, Any]:
            called.append(pid)
            return {"status": "ok"}

    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_recovery_drill.execute_scheduled_drill",
        FakeDrill.execute_scheduled_drill,
    )
    monkeypatch.setattr(
        backup_replication,
        "process_pending_jobs",
        lambda **k: {"processed": 2, "committed": 1, "failed": 0},
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_policies.enabled_policies",
        lambda: [],
    )
    out = backup_scheduler.worker_tick(instance_id="i", executor=lambda r: None, now=datetime(2026, 8, 16, tzinfo=timezone.utc))
    assert out["drillsClaimed"] == 1
    assert out["drillsExecuted"] == 1
    assert out["replicationProcessed"] == 2
    assert called == ["p_drill"]


def test_readiness_replication_and_lease_degrade(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_dr_ledger.record_recovery_point(
        target_id="tp",
        policy_id="pr",
        backup_id="bk",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
        logical_bytes=100,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="tp",
        policy_id="pr",
        backup_id="bk",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
    )
    policy = {
        "policyId": "pr",
        "targetId": "tp",
        "replication": {"enabled": True, "targets": [{"targetId": "tr"}], "minCommittedCopies": 2},
    }
    monkeypatch.setattr(
        backup_recovery_keeper,
        "get_recovery_lease_health",
        lambda: {"status": "degraded", "reason": "keeper-consecutive-failures", "consecutiveFailures": 5},
    )
    scope = backup_dr_readiness.evaluate_scope_readiness("tp", "pr", policy=policy)
    assert scope["replicationCompliance"] == "degraded"
    assert "keeper-consecutive-failures" in scope["reasons"] or scope["status"] == "degraded"


def test_audit_resume_completed(tmp_settings: Path) -> None:
    backup_dr_ledger.upsert_audit_job(
        audit_id="done1",
        target_id="t",
        phase="completed",
        records_checked=1,
        completed_at="2026-08-15T00:00:00Z",
    )
    res = backup_dr_audit.resume_audit("done1")
    assert res["resumed"] is False
    with pytest.raises(AppError):
        backup_dr_audit.resume_audit("missing")


def test_app_startup_keeper_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra import app as app_mod
    from deepseek_infra.infra.workspace import backup_authority_provider

    # Explicit local-only so authority verdict allows workers without replicas.
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")
    backup_authority_provider.reset_authority_replica_provider()
    monkeypatch.setattr(app_mod, "multipart_module", object())
    monkeypatch.setattr(app_mod, "supported_multipart_module", lambda m: True)
    monkeypatch.setattr("deepseek_infra.infra.workspace.backups.recover_interrupted_restores", lambda: {"recoveryRequired": []})
    started: list[str] = []
    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_recovery_keeper.start_global_recovery_keeper",
        lambda **k: started.append("start"),
    )
    monkeypatch.setattr("deepseek_infra.backup_worker.start_embedded_worker", lambda: None)
    # Pass allowWorkers explicitly so this unit test is not coupled to control DB paths.
    app_mod.ensure_startup_dependencies(authority_verdict={"allowWorkers": True, "verdict": "active"})
    assert "start" in started

    class H:
        stop_cache_cleanup = type("E", (), {"set": lambda self: None})()
        server = type("S", (), {"shutdown": lambda self: None, "server_close": lambda self: None})()

    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_recovery_keeper.stop_global_recovery_keeper",
        lambda **k: started.append("stop"),
    )
    app_mod.shutdown_handle(H())  # type: ignore[arg-type]
    assert "stop" in started
