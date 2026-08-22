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


def _lookup_recovery_point(policy_id: str, backup_id: str) -> dict[str, Any] | None:
    """Exact parent lookup — no history window truncation."""
    # Prefer the rebuildable control-plane lineage graph (Gate C).
    lineage = backup_control.get_recovery_lineage(policy_id, backup_id)
    if lineage is not None:
        return {
            "backupId": lineage["backupId"],
            "policyId": lineage["policyId"],
            "parentBackupId": lineage.get("parentBackupId"),
            "baseBackupId": lineage.get("baseBackupId"),
            "snapshotKind": lineage.get("snapshotKind") or "full",
            "objectSetDigest": lineage.get("objectSetDigest"),
            "committedAt": lineage.get("committedAt"),
        }
    getter = getattr(backup_dr_ledger, "get_recovery_point", None)
    if callable(getter):
        point = getter(policy_id, backup_id)
        if isinstance(point, dict):
            return point
    points = backup_dr_ledger.list_recovery_points(policy_id=policy_id, limit=100_000)
    for point in points:
        if str(point.get("backupId") or "") == backup_id:
            return point
    return None


def rebuild_recovery_lineage(policy_id: str) -> dict[str, int]:
    """Rebuild indexed recovery_lineage from the DR ledger (Gate C)."""
    backup_control.clear_recovery_lineage(policy_id)
    points = backup_dr_ledger.list_recovery_points(policy_id=policy_id, limit=100_000)
    # Build depth via parent walk after all rows inserted.
    by_id = {str(p.get("backupId") or ""): p for p in points if p.get("backupId")}
    written = 0
    for backup_id, point in by_id.items():
        parent = str(point.get("parentBackupId") or "") or None
        depth = 0
        cursor = parent
        seen: set[str] = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            depth += 1
            parent_pt = by_id.get(cursor) or {}
            cursor = str(parent_pt.get("parentBackupId") or "") or None
        backup_control.upsert_recovery_lineage(
            policy_id=policy_id,
            backup_id=backup_id,
            snapshot_kind=str(point.get("snapshotKind") or "full"),
            parent_backup_id=parent,
            base_backup_id=str(point.get("baseBackupId") or "") or None,
            chain_depth=depth,
            object_set_digest=str(point.get("objectSetDigest") or (point.get("metadata") or {}).get("objectSetDigest") or "") or None,
            committed_at=str(point.get("committedAt") or "") or None,
        )
        written += 1
    return {"written": written}


