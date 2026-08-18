"""Target capacity governance, size admission, and cost estimation (4.5.7).

Enforces soft/hard watermarks for backup write placement, predicts backup size
requirements, validates Force Full size admission, and calculates target
exhaustion horizons.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import backup_dr_ledger, backup_targets

DEFAULT_MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024     # 10 GiB
DEFAULT_MIN_FREE_PERCENT = 10.0                       # 10%
DEFAULT_SOFT_WATERMARK_PERCENT = 80.0                 # 80%
DEFAULT_HARD_WATERMARK_PERCENT = 90.0                 # 90%
DEFAULT_PREDICTED_BACKUP_BYTES = 500 * 1024 * 1024    # 500 MiB fallback


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_target_capacity(target_id: str) -> dict[str, Any]:
    """Retrieve the latest observed capacity for a target."""
    return backup_targets.probe_target_capacity(target_id)


def predict_next_backup_bytes(
    policy_id: str,
    *,
    snapshot_kind: str = "full",
) -> int:
    """Calculate P90 predicted physical ciphertext size from recent backups."""
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=20)
    if not copies:
        return DEFAULT_PREDICTED_BACKUP_BYTES

    # Look for sizes in copies
    sizes: list[int] = []
    for c in copies:
        meta_val = c.get("metadata") if isinstance(c, dict) else None
        meta: dict[str, Any] = meta_val if isinstance(meta_val, dict) else {}
        sz = (
            (c.get("logicalBytes") if isinstance(c, dict) else None)
            or (c.get("physicalBytes") if isinstance(c, dict) else None)
            or (c.get("ciphertextBytes") if isinstance(c, dict) else None)
            or meta.get("logicalBytes")
            or meta.get("physicalBytes")
            or meta.get("ciphertextBytes")
        )
        if sz and isinstance(sz, int) and sz > 0:
            sizes.append(sz)

    if not sizes:
        return DEFAULT_PREDICTED_BACKUP_BYTES

    sizes.sort()
    # P90 index
    idx = int(len(sizes) * 0.9)
    p90 = sizes[min(idx, len(sizes) - 1)]

    if snapshot_kind == "incremental":
        # Incremental typical footprint is ~20% of full
        return max(10 * 1024 * 1024, int(p90 * 0.3))
    return max(50 * 1024 * 1024, p90)


def check_target_capacity_admission(
    target_id: str,
    required_bytes: int,
    *,
    policy: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Check if target can admit a write of required_bytes without breaching policy watermarks."""
    policy_dict = policy or {}
    placement = policy_dict.get("placement") or {}
    hard_watermark = float(placement.get("hardWatermarkPercent") or DEFAULT_HARD_WATERMARK_PERCENT)
    min_free_bytes = int(placement.get("minFreeBytes") or DEFAULT_MIN_FREE_BYTES)
    min_free_pct = float(placement.get("minFreePercent") or DEFAULT_MIN_FREE_PERCENT)

    cap = get_target_capacity(target_id)
    if cap.get("freeBytes") is None:
        # S3 target without operator quota: admit
        return True, "unconstrained"

    free_bytes = int(cap["freeBytes"])
    total_bytes = int(cap.get("totalBytes") or 0)
    used_val = cap.get("usedBytes")
    used_bytes = int(used_val) if used_val is not None else max(0, total_bytes - free_bytes)

    # 1. Hard watermark check
    if total_bytes > 0:
        used_pct = (used_bytes / total_bytes) * 100.0
        if used_pct >= hard_watermark:
            return False, f"target-hard-watermark-exceeded:{used_pct:.1f}%>={hard_watermark:.1f}%"

    # 2. Free space after write check
    remaining_free = free_bytes - required_bytes
    if remaining_free < 0:
        return False, f"target-insufficient-space:{free_bytes}<{required_bytes}"

    if remaining_free < min_free_bytes:
        return False, f"target-breaches-min-free-bytes:{remaining_free}<{min_free_bytes}"

    if total_bytes > 0:
        free_pct_after = (remaining_free / total_bytes) * 100.0
        if free_pct_after < min_free_pct:
            return False, f"target-breaches-min-free-percent:{free_pct_after:.1f}%<{min_free_pct:.1f}%"

    return True, "admitted"


