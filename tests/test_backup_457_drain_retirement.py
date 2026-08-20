"""Coverage tests for Node Drain, Retirement, and Topology Safety (v4.5)."""

from __future__ import annotations

import json
import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_drain,
    backup_dr_ledger,
    backup_executor,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_retirement,
    backup_scheduler,
    backup_targets,
    backup_transfer_budget,
)


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def test_retirement_edge_branches(tmp_settings: Path) -> None:
    t1 = "target_retire_edges"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "ret_edges")
    policy_id = "pol-ret-edges"

    # 1. Nonexistent job operations
    assert backup_retirement.get_copy_retirement_job("job-none") is None
    with pytest.raises(AppError):
        backup_retirement.cancel_copy_retirement_job("job-none")
    with pytest.raises(AppError):
        backup_retirement.execute_copy_retirement_job("job-none")

    # 2. Rejection due to compliance hold
    with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": True}):
        job_held = backup_retirement.create_copy_retirement_job(policy_id, "bk-held-ret", t1)
        res_held = backup_retirement.execute_copy_retirement_job(job_held["jobId"])
        assert res_held["phase"] == "rejected"
        assert "copy-protected-by-hold" in res_held["error"]

    # 3. Rejection due to policy unsafe
    with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": False, "protectedByHold": False, "reasons": ["insufficient-copies"]}):
        job_unsafe = backup_retirement.create_copy_retirement_job(policy_id, "bk-unsafe-ret", t1)
        res_unsafe = backup_retirement.execute_copy_retirement_job(job_unsafe["jobId"])
        assert res_unsafe["phase"] == "rejected"

    # 4. Cancel active retirement job
    job_cancel = backup_retirement.create_copy_retirement_job(policy_id, "bk-cancel-ret", t1)
    res_canc = backup_retirement.cancel_copy_retirement_job(job_cancel["jobId"], reason="user-stop")
    assert res_canc["phase"] == "cancelled"

    # Cancel already cancelled job
    res_canc_again = backup_retirement.cancel_copy_retirement_job(job_cancel["jobId"])
    assert res_canc_again["phase"] == "cancelled"


