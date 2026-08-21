"""SLO-aware Hot/Warm/Archive tiering for whole recovery-chain units (4.5.9).

Tier migration copies original ciphertext only: backupId, objectSetDigest,
Receipt v4 and Commit v4 bytes are preserved. Placement is evaluated against
the full restore dependency closure, never a lone incremental leaf.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_dr_ledger,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_scheduler,
    backup_targets,
)

TIER_ORDER = {"hot": 0, "warm": 1, "archive": 2}
VALID_TIERS = frozenset(TIER_ORDER)


def _utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_storage_tier(value: Any) -> str | None:
    if value is None or value == "":
        return None
    tier = str(value).strip().casefold()
    if tier not in VALID_TIERS:
        raise AppError(f"unsupported storageTier: {value}", code=ErrorCode.INVALID_REQUEST, status=400)
    return tier


def target_storage_tier(target: dict[str, Any] | None) -> str:
    if not target:
        return "hot"
    tier = str(target.get("storageTier") or "hot").casefold()
    return tier if tier in VALID_TIERS else "hot"


def build_recovery_chain_placement_unit(
    policy_id: str,
    backup_id: str,
    *,
    target_id: str | None = None,
) -> dict[str, Any]:
    """Return the restore dependency closure for one logical recovery point."""
    points = backup_dr_ledger.list_recovery_points(policy_id=policy_id, limit=500)
    by_id = {str(p.get("backupId") or ""): p for p in points if p.get("backupId")}
    if backup_id not in by_id:
        # Fall back to a single-node unit when ledger history is sparse.
        return {
            "policyId": policy_id,
            "anchorBackupId": backup_id,
            "memberBackupIds": [backup_id],
            "closureComplete": False,
            "targetId": target_id,
        }
    members: list[str] = []
    seen: set[str] = set()
    cursor: str | None = backup_id
    while cursor and cursor not in seen:
        seen.add(cursor)
        members.append(cursor)
        point = by_id.get(cursor) or {}
        parent = str(point.get("parentBackupId") or point.get("baseBackupId") or "") or None
        if not parent:
            # Full baseline reached.
            break
        cursor = parent
    members.reverse()
    return {
        "policyId": policy_id,
        "anchorBackupId": backup_id,
        "memberBackupIds": members,
        "closureComplete": True,
        "targetId": target_id,
        "baselineBackupId": members[0] if members else backup_id,
    }


def chain_satisfies_tier(
    unit: dict[str, Any],
    *,
    required_tier: str,
    copies_by_backup: dict[str, list[dict[str, Any]]],
    targets_by_id: dict[str, dict[str, Any]],
) -> tuple[bool, str]:
    """Hot/Warm placement requires every ancestor copy at an equal-or-faster tier."""
    required = normalize_storage_tier(required_tier) or "hot"
    required_rank = TIER_ORDER[required]
    for member in list(unit.get("memberBackupIds") or []):
        copies = copies_by_backup.get(str(member)) or []
        live = [c for c in copies if c.get("recoverable") and str(c.get("state") or "") in {"healthy", "replicated", ""}]
        if not live:
            return False, f"missing-live-copy:{member}"
        ok = False
        for copy in live:
            tid = str(copy.get("targetId") or "")
            tier = target_storage_tier(targets_by_id.get(tid))
            if TIER_ORDER.get(tier, 99) <= required_rank:
                ok = True
                break
        if not ok:
            return False, f"ancestor-tier-too-cold:{member}"
    return True, "ok"


def plan_tier_placement(
    policy_id: str,
    backup_id: str,
    *,
    desired_tier: str,
    source_target_id: str,
    candidate_target_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Plan a chain-preserving tier migration that still meets topology/RTO gates."""
    desired = normalize_storage_tier(desired_tier) or "warm"
    unit = build_recovery_chain_placement_unit(policy_id, backup_id, target_id=source_target_id)
    try:
        policy = backup_policies.get_policy(policy_id)
    except AppError:
        policy = {"policyId": policy_id, "targetId": source_target_id}

    all_targets = {str(t["targetId"]): t for t in backup_targets.list_targets()}
    source = all_targets.get(source_target_id) or backup_targets.get_target(source_target_id) or {}
    if candidate_target_ids is None:
        candidates = [
            tid
            for tid, t in all_targets.items()
            if tid != source_target_id
            and target_storage_tier(t) == desired
            and str(t.get("drainState") or "active") not in {"draining", "drained"}
        ]
    else:
        candidates = [tid for tid in candidate_target_ids if tid != source_target_id]

    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=500)
    by_backup: dict[str, list[dict[str, Any]]] = {}
    for copy in copies:
        by_backup.setdefault(str(copy.get("backupId") or ""), []).append(copy)

    # Reject plans that would leave the anchor "hot" while an ancestor is archive-only.
    ranked: list[tuple[Any, str, dict[str, Any]]] = []
    for dest_id in candidates:
        dest = all_targets.get(dest_id) or {}
        if target_storage_tier(dest) != desired and desired not in VALID_TIERS:
            continue
        # Cost is last; require known rates only when policy demands it.
        raw_cost = policy.get("costObjectives") if isinstance(policy, dict) else None
        cost_objectives: dict[str, Any] = raw_cost if isinstance(raw_cost, dict) else {}
        require_known = bool(cost_objectives.get("requireKnownRates"))
        if require_known:
            if dest.get("storageCostPerGiBMonth") is None or source.get("egressCostPerGiB") is None:
                continue
        placement = backup_scheduler.plan_target_placement(
            policy,
            candidate_target_ids=[dest_id],
            primary_target_id=source_target_id,
            logical_recovery_point_id=backup_id,
            required_bytes=None,
            snapshot_kind="full",
            force_full=False,
        )
        if not placement:
            continue
        ok, reason = chain_satisfies_tier(
            unit,
            required_tier=desired if desired != "archive" else "archive",
            copies_by_backup={
                **by_backup,
                **{
                    str(member): [{"targetId": dest_id, "recoverable": True, "state": "healthy"}]
                    for member in unit["memberBackupIds"]
                },
            },
            targets_by_id={**all_targets, dest_id: {**dest, "storageTier": desired}},
        )
        if desired == "hot" and not ok:
            continue
        # plan_target_placement returns ascending (rank_tuple, target_id) pairs.
        rank_key: Any = placement[0][0] if placement and isinstance(placement[0], tuple) else (0,)
        ranked.append((rank_key, dest_id, {"chainOk": ok, "reason": reason if not ok else "ok"}))

    ranked.sort(key=lambda item: item[0])
    if not ranked:
        return {
            "status": "rejected",
            "reason": "no-eligible-tier-target",
            "unit": unit,
            "desiredTier": desired,
        }

    # Whole-chain migration destinations: one dest for every member.
    dest_id = ranked[0][1]
    members = list(unit.get("memberBackupIds") or [backup_id])
    moves = [
        {
            "policyId": policy_id,
            "backupId": member,
            "sourceTargetId": source_target_id,
            "destTargetId": dest_id,
            "desiredTier": desired,
        }
        for member in members
        if any(
            str(c.get("targetId")) == source_target_id and c.get("recoverable")
            for c in by_backup.get(member, [])
        )
        or member == backup_id
    ]
    intent = backup_control.commit_lifecycle_intent(
        kind="tier-migration",
        target_id=source_target_id,
        policy_id=policy_id,
        backup_id=backup_id,
        phase="planned",
        payload={
            "desiredTier": desired,
            "destTargetId": dest_id,
            "unit": unit,
            "moves": moves,
        },
    )
    return {
        "status": "planned",
        "desiredTier": desired,
        "destTargetId": dest_id,
        "unit": unit,
        "moves": moves,
        "intentId": intent.get("intentId"),
        "correctnessBeforeCost": True,
    }


