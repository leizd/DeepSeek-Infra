"""Coverage tests for Replication, Repair, and Rebalance Pipelines (v4.5)."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_retention,
    backup_targets,
    backup_writer_lease,
)


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def test_replication_lag_and_compliance(tmp_settings: Path) -> None:
    p_tid = "target_lag_p"
    r_tid = "target_lag_r"
    backup_targets.register_filesystem_target(p_tid, path=tmp_settings / "lag_p")
    backup_targets.register_filesystem_target(r_tid, path=tmp_settings / "lag_r")

    policy_id = "pol-lag"
    # 1. Lag with no copies
    lag_no_p = backup_replication.calculate_replica_lag(policy_id, r_tid, primary_target_id=p_tid)
    assert lag_no_p["status"] == "no-primary"

    # Record primary copy
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=p_tid,
        policy_id=policy_id,
        backup_id="bk-lag-1",
        committed_at="2026-08-18T10:00:00Z",
        state="healthy",
        recoverable=True,
    )

    lag_no_r = backup_replication.calculate_replica_lag(policy_id, r_tid, primary_target_id=p_tid)
    assert lag_no_r["status"] == "no-replica"

    # Record replica copy with lag
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=r_tid,
        policy_id=policy_id,
        backup_id="bk-lag-0",
        committed_at="2026-08-18T09:00:00Z",
        state="healthy",
        recoverable=True,
    )
    lag_calc = backup_replication.calculate_replica_lag(policy_id, r_tid, primary_target_id=p_tid)
    assert lag_calc["status"] == "calculated"
    assert lag_calc["lagSeconds"] == 3600

    # 2. Replication compliance
    policy_disabled = {"policyId": policy_id, "replication": {"enabled": False}}
    comp_dis = backup_replication.replication_compliance(policy=policy_disabled, backup_id="bk-lag-1")
    assert comp_dis["enabled"] is False

    policy_lag_limit = {
        "policyId": policy_id,
        "targetId": p_tid,
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "maxReplicaLagSeconds": 60,
            "targets": [{"targetId": r_tid, "mode": "required"}],
        },
    }
    comp_lag = backup_replication.replication_compliance(policy=policy_lag_limit, backup_id="bk-lag-1")
    assert comp_lag["compliance"] == "degraded"
    assert "replica-lag-exceeded:target_lag_r" in comp_lag["reasons"]


def test_replication_deep_gap_and_quarantine_coverage(tmp_settings: Path) -> None:
    t_src = "target_rep_deep_src"
    t_dst = "target_rep_deep_dst"
    backup_targets.register_filesystem_target(t_src, path=tmp_settings / "rdeep_src")
    backup_targets.register_filesystem_target(t_dst, path=tmp_settings / "rdeep_dst")

    policy_id = "pol-rep-deep"
    b_id = "bk-rep-deep-1"

    # 1. replication_compliance with open and failed required jobs
    policy_comp = {
        "policyId": policy_id,
        "targetId": t_src,
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "targets": [{"targetId": t_dst, "mode": "required"}],
        },
    }

    # Record open required job
    job_id = "repl_deep_1"
    job_record = {
        "jobId": job_id,
        "policyId": policy_id,
        "backupId": b_id,
        "destTargetId": t_dst,
        "sourceTargetId": t_src,
        "mode": "required",
        "phase": "queued",
        "createdAt": "2026-08-18T10:00:00Z",
    }
    backup_replication._atomic_write(backup_replication._job_path(job_id), job_record)

    comp_open = backup_replication.replication_compliance(policy=policy_comp, backup_id=b_id)
    assert comp_open["compliance"] == "degraded"
    assert "open-required-jobs" in comp_open["reasons"]

    # Update job to failed
    job_record["phase"] = "failed"
    backup_replication._atomic_write(backup_replication._job_path(job_id), job_record)

    comp_failed = backup_replication.replication_compliance(policy=policy_comp, backup_id=b_id)
    assert comp_failed["compliance"] == "degraded"
    assert "failed-required-jobs" in comp_failed["reasons"]

    # 2. rebalance_policy_replicas with failure domain rebalancing
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_src,
        policy_id=policy_id,
        backup_id=b_id,
        committed_at="2026-08-18T10:00:00Z",
        state="healthy",
        recoverable=True,
    )

    policy_rebalance = {
        "name": "Policy Rebalance Deep",
        "policyId": policy_id,
        "targetId": t_src,
        "maintenanceWindow": {"enabled": False},
        "placement": {"failureDomainSpread": "max-diversity", "minFailureDomains": 2},
        "replication": {
            "enabled": True,
            "minCommittedCopies": 2,
            "minFailureDomains": 2,
            "targets": [{"targetId": t_dst, "mode": "required"}],
            "maxReplicaLagSeconds": 10,
        },
    }
    backup_policies.create_policy(policy_rebalance)

    # Real rebalance with mock execution
    with patch.object(backup_replication, "execute_rebalance_job", return_value={"phase": "completed"}):
        real_res = backup_replication.rebalance_policy_replicas(policy_id)
        assert real_res["status"] in {"completed", "skipped", "ok"}

    # Compliance with lag exceeded
    with patch.object(backup_replication, "calculate_replica_lag", return_value={"lagSeconds": 100}):
        comp_lag = backup_replication.replication_compliance(policy=policy_rebalance, backup_id=b_id)
        assert comp_lag["compliance"] == "degraded"
        assert any("replica-lag-exceeded" in r for r in comp_lag["reasons"])


def test_stream_ciphertext_transfer_and_authentication(tmp_settings: Path) -> None:
    t_src = "target_str_src"
    t_dst = "target_str_dst"
    src_root = tmp_settings / "str_src"
    dst_root = tmp_settings / "str_dst"
    backup_targets.register_filesystem_target(t_src, path=src_root)
    backup_targets.register_filesystem_target(t_dst, path=dst_root)

    src_target = backup_publish.resolve_target(t_src)
    dst_target = backup_publish.resolve_target(t_dst)

    # 1. Setup source component
    payload = b"test-stream-ciphertext-payload-123456"
    digest = hashlib.sha256(payload).hexdigest()
    comp_rel = f"ciphertext/sha256/{digest}"
    (src_root / "ciphertext" / "sha256").mkdir(parents=True, exist_ok=True)
    (src_root / comp_rel).write_bytes(payload)

    # 2. Stream to filesystem destination
    n_bytes = backup_replication.stream_ciphertext_transfer(
        src_target,
        dst_target,
        comp_rel,
        comp_rel,
        digest,
        chunk_size=8,
    )
    assert n_bytes == len(payload)
    assert (dst_root / comp_rel).is_file()

    # 3. Stream with digest mismatch raises AppError
    with pytest.raises(AppError):
        backup_replication.stream_ciphertext_transfer(
            src_target,
            dst_target,
            comp_rel,
            "ciphertext/sha256/mismatch",
            "0000000000000000000000000000000000000000000000000000000000000000",
            chunk_size=8,
        )

    # 4. Stream to mock remote store (single chunk & multi-part & resume)
    mock_store = MagicMock()
    mock_store.begin_multipart.return_value = MagicMock(upload_id="mp-1")
    mock_store.upload_part.return_value = MagicMock(etag="etag-1")
    dst_remote = SimpleNamespace(root=None, store=mock_store)

    # Multi-chunk streaming
    prog: dict[str, Any] = {}
    n_remote = backup_replication.stream_ciphertext_transfer(
        src_target,
        dst_remote,
        comp_rel,
        comp_rel,
        digest,
        chunk_size=4,
        progress_state=prog,
    )
    assert n_remote == len(payload)
    assert mock_store.begin_multipart.called
    assert mock_store.complete_multipart_if_absent.called

    # Resuming multi-part streaming
    mock_store.reset_mock()
    prog_resume = {
        "multipartUploadId": "mp-1",
        "nextOffset": 8,
        "parts": [{"number": 1, "etag": "etag-1"}, {"number": 2, "etag": "etag-2"}],
    }
    n_resumed = backup_replication.stream_ciphertext_transfer(
        src_target,
        dst_remote,
        comp_rel,
        comp_rel,
        digest,
        chunk_size=4,
        progress_state=prog_resume,
    )
    assert n_resumed == len(payload)
    assert mock_store.complete_multipart_if_absent.called

    # 5. _verify_destination_component
    valid, corrupt = backup_replication._verify_destination_component(dst_target, comp_rel, digest)
    assert valid is True
    assert corrupt is False

    valid_bad, corrupt_bad = backup_replication._verify_destination_component(
        dst_target, comp_rel, "bad_digest_hex"
    )
    assert valid_bad is False
    assert corrupt_bad is True

    valid_miss, corrupt_miss = backup_replication._verify_destination_component(
        dst_target, "nonexistent_comp", digest
    )
    assert valid_miss is False
    assert corrupt_miss is False

    # 6. authenticate_committed_copy error paths
    policy_id = "pol-auth-test"
    backup_id = "bk-auth-test"
    (dst_root / "receipts").mkdir(parents=True, exist_ok=True)
    (dst_root / "commits" / policy_id).mkdir(parents=True, exist_ok=True)

    r_bytes = json.dumps({"policyId": policy_id, "backupId": backup_id, "objectSetDigest": "osd1"}).encode()
    r_digest = hashlib.sha256(r_bytes).hexdigest()
    c_bytes = json.dumps({
        "schemaVersion": 4,
        "policyId": policy_id,
        "backupId": backup_id,
        "receiptDigest": r_digest,
        "objectSetDigest": "osd1",
        "commitHash": "fake_hash",
    }).encode()

    (dst_root / "receipts" / f"{backup_id}.json").write_bytes(r_bytes)
    (dst_root / "commits" / policy_id / f"{backup_id}.json").write_bytes(c_bytes)

    # Authenticate fails due to invalid commit hash
    status_corrupt, _, _ = backup_replication.authenticate_committed_copy(dst_target, policy_id, backup_id)
    assert status_corrupt == "corrupt"

    # authenticate_transition_parent
    auth_par, reason = backup_replication.authenticate_transition_parent(
        dst_target,
        policy_id,
        expected_parent_backup_id=backup_id,
        expected_receipt_digest="wrong_digest",
    )
    assert auth_par is False

    # 7. process_pending_jobs
    q_res = backup_replication.process_pending_jobs(limit=10)
    assert "processed" in q_res


def test_execute_replication_job_full_flow(tmp_settings: Path) -> None:
    t_repl = "target_exec_repl_1"
    t_root = tmp_settings / "exec_repl_1"
    backup_targets.register_filesystem_target(t_repl, path=t_root)

    # 1. Missing job raises 404
    with pytest.raises(AppError):
        backup_replication.execute_replication_job("nonexistent_job_xyz")

    # 2. Terminal job returns immediately
    job_term = {
        "jobId": "repl_term_1",
        "phase": "committed",
        "replicaTargetId": t_repl,
    }
    backup_replication._atomic_write(backup_replication._job_path("repl_term_1"), job_term)
    res_term = backup_replication.execute_replication_job("repl_term_1")
    assert res_term["phase"] == "committed"

    # 3. Resolve target error fails job
    job_bad_target = {
        "jobId": "repl_bad_t_1",
        "phase": "queued",
        "replicaTargetId": "target_nonexistent_xyz",
        "mode": "required",
    }
    backup_replication._atomic_write(backup_replication._job_path("repl_bad_t_1"), job_bad_target)
    res_bad = backup_replication.execute_replication_job("repl_bad_t_1")
    assert res_bad["phase"] in {"failed", "failed-terminal", "retry-wait"}

    # 4. Successful replication job execution
    job_success = {
        "jobId": "repl_succ_1",
        "policyId": "pol-succ",
        "backupId": "bk-succ-1",
        "replicaTargetId": t_repl,
        "mode": "required",
        "phase": "queued",
        "spoolKey": "spool_succ_1",
    }
    backup_replication._atomic_write(backup_replication._job_path("repl_succ_1"), job_success)

    mock_pub = MagicMock()
    mock_pub.receipt = {"objectSetDigest": "osd_succ", "createdAt": "2026-08-18T10:00:00Z", "snapshotKind": "full"}
    mock_pub.commit = {"committedAt": "2026-08-18T10:00:00Z"}
    mock_pub.converged = True

    with patch.object(backup_replication, "_load_package_from_spool", return_value={"package": "mock"}):
        with patch.object(backup_publish, "publish_backup", return_value=mock_pub):
            res_succ = backup_replication.execute_replication_job("repl_succ_1")
            assert res_succ["phase"] == "committed"

    # 5. Repair job with missing source receipt
    r_job = backup_replication.create_repair_job(
        policy_id="pol-repair-src",
        backup_id="bk-repair-src",
        dest_target_id=t_repl,
        source_target_id=t_repl,
    )
    with pytest.raises(AppError):
        backup_replication.execute_repair_job_instance(r_job["repairId"])


def test_execute_repair_job_instance_full_healing_flow(tmp_settings: Path) -> None:
    t_src_id = "target_heal_src_1"
    t_dst_id = "target_heal_dst_1"
    t_src_root = tmp_settings / "heal_src_1"
    t_dst_root = tmp_settings / "heal_dst_1"

    backup_targets.register_filesystem_target(t_src_id, path=t_src_root)
    backup_targets.register_filesystem_target(t_dst_id, path=t_dst_root)

    content = b"heal payload 123"
    digest = hashlib.sha256(content).hexdigest()

    # Create component on source
    comp_file = t_src_root / "objects" / digest[:2] / digest[2:4] / f"{digest}.age"
    comp_file.parent.mkdir(parents=True, exist_ok=True)
    comp_file.write_bytes(content)

    receipt = {
        "schemaVersion": 4,
        "receiptDigest": "rcpt_heal_1",
        "objectSetDigest": "osd_heal_1",
        "policyId": "pol-heal-1",
        "backupId": "bk-heal-1",
        "createdAt": "2026-08-18T10:00:00Z",
        "objects": [{"digest": digest, "size": len(content)}],
    }

    rcpt_src_file = t_src_root / "receipts" / "pol-heal-1" / "bk-heal-1.json"
    rcpt_src_file.parent.mkdir(parents=True, exist_ok=True)
    rcpt_src_file.write_text(json.dumps(receipt), encoding="utf-8")

    # Record healthy source copy in ledger
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_src_id,
        policy_id="pol-heal-1",
        backup_id="bk-heal-1",
        committed_at="2026-08-18T10:00:00Z",
        object_set_digest="osd_heal_1",
        recoverable=True,
        state="healthy",
    )

    # Create and execute repair job
    r_job = backup_replication.create_repair_job(
        policy_id="pol-heal-1",
        backup_id="bk-heal-1",
        dest_target_id=t_dst_id,
        source_target_id=t_src_id,
    )

    with patch.object(backup_replication, "authenticate_recovery_copy") as mock_auth:
        mock_auth.side_effect = [
            ("authenticated", receipt, {}),  # Source authentication
            ("missing", None, {}),            # Destination initial check
        ]
        res = backup_replication.execute_repair_job_instance(r_job["repairId"])
        assert res["status"] == "success" or res.get("job", {}).get("phase") == "healthy"

    # Destination now has the component and receipt
    dst_comp = t_dst_root / "objects" / digest[:2] / digest[2:4] / f"{digest}.age"
    assert dst_comp.is_file()
    assert dst_comp.read_bytes() == content


def test_replication_job_management_and_spool_failure(tmp_settings: Path) -> None:
    # 1. _load_package_from_spool raises 409 when missing
    with pytest.raises(AppError) as exc_pkg:
        backup_replication._load_package_from_spool({
            "policyId": "pol_no_spool",
            "slotDigest": "slot_missing_123",
        })
    assert exc_pkg.value.status == 409

    # 2. _set_phase transitions and writes job
    job = {
        "jobId": "repl_job_phase_test",
        "phase": "queued",
    }
    updated = backup_replication._set_phase(job, "replicating", customKey="val1")
    assert updated["phase"] == "replicating"
    assert updated["customKey"] == "val1"

    # 3. SourceHold acquire, renew and release
    hold = backup_replication.acquire_source_hold("t1", "pol1", "bk1", "holder_test", hold_seconds=3600)
    assert hold.hold_id.startswith("hold_")
    hold.renew(1800)
    hold.release()


def test_replication_fail_job_and_process_pending_branches(tmp_settings: Path) -> None:
    # 1. _fail_job mode required + spool missing
    j1 = {"jobId": "j1", "attempts": 1, "maxAttempts": 5}
    r1 = backup_replication._fail_job(j1, Exception("spool missing for package"), mode="required")
    assert r1["phase"] == "repair-needed"

    # 2. _fail_job mode required + retry-wait
    r2 = backup_replication._fail_job(j1, Exception("network timeout"), mode="required")
    assert r2["phase"] == "retry-wait"
    assert "nextRetryAt" in r2

    # 3. _fail_job mode required + failed-terminal (max attempts exceeded)
    j3 = {"jobId": "j3", "attempts": 5, "maxAttempts": 5}
    r3 = backup_replication._fail_job(j3, Exception("fatal error"), mode="required")
    assert r3["phase"] == "failed-terminal"

    # 4. _fail_job mode optional -> failed
    r4 = backup_replication._fail_job(j1, Exception("optional fail"), mode="optional")
    assert r4["phase"] == "failed"

    # 5. process_pending_jobs with retry-wait in future
    future_iso = backup_replication._utc_iso(datetime.now(tz=timezone.utc) + timedelta(hours=2))
    past_iso = backup_replication._utc_iso(datetime.now(tz=timezone.utc) - timedelta(hours=2))
    jobs_mock = [
        {"jobId": "j_fut", "phase": "retry-wait", "nextRetryAt": future_iso},
        {"jobId": "j_past", "phase": "retry-wait", "nextRetryAt": past_iso},
    ]
    with patch.object(backup_replication, "list_jobs", return_value=jobs_mock):
        with patch.object(backup_replication, "execute_replication_job", return_value={"phase": "committed"}):
            summary = backup_replication.process_pending_jobs(limit=10)
            assert summary["processed"] == 1
            assert summary["committed"] == 1

    # 6. authenticate_recovery_copy missing & corrupt
    t_empty = SimpleNamespace(root=tmp_settings / "auth_empty", store=None)
    (tmp_settings / "auth_empty").mkdir(parents=True, exist_ok=True)
    st_miss, _, _ = backup_replication.authenticate_recovery_copy(t_empty, "pol1", "bk1")
    assert st_miss == "missing"

    # Corrupt receipt
    t_corr = SimpleNamespace(root=tmp_settings / "auth_corr", store=None)
    (tmp_settings / "auth_corr" / "receipts").mkdir(parents=True, exist_ok=True)
    (tmp_settings / "auth_corr" / "receipts" / "bk_bad.json").write_text("invalid json {{{", encoding="utf-8")
    st_corr, _, _ = backup_replication.authenticate_recovery_copy(t_corr, "pol1", "bk_bad")
    assert st_corr == "corrupt"


def test_replication_repair_exception_branches_and_pending_repairs(tmp_settings: Path) -> None:
    # 1. process_pending_repairs with empty list
    with patch.object(backup_replication, "list_repair_jobs", return_value=[]):
        res = backup_replication.process_pending_repairs()
        assert res["processed"] == 0

    # 2. process_pending_repairs with future nextAttemptAt (skipped)
    future_at = backup_replication._utc_iso(datetime.now(tz=timezone.utc) + timedelta(hours=2))
    past_at = backup_replication._utc_iso(datetime.now(tz=timezone.utc) - timedelta(hours=2))
    jobs = [
        {"repairId": "rep_fut", "phase": "retry-wait", "nextAttemptAt": future_at},
        {"repairId": "rep_past", "phase": "retry-wait", "nextAttemptAt": past_at, "attempt": 1, "maxAttempts": 5},
    ]
    with patch.object(backup_replication, "list_repair_jobs", return_value=jobs):
        with patch.object(backup_replication, "execute_repair_job_instance", return_value={"status": "success"}):
            res_proc = backup_replication.process_pending_repairs()
            assert res_proc["processed"] == 1

    # 3. _compute_repair_backoff_seconds
    assert backup_replication._compute_repair_backoff_seconds(1) > 0
    assert backup_replication._compute_repair_backoff_seconds(10) <= 300


def test_replication_authenticate_transition_parent_and_verify_dest(tmp_settings: Path) -> None:
    # 1. authenticate_transition_parent with various mismatches
    dummy_rc = {"receiptDigest": "rd1", "lineageId": "lin1", "objectSetDigest": "osd1"}
    dummy_cm = {"commitHash": "ch1", "receiptDigest": "rd1", "lineageId": "lin1", "objectSetDigest": "osd1"}

    with patch.object(backup_replication, "authenticate_recovery_copy", return_value=("authenticated", dummy_rc, dummy_cm)):
        # Successful match
        ok, reason = backup_replication.authenticate_transition_parent(
            SimpleNamespace(root=tmp_settings),
            "pol1",
            expected_parent_backup_id="bk_p",
            expected_receipt_digest="rd1",
            expected_commit_hash="ch1",
            expected_lineage_id="lin1",
            expected_object_set_digest="osd1",
        )
        assert ok is True
        assert reason == "authenticated"

        # Receipt digest mismatch
        ok_rd, r_rd = backup_replication.authenticate_transition_parent(
            SimpleNamespace(root=tmp_settings),
            "pol1",
            expected_parent_backup_id="bk_p",
            expected_receipt_digest="wrong_rd",
        )
        assert ok_rd is False
        assert r_rd == "parent-receipt-digest-mismatch"

        # Commit hash mismatch
        ok_ch, r_ch = backup_replication.authenticate_transition_parent(
            SimpleNamespace(root=tmp_settings),
            "pol1",
            expected_parent_backup_id="bk_p",
            expected_commit_hash="wrong_ch",
        )
        assert ok_ch is False
        assert r_ch == "parent-commit-hash-mismatch"

        # Lineage mismatch
        ok_lin, r_lin = backup_replication.authenticate_transition_parent(
            SimpleNamespace(root=tmp_settings),
            "pol1",
            expected_parent_backup_id="bk_p",
            expected_lineage_id="wrong_lin",
        )
        assert ok_lin is False
        assert r_lin == "parent-lineage-mismatch"

        # ObjectSetDigest mismatch
        ok_osd, r_osd = backup_replication.authenticate_transition_parent(
            SimpleNamespace(root=tmp_settings),
            "pol1",
            expected_parent_backup_id="bk_p",
            expected_object_set_digest="wrong_osd",
        )
        assert ok_osd is False
        assert r_osd == "parent-object-set-digest-mismatch"

    # 2. _verify_destination_component with store
    fake_store = SimpleNamespace(
        stat=lambda rel: SimpleNamespace(sha256="good_sha", provider_sha256=None) if rel == "good" else (SimpleNamespace(sha256="bad_sha", provider_sha256=None) if rel == "bad" else None),
        get_stream=lambda rel: [b"some-streamed-content"],
    )
    dest_t = SimpleNamespace(root=None, store=fake_store)
    is_v, is_c = backup_replication._verify_destination_component(dest_t, "good", "good_sha")
    assert is_v is True
    assert is_c is False

    is_v_bad, is_c_bad = backup_replication._verify_destination_component(dest_t, "bad", "expected_sha")
    assert is_v_bad is False
    assert is_c_bad is True

    is_v_miss, is_c_miss = backup_replication._verify_destination_component(dest_t, "missing", "good_sha")
    assert is_v_miss is False
    assert is_c_miss is False


def test_stream_ciphertext_transfer_store_mismatch_and_quarantine(tmp_settings: Path) -> None:
    # 1. Single chunk store digest mismatch
    fake_source = SimpleNamespace(root=None, store=SimpleNamespace(get_stream=lambda rel: iter([b"short-payload"])))
    fake_dest = SimpleNamespace(root=None, store=SimpleNamespace(put_if_absent=lambda *a, **k: None))

    with pytest.raises(AppError) as exc_dig:
        backup_replication.stream_ciphertext_transfer(
            fake_source,
            fake_dest,
            "src.bin",
            "dst.bin",
            "expected_wrong_sha256",
        )
    assert exc_dig.value.status == 500

    # 2. Quarantine replace on local filesystem with existing corrupt file
    src_dir = tmp_settings / "q_src"
    dst_dir = tmp_settings / "q_dst"
    src_dir.mkdir(parents=True, exist_ok=True)
    dst_dir.mkdir(parents=True, exist_ok=True)

    good_content = b"clean-content-bytes"
    good_digest = hashlib.sha256(good_content).hexdigest()
    (src_dir / "clean.bin").write_bytes(good_content)
    (dst_dir / "clean.bin").write_bytes(b"corrupt-data")

    t_s = SimpleNamespace(root=src_dir, store=None)
    t_d = SimpleNamespace(root=dst_dir, store=None)

    repaired_bytes = backup_replication.quarantine_and_replace_corrupt_remote_object(
        t_d,
        "clean.bin",
        good_digest,
        t_s,
        "clean.bin",
    )
    assert repaired_bytes == len(good_content)
    assert (dst_dir / "clean.bin").read_bytes() == good_content


def test_replication_reconcile_and_rebalance_filters(tmp_settings: Path) -> None:
    # 1. reconcile_policy_replicas with disabled replication
    pol_dis = {"policyId": "pol_dis", "replication": {"enabled": False}}
    with patch.object(backup_policies, "get_policy", return_value=pol_dis):
        res_dis = backup_replication.reconcile_policy_replicas("pol_dis")
        assert res_dis["status"] == "skipped"

    # 2. reconcile_policy_replicas with no targets
    pol_no_t = {"policyId": "pol_not", "replication": {"enabled": True, "targets": []}, "targetId": None}
    with patch.object(backup_policies, "get_policy", return_value=pol_no_t):
        with patch.object(backup_replication, "_load_cursors", return_value={}):
            res_not = backup_replication.reconcile_policy_replicas("pol_not")
            assert res_not["status"] in {"noop", "completed"}

    # 3. read_rebalance_job nonexistent
    assert backup_replication.read_rebalance_job("nonexistent_rebalance_job_id") is None

    # 4. list_rebalance_jobs filtering
    j1 = backup_replication.create_rebalance_job(
        policy_id="pol_filter_1",
        backup_id="bk_filter_1",
        dest_target_id="dest_t1",
        source_target_id="src_t1",
        reason="test",
    )
    j2 = backup_replication.create_rebalance_job(
        policy_id="pol_filter_2",
        backup_id="bk_filter_2",
        dest_target_id="dest_t2",
        source_target_id="src_t2",
        reason="test",
    )
    assert j1["policyId"] == "pol_filter_1"
    assert j2["policyId"] == "pol_filter_2"
    assert len(backup_replication.list_rebalance_jobs(policy_id="pol_filter_1")) == 1
    assert len(backup_replication.list_rebalance_jobs(backup_id="bk_filter_2")) == 1
    assert len(backup_replication.list_rebalance_jobs(dest_target_id="dest_t1")) == 1
    assert len(backup_replication.list_rebalance_jobs(source_target_id="src_t2")) == 1
    assert len(backup_replication.list_rebalance_jobs(phase="pending")) >= 2


def test_replication_maintenance_window_and_rebalance_execution(tmp_settings: Path) -> None:
    # 1. is_inside_maintenance_window
    pol_no_mw: dict[str, Any] = {}
    assert backup_replication.is_inside_maintenance_window(pol_no_mw) is True

    pol_day = {
        "placement": {
            "maintenanceWindow": {
                "timezone": "UTC",
                "start": "02:00",
                "end": "04:00",
            }
        }
    }
    t_inside = datetime(2026, 8, 18, 3, 0, 0, tzinfo=timezone.utc)
    t_outside = datetime(2026, 8, 18, 5, 0, 0, tzinfo=timezone.utc)
    assert backup_replication.is_inside_maintenance_window(pol_day, now=t_inside) is True
    assert backup_replication.is_inside_maintenance_window(pol_day, now=t_outside) is False

    # Wrap midnight
    pol_night = {
        "placement": {
            "maintenanceWindow": {
                "timezone": "UTC",
                "start": "22:00",
                "end": "04:00",
            }
        }
    }
    t_night_in = datetime(2026, 8, 18, 23, 0, 0, tzinfo=timezone.utc)
    t_night_out = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert backup_replication.is_inside_maintenance_window(pol_night, now=t_night_in) is True
    assert backup_replication.is_inside_maintenance_window(pol_night, now=t_night_out) is False

    # 2. execute_rebalance_job 404
    with pytest.raises(AppError) as exc_404:
        backup_replication.execute_rebalance_job("nonexistent_reb_job")
    assert exc_404.value.status == 404

    # 3. process_pending_rebalances empty
    with patch.object(backup_replication, "list_rebalance_jobs", return_value=[]):
        res_reb = backup_replication.process_pending_rebalances()
        assert res_reb["processed"] == 0


def test_replication_lag_and_rebalance_window_branches(tmp_settings: Path) -> None:
    # 1. calculate_replica_lag with no primary
    with patch.object(backup_dr_ledger, "list_logical_recovery_copies", return_value=[]):
        with patch.object(backup_dr_ledger, "get_latest_recoverable_point", return_value=(None, None)):
            res_nop = backup_replication.calculate_replica_lag("pol_nop", "rep_t", primary_target_id="p_t")
            assert res_nop["status"] == "no-primary"

    # 2. calculate_replica_lag with no replica
    p_dummy = {"targetId": "p_t", "backupId": "b1", "committedAt": backup_replication._utc_iso(), "recoverable": True}
    with patch.object(backup_dr_ledger, "list_logical_recovery_copies", return_value=[p_dummy]):
        with patch.object(backup_dr_ledger, "get_latest_recoverable_point", side_effect=[(p_dummy, None), (None, None)]):
            res_nor = backup_replication.calculate_replica_lag("pol_nor", "rep_t", primary_target_id="p_t")
            assert res_nor["status"] == "no-replica"

    # 3. rebalance_policy_replicas outside maintenance window
    pol_window = {
        "policyId": "pol_win",
        "replication": {"enabled": True, "targets": [{"targetId": "t1"}]},
        "placement": {
            "maintenanceWindow": {
                "timezone": "UTC",
                "start": "02:00",
                "end": "03:00",
            }
        },
    }
    t_outside = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
    with patch.object(backup_policies, "get_policy", return_value=pol_window):
        res_out = backup_replication.rebalance_policy_replicas("pol_win", now=t_outside)
        assert res_out["status"] == "skipped"
        assert res_out["reason"] == "outside-maintenance-window"

    # 4. rebalance_policy_replicas replication disabled
    pol_dis = {"policyId": "pol_dis", "replication": {"enabled": False}}
    with patch.object(backup_policies, "get_policy", return_value=pol_dis):
        res_d = backup_replication.rebalance_policy_replicas("pol_dis")
        assert res_d["status"] == "skipped"
        assert res_d["reason"] == "replication-disabled"


def test_replication_compliance_lag_and_retention_holds(tmp_settings: Path) -> None:
    # 1. replication_compliance with replica lag exceeded
    pol_lag = {
        "policyId": "pol_lag_ex",
        "replication": {
            "enabled": True,
            "minCommittedCopies": 1,
            "maxReplicaLagSeconds": 60,
            "targets": [{"targetId": "rep_1"}],
        },
    }
    with patch.object(backup_replication, "calculate_replica_lag", return_value={"lagSeconds": 120}):
        with patch.object(backup_dr_ledger, "list_logical_recovery_copies", return_value=[{"targetId": "rep_1", "recoverable": True, "state": "healthy"}]):
            comp = backup_replication.replication_compliance(policy=pol_lag, backup_id="bk_lag")
            assert comp["compliance"] == "degraded"
            assert any("replica-lag-exceeded" in r for r in comp["reasons"])

    # 2. _restore_hold_digests in retention
    fake_obj = SimpleNamespace(key="holds/restore/h1.json")
    fake_hold_data = {
        "expiresAt": backup_retention._utc_iso(),
        "objectSetDigest": "a" * 64,
        "objects": [{"digest": "b" * 64}],
    }
    fake_store = SimpleNamespace(
        list_objects=lambda prefix, cursor=None: SimpleNamespace(objects=[fake_obj], cursor=None),
    )
    with patch("deepseek_infra.infra.workspace.backup_target_store.read_json", return_value=fake_hold_data):
        digs = backup_retention._restore_hold_digests(fake_store)
        assert "a" * 64 in digs
        assert "b" * 64 in digs


def test_backup_replication_repair_job_instance_exception_paths(tmp_settings: Path) -> None:
    # 1. Missing repair job raises 404
    with pytest.raises(AppError) as exc_rep:
        backup_replication.execute_repair_job_instance("missing_repair_id")
    assert exc_rep.value.status == 404

    # 2. Max attempts exceeded raises 409
    job_max = {
        "repairId": "rep_max",
        "policyId": "pol_1",
        "backupId": "bk_1",
        "destTargetId": "dst_1",
        "attempt": 10,
        "maxAttempts": 5,
        "phase": "queued",
    }
    with patch.object(backup_replication, "read_repair_job", return_value=job_max):
        with patch.object(backup_replication, "_set_repair_phase", return_value=job_max):
            with pytest.raises(AppError) as exc_max:
                backup_replication.execute_repair_job_instance("rep_max")
            assert exc_max.value.status == 409

    # 3. No healthy source copy raises 404
    job_nosrc = {
        "repairId": "rep_nosrc",
        "policyId": "pol_1",
        "backupId": "bk_1",
        "destTargetId": "dst_1",
        "attempt": 1,
        "maxAttempts": 5,
        "phase": "queued",
        "sourceTargetId": None,
    }
    with patch.object(backup_replication, "read_repair_job", return_value=job_nosrc):
        with patch.object(backup_replication, "_set_repair_phase", return_value=job_nosrc):
            with patch.object(backup_dr_ledger, "list_logical_recovery_copies", return_value=[]):
                with pytest.raises(AppError) as exc_nosrc:
                    backup_replication.execute_repair_job_instance("rep_nosrc")
                assert exc_nosrc.value.status == 404

    # 4. list_repair_jobs with multiple filters
    rep_list = [
        {"repairId": "r1", "policyId": "p1", "backupId": "b1", "destTargetId": "d1", "sourceTargetId": "s1", "phase": "queued"},
        {"repairId": "r2", "policyId": "p2", "backupId": "b2", "destTargetId": "d2", "sourceTargetId": "s2", "phase": "healthy"},
    ]
    backup_replication.REPAIRS_DIR.mkdir(parents=True, exist_ok=True)
    (backup_replication.REPAIRS_DIR / "r1.json").write_text(json.dumps(rep_list[0]), encoding="utf-8")
    (backup_replication.REPAIRS_DIR / "r2.json").write_text(json.dumps(rep_list[1]), encoding="utf-8")

    filtered = backup_replication.list_repair_jobs(policy_id="p1", phase="queued")
    assert len(filtered) == 1
    assert filtered[0]["repairId"] == "r1"


def test_backup_replication_execute_repair_job_instance_full_flow(tmp_settings: Path) -> None:
    src_dir = tmp_settings / "src_rep_flow"
    dst_dir = tmp_settings / "dst_rep_flow"
    src_dir.mkdir(parents=True, exist_ok=True)
    dst_dir.mkdir(parents=True, exist_ok=True)

    fake_src_target = SimpleNamespace(root=src_dir, store=None, target_id="src_flow")
    fake_dst_target = SimpleNamespace(root=dst_dir, store=None, target_id="dst_flow")

    d_hex = "f" * 64
    comp_file = src_dir / "objects" / d_hex[:2] / d_hex[2:4] / f"{d_hex}.age"
    comp_file.parent.mkdir(parents=True, exist_ok=True)
    comp_file.write_bytes(b"ciphertext-flow-data")

    source_rcpt = {
        "schemaVersion": 4,
        "backupId": "bk_flow_1",
        "targetId": "src_flow",
        "objects": [{"digest": d_hex, "size": len(b"ciphertext-flow-data")}],
        "objectSetDigest": "os_" + d_hex[:32],
    }

    job_flow = {
        "repairId": "rep_flow_1",
        "policyId": "pol_flow",
        "backupId": "bk_flow_1",
        "destTargetId": "dst_flow",
        "sourceTargetId": "src_flow",
        "attempt": 0,
        "maxAttempts": 5,
        "phase": "queued",
        "components": {},
    }
    backup_replication.REPAIRS_DIR.mkdir(parents=True, exist_ok=True)
    (backup_replication.REPAIRS_DIR / "rep_flow_1.json").write_text(json.dumps(job_flow), encoding="utf-8")

    fake_hold = SimpleNamespace(hold_id="h_flow", renew=lambda: None, release=lambda: None)
    fake_writer = SimpleNamespace(acquire=lambda: None, release=lambda: None)

    with patch.object(backup_publish, "resolve_target", side_effect=[fake_src_target, fake_dst_target]):
        with patch.object(backup_replication, "acquire_source_hold", return_value=fake_hold):
            with patch.object(backup_replication, "authenticate_recovery_copy", side_effect=[("authenticated", source_rcpt, None), ("missing", None, None)]):
                with patch.object(backup_writer_lease, "TargetWriterLease", return_value=fake_writer):
                    with patch.object(backup_replication, "_verify_destination_component", return_value=(False, False)):
                        with patch.object(backup_replication, "stream_ciphertext_transfer", return_value=len(b"ciphertext-flow-data")):
                            with patch.object(backup_replication, "append_target_local_catalog"):
                                res = backup_replication.execute_repair_job_instance("rep_flow_1")
                                assert res["status"] == "success"
                                assert res["repairMode"] == "provision"
                                assert res["bytesRepaired"] == len(b"ciphertext-flow-data")


def test_backup_replication_committed_auth_and_resumed_stream(tmp_settings: Path) -> None:
    # 1. authenticate_committed_copy mismatch branches
    dummy_target = SimpleNamespace(root=tmp_settings, store=None)
    fake_rcpt = {"policyId": "p1", "backupId": "b1", "objectSetDigest": "osd_1"}
    fake_cmt = {
        "schemaVersion": 4,
        "policyId": "p1",
        "backupId": "b1",
        "objectSetDigest": "osd_1",
        "commitHash": "c" * 64,
        "previousCommitHash": "prev_hash_1",
        "targetGeneration": 2,
    }
    with patch.object(backup_replication, "authenticate_recovery_copy", return_value=("authenticated", fake_rcpt, fake_cmt)):
        with patch.object(backup_publish, "_commit_hash", return_value="different_calc_hash"):
            st_c, _, _ = backup_replication.authenticate_committed_copy(dummy_target, "p1", "b1")
            assert st_c == "corrupt"

    with patch.object(backup_replication, "authenticate_recovery_copy", return_value=("authenticated", fake_rcpt, fake_cmt)):
        with patch.object(backup_publish, "_commit_hash", return_value="c" * 64):
            # previous commit hash mismatch
            st_prev, _, _ = backup_replication.authenticate_committed_copy(dummy_target, "p1", "b1", expected_previous_commit_hash="other_prev")
            assert st_prev == "conflicting"

            # target generation mismatch
            st_gen, _, _ = backup_replication.authenticate_committed_copy(dummy_target, "p1", "b1", expected_target_generation=99)
            assert st_gen == "conflicting"

    # 2. stream_ciphertext_transfer resuming multipart upload
    data_total = b"part1_content" + b"part2_content"
    d_total_hex = hashlib.sha256(data_total).hexdigest()

    fake_store = SimpleNamespace(
        upload_part=lambda upload, num, chunk: SimpleNamespace(etag=f"etag_{num}"),
        abort_multipart=lambda upload: None,
        complete_multipart_if_absent=lambda upload: None,
    )
    src_target = SimpleNamespace(
        root=None,
        store=SimpleNamespace(get_stream=lambda rel: iter([b"part1_content", b"part2_content"])),
    )
    dst_target = SimpleNamespace(root=None, store=fake_store)

    prog_state = {
        "multipartUploadId": "mp_up_123",
        "nextOffset": len(b"part1_content"),
        "parts": [{"number": 1, "etag": "etag_1"}],
    }
    transferred = backup_replication.stream_ciphertext_transfer(
        src_target,
        dst_target,
        "src_rel_key",
        "dst_rel_key",
        d_total_hex,
        progress_state=prog_state,
    )
    assert transferred == len(data_total)
    parts_list = prog_state["parts"]
    assert isinstance(parts_list, list) and len(parts_list) == 2


def test_backup_replication_full_rebalance_flow_and_reasons(tmp_settings: Path) -> None:
    pol = {
        "policyId": "pol_reb_full",
        "replication": {
            "enabled": True,
            "minFailureDomains": 2,
            "targets": [{"targetId": "t_src"}, {"targetId": "t_dst"}],
        },
        "placement": {
            "softWatermarkPercent": 80.0,
            "maxCopiesPerFailureDomain": 2,
        },
    }

    t_src_rec = {"targetId": "t_src", "failureDomain": "fd-1", "drainState": "active"}
    t_dst_rec = {"targetId": "t_dst", "failureDomain": "fd-2", "drainState": "active"}

    copy_src = {
        "targetId": "t_src",
        "policyId": "pol_reb_full",
        "backupId": "bk_reb_1",
        "recoverable": True,
        "state": "healthy",
        "committedAt": backup_replication._utc_iso(),
    }

    with patch.object(backup_policies, "get_policy", return_value=pol):
        with patch.object(backup_targets, "list_targets", return_value=[t_src_rec, t_dst_rec]):
            with patch.object(backup_dr_ledger, "list_logical_recovery_copies", return_value=[copy_src]):
                with patch.object(backup_targets, "probe_target_capacity", return_value={"freePercent": 50.0}):
                    with patch.object(backup_replication, "execute_replica_repair", return_value={"status": "success", "bytesRepaired": 100}):
                        with patch.object(backup_publish, "resolve_target", return_value=SimpleNamespace(root=tmp_settings, store=None)):
                            with patch.object(backup_replication, "authenticate_committed_copy", return_value=("authenticated", {"backupId": "bk_reb_1"}, {"backupId": "bk_reb_1"})):
                                res = backup_replication.rebalance_policy_replicas("pol_reb_full")
                                assert res["status"] == "completed"
                                assert res["jobsCreated"] == 1

    # Drain migration scenario
    t_src_draining = {"targetId": "t_src", "failureDomain": "fd-1", "drainState": "draining"}
    with patch.object(backup_policies, "get_policy", return_value=pol):
        with patch.object(backup_targets, "list_targets", return_value=[t_src_draining, t_dst_rec]):
            with patch.object(backup_dr_ledger, "list_logical_recovery_copies", return_value=[copy_src]):
                with patch.object(backup_targets, "probe_target_capacity", return_value={"freePercent": 50.0}):
                    with patch.object(backup_replication, "execute_replica_repair", return_value={"status": "success", "bytesRepaired": 100}):
                        with patch.object(backup_publish, "resolve_target", return_value=SimpleNamespace(root=tmp_settings, store=None)):
                            with patch.object(backup_replication, "authenticate_committed_copy", return_value=("authenticated", {"backupId": "bk_reb_1"}, {"backupId": "bk_reb_1"})):
                                with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}):
                                    res_drain = backup_replication.rebalance_policy_replicas("pol_reb_full")
                                    assert res_drain["status"] == "completed"
                                    assert res_drain["jobsCreated"] == 1


def test_backup_replication_source_hold_remote_store_renew_and_exceptions(tmp_settings: Path) -> None:
    fake_store = SimpleNamespace(
        put_if_absent=lambda key, data: SimpleNamespace(etag="etag_init"),
        put_if_match=lambda key, data, expected_etag=None: SimpleNamespace(etag="etag_renewed"),
        delete_if_match=lambda key, expected_etag=None: True,
    )

    # 1. Acquire remote store hold and renew
    hold = backup_replication.acquire_source_hold(
        "tgt_rem",
        "pol_rem",
        "bk_rem",
        "holder_1",
        target_store=fake_store,
    )
    assert hold.etag == "etag_init"

    hold.renew()
    assert hold.etag == "etag_renewed"
    assert hold.generation == 2

    # 2. Renew failure raises RepairLeaseLostError
    failing_store = SimpleNamespace(
        put_if_match=MagicMock(side_effect=Exception("etag conflict")),
    )
    hold_fail = backup_replication.SourceHold(
        "h_fail",
        "tgt_f",
        "pol_f",
        "bk_f",
        "holder_f",
        target_store=failing_store,
        etag="old_etag",
    )
    with pytest.raises(backup_replication.RepairLeaseLostError):
        hold_fail.renew()

    # 3. acquire_source_hold store exception raises 503
    failing_init_store = SimpleNamespace(
        put_if_absent=MagicMock(side_effect=Exception("network down")),
    )
    with pytest.raises(AppError) as exc_acq:
        backup_replication.acquire_source_hold(
            "tgt_rem",
            "pol_rem",
            "bk_rem",
            "holder_1",
            target_store=failing_init_store,
        )
    assert exc_acq.value.status == 503


def test_backup_replication_has_open_required_jobs_branches(tmp_settings: Path) -> None:
    # 1. No jobs -> False
    with patch.object(backup_replication, "list_jobs", return_value=[]):
        assert backup_replication.has_open_required_jobs(policy_id="p1") is False

    # 2. Terminal or optional jobs -> False
    job_term = {"mode": "required", "phase": "committed", "slotDigest": "sd1"}
    job_opt = {"mode": "optional", "phase": "queued", "slotDigest": "sd1"}
    with patch.object(backup_replication, "list_jobs", return_value=[job_term, job_opt]):
        assert backup_replication.has_open_required_jobs(policy_id="p1") is False

    # 3. Open required job matching slotDigest -> True
    job_open = {"mode": "required", "phase": "queued", "slotDigest": "sd1"}
    with patch.object(backup_replication, "list_jobs", return_value=[job_open]):
        assert backup_replication.has_open_required_jobs(policy_id="p1", slot_digest="sd1") is True
        assert backup_replication.has_open_required_jobs(policy_id="p1", slot_digest="other_sd") is False