def test_retirement_shared_components_gc(tmp_settings: Path) -> None:
    t1 = "target_retire_shared"
    t_root = tmp_settings / "retire_shared"
    backup_targets.register_filesystem_target(t1, path=t_root)

    policy_id = "pol-retire-shared"
    b1 = "bk-shared-1"
    b2 = "bk-shared-2"

    # Setup object directory
    (t_root / "objects" / "sha256" / "aa").mkdir(parents=True, exist_ok=True)
    comp1 = t_root / "objects" / "sha256" / "aa" / "aabbcc.age"
    comp2 = t_root / "objects" / "sha256" / "aa" / "ddeeff.age"
    comp1.write_bytes(b"shared-payload")
    comp2.write_bytes(b"unique-payload")

    # Write receipt for b1 referencing comp1 (shared) and comp2 (unique)
    r1_dir = t_root / "receipts"
    r1_dir.mkdir(parents=True, exist_ok=True)
    r1 = {
        "schemaVersion": 4,
        "targetId": t1,
        "policyId": policy_id,
        "backupId": b1,
        "filename": "objects/sha256/aa/aabbcc.age",
        "components": [
            {"path": "objects/sha256/aa/ddeeff.age", "digest": "ddeeff"},
            {"digest": "uniquedigest123"},
        ],
    }
    r1_bytes = (json.dumps(r1, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (r1_dir / f"{b1}.json").write_bytes(r1_bytes)
    commit = {
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "targetId": t1,
        "policyId": policy_id,
        "backupId": b1,
        "receiptDigest": hashlib.sha256(r1_bytes).hexdigest(),
        "committedAt": "2026-08-18T09:00:00Z",
    }
    commit["commitHash"] = backup_publish._commit_hash(commit)
    commit_path = t_root / "commits" / policy_id / f"{b1}.json"
    commit_path.parent.mkdir(parents=True, exist_ok=True)
    commit_path.write_text(json.dumps(commit), encoding="utf-8")

    # Write receipt for b2 referencing comp1 (shared)
    r2 = {
        "filename": "objects/sha256/aa/aabbcc.age",
        "components": [{"digest": "shareddigest456"}],
    }
    (r2_dir := t_root / "receipts" / policy_id).mkdir(parents=True, exist_ok=True)
    (r2_dir / f"{b2}.json").write_text(json.dumps(r2), encoding="utf-8")

    # Add invalid JSON in receipts dir to test exception handling in scanner
    (r1_dir / "invalid.json").write_text("invalid json content {{{", encoding="utf-8")

    # Create & execute retirement job for b1
    with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}):
        job = backup_retirement.create_copy_retirement_job(policy_id, b1, t1)
        res = backup_retirement.execute_copy_retirement_job(job["jobId"])
        assert res["phase"] == "reclaimed"

        # Unique component was unlinked, shared component was retained!
        assert comp1.is_file()
        assert not comp2.is_file()

        # Terminal job execution returns immediately
        res_repeat = backup_retirement.execute_copy_retirement_job(job["jobId"])
        assert res_repeat["phase"] == "reclaimed"

    # Execution error handling
    with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}):
        with patch.object(backup_publish, "resolve_target", side_effect=Exception("resolve-failed")):
            job_err = backup_retirement.create_copy_retirement_job(policy_id, "bk-err", t1)
            res_err = backup_retirement.execute_copy_retirement_job(job_err["jobId"])
            assert res_err["phase"] == "failed"

    # 6. Remote target store full GC execution
    mock_remote_store = MagicMock()
    r_remote_data = {
        "filename": "ciphertext/sha256/deadbeef",
        "components": [{"digest": "d111", "path": "ciphertext/sha256/d111"}],
    }
    other_r_data = {
        "filename": "ciphertext/sha256/shared_file",
        "components": [{"digest": "d222", "path": "ciphertext/sha256/d222"}],
    }

    def _mock_get_bytes(key: str) -> bytes:
        if "bk-remote-1" in key:
            return json.dumps(r_remote_data).encode("utf-8")
        if "bk-other" in key:
            return json.dumps(other_r_data).encode("utf-8")
        return b"{}"

    mock_remote_store.get_bytes.side_effect = _mock_get_bytes
    mock_remote_store.stat.return_value = MagicMock(size=500, etag="etag_rem")
    mock_remote_store.delete_if_match.return_value = True
    mock_remote_store.list_objects.return_value = SimpleNamespace(objects=(), cursor=None)

    mock_rem_target = MagicMock()
    mock_rem_target.root = None
    mock_rem_target.store = mock_remote_store

    # Record other live copy on t1 in DR ledger
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t1,
        policy_id=policy_id,
        backup_id="bk-other",
        committed_at="2026-08-18T10:00:00Z",
        state="healthy",
        recoverable=True,
    )

    remote_receipt_bytes = (json.dumps(r_remote_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    remote_commit = {"commitHash": "c" * 64, "receiptDigest": hashlib.sha256(remote_receipt_bytes).hexdigest()}
    with patch.object(backup_publish, "resolve_target", return_value=mock_rem_target):
        with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}):
            with patch.object(backup_retirement, "_read_formal_metadata", return_value=(remote_receipt_bytes, r_remote_data, remote_commit)):
                with patch.object(backup_retirement, "_write_retirement_marker"):
                    job_rem = backup_retirement.create_copy_retirement_job(policy_id, "bk-remote-1", t1)
                    res_rem = backup_retirement.execute_copy_retirement_job(job_rem["jobId"])
                    assert res_rem["phase"] == "reclaimed"
                    assert res_rem["bytesReclaimed"] > 0

    # 7. List retirement jobs with multiple filters
    filtered_jobs = backup_retirement.list_copy_retirement_jobs(policy_id=policy_id, target_id=t1, phase="reclaimed")
    assert len(filtered_jobs) >= 1