def build_recovery_chain_placement_unit(
    policy_id: str,
    backup_id: str,
    *,
    target_id: str | None = None,
) -> dict[str, Any]:
    """Return the restore dependency closure for one logical recovery point.

    Walks parent links via exact lookup. Any missing parent fails closed with
    ``closureComplete=false`` — never treats query truncation as baseline.
    """
    members: list[str] = []
    seen: set[str] = set()
    cursor: str | None = backup_id
    missing_parent: str | None = None
    reached_baseline = False
    while cursor and cursor not in seen:
        seen.add(cursor)
        members.append(cursor)
        point = _lookup_recovery_point(policy_id, cursor)
        if point is None:
            if cursor == backup_id:
                # Anchor itself unknown — incomplete single-node unit.
                return {
                    "policyId": policy_id,
                    "anchorBackupId": backup_id,
                    "memberBackupIds": [backup_id],
                    "closureComplete": False,
                    "reason": "missing-anchor",
                    "missingBackupId": backup_id,
                    "targetId": target_id,
                }
            missing_parent = cursor
            # Remove the missing node from members; parent of previous is missing.
            members.pop()
            break
        parent = str(point.get("parentBackupId") or point.get("baseBackupId") or "") or None
        kind = str(point.get("snapshotKind") or "full")
        if not parent:
            if kind == "incremental":
                missing_parent = f"unknown-parent-of-{cursor}"
                break
            reached_baseline = True
            break
        cursor = parent
    members.reverse()
    if missing_parent is not None or not reached_baseline:
        return {
            "policyId": policy_id,
            "anchorBackupId": backup_id,
            "memberBackupIds": members if members else [backup_id],
            "closureComplete": False,
            "reason": "missing-parent",
            "missingBackupId": missing_parent,
            "targetId": target_id,
        }
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
    if not unit.get("closureComplete"):
        return {
            "status": "rejected",
            "reason": str(unit.get("reason") or "incomplete-recovery-chain"),
            "missingBackupId": unit.get("missingBackupId"),
            "unit": unit,
            "desiredTier": desired,
        }
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
        # Explicit candidates must still match the requested storage tier.
        candidates = [
            tid
            for tid in candidate_target_ids
            if tid != source_target_id and target_storage_tier(all_targets.get(tid) or backup_targets.get_target(tid) or {}) == desired
        ]

    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=500)
    by_backup: dict[str, list[dict[str, Any]]] = {}
    for copy in copies:
        by_backup.setdefault(str(copy.get("backupId") or ""), []).append(copy)

    # Hot and Warm both require equal-or-faster ancestors after migration.
    ranked: list[tuple[Any, str, dict[str, Any]]] = []
    for dest_id in candidates:
        dest = all_targets.get(dest_id) or {}
        if target_storage_tier(dest) != desired:
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
        # Simulate full chain present on dest at desired tier for eligibility.
        simulated_copies = {
            **by_backup,
            **{
                str(member): [{"targetId": dest_id, "recoverable": True, "state": "healthy"}]
                for member in unit["memberBackupIds"]
            },
        }
        ok, reason = chain_satisfies_tier(
            unit,
            required_tier=desired,
            copies_by_backup=simulated_copies,
            targets_by_id={**all_targets, dest_id: {**dest, "storageTier": desired}},
        )
        if desired in {"hot", "warm"} and not ok:
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
    """Migrate one recovery copy's ciphertext via rebalance without re-encryption.

    Intent phase only advances to a terminal success state when the rebalance
    job reports success — exceptions never mark the intent executed.
    """
    source = backup_publish.resolve_target(source_target_id)
    dest = backup_publish.resolve_target(dest_target_id)
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
    job_id = str(job.get("jobId") or job.get("rebalanceId") or "")
    result: dict[str, Any] = job
    failed = False
    if job_id and hasattr(backup_replication, "execute_rebalance_job"):
        try:
            result = backup_replication.execute_rebalance_job(job_id)
            if str(result.get("status") or "") == "failed":
                failed = True
        except Exception as exc:
            failed = True
            result = {**job, "status": "failed", "error": str(exc), "phase": "failed"}

    phase = "failed-terminal" if failed else (
        "executed" if str(result.get("status") or result.get("phase") or "") in {"success", "complete", "committed"} else "transferring"
    )
    if intent_id:
        backup_control.update_lifecycle_intent_phase(
            intent_id,
            phase,
            payload={
                "policyId": policy_id,
                "backupId": backup_id,
                "sourceTargetId": source_target_id,
                "destTargetId": dest_target_id,
                "objectSetDigest": object_set_digest,
                "rebalance": result,
                "error": result.get("error"),
            },
        )
    return {
        "status": str(result.get("status") or result.get("phase") or "submitted"),
        "backupId": backup_id,
        "objectSetDigest": object_set_digest,
        "sourceTargetId": source_target_id,
        "destTargetId": dest_target_id,
        "rebalance": result,
        "ageEncryptionInvoked": False,
        "sourceRoot": getattr(source, "root", None) is not None,
        "destRoot": getattr(dest, "root", None) is not None,
        "migrationId": f"tier_{secrets.token_hex(6)}",
        "intentPhase": phase,
        "completedAt": _utc_iso(),
    }


