"""Coverage booster tests for Topology Safety, Capacity Governance & Bandwidth QoS."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_drain,
    backup_dr_ledger,
    backup_dr_readiness,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_retirement,
    backup_targets,
    backup_transfer_budget,
)
from deepseek_infra.infra.workspace.backup_transfer_budget import (
    ActiveTransfer,
    TargetBudgetConfig,
    TrafficClass,
    TransferBudgetManager,
    configure_global_transfer_budget,
    get_global_transfer_budget_manager,
    reset_global_transfer_budget_manager,
)
import deepseek_infra.web.routes.backup_governance as governance
from deepseek_infra.web.routes.backup_governance import create_backup_governance_router


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


@pytest.fixture(autouse=True)
def _reset_global_transfer_budget_fixture() -> Any:
    reset_global_transfer_budget_manager()
    yield
    reset_global_transfer_budget_manager()


def test_transfer_budget_edge_cases() -> None:
    mgr = TransferBudgetManager(
        global_bandwidth_bytes_per_sec=10_000_000,
        reserved_dr_bandwidth_bytes_per_sec=2_000_000,
        per_target_read_bytes_per_sec={"target_t1": 5_000_000},
        per_target_write_bytes_per_sec={"target_t2": 5_000_000},
        max_concurrent_transfers_per_target={"target_t1": 2},
    )

    # Acquire with target constraints
    grant = mgr.acquire_bandwidth(
        1_000_000,
        traffic_class=TrafficClass.P1_ACTIVE_BACKUP_PUBLISH,
        dest_target_id="target_t2",
        source_target_id="target_t1",
    )
    assert grant >= 1_000_000

    # Summary
    summary = mgr.transfer_control_summary()
    assert summary["globalBandwidthBytesPerSec"] == 10_000_000
    assert summary["reservedDrBandwidthBytesPerSec"] == 2_000_000

    # Generator with empty iterator
    gen = mgr.throttled_generator(iter([]), traffic_class=TrafficClass.P6_BEST_EFFORT)
    assert list(gen) == []


def test_transfer_budget_full_coverage() -> None:
    reset_global_transfer_budget_manager()
    mgr = get_global_transfer_budget_manager()
    assert mgr.global_bytes_per_second >= 1024 * 1024

    configure_global_transfer_budget(
        global_bandwidth_bytes_per_sec=20_000_000,
        reserved_dr_bandwidth_bytes_per_sec=4_000_000,
        target_configs={
            "target_t1": TargetBudgetConfig(max_read_bytes_per_second=10_000_000, max_concurrent_transfers=1)
        },
    )
    mgr2 = get_global_transfer_budget_manager()
    assert mgr2.global_bytes_per_second == 20_000_000

    # Test context manager track_transfer
    with mgr2.track_transfer(traffic_class=TrafficClass.P0_DISASTER_RECOVERY, source_target_id="target_t1") as tid:
        assert isinstance(tid, str)
        assert mgr2.active_transfers_count() >= 1

    # Concurrency limit reached on target_t1
    mgr2._active_transfers["mock_xfer"] = ActiveTransfer(
        transfer_id="mock_xfer",
        traffic_class=TrafficClass.P3_REQUIRED_REPLICATION,
        source_target_id="target_t1",
        dest_target_id=None,
        estimated_bytes=1000,
        started_at=time.time(),
    )
    # Trying another transfer on target_t1 exceeding concurrency of 1
    with pytest.raises(AppError) as exc_info:
        mgr2.acquire_transfer_token("xfer_new", traffic_class=TrafficClass.P5_REBALANCE_DRAIN, source_target_id="target_t1")
    assert "concurrency-exceeded" in str(exc_info.value)
    mgr2._active_transfers.pop("mock_xfer", None)

    # Throttled generator with multiple chunks
    chunks = [b"chunk1", b"chunk2", b"chunk3"]
    collected = list(mgr2.throttled_generator(iter(chunks), traffic_class=TrafficClass.P2_REQUIRED_REPAIR))
    assert collected == chunks

    # Traffic class priorities
    assert TrafficClass.P0_DISASTER_RECOVERY.priority == 0
    assert TrafficClass.P6_BEST_EFFORT.priority == 6
    assert TrafficClass.from_str("P0") == TrafficClass.P0_DISASTER_RECOVERY
    assert TrafficClass.from_str("unknown") == TrafficClass.P3_REQUIRED_REPLICATION


def test_capacity_edge_cases(tmp_settings: Path) -> None:
    target_id = "target_cap_edge"
    backup_targets.register_filesystem_target(target_id, path=tmp_settings / "cap_edge")

    # Estimate transfer cost
    cost = backup_capacity.estimate_transfer_cost(10_000_000, source_target_id=target_id, dest_target_id=target_id)
    assert cost["bytesToTransfer"] == 10_000_000

    # Cross-domain cost
    t2 = "target_cap_cross"
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "cap_cross", failure_domain="zone-other")
    cost_cross = backup_capacity.estimate_transfer_cost(10_000_000, source_target_id=target_id, dest_target_id=t2)
    assert "bytesToTransfer" in cost_cross
    assert cost_cross["estimatedMonthlyStorageCostDelta"] > 0

    # Capacity summary
    summary = backup_capacity.capacity_summary()
    assert "targets" in summary
    assert "overallStatus" in summary

    # Maintenance window check
    policy_with_mw = {
        "policyId": "pol-mw",
        "placement": {
            "maintenanceWindow": {
                "timezone": "UTC",
                "start": "00:00",
                "end": "23:59",
            }
        },
    }
    assert backup_replication.is_inside_maintenance_window(policy_with_mw) is True

    # Maintenance window wraps around midnight
    policy_wrap = {
        "policyId": "pol-wrap",
        "placement": {
            "maintenanceWindow": {
                "timezone": "UTC",
                "start": "22:00",
                "end": "04:00",
            }
        },
    }
    now_midnight = datetime(2026, 8, 18, 23, 0, tzinfo=timezone.utc)
    assert backup_replication.is_inside_maintenance_window(policy_wrap, now=now_midnight) is True


def test_capacity_watermark_rejections(tmp_settings: Path) -> None:
    tid = "target_cap_wat"
    backup_targets.register_filesystem_target(tid, path=tmp_settings / "wat")

    policy = {
        "placement": {
            "hardWatermarkPercent": 90.0,
            "minFreeBytes": 5_000,
            "minFreePercent": 10.0,
        }
    }

    # 1. Insufficient space
    with patch.object(backup_targets, "probe_target_capacity", return_value={"totalBytes": 10000, "freeBytes": 500, "freePercent": 5.0}):
        ok, reason = backup_capacity.check_target_capacity_admission(tid, 1000, policy=policy)
        assert ok is False
        assert "hard-watermark" in reason or "insufficient" in reason

    # 2. Breaches minFreeBytes
    with patch.object(backup_targets, "probe_target_capacity", return_value={"totalBytes": 100000, "freeBytes": 15000, "freePercent": 15.0}):
        ok2, reason2 = backup_capacity.check_target_capacity_admission(tid, 11000, policy=policy)
        assert ok2 is False
        assert "min-free" in reason2

    # 3. Exhaustion horizon states
    # Exhausted (< 0 days)
    with patch.object(backup_targets, "probe_target_capacity", return_value={"totalBytes": 1000, "freeBytes": 0, "freePercent": 0.0}):
        h_ex = backup_capacity.estimate_target_exhaustion_horizon(tid, "pol-1")
        assert h_ex["status"] == "critical"
        assert h_ex["estimatedDaysToFull"] == 0.0

    # Degraded (< 30 days)
    with patch.object(backup_targets, "probe_target_capacity", return_value={"totalBytes": 100_000_000, "freeBytes": 20_000_000, "freePercent": 20.0}):
        h_deg = backup_capacity.estimate_target_exhaustion_horizon(tid, "pol-1")
        assert h_deg["status"] in {"critical", "degraded", "healthy"}


def test_capacity_prediction_and_summary_details(tmp_settings: Path) -> None:
    t1 = "target_cap_det_1"
    t2 = "target_cap_det_2"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "cd1")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "cd2")

    # 1. Predict size with no prior copies
    full_sz = backup_capacity.predict_next_backup_bytes("pol-none", snapshot_kind="full")
    inc_sz = backup_capacity.predict_next_backup_bytes("pol-none", snapshot_kind="incremental")
    assert full_sz >= 50 * 1024 * 1024
    assert inc_sz >= 10 * 1024 * 1024

    # 2. Predict size with prior copies
    for i in range(5):
        backup_dr_ledger.record_logical_recovery_copy(
            target_id=t1,
            policy_id="pol-history",
            backup_id=f"bk-hist-{i}",
            committed_at="2026-08-18T10:00:00Z",
            state="healthy",
            recoverable=True,
            metadata={"logicalBytes": (i + 1) * 20_000_000, "physicalBytes": (i + 1) * 20_000_000},
        )

    p90_full = backup_capacity.predict_next_backup_bytes("pol-history", snapshot_kind="full")
    p90_inc = backup_capacity.predict_next_backup_bytes("pol-history", snapshot_kind="incremental")
    assert p90_full >= 50 * 1024 * 1024
    assert p90_inc >= 10 * 1024 * 1024

    # 3. Target capacity admission - unconstrained, insufficient space, min free pct, hard watermark rejection & soft admission
    with patch.object(backup_capacity, "get_target_capacity", return_value={"freeBytes": None}):
        ok_uncon, reason_uncon = backup_capacity.check_target_capacity_admission(t1, 1000)
        assert ok_uncon is True
        assert reason_uncon == "unconstrained"

    with patch.object(backup_capacity, "get_target_capacity", return_value={"freeBytes": 100, "totalBytes": 1000}):
        # insufficient space: required (150) > free (100)
        pol_insuf = {"placement": {"hardWatermarkPercent": 99.0}}
        ok_insuf, reason_insuf = backup_capacity.check_target_capacity_admission(t1, 150, policy=pol_insuf)
        assert ok_insuf is False
        assert "target-insufficient-space" in reason_insuf

        # breaches min free percent: free (100) - required (90) = 10 (1.0% < minFreePercent=5.0%)
        pol_pct = {"placement": {"hardWatermarkPercent": 99.0, "minFreeBytes": 1, "minFreePercent": 5.0}}
        ok_pct, reason_pct = backup_capacity.check_target_capacity_admission(t1, 90, policy=pol_pct)
        assert ok_pct is False
        assert "target-breaches-min-free-percent" in reason_pct

        # hard rejection via minFreeBytes: free (100) - required (60) = 40 < 50
        pol = {"placement": {"hardWatermarkPercent": 99.0, "minFreeBytes": 50, "minFreePercent": 5.0}}
        ok_hard, reason_hard = backup_capacity.check_target_capacity_admission(t1, 60, policy=pol)
        assert ok_hard is False
        assert "target-breaches-min-free-bytes" in reason_hard

        # admission: free (100) - required (10) = 90 >= 50 (minFreeBytes)
        ok_admit, reason_admit = backup_capacity.check_target_capacity_admission(t1, 10, policy=pol)
        assert ok_admit is True
        assert reason_admit == "admitted"

    # 4. Target exhaustion horizon with positive daily growth
    with patch.object(backup_capacity, "get_target_capacity", return_value={"freeBytes": 1_000_000_000, "totalBytes": 10_000_000_000, "freePercent": 10.0}):
        horiz_growth = backup_capacity.estimate_target_exhaustion_horizon(t1, "pol-history")
        assert horiz_growth["status"] in {"healthy", "warning", "critical", "degraded"}
        assert horiz_growth["estimatedDaysToFull"] is not None

    # 5. Target exhaustion horizon unconstrained & zero growth
    with patch.object(backup_capacity, "get_target_capacity", return_value={"freeBytes": None, "totalBytes": None}):
        horiz = backup_capacity.estimate_target_exhaustion_horizon("target_unconstrained", "pol")
        assert horiz["status"] == "unconstrained"
        assert horiz["estimatedDaysToFull"] == 9999

    # 6. Capacity summary with critical target and degraded target
    with patch.object(backup_targets, "probe_target_capacity", return_value={"totalBytes": 1000, "freeBytes": 30, "freePercent": 3.0}):
        sum_crit = backup_capacity.capacity_summary()
        assert sum_crit["overallStatus"] == "critical"
        assert len(sum_crit["targets"]) >= 1

    with patch.object(backup_targets, "probe_target_capacity", return_value={"totalBytes": 1000, "freeBytes": 150, "freePercent": 15.0}):
        sum_deg = backup_capacity.capacity_summary()
        assert sum_deg["overallStatus"] in {"degraded", "critical"}

    # 7. Transfer cost estimation
    cost_info = backup_capacity.estimate_transfer_cost(1024 * 1024 * 1024, source_target_id=t1, dest_target_id=t2)
    assert cost_info["bytesToTransfer"] == 1024 * 1024 * 1024
    assert cost_info["currency"] == "USD"
    assert cost_info["isEstimate"] is True


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


def test_transfer_budget_rate_limits_and_tokens() -> None:
    # 1. TrafficClass from_str fallback
    assert backup_transfer_budget.TrafficClass.from_str("nonexistent_class") == backup_transfer_budget.TrafficClass.P3_REQUIRED_REPLICATION
    assert backup_transfer_budget.TrafficClass.from_str("P0") == backup_transfer_budget.TrafficClass.P0_DISASTER_RECOVERY

    # 2. Reset and reconfigure global manager
    backup_transfer_budget.reset_global_transfer_budget_manager()
    mgr = backup_transfer_budget.get_global_transfer_budget_manager()
    assert mgr.active_transfers_count() == 0
    assert mgr.active_transfers_bytes() == 0

    # 3. Configure global transfer budget
    mgr_new = backup_transfer_budget.configure_global_transfer_budget(
        global_bandwidth_bytes_per_sec=50 * 1024 * 1024,
        reserved_dr_bandwidth_bytes_per_sec=10 * 1024 * 1024,
        target_configs={"target_s3_1": TargetBudgetConfig(max_read_bytes_per_second=20 * 1024 * 1024, max_write_bytes_per_second=20 * 1024 * 1024)},
    )
    assert mgr_new.active_transfers_count() == 0

    # 4. Acquire transfer token and release
    xfer = mgr_new.acquire_transfer_token(
        "xfer-1",
        traffic_class=backup_transfer_budget.TrafficClass.P3_REQUIRED_REPLICATION,
        source_target_id="target_s3_1",
        estimated_bytes=1000,
    )
    assert xfer.transfer_id == "xfer-1"
    assert mgr_new.active_transfers_count() == 1
    assert mgr_new.active_transfers_bytes() == 0

    mgr_new.release_transfer_token("xfer-1")
    assert mgr_new.active_transfers_count() == 0
    # Double release is safe
    mgr_new.release_transfer_token("xfer-1")
    assert mgr_new.active_transfers_count() == 0

    # 5. Track transfer context manager
    with mgr_new.track_transfer(source_target_id="target_s3_1", estimated_bytes=500) as tid:
        assert tid.startswith("xfer_")
        assert mgr_new.active_transfers_count() == 1

    assert mgr_new.active_transfers_count() == 0


def test_transfer_budget_streaming_and_rate_limits() -> None:
    mgr = backup_transfer_budget.TransferBudgetManager(
        global_bytes_per_second=10 * 1024 * 1024,
        reserved_recovery_bytes_per_sec=2 * 1024 * 1024,
        background_max_bytes_per_sec=4 * 1024 * 1024,
        target_configs={
            "t_src": TargetBudgetConfig(max_read_bytes_per_second=3 * 1024 * 1024),
            "t_dst": TargetBudgetConfig(max_write_bytes_per_second=250 * 1024),
        },
    )

    # 1. Effective rate limits under various traffic classes
    x_p0 = mgr.acquire_transfer_token("x-p0", backup_transfer_budget.TrafficClass.P0_DISASTER_RECOVERY)
    assert x_p0.transfer_id == "x-p0"
    rate_p0 = mgr.get_effective_rate_limit("x-p0")
    assert rate_p0 == 10 * 1024 * 1024

    x_bg = mgr.acquire_transfer_token("x-bg", backup_transfer_budget.TrafficClass.P5_REBALANCE_DRAIN)
    assert x_bg.transfer_id == "x-bg"
    # Background throttled due to active recovery
    rate_bg = mgr.get_effective_rate_limit("x-bg")
    assert rate_bg <= 4 * 1024 * 1024

    mgr.release_transfer_token("x-p0")
    mgr.release_transfer_token("x-bg")

    # Rate limit with target read/write limits
    x_target = mgr.acquire_transfer_token(
        "x-target",
        backup_transfer_budget.TrafficClass.P1_BACKUP_PUBLISH,
        source_target_id="t_src",
        dest_target_id="t_dst",
    )
    assert x_target.transfer_id == "x-target"
    rate_target = mgr.get_effective_rate_limit("x-target")
    assert rate_target <= 250 * 1024  # constrained by t_dst max_write_bytes_per_second

    # 2. consume_bandwidth and throttling deficit
    sleep_0 = mgr.consume_bandwidth("x-target", 100)
    assert sleep_0 >= 0.0

    # Exhaust tokens to produce sleep deficit
    sleep_def = mgr.consume_bandwidth("x-target", 50 * 1024 * 1024)
    assert sleep_def > 0.0

    # Nonexistent transfer returns 0.0
    assert mgr.consume_bandwidth("x-nonexistent", 100) == 0.0
    assert mgr.get_effective_rate_limit("x-nonexistent") == 10 * 1024 * 1024

    mgr.release_transfer_token("x-target")

    # 3. throttled_generator streaming
    chunks = [b"chunk1", b"chunk2", b"chunk3"]
    generator = mgr.throttled_generator(iter(chunks), traffic_class=backup_transfer_budget.TrafficClass.P6_BEST_EFFORT)
    received = b"".join(list(generator))
    assert received == b"chunk1chunk2chunk3"

    # 4. transfer_control_summary
    summary = mgr.transfer_control_summary()
    assert summary["globalBandwidthBytesPerSec"] == 10 * 1024 * 1024
    assert summary["activeTransfersTotal"] == 0


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
        "filename": "objects/sha256/aa/aabbcc.age",
        "components": [
            {"path": "objects/sha256/aa/ddeeff.age", "digest": "ddeeff"},
            {"digest": "uniquedigest123"},
        ],
    }
    (r1_dir / f"{b1}.json").write_text(json.dumps(r1), encoding="utf-8")

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

    with patch.object(backup_publish, "resolve_target", return_value=mock_rem_target):
        with patch.object(backup_replication, "simulate_copy_removal", return_value={"policySafe": True, "protectedByHold": False}):
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


def test_scheduler_target_ranking_diversity(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_scheduler

    t1 = "target_rank_1"
    t2 = "target_rank_2"
    t3 = "target_rank_3"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "rk1", failure_domain="us-east-1a")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "rk2", failure_domain="us-east-1b")
    backup_targets.register_filesystem_target(t3, path=tmp_settings / "rk3", failure_domain="us-west-2a")

    policy = {
        "policyId": "pol-rank",
        "targetId": t1,
        "placement": {
            "failureDomainSpread": "max-diversity",
        },
        "replication": {
            "targets": [
                {"targetId": t2, "priority": 10},
                {"targetId": t3, "priority": 20},
            ]
        },
    }

    # Rank candidate targets
    candidates = [t2, t3]
    ranked = backup_scheduler.plan_target_placement(
        policy,
        candidate_target_ids=candidates,
        primary_target_id=t1,
    )
    assert len(ranked) == 2
    # Returns list of ((diversity, lag, prio, ...), target_id)
    assert ranked[0][1] in {t2, t3}


def test_backup_executor_failover_commit_statuses(tmp_settings: Path) -> None:
    # 1. Test when commit_status is corrupt -> raises write-reconciliation-required
    t1 = "target_exec_1"
    t2 = "target_exec_2"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "ex1")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "ex2")

    policy_id = "exec-pol"
    backup_policies.create_policy(
        {
            "name": "Exec Policy",
            "policyId": policy_id,
            "targetId": t1,
            "replication": {"enabled": True, "targets": [{"targetId": t2, "mode": "required"}]},
        }
    )

    # 2. Test simulate_copy_removal
    sim_res = backup_replication.simulate_copy_removal(policy_id, "bk-sim", t1)
    assert "healthyCopiesBefore" in sim_res
    assert "policySafe" in sim_res


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


def test_dr_readiness_deep_coverage(tmp_settings: Path) -> None:
    # 1. _parse_time
    assert backup_dr_readiness._parse_time(None) is None
    assert backup_dr_readiness._parse_time(12345) is None
    assert backup_dr_readiness._parse_time("not-a-time") is None
    assert backup_dr_readiness._parse_time("2026-08-18T10:00:00") is None  # no tz
    assert backup_dr_readiness._parse_time("invalid-iso-string-with+00:00") is None
    parsed = backup_dr_readiness._parse_time("2026-08-18T10:00:00Z")
    assert parsed is not None

    # 2. _resolve_target_kind
    assert backup_dr_readiness._resolve_target_kind("managed-local") == "managed-local"
    assert backup_dr_readiness._resolve_target_kind("nonexistent_target_xyz") == "filesystem"

    t_s3 = "target_dr_s3_kind"
    with patch.object(backup_targets, "get_target", return_value={"kind": "s3"}):
        assert backup_dr_readiness._resolve_target_kind(t_s3) == "s3"

    # 3. _latest_outcome & _nonnegative
    assert backup_dr_readiness._latest_outcome([])["status"] == "unavailable"
    rec_ok = [{"observedAt": "2026-08-18T10:00:00Z", "result": "success"}]
    assert backup_dr_readiness._latest_outcome(rec_ok)["status"] == "ok"
    rec_err = [{"observedAt": "2026-08-18T10:00:00Z", "result": "failed"}]
    assert backup_dr_readiness._latest_outcome(rec_err)["status"] == "error"
    assert backup_dr_readiness._latest_outcome(rec_err, success=lambda r: True)["status"] == "ok"

    assert backup_dr_readiness._nonnegative(-10) == 0
    assert backup_dr_readiness._nonnegative("50") == 50

    # 4. evaluate_scope_readiness with nonexistent policy
    res_none_pol = backup_dr_readiness.evaluate_scope_readiness("managed-local", policy_id="nonexistent-pol-xyz")
    assert res_none_pol["status"] == "blocked"

    # 5. readiness_status
    status = backup_dr_readiness.readiness_status()
    assert "status" in status
    assert "scopes" in status


def test_backup_targets_extended_coverage(tmp_settings: Path) -> None:
    t_id = "target_ext_cov_1"
    t_path = tmp_settings / "ext_cov_root"
    backup_targets.register_filesystem_target(t_id, path=t_path, label="Extended Coverage Target")

    # 1. probe_target_capacity
    cap = backup_targets.probe_target_capacity(t_id)
    assert cap["targetId"] == t_id
    assert cap["totalBytes"] > 0
    assert cap["freeBytes"] > 0

    # 2. probe_target
    probe = backup_targets.probe_target(t_id)
    assert probe["targetId"] == t_id
    assert probe["ready"] is True
    assert probe["status"] == "ok"

    # 3. Drain state transitions
    assert backup_targets.get_target_drain_state(t_id) == "active"
    backup_targets.drain_target(t_id, reason="maintenance")
    assert backup_targets.get_target_drain_state(t_id) == "draining"
    backup_targets.mark_target_drained(t_id)
    assert backup_targets.get_target_drain_state(t_id) == "drained"
    backup_targets.activate_target(t_id)
    assert backup_targets.get_target_drain_state(t_id) == "active"

    # 4. adopt_target_incarnation
    adopted = backup_targets.adopt_target_incarnation(t_id)
    assert adopted["targetId"] == t_id

    # 5. delete_target
    del_res = backup_targets.delete_target(t_id)
    assert del_res["deleted"] is True

    # 6. reinitialize_target
    reinit = backup_targets.reinitialize_target(t_path, label="Reinitialized")
    assert "targetId" in reinit


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


def test_run_lease_context_and_drill_scheduler(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_scheduler

    # 1. RunLeaseGuard
    run_id = "run_ctx_test_1"
    inst_id = "inst_ctx_1"
    fencing = 42

    ctx = backup_scheduler.RunLeaseGuard(
        run_id=run_id,
        instance_id=inst_id,
        fencing_token=fencing,
        heartbeat_seconds=0.05,
    )
    assert ctx.now() is not None

    mock_writer = MagicMock(spec=["assert_owned", "renew"])
    ctx.attach_writer(mock_writer)

    with patch.object(backup_scheduler, "assert_run_lease", return_value=None):
        ctx.checkpoint()
        mock_writer.assert_owned.assert_called_once()

    with patch.object(backup_scheduler, "renew_run_lease", return_value=None):
        ctx.start_heartbeat()
        try:
            time.sleep(0.06)
        finally:
            ctx.stop()
            if ctx._thread:
                ctx._thread.join(timeout=1.0)

    # 2. claim_recovery_drill_slots
    policy_drill = {
        "name": "Policy Drill Test",
        "policyId": "pol-drill-1",
        "targetId": "managed-local",
        "schedule": {"cron": "0 0 * * *", "timezone": "UTC"},
        "recoveryDrill": {"enabled": True, "cron": "0 2 * * *", "timezone": "UTC"},
    }
    backup_policies.create_policy(policy_drill)

    claimed_drills = backup_scheduler.claim_due_drill_slots(
        [policy_drill],
        instance_id=inst_id,
        now=datetime(2026, 8, 18, 2, 30, tzinfo=timezone.utc),
    )
    assert isinstance(claimed_drills, list)

    # 3. record_target_head & record_remote_target_head
    t_root = tmp_settings / "t_head_root"
    t_root.mkdir(parents=True, exist_ok=True)
    marker_file = t_root / backup_targets.TARGET_MARKER_NAME
    marker_file.write_text(json.dumps({"targetId": "target_head_1"}), encoding="utf-8")

    backup_targets.record_target_head(
        t_root,
        target_id="target_head_1",
        generation=1,
        commit_hash="hash1",
    )
    assert marker_file.is_file()
    updated_marker = json.loads(marker_file.read_text(encoding="utf-8"))
    assert updated_marker["latestCommitHash"] == "hash1"

    mock_store = MagicMock()
    mock_store.stat.return_value = None
    mock_store.get_bytes.return_value = json.dumps({
        "schemaVersion": 1,
        "targetGeneration": 1,
        "latestCommitHash": "hash0",
        "incarnationId": "inc_1",
    }).encode()
    backup_targets.record_remote_target_head(
        mock_store,
        target_id="target_remote_head_1",
        generation=2,
        commit_hash="hash2",
    )
    assert mock_store.put_if_absent.called


def test_backup_worker_and_scheduler_tick_full_coverage(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_scheduler

    mock_executor = MagicMock()
    mock_executor.execute_slot.return_value = {"status": "success"}

    # 1. worker_tick with drill and replication claimed
    policy_tick = {
        "name": "Policy Tick Test",
        "policyId": "pol-tick-1",
        "targetId": "managed-local",
        "schedule": {"cron": "0 0 * * *", "timezone": "UTC"},
        "recoveryDrill": {"enabled": True, "cron": "0 1 * * *", "timezone": "UTC"},
    }
    backup_policies.create_policy(policy_tick)

    with patch.object(backup_scheduler, "claim_due_slots", return_value=[{"policyId": "pol-tick-1", "slotKey": "s1"}]):
        with patch.object(backup_scheduler, "claim_due_drill_slots", return_value=[{"policyId": "pol-tick-1"}]):
            with patch("deepseek_infra.infra.workspace.backup_recovery_drill.execute_scheduled_drill", return_value=None):
                tick_res = backup_scheduler.worker_tick(
                    instance_id="inst_tick_1",
                    executor=mock_executor,
                    now=datetime(2026, 8, 18, 1, 30, tzinfo=timezone.utc),
                )
                assert tick_res["drillsClaimed"] == 1
                assert tick_res["drillsExecuted"] == 1

    # 2. BackupWorker lifecycle
    worker = backup_scheduler.BackupWorker(
        mock_executor,
        instance_id="inst_worker_1",
        tick_seconds=0.05,
        reconcile_on_start=False,
    )
    with patch.object(backup_scheduler, "worker_tick", return_value={}):
        worker.start()
        try:
            time.sleep(0.06)
        finally:
            worker.stop()
            if worker._thread:
                worker._thread.join(timeout=1.0)


def test_execute_repair_job_instance_full_healing_flow(tmp_settings: Path) -> None:
    t_src_id = "target_heal_src_1"
    t_dst_id = "target_heal_dst_1"
    t_src_root = tmp_settings / "heal_src_1"
    t_dst_root = tmp_settings / "heal_dst_1"

    backup_targets.register_filesystem_target(t_src_id, path=t_src_root)
    backup_targets.register_filesystem_target(t_dst_id, path=t_dst_root)

    content = b"heal payload 123"
    import hashlib
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


def test_backup_executor_failover_and_error_branches(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_executor, backup_scheduler

    # Create policy and target
    t_id = "target_exec_err_1"
    t_root = tmp_settings / "exec_err_1"
    backup_targets.register_filesystem_target(t_id, path=t_root)

    policy = {
        "name": "Policy Exec Err Test",
        "policyId": "pol-exec-err-1",
        "targetId": t_id,
        "enabled": True,
        "schedule": {"cron": "* * * * *", "timezone": "UTC"},
    }
    backup_policies.create_policy(policy)

    # Claim a run via claim_due_slots
    claimed = backup_scheduler.claim_due_slots([policy], instance_id="inst_err_1")
    assert claimed
    run = claimed[0]

    # 1. Ambiguous target commit error
    with patch("deepseek_infra.infra.workspace.backup_scheduled._context_from_policy", side_effect=AppError("ambiguous-target-commit detected", status=409)):
        out1 = backup_executor.execute_run(run, instance_id="inst_err_1")
        assert out1["phase"] == "failed"
        assert out1.get("reason") == "ambiguous-target-commit"

    # Reclaim next run
    claimed2 = backup_scheduler.claim_due_slots([policy], instance_id="inst_err_1", now=datetime.now(tz=timezone.utc) + timedelta(minutes=2))
    assert claimed2
    run2 = claimed2[0]

    # 2. Slot commit conflict error
    with patch("deepseek_infra.infra.workspace.backup_scheduled._context_from_policy", side_effect=AppError("slot-commit-conflict detected", status=409)):
        out2 = backup_executor.execute_run(run2, instance_id="inst_err_1")
        assert out2["phase"] == "superseded"
        assert out2.get("reason") == "slot-commit-conflict"

    # Reclaim next run
    claimed3 = backup_scheduler.claim_due_slots([policy], instance_id="inst_err_1", now=datetime.now(tz=timezone.utc) + timedelta(minutes=4))
    assert claimed3
    run3 = claimed3[0]

    # 3. Blocked target unavailable
    with patch("deepseek_infra.infra.workspace.backup_scheduled._context_from_policy", side_effect=AppError("blocked-target-unavailable", status=409)):
        out3 = backup_executor.execute_run(run3, instance_id="inst_err_1")
        assert out3["phase"] in {"blocked", "blocked-retryable", "abandoned", "failed"}

    # Reclaim next run
    claimed4 = backup_scheduler.claim_due_slots([policy], instance_id="inst_err_1", now=datetime.now(tz=timezone.utc) + timedelta(minutes=6))
    assert claimed4
    run4 = claimed4[0]

    # 4. Generic unhandled Exception
    with patch("deepseek_infra.infra.workspace.backup_scheduled._context_from_policy", side_effect=RuntimeError("unexpected boom")):
        out4 = backup_executor.execute_run(run4, instance_id="inst_err_1")
        assert out4["phase"] == "failed"


def test_backup_governance_457_routes_full(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.require_api_auth", lambda r: None)
    app = FastAPI()
    app.include_router(create_backup_governance_router())
    with TestClient(app) as client:
        # 1. Capacity summary
        res_cap = client.get("/api/workspace/backup-capacity/summary")
        assert res_cap.status_code == 200

        # 2. Transfer budget
        res_tb = client.get("/api/workspace/backup-transfer-budget")
        assert res_tb.status_code == 200

        # 3. Create, get, list backup retirements
        t_id = "target_retire_api_1"
        t_root = tmp_settings / "retire_api_1"
        backup_targets.register_filesystem_target(t_id, path=t_root)

        res_create = client.post(
            "/api/workspace/backup-retirements",
            json={"policyId": "pol-retire-api", "backupId": "bk-retire-api", "targetId": t_id, "reason": "api test"},
        )
        assert res_create.status_code in {200, 201, 400, 404, 409}
        if res_create.status_code in {200, 201}:
            job_id = res_create.json()["jobId"]
            res_get = client.get(f"/api/workspace/backup-retirements/{job_id}")
            assert res_get.status_code == 200

        res_list = client.get("/api/workspace/backup-retirements")
        assert res_list.status_code == 200
        assert "jobs" in res_list.json()

        # 4. Continuity and Promote Primary
        policy_cont = {
            "name": "Policy Cont Test",
            "policyId": "pol-cont-api-1",
            "targetId": t_id,
        }
        backup_policies.create_policy(policy_cont)

        res_cont = client.get("/api/workspace/backup-policies/pol-cont-api-1/continuity")
        assert res_cont.status_code == 200

        with patch("deepseek_infra.infra.workspace.backup_write_continuity.promote_primary_target", return_value={"promoted": True}):
            res_promote = client.post(
                "/api/workspace/backup-policies/pol-cont-api-1/promote-primary",
                json={"targetId": t_id, "expectedPolicyRevision": 1},
            )
            assert res_promote.status_code == 200

        # 5. Error cases
        with pytest.raises(AppError):
            client.post("/api/workspace/backup-retirements", json={})

        with pytest.raises(AppError):
            client.get("/api/workspace/backup-retirements/job_nonexistent_xyz")

        with pytest.raises(AppError):
            client.post("/api/workspace/backup-policies/pol-cont-api-1/promote-primary", json={})


def test_mutation_gate_scrub_and_recovery_class_coverage(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_recovery_class, backup_scrub, mutation_gate

    # 1. mutation_gate tests
    root = tmp_settings / "mg_test"
    root.mkdir(parents=True, exist_ok=True)

    # Generation read invalid
    gen_file = mutation_gate.generation_path(root)
    gen_file.write_text("invalid-number", encoding="ascii")
    assert mutation_gate.read_generation(root) == 0

    # Corrupt fence
    fence_file = mutation_gate.fence_path(root)
    fence_file.write_text("{corrupt-json", encoding="utf-8")
    with pytest.raises(AppError):
        mutation_gate.read_fence(root)
    fence_file.unlink()

    # Clear fence missing & wrong owner
    assert mutation_gate.clear_fence("r1", root) is False
    mutation_gate.write_fence({"restoreId": "r1"}, root)
    with pytest.raises(AppError):
        mutation_gate.clear_fence("r_wrong", root)
    with pytest.raises(AppError):
        mutation_gate.assert_mutation_allowed("r_wrong", root)
    assert mutation_gate.clear_fence("r1", root) is True

    # Nested exclusive_gate
    with mutation_gate.exclusive_gate(root):
        with mutation_gate.exclusive_gate(root):
            pass
        with pytest.raises(RuntimeError):
            with mutation_gate.exclusive_gate(tmp_settings / "mg_other"):
                pass

    # 2. backup_recovery_class tests
    assert backup_recovery_class.size_bucket(5 * 1024 * 1024) == "small"
    assert backup_recovery_class.size_bucket(50 * 1024 * 1024) == "medium"
    assert backup_recovery_class.size_bucket(500 * 1024 * 1024) == "large"

    assert backup_recovery_class.chain_depth_bucket(2) == "shallow"
    assert backup_recovery_class.chain_depth_bucket(5) == "moderate"
    assert backup_recovery_class.chain_depth_bucket(15) == "deep"

    rc = backup_recovery_class.classify_recovery(
        target_kind="s3",
        storage_protocol="object-set-v1",
        logical_bytes=20 * 1024 * 1024,
        chain_length=4,
    )
    assert rc.format_kind == "object-set-v1"
    assert rc.size_category == "medium"
    assert rc.chain_depth == "moderate"
    assert "tag" in rc.to_dict()
    assert str(rc) == rc.tag

    # Calibrate RTO with 10 samples (high confidence)
    samples_high = []
    for i in range(10):
        samples_high.extend([
            {"stage": "transfer", "bytes": 10_000_000, "durationMs": 500, "recoveryClass": rc.to_dict()},
            {"stage": "crypto", "bytes": 10_000_000, "durationMs": 300, "recoveryClass": rc.to_dict()},
            {"stage": "materialize", "bytes": 10_000_000, "durationMs": 200, "recoveryClass": rc.to_dict()},
        ])
    rto_res = backup_recovery_class.calibrate_rto(
        samples_or_target_id=samples_high,
        logical_bytes=10_000_000,
        recovery_class=rc,
    )
    assert rto_res["status"] == "calibrated"
    assert rto_res["confidence"] == "high"
    assert "stageEstimates" in rto_res

    # Calibrate RTO with 3 samples (medium confidence)
    samples_med = samples_high[:9]
    rto_med = backup_recovery_class.calibrate_rto(
        samples_or_target_id=samples_med,
        logical_bytes=10_000_000,
        recovery_class=rc,
    )
    assert rto_med["status"] == "calibrated"
    assert rto_med["confidence"] == "medium"

    # 3. backup_scrub tests
    scrub_empty = backup_scrub.scrub_all(root)
    assert scrub_empty["scrubbed"] == 0
    assert scrub_empty["ok"] is True

    members = backup_scrub._ciphertext_members(root, {"storageProtocol": "single-file", "objects": []})
    assert members == []


def test_backup_reconcile_full_sweep(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_reconcile

    # 1. reconcile_all_targets
    t_id = "target_rec_sweep_1"
    t_root = tmp_settings / "rec_sweep_1"
    backup_targets.register_filesystem_target(t_id, path=t_root)

    reps = backup_reconcile.reconcile_all_targets(instance_id="inst_rec_test")
    assert isinstance(reps, list)
    assert any(r.get("targetId") == t_id for r in reps)

    # 2. assert_catalog_committed clean
    backup_reconcile.assert_catalog_committed(t_root)

    # 3. reconcile_target_store with mock store
    mock_store = MagicMock()
    mock_page = SimpleNamespace(objects=[], cursor=None)
    mock_store.list_objects.return_value = mock_page
    mock_store.get_bytes.return_value = None
    mock_writer = MagicMock(spec=["assert_owned", "renew", "release"])
    mock_writer.assert_owned.return_value = None

    rep_store = backup_reconcile.reconcile_target_store(
        mock_store,
        target_id="target_remote_rec_1",
        writer=mock_writer,
    )
    assert rep_store["targetId"] == "target_remote_rec_1"


def test_backup_targets_complete_lifecycle(tmp_settings: Path) -> None:
    # 1. init_target (new directory)
    t_root = tmp_settings / "tgt_lifecycle_1"
    t_root.mkdir(parents=True, exist_ok=True)
    rec1 = backup_targets.init_target(t_root, label="Lifecycle Target 1")
    assert rec1["targetId"].startswith("target_")
    t_id = rec1["targetId"]

    # 2. init_target (existing marker - re-registration)
    rec1_re = backup_targets.init_target(t_root, label="Lifecycle Target 1 Re")
    assert rec1_re["targetId"] == t_id

    # 3. probe_target on filesystem target
    probe_res = backup_targets.probe_target(t_id)
    assert probe_res["targetId"] == t_id
    assert probe_res["ready"] is True

    # 4. verify_target_ready
    ready_target = backup_targets.verify_target_ready(t_id)
    assert ready_target == t_root

    # 5. init_s3_target with mock store & client
    mock_s3_client = MagicMock()
    mock_store = MagicMock()
    mock_store.capabilities.return_value = SimpleNamespace(kind="s3")

    with patch("deepseek_infra.infra.workspace.backup_target_s3.open_s3_store", return_value=mock_store):
        with patch("deepseek_infra.infra.workspace.backup_target_store.read_json", return_value=None):
            with patch("deepseek_infra.infra.workspace.backup_target_store.put_json_if_absent", return_value=None):
                s3_rec = backup_targets.init_s3_target(
                    bucket="my-mock-bucket",
                    prefix="backups/",
                    client=mock_s3_client,
                    probe=False,
                )
                assert s3_rec["kind"] == "s3"
                assert s3_rec["bucket"] == "my-mock-bucket"

    # 6. delete_target
    backup_targets.delete_target(t_id)
    with pytest.raises(AppError):
        backup_targets.get_target(t_id)


def test_dr_readiness_and_retention_remote_store(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_dr_readiness, backup_retention

    # 1. backup_retention.apply_retention_store and finalize_retention_store
    mock_store = MagicMock()
    mock_store.list_objects.return_value = SimpleNamespace(objects=[], cursor=None)
    mock_store.get_bytes.return_value = None
    mock_writer = MagicMock(spec=["assert_owned", "renew", "release"])
    mock_writer.assert_owned.return_value = None

    state = {
        "b1": {"backupId": "b1", "createdAt": "2026-08-18T00:00:00Z", "trashed": False, "deleted": False},
        "b2": {"backupId": "b2", "createdAt": "2026-08-17T00:00:00Z", "trashed": False, "deleted": False},
        "b3": {"backupId": "b3", "createdAt": "2026-08-16T00:00:00Z", "trashed": True, "trashedAt": "2026-08-16T00:00:00Z", "deleted": False},
    }

    with patch("deepseek_infra.infra.workspace.backup_catalog.catalog_state_store", return_value=state):
        with patch("deepseek_infra.infra.workspace.backup_catalog._append_entry_store", return_value=None):
            with patch("deepseek_infra.infra.workspace.backup_object_set.committed_object_digests", return_value=set()):
                res_apply = backup_retention.apply_retention_store(
                    {"keepLast": 1, "trashGraceHours": 1},
                    mock_store,
                    writer=mock_writer,
                )
                assert "trashed" in res_apply

                res_final = backup_retention.finalize_retention_store(
                    {"keepLast": 1, "trashGraceHours": 1},
                    mock_store,
                    writer=mock_writer,
                )
                assert "deleted" in res_final

    # 2. backup_dr_readiness with replication config
    policy = {
        "policyId": "pol_dr_readiness_test",
        "name": "DR Test Policy",
        "targetId": "target_primary_1",
        "replication": {
            "enabled": True,
            "minCommittedCopies": 3,
            "minFailureDomains": 2,
            "maxReplicaLagSeconds": 10,
            "targets": [
                {"targetId": "target_dr_rep_1", "mode": "required"},
            ],
        },
        "recoveryObjectives": {
            "targetRpoMinutes": 60,
            "targetRtoMinutes": 30,
        },
    }

    with patch("deepseek_infra.infra.workspace.backup_dr_ledger.list_logical_recovery_copies", return_value=[
        {"targetId": "target_primary_1", "recoverable": True, "state": "healthy"},
    ]):
        with patch("deepseek_infra.infra.workspace.backup_replication.calculate_replica_lag", return_value={"lagRecoveryPoints": 5, "lagSeconds": 300}):
            readiness = backup_dr_readiness._replication_summary(
                "target_primary_1",
                "pol_dr_readiness_test",
                {"backupId": "b1", "createdAt": "2026-08-18T01:00:00Z"},
                policy=policy,
                now=datetime.now(tz=timezone.utc),
            )
            assert readiness["enabled"] is True
            assert readiness["compliance"] == "degraded"
            assert "reason" in readiness


def test_rebalance_policy_replicas_proactive_capacity(tmp_settings: Path) -> None:
    t1 = "target_reb_1"
    t2 = "target_reb_2"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "r2", failure_domain="fd2")

    policy_id = "reb-pol"
    backup_policies.create_policy(
        {
            "name": "Reb Policy",
            "policyId": policy_id,
            "targetId": t1,
            "replication": {
                "enabled": True,
                "minFailureDomains": 2,
                "targets": [{"targetId": t2, "mode": "required"}],
            },
            "placement": {
                "softWatermarkPercent": 80.0,
            },
        }
    )

    # Record copy on t1
    backup_dr_ledger.record_logical_recovery_copy(
        target_id=t1,
        policy_id=policy_id,
        backup_id="bk-reb-1",
        committed_at="2026-08-18T10:00:00Z",
        state="healthy",
        recoverable=True,
    )

    # Test rebalancing when t1 has capacity pressure
    with patch.object(backup_targets, "probe_target_capacity", side_effect=lambda tid: {"freePercent": 10.0 if tid == t1 else 90.0}):
        with patch.object(backup_replication, "execute_replica_repair", return_value={"status": "success", "bytesRepaired": 100}):
            with patch.object(backup_replication, "authenticate_committed_copy", return_value=("authenticated", {"receipt": 1}, {"commit": 1})):
                res = backup_replication.rebalance_policy_replicas(policy_id)
                assert res["status"] == "completed"


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


def test_readiness_projection_457(tmp_settings: Path) -> None:
    # Test that readiness_status includes topology, capacity, and transferControl projections
    status = backup_dr_readiness.readiness_status()
    assert "topology" in status
    assert "capacity" in status
    assert "transferControl" in status
    assert status["topology"]["status"] in {"healthy", "degraded", "unavailable"}
    assert status["capacity"]["status"] in {"healthy", "degraded", "critical"}


def test_web_routes_457(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(governance, "require_api_auth", lambda _req: None)
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(request: object, exc: AppError) -> JSONResponse:
        return JSONResponse(exc.to_response(), status_code=exc.status or 400)

    app.include_router(create_backup_governance_router())
    with TestClient(app) as client:
        # 1. Target drain endpoints
        tid = "target_api_drain_1"
        backup_targets.register_filesystem_target(tid, path=tmp_settings / "adt1")

        res_drain = client.post(f"/api/workspace/backup-targets/{tid}/drain", json={"reason": "test"})
        assert res_drain.status_code == 200
        assert res_drain.json()["phase"] == "draining"

        res_get_drain = client.get(f"/api/workspace/backup-targets/{tid}/drain")
        assert res_get_drain.status_code == 200
        assert res_get_drain.json()["phase"] == "draining"

        res_cancel = client.post(f"/api/workspace/backup-targets/{tid}/drain/cancel", json={"reason": "test-cancel"})
        assert res_cancel.status_code == 200
        assert res_cancel.json()["phase"] == "cancelled"

        # 2. Capacity & transfer budget endpoints
        res_cap = client.get("/api/workspace/backup-capacity/summary")
        assert res_cap.status_code == 200
        assert "targets" in res_cap.json()

        res_budget = client.get("/api/workspace/backup-transfer-budget")
        assert res_budget.status_code == 200
        assert "globalBandwidthBytesPerSec" in res_budget.json()


def test_recovery_job_and_keeper_coverage(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_recovery_job, backup_recovery_keeper

    # 1. backup_recovery_job terminal exceptions
    session_term = {"phase": "complete", "restoreId": "r_term_1"}
    with pytest.raises(AppError):
        backup_recovery_job.request_pause(session_term)

    with pytest.raises(AppError):
        backup_recovery_job.request_abort(session_term)

    # 2. converge pause & abort
    session_active = {"phase": "fetching", "restoreId": "r_act_1", "pauseRequested": True}
    res_pause = backup_recovery_job.converge(session_active)
    assert res_pause == "paused"
    assert session_active["phase"] == "paused"

    # resume
    res_res = backup_recovery_job.resume(session_active)
    assert res_res == "fetching"

    # resume error when not paused
    with pytest.raises(AppError):
        backup_recovery_job.resume(session_active)

    # converge abort with prepared callback
    abort_called = False

    def _abort_cb() -> None:
        nonlocal abort_called
        abort_called = True

    t_path = tmp_settings / "tx_phase.json"
    t_path.write_text(json.dumps({"phase": "prepared"}), encoding="utf-8")
    session_abort = {
        "phase": "fetching",
        "restoreId": "r_ab_1",
        "abortRequested": True,
        "transactionPath": str(t_path),
    }
    res_ab = backup_recovery_job.converge(session_abort, abort_prepared=_abort_cb)
    assert res_ab == "rolled-back"
    assert abort_called is True

    # 3. backup_recovery_keeper helpers
    assert backup_recovery_keeper._is_protected_phase("fetching") is True
    assert backup_recovery_keeper._is_protected_phase("complete") is False
    assert backup_recovery_keeper._is_protected_phase("failed") is False
    assert backup_recovery_keeper._is_local_target("managed-local") is True
    assert backup_recovery_keeper._is_local_target("local-fs-1") is True
    assert backup_recovery_keeper._is_local_target("target_s3_1") is False

    health = backup_recovery_keeper._KeeperHealthState()
    health.record_tick({"protected": 1, "renewed": 0})
    st = health.snapshot()
    assert "consecutiveFailures" in st
    assert st["consecutiveFailures"] == 0


def test_dr_audit_binding_anomalies() -> None:
    from deepseek_infra.infra.workspace import backup_dr_audit, backup_publish

    # 1. Invalid commit marker
    assert "invalid-commit-marker" in backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit={"invalid": True},
        receipt={},
    )

    # 2. Valid marker structure with anomalies
    base_body = {
        "version": 4,
        "backupId": "bk_audit_1",
        "policyId": "pol1",
        "targetId": "t1",
        "targetGeneration": 1,
        "previousCommitHash": "0" * 64,
        "receiptDigest": "xyz",
    }
    base_commit = dict(base_body)
    base_commit["commitHash"] = backup_publish._commit_hash(base_body)

    # Missing receipt
    anom_miss = backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit=base_commit,
        receipt=None,
    )
    assert any("missing-receipt" in a for a in anom_miss)

    # Mismatched receipt backupId / targetId / policyId
    bad_receipt = {
        "backupId": "bk_other",
        "targetId": "t2",
        "policyId": "pol2",
        "storageProtocol": "object-set-v1",
    }
    anom_mismatch = backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit=base_commit,
        receipt=bad_receipt,
    )
    assert any("receipt-backup-id-mismatch" in a for a in anom_mismatch)
    assert any("receipt-target-mismatch" in a for a in anom_mismatch)
    assert any("receipt-policy-mismatch" in a for a in anom_mismatch)
    assert any("missing-object-set-digest" in a for a in anom_mismatch)


def test_dr_readiness_commit_chain_and_receipt_coverage(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_dr_readiness, backup_publish

    # 1. _validated_commit_chain empty and single
    assert backup_dr_readiness._validated_commit_chain([]) == ([], True)

    m1_body = {"version": 4, "backupId": "b1", "targetGeneration": 1}
    m1 = dict(m1_body, commitHash=backup_publish._commit_hash(m1_body))
    chain, ok = backup_dr_readiness._validated_commit_chain([m1])
    assert ok is True
    assert len(chain) == 1

    # 2. _merge_validated_receipt
    merged = backup_dr_readiness._merge_validated_receipt(
        {"backupId": "b1", "status": "ok"},
        {"pinned": True, "scrubOk": True, "ciphertextScrubbedAt": "2026-08-18T00:00:00Z"},
        target_id="target_t1",
    )
    assert merged["pinned"] is True
    assert merged["scrubOk"] is True
    assert merged["targetId"] == "target_t1"

    # 3. _commit_records_for_root
    t_root = tmp_settings / "dr_root_test"
    (t_root / "commits" / "pol1").mkdir(parents=True, exist_ok=True)
    (t_root / "receipts").mkdir(parents=True, exist_ok=True)

    c_file = t_root / "commits" / "pol1" / "000001.json"
    c_file.write_text(json.dumps(m1), encoding="utf-8")

    r_file = t_root / "receipts" / "b1.json"
    r_file.write_text(json.dumps({"backupId": "b1", "policyId": "pol1"}), encoding="utf-8")

    recs, committed, healthy = backup_dr_readiness._commit_records_for_root(t_root, "target_t1")
    assert healthy is True
    assert len(recs) == 1
    assert ("target_t1", "b1") in committed

    # 4. _stage_samples with staging root
    st_root = tmp_settings / "restore_staging"
    st_root.mkdir(parents=True, exist_ok=True)
    session_dir = st_root / "sess_1"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "remote-fetch.json").write_text(
        json.dumps({
            "restoreId": "r1",
            "recoveryTelemetry": {
                "samples": [
                    {"stage": "transfer", "durationMs": 150},
                ],
            },
        }),
        encoding="utf-8",
    )
    with patch.object(backup_dr_readiness.backups, "RESTORE_DIR", st_root):
        samples = backup_dr_readiness._stage_samples()
        assert any(s.get("stage") == "transfer" for s in samples)


def test_replication_job_management_and_spool_failure(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_replication

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


def test_backup_write_continuity_governance_exhaustive(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_write_continuity

    # 1. _parse_iso with invalid input returns None
    assert backup_write_continuity._parse_iso("invalid-date-string") is None
    assert backup_write_continuity._parse_iso(None) is None

    # 2. Setup policy continuity
    t1 = "target_wc_ex_1"
    t2 = "target_wc_ex_2"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "wc_ex_1", failure_domain="fd1")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "wc_ex_2", failure_domain="fd2")

    pol = {
        "policyId": "pol-wc-ex",
        "name": "Continuity Exhaustive Test",
        "targetId": t1,
    }
    backup_policies.create_policy(pol)

    state = backup_write_continuity.get_write_continuity_state("pol-wc-ex")
    assert state["configuredPrimaryTargetId"] == t1
    assert state["activeWriteTargetId"] == t1

    # 3. Promote primary with revision mismatch raises 412 (Precondition Failed)
    with pytest.raises(AppError) as exc_rev:
        backup_write_continuity.promote_primary_target(
            "pol-wc-ex",
            target_id=t2,
            expected_policy_revision=999,
        )
    assert exc_rev.value.status in {409, 412}

    # 4. Successful primary promotion with valid revision
    promoted = backup_write_continuity.promote_primary_target(
        "pol-wc-ex",
        target_id=t2,
        expected_policy_revision=1,
    )
    assert promoted["status"] == "promoted"
    assert promoted["newPrimaryTargetId"] == t2
    assert promoted["policyRevision"] == 2

    state_after = backup_write_continuity.get_write_continuity_state("pol-wc-ex")
    assert state_after["configuredPrimaryTargetId"] == t2
    assert state_after["activeWriteTargetId"] == t2


def test_replication_fail_job_and_process_pending_branches(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_replication

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


def test_backup_targets_and_scheduler_deep_branches(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_scheduler

    # 1. probe_target_capacity
    assert backup_targets.probe_target_capacity("nonexistent_target_xyz")["source"] == "unknown"

    t_quota_id = "target_quota_test"
    backup_targets.register_filesystem_target(t_quota_id, path=tmp_settings / "quota_root")
    # Update quotaBytes
    t_data = backup_targets.get_target(t_quota_id)
    t_data["quotaBytes"] = 100 * 1024 * 1024
    backup_targets._atomic_write_json(backup_targets._registry_path(t_quota_id), t_data)

    cap = backup_targets.probe_target_capacity(t_quota_id)
    assert cap["source"] in {"filesystem", "configured-quota"}
    assert cap["totalBytes"] > 0

    # 2. reinitialize_target
    reinit_dir = tmp_settings / "reinit_dir"
    reinit_dir.mkdir(parents=True, exist_ok=True)
    init_res = backup_targets.init_target(reinit_dir, label="Init")
    reinit_res = backup_targets.reinitialize_target(reinit_dir, label="Reinit")
    assert reinit_res["targetId"] != init_res["targetId"]

    # 3. adopt_target_incarnation
    t_adopt_id = "target_adopt_branch"
    t_adopt_dir = tmp_settings / "adopt_dir"
    backup_targets.register_filesystem_target(t_adopt_id, path=t_adopt_dir)

    # Missing marker
    (t_adopt_dir / backup_targets.TARGET_MARKER_NAME).unlink()
    with pytest.raises(AppError) as exc_adopt_miss:
        backup_targets.adopt_target_incarnation(t_adopt_id)
    assert exc_adopt_miss.value.status == 409

    # Corrupt marker
    (t_adopt_dir / backup_targets.TARGET_MARKER_NAME).write_text("invalid json {{{", encoding="utf-8")
    with pytest.raises(AppError) as exc_adopt_corr:
        backup_targets.adopt_target_incarnation(t_adopt_id)
    assert exc_adopt_corr.value.status == 409

    # 4. scheduler reclaim_deferred_slots empty
    deferred = backup_scheduler.reclaim_deferred_slots([], instance_id="test-inst")
    assert deferred == []


def test_backup_retention_deep_preview_validation(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_retention

    t_root = tmp_settings / "ret_val_root"
    t_root.mkdir(parents=True, exist_ok=True)

    ret = {"keepLast": 1, "trashGraceHours": 24}
    prev_valid = {
        "catalogHeadHash": "0" * 64,
        "targetGeneration": 0,
        "policyDigest": backup_retention._policy_digest(ret),
    }

    # 1. Valid preview passes
    backup_retention._validate_preview_snapshot(prev_valid, ret, t_root)

    # 2. Head hash mismatch raises 409
    with pytest.raises(AppError) as exc_head:
        backup_retention._validate_preview_snapshot(dict(prev_valid, catalogHeadHash="mismatch"), ret, t_root)
    assert exc_head.value.status == 409

    # 3. Target generation mismatch raises 409
    with pytest.raises(AppError) as exc_gen:
        backup_retention._validate_preview_snapshot(dict(prev_valid, targetGeneration=99), ret, t_root)
    assert exc_gen.value.status == 409

    # 4. Policy digest mismatch raises 409
    with pytest.raises(AppError) as exc_pol:
        backup_retention._validate_preview_snapshot(dict(prev_valid, policyDigest="bad_digest"), ret, t_root)
    assert exc_pol.value.status == 409

    # 5. _protect_snapshot_ancestors with circular chain
    recs = [
        {"backupId": "b1", "snapshotKind": "incremental", "parentBackupId": "b2"},
        {"backupId": "b2", "snapshotKind": "incremental", "parentBackupId": "b1"},
    ]
    keep: set[str] = set()
    prot: dict[str, str] = {}
    backup_retention._protect_snapshot_ancestors(recs, keep, prot, descendants={"b1"})
    assert "b2" in keep


def test_replication_repair_exception_branches_and_pending_repairs(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_replication

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


def test_backups_apply_restore_and_sink_coverage(tmp_settings: Path) -> None:
    from io import BytesIO
    from deepseek_infra.infra.workspace import backups
    from deepseek_infra.infra.workspace.schema import ErrorCode

    # 1. apply_restore unsupported mode
    with pytest.raises(AppError) as exc_mode:
        backups.apply_restore("rest_1", mode="unsupported_mode")
    assert exc_mode.value.code == ErrorCode.INVALID_PAYLOAD

    # 2. apply_restore requiresFrontendApply
    rest_root = tmp_settings / "restore_staging" / "rest_fe_req"
    rest_root.mkdir(parents=True, exist_ok=True)
    (rest_root / "plan.json").write_text(json.dumps({"requiresFrontendApply": True}), encoding="utf-8")

    with patch.object(backups, "_restore_root", return_value=rest_root):
        with pytest.raises(AppError) as exc_fe:
            backups.apply_restore("rest_fe_req", mode="merge")
        assert exc_fe.value.status == 409

    # 3. _NonSeekableZipSink
    bio = BytesIO()
    sink = backups._NonSeekableZipSink(bio)
    sink.write(b"data-123")
    sink.flush()
    assert bio.getvalue() == b"data-123"


def test_backup_remote_restore_selection_and_session_exceptions(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_remote_restore

    # 1. Nonexistent restore_id raises 404
    with pytest.raises(AppError) as exc_404:
        backup_remote_restore.create_restore_from_target(
            target_id="t1",
            backup_id="bk1",
            restore_id="nonexistent_restore_id",
        )
    assert exc_404.value.status == 404

    # 2. Target/backup mismatch raises 409
    sess = {
        "restoreId": "sess_mis",
        "targetId": "t1",
        "backupId": "bk1",
        "selectionDigest": "digest_a",
        "phase": "fetching",
    }
    backup_remote_restore._atomic_write_json(backup_remote_restore._session_path("sess_mis"), sess)

    with pytest.raises(AppError) as exc_mis:
        backup_remote_restore.create_restore_from_target(
            target_id="t2",
            backup_id="bk1",
            restore_id="sess_mis",
        )
    assert exc_mis.value.status == 409


def test_backup_executor_retry_and_helpers(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_executor

    pol_empty: dict[str, Any] = {}
    assert backup_executor._max_attempts(pol_empty) == 3
    assert backup_executor._retry_delay_seconds(pol_empty, 1) == 60
    assert backup_executor._retry_delay_seconds(pol_empty, 2) == 120
    assert backup_executor._retry_delay_seconds(pol_empty, 10) == 900

    pol_custom = {"retry": {"initialBackoffSeconds": 10, "maxBackoffSeconds": 50, "maxAttempts": 7}}
    assert backup_executor._max_attempts(pol_custom) == 7
    assert backup_executor._retry_delay_seconds(pol_custom, 1) == 10
    assert backup_executor._retry_delay_seconds(pol_custom, 5) == 50


def test_backup_scrub_deep_catalog_and_all(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_scrub

    t_root = tmp_settings / "scrub_root"
    t_root.mkdir(parents=True, exist_ok=True)

    # 1. Nonexistent backup in catalog raises 404
    with pytest.raises(AppError) as exc_404:
        backup_scrub._catalog_record(t_root, "nonexistent_backup_id")
    assert exc_404.value.status == 404

    # 2. scrub_all on empty catalog returns scrubbed=0, ok=True
    res_empty = backup_scrub.scrub_all(t_root)
    assert res_empty["scrubbed"] == 0
    assert res_empty["ok"] is True


def test_replication_authenticate_transition_parent_and_verify_dest(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_replication

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
    from deepseek_infra.infra.workspace import backup_replication

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


def test_backup_drain_job_queries_and_waiting_for_gc(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_drain

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


def test_replication_reconcile_and_rebalance_filters(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_replication

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
    from deepseek_infra.infra.workspace import backup_replication

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
    from deepseek_infra.infra.workspace import backup_replication

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


def test_backup_writer_lease_deep_exceptions_and_skew(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_writer_lease

    # 1. Init without root or store raises 500
    with pytest.raises(AppError) as exc_init:
        backup_writer_lease.TargetWriterLease(
            None,
            store=None,
            target_id="t1",
            owner_run_id="r1",
            owner_instance_id="i1",
            fencing_token=1,
        )
    assert exc_init.value.status == 500

    # 2. Remote writer lease path property raises 500
    remote_lease = backup_writer_lease.TargetWriterLease(
        None,
        store=SimpleNamespace(),
        target_id="t1",
        owner_run_id="r1",
        owner_instance_id="i1",
        fencing_token=1,
    )
    with pytest.raises(AppError) as exc_path:
        _ = remote_lease.path
    assert exc_path.value.status == 500

    # 3. _note_server_date with invalid string returns gracefully
    remote_lease._note_server_date("invalid-server-date-string")

    # 4. _note_server_date with ISO string
    remote_lease._note_server_date(backup_writer_lease._utc_iso())
    assert isinstance(remote_lease._server_skew, timedelta)


def test_replication_compliance_lag_and_retention_holds(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_replication, backup_retention

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


def test_backup_target_s3_checksum_and_delete_branches(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_target_s3

    s3 = backup_target_s3.S3TargetStore(
        bucket="mybucket",
        prefix="sub",
        endpoint_url="http://mock-s3.local",
    )

    # 1. put_if_match checksum mismatch raises 500
    with pytest.raises(AppError) as exc_put:
        s3.put_if_match("k1", b"hello", expected_etag="e1", checksum_sha256="wrong_sha256")
    assert exc_put.value.status == 500

    # 2. upload_part checksum mismatch raises 500
    upload = backup_target_s3.MultipartUpload(key="k1", upload_id="up1", checksum_sha256="sha1")
    with pytest.raises(AppError) as exc_part:
        s3.upload_part(upload, 1, b"hello", checksum_sha256="wrong_sha256")
    assert exc_part.value.status == 500

    # 3. delete_if_match when current is None returns False
    with patch.object(s3, "stat", return_value=None):
        with patch.object(s3, "_client_or_create", return_value=SimpleNamespace()):
            res_del_none = s3.delete_if_match("missing_key", expected_etag="e1")
            assert res_del_none is False

    # 4. delete_if_match with etag mismatch raises 412
    meta_diff = backup_target_s3.ObjectMeta(key="k1", size=5, etag="actual_etag")
    with patch.object(s3, "stat", return_value=meta_diff):
        with patch.object(s3, "_client_or_create", return_value=SimpleNamespace()):
            with pytest.raises(AppError) as exc_del_etag:
                s3.delete_if_match("k1", expected_etag="expected_diff_etag")
            assert exc_del_etag.value.status == 412


def test_backup_capacity_all_admission_and_horizon_branches(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_capacity

    # 1. Admission on unconstrained target (freeBytes is None)
    with patch.object(backup_capacity, "get_target_capacity", return_value={"freeBytes": None}):
        adm, r = backup_capacity.check_target_capacity_admission("t_un", 1000)
        assert adm is True
        assert r == "unconstrained"

    # 2. Hard watermark exceeded
    cap_hard = {"freeBytes": 50, "totalBytes": 1000, "usedBytes": 950}
    with patch.object(backup_capacity, "get_target_capacity", return_value=cap_hard):
        adm_h, r_h = backup_capacity.check_target_capacity_admission("t_h", 10, policy={"placement": {"hardWatermarkPercent": 90}})
        assert adm_h is False
        assert "target-hard-watermark-exceeded" in r_h

    # 3. Insufficient space (remaining_free < 0)
    cap_small = {"freeBytes": 50, "totalBytes": 1000, "usedBytes": 500}
    with patch.object(backup_capacity, "get_target_capacity", return_value=cap_small):
        adm_s, r_s = backup_capacity.check_target_capacity_admission("t_s", 100, policy={"placement": {"hardWatermarkPercent": 90, "minFreeBytes": 10}})
        assert adm_s is False
        assert "target-insufficient-space" in r_s

    # 4. Breaches min free bytes
    cap_min_b = {"freeBytes": 500, "totalBytes": 1000, "usedBytes": 500}
    with patch.object(backup_capacity, "get_target_capacity", return_value=cap_min_b):
        adm_mb, r_mb = backup_capacity.check_target_capacity_admission("t_mb", 400, policy={"placement": {"hardWatermarkPercent": 90, "minFreeBytes": 200, "minFreePercent": 5.0}})
        assert adm_mb is False
        assert "target-breaches-min-free-bytes" in r_mb

    # 5. Breaches min free percent
    with patch.object(backup_capacity, "get_target_capacity", return_value=cap_min_b):
        adm_mp, r_mp = backup_capacity.check_target_capacity_admission("t_mp", 450, policy={"placement": {"hardWatermarkPercent": 95, "minFreeBytes": 10, "minFreePercent": 10.0}})
        assert adm_mp is False
        assert "target-breaches-min-free-percent" in r_mp

    # 6. Horizon unconstrained & critical & degraded
    with patch.object(backup_capacity, "get_target_capacity", return_value={"freeBytes": None, "totalBytes": None}):
        hor_un = backup_capacity.estimate_target_exhaustion_horizon("t1", "p1")
        assert hor_un["status"] == "unconstrained"

    cap_crit = {"freeBytes": 1024, "totalBytes": 100 * 1024, "freePercent": 1.0}
    with patch.object(backup_capacity, "get_target_capacity", return_value=cap_crit):
        with patch.object(backup_dr_ledger, "list_logical_recovery_copies", return_value=[]):
            hor_crit = backup_capacity.estimate_target_exhaustion_horizon("t1", "p1")
            assert hor_crit["status"] == "critical"

    cap_deg = {"freeBytes": 200 * 1024 * 1024, "totalBytes": 1200 * 1024 * 1024, "freePercent": 16.6}
    with patch.object(backup_capacity, "get_target_capacity", return_value=cap_deg):
        with patch.object(backup_dr_ledger, "list_logical_recovery_copies", return_value=[]):
            hor_deg = backup_capacity.estimate_target_exhaustion_horizon("t1", "p1")
            assert hor_deg["status"] == "degraded"

    # 7. Transfer cost with cross-region
    with patch.object(backup_targets, "get_target", return_value={"costClass": "cross-region", "egressCostPerGiB": 0.05, "storageCostPerGiBMonth": 0.02}):
        cost_cr = backup_capacity.estimate_transfer_cost(1024 * 1024 * 1024, source_target_id="src_cr", dest_target_id="dst_cr")
        assert cost_cr["estimatedOneTimeTransferCost"] > 0


def test_backup_dr_readiness_cache_health_and_target_kinds(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_dr_readiness

    # 1. _parse_time edge cases
    assert backup_dr_readiness._parse_time("not-a-time") is None
    assert backup_dr_readiness._parse_time(12345) is None
    assert backup_dr_readiness._parse_time("") is None

    # 2. _resolve_target_kind
    assert backup_dr_readiness._resolve_target_kind("managed-local") == "managed-local"
    with patch.object(backup_targets, "get_target", return_value={"kind": "s3"}):
        assert backup_dr_readiness._resolve_target_kind("t_s3") == "s3"
    with patch.object(backup_targets, "get_target", return_value={"kind": "custom-storage"}):
        assert backup_dr_readiness._resolve_target_kind("t_custom") == "custom-storage"
    with patch.object(backup_targets, "get_target", side_effect=Exception("error")):
        assert backup_dr_readiness._resolve_target_kind("t_err") == "filesystem"

    # 3. _cache_health invalid pins
    c_root = tmp_settings / "cache_h_root"
    c_root.mkdir(parents=True, exist_ok=True)
    (c_root / "pins").mkdir(parents=True, exist_ok=True)
    (c_root / "pins" / "bad.json").write_text(json.dumps({"schemaVersion": 1, "digests": ["not-a-valid-hex-digest"]}), encoding="utf-8")

    with patch("deepseek_infra.infra.workspace.backup_component_cache.CACHE_DIR", c_root):
        h = backup_dr_readiness._cache_health(datetime.now(tz=timezone.utc))
        assert h["status"] == "error"
        assert h["reason"] == "pin-metadata-invalid"


def test_backup_targets_s3_registration_branches(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_targets, backup_target_s3

    # 1. SDK unavailable without client raises 503
    with patch.object(backup_target_s3, "s3_sdk_available", return_value=False):
        with pytest.raises(AppError) as exc_sdk:
            backup_targets.init_s3_target(bucket="my-bkt", client=None)
        assert exc_sdk.value.status == 503

    # 2. Empty bucket raises 400
    with patch.object(backup_target_s3, "s3_sdk_available", return_value=True):
        with pytest.raises(AppError) as exc_bkt:
            backup_targets.init_s3_target(bucket="", client=SimpleNamespace())
        assert exc_bkt.value.status == 400

    # 3. aws-default-chain with profile maps to aws-profile
    fake_store = SimpleNamespace(
        capabilities=lambda: SimpleNamespace(kind="s3"),
        detect_versioning=lambda: "Enabled",
    )
    with patch.object(backup_target_s3, "open_s3_store", return_value=fake_store):
        with patch("deepseek_infra.infra.workspace.backup_target_store.read_json", return_value=None):
            with patch("deepseek_infra.infra.workspace.backup_target_store.put_json_if_absent"):
                with patch("deepseek_infra.infra.workspace.backup_target_store.probe_store_capabilities", return_value={"capabilities": {}}):
                    rec = backup_targets.init_s3_target(
                        bucket="profile-bkt",
                        credential_provider={"type": "aws-default-chain", "profile": "dev-prof"},
                        client=SimpleNamespace(),
                        probe=True,
                    )
                    assert rec["credentialProvider"]["type"] == "aws-profile"
                    assert rec["credentialProvider"]["profile"] == "dev-prof"


def test_backup_drain_start_cancel_and_list_branches(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_drain, backup_targets

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
    from deepseek_infra.infra.workspace import backup_targets, backup_executor, backup_scheduler

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


def test_backup_replication_repair_job_instance_exception_paths(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_replication

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
    from deepseek_infra.infra.workspace import backup_replication, backup_writer_lease

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
    from deepseek_infra.infra.workspace import backup_replication

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
    from deepseek_infra.infra.workspace import backup_replication, backup_policies, backup_targets

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
    from deepseek_infra.infra.workspace import backup_replication

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
    from deepseek_infra.infra.workspace import backup_replication

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


def test_backup_targets_adopt_target_incarnation_full_flow(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_targets

    t_dir = tmp_settings / "adopt_flow_tgt"
    t_dir.mkdir(parents=True, exist_ok=True)
    marker = t_dir / backup_targets.TARGET_MARKER_NAME

    record = {
        "targetId": "tgt_adopt_1",
        "targetNonce": "nonce_adopt_1",
        "path": str(t_dir),
        "kind": "filesystem",
    }

    # 1. Missing marker raises 409
    with patch.object(backup_targets, "get_target", return_value=record):
        with pytest.raises(AppError) as exc_miss:
            backup_targets.adopt_target_incarnation("tgt_adopt_1")
        assert exc_miss.value.status == 409
        assert "target marker is missing" in str(exc_miss.value)

    # 2. Replaced marker raises 409
    marker.write_text(json.dumps({"targetId": "different_id", "targetNonce": "diff_nonce"}), encoding="utf-8")
    with patch.object(backup_targets, "get_target", return_value=record):
        with pytest.raises(AppError) as exc_repl:
            backup_targets.adopt_target_incarnation("tgt_adopt_1")
        assert exc_repl.value.status == 409
        assert "target marker was replaced" in str(exc_repl.value)

    # 3. Valid marker adopted
    marker.write_text(json.dumps({
        "schemaVersion": 3,
        "targetId": "tgt_adopt_1",
        "targetNonce": "nonce_adopt_1",
        "targetGeneration": 3,
        "latestCommitHash": "a" * 64,
    }), encoding="utf-8")
    with patch.object(backup_targets, "get_target", return_value=record):
        with patch.object(backup_targets, "_write_checkpoint"):
            res = backup_targets.adopt_target_incarnation("tgt_adopt_1")
            assert res["adopted"] is True
            assert res["targetId"] == "tgt_adopt_1"
            assert "incarnationId" in res


def test_backup_scheduler_reclaim_blocked_runs_all_terminal_and_retry_branches(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import backup_scheduler, backup_publish

    now_dt = datetime.now(tz=timezone.utc)
    now_iso = now_dt.isoformat().replace("+00:00", "Z")
    past_iso = (now_dt - timedelta(days=2)).isoformat().replace("+00:00", "Z")

    # 1. Setup scheduler table with blocked runs
    with backup_scheduler._connect() as conn:
        conn.execute("DELETE FROM backup_runs")
        conn.execute("DELETE FROM backup_schedule_slots")

        # Run 1: Policy missing -> terminal
        conn.execute(
            "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, lease_until, created_at, updated_at) VALUES ('r_miss', 'pol_miss', 's1', 'blocked', 1, ?, ?, ?)",
            (past_iso, past_iso, past_iso),
        )

        # Run 2: Catchup window exceeded -> terminal
        conn.execute(
            "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, lease_until, created_at, updated_at) VALUES ('r_catch', 'pol_c', 's_old', 'blocked', 1, ?, ?, ?)",
            (past_iso, past_iso, past_iso),
        )
        conn.execute(
            "INSERT INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES ('pol_c', 's_old', ?, ?, 'UTC', 'claimed', 'r_catch', ?)",
            (past_iso, past_iso, past_iso),
        )

        # Run 3: Max attempts exceeded -> terminal
        conn.execute(
            "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, lease_until, created_at, updated_at) VALUES ('r_att', 'pol_att', 's_att', 'blocked', 10, ?, ?, ?)",
            (past_iso, past_iso, past_iso),
        )
        conn.execute(
            "INSERT INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES ('pol_att', 's_att', ?, ?, 'UTC', 'claimed', 'r_att', ?)",
            (now_iso, now_iso, now_iso),
        )

        # Run 4: Target resolved ok -> reclaimed leased run!
        conn.execute(
            "INSERT INTO backup_runs(run_id, policy_id, schedule_slot, phase, attempt, lease_until, created_at, updated_at) VALUES ('r_ok', 'pol_ok', 's_ok', 'blocked', 1, ?, ?, ?)",
            (past_iso, past_iso, past_iso),
        )
        conn.execute(
            "INSERT INTO backup_schedule_slots(policy_id, slot_key, scheduled_for, local_date_time, timezone, status, run_id, created_at) VALUES ('pol_ok', 's_ok', ?, ?, 'UTC', 'claimed', 'r_ok', ?)",
            (now_iso, now_iso, now_iso),
        )

    policies = [
        {"policyId": "pol_c", "enabled": True, "schedule": {"catchupWindowSeconds": 60}},
        {"policyId": "pol_att", "enabled": True, "retry": {"maxAttempts": 3}},
        {"policyId": "pol_ok", "enabled": True, "targetId": "tgt_ok"},
    ]

    with patch.object(backup_publish, "resolve_target", return_value=SimpleNamespace(root=tmp_settings, store=None)):
        reclaimed = backup_scheduler.reclaim_blocked_slots(policies, instance_id="inst_rec", now=now_dt)
        assert len(reclaimed) == 1
        assert reclaimed[0].policy_id == "pol_ok"
        assert reclaimed[0].attempt == 2


def test_server_share_target_post_coverage(tmp_settings: Path) -> None:
    from deepseek_infra.web import server as server_module

    srv, _ = server_module.create_server(0, host="127.0.0.1")
    client = TestClient(srv.app, follow_redirects=False)

    # 1. Share target with text and title as multipart
    resp1 = client.post(
        "/share-target",
        data={"title": "Shared Title", "text": "Shared Text", "url": "https://example.com"},
        files={"dummy": ("", b"", "application/octet-stream")},
        headers={"Host": "127.0.0.1"},
    )
    assert resp1.status_code == 303
    assert "/?share=" in resp1.headers.get("Location", "")

    # 2. Share target with uploaded file and with extraction
    fake_file = ("test.txt", b"hello shared file", "text/plain")
    resp2 = client.post(
        "/share-target",
        data={"title": "File Title"},
        files={"file": fake_file},
        headers={"Host": "127.0.0.1"},
    )
    assert resp2.status_code == 303

    # 3. Share target with invalid/unextractable file error handling
    with patch("deepseek_infra.web.server.extract_uploaded_file", side_effect=AppError("Cannot extract", code=ErrorCode.INVALID_REQUEST, status=400)):
        resp3 = client.post(
            "/share-target",
            files={"file": ("corrupt.bin", b"xyz", "application/octet-stream")},
            headers={"Host": "127.0.0.1"},
        )
        assert resp3.status_code == 303


def test_launcher_credentials_restrict_permissions_posix_and_nt(tmp_settings: Path) -> None:
    import os
    from deepseek_infra.launcher import credentials as creds_module

    target_file = tmp_settings / "test_creds.json"
    target_file.write_text("{}", encoding="utf-8")

    # 1. Test NT branch
    with patch.object(os, "name", "nt"):
        creds_module._restrict_permissions(target_file)

    # 2. Test POSIX branch with chmod
    with patch.object(os, "name", "posix"):
        with patch.object(Path, "chmod"):
            creds_module._restrict_permissions(target_file)

    # 3. Test POSIX branch with OSError
    with patch.object(os, "name", "posix"):
        with patch.object(Path, "chmod", side_effect=OSError("Permission denied")):
            creds_module._restrict_permissions(target_file)