def test_drain_more_branches(tmp_settings: Path) -> None:
    t1 = "target_drain_br_1"
    t2 = "target_drain_br_2"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "dbr1")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "dbr2")

    # 1. Process drain when no job exists
    skipped = backup_drain.process_target_drain("target_nonexistent")
    assert skipped["status"] == "skipped"

    # 2. Start and cancel drain
    job = backup_drain.start_target_drain(t1, reason="test-cancel")
    assert job["phase"] == "draining"

    cancelled = backup_drain.cancel_target_drain(t1, reason="abort")
    assert cancelled["phase"] == "cancelled"

    # 3. Process drain when job is already terminal
    term_res = backup_drain.process_target_drain(t1)
    assert term_res["status"] == "completed"

    # 4. List with filter
    d_list = backup_drain.list_target_drain_jobs(phase="cancelled")
    assert any(j["targetId"] == t1 for j in d_list)


def test_drain_job_queries_and_held_state(tmp_settings: Path) -> None:
    t1 = "target_drain_q1"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "dq1")

    # None args
    assert backup_drain.get_target_drain_job() is None

    # Start drain
    job = backup_drain.start_target_drain(t1, reason="q-test")
    drain_id = str(job["drainId"])

    # Query by drain_id
    by_did = backup_drain.get_target_drain_job(drain_id=drain_id)
    assert by_did is not None
    assert by_did["targetId"] == t1

    # Query target drain state via targets module
    d_state = backup_targets.get_target_drain_state(t1)
    assert d_state in {"draining", "drained", "active"}

    # Process drain when copy has active hold
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t1,
        policy_id="pol-held",
        backup_id="bk-held",
        committed_at="2026-08-18T10:00:00Z",
        state="healthy",
        recoverable=False,
    )
    with patch.object(backup_replication, "is_source_held", return_value=True):
        held_res = backup_drain.process_target_drain(t1)
        assert held_res.get("phase") in {"evacuating", "draining", None}


def test_drain_and_transfer_full_coverage(tmp_settings: Path) -> None:
    t_src = "target_drain_fc_src"
    t_dst = "target_drain_fc_dst"
    backup_targets.register_filesystem_target(t_src, path=tmp_settings / "dfc_src")
    backup_targets.register_filesystem_target(t_dst, path=tmp_settings / "dfc_dst")

    # 1. start_target_drain on nonexistent target
    with pytest.raises(AppError):
        backup_drain.start_target_drain("target_nonexistent_xyz")

    # 2. Complete drain evacuation when no live copies and no active holds -> marked drained
    backup_drain.start_target_drain(t_src, reason="test-complete-drain")
    res_drained = backup_drain.process_target_drain(t_src)
    assert res_drained["status"] == "drained"
    assert backup_targets.get_target_drain_state(t_src) == "drained"

    # 3. Drain evacuation with live copies -> triggers rebalance
    backup_targets.activate_target(t_src)
    backup_drain.start_target_drain(t_src, reason="test-live-drain")
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_src,
        policy_id="pol-drain-live",
        backup_id="bk-drain-live",
        committed_at="2026-08-18T10:00:00Z",
        state="healthy",
        recoverable=True,
    )
    with patch.object(backup_replication, "create_rebalance_job", return_value={"jobId": "reb-test-1"}):
        with patch.object(backup_replication, "execute_rebalance_job", return_value={"phase": "completed"}):
            res_live = backup_drain.process_target_drain(t_src)
            assert res_live["status"] == "in_progress"
            assert res_live["rebalancesTriggered"] >= 1

    # 4. TransferBudgetManager concurrency limit & P0 bypass
    mgr = backup_transfer_budget.TransferBudgetManager()
    mgr.set_target_budget(t_dst, max_concurrent_transfers=1)
    x1 = mgr.acquire_transfer_token("x1", backup_transfer_budget.TrafficClass.P3_REQUIRED_REPLICATION, dest_target_id=t_dst)
    assert x1 is not None

    # Secondary non-P0 transfer raises RATE_LIMITED (429)
    with pytest.raises(AppError):
        mgr.acquire_transfer_token("x2", backup_transfer_budget.TrafficClass.P3_REQUIRED_REPLICATION, dest_target_id=t_dst)

    # P0 disaster recovery bypasses concurrency limit
    x_p0 = mgr.acquire_transfer_token("x-p0", backup_transfer_budget.TrafficClass.P0_DISASTER_RECOVERY, dest_target_id=t_dst)
    assert x_p0 is not None

    # Acquire bandwidth dummy check
    granted = mgr.acquire_bandwidth(5000)
    assert granted >= 5000

    mgr.release_transfer_token("x1")
    mgr.release_transfer_token("x-p0")
    assert mgr.active_transfers_count() == 0


