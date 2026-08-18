"""Bandwidth QoS and Transfer Budget Manager (4.5.7).

Provides unified rate-limiting, traffic-class prioritization, and target-level
concurrency/bandwidth control for all backup, replication, repair, restore,
scrub, drill, and rebalance operations.
"""

from __future__ import annotations

import enum
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode


class TrafficClass(enum.IntEnum):
    """Traffic priority classes for transfer operations.

    Lower integer value = higher priority.
    """
    P0_DISASTER_RECOVERY = 0   # Interactive / disaster recovery restore (highest)
    P1_BACKUP_PUBLISH = 1       # Active primary backup publish
    P1_ACTIVE_BACKUP_PUBLISH = 1
    P2_REQUIRED_REPAIR = 2      # Required replica repair
    P2_REQUIRED_REPLICA_REPAIR = 2
    P3_REQUIRED_REPLICATION = 3 # Required replica initial replication
    P4_SCRUB_DRILL = 4          # Scrub & recovery drill verification
    P4_SCRUB_AND_DRILL = 4
    P5_REBALANCE_DRAIN = 5      # Rebalance & target drain migration
    P5_REBALANCE_AND_DRAIN = 5
    P6_BEST_EFFORT = 6          # Best-effort replication / speculative

    @property
    def priority(self) -> int:
        return int(self.value)

    @classmethod
    def from_str(cls, val: str) -> TrafficClass:
        normalized = val.strip().upper()
        for member in cls:
            if member.name == normalized or normalized in member.name or str(member.value) == normalized:
                return member
        return cls.P3_REQUIRED_REPLICATION


DEFAULT_GLOBAL_BYTES_PER_SECOND = 200 * 1024 * 1024       # 200 MiB/s
DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC = 100 * 1024 * 1024 # 100 MiB/s
DEFAULT_BACKGROUND_MAX_BYTES_PER_SEC = 50 * 1024 * 1024     # 50 MiB/s
DEFAULT_TARGET_MAX_CONCURRENCY = 4


@dataclass
class TargetBudgetConfig:
    max_read_bytes_per_second: int = 100 * 1024 * 1024
    max_write_bytes_per_second: int = 100 * 1024 * 1024
    max_concurrent_transfers: int = DEFAULT_TARGET_MAX_CONCURRENCY


@dataclass
class ActiveTransfer:
    transfer_id: str
    traffic_class: TrafficClass
    source_target_id: str | None
    dest_target_id: str | None
    estimated_bytes: int
    started_at: float
    bytes_transferred: int = 0
    last_chunk_at: float = field(default_factory=time.monotonic)


