"""Unit and contract tests for Replica Self-Healing and Lifecycle Governance."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_ledger,
    backup_policies,
    backup_recovery_drill,
    backup_recovery_planner,
    backup_replication,
    backup_targets,
)


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Policy Validation Tests ─────────────────────────────────────────────────


def test_policy_validates_registered_replica_targets(tmp_settings: Path) -> None:
    # 1. Reject unregistered primary target
    with pytest.raises(AppError) as exc:
        backup_policies.create_policy({
            "policyId": "pol_bad_primary",
            "name": "Bad Primary Policy",
            "targetId": "target_unregistered_pri",
            "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        })
    assert "Unregistered primary targetId" in str(exc.value)

    # 2. Reject unregistered replica target
    with pytest.raises(AppError) as exc2:
        backup_policies.create_policy({
            "policyId": "pol_bad_replica",
            "name": "Bad Replica Policy",
            "targetId": "managed-local",
            "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
            "replication": {
                "enabled": True,
                "targets": [{"targetId": "target_unregistered_rep", "mode": "required"}],
            },
        })
    assert "Unregistered replica targetId" in str(exc2.value)


# ── Replica Job Retry and Spool Retention Tests ─────────────────────────────


def test_replica_job_durable_retry_and_terminal_phases(tmp_settings: Path) -> None:
    job = {
        "jobId": "repl_test_1",
        "policyId": "pol1",
        "backupId": "bk1",
        "mode": "required",
        "phase": "queued",
        "attempts": 0,
        "maxAttempts": 3,
        "createdAt": _utc_iso(),
        "updatedAt": _utc_iso(),
    }
    backup_replication._atomic_write(backup_replication._job_path("repl_test_1"), job)

    # Transient error -> transition to retry-wait
    failed_job = backup_replication._fail_job(job, RuntimeError("network timeout"), mode="required")
    assert failed_job["phase"] == "retry-wait"
    assert failed_job["attempts"] == 1
    assert "nextRetryAt" in failed_job

    # Missing spool error -> transition to repair-needed
    repair_job = backup_replication._fail_job(job, RuntimeError("replication spool package missing"), mode="required")
    assert repair_job["phase"] == "repair-needed"

    # Max attempts exceeded -> failed-terminal
    max_job = dict(job, attempts=3, maxAttempts=3)
    terminal_job = backup_replication._fail_job(max_job, RuntimeError("permanent error"), mode="required")
    assert terminal_job["phase"] == "failed-terminal"


# ── DR Audit Raw Receipt Bytes and Generation Continuity ───────────────────


def test_dr_audit_raw_receipt_bytes_and_generation_continuity(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw_receipt = b'{\n  "backupId": "bk_audit_1",\n  "policyId": "pol_audit",\n  "targetId": "target_dr",\n  "size": 128,\n  "storageProtocol": "object-set-v1",\n  "objectSetDigest": "' + b"a" * 64 + b'",\n  "objects": [{"digest": "' + b"b" * 64 + b'", "size": 128}]\n}\n'
    valid_digest = hashlib.sha256(raw_receipt).hexdigest()

    commits = {
        "commits/p/c1.json": {
            "schemaVersion": 4,
            "backupId": "bk_audit_1",
            "policyId": "pol_audit",
            "targetGeneration": 1,
            "previousCommitHash": "0" * 64,
            "commitHash": "1" * 64,
            "receiptDigest": valid_digest,
            "storageProtocol": "object-set-v1",
            "objectSetDigest": "a" * 64,
            "controlObjectDigest": "k" * 64,
        },
        "commits/p/c2.json": {
            "schemaVersion": 4,
            "backupId": "bk_audit_2",
            "policyId": "pol_audit",
            "targetGeneration": 3,  # Generation gap (1 -> 3)
            "previousCommitHash": "1" * 64,
            "commitHash": "2" * 64,
            "receiptDigest": valid_digest,
            "storageProtocol": "object-set-v1",
            "objectSetDigest": "a" * 64,
            "controlObjectDigest": "k" * 64,
        },
    }

    class Page:
        def __init__(self) -> None:
            self.objects = [
                type("M", (), {"key": "commits/p/c1.json"})(),
                type("M", (), {"key": "commits/p/c2.json"})(),
            ]
            self.cursor = None

    class MockStore:
        def list_objects(self, *a: Any, **k: Any) -> Page:
            return Page()

        def get_bytes(self, key: str) -> bytes | None:
            if key.startswith("receipts/"):
                return raw_receipt
            return None

    monkeypatch.setattr(backup_dr_audit.backup_targets, "open_target_store", lambda *a, **k: MockStore())
    monkeypatch.setattr(backup_dr_audit, "read_json", lambda s, k: commits.get(k) if k in commits else (json.loads(raw_receipt.decode("utf-8")) if k.startswith("receipts/") else None))
    monkeypatch.setattr(backup_dr_audit.backup_publish, "commit_marker_valid", lambda m: True)

    audit_res = backup_dr_audit.audit_remote_target("target_dr")
    assert any("generation-gap:1->3" in a for a in audit_res["anomalies"])


# ── Scheduled Drill Atomic Copy Selection Tests ─────────────────────────────


def test_scheduled_drill_candidate_copy_selected_atomically(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = {
        "policyId": "pol_drill",
        "name": "Drill Policy",
        "targetId": "target_pri",
        "replication": {
            "enabled": True,
            "targets": [{"targetId": "target_rep_1", "mode": "required"}],
        },
    }
    # Record copy on target_rep_1
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_rep_1",
        policy_id="pol_drill",
        backup_id="bk_rep_1",
        committed_at="2026-08-16T12:00:00Z",
        recoverable=True,
        state="healthy",
    )

    cand = backup_recovery_drill._select_drill_candidate_copy(policy, "pol_drill", fallback_target_id="target_pri")
    assert cand is not None
    target_id, backup_id = cand
    assert target_id == "target_rep_1"
    assert backup_id == "bk_rep_1"


# ── Pure Ciphertext-Plane Replica Self-Healing ──────────────────────────────


def test_replica_repair_pure_ciphertext_plane(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pri_root = tmp_settings / "targets_dir" / "pri"
    rep_root = tmp_settings / "targets_dir" / "rep"
    pri_root.mkdir(parents=True, exist_ok=True)
    rep_root.mkdir(parents=True, exist_ok=True)

    t_src = backup_targets.init_target(pri_root, label="source")
    t_dst = backup_targets.init_target(rep_root, label="dest")
    src_id = str(t_src["targetId"])
    dst_id = str(t_dst["targetId"])

    # Prepare encrypted ciphertext component in source
    comp_data = b"AGE-ENCRYPTED-CIPHERTEXT-SAMPLE-12345"
    comp_digest = hashlib.sha256(comp_data).hexdigest()
    comp_path = pri_root / "objects" / comp_digest[:2] / comp_digest[2:4] / f"{comp_digest}.age"
    comp_path.parent.mkdir(parents=True, exist_ok=True)
    comp_path.write_bytes(comp_data)

    receipt = {
        "schemaVersion": 4,
        "backupId": "bk_repair_1",
        "policyId": "pol_repair",
        "targetId": src_id,
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "objset_" + "d" * 57,
        "controlObjectDigest": "ctrl_" + "k" * 59,
        "size": len(comp_data),
        "objects": [{"digest": comp_digest, "size": len(comp_data)}],
    }
    receipt_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (pri_root / "receipts").mkdir(parents=True, exist_ok=True)
    (pri_root / "receipts" / "bk_repair_1.json").write_bytes(receipt_bytes)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "policyId": "pol_repair",
        "backupId": "bk_repair_1",
        "receiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
        "objectSetDigest": receipt["objectSetDigest"],
        "storageProtocol": "object-set-v1",
        "committedAt": _utc_iso(),
    }
    (pri_root / "commits" / "pol_repair").mkdir(parents=True, exist_ok=True)
    (pri_root / "commits" / "pol_repair" / "bk_repair_1.json").write_text(json.dumps(commit), encoding="utf-8")

    # Record source copy in ledger
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=src_id,
        policy_id="pol_repair",
        backup_id="bk_repair_1",
        committed_at=_utc_iso(),
        recoverable=True,
        state="healthy",
    )

    # Corrupt destination component
    corrupt_path = rep_root / "objects" / comp_digest[:2] / comp_digest[2:4] / f"{comp_digest}.age"
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"CORRUPTED-BYTES-XYZ")

    # Execute repair
    result = backup_replication.execute_replica_repair(
        policy_id="pol_repair",
        backup_id="bk_repair_1",
        dest_target_id=dst_id,
        source_target_id=src_id,
    )
    assert result["status"] == "success"
    assert result["bytesRepaired"] == len(comp_data)

    # Verify destination now contains identical ciphertext
    assert corrupt_path.read_bytes() == comp_data
    # Verify corrupted version was quarantined
    quarantine_files = list((rep_root / ".quarantine").glob("*"))
    assert len(quarantine_files) == 1

    # Verify target-local receipt written
    dst_receipt_path = rep_root / "receipts" / "bk_repair_1.json"
    assert dst_receipt_path.is_file()
    dst_receipt = json.loads(dst_receipt_path.read_text(encoding="utf-8"))
    assert dst_receipt["targetId"] == dst_id

    # Verify target-local catalog appended
    cat_path = rep_root / "catalogs" / "pol_repair.jsonl"
    assert cat_path.is_file()

    # Verify ledger updated
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id="pol_repair", backup_id="bk_repair_1")
    dst_copy = [c for c in copies if c.get("targetId") == dst_id]
    assert dst_copy and dst_copy[0]["recoverable"] and dst_copy[0]["state"] == "healthy"


# ── Desired-State Reconciler Tests ──────────────────────────────────────────


def test_desired_state_reconciler_converges_and_skips_retired(tmp_settings: Path) -> None:
    pri_root = tmp_settings / "targets_dir" / "primary_rec"
    rep_root = tmp_settings / "targets_dir" / "replica_rec"
    pri_root.mkdir(parents=True, exist_ok=True)
    rep_root.mkdir(parents=True, exist_ok=True)

    t_p = backup_targets.init_target(pri_root, label="primary")
    t_r = backup_targets.init_target(rep_root, label="replica")
    p_id = str(t_p["targetId"])
    r_id = str(t_r["targetId"])

    policy = {
        "policyId": "pol_reconcile",
        "name": "Reconcile Policy",
        "targetId": p_id,
        "schedule": {"cron": "0 1 * * *", "timezone": "UTC"},
        "replication": {
            "enabled": True,
            "targets": [{"targetId": r_id, "mode": "required"}],
        },
    }
    backup_policies.create_policy(policy)

    # Point 1: Active, missing on replica -> should repair
    comp_data = b"CIPHERTEXT-OBJ-POINT-1"
    comp_digest = hashlib.sha256(comp_data).hexdigest()
    comp_p = pri_root / "objects" / comp_digest[:2] / comp_digest[2:4] / f"{comp_digest}.age"
    comp_p.parent.mkdir(parents=True, exist_ok=True)
    comp_p.write_bytes(comp_data)

    receipt = {
        "schemaVersion": 4,
        "backupId": "bk_rec_1",
        "policyId": "pol_reconcile",
        "targetId": p_id,
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "objset_1",
        "size": len(comp_data),
        "objects": [{"digest": comp_digest, "size": len(comp_data)}],
    }
    (pri_root / "receipts").mkdir(parents=True, exist_ok=True)
    r_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (pri_root / "receipts" / "bk_rec_1.json").write_bytes(r_bytes)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "policyId": "pol_reconcile",
        "backupId": "bk_rec_1",
        "receiptDigest": hashlib.sha256(r_bytes).hexdigest(),
        "objectSetDigest": receipt["objectSetDigest"],
        "storageProtocol": "object-set-v1",
        "committedAt": _utc_iso(),
    }
    (pri_root / "commits" / "pol_reconcile").mkdir(parents=True, exist_ok=True)
    (pri_root / "commits" / "pol_reconcile" / "bk_rec_1.json").write_text(json.dumps(commit), encoding="utf-8")

    backup_dr_ledger.record_logical_recovery_copy(
        target_id=p_id,
        policy_id="pol_reconcile",
        backup_id="bk_rec_1",
        committed_at=_utc_iso(),
        recoverable=True,
        state="healthy",
    )

    # Point 2: Retired point -> must be skipped
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=p_id,
        policy_id="pol_reconcile",
        backup_id="bk_rec_retired",
        committed_at=_utc_iso(),
        recoverable=True,
        state="healthy",
    )
    backup_dr_ledger.mark_logical_recovery_point_retired("pol_reconcile", "bk_rec_retired")

    # Run reconciler
    report = backup_replication.reconcile_policy_replicas("pol_reconcile")
    assert report["status"] == "completed"
    assert report["scannedPoints"] == 1  # only point 1 scanned, retired skipped
    assert report["repairsTriggered"] == 1
    assert report["repairsSucceeded"] == 1

    # Verify replica now has point 1
    rep_copies = backup_dr_ledger.list_logical_recovery_copies(policy_id="pol_reconcile", backup_id="bk_rec_1")
    assert any(c.get("targetId") == r_id and c.get("recoverable") for c in rep_copies)


# ── Recovery Planner Lexicographic Ranking Tests ────────────────────────────


def test_planner_lexicographic_ranking(tmp_settings: Path) -> None:
    # Record copies across 2 targets
    backup_dr_ledger.record_recovery_point(
        target_id="target_1",
        policy_id="pol_plan",
        backup_id="bk_plan_1",
        committed_at=_utc_iso(),
        recoverable=True,
        logical_bytes=1000,
    )
    backup_dr_ledger.record_recovery_point(
        target_id="target_2",
        policy_id="pol_plan",
        backup_id="bk_plan_1",
        committed_at=_utc_iso(),
        recoverable=True,
        logical_bytes=1000,
    )
    # Give target_2 target-health-ok
    backup_dr_ledger.record_target_evidence(
        target_id="target_2",
        observed_at=_utc_iso(),
        status="ok",
        scheduled_ready=True,
    )

    plan = backup_recovery_planner.plan_recovery(
        policy_id="pol_plan",
        backup_id="bk_plan_1",
        preferred_target_id="target_2",
    )
    assert plan["selectedTargetId"] == "target_2"


# ── Replica Lag Telemetry and Objective Tests ───────────────────────────────


def test_replica_lag_telemetry_and_objectives(tmp_settings: Path) -> None:
    # Primary at T=0
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="managed-local",
        policy_id="pol_lag",
        backup_id="bk_lag_pri",
        committed_at="2026-08-16T12:00:00Z",
        recoverable=True,
        state="healthy",
    )
    # Replica at T - 3600s
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_lag_rep",
        policy_id="pol_lag",
        backup_id="bk_lag_old",
        committed_at="2026-08-16T11:00:00Z",
        recoverable=True,
        state="healthy",
    )

    policy = {
        "policyId": "pol_lag",
        "name": "Lag Policy",
        "targetId": "managed-local",
        "recoveryObjectives": {"maxReplicaLagSeconds": 1800},  # 30 min max lag
        "replication": {
            "enabled": True,
            "targets": [{"targetId": "target_lag_rep", "mode": "required"}],
        },
    }

    comp = backup_replication.replication_compliance(policy=policy, backup_id="bk_lag_pri")
    assert comp["compliance"] == "degraded"
    assert any("replica-lag-exceeded:target_lag_rep" in r for r in comp["reasons"])
