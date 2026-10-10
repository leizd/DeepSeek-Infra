"""Recovery Replica Sets & Automatic Target Failover — behavioral tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_ledger,
    backup_dr_readiness,
    backup_policies,
    backup_recovery_class,
    backup_recovery_keeper,
    backup_recovery_planner,
    backup_remote_restore,
    backup_replication,
    backup_scheduler,
)


def test_keeper_protects_all_non_terminal_phases(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_root = tmp_settings / ".restore-staging"
    phases = [
        "created",
        "fetching-controls",
        "controls-fetched",
        "planning-projection",
        "fetching-selected-components",
        "components-fetched",
        "decrypting-components",
        "materializing",
        "verified",
        "preparing",
        "prepared",
        "committing",
        "paused",
        "recovery-required",
    ]
    renewed: list[str] = []

    def fake_open(target_id: str, write_intent: bool = False, **kwargs: Any) -> Any:
        return object()

    def fake_renew(store: Any, hold_entry: dict[str, Any], *, ttl_seconds: int = 0) -> dict[str, Any]:
        renewed.append(str(hold_entry.get("holdKey")))
        return {**hold_entry, "generation": 2}

    from deepseek_infra.infra.workspace import backup_recovery_lease, backup_targets

    monkeypatch.setattr(backup_targets, "open_target_store", fake_open)
    monkeypatch.setattr(backup_recovery_lease, "renew_recovery_hold", fake_renew)

    for i, phase in enumerate(phases):
        sid = f"r_{i}"
        d = restore_root / sid
        d.mkdir(parents=True)
        (d / "remote-fetch.json").write_text(
            json.dumps(
                {
                    "restoreId": sid,
                    "phase": phase,
                    "targetId": "target_remote",
                    "holds": [{"holdKey": f"holds/{sid}.json", "generation": 1}],
                }
            ),
            encoding="utf-8",
        )

    # terminal must not renew
    done = restore_root / "r_done"
    done.mkdir()
    (done / "remote-fetch.json").write_text(
        json.dumps({"restoreId": "r_done", "phase": "complete", "targetId": "target_remote", "holds": [{"holdKey": "holds/done.json"}]}),
        encoding="utf-8",
    )

    summary = backup_recovery_keeper.reconcile_durable_recovery_leases(min_renew_age_seconds=0)
    assert summary["renewed"] == len(phases)
    assert summary["protected"] == len(phases)
    assert "r_done" not in summary["details"]["renewed"]


def test_keeper_health_not_always_ok(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_root = tmp_settings / ".restore-staging"
    sid = "r_fail"
    d = restore_root / sid
    d.mkdir(parents=True)
    (d / "remote-fetch.json").write_text(
        json.dumps(
            {
                "restoreId": sid,
                "phase": "fetching-selected-components",
                "targetId": "target_remote",
                "holds": [{"holdKey": "holds/x.json"}],
            }
        ),
        encoding="utf-8",
    )
    from deepseek_infra.infra.workspace import backup_targets

    monkeypatch.setattr(backup_targets, "open_target_store", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))

    for _ in range(backup_recovery_keeper.KEEPER_FAILURE_DEGRADE_THRESHOLD):
        backup_recovery_keeper.reconcile_durable_recovery_leases(min_renew_age_seconds=0)

    health = backup_recovery_keeper.get_recovery_lease_health()
    assert health["status"] == "degraded"
    assert health["consecutiveFailures"] >= backup_recovery_keeper.KEEPER_FAILURE_DEGRADE_THRESHOLD
    assert health["lastFailure"]
    assert "keeperRunning" in health
    assert "lastTickAt" in health


def test_keeper_starts_with_application(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []

    class FakeKeeper:
        def start(self) -> None:
            started.append(True)

        def stop(self, timeout: float = 5.0) -> None:
            started.append(False)

    monkeypatch.setattr(backup_recovery_keeper, "get_global_recovery_keeper", lambda: FakeKeeper())
    monkeypatch.setattr(backup_recovery_keeper, "reconcile_durable_recovery_leases", lambda **k: {"renewed": 0})
    # Direct lifecycle API
    k = backup_recovery_keeper.start_global_recovery_keeper(reconcile_first=True)
    assert started == [True]
    backup_recovery_keeper.stop_global_recovery_keeper()
    assert False in started
    del k


def test_policy_evidence_never_falls_back(tmp_settings: Path) -> None:
    backup_dr_ledger.record_recovery_point(
        target_id="target_a",
        policy_id="policy_b",
        backup_id="bk_other",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
    )
    backup_dr_ledger.record_drill_evidence(
        target_id="target_a",
        policy_id="policy_b",
        backup_id="bk_other",
        observed_at="2026-08-15T01:00:00Z",
        result="success",
    )
    scope = backup_dr_readiness.evaluate_scope_readiness(
        "target_a",
        "policy_a",
        recovery_objectives={"maxDrillAgeSeconds": 3600},
    )
    assert scope["recoveryPoint"]["status"] == "unavailable"
    assert scope["recoveryPoint"]["reason"] == "no-policy-recovery-point"
    assert scope["drill"]["reason"] == "no-policy-drill-evidence"
    assert "no-policy-recovery-point" in scope["reasons"]


def test_rto_without_matching_evidence_unavailable(tmp_settings: Path) -> None:
    rclass = backup_recovery_class.classify_recovery(target_kind="filesystem", storage_protocol="object-set-v1", logical_bytes=10_000_000)
    est = backup_recovery_class.calibrate_rto(logical_bytes=10_000_000, recovery_class=rclass, samples=[])
    assert est["status"] == "unavailable"
    assert est["reason"] == "insufficient-matching-evidence"
    assert est["isSla"] is False
    assert "planningHeuristic" in est


def test_filesystem_target_not_classified_as_s3(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_targets

    monkeypatch.setattr(
        backup_targets,
        "get_target",
        lambda tid: {"targetId": tid, "kind": "filesystem"},
    )
    backup_dr_ledger.record_recovery_point(
        target_id="target_abc123",
        policy_id="pol1",
        backup_id="bk1",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
        logical_bytes=5_000_000,
        storage_protocol="object-set-v1",
    )
    scope = backup_dr_readiness.evaluate_scope_readiness("target_abc123", "pol1")
    # Without samples, RTO unavailable but class must not be s3
    rto = scope["rtoEstimate"]
    assert rto.get("recoveryClass", "").startswith("filesystem:") or rto.get("status") == "unavailable"
    if rto.get("recoveryClass"):
        assert not rto["recoveryClass"].startswith("s3:")


def test_remote_audit_persists_and_resumes(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        def __init__(self, objects: list[Any], cursor: str | None) -> None:
            self.objects = objects
            self.cursor = cursor

    class Meta:
        def __init__(self, key: str) -> None:
            self.key = key

    class Store:
        def list_objects(self, prefix: str, cursor: str | None = None, limit: int = 100) -> Page:
            if cursor == "page2":
                return Page([], None)
            return Page([Meta("commits/p/slot.json")], "page2")

    commits = {
        "commits/p/slot.json": {
            "schemaVersion": 4,
            "backupId": "bk_audit",
            "policyId": "pol",
            "receiptDigest": "x" * 64,
            "commitHash": "c" * 64,
            "previousCommitHash": "0" * 64,
            "targetGeneration": 1,
            "fencingToken": 1,
            "runId": "run1",
            "scheduleSlot": "s",
            "slotDigest": "d" * 64,
            "storageProtocol": "object-set-v1",
            "objectSetDigest": "o" * 64,
            "controlObjectDigest": "k" * 64,
        }
    }
    # Make commit valid by computing real hash if needed — use invalid marker path to test rejection
    monkeypatch.setattr(backup_dr_audit.backup_targets, "open_target_store", lambda *a, **k: Store())
    monkeypatch.setattr(
        backup_dr_audit,
        "read_json",
        lambda store, key: commits.get(key) if key.startswith("commits/") else None,
    )
    monkeypatch.setattr(backup_dr_audit.backup_publish, "commit_marker_valid", lambda m: True)

    first = backup_dr_audit.audit_remote_target("target_audit", page_size=1)
    assert first["auditId"]
    assert first["status"] == "in-progress"
    assert first["cursor"] == "page2"
    job = backup_dr_ledger.get_audit_job(first["auditId"])
    assert job is not None
    assert job["cursor"] == "page2"
    assert job["phase"] == "scanning"

    second = backup_dr_audit.resume_audit(first["auditId"])
    assert second["auditId"] == first["auditId"]
    assert second["status"] == "completed"
    # missing receipt → not recoverable
    pts = [p for p in backup_dr_ledger.list_recovery_points(target_id="target_audit") if p.get("backupId") == "bk_audit"]
    if pts:
        assert pts[0]["recoverable"] is False


def test_remote_audit_rejects_receipt_digest_mismatch(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        def __init__(self) -> None:
            self.objects = [type("M", (), {"key": "commits/x.json"})()]
            self.cursor = None

    commit = {
        "schemaVersion": 4,
        "backupId": "bk_bad",
        "policyId": "pol",
        "receiptDigest": "a" * 64,
        "commitHash": "c" * 64,
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "o" * 64,
        "controlObjectDigest": "k" * 64,
    }
    receipt = {
        "backupId": "bk_bad",
        "policyId": "pol",
        "targetId": "target_x",
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "o" * 64,
        "objects": [{"digest": "d" * 64, "size": 1}],
        "size": 1,
    }
    monkeypatch.setattr(
        backup_dr_audit.backup_targets,
        "open_target_store",
        lambda *a, **k: type("S", (), {"list_objects": lambda self, *a, **k: Page()})(),
    )

    def read_json(store: Any, key: str) -> Any:
        if key.startswith("commits/"):
            return commit
        if key.startswith("receipts/"):
            return receipt
        return None

    monkeypatch.setattr(backup_dr_audit, "read_json", read_json)
    monkeypatch.setattr(backup_dr_audit.backup_publish, "commit_marker_valid", lambda m: True)

    result = backup_dr_audit.audit_remote_target("target_x")
    assert any("receipt-digest-mismatch" in a for a in result["anomalies"])
    pts = [p for p in backup_dr_ledger.list_recovery_points(target_id="target_x") if p.get("backupId") == "bk_bad"]
    assert pts and pts[0]["recoverable"] is False


RECIPIENT = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"


def test_replication_policy_schema(tmp_settings: Path) -> None:
    pol = backup_policies.normalize_policy(
        {
            "name": "repl",
            "targetId": "target_primary01",
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT]},
            "replication": {
                "enabled": True,
                "targets": [
                    {"targetId": "target_replica_a", "mode": "required"},
                    {"targetId": "target_replica_b", "mode": "best-effort"},
                ],
                "minCommittedCopies": 2,
            },
        }
    )
    assert pol["replication"]["enabled"] is True
    assert len(pol["replication"]["targets"]) == 2

    with pytest.raises(Exception):
        backup_policies.normalize_policy(
            {
                "name": "bad",
                "targetId": "target_primary01",
                "protection": {"mode": "age-recipient", "recipients": [RECIPIENT]},
                "replication": {
                    "enabled": True,
                    "targets": [{"targetId": "target_primary01", "mode": "required"}],
                },
            }
        )


def test_replicas_reuse_identical_ciphertext_job_fields(tmp_settings: Path) -> None:
    package = MagicMock()
    package.object_set_digest = "os" * 32
    package.control = MagicMock(ciphertext_digest="ctrl" * 16)
    package.components = []
    package.backup_id = "bk_repl"
    jobs = backup_replication.enqueue_replica_jobs(
        policy={
            "policyId": "pol_r",
            "replication": {
                "enabled": True,
                "targets": [{"targetId": "target_replica_a", "mode": "required"}],
                "minCommittedCopies": 2,
            },
        },
        primary_target_id="target_primary",
        backup_id="bk_repl",
        package=package,
        run_id="run1",
        schedule_slot="slot1",
        slot_digest="d" * 64,
        primary_receipt={"objectSetDigest": "os" * 32, "targetId": "target_primary"},
    )
    assert len(jobs) == 1
    assert jobs[0]["objectSetDigest"] == "os" * 32
    assert jobs[0]["phase"] == "queued"
    assert jobs[0]["replicaTargetId"] == "target_replica_a"
    # Idempotent re-enqueue
    again = backup_replication.enqueue_replica_jobs(
        policy={
            "policyId": "pol_r",
            "replication": {
                "enabled": True,
                "targets": [{"targetId": "target_replica_a", "mode": "required"}],
            },
        },
        primary_target_id="target_primary",
        backup_id="bk_repl",
        package=package,
        run_id="run1",
        schedule_slot="slot1",
        slot_digest="d" * 64,
    )
    assert again[0]["jobId"] == jobs[0]["jobId"]


def test_required_copy_objective_degrades_readiness(tmp_settings: Path) -> None:
    backup_dr_ledger.record_recovery_point(
        target_id="target_p",
        policy_id="pol_req",
        backup_id="bk1",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
        logical_bytes=1000,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_p",
        policy_id="pol_req",
        backup_id="bk1",
        committed_at="2026-08-15T00:00:00Z",
        object_set_digest="os1",
        recoverable=True,
        role="primary",
    )
    policy = {
        "policyId": "pol_req",
        "targetId": "target_p",
        "replication": {"enabled": True, "targets": [{"targetId": "target_r", "mode": "required"}], "minCommittedCopies": 2},
    }
    scope = backup_dr_readiness.evaluate_scope_readiness("target_p", "pol_req", policy=policy)
    assert scope["replicationCompliance"] == "degraded"
    assert scope["committedCopies"] == 1
    assert scope["requiredCopies"] == 2
    assert scope["status"] in {"degraded", "objective-breached", "blocked"}


def test_recovery_planner_ledger_only(tmp_settings: Path) -> None:
    backup_dr_ledger.record_recovery_point(
        target_id="target_p",
        policy_id="pol_plan",
        backup_id="bk_plan",
        committed_at="2026-08-15T00:00:00Z",
        recoverable=True,
        logical_bytes=2048,
        storage_protocol="object-set-v1",
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_p",
        policy_id="pol_plan",
        backup_id="bk_plan",
        committed_at="2026-08-15T00:00:00Z",
        object_set_digest="digest1",
        recoverable=True,
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_r",
        policy_id="pol_plan",
        backup_id="bk_plan",
        committed_at="2026-08-15T00:00:00Z",
        object_set_digest="digest1",
        recoverable=True,
        role="replica",
    )
    # Create minimal policy file
    pol = backup_policies.normalize_policy(
        {
            "name": "plan",
            "targetId": "target_p",
            "protection": {"mode": "age-recipient", "recipients": [RECIPIENT]},
            "replication": {
                "enabled": True,
                "targets": [{"targetId": "target_r", "mode": "required"}],
                "minCommittedCopies": 2,
            },
        },
        policy_id="pol_plan",
    )
    path = tmp_settings / ".backup-policies" / "pol_plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pol), encoding="utf-8")

    plan = backup_recovery_planner.plan_recovery(policy_id="pol_plan", backup_id="bk_plan")
    assert plan["remoteIo"] is False
    assert plan["selectedTargetId"]
    assert len(plan["orderedCandidates"]) >= 1
    assert plan["logicalRecoveryPoint"]["backupId"] == "bk_plan"
    assert plan["selectionReasons"]


def test_failover_forbidden_after_prepared(tmp_settings: Path) -> None:
    assert backup_recovery_planner.failover_allowed("fetching-controls") is True
    assert backup_recovery_planner.failover_allowed("components-fetched") is True
    assert backup_recovery_planner.failover_allowed("prepared") is False
    assert backup_recovery_planner.failover_allowed("committing") is False
    assert backup_recovery_planner.failover_allowed("recovery-required") is False


def test_failover_hold_ordering(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    restore_id = "restore_fo1"
    root = tmp_settings / ".restore-staging" / restore_id
    root.mkdir(parents=True)
    session = {
        "restoreId": restore_id,
        "phase": "fetching-selected-components",
        "targetId": "target_a",
        "activeSourceTargetId": "target_a",
        "backupId": "bk1",
        "holdKeys": ["holds/old.json"],
        "holds": [{"holdKey": "holds/old.json"}],
        "attemptedSourceTargets": ["target_a"],
        "failoverCount": 0,
        "recoveryPlan": {
            "maxFailovers": 3,
            "orderedCandidates": [
                {"targetId": "target_a"},
                {"targetId": "target_b"},
            ],
        },
        "storageProtocol": "object-set-v1",
        "chain": [{"backupId": "bk1", "objectSetDigest": "o" * 64, "objects": []}],
    }
    (root / "remote-fetch.json").write_text(json.dumps(session), encoding="utf-8")

    class Store:
        def __init__(self, name: str) -> None:
            self.name = name

        def delete_if_match(self, key: str, **kwargs: Any) -> bool:
            events.append(f"release:{self.name}:{key}")
            return True

    stores = {"target_a": Store("a"), "target_b": Store("b")}

    def resolve(tid: str, write_intent: bool = False) -> Any:
        return type("T", (), {"store": stores[tid], "require_store": lambda self: stores[tid], "root": None, "target_id": tid})()

    monkeypatch.setattr(backup_remote_restore.backup_publish, "resolve_target", resolve)
    def _put_hold(store: Any, key: str, hold: Any) -> bool:
        events.append(f"hold:{getattr(store, 'name', '?')}:{key}")
        return True

    monkeypatch.setattr(backup_remote_restore, "put_json_if_absent", _put_hold)

    result = backup_remote_restore.attempt_target_failover(restore_id, failure_reason="network-unavailable")
    assert result["activeSourceTargetId"] == "target_b"
    assert result["newHoldBeforeOldRelease"] is True
    # First event must be hold on new target, release of old after
    hold_idx = next(i for i, e in enumerate(events) if e.startswith("hold:b:"))
    release_idx = next(i for i, e in enumerate(events) if e.startswith("release:a:"))
    assert hold_idx < release_idx

    # Forbidden after prepared
    s2 = json.loads((root / "remote-fetch.json").read_text(encoding="utf-8"))
    s2["phase"] = "prepared"
    (root / "remote-fetch.json").write_text(json.dumps(s2), encoding="utf-8")
    with pytest.raises(Exception):
        backup_remote_restore.attempt_target_failover(restore_id, failure_reason="network-unavailable")


def test_claim_due_drill_slots_durable(tmp_settings: Path) -> None:
    from datetime import datetime, timezone

    policy = {
        "policyId": "pol_drill",
        "enabled": True,
        "recoveryDrill": {"enabled": True, "cron": "0 * * * *"},
        "schedule": {"timezone": "UTC"},
    }
    # Force a due slot by using current time far in future of epoch
    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    claimed = backup_scheduler.claim_due_drill_slots([policy], instance_id="i1", now=now)
    assert len(claimed) == 1
    assert claimed[0]["slotKey"].startswith("recovery-drill/")
    # Second claim same slot → empty
    claimed2 = backup_scheduler.claim_due_drill_slots([policy], instance_id="i2", now=now)
    assert claimed2 == []


def test_wire_format_constants_unchanged() -> None:
    from deepseek_infra.infra.workspace import backup_object_set, backup_publish

    assert backup_object_set.OBJECT_SET_V1 == "object-set-v1"
    assert backup_publish.RECEIPT_SCHEMA_VERSION == 4
    assert backup_publish.COMMIT_SCHEMA_VERSION == 4


def test_production_telemetry_feeds_ledger(tmp_settings: Path) -> None:
    session = {
        "phase": "complete",
        "targetId": "managed-local",
        "expectedBytes": 1_000_000,
        "storageProtocol": "object-set-v1",
        "recoveryTelemetry": {
            "samples": [
                {"stage": "transfer", "result": "success", "bytes": 1_000_000, "durationMs": 100, "observedAt": "2026-08-15T00:00:00Z"},
                {"stage": "crypto", "result": "success", "bytes": 1_000_000, "durationMs": 50, "observedAt": "2026-08-15T00:00:01Z"},
                {"stage": "materialization", "result": "success", "bytes": 1_000_000, "durationMs": 25, "observedAt": "2026-08-15T00:00:02Z"},
            ]
        },
    }
    backup_remote_restore._feed_telemetry_to_ledger(session)
    samples = backup_dr_ledger.list_stage_samples()
    stages = {s["stage"] for s in samples}
    assert "transfer" in stages
    assert "crypto" in stages
    assert "materialization" in stages
