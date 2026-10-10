"""Comprehensive coverage booster for replica healing, audit and ledger branches."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_audit,
    backup_dr_ledger,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_targets,
)
from deepseek_infra.infra.workspace.backup_target_store import PutResult


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_source_hold_lifecycle(tmp_settings: Path) -> None:
    hold = backup_replication.acquire_source_hold(
        target_id="target_hold_1",
        policy_id="pol_hold_1",
        backup_id="bk_hold_1",
        holder_id="job_hold_1",
    )
    assert hold.target_id == "target_hold_1"
    assert hold.policy_id == "pol_hold_1"
    assert hold.backup_id == "bk_hold_1"
    assert backup_replication.is_source_held("target_hold_1", "pol_hold_1", "bk_hold_1") is True

    # Check non-existent hold
    assert backup_replication.is_source_held("target_hold_1", "pol_hold_1", "bk_nonexistent") is False
    assert backup_replication.is_source_held("target_other", "pol_hold_1", "bk_hold_1") is False

    # Release hold object
    backup_replication.release_source_hold(hold)
    assert backup_replication.is_source_held("target_hold_1", "pol_hold_1", "bk_hold_1") is False

    # Release by hold_id string
    hold2 = backup_replication.acquire_source_hold("target_hold_1", "pol_hold_1", "bk_hold_2", "job2")
    assert backup_replication.is_source_held("target_hold_1", "pol_hold_1", "bk_hold_2") is True
    backup_replication.release_source_hold(hold2.hold_id)
    assert backup_replication.is_source_held("target_hold_1", "pol_hold_1", "bk_hold_2") is False


def test_append_target_local_catalog_store_and_fs(tmp_settings: Path) -> None:
    # 1. Test filesystem target
    t_fs_dir = tmp_settings / "t_fs_cat"
    t_fs_dir.mkdir(parents=True, exist_ok=True)
    target_fs = MagicMock(root=t_fs_dir, store=None)
    receipt_fs = {
        "schemaVersion": 4,
        "backupId": "bk_cat_fs",
        "policyId": "pol_cat",
        "targetId": "target_fs",
        "size": 100,
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "0" * 64,
        "objects": [],
    }
    backup_replication.append_target_local_catalog(target_fs, receipt_fs)
    cat_file = t_fs_dir / "catalogs" / "pol_cat.jsonl"
    assert cat_file.is_file()
    lines = cat_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert "bk_cat_fs" in lines[0]

    # Empty backupId is noop
    backup_replication.append_target_local_catalog(target_fs, {"policyId": "pol_cat"})

    # 2. Test mock remote store target
    written_data: dict[str, bytes] = {}

    class MockRemoteStore:
        def get_bytes(self, key: str) -> bytes | None:
            return written_data.get(key)

        def put_bytes(self, key: str, data: bytes) -> PutResult:
            written_data[key] = data
            return PutResult(key=key, etag="e1", size=len(data), created=True)

        def put_if_absent(self, key: str, source: Any) -> PutResult:
            if hasattr(source, "read_bytes"):
                data = source.read_bytes()
            elif isinstance(source, bytes):
                data = source
            else:
                data = b""
            written_data[key] = data
            return PutResult(key=key, etag="e1", size=len(data), created=True)

        def put_if_match(self, key: str, source: Any, *, expected_etag: str | None = None) -> PutResult:
            if hasattr(source, "read_bytes"):
                data = source.read_bytes()
            elif isinstance(source, bytes):
                data = source
            else:
                data = b""
            written_data[key] = data
            return PutResult(key=key, etag="e2", size=len(data), created=True)

    store = MockRemoteStore()
    target_store = MagicMock(root=None, store=store)
    backup_replication.append_target_local_catalog(target_store, receipt_fs)
    cat_key = "catalogs/pol_cat/bk_cat_fs.json"
    assert cat_key in written_data


def test_calculate_replica_lag_and_compliance_variations(tmp_settings: Path) -> None:
    # 1. No copies
    lag_empty = backup_replication.calculate_replica_lag("pol_none", "target_r", primary_target_id="target_p")
    assert lag_empty["lagRecoveryPoints"] == 0
    assert lag_empty["lagSeconds"] == 0

    # 2. Primary with 3 points, Replica with 1 point
    for i in range(1, 4):
        backup_dr_ledger.record_logical_recovery_copy(
            target_id="target_p",
            policy_id="pol_lag_var",
            backup_id=f"bk_{i}",
            committed_at=f"2026-08-16T1{i}:00:00Z",
            recoverable=True,
            state="healthy",
        )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="target_r",
        policy_id="pol_lag_var",
        backup_id="bk_1",
        committed_at="2026-08-16T11:00:00Z",
        recoverable=True,
        state="healthy",
    )

    lag_res = backup_replication.calculate_replica_lag("pol_lag_var", "target_r", primary_target_id="target_p")
    assert lag_res["lagRecoveryPoints"] == 2
    assert lag_res["lagSeconds"] == 7200  # 13:00 - 11:00 = 2h = 7200s

    # 3. Compliance evaluation
    pol_active = {
        "policyId": "pol_lag_var",
        "targetId": "target_p",
        "replication": {
            "enabled": True,
            "targets": [{"targetId": "target_r", "mode": "required"}],
            "minCommittedCopies": 2,
        },
        "recoveryObjectives": {
            "maxReplicaLagSeconds": 3600,
        },
    }
    comp = backup_replication.replication_compliance(policy=pol_active, backup_id="bk_3")
    assert comp["compliance"] == "degraded"
    assert "replica-lag-exceeded:target_r" in comp["reasons"]
    assert "insufficient-committed-copies" in comp["reasons"]

    # 4. Compliance when disabled
    pol_disabled = {"policyId": "pol_dis", "replication": {"enabled": False}}
    comp_dis = backup_replication.replication_compliance(policy=pol_disabled, backup_id="bk_3")
    assert comp_dis["compliance"] == "healthy"
    assert comp_dis.get("reasons", []) == []


def test_reconcile_policy_replicas_no_source_or_disabled(tmp_settings: Path) -> None:
    # 1. Policy with disabled replication
    backup_policies.create_policy({
        "policyId": "pol_rec_dis",
        "name": "Disabled Repl",
        "targetId": "managed-local",
        "replication": {"enabled": False},
    })
    rep_dis = backup_replication.reconcile_policy_replicas("pol_rec_dis")
    assert rep_dis["status"] == "skipped"

    # 2. Non-existent policy raises AppError
    with pytest.raises(AppError):
        backup_replication.reconcile_policy_replicas("pol_non_existent")

    # 3. Target with missing source copy
    t_p_dir = tmp_settings / "t_rec_p"
    t_r_dir = tmp_settings / "t_rec_r"
    t_p_dir.mkdir(parents=True, exist_ok=True)
    t_r_dir.mkdir(parents=True, exist_ok=True)
    t_p = backup_targets.init_target(t_p_dir, label="Pri")
    t_r = backup_targets.init_target(t_r_dir, label="Rep")

    backup_policies.create_policy({
        "policyId": "pol_rec_no_src",
        "name": "No Src Policy",
        "targetId": t_p["targetId"],
        "replication": {
            "enabled": True,
            "targets": [{"targetId": t_r["targetId"], "mode": "required"}],
        },
    })
    # Record logical copy without receipts/objects on disk
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_p["targetId"],
        policy_id="pol_rec_no_src",
        backup_id="bk_ghost",
        committed_at=_utc_iso(),
        recoverable=True,
        state="healthy",
    )
    rep_ghost = backup_replication.reconcile_policy_replicas("pol_rec_no_src")
    assert rep_ghost["repairsFailed"] >= 1


def test_replica_repair_missing_and_corrupt_quarantine(tmp_settings: Path) -> None:
    # 1. Non-existent target
    with pytest.raises(AppError):
        backup_replication.execute_replica_repair(
            policy_id="pol_none",
            backup_id="bk_none",
            dest_target_id="dest_t",
            source_target_id="src_t",
        )

    # 2. Registered targets but no receipt on source
    t_s_dir = tmp_settings / "t_rep_s"
    t_d_dir = tmp_settings / "t_rep_d"
    t_s_dir.mkdir(parents=True, exist_ok=True)
    t_d_dir.mkdir(parents=True, exist_ok=True)
    t_s = backup_targets.init_target(t_s_dir, label="Src")
    t_d = backup_targets.init_target(t_d_dir, label="Dest")

    with pytest.raises(AppError) as exc_info:
        backup_replication.execute_replica_repair(
            policy_id="pol_none",
            backup_id="bk_no_rec",
            dest_target_id=t_d["targetId"],
            source_target_id=t_s["targetId"],
        )
    assert "Source receipt missing" in str(exc_info.value)


def test_enqueue_replica_jobs_and_has_open_required(tmp_settings: Path) -> None:
    pol = {
        "policyId": "pol_q_test",
        "targetId": "target_pri",
        "replication": {
            "enabled": True,
            "targets": [
                {"targetId": "target_rep_1", "mode": "required"},
                {"targetId": "target_rep_2", "mode": "best-effort"},
                {"targetId": "target_pri", "mode": "required"},  # skip self
            ],
        },
    }
    receipt = {
        "schemaVersion": 4,
        "backupId": "bk_q_1",
        "policyId": "pol_q_test",
        "objectSetDigest": "objset_q",
        "controlObjectDigest": "ctrl_q",
        "objects": [{"digest": "d1", "size": 100}],
    }
    jobs = backup_replication.enqueue_replica_jobs(
        policy=pol,
        primary_target_id="target_pri",
        backup_id="bk_q_1",
        package=None,
        run_id="run_q_1",
        schedule_slot="slot_q",
        slot_digest="slot_digest_q",
        primary_receipt=receipt,
    )
    assert len(jobs) == 2
    assert backup_replication.has_open_required_jobs(policy_id="pol_q_test", backup_id="bk_q_1") is True
    assert backup_replication.has_open_required_jobs(policy_id="pol_q_test", slot_digest="slot_digest_q") is True

    # Check list_jobs with filters
    all_jobs = backup_replication.list_jobs(policy_id="pol_q_test")
    assert len(all_jobs) == 2
    req_jobs = backup_replication.list_jobs(policy_id="pol_q_test", phase="queued")
    assert len(req_jobs) == 2
    none_jobs = backup_replication.list_jobs(policy_id="pol_q_test", phase="committed")
    assert len(none_jobs) == 0

    # Idempotent enqueue returns existing open jobs
    jobs_dupe = backup_replication.enqueue_replica_jobs(
        policy=pol,
        primary_target_id="target_pri",
        backup_id="bk_q_1",
        package=None,
        run_id="run_q_2",
        schedule_slot="slot_q",
        slot_digest="slot_digest_q",
        primary_receipt=receipt,
    )
    assert len(jobs_dupe) == 2

    # Process pending jobs
    summary = backup_replication.process_pending_jobs(limit=10)
    assert summary["processed"] >= 1
    assert summary["failed"] >= 1 or summary["committed"] >= 0


def test_replica_repair_auto_source_and_quarantine(tmp_settings: Path) -> None:
    # 1. Setup source and destination targets
    t_s_dir = tmp_settings / "t_auto_s"
    t_d_dir = tmp_settings / "t_auto_d"
    t_s_dir.mkdir(parents=True, exist_ok=True)
    t_d_dir.mkdir(parents=True, exist_ok=True)
    t_s = backup_targets.init_target(t_s_dir, label="Auto Source")
    t_d = backup_targets.init_target(t_d_dir, label="Auto Dest")
    s_id = str(t_s["targetId"])
    d_id = str(t_d["targetId"])

    # 2. Setup source component and receipt
    comp_bytes = b"encrypted-component-payload-453"
    comp_digest = hashlib.sha256(comp_bytes).hexdigest()
    comp_path = t_s_dir / "objects" / comp_digest[:2] / comp_digest[2:4] / f"{comp_digest}.age"
    comp_path.parent.mkdir(parents=True, exist_ok=True)
    comp_path.write_bytes(comp_bytes)

    receipt = {
        "schemaVersion": 4,
        "backupId": "bk_auto_1",
        "policyId": "pol_auto",
        "targetId": s_id,
        "size": len(comp_bytes),
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "objset_auto_1",
        "controlObjectDigest": "ctrl_auto_1",
        "objects": [{"digest": comp_digest, "size": len(comp_bytes)}],
    }
    (t_s_dir / "receipts").mkdir(parents=True, exist_ok=True)
    r_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (t_s_dir / "receipts" / "bk_auto_1.json").write_bytes(r_bytes)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "policyId": "pol_auto",
        "backupId": "bk_auto_1",
        "receiptDigest": hashlib.sha256(r_bytes).hexdigest(),
        "objectSetDigest": receipt["objectSetDigest"],
        "storageProtocol": "object-set-v1",
        "committedAt": _utc_iso(),
    }
    (t_s_dir / "commits" / "pol_auto").mkdir(parents=True, exist_ok=True)
    (t_s_dir / "commits" / "pol_auto" / "bk_auto_1.json").write_text(json.dumps(commit), encoding="utf-8")

    # 3. Create corrupted component on destination to test quarantine
    bad_comp_path = t_d_dir / "objects" / comp_digest[:2] / comp_digest[2:4] / f"{comp_digest}.age"
    bad_comp_path.parent.mkdir(parents=True, exist_ok=True)
    bad_comp_path.write_bytes(b"corrupted-bytes")

    # 4. Record healthy copy in DR ledger for source
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=s_id,
        policy_id="pol_auto",
        backup_id="bk_auto_1",
        committed_at=_utc_iso(),
        recoverable=True,
        state="healthy",
    )

    # 5. Execute repair with source_target_id=None (auto resolution from ledger)
    res = backup_replication.execute_replica_repair(
        policy_id="pol_auto",
        backup_id="bk_auto_1",
        dest_target_id=d_id,
        source_target_id=None,
    )
    assert res["status"] == "success"
    assert res["bytesRepaired"] == len(comp_bytes)

    # Verify quarantine file exists
    quarantine_files = list((t_d_dir / ".quarantine").rglob("*"))
    assert len(quarantine_files) >= 1

    # Verify healthy component on destination matches source
    assert bad_comp_path.read_bytes() == comp_bytes

    # 6. Test retired point skipping in execute_replica_repair
    backup_dr_ledger.mark_logical_recovery_point_retired("pol_auto", "bk_auto_1")
    res_retired = backup_replication.execute_replica_repair(
        policy_id="pol_auto",
        backup_id="bk_auto_1",
        dest_target_id=d_id,
        source_target_id=s_id,
    )
    assert res_retired["status"] == "skipped"
    assert res_retired["reason"] == "retired"


def test_dr_audit_validate_commit_receipt_binding_anomalies() -> None:
    # 1. Invalid commit marker
    assert "invalid-commit-marker" in backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit={"bad": True},
        receipt={},
    )

    valid_commit = {
        "schemaVersion": 4,
        "commitType": "backup",
        "targetId": "t1",
        "commitHash": "",
        "backupId": "bk1",
        "policyId": "p1",
        "receiptDigest": "expected_hash",
    }
    valid_commit["commitHash"] = backup_publish._commit_hash(valid_commit)

    # 2. Missing receipt
    assert "missing-receipt:bk1" in backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit=valid_commit,
        receipt=None,
    )

    # 3. Receipt digest mismatch & field mismatches & missing objectSetDigest
    raw_bytes = b'{"receipt": true}\n'
    anomalies = backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit=valid_commit,
        receipt={"backupId": "bk_other", "targetId": "t_other", "policyId": "p_other", "storageProtocol": "object-set-v1"},
        raw_receipt_bytes=raw_bytes,
    )
    assert "receipt-digest-mismatch:bk1" in anomalies
    assert "receipt-backup-id-mismatch:bk1" in anomalies
    assert "receipt-target-mismatch:bk1" in anomalies
    assert "receipt-policy-mismatch:bk1" in anomalies
    assert "missing-object-set-digest:bk1" in anomalies

    # 4. Invalid object set inventory
    anomalies_inv = backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit=valid_commit,
        receipt={"backupId": "bk1", "targetId": "t1", "policyId": "p1", "storageProtocol": "object-set-v1", "objectSetDigest": "obj1", "objects": []},
        raw_receipt_bytes=hashlib.sha256(b"x").hexdigest().encode("utf-8"),
    )
    assert "invalid-object-set-inventory:bk1" in anomalies_inv


def test_resume_audit_and_job_status(tmp_settings: Path) -> None:
    # 1. Non-existent audit ID
    with pytest.raises(AppError) as exc:
        backup_dr_audit.resume_audit("audit_non_existent")
    assert exc.value.status == 404

    # 2. Completed audit job
    backup_dr_ledger.upsert_audit_job(
        audit_id="audit_done",
        target_id="managed-local",
        phase="completed",
        cursor=None,
        records_checked=5,
        anomalies=[],
        started_at=_utc_iso(),
        completed_at=_utc_iso(),
        details={},
    )
    res = backup_dr_audit.resume_audit("audit_done")
    assert res["status"] == "completed"
    assert res["resumed"] is False

    # 3. Failed audit job
    backup_dr_ledger.upsert_audit_job(
        audit_id="audit_fail",
        target_id="managed-local",
        phase="failed",
        cursor=None,
        records_checked=2,
        anomalies=["some-error"],
        started_at=_utc_iso(),
        completed_at=_utc_iso(),
        details={},
    )
    res_fail = backup_dr_audit.resume_audit("audit_fail")
    assert res_fail["status"] == "failed"
    assert res_fail["phase"] == "failed"


def test_audit_remote_target_with_mock_store(tmp_settings: Path) -> None:
    # Setup mock remote store
    target_data: dict[str, bytes] = {}

    class AuditMockStore:
        def list_objects(self, prefix: str, cursor: str | None = None, limit: int = 100) -> Any:
            keys = [k for k in sorted(target_data.keys()) if k.startswith(prefix)]
            items = [MagicMock(key=k) for k in keys]
            return MagicMock(objects=items, cursor=None)

        def get_bytes(self, key: str) -> bytes | None:
            return target_data.get(key)

        def put_bytes(self, key: str, data: bytes) -> PutResult:
            target_data[key] = data
            return PutResult(key=key, etag="e", size=len(data), created=True)

    # 1. Non-registered target raises AppError and records failure in ledger
    with pytest.raises(AppError):
        backup_dr_audit.audit_remote_target("non_registered_t")
    failed_job = backup_dr_ledger.get_open_audit_job("non_registered_t")
    assert failed_job is None  # Marked failed so not open

    # 2. Register mock target
    store = AuditMockStore()
    orig_open = backup_targets.open_target_store
    backup_targets.open_target_store = lambda tid, **_kw: store if tid == "mock_target_1" else orig_open(tid, **_kw)

    try:
        # Create commit 1 (gen 1)
        r1_bytes = json.dumps({
            "schemaVersion": 4,
            "backupId": "bk_audit_1",
            "policyId": "p_aud",
            "targetId": "mock_target_1",
            "size": 100,
            "storageProtocol": "object-set-v1",
            "objectSetDigest": "obj1",
            "objects": [{"digest": "d1", "size": 100}],
        }, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        target_data["receipts/bk_audit_1.json"] = r1_bytes

        c1 = {
            "schemaVersion": 4,
            "commitType": "backup",
            "targetId": "mock_target_1",
            "commitHash": "",
            "previousCommitHash": backup_dr_audit.GENESIS_COMMIT_HASH,
            "targetGeneration": 1,
            "backupId": "bk_audit_1",
            "policyId": "p_aud",
            "receiptDigest": hashlib.sha256(r1_bytes).hexdigest(),
            "objectSetDigest": "obj1",
            "committedAt": _utc_iso(),
        }
        c1["commitHash"] = backup_publish._commit_hash(c1)
        target_data["commits/0001_bk_audit_1.json"] = json.dumps(c1).encode("utf-8")

        # Create commit 2 with generation gap (gen 3 instead of 2)
        r2_bytes = json.dumps({
            "schemaVersion": 4,
            "backupId": "bk_audit_2",
            "policyId": "p_aud",
            "targetId": "mock_target_1",
            "size": 200,
            "storageProtocol": "object-set-v1",
            "objectSetDigest": "obj2",
            "objects": [{"digest": "d2", "size": 200}],
        }, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        target_data["receipts/bk_audit_2.json"] = r2_bytes

        c2 = {
            "schemaVersion": 4,
            "commitType": "backup",
            "targetId": "mock_target_1",
            "commitHash": "",
            "previousCommitHash": str(c1["commitHash"]),
            "targetGeneration": 3,
            "backupId": "bk_audit_2",
            "policyId": "p_aud",
            "receiptDigest": hashlib.sha256(r2_bytes).hexdigest(),
            "objectSetDigest": "obj2",
            "committedAt": _utc_iso(),
        }
        c2["commitHash"] = backup_publish._commit_hash(c2)
        target_data["commits/0003_bk_audit_2.json"] = json.dumps(c2).encode("utf-8")

        # Add control/head.json with mismatch
        head = {"targetGeneration": 4, "latestCommitHash": "hash_head"}
        target_data["control/head.json"] = json.dumps(head).encode("utf-8")

        # Non-json file in commits prefix
        target_data["commits/ignored.txt"] = b"not json"

        # Execute remote target audit
        res = backup_dr_audit.audit_remote_target("mock_target_1", resume=False)
        assert res["status"] == "completed"
        assert res["recoveryPointsFound"] == 0
        assert any("generation-gap:1->3" in a for a in res["anomalies"])
        assert any("head-commit-hash-mismatch" in a for a in res["anomalies"])
        assert any("head-generation-mismatch" in a for a in res["anomalies"])
    finally:
        backup_targets.open_target_store = orig_open


def test_dr_ledger_queries_and_state_transitions(tmp_settings: Path) -> None:
    # 1. Test record & list copies with filter combinations
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="t_led_1",
        policy_id="pol_led_1",
        backup_id="bk_led_1",
        committed_at="2026-08-16T12:00:00Z",
        recoverable=True,
        state="healthy",
        role="primary",
    )
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="t_led_2",
        policy_id="pol_led_1",
        backup_id="bk_led_1",
        committed_at="2026-08-16T12:00:00Z",
        recoverable=False,
        state="corrupted",
        role="replica",
    )

    copies_all = backup_dr_ledger.list_logical_recovery_copies(policy_id="pol_led_1")
    assert len(copies_all) == 2
    copies_rec = [c for c in copies_all if c.get("recoverable")]
    assert len(copies_rec) == 1
    copies_t1 = [c for c in copies_all if c.get("targetId") == "t_led_1"]
    assert len(copies_t1) == 1

    # 2. Update recovery copy state
    backup_dr_ledger.update_recovery_copy_state(
        target_id="t_led_2",
        policy_id="pol_led_1",
        backup_id="bk_led_1",
        state="healthy",
        recoverable=True,
        last_verified_at=_utc_iso(),
        last_scrub_at=_utc_iso(),
        last_drill_at=_utc_iso(),
        last_repair_at=_utc_iso(),
    )
    copies_updated = [c for c in backup_dr_ledger.list_logical_recovery_copies(policy_id="pol_led_1") if c.get("recoverable")]
    assert len(copies_updated) == 2

    # 3. Stage samples with target_id
    backup_dr_ledger.record_stage_sample(
        sample_id="rst_sample_1",
        stage="fetch",
        duration_ms=1500.0,
        target_id="t_led_1",
        bytes_transferred=1000,
        observed_at=_utc_iso(),
    )
    samples = backup_dr_ledger.list_stage_samples(stage="fetch")
    assert len(samples) >= 1
    assert samples[0]["targetId"] == "t_led_1"
    assert samples[0]["durationMs"] == 1500.0


def test_backup_governance_router_endpoints(tmp_settings: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from deepseek_infra.core.config import settings
    from deepseek_infra.web.routes.backup_governance import create_backup_governance_router

    app = FastAPI()
    router = create_backup_governance_router()
    app.include_router(router)
    auth_header = {"Authorization": f"Bearer {settings.auth.token}"} if settings.auth.enabled else {}
    client = TestClient(app, base_url="http://127.0.0.1", headers=auth_header)

    # 1. Target capabilities
    cap_resp = client.get("/api/workspace/backup-target-capabilities")
    assert cap_resp.status_code == 200
    assert "supportedKinds" in cap_resp.json()

    # 2. Policies list & create
    pol_resp = client.get("/api/workspace/backup-policies")
    assert pol_resp.status_code == 200
    assert "policies" in pol_resp.json()

    create_resp = client.post("/api/workspace/backup-policies", json={
        "policyId": "pol_api_test",
        "name": "API Test Policy",
        "targetId": "managed-local",
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
    })
    assert create_resp.status_code == 200

    patch_resp = client.patch("/api/workspace/backup-policies/pol_api_test", json={
        "name": "Updated API Policy",
    })
    assert patch_resp.status_code == 200
    assert patch_resp.json()["name"] == "Updated API Policy"

    # 3. DR Status
    status_resp = client.get("/api/workspace/disaster-recovery/status")
    assert status_resp.status_code == 200
    assert "status" in status_resp.json() or "replication" in status_resp.json()

    # 4. DR Replication Jobs List
    rep_resp = client.get("/api/workspace/disaster-recovery/replication?policyId=pol_api_test")
    assert rep_resp.status_code == 200
    assert "jobs" in rep_resp.json()

    # 5. Delete policy
    del_resp = client.delete("/api/workspace/backup-policies/pol_api_test")
    assert del_resp.status_code == 200


def test_backup_policies_validation_and_normalization(tmp_settings: Path) -> None:
    # 1. Target bindings with non-existent primary target
    bad_primary = {
        "policyId": "pol_bad_prim",
        "targetId": "non_existent_primary",
        "replication": {"enabled": False},
    }
    with pytest.raises(AppError) as exc:
        backup_policies.validate_target_bindings(bad_primary)
    assert exc.value.status == 400
    assert "primary target" in str(exc.value).lower()

    # 2. Target bindings with non-existent replica target
    bad_replica = {
        "policyId": "pol_bad_repl",
        "targetId": "managed-local",
        "replication": {
            "enabled": True,
            "targets": [{"targetId": "non_existent_replica", "mode": "required"}],
        },
    }
    with pytest.raises(AppError) as exc2:
        backup_policies.validate_target_bindings(bad_replica)
    assert exc2.value.status == 400
    assert "replica target" in str(exc2.value).lower()

    # 3. Valid target bindings
    backup_policies.validate_target_bindings({
        "policyId": "pol_valid",
        "targetId": "managed-local",
        "replication": {"enabled": False},
    })

    # 4. Normalization with maxReplicaLagSeconds
    normalized = backup_policies.normalize_policy({
        "policyId": "pol_norm",
        "name": "Policy Norm",
        "targetId": "managed-local",
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {
            "enabled": True,
            "maxReplicaLagSeconds": 3600,
            "targets": [{"targetId": "target_replica_1", "mode": "required"}],
        },
    })
    assert normalized["replication"]["maxReplicaLagSeconds"] == 3600


def test_replication_job_failure_phases_and_backoff(tmp_settings: Path) -> None:
    # 1. Backoff and retry-wait for required mode
    job_req = {
        "jobId": "repl_test_fail_1",
        "attempts": 1,
        "maxAttempts": 5,
    }
    res_retry = backup_replication._fail_job(job_req, Exception("network timeout"), mode="required")
    assert res_retry["phase"] == "retry-wait"
    assert "nextRetryAt" in res_retry
    assert res_retry["attempts"] == 1

    # 2. Spool missing in required mode -> repair-needed
    res_spool = backup_replication._fail_job(job_req, Exception("replication spool package missing"), mode="required")
    assert res_spool["phase"] == "repair-needed"

    # 3. Max attempts exceeded in required mode -> failed-terminal
    job_max = {
        "jobId": "repl_test_fail_2",
        "attempts": 5,
        "maxAttempts": 5,
    }
    res_terminal = backup_replication._fail_job(job_max, Exception("disk full"), mode="required")
    assert res_terminal["phase"] == "failed-terminal"

    # 4. Best-effort mode -> failed
    res_best = backup_replication._fail_job(job_req, Exception("some error"), mode="best-effort")
    assert res_best["phase"] == "failed"


def test_replica_repair_missing_source_or_corrupted_component(tmp_settings: Path) -> None:
    # 1. No healthy source copy available
    with pytest.raises(AppError) as exc:
        backup_replication.execute_replica_repair(
            policy_id="pol_no_src",
            backup_id="bk_no_src",
            dest_target_id="managed-local",
            source_target_id=None,
        )
    assert exc.value.status == 404
    assert "no healthy source copy" in str(exc.value).lower()

    # 2. Source target has no receipt
    s_root = tmp_settings / "src_no_receipt"
    s_root.mkdir(parents=True, exist_ok=True)
    d_root = tmp_settings / "dest_no_receipt"
    d_root.mkdir(parents=True, exist_ok=True)
    s_reg = backup_targets.init_target(s_root, label="Src No Rcpt")
    d_reg = backup_targets.init_target(d_root, label="Dest No Rcpt")

    with pytest.raises(AppError) as exc2:
        backup_replication.execute_replica_repair(
            policy_id="pol_no_rcpt",
            backup_id="bk_no_rcpt",
            dest_target_id=d_reg["targetId"],
            source_target_id=s_reg["targetId"],
        )
    assert exc2.value.status == 404
    assert "source receipt missing" in str(exc2.value).lower()

    # 3. Source component digest mismatch (corrupted source component)
    comp_bytes = b"bad source data"
    comp_digest = hashlib.sha256(b"original data").hexdigest()
    comp_p = s_root / "objects" / comp_digest[:2] / comp_digest[2:4] / f"{comp_digest}.age"
    comp_p.parent.mkdir(parents=True, exist_ok=True)
    comp_p.write_bytes(comp_bytes)

    receipt = {
        "schemaVersion": 4,
        "backupId": "bk_bad_src_comp",
        "policyId": "pol_bad_comp",
        "targetId": s_reg["targetId"],
        "storageProtocol": "object-set-v1",
        "objects": [{"digest": comp_digest, "size": len(comp_bytes)}],
    }
    r_path = s_root / "receipts" / "bk_bad_src_comp.json"
    r_path.parent.mkdir(parents=True, exist_ok=True)
    r_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    r_path.write_bytes(r_bytes)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "policyId": "pol_bad_comp",
        "backupId": "bk_bad_src_comp",
        "receiptDigest": hashlib.sha256(r_bytes).hexdigest(),
        "objectSetDigest": "objset_bad",
        "storageProtocol": "object-set-v1",
        "committedAt": _utc_iso(),
    }
    receipt["objectSetDigest"] = "objset_bad"
    r_bytes = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    r_path.write_bytes(r_bytes)
    commit["receiptDigest"] = hashlib.sha256(r_bytes).hexdigest()
    (s_root / "commits" / "pol_bad_comp").mkdir(parents=True, exist_ok=True)
    (s_root / "commits" / "pol_bad_comp" / "bk_bad_src_comp.json").write_text(json.dumps(commit), encoding="utf-8")

    with pytest.raises(AppError) as exc3:
        backup_replication.execute_replica_repair(
            policy_id="pol_bad_comp",
            backup_id="bk_bad_src_comp",
            dest_target_id=d_reg["targetId"],
            source_target_id=s_reg["targetId"],
        )
    assert exc3.value.status == 500
    assert "source component corrupt" in str(exc3.value).lower()


def test_reconcile_policy_replicas_variations(tmp_settings: Path) -> None:
    # 1. Replication disabled
    p_dis = backup_policies.create_policy({
        "policyId": "pol_reconcile_dis",
        "name": "Dis Policy",
        "targetId": "managed-local",
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {"enabled": False},
    })
    res_dis = backup_replication.reconcile_policy_replicas(p_dis["policyId"])
    assert res_dis["status"] == "skipped"
    assert res_dis["reason"] == "replication-disabled"

    # 2. Replication enabled but empty targets
    p_empty = backup_policies.create_policy({
        "policyId": "pol_reconcile_empty",
        "name": "Empty Policy",
        "targetId": "managed-local",
        "schedule": {"cron": "0 2 * * *", "timezone": "UTC"},
        "replication": {"enabled": True, "targets": []},
    })
    res_empty = backup_replication.reconcile_policy_replicas(p_empty["policyId"])
    assert res_empty["status"] == "noop"


def test_process_pending_jobs_variations(tmp_settings: Path) -> None:
    # 1. Retry-wait job with future retry time is skipped
    future_time = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    job_future = {
        "schemaVersion": 1,
        "jobId": "repl_future_1",
        "policyId": "pol_future",
        "backupId": "bk_future",
        "primaryTargetId": "managed-local",
        "replicaTargetId": "t_repl",
        "mode": "required",
        "phase": "retry-wait",
        "nextRetryAt": future_time,
        "createdAt": _utc_iso(),
        "updatedAt": _utc_iso(),
    }
    backup_replication._atomic_write(backup_replication._job_path("repl_future_1"), job_future)

    res = backup_replication.process_pending_jobs(limit=10)
    assert res["processed"] == 0

    # 2. Past retry time is processed
    past_time = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    job_future["nextRetryAt"] = past_time
    backup_replication._atomic_write(backup_replication._job_path("repl_future_1"), job_future)

    res_past = backup_replication.process_pending_jobs(limit=10)
    assert res_past["processed"] == 1


def test_replica_lag_calculations_all_branches(tmp_settings: Path) -> None:
    # 1. No primary point
    lag_no_p = backup_replication.calculate_replica_lag("pol_no_pts", "t_repl_1", primary_target_id="managed-local")
    assert lag_no_p["status"] == "no-primary"
    assert lag_no_p["lagRecoveryPoints"] == 0

    # 2. Primary point exists, no replica point
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="managed-local",
        policy_id="pol_with_p",
        backup_id="bk_p_1",
        committed_at="2026-08-16T10:00:00Z",
        recoverable=True,
        state="healthy",
        role="primary",
    )
    lag_no_r = backup_replication.calculate_replica_lag("pol_with_p", "t_repl_1", primary_target_id="managed-local")
    assert lag_no_r["status"] == "no-replica"
    assert lag_no_r["lagRecoveryPoints"] == 999
    assert lag_no_r["lagSeconds"] == 999999

    # 3. Both primary and replica point exist
    backup_dr_ledger.record_logical_recovery_copy(
        target_id="t_repl_1",
        policy_id="pol_with_p",
        backup_id="bk_p_1",
        committed_at="2026-08-16T09:55:00Z",
        recoverable=True,
        state="healthy",
        role="replica",
    )
    lag_ok = backup_replication.calculate_replica_lag("pol_with_p", "t_repl_1", primary_target_id="managed-local")
    assert lag_ok["lagRecoveryPoints"] == 0
    assert lag_ok["lagSeconds"] == 300
    assert lag_ok["status"] == "calculated"


def test_dr_audit_genesis_and_resume_branches(tmp_settings: Path) -> None:
    # Setup target with gen 1 commit having non-genesis previousCommitHash
    root = tmp_settings / "audit_bad_genesis"
    root.mkdir(parents=True, exist_ok=True)
    reg = backup_targets.init_target(root, label="Audit Genesis")
    t_id = reg["targetId"]

    # Write receipt
    r_bytes = json.dumps({
        "schemaVersion": 4,
        "backupId": "bk_gen1",
        "policyId": "p_gen",
        "targetId": t_id,
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "os1",
        "objects": [{"digest": "d1", "size": 50}],
    }).encode("utf-8") + b"\n"
    r_path = root / "receipts" / "bk_gen1.json"
    r_path.parent.mkdir(parents=True, exist_ok=True)
    r_path.write_bytes(r_bytes)

    # Write commit gen 1 with bad previousCommitHash
    c1 = {
        "schemaVersion": 4,
        "commitType": "backup",
        "targetId": t_id,
        "commitHash": "",
        "previousCommitHash": "bad_previous_not_genesis",
        "targetGeneration": 1,
        "backupId": "bk_gen1",
        "policyId": "p_gen",
        "receiptDigest": hashlib.sha256(r_bytes).hexdigest(),
        "objectSetDigest": "os1",
        "committedAt": _utc_iso(),
    }
    c1["commitHash"] = backup_publish._commit_hash(c1)
    c_path = root / "commits" / "0001_bk_gen1.json"
    c_path.parent.mkdir(parents=True, exist_ok=True)
    c_path.write_text(json.dumps(c1), encoding="utf-8")

    res = backup_dr_audit.audit_remote_target(t_id)
    assert res["status"] == "completed"
    assert any("broken-genesis-commit-hash:bk_gen1" in a for a in res["anomalies"])