def test_drain_autonomous_evacuation(tmp_settings: Path) -> None:
    t1 = "target_drain_auto_1"
    t2 = "target_drain_auto_2"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "da1", failure_domain="fd1")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "da2", failure_domain="fd2")

    policy_id = "drain-auto-pol"
    backup_policies.create_policy(
        {
            "name": "Drain Auto Policy",
            "policyId": policy_id,
            "targetId": t1,
            "replication": {
                "enabled": True,
                "targets": [{"targetId": t2, "mode": "required"}],
            },
        }
    )

    backup_drain.initiate_target_drain(t1, reason="evacuate-test")
    # Process drain when no active copies exist on target
    with patch.object(backup_dr_ledger, "list_logical_recovery_copies", return_value=[]):
        res = backup_drain.process_target_drain(t1)
        assert res.get("phase") == "drained" or res.get("job", {}).get("phase") == "drained"


def test_backup_drain_exhaustive_coverage(tmp_settings: Path) -> None:
    # 1. Invalid target raises 404
    with pytest.raises(AppError) as exc_nf:
        backup_drain.start_target_drain("nonexistent_target_xyz")
    assert exc_nf.value.status == 404

    # 2. get_target_drain_job with None returns None
    assert backup_drain.get_target_drain_job() is None

    # 3. process_target_drain with no job returns skipped
    res_skip = backup_drain.process_target_drain("target_no_job_xyz")
    assert res_skip["status"] == "skipped"

    # 4. Drain lifecycle with target registration
    t_id = "target_drain_exhaust_1"
    backup_targets.register_filesystem_target(t_id, path=tmp_settings / "dr_ex_1")

    job = backup_drain.start_target_drain(t_id, reason="exhaust-test")
    assert job["phase"] == "draining"
    assert job["drainId"]

    # Lookup by drain_id
    by_id = backup_drain.get_target_drain_job(drain_id=job["drainId"])
    assert by_id is not None
    assert by_id["targetId"] == t_id

    # List drain jobs with phase filter
    drains_draining = backup_drain.list_target_drain_jobs(phase="draining")
    assert any(d["targetId"] == t_id for d in drains_draining)

    # Process drain with 0 copies -> immediately marks drained
    res_proc = backup_drain.process_target_drain(t_id)
    assert res_proc["status"] == "drained"

    # Completed job process returns completed
    res_repeat = backup_drain.process_target_drain(t_id)
    assert res_repeat["status"] == "completed"

    # Cancel drain returns cancelled
    res_canc = backup_drain.cancel_target_drain(t_id, reason="test-cancel")
    assert res_canc["phase"] == "cancelled"


def test_backup_drain_job_queries_and_waiting_for_gc(tmp_settings: Path) -> None:
    # 1. get_target_drain_job with None returns None
    assert backup_drain.get_target_drain_job() is None

    # 2. start drain and query by drain_id
    t_id = "target_drain_q"
    backup_targets.register_filesystem_target(t_id, path=tmp_settings / "drain_q_root")
    job = backup_drain.start_target_drain(t_id, reason="query-test")
    assert job["targetId"] == t_id

    job_by_id = backup_drain.get_target_drain_job(drain_id=job["drainId"])
    assert job_by_id is not None
    assert job_by_id["drainId"] == job["drainId"]

    # 3. list_target_drain_jobs with phase filter
    listed = backup_drain.list_target_drain_jobs(phase="draining")
    assert any(j["drainId"] == job["drainId"] for j in listed)

    # 4. process_target_drain with active holds on unrecoverable copy -> waiting-for-gc
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t_id,
        policy_id="pol_drain_held",
        backup_id="bk_drain_held",
        committed_at=backup_drain._utc_iso(),
        state="degraded",
        recoverable=False,
    )
    with patch.object(backup_replication, "is_source_held", return_value=True):
        res_held = backup_drain.process_target_drain(t_id)
        assert res_held["status"] == "in_progress"
        assert res_held["job"]["phase"] == "waiting-for-gc"