class TransferBudgetManager:
    """Thread-safe cooperative bandwidth budget manager."""

    def __init__(
        self,
        *,
        global_bytes_per_second: int | None = None,
        global_bandwidth_bytes_per_sec: int | None = None,
        reserved_recovery_bytes_per_sec: int | None = None,
        reserved_dr_bandwidth_bytes_per_sec: int | None = None,
        background_max_bytes_per_sec: int = DEFAULT_BACKGROUND_MAX_BYTES_PER_SEC,
        max_burst_bytes: int | None = None,
        per_target_read_bytes_per_sec: dict[str, int] | None = None,
        per_target_write_bytes_per_sec: dict[str, int] | None = None,
        max_concurrent_transfers_per_target: dict[str, int] | None = None,
        target_configs: dict[str, TargetBudgetConfig] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        g_rate = (
            global_bytes_per_second
            if global_bytes_per_second is not None
            else (global_bandwidth_bytes_per_sec if global_bandwidth_bytes_per_sec is not None else DEFAULT_GLOBAL_BYTES_PER_SECOND)
        )
        self.global_bytes_per_second = max(1024 * 1024, g_rate)
        dr_res = (
            reserved_recovery_bytes_per_sec
            if reserved_recovery_bytes_per_sec is not None
            else (reserved_dr_bandwidth_bytes_per_sec if reserved_dr_bandwidth_bytes_per_sec is not None else DEFAULT_RESERVED_RECOVERY_BYTES_PER_SEC)
        )
        self.reserved_recovery_bytes_per_sec = max(0, dr_res)
        self.background_max_bytes_per_sec = max(1024 * 1024, background_max_bytes_per_sec)
        self.max_burst_bytes = max_burst_bytes or (self.global_bytes_per_second * 2)

        self._target_configs: dict[str, TargetBudgetConfig] = {}
        if target_configs:
            for tid, cfg in target_configs.items():
                if isinstance(cfg, TargetBudgetConfig):
                    self._target_configs[tid] = cfg
                elif isinstance(cfg, dict):
                    self.set_target_budget(
                        tid,
                        max_read_bytes_per_second=cfg.get("max_read_bytes_per_second") or cfg.get("readBytesPerSec"),
                        max_write_bytes_per_second=cfg.get("max_write_bytes_per_second") or cfg.get("writeBytesPerSec"),
                        max_concurrent_transfers=cfg.get("max_concurrent_transfers") or cfg.get("maxConcurrentTransfers"),
                    )
        self._active_transfers: dict[str, ActiveTransfer] = {}

        if per_target_read_bytes_per_sec:
            for tid, r_lim in per_target_read_bytes_per_sec.items():
                self.set_target_budget(tid, max_read_bytes_per_second=r_lim)
        if per_target_write_bytes_per_sec:
            for tid, w_lim in per_target_write_bytes_per_sec.items():
                self.set_target_budget(tid, max_write_bytes_per_second=w_lim)
        if max_concurrent_transfers_per_target:
            for tid, c_lim in max_concurrent_transfers_per_target.items():
                self.set_target_budget(tid, max_concurrent_transfers=c_lim)

        # Token buckets: key -> (tokens, last_refill_time)
        self._global_tokens: float = float(self.global_bytes_per_second)
        self._global_last_refill: float = time.monotonic()

    def set_target_budget(
        self,
        target_id: str,
        *,
        max_read_bytes_per_second: int | None = None,
        max_write_bytes_per_second: int | None = None,
        max_concurrent_transfers: int | None = None,
    ) -> None:
        with self._lock:
            existing = self._target_configs.get(target_id, TargetBudgetConfig())
            self._target_configs[target_id] = TargetBudgetConfig(
                max_read_bytes_per_second=max_read_bytes_per_second if max_read_bytes_per_second is not None else existing.max_read_bytes_per_second,
                max_write_bytes_per_second=max_write_bytes_per_second if max_write_bytes_per_second is not None else existing.max_write_bytes_per_second,
                max_concurrent_transfers=max_concurrent_transfers if max_concurrent_transfers is not None else existing.max_concurrent_transfers,
            )

    def get_target_budget(self, target_id: str) -> TargetBudgetConfig:
        with self._lock:
            return self._target_configs.get(target_id, TargetBudgetConfig())

    def acquire_bandwidth(
        self,
        requested_bytes: int,
        *,
        traffic_class: TrafficClass = TrafficClass.P6_BEST_EFFORT,
        dest_target_id: str | None = None,
        source_target_id: str | None = None,
    ) -> int:
        with self._lock:
            return max(1, requested_bytes)

    @contextmanager
    def track_transfer(
        self,
        dest_target_id: str | None = None,
        source_target_id: str | None = None,
        traffic_class: TrafficClass = TrafficClass.P6_BEST_EFFORT,
        transfer_id: str | None = None,
        estimated_bytes: int = 0,
    ) -> Iterator[str]:
        tid = transfer_id or f"xfer_{secrets.token_hex(6)}"
        self.acquire_transfer_token(
            tid,
            traffic_class,
            source_target_id=source_target_id,
            dest_target_id=dest_target_id,
            estimated_bytes=estimated_bytes,
        )
        try:
            yield tid
        finally:
            self.release_transfer_token(tid)

    def acquire_transfer_token(
        self,
        transfer_id: str,
        traffic_class: TrafficClass,
        *,
        source_target_id: str | None = None,
        dest_target_id: str | None = None,
        estimated_bytes: int = 0,
    ) -> ActiveTransfer:
        with self._lock:
            # Enforce per-target concurrency
            for tid, role in [(source_target_id, "read"), (dest_target_id, "write")]:
                if tid:
                    cfg = self.get_target_budget(tid)
                    active_count = sum(
                        1 for t in self._active_transfers.values()
                        if (t.source_target_id == tid or t.dest_target_id == tid)
                    )
                    if active_count >= cfg.max_concurrent_transfers:
                        # Allow P0 recovery to bypass concurrency limit
                        if traffic_class != TrafficClass.P0_DISASTER_RECOVERY:
                            raise AppError(
                                f"target-transfer-concurrency-exceeded: target {tid} has {active_count} active transfers (max {cfg.max_concurrent_transfers})",
                                code=ErrorCode.RATE_LIMITED,
                                status=429,
                            )

            transfer = ActiveTransfer(
                transfer_id=transfer_id,
                traffic_class=traffic_class,
                source_target_id=source_target_id,
                dest_target_id=dest_target_id,
                estimated_bytes=estimated_bytes,
                started_at=time.monotonic(),
            )
            self._active_transfers[transfer_id] = transfer
            return transfer

    def release_transfer_token(self, transfer_id: str) -> None:
        with self._lock:
            self._active_transfers.pop(transfer_id, None)

    def get_effective_rate_limit(self, transfer_id: str) -> int:
        """Compute effective bytes/sec for this transfer taking into account priority and targets."""
        with self._lock:
            transfer = self._active_transfers.get(transfer_id)
            if not transfer:
                return self.global_bytes_per_second

            has_active_recovery = any(
                t.traffic_class == TrafficClass.P0_DISASTER_RECOVERY
                for t in self._active_transfers.values()
            )

            # Base class limit
            if transfer.traffic_class == TrafficClass.P0_DISASTER_RECOVERY:
                base_rate = self.global_bytes_per_second
            elif has_active_recovery:
                # Background transfers are throttled when recovery is active
                base_rate = min(self.background_max_bytes_per_sec, max(1024 * 1024, self.global_bytes_per_second - self.reserved_recovery_bytes_per_sec))
            else:
                if transfer.traffic_class in {TrafficClass.P1_BACKUP_PUBLISH, TrafficClass.P2_REQUIRED_REPAIR, TrafficClass.P3_REQUIRED_REPLICATION}:
                    base_rate = self.global_bytes_per_second
                else:
                    base_rate = min(self.background_max_bytes_per_sec, self.global_bytes_per_second)

            # Target read/write limits
            if transfer.source_target_id:
                src_cfg = self.get_target_budget(transfer.source_target_id)
                base_rate = min(base_rate, src_cfg.max_read_bytes_per_second)

            if transfer.dest_target_id:
                dst_cfg = self.get_target_budget(transfer.dest_target_id)
                base_rate = min(base_rate, dst_cfg.max_write_bytes_per_second)

            return max(64 * 1024, base_rate)

    def consume_bandwidth(
        self,
        transfer_id: str,
        chunk_size: int,
        *,
        now: float | None = None,
    ) -> float:
        """Record chunk transfer and return required sleep time in seconds (if any) to throttle."""
        with self._lock:
            transfer = self._active_transfers.get(transfer_id)
            if not transfer:
                return 0.0

            current_time = now if now is not None else time.monotonic()
            transfer.bytes_transferred += chunk_size
            transfer.last_chunk_at = current_time

            rate_limit = min(float(self.global_bytes_per_second), float(self.get_effective_rate_limit(transfer_id)))

            # Refill global tokens
            elapsed = current_time - self._global_last_refill
            if elapsed > 0:
                self._global_tokens = min(float(self.global_bytes_per_second), self._global_tokens + elapsed * self.global_bytes_per_second)
                self._global_last_refill = current_time

            self._global_tokens -= chunk_size
            sleep_needed = 0.0
            if self._global_tokens < 0:
                deficit = -self._global_tokens
                sleep_needed = min(1.0, deficit / max(1.0, rate_limit))

            return max(0.0, sleep_needed)

    def throttled_generator(
        self,
        stream: Iterator[bytes],
        transfer_id: str | None = None,
        *,
        traffic_class: TrafficClass = TrafficClass.P6_BEST_EFFORT,
        dest_target_id: str | None = None,
        source_target_id: str | None = None,
    ) -> Iterator[bytes]:
        """Wrap an iterator of bytes to apply rate-limiting delays between chunks."""
        tid = transfer_id or "ephemeral_transfer"
        cleanup = False
        if tid not in self._active_transfers:
            self.acquire_transfer_token(
                tid,
                traffic_class,
                source_target_id=source_target_id,
                dest_target_id=dest_target_id,
            )
            cleanup = True

        try:
            for chunk in stream:
                if not chunk:
                    continue
                sleep_sec = self.consume_bandwidth(tid, len(chunk))
                if sleep_sec > 0.001:
                    time.sleep(sleep_sec)
                yield chunk
        finally:
            if cleanup:
                self.release_transfer_token(tid)

    def transfer_control_summary(self) -> dict[str, Any]:
        """Return real-time projection of transfer control without remote I/O."""
        with self._lock:
            p0_count = sum(1 for t in self._active_transfers.values() if t.traffic_class == TrafficClass.P0_DISASTER_RECOVERY)
            p2_count = sum(1 for t in self._active_transfers.values() if t.traffic_class in {TrafficClass.P2_REQUIRED_REPAIR, TrafficClass.P3_REQUIRED_REPLICATION})
            rebalance_backlog = sum(
                t.estimated_bytes for t in self._active_transfers.values()
                if t.traffic_class in {TrafficClass.P5_REBALANCE_DRAIN, TrafficClass.P6_BEST_EFFORT}
            )
            has_throttle = p0_count > 0

            return {
                "activeTransfersTotal": len(self._active_transfers),
                "activeRecoveryTransfers": p0_count,
                "activeRepairTransfers": p2_count,
                "rebalanceBacklogBytes": rebalance_backlog,
                "backgroundThrottleActive": has_throttle,
                "globalBytesPerSecond": self.global_bytes_per_second,
                "globalBandwidthBytesPerSec": self.global_bytes_per_second,
                "reservedRecoveryBytesPerSecond": self.reserved_recovery_bytes_per_sec,
                "reservedDrBandwidthBytesPerSec": self.reserved_recovery_bytes_per_sec,
            }

    def active_transfers_count(self) -> int:
        with self._lock:
            return len(self._active_transfers)

    def active_transfers_bytes(self) -> int:
        with self._lock:
            return sum(t.bytes_transferred for t in self._active_transfers.values())


# Singleton manager instance
_GLOBAL_BUDGET_MANAGER = TransferBudgetManager()


def get_global_transfer_budget_manager() -> TransferBudgetManager:
    return _GLOBAL_BUDGET_MANAGER


def reset_global_transfer_budget_manager() -> TransferBudgetManager:
    global _GLOBAL_BUDGET_MANAGER
    _GLOBAL_BUDGET_MANAGER = TransferBudgetManager()
    return _GLOBAL_BUDGET_MANAGER


def configure_global_transfer_budget(
    *,
    global_bandwidth_bytes_per_sec: int | None = None,
    reserved_dr_bandwidth_bytes_per_sec: int | None = None,
    target_configs: dict[str, TargetBudgetConfig] | None = None,
) -> TransferBudgetManager:
    global _GLOBAL_BUDGET_MANAGER
    _GLOBAL_BUDGET_MANAGER = TransferBudgetManager(
        global_bandwidth_bytes_per_sec=global_bandwidth_bytes_per_sec,
        reserved_dr_bandwidth_bytes_per_sec=reserved_dr_bandwidth_bytes_per_sec,
        target_configs=target_configs,
    )
    return _GLOBAL_BUDGET_MANAGER
