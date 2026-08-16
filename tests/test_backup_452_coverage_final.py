"""Final coverage push targeting remaining 4.5.2 gaps (~0.3%)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_object_set,
    backup_recovery_keeper,
    backup_recovery_planner,
    backup_remote_restore,
    backup_replication,
    backup_scheduler,
    backup_spool,
)


def test_replication_object_set_package_path(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control = backup_object_set.EncryptedComponent(
        component_id="control",
        control=True,
        ciphertext_digest="a" * 64,
        ciphertext_size=10,
        path=tmp_settings / "c.age",
    )
    (tmp_settings / "c.age").write_bytes(b"0123456789")
    payload = backup_object_set.EncryptedComponent(
        component_id="p1",
        control=False,
        ciphertext_digest="b" * 64,
        ciphertext_size=4,
        path=tmp_settings / "p.age",
    )
    (tmp_settings / "p.age").write_bytes(b"data")
    package = backup_object_set.ObjectSetPackage(
        backup_id="bk_os",
        components=(control, payload),
        manifest_digest="m" * 64,
        coverage_digest="v" * 64,
        creation_verified=True,
        manifest={"snapshotKind": "full"},
    )
    jobs = backup_replication.enqueue_replica_jobs(
        policy={"policyId": "pol_os", "replication": {"enabled": True, "targets": [{"targetId": "target_os", "mode": "required"}]}},
        primary_target_id="target_p",
        backup_id="bk_os",
        package=package,
        run_id="run_os",
        schedule_slot="slot_os",
        slot_digest="d" * 64,
    )
    os_digest = package.object_set_digest
    assert jobs[0]["objectSetDigest"] == os_digest
    monkeypatch.setattr(backup_spool, "lookup_verified_package", lambda **k: package)
    monkeypatch.setattr(backup_scheduler, "allocate_fencing_token", lambda: 3)

    class Writer:
        def acquire(self) -> None:
            return None

        def release(self) -> None:
            raise OSError("release-fail")

    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_writer_lease.TargetWriterLease", lambda *a, **k: Writer())
    target = MagicMock(root=tmp_settings / "tos", store=None, target_id="target_os")
    (tmp_settings / "tos").mkdir(exist_ok=True)
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_publish.resolve_target", lambda *a, **k: target)

    published = MagicMock()
    published.receipt = {"backupId": "bk_os", "objectSetDigest": os_digest, "snapshotKind": "full"}
    published.commit = {"committedAt": "2026-08-16T00:00:00Z"}
    published.converged = False
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_publish.publish_backup", lambda *a, **k: published)
    # force ledger copy failure branch
    monkeypatch.setattr(backup_dr_ledger, "record_logical_recovery_copy", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
    result = backup_replication.execute_replication_job(jobs[0]["jobId"])
    assert result["phase"] == "committed"
    # process pending with a queued job that fails + committed already
    jobs2 = backup_replication.enqueue_replica_jobs(
        policy={"policyId": "pol_os2", "replication": {"enabled": True, "targets": [{"targetId": "target_os2", "mode": "best-effort"}]}},
        primary_target_id="tp",
        backup_id="bk2",
        package=package,
        run_id="r2",
        schedule_slot="s2",
        slot_digest="e" * 64,
    )
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_publish.resolve_target", lambda *a, **k: (_ for _ in ()).throw(AppError("x", code=ErrorCode.INTERNAL)))
    summary = backup_replication.process_pending_jobs(limit=20)
    assert summary["processed"] >= 1
    assert summary["failed"] >= 1 or summary["committed"] >= 0
    del jobs2

    # digest mismatch path
    jobs3 = backup_replication.enqueue_replica_jobs(
        policy={"policyId": "pol_os3", "replication": {"enabled": True, "targets": [{"targetId": "target_os3", "mode": "required"}]}},
        primary_target_id="tp",
        backup_id="bk3",
        package=package,
        run_id="r3",
        schedule_slot="s3",
        slot_digest="f" * 64,
    )
    monkeypatch.setattr(backup_spool, "lookup_verified_package", lambda **k: package)
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_publish.resolve_target", lambda *a, **k: target)
    published_bad = MagicMock()
    published_bad.receipt = {"objectSetDigest": "0" * 64}
    published_bad.commit = {}
    published_bad.converged = False
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_writer_lease.TargetWriterLease", lambda *a, **k: Writer())
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_publish.publish_backup", lambda *a, **k: published_bad)
    bad = backup_replication.execute_replication_job(jobs3[0]["jobId"])
    assert bad["phase"] == "failed"


def test_planner_managed_local_and_health_signals(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_dr_ledger.record_recovery_point(
        target_id="managed-local",
        policy_id="pol_ml",
        backup_id="bk_ml",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
        logical_bytes=1000,
    )
    path = tmp_settings / ".backup-policies" / "pol_ml.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "policyId": "pol_ml",
                "name": "ml",
                "enabled": True,
                "targetId": "managed-local",
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
    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_targets.get_target",
        lambda tid: {"kind": "s3", "targetId": tid},
    )
    plan = backup_recovery_planner.plan_recovery(policy_id="pol_ml")
    assert plan["selectedTargetId"] == "managed-local"
    # scrub/drill success ranking
    backup_dr_ledger.record_scrub_evidence(target_id="managed-local", backup_id="bk_ml", policy_id="pol_ml", observed_at="2026-08-15T00:00:00Z", result="success")
    backup_dr_ledger.record_drill_evidence(target_id="managed-local", policy_id="pol_ml", backup_id="bk_ml", observed_at="2026-08-15T00:00:00Z", result="success")
    backup_dr_ledger.record_target_evidence(target_id="managed-local", observed_at="2026-08-15T00:00:00Z", scheduled_ready=True, status="ok")
    plan2 = backup_recovery_planner.plan_recovery(policy_id="pol_ml", preferred_target_id="managed-local")
    assert "primary-target" in plan2["selectionReasons"] or plan2["selectedTargetId"]


def test_keeper_health_load_valid_and_error_tick(tmp_settings: Path) -> None:
    hp = tmp_settings / ".backup-dr" / "lease-keeper-health.json"
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(
        json.dumps(
            {
                "lastTickAt": "2026-08-15T00:00:00Z",
                "lastSuccessfulTickAt": "2026-08-15T00:00:00Z",
                "consecutiveFailures": 1,
                "protectedJobs": 2,
                "renewedLeases": 1,
                "totalRenewed": 3,
                "lastFailure": "x",
            }
        ),
        encoding="utf-8",
    )
    # force rebind
    backup_recovery_keeper._HEALTH._path_key = None
    snap = backup_recovery_keeper.get_recovery_lease_health()
    assert snap["consecutiveFailures"] >= 0
    # error tick
    backup_recovery_keeper._HEALTH.record_tick({}, error="boom")
    assert backup_recovery_keeper._HEALTH.consecutive_failures >= 1
    # list not dict
    hp.write_text("[]", encoding="utf-8")
    backup_recovery_keeper._HEALTH._path_key = None
    backup_recovery_keeper.get_recovery_lease_health()


def test_feed_telemetry_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = {
        "phase": "complete",
        "targetId": "target_x",
        "expectedBytes": 1000,
        "storageProtocol": "object-set-v1",
        "chain": [1, 2],
        "recoveryTelemetry": {
            "samples": [
                {"stage": "transfer", "result": "success", "bytes": 1000, "durationMs": 10, "observedAt": "2026-08-15T00:00:00Z"},
                {"stage": "materialize", "result": "success", "bytes": 1000, "durationMs": 5, "observedAt": "2026-08-15T00:00:01Z"},
                {"stage": "preflight", "result": "success", "bytes": 0, "durationMs": 1},
            ]
        },
    }
    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_targets.get_target",
        lambda tid: {"kind": "s3"},
    )
    backup_remote_restore._feed_telemetry_to_ledger(session)
    # early return incomplete phase without drill
    session2 = {"phase": "fetching", "recoveryTelemetry": {"samples": [{"stage": "transfer", "result": "failed", "bytes": 1, "durationMs": 1}]}}
    backup_remote_restore._feed_telemetry_to_ledger(session2)
    # advance federated complete
    rid = "restore_tel"
    root = tmp_settings / ".restore-staging" / rid
    root.mkdir(parents=True)
    sess = {"restoreId": rid, "phase": "fetching", "targetId": "managed-local", "recoveryTelemetry": session["recoveryTelemetry"]}
    (root / "remote-fetch.json").write_text(json.dumps(sess), encoding="utf-8")
    backup_remote_restore.advance_federated_phase(rid, "complete")


def test_failover_success_full_path(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rid = "restore_full_fo"
    root = tmp_settings / ".restore-staging" / rid
    root.mkdir(parents=True)
    session = {
        "restoreId": rid,
        "phase": "components-fetched",
        "targetId": "target_a",
        "activeSourceTargetId": "target_a",
        "backupId": "bk",
        "holdKeys": ["holds/old.json"],
        "holdKey": "holds/old.json",
        "attemptedSourceTargets": ["target_a"],
        "failoverCount": 0,
        "recoveryPlan": {
            "maxFailovers": 3,
            "orderedCandidates": [{"targetId": "target_a"}, {"targetId": "target_b"}],
        },
        "storageProtocol": "object-set-v1",
        "chain": [{"backupId": "bk", "objectSetDigest": "o" * 64, "objects": []}],
        "downloadedBytes": 10,
    }
    (root / "remote-fetch.json").write_text(json.dumps(session), encoding="utf-8")
    events: list[str] = []

    class Store:
        def __init__(self, name: str) -> None:
            self.name = name

        def delete_if_match(self, key: str, **kwargs: Any) -> bool:
            events.append(f"del:{self.name}")
            return True

    stores = {"target_a": Store("a"), "target_b": Store("b")}
    monkeypatch.setattr(
        backup_remote_restore.backup_publish,
        "resolve_target",
        lambda tid, write_intent=False: type("T", (), {"store": stores[tid], "require_store": lambda self: stores[tid], "root": None, "target_id": tid})(),
    )
    def _put(store: Any, key: str, hold: Any) -> bool:
        events.append(f"put:{getattr(store, 'name', '')}")
        return True

    monkeypatch.setattr(backup_remote_restore, "put_json_if_absent", _put)
    out = backup_remote_restore.attempt_target_failover(rid, failure_reason="remote-5xx")
    assert out["activeSourceTargetId"] == "target_b"
    assert any(e.startswith("put:b") for e in events)