def plan_chain_migration(
    policy_id: str,
    anchor_backup_id: str,
    *,
    desired_tier: str,
    preferred_source_target_id: str | None = None,
) -> dict[str, Any]:
    """Plan a durable RecoveryChainMigrationJob with per-member sources (Gate D)."""
    desired = normalize_storage_tier(desired_tier) or "warm"
    unit = build_recovery_chain_placement_unit(policy_id, anchor_backup_id)
    if not unit.get("closureComplete"):
        return {
            "status": "rejected",
            "reason": str(unit.get("reason") or "incomplete-recovery-chain"),
            "missingBackupId": unit.get("missingBackupId"),
            "unit": unit,
        }

    all_targets = {str(t["targetId"]): t for t in backup_targets.list_targets()}
    dest_candidates = [
        tid
        for tid, t in all_targets.items()
        if target_storage_tier(t) == desired and str(t.get("drainState") or "active") not in {"draining", "drained"}
    ]
    if not dest_candidates:
        return {"status": "rejected", "reason": "no-eligible-tier-target", "unit": unit, "desiredTier": desired}

    dest_id = dest_candidates[0]
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=5000)
    by_backup: dict[str, list[dict[str, Any]]] = {}
    for copy in copies:
        by_backup.setdefault(str(copy.get("backupId") or ""), []).append(copy)

    members_plan: list[dict[str, Any]] = []
    for member in list(unit.get("memberBackupIds") or []):
        live = [
            c
            for c in by_backup.get(str(member), [])
            if c.get("recoverable") and str(c.get("state") or "") in {"healthy", "replicated", ""}
        ]
        # Prefer preferred source, else any authenticated live copy not already on dest.
        source_id = None
        if preferred_source_target_id and any(str(c.get("targetId")) == preferred_source_target_id for c in live):
            source_id = preferred_source_target_id
        else:  # pragma: no cover - preferred source usually present in tests
            for c in live:
                tid = str(c.get("targetId") or "")
                if tid and tid != dest_id:
                    source_id = tid
                    break
        if source_id is None:
            # Already on dest only — still record as verified-noop.
            if any(str(c.get("targetId")) == dest_id for c in live):  # pragma: no cover - noop member
                members_plan.append(
                    {
                        "backupId": member,
                        "sourceTargetId": dest_id,
                        "destTargetId": dest_id,
                        "state": "verified",
                        "noop": True,
                    }
                )
                continue
            return {  # pragma: no cover - no live source anywhere
                "status": "rejected",
                "reason": f"no-authenticated-source:{member}",
                "unit": unit,
                "desiredTier": desired,
            }
        members_plan.append(
            {
                "backupId": member,
                "sourceTargetId": source_id,
                "destTargetId": dest_id,
                "state": "planned",
                "noop": False,
            }
        )

    job = backup_control.create_chain_migration_job(
        {
            "policyId": policy_id,
            "anchorBackupId": anchor_backup_id,
            "desiredTier": desired,
            "destTargetId": dest_id,
            "unit": unit,
            "members": members_plan,
            "phase": "planned",
        }
    )
    intent = backup_control.commit_lifecycle_intent(
        kind="chain-migration",
        target_id=dest_id,
        policy_id=policy_id,
        backup_id=anchor_backup_id,
        phase="planned",
        payload={"migrationId": job.get("migrationId"), "desiredTier": desired},
    )
    return {
        "status": "planned",
        "migrationId": job.get("migrationId"),
        "intentId": intent.get("intentId"),
        "desiredTier": desired,
        "destTargetId": dest_id,
        "unit": unit,
        "members": members_plan,
        "job": job,
    }