def estimate_target_exhaustion_horizon(
    target_id: str,
    policy_id: str,
) -> dict[str, Any]:
    """Estimate days until target runs out of space based on daily ingestion rate."""
    cap = get_target_capacity(target_id)
    free_bytes = cap.get("freeBytes")
    total_bytes = cap.get("totalBytes")
    free_pct = cap.get("freePercent")

    if free_bytes is None or total_bytes is None:
        return {
            "targetId": target_id,
            "status": "unconstrained",
            "freePercent": None,
            "estimatedDaysToFull": 9999,
        }

    # Estimate daily growth from recent 7 days
    copies = backup_dr_ledger.list_logical_recovery_copies(target_id=target_id, policy_id=policy_id, limit=30)
    total_ingested = 0
    for c in copies:
        sz = c.get("logicalBytes") or c.get("physicalBytes") or 50 * 1024 * 1024
        total_ingested += int(sz)

    daily_rate = max(10 * 1024 * 1024, total_ingested // max(1, len(copies)))
    days_to_full = max(0, int(free_bytes // daily_rate))

    status = "healthy"
    if free_pct is not None:
        if free_pct < 10.0 or days_to_full < 7:
            status = "critical"
        elif free_pct < 20.0 or days_to_full < 30:
            status = "degraded"

    return {
        "targetId": target_id,
        "status": status,
        "freeBytes": free_bytes,
        "totalBytes": total_bytes,
        "freePercent": free_pct,
        "dailyIngressEstimateBytes": daily_rate,
        "estimatedDaysToFull": days_to_full,
    }


def estimate_transfer_cost(
    bytes_to_transfer: int,
    *,
    source_target_id: str | None = None,
    dest_target_id: str | None = None,
) -> dict[str, Any]:
    """Calculate operator-supplied cost estimates for transfer and storage delta."""
    src_record = backup_targets.get_target(source_target_id) if source_target_id else {}
    dst_record = backup_targets.get_target(dest_target_id) if dest_target_id else {}

    gib = bytes_to_transfer / (1024.0 * 1024.0 * 1024.0)

    # Operator configured rates
    egress_rate = float((src_record or {}).get("egressCostPerGiB") or 0.09)
    storage_rate = float((dst_record or {}).get("storageCostPerGiBMonth") or 0.023)

    one_time_egress_cost = gib * egress_rate if (src_record or {}).get("costClass") == "cross-region" else 0.0
    monthly_storage_cost = gib * storage_rate

    return {
        "bytesToTransfer": bytes_to_transfer,
        "gibToTransfer": round(gib, 4),
        "estimatedOneTimeTransferCost": round(one_time_egress_cost, 4),
        "estimatedMonthlyStorageCostDelta": round(monthly_storage_cost, 4),
        "currency": "USD",
        "isEstimate": True,
    }


def capacity_summary() -> dict[str, Any]:
    """Provide a cluster-wide capacity governance summary across all targets."""
    targets = backup_targets.list_targets()
    target_summaries = []
    overall_status = "healthy"
    for t in targets:
        tid = str(t.get("targetId"))
        cap = backup_targets.probe_target_capacity(tid)
        horizon = estimate_target_exhaustion_horizon(tid, policy_id="")
        if horizon.get("status") == "critical":
            overall_status = "critical"
        elif horizon.get("status") == "degraded" and overall_status != "critical":
            overall_status = "degraded"
        target_summaries.append(
            {
                "targetId": tid,
                "label": t.get("label"),
                "kind": t.get("kind"),
                "capacity": cap,
                "horizon": horizon,
            }
        )
    return {
        "overallStatus": overall_status,
        "targets": target_summaries,
    }
