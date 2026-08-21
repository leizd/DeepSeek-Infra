"""Target capacity governance, size admission, and cost estimation (4.5.9).

Enforces soft/hard watermarks for backup write placement, predicts backup size
requirements, validates Force Full size admission, and calculates target
exhaustion horizons from elapsed-time physical growth observations.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import backup_control, backup_dr_ledger, backup_targets

DEFAULT_MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024     # 10 GiB
DEFAULT_MIN_FREE_PERCENT = 10.0                       # 10%
DEFAULT_SOFT_WATERMARK_PERCENT = 80.0                 # 80%
DEFAULT_HARD_WATERMARK_PERCENT = 90.0                 # 90%
WORKSPACE_ESTIMATOR_SAFETY_FACTOR = 1.20


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_target_capacity(target_id: str, *, probe: bool = True) -> dict[str, Any]:
    """Retrieve capacity for a target.

    ``probe=True`` (default) may perform remote/filesystem observation for
    maintenance workers. ``probe=False`` reads only persisted projections —
    required for DR Readiness zero-remote-I/O paths.
    """
    if not probe:
        observation = backup_control.get_target_capacity_observation(target_id)
        if observation is not None:
            return observation
        usage = backup_control.physical_usage_summary(target_id)
        try:
            target = backup_targets.get_target(target_id)
        except Exception:
            target = None
        quota = (target or {}).get("quotaBytes") if isinstance(target, dict) else None
        physical = int(usage.get("physicalStoredBytes") or 0)
        if quota is not None and int(quota) > 0:
            total = int(quota)
            free = max(0, total - physical)
            return {
                "targetId": target_id,
                "totalBytes": total,
                "usedBytes": physical,
                "freeBytes": free,
                "freePercent": round((free / total) * 100.0, 2) if total else 0.0,
                "physicalStoredBytes": physical,
                "liveReferencedBytes": int(usage.get("liveReferencedBytes") or 0),
                "retiredPendingGcBytes": int(usage.get("retiredPendingGcBytes") or 0),
                "capacityConfidence": str(usage.get("confidence") or "unavailable"),
                "source": "persisted-projection",
                "observedAt": None,
            }
        return {
            "targetId": target_id,
            "totalBytes": None,
            "usedBytes": physical or None,
            "freeBytes": None,
            "freePercent": None,
            "physicalStoredBytes": physical,
            "source": "persisted-projection-unconstrained",
            "observedAt": None,
        }
    return backup_targets.probe_target_capacity(target_id)


def predict_next_backup_size(
    policy_id: str,
    *,
    snapshot_kind: str = "full",
    workspace_physical_bytes: int | None = None,
) -> dict[str, Any]:
    """Return a confidence-labelled physical ciphertext size prediction.

    Logical workspace bytes are deliberately not used as ciphertext evidence.
    When no physical evidence or explicit workspace estimator is available the
    result remains unavailable; callers must not substitute a small constant.
    """
    sizes: list[int] = []
    source = "historical-physical-p90"
    evidence = backup_control.list_capacity_evidence(policy_id, snapshot_kind=snapshot_kind, limit=20)
    seen_evidence_backups: set[str] = set()
    for item in evidence:
        evidence_backup_id = str(item.get("backupId") or f"evidence-{item.get('evidenceId')}")
        if evidence_backup_id in seen_evidence_backups:
            continue
        seen_evidence_backups.add(evidence_backup_id)
        sz = item.get("physicalBytes")
        if isinstance(sz, int) and not isinstance(sz, bool) and sz > 0:
            sizes.append(sz)

    if not sizes:
        recovery_points = backup_dr_ledger.list_recovery_points(policy_id=policy_id, limit=40)
        seen_backups: set[str] = set()
        for point in recovery_points:
            if str(point.get("snapshotKind") or "full") != str(snapshot_kind or "full"):
                continue
            backup_id = str(point.get("backupId") or "")
            if backup_id in seen_backups:
                continue
            seen_backups.add(backup_id)
            sz = point.get("ciphertextBytes")
            if isinstance(sz, int) and not isinstance(sz, bool) and sz > 0:
                sizes.append(sz)

    if not sizes:
        copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=20)
        for copy in copies:
            meta_val = copy.get("metadata") if isinstance(copy, dict) else None
            meta: dict[str, Any] = meta_val if isinstance(meta_val, dict) else {}
            sz = (
                (copy.get("physicalBytes") if isinstance(copy, dict) else None)
                or (copy.get("ciphertextBytes") if isinstance(copy, dict) else None)
                or meta.get("physicalBytes")
                or meta.get("ciphertextBytes")
            )
            if isinstance(sz, int) and not isinstance(sz, bool) and sz > 0:
                sizes.append(sz)

    if sizes:
        sizes.sort()
        idx = int(len(sizes) * 0.9)
        p90 = sizes[min(idx, len(sizes) - 1)]
        confidence = "high" if len(sizes) >= 10 else "medium" if len(sizes) >= 3 else "low"
        return {
            "predictedBytes": p90,
            "capacityConfidence": confidence,
            "source": source,
            "isEstimate": True,
            "snapshotKind": str(snapshot_kind or "full"),
            "sampleCount": len(sizes),
        }

    if workspace_physical_bytes is not None and workspace_physical_bytes > 0:
        predicted = max(1, int(workspace_physical_bytes * WORKSPACE_ESTIMATOR_SAFETY_FACTOR))
        return {
            "predictedBytes": predicted,
            "capacityConfidence": "medium",
            "source": "workspace-physical-estimator-with-safety-factor",
            "isEstimate": True,
            "snapshotKind": str(snapshot_kind or "full"),
            "sampleCount": 1,
        }

    return {
        "predictedBytes": None,
        "capacityConfidence": "unavailable",
        "source": "no-physical-evidence",
        "isEstimate": True,
        "snapshotKind": str(snapshot_kind or "full"),
        "sampleCount": 0,
    }


def predict_next_backup_bytes(
    policy_id: str,
    *,
    snapshot_kind: str = "full",
    workspace_physical_bytes: int | None = None,
) -> int | None:
    """Compatibility wrapper returning the confidence-aware prediction bytes."""
    prediction = predict_next_backup_size(
        policy_id,
        snapshot_kind=snapshot_kind,
        workspace_physical_bytes=workspace_physical_bytes,
    )
    value = prediction.get("predictedBytes")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def record_physical_size_evidence(
    *,
    policy_id: str,
    backup_id: str,
    snapshot_kind: str,
    physical_bytes: int,
    observed_at: str | None = None,
    source: str = "formal-receipt-v4",
) -> None:
    """Persist observed ciphertext bytes for later confidence-aware admission."""
    backup_control.record_capacity_evidence(
        policy_id=policy_id,
        backup_id=backup_id,
        snapshot_kind=snapshot_kind,
        physical_bytes=physical_bytes,
        confidence="high",
        source=source,
        observed_at=observed_at,
    )


def check_target_capacity_admission(
    target_id: str,
    required_bytes: int | None,
    *,
    policy: dict[str, Any] | None = None,
    force_full: bool = False,
) -> tuple[bool, str]:
    """Check if target can admit a write of required_bytes without breaching policy watermarks."""
    if required_bytes is None and force_full:
        return False, "capacity-evidence-unavailable"

    policy_dict = policy or {}
    placement = policy_dict.get("placement") or {}
    hard_watermark = float(placement.get("hardWatermarkPercent") or DEFAULT_HARD_WATERMARK_PERCENT)
    min_free_bytes = int(placement.get("minFreeBytes") or DEFAULT_MIN_FREE_BYTES)
    min_free_pct = float(placement.get("minFreePercent") or DEFAULT_MIN_FREE_PERCENT)

    cap = get_target_capacity(target_id)
    if cap.get("freeBytes") is None:
        # A target without an operator quota is not bounded by this admission
        # layer, but the prediction remains explicitly unavailable upstream.
        return True, "unconstrained"

    if required_bytes is None:
        return False, "capacity-evidence-unavailable"
    if required_bytes < 0:
        return False, "invalid-required-bytes"

    free_bytes = int(cap["freeBytes"])
    total_bytes = int(cap.get("totalBytes") or 0)
    used_val = cap.get("usedBytes")
    used_bytes = int(used_val) if used_val is not None else max(0, total_bytes - free_bytes)

    # Prefer physical object accounting when the probe surfaces it.
    physical = cap.get("physicalStoredBytes")
    if isinstance(physical, int) and not isinstance(physical, bool) and physical >= 0 and total_bytes > 0:
        used_bytes = physical
        free_bytes = max(0, total_bytes - used_bytes)

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


def _growth_rates_from_observations(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute elapsed-time growth rates from timestamped physical observations."""
    points: list[tuple[datetime, int]] = []
    for item in observations:
        ts = _parse_iso(str(item.get("observedAt") or ""))
        stored = item.get("physicalStoredBytes")
        if ts is None or not isinstance(stored, int) or isinstance(stored, bool):
            continue
        points.append((ts, int(stored)))
    points.sort(key=lambda pair: pair[0])
    if len(points) < 2:
        return {"status": "unavailable", "confidence": "unavailable", "reason": "insufficient-time-series"}

    oldest_ts, oldest_bytes = points[0]
    newest_ts, newest_bytes = points[-1]
    elapsed_seconds = max(1.0, (newest_ts - oldest_ts).total_seconds())
    elapsed_days = elapsed_seconds / 86400.0
    delta = newest_bytes - oldest_bytes
    # Non-positive growth is reported honestly; admission still uses free space.
    bytes_per_day = delta / elapsed_days if elapsed_days > 0 else 0.0

    # Windowed rates for P50/P90-ish robust stats over consecutive samples.
    daily_samples: list[float] = []
    for idx in range(1, len(points)):
        prev_ts, prev_bytes = points[idx - 1]
        cur_ts, cur_bytes = points[idx]
        seconds = max(1.0, (cur_ts - prev_ts).total_seconds())
        daily_samples.append((cur_bytes - prev_bytes) * 86400.0 / seconds)
    daily_samples.sort()
    if daily_samples:
        mid = daily_samples[len(daily_samples) // 2]
        p90_idx = min(len(daily_samples) - 1, int(len(daily_samples) * 0.9))
        p90 = daily_samples[p90_idx]
    else:
        mid = bytes_per_day
        p90 = bytes_per_day

    confidence = "high" if len(points) >= 10 and elapsed_days >= 7 else "medium" if len(points) >= 3 and elapsed_days >= 1 else "low"
    return {
        "status": "ok",
        "confidence": confidence,
        "bytesPerDayP50": max(0.0, float(mid)),
        "bytesPerDayP90": max(0.0, float(p90)),
        "bytesPerDay": max(0.0, float(bytes_per_day)),
        "sampleCount": len(points),
        "elapsedDays": elapsed_days,
        "netGrowthBytes": delta,
    }


def estimate_target_exhaustion_horizon(
    target_id: str,
    policy_id: str,
    *,
    probe: bool = True,
    record_observation: bool | None = None,
) -> dict[str, Any]:
    """Estimate days until target runs out of space from elapsed-time growth.

    For DR Readiness / pure read models call with ``probe=False`` and
    ``record_observation=False`` so no remote I/O or control mutations occur.
    """
    del policy_id  # retained for API compatibility; growth is target-scoped
    should_record = probe if record_observation is None else bool(record_observation)
    cap = get_target_capacity(target_id, probe=probe)
    free_bytes = cap.get("freeBytes")
    total_bytes = cap.get("totalBytes")
    free_pct = cap.get("freePercent")

    if free_bytes is None or total_bytes is None:
        return {
            "targetId": target_id,
            "status": "unconstrained",
            "freePercent": None,
            # Compatibility sentinel for unconstrained targets; timed forecasts use None.
            "estimatedDaysToFull": 9999,
            "confidence": "unavailable",
            "forecastStatus": "unconstrained",
        }

    # Zero free space is immediately exhausted regardless of growth samples.
    if int(free_bytes) <= 0:
        return {
            "targetId": target_id,
            "status": "critical",
            "freeBytes": free_bytes,
            "totalBytes": total_bytes,
            "freePercent": free_pct if free_pct is not None else 0.0,
            "dailyIngressEstimateBytes": None,
            "bytesPerDayP50": None,
            "bytesPerDayP90": None,
            "estimatedDaysToFull": 0,
            "daysToSoftWatermark": 0,
            "daysToHardWatermark": 0,
            "confidence": "high",
            "forecastStatus": "exhausted",
        }

    # Record a growth observation only when explicitly probing (maintenance).
    if should_record:
        physical = cap.get("physicalStoredBytes")
        if isinstance(physical, int) and not isinstance(physical, bool):
            backup_control.record_capacity_growth_observation(
                target_id=target_id,
                physical_stored_bytes=int(physical),
                live_referenced_bytes=int(cap.get("liveReferencedBytes") or 0),
                retired_pending_gc_bytes=int(cap.get("retiredPendingGcBytes") or 0),
                observed_at=str(cap.get("observedAt") or _utc_iso()),
            )
        else:
            used = cap.get("usedBytes")
            if isinstance(used, int) and not isinstance(used, bool):
                backup_control.record_capacity_growth_observation(
                    target_id=target_id,
                    physical_stored_bytes=int(used),
                    observed_at=str(cap.get("observedAt") or _utc_iso()),
                )

    observations = backup_control.list_capacity_growth_observations(target_id, limit=60)
    rates = _growth_rates_from_observations(observations)

    status = "healthy"
    if free_pct is not None:
        if free_pct < 10.0:
            status = "critical"
        elif free_pct < 20.0:
            status = "degraded"

    if rates.get("status") != "ok":
        return {
            "targetId": target_id,
            "status": status,
            "freeBytes": free_bytes,
            "totalBytes": total_bytes,
            "freePercent": free_pct,
            "dailyIngressEstimateBytes": None,
            "bytesPerDayP50": None,
            "bytesPerDayP90": None,
            "estimatedDaysToFull": None,
            "daysToSoftWatermark": None,
            "daysToHardWatermark": None,
            "confidence": "unavailable",
            "forecastStatus": "unavailable",
        }

    p50 = float(rates["bytesPerDayP50"])
    p90 = float(rates["bytesPerDayP90"])
    daily_rate = max(p50, 0.0)
    if daily_rate <= 0:
        days_to_full: int | None = None
        days_soft: int | None = None
        days_hard: int | None = None
        forecast_status = "stable-or-shrinking"
    else:
        days_to_full = max(0, int(float(free_bytes) // daily_rate))
        soft_free = max(0.0, float(total_bytes) * (1.0 - DEFAULT_SOFT_WATERMARK_PERCENT / 100.0))
        hard_free = max(0.0, float(total_bytes) * (1.0 - DEFAULT_HARD_WATERMARK_PERCENT / 100.0))
        # Days until free space falls to the watermark residual.
        days_soft = max(0, int(max(0.0, float(free_bytes) - soft_free) // daily_rate))
        days_hard = max(0, int(max(0.0, float(free_bytes) - hard_free) // daily_rate))
        forecast_status = "ok"
        if days_to_full < 7:
            status = "critical"
        elif days_to_full < 30 and status == "healthy":
            status = "degraded"

    return {
        "targetId": target_id,
        "status": status,
        "freeBytes": free_bytes,
        "totalBytes": total_bytes,
        "freePercent": free_pct,
        "dailyIngressEstimateBytes": int(daily_rate) if daily_rate > 0 else 0,
        "bytesPerDayP50": int(p50),
        "bytesPerDayP90": int(p90),
        "estimatedDaysToFull": days_to_full,
        "daysToSoftWatermark": days_soft,
        "daysToHardWatermark": days_hard,
        "confidence": rates.get("confidence"),
        "forecastStatus": forecast_status,
        "sampleCount": rates.get("sampleCount"),
        "elapsedDays": rates.get("elapsedDays"),
    }


def estimate_transfer_cost(
    bytes_to_transfer: int,
    *,
    source_target_id: str | None = None,
    dest_target_id: str | None = None,
) -> dict[str, Any]:
    """Calculate operator-supplied cost estimates; never invent default prices."""
    src_record = backup_targets.get_target(source_target_id) if source_target_id and source_target_id != "managed-local" else {}
    dst_record = backup_targets.get_target(dest_target_id) if dest_target_id and dest_target_id != "managed-local" else {}
    src = src_record or {}
    dst = dst_record or {}

    gib = bytes_to_transfer / (1024.0 * 1024.0 * 1024.0)

    egress_raw = src.get("egressCostPerGiB")
    storage_raw = dst.get("storageCostPerGiBMonth")
    egress_rate: float | None = None
    storage_rate: float | None = None
    if isinstance(egress_raw, (int, float)) and not isinstance(egress_raw, bool):
        egress_rate = float(egress_raw)
    if isinstance(storage_raw, (int, float)) and not isinstance(storage_raw, bool):
        storage_rate = float(storage_raw)
    egress_known = egress_rate is not None
    storage_known = storage_rate is not None

    source_region = str(src.get("region") or "")
    dest_region = str(dst.get("region") or "")
    crosses_region = bool(source_region and dest_region and source_region != dest_region)
    explicitly_metered = str(src.get("costClass") or "") == "cross-region"
    needs_egress = crosses_region or explicitly_metered

    if (needs_egress and not egress_known) or not storage_known:
        return {
            "bytesToTransfer": bytes_to_transfer,
            "gibToTransfer": round(gib, 4),
            "estimatedOneTimeTransferCost": None,
            "estimatedMonthlyStorageCostDelta": None,
            "currency": "USD",
            "isEstimate": True,
            "costStatus": "unavailable",
            "rateSource": "missing-operator-rates",
            "egressCostPerGiB": egress_rate,
            "storageCostPerGiBMonth": storage_rate,
            "egressRateSource": "operator-configured" if egress_known else "unavailable",
            "storageRateSource": "operator-configured" if storage_known else "unavailable",
        }

    assert storage_rate is not None
    effective_egress = float(egress_rate or 0.0)
    one_time_egress_cost = gib * effective_egress if needs_egress else 0.0
    monthly_storage_cost = gib * storage_rate
    effective_at = str(dst.get("costRatesEffectiveAt") or src.get("costRatesEffectiveAt") or "")

    return {
        "bytesToTransfer": bytes_to_transfer,
        "gibToTransfer": round(gib, 4),
        "estimatedOneTimeTransferCost": round(one_time_egress_cost, 4),
        "estimatedMonthlyStorageCostDelta": round(monthly_storage_cost, 4),
        "currency": "USD",
        "isEstimate": True,
        "costStatus": "ok",
        "rateSource": "operator-configured",
        "egressCostPerGiB": effective_egress if needs_egress else 0.0,
        "storageCostPerGiBMonth": storage_rate,
        "egressRateSource": "operator-configured" if needs_egress else "not-applicable",
        "storageRateSource": "operator-configured",
        "effectiveAt": effective_at or None,
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
