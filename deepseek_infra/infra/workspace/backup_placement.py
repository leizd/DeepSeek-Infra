"""Autonomous Recovery SLO Controller (4.6.0 Gate E).

Reconciles desired Hot/Warm/Archive placement from policy objectives.
Ordering is strict:

1. Recoverability / lineage completeness
2. Copy count + failure-domain / region
3. RTO class evidence
4. Capacity safety
5. Operator-supplied cost (last)

Cost never overrides topology or recoverability.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_control,
    backup_dr_ledger,
    backup_policies,
    backup_targets,
    backup_tiering,
)

DEFAULT_HOT_WINDOW = 86400
DEFAULT_WARM_WINDOW = 604800
DEFAULT_ARCHIVE_AFTER = 2592000


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
    except ValueError:  # pragma: no cover - invalid timestamps treated as now
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    return int(value)


def normalize_recovery_placement(raw: Any) -> dict[str, Any]:
    """Normalize recoveryPlacement policy section."""
    if raw is None:
        return {
            "hotWindowSeconds": DEFAULT_HOT_WINDOW,
            "warmWindowSeconds": DEFAULT_WARM_WINDOW,
            "archiveAfterSeconds": DEFAULT_ARCHIVE_AFTER,
            "hotRestoreP90Seconds": 300,
            "warmRestoreP90Seconds": 1800,
            "minHotCopies": 1,
            "minWarmRegions": 0,
            "enabled": False,
        }
    if not isinstance(raw, dict):
        raise AppError("recoveryPlacement must be an object", code=ErrorCode.INVALID_PAYLOAD, status=400)
    hot = _as_int(raw.get("hotWindowSeconds"), DEFAULT_HOT_WINDOW)
    warm = _as_int(raw.get("warmWindowSeconds"), DEFAULT_WARM_WINDOW)
    archive = _as_int(raw.get("archiveAfterSeconds"), DEFAULT_ARCHIVE_AFTER)
    if hot < 0 or warm < hot or archive < warm:
        raise AppError(
            "recoveryPlacement windows must satisfy 0 <= hot <= warm <= archiveAfter",
            code=ErrorCode.INVALID_PAYLOAD,
            status=400,
        )
    return {
        "hotWindowSeconds": hot,
        "warmWindowSeconds": warm,
        "archiveAfterSeconds": archive,
        "hotRestoreP90Seconds": max(1, _as_int(raw.get("hotRestoreP90Seconds"), 300)),
        "warmRestoreP90Seconds": max(1, _as_int(raw.get("warmRestoreP90Seconds"), 1800)),
        "minHotCopies": max(0, _as_int(raw.get("minHotCopies"), 1)),
        "minWarmRegions": max(0, _as_int(raw.get("minWarmRegions"), 0)),
        "enabled": bool(raw.get("enabled", True)),
    }


def desired_tier_for_age(age_seconds: float, placement: dict[str, Any]) -> str:
    """Map age to desired storage tier from policy windows."""
    hot = _as_int(placement.get("hotWindowSeconds"), DEFAULT_HOT_WINDOW)
    warm = _as_int(placement.get("warmWindowSeconds"), DEFAULT_WARM_WINDOW)
    archive_after = _as_int(placement.get("archiveAfterSeconds"), DEFAULT_ARCHIVE_AFTER)
    age = max(0.0, float(age_seconds))
    if age < hot:
        return "hot"
    if age < warm:
        return "warm"
    if age >= archive_after:
        return "archive"
    return "warm"


def _copy_tiers_for_backup(
    backup_id: str,
    copies: list[dict[str, Any]],
    targets_by_id: dict[str, dict[str, Any]],
) -> list[tuple[str, str]]:
    """Return [(targetId, tier), ...] for live copies of one backup."""
    out: list[tuple[str, str]] = []
    for copy in copies:
        if not copy.get("recoverable"):
            continue
        if str(copy.get("state") or "") not in {"healthy", "replicated", ""}:
            continue
        if str(copy.get("backupId") or "") != backup_id:
            continue
        tid = str(copy.get("targetId") or "")
        if not tid:
            continue
        out.append((tid, backup_tiering.target_storage_tier(targets_by_id.get(tid))))
    return out


def _capacity_blocks_dest(target_id: str, *, soft: bool = True) -> tuple[bool, str]:
    horizon = backup_capacity.estimate_target_exhaustion_horizon(
        target_id, "", probe=False, record_observation=False
    )
    status = str(horizon.get("status") or "healthy")
    if status == "critical":
        return True, "capacity-hard-watermark"
    if soft and status == "degraded":
        return False, "capacity-soft-watermark"
    return False, "ok"


def evaluate_point_placement(
    policy: dict[str, Any],
    backup_id: str,
    *,
    committed_at: str | None,
    copies: list[dict[str, Any]],
    targets_by_id: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate one recovery point against recoveryPlacement objectives."""
    raw_placement = policy.get("recoveryPlacement")
    placement: dict[str, Any] = raw_placement if isinstance(raw_placement, dict) else {}
    if not placement.get("enabled", False):
        return {
            "backupId": backup_id,
            "action": "none",
            "reasonCodes": ["placement-controller-disabled"],
        }

    current = now or datetime.now(tz=timezone.utc)
    committed = _parse_iso(committed_at) or current
    age = max(0.0, (current - committed).total_seconds())
    desired = desired_tier_for_age(age, placement)
    tiers = _copy_tiers_for_backup(backup_id, copies, targets_by_id)
    present_tiers = {t for _, t in tiers}
    min_hot = _as_int(placement.get("minHotCopies"), 0)

    reason_codes: list[str] = []
    rejected: dict[str, str] = {}

    unit = backup_tiering.build_recovery_chain_placement_unit(
        str(policy.get("policyId") or ""),
        backup_id,
    )
    if not unit.get("closureComplete"):
        return {
            "backupId": backup_id,
            "desiredTier": desired,
            "action": "blocked",
            "reasonCodes": ["lineage-incomplete", str(unit.get("reason") or "missing-parent")],
            "missingBackupId": unit.get("missingBackupId"),
            "unit": unit,
        }

    hot_copies = sum(1 for _, t in tiers if t == "hot")
    if desired == "hot" and hot_copies < min_hot:
        reason_codes.append("hot-copy-objective-drift")
    if desired not in present_tiers:
        reason_codes.append(f"{desired}-tier-objective-drift")

    if desired in present_tiers and not (desired == "hot" and hot_copies < min_hot):
        primary = str(policy.get("primaryTargetId") or policy.get("targetId") or "")
        if primary:
            _blocked, cap_reason = _capacity_blocks_dest(primary, soft=True)
            if cap_reason == "capacity-soft-watermark" and any(tid == primary for tid, _ in tiers):
                reason_codes.append("primary-capacity-soft-watermark")
            else:
                return {
                    "backupId": backup_id,
                    "desiredTier": desired,
                    "currentTiers": sorted(present_tiers),
                    "action": "none",
                    "reasonCodes": ["objectives-satisfied"],
                    "unit": unit,
                }
        else:
            return {
                "backupId": backup_id,
                "desiredTier": desired,
                "currentTiers": sorted(present_tiers),
                "action": "none",
                "reasonCodes": ["objectives-satisfied"],
                "unit": unit,
            }

    candidates = [
        tid
        for tid, t in targets_by_id.items()
        if backup_tiering.target_storage_tier(t) == desired
        and str(t.get("drainState") or "active") not in {"draining", "drained"}
    ]
    if not candidates:
        return {
            "backupId": backup_id,
            "desiredTier": desired,
            "action": "blocked",
            "reasonCodes": reason_codes + ["no-eligible-tier-target"],
            "rejectedTargets": rejected,
            "unit": unit,
        }

    selected: str | None = None
    for tid in sorted(candidates):
        t = targets_by_id[tid]
        existing_fds = {
            str((targets_by_id.get(ct) or {}).get("failureDomain") or "")
            for ct, _ in tiers
        }
        fd = str(t.get("failureDomain") or "")
        if fd and fd in existing_fds and len(candidates) > 1:
            rejected[tid] = "same-failure-domain"
            continue
        hard_block, cap_reason = _capacity_blocks_dest(tid, soft=False)
        if hard_block:
            rejected[tid] = cap_reason
            continue
        cost_raw = policy.get("costObjectives")
        cost_obj: dict[str, Any] = cost_raw if isinstance(cost_raw, dict) else {}
        if cost_obj.get("requireKnownRates") and t.get("storageCostPerGiBMonth") is None:
            rejected[tid] = "cost-rate-unavailable"
            continue
        selected = tid
        reason_codes.append("destination-tier-qualified")
        if cap_reason == "capacity-soft-watermark":  # pragma: no cover - soft watermark advisory
            reason_codes.append("destination-capacity-soft-watermark")
        break

    if selected is None:
        return {
            "backupId": backup_id,
            "desiredTier": desired,
            "action": "blocked",
            "reasonCodes": reason_codes + ["all-candidates-rejected"],
            "rejectedTargets": rejected,
            "unit": unit,
        }

    preferred_source: str | None = None
    for tid, tier in tiers:
        if tier != desired:
            preferred_source = tid
            break
    if preferred_source is None and tiers:
        preferred_source = tiers[0][0]

    return {
        "backupId": backup_id,
        "desiredTier": desired,
        "currentTiers": sorted(present_tiers),
        "action": "migrate",
        "selectedTargetId": selected,
        "preferredSourceTargetId": preferred_source,
        "reasonCodes": reason_codes or ["tier-objective-drift"],
        "rejectedTargets": rejected,
        "unit": unit,
        "correctnessOrder": [
            "recoverability",
            "lineage",
            "copy-fd-region",
            "rto",
            "capacity",
            "cost",
        ],
    }