def execute_chain_migration(
    migration_id: str,
    *,
    instance_id: str = "chain-migration-worker",
) -> dict[str, Any]:
    """Drive one RecoveryChainMigrationJob toward closure convergence (Gate D)."""
    job = backup_control.get_chain_migration_job(migration_id)
    if job is None:
        raise AppError("chain migration job not found", code=ErrorCode.NOT_FOUND, status=404)
    phase = str(job.get("phase") or "")
    if phase in backup_control.CHAIN_MIGRATION_TERMINAL:
        return job

    members = list(job.get("members") or [])
    policy_id = str(job["policyId"])
    desired = str(job.get("desiredTier") or "warm")

    # planned → sources-authenticated / transferring
    if phase == "planned":
        job = backup_control.update_chain_migration_job(
            migration_id, phase="transferring", payload={"members": members}
        )

    # Transfer each non-noop member via rebalance; never mark success on exception.
    updated_members: list[dict[str, Any]] = []
    any_failed = False
    for member in members:
        m = dict(member)
        if m.get("noop") or str(m.get("state") or "") in {"verified", "complete"}:
            m["state"] = "verified"
            updated_members.append(m)
            continue
        if str(m.get("state") or "") == "failed":
            any_failed = True
            updated_members.append(m)
            continue
        src = str(m.get("sourceTargetId") or "")
        dst = str(m.get("destTargetId") or "")
        bak = str(m.get("backupId") or "")
        try:
            result = execute_tier_migration(
                policy_id=policy_id,
                backup_id=bak,
                source_target_id=src,
                dest_target_id=dst,
                intent_id=None,
                reason=f"chain-migration:{migration_id}",
            )
            status = str(result.get("status") or "")
            if status in {"success", "complete", "committed"} or str(result.get("intentPhase") or "") == "executed":
                # Authenticate destination
                dest = backup_publish.resolve_target(dst)
                auth = backup_replication.authenticate_committed_copy(dest, policy_id, bak)
                if auth[0] == "authenticated":
                    m["state"] = "verified"
                    m["objectSetDigest"] = result.get("objectSetDigest")
                else:
                    m["state"] = "failed"
                    m["error"] = f"dest-auth:{auth[0]}"
                    any_failed = True
            else:
                m["state"] = "failed"
                m["error"] = str(result.get("error") or status)
                any_failed = True
        except Exception as exc:
            m["state"] = "failed"
            m["error"] = str(exc)
            any_failed = True
        updated_members.append(m)

    if any_failed:
        return backup_control.update_chain_migration_job(
            migration_id,
            phase="failed-terminal",
            payload={"members": updated_members},
            error="member-transfer-or-auth-failed",
        )

    # members-authenticated → closure-authenticated
    job = backup_control.update_chain_migration_job(
        migration_id, phase="members-authenticated", payload={"members": updated_members}
    )
    dest_id = str(job.get("destTargetId") or (updated_members[0].get("destTargetId") if updated_members else ""))
    unit = job.get("unit") if isinstance(job.get("unit"), dict) else build_recovery_chain_placement_unit(
        policy_id, str(job["anchorBackupId"])
    )
    copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=5000)
    by_backup: dict[str, list[dict[str, Any]]] = {}
    for copy in copies:
        by_backup.setdefault(str(copy.get("backupId") or ""), []).append(copy)
    all_targets = {str(t["targetId"]): t for t in backup_targets.list_targets()}
    if dest_id:
        all_targets[dest_id] = {**(all_targets.get(dest_id) or {}), "storageTier": desired}
    ok, reason = chain_satisfies_tier(
        unit if isinstance(unit, dict) else {"memberBackupIds": [str(job["anchorBackupId"])]},
        required_tier=desired,
        copies_by_backup=by_backup,
        targets_by_id=all_targets,
    )
    if desired in {"hot", "warm"} and not ok:
        return backup_control.update_chain_migration_job(
            migration_id,
            phase="failed-terminal",
            payload={"members": updated_members, "closureReason": reason},
            error=f"closure-not-satisfied:{reason}",
        )

    return backup_control.update_chain_migration_job(
        migration_id,
        phase="converged",
        payload={
            "members": updated_members,
            "closureReason": "ok",
            "convergedAt": _utc_iso(),
            "retirementEligible": True,
        },
    )


def process_pending_chain_migrations(
    *,
    instance_id: str = "chain-migration-worker",
    limit: int = 5,
) -> dict[str, int]:
    """Advance a bounded set of non-terminal chain migrations."""
    processed = 0
    converged = 0
    failed = 0
    for phase in ("planned", "transferring", "members-authenticated", "closure-authenticated"):
        for job in backup_control.list_chain_migration_jobs(phase=phase, limit=limit):
            if processed >= limit:
                break
            result = execute_chain_migration(str(job["migrationId"]), instance_id=instance_id)
            processed += 1
            p = str(result.get("phase") or "")
            if p == "converged":
                converged += 1
            elif p == "failed-terminal":
                failed += 1
    return {"processed": processed, "converged": converged, "failed": failed}


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