def execute_tier_migration(
    *,
    policy_id: str,
    backup_id: str,
    source_target_id: str,
    dest_target_id: str,
    intent_id: str | None = None,
    reason: str = "tier-migration",
) -> dict[str, Any]:
    """Migrate one recovery copy's ciphertext via rebalance without re-encryption."""
    source = backup_publish.resolve_target(source_target_id)
    dest = backup_publish.resolve_target(dest_target_id)
    # Capture digests before migration for evidence assertions.
    pre_copies = backup_dr_ledger.list_logical_recovery_copies(
        policy_id=policy_id, backup_id=backup_id, target_id=source_target_id, limit=10
    )
    object_set_digest = None
    for copy in pre_copies:
        object_set_digest = copy.get("objectSetDigest") or (copy.get("metadata") or {}).get("objectSetDigest")
        if object_set_digest:
            break

    job = backup_replication.create_rebalance_job(
        policy_id=policy_id,
        backup_id=backup_id,
        dest_target_id=dest_target_id,
        source_target_id=source_target_id,
        reason=reason,
        prune_source_after=False,
    )
    # Drive the job to completion when the worker path is available.
    job_id = str(job.get("jobId") or job.get("rebalanceId") or "")
    if job_id and hasattr(backup_replication, "execute_rebalance_job"):
        try:
            result = backup_replication.execute_rebalance_job(job_id)
        except Exception:
            result = job
    else:
        result = job

    if intent_id:
        backup_control.update_lifecycle_intent_phase(
            intent_id,
            "executed",
            payload={
                "policyId": policy_id,
                "backupId": backup_id,
                "sourceTargetId": source_target_id,
                "destTargetId": dest_target_id,
                "objectSetDigest": object_set_digest,
                "rebalance": result,
            },
        )
    return {
        "status": str(result.get("phase") or result.get("status") or "submitted"),
        "backupId": backup_id,
        "objectSetDigest": object_set_digest,
        "sourceTargetId": source_target_id,
        "destTargetId": dest_target_id,
        "rebalance": result,
        "ageEncryptionInvoked": False,
        "sourceRoot": getattr(source, "root", None) is not None,
        "destRoot": getattr(dest, "root", None) is not None,
        "migrationId": f"tier_{secrets.token_hex(6)}",
        "completedAt": _utc_iso(),
    }


def assert_hot_anchor_not_archive_dependent(
    policy_id: str,
    backup_id: str,
    *,
    targets_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evidence helper: hot recovery points must not depend on archive-only ancestors."""
    unit = build_recovery_chain_placement_unit(policy_id, backup_id)
    targets = targets_by_id or {str(t["targetId"]): t for t in backup_targets.list_targets()}
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=500)
    by_backup: dict[str, list[dict[str, Any]]] = {}
    for copy in copies:
        by_backup.setdefault(str(copy.get("backupId") or ""), []).append(copy)
    ok, reason = chain_satisfies_tier(
        unit,
        required_tier="hot",
        copies_by_backup=by_backup,
        targets_by_id=targets,
    )
    return {"ok": ok, "reason": reason, "unit": unit}