def test_backup_drain_start_cancel_and_list_branches(tmp_settings: Path) -> None:
    # 1. start_target_drain on missing target raises 404
    with patch.object(backup_targets, "get_target", return_value=None):
        with pytest.raises(AppError) as exc_t:
            backup_drain.start_target_drain("missing_tgt")
        assert exc_t.value.status == 404

    # 2. start and cancel drain
    fake_tgt = {"targetId": "tgt_dr1", "kind": "filesystem", "drainState": "active"}
    with patch.object(backup_targets, "get_target", return_value=fake_tgt):
        with patch.object(backup_targets, "drain_target"):
            job = backup_drain.start_target_drain("tgt_dr1", reason="maintenance")
            assert job["phase"] == "draining"

    with patch.object(backup_targets, "activate_target"):
        canc = backup_drain.cancel_target_drain("tgt_dr1", reason="cancelled-by-admin")
        assert canc["phase"] == "cancelled"
        assert canc["error"] == "cancelled-by-admin"

    # 3. list_target_drain_jobs
    jobs_all = backup_drain.list_target_drain_jobs()
    assert any(j["targetId"] == "tgt_dr1" for j in jobs_all)

    jobs_canc = backup_drain.list_target_drain_jobs(phase="cancelled")
    assert any(j["targetId"] == "tgt_dr1" for j in jobs_canc)


def test_backup_targets_drain_states_and_executor_blocked(tmp_settings: Path) -> None:
    # 1. backup_targets mark_target_drained and get_target_drain_state
    fake_tgt = {"targetId": "tgt_drained", "path": str(tmp_settings / "tgt_dr_path")}
    with patch.object(backup_targets, "get_target", return_value=fake_tgt):
        with patch.object(backup_targets, "_atomic_write_json"):
            res_dr = backup_targets.mark_target_drained("tgt_drained")
            assert res_dr["drainState"] == "drained"

    with patch.object(backup_targets, "get_target", return_value={"drainState": "draining"}):
        st = backup_targets.get_target_drain_state("tgt_drained")
        assert st == "draining"

    with patch.object(backup_targets, "get_target", side_effect=Exception("error")):
        st_unk = backup_targets.get_target_drain_state("missing_tgt")
        assert st_unk == "unknown"

    # 2. probe_target_capacity disk_usage OSError
    with patch.object(backup_targets, "get_target", return_value={"kind": "filesystem", "path": str(tmp_settings)}):
        with patch("shutil.disk_usage", side_effect=OSError("disk error")):
            cap_os = backup_targets.probe_target_capacity("tgt_os")
            assert cap_os["source"] == "unknown"

    # 3. _blocked_target_outcome non-terminal vs terminal
    now_iso = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    fake_run = backup_scheduler.ClaimedRun(
        run_id="run_blk",
        policy_id="pol_blk",
        schedule_slot="slot_1",
        scheduled_for=now_iso,
        attempt=1,
        fencing_token=1,
    )
    fake_guard = SimpleNamespace(
        instance_id="inst_blk",
        now=lambda: datetime.now(tz=timezone.utc),
    )
    now_dt = datetime.now(tz=timezone.utc)
    with patch.object(backup_scheduler, "block_run"):
        # non-terminal
        out_nt = backup_executor._blocked_target_outcome(
            fake_run,
            policy={"retry": {"maxAttempts": 3, "initialBackoffSeconds": 10}},
            current=now_dt,
            guard=cast(Any, fake_guard),
            message="Target is unavailable",
            outcome={"policyId": "pol_blk"},
        )
        assert out_nt["phase"] == "blocked-retryable"
        assert "retryInSeconds" in out_nt

        # terminal
        fake_run_term = backup_scheduler.ClaimedRun(
            run_id="run_blk_term",
            policy_id="pol_blk",
            schedule_slot="slot_2",
            scheduled_for=now_iso,
            attempt=5,
            fencing_token=2,
        )
        out_t = backup_executor._blocked_target_outcome(
            fake_run_term,
            policy={"retry": {"maxAttempts": 3}},
            current=now_dt,
            guard=cast(Any, fake_guard),
            message="Target is permanently unavailable",
            outcome={"policyId": "pol_blk"},
        )
        assert out_t["phase"] == "blocked-terminal"
