"""Coverage tests for Bandwidth QoS and Transfer Budget (v4.5)."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_transfer_budget
from deepseek_infra.infra.workspace.backup_transfer_budget import (
    ActiveTransfer,
    TargetBudgetConfig,
    TrafficClass,
    TransferBudgetManager,
    configure_global_transfer_budget,
    get_global_transfer_budget_manager,
    reset_global_transfer_budget_manager,
)


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