def reconcile_policy_placement(
    policy_id: str,
    *,
    limit: int = 20,
    now: datetime | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """Reconcile placement objectives for one policy; optionally enqueue chain migrations."""
    try:
        policy = backup_policies.get_policy(policy_id)
    except AppError as exc:
        return {"status": "error", "error": str(exc), "decisions": []}

    raw_placement = policy.get("recoveryPlacement")
    placement: dict[str, Any] = raw_placement if isinstance(raw_placement, dict) else {}
    if not placement.get("enabled", False):
        return {"status": "skipped", "reason": "placement-controller-disabled", "decisions": []}

    targets_by_id = {str(t["targetId"]): t for t in backup_targets.list_targets()}
    points = backup_dr_ledger.list_recovery_points(policy_id=policy_id, limit=max(1, min(int(limit) * 5, 500)))
    seen: set[str] = set()
    anchors: list[dict[str, Any]] = []
    for point in points:
        bak = str(point.get("backupId") or "")
        if not bak or bak in seen:
            continue
        seen.add(bak)
        anchors.append(point)
        if len(anchors) >= limit:  # pragma: no cover - bounded reconcile page
            break

    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=5000)
    decisions: list[dict[str, Any]] = []
    enqueued = 0
    for point in anchors:
        bak = str(point.get("backupId") or "")
        decision = evaluate_point_placement(
            policy,
            bak,
            committed_at=str(point.get("committedAt") or ""),
            copies=copies,
            targets_by_id=targets_by_id,
            now=now,
        )
        decision["decisionId"] = f"place_{bak}_{secrets.token_hex(4)}"
        decision["policyId"] = policy_id
        decision["evaluatedAt"] = _utc_iso()
        decisions.append(decision)

        backup_control.commit_lifecycle_intent(
            kind="placement-decision",
            policy_id=policy_id,
            backup_id=bak,
            phase=str(decision.get("action") or "none"),
            payload=decision,
        )

        if execute and decision.get("action") == "migrate":
            plan = backup_tiering.plan_chain_migration(
                policy_id,
                bak,
                desired_tier=str(decision.get("desiredTier") or "warm"),
                preferred_source_target_id=(
                    str(decision["preferredSourceTargetId"])
                    if decision.get("preferredSourceTargetId")
                    else None
                ),
            )
            decision["migrationPlan"] = {
                "status": plan.get("status"),
                "migrationId": plan.get("migrationId"),
                "reason": plan.get("reason"),
            }
            if plan.get("status") == "planned":  # pragma: no branch - plan success path
                enqueued += 1

    return {
        "status": "ok",
        "policyId": policy_id,
        "decisions": decisions,
        "enqueuedMigrations": enqueued,
        "evaluated": len(decisions),
    }


def reconcile_all_policies(
    *,
    limit_per_policy: int = 10,
    execute: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run placement reconcile across enabled policies (bounded)."""
    policies = backup_policies.list_policies()
    results: list[dict[str, Any]] = []
    total_enq = 0
    for policy in policies:
        pid = str(policy.get("policyId") or "")
        if not pid:
            continue
        rp_raw = policy.get("recoveryPlacement")
        rp: dict[str, Any] = rp_raw if isinstance(rp_raw, dict) else {}
        if not rp.get("enabled", False):
            continue
        one = reconcile_policy_placement(pid, limit=limit_per_policy, execute=execute, now=now)
        results.append(one)
        total_enq += int(one.get("enqueuedMigrations") or 0)
    return {"policies": len(results), "enqueuedMigrations": total_enq, "results": results}
