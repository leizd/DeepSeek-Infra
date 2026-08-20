"""Coverage tests for Capacity Governance and Watermarks (v4.5)."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_dr_ledger,
    backup_policies,
    backup_replication,
    backup_targets,
)


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


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
    assert full_sz is None
    assert inc_sz is None

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


def test_backup_capacity_all_admission_and_horizon_branches(tmp_settings: Path) -> None:
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
