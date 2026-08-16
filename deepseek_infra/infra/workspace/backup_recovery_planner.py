"""Recovery Plan abstraction — ledger-only, zero remote I/O (4.5.2).

Selects a logical recovery point and ranks compatible replica targets with
explicit reason codes. Automatic target failover uses the frozen candidate list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_policies,
    backup_recovery_class,
    backup_targets,
)

PLAN_SCHEMA_VERSION = 1
DEFAULT_MAX_FAILOVERS = 3

# Phases where automatic source-target failover is still allowed.
FAILOVER_ALLOWED_PHASES = frozenset(
    {
        "created",
        "preflight",
        "preflighted",
        "fetching",
        "fetching-controls",
        "controls-fetched",
        "fetching-selected-components",
        "components-fetched",
        "fetching-chain",
        "chain-fetched",
        "fetched",
        "planning-projection",
        "preview-planned",
    }
)

# After these phases, silent failover is forbidden.
FAILOVER_FORBIDDEN_PHASES = frozenset(
    {
        "preparing",
        "prepared",
        "committing",
        "recovery-required",
        "decrypting-controls",
        "decrypting-components",
        "decrypting-chain",
        "materializing",
        "verified",
        "complete",
        "aborted",
        "rolled-back",
        "failed",
    }
)

RETRYABLE_FAILOVER_REASONS = frozenset(
    {
        "network-unavailable",
        "remote-5xx",
        "remote-object-missing",
        "remote-object-corrupt",
        "target-health-failure",
        "hold-acquire-failed",
        "transfer-failed",
    }
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _target_kind(target_id: str) -> str:
    if target_id == "managed-local":
        return "managed-local"
    try:
        target = backup_targets.get_target(target_id)
    except Exception:
        target = None
    if isinstance(target, dict):
        kind = str(target.get("kind") or "").strip().lower()
        if kind:
            return kind
    return "filesystem"


def _target_broken(target_id: str) -> bool:
    ev = backup_dr_ledger.get_target_evidence(target_id)
    if ev is None:
        return False
    status = str(ev.get("status") or "")
    return status in {"error", "failed", "broken"} or ev.get("scheduledReady") is False


def _audit_failed(target_id: str) -> bool:
    audit = backup_dr_ledger.get_latest_audit_evidence(target_id)
    if audit is None:
        return False
    return str(audit.get("status") or "") in {"failed", "error"}


def plan_recovery(
    *,
    policy_id: str,
    backup_id: str | None = None,
    restore_selection: dict[str, Any] | None = None,
    preferred_target_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a recovery plan from the local DR Evidence Ledger only (zero remote I/O)."""
    del restore_selection  # reserved for future contributor selection; no remote I/O
    current = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    try:
        policy = backup_policies.get_policy(policy_id)
    except AppError:
        policy = {"policyId": policy_id, "targetId": "managed-local", "replication": {"enabled": False}}

    primary_target = str(policy.get("targetId") or "managed-local")
    replication = policy.get("replication") if isinstance(policy.get("replication"), dict) else {}
    replica_targets = []
    if isinstance(replication, dict) and replication.get("enabled"):
        for entry in list(replication.get("targets") or []):
            if isinstance(entry, dict) and entry.get("targetId"):
                replica_targets.append(str(entry["targetId"]))

    # Resolve logical recovery point
    if backup_id:
        points = backup_dr_ledger.list_recovery_points(policy_id=policy_id, limit=500)
        match = next((p for p in points if str(p.get("backupId")) == backup_id and p.get("recoverable")), None)
        if match is None:
            copies_for_backup = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id, limit=50)
            copy_match = next((c for c in copies_for_backup if str(c.get("backupId")) == backup_id and c.get("recoverable")), None)
            if copy_match is None:
                raise AppError(
                    "No authenticated recoverable point for backupId under policy",
                    code=ErrorCode.NOT_FOUND,
                    status=404,
                )
            logical_backup_id = backup_id
            object_set_digest = str(copy_match.get("objectSetDigest") or "")
            committed_at = str(copy_match.get("committedAt") or "")
            chain_length = 1
            logical_bytes = int((copy_match.get("metadata") or {}).get("logicalBytes") or (copy_match.get("metadata") or {}).get("size") or 0)
            storage_protocol = str((copy_match.get("metadata") or {}).get("storageProtocol") or "object-set-v1")
        else:
            logical_backup_id = backup_id
            object_set_digest = str((match.get("metadata") or {}).get("objectSetDigest") or match.get("chainDigest") or "")
            committed_at = str(match.get("committedAt") or "")
            chain_length = int(match.get("chainLength") or 1)
            logical_bytes = int(match.get("logicalBytes") or 0)
            storage_protocol = str(match.get("storageProtocol") or "object-set-v1")
    else:
        latest, chain = backup_dr_ledger.get_latest_recoverable_point(primary_target, policy_id, now=current)
        if latest is None:
            # Try any copy under policy via logical copies
            copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=50)
            recoverable_copies = [c for c in copies if c.get("recoverable")]
            if not recoverable_copies:
                raise AppError("No policy recovery point available", code=ErrorCode.NOT_FOUND, status=404)
            head = recoverable_copies[0]
            logical_backup_id = str(head.get("backupId") or "")
            object_set_digest = str(head.get("objectSetDigest") or "")
            committed_at = str(head.get("committedAt") or "")
            chain_length = 1
            logical_bytes = 0
            storage_protocol = "object-set-v1"
        else:
            logical_backup_id = str(latest.get("backupId") or "")
            object_set_digest = str(latest.get("objectSetDigest") or latest.get("chainDigest") or "")
            committed_at = str(latest.get("committedAt") or "")
            chain_length = int(latest.get("chainLength") or len(chain) or 1)
            logical_bytes = int(latest.get("logicalBytes") or 0)
            storage_protocol = str(latest.get("storageProtocol") or "object-set-v1")

    copies = backup_dr_ledger.list_logical_recovery_copies(
        policy_id=policy_id,
        backup_id=logical_backup_id,
        object_set_digest=object_set_digest or None,
    )
    if not copies:
        # Fall back to per-target recovery_points rows for same backupId
        all_pts = backup_dr_ledger.list_recovery_points(policy_id=policy_id, limit=500)
        copies = [
            {
                "targetId": p.get("targetId"),
                "policyId": p.get("policyId"),
                "backupId": p.get("backupId"),
                "objectSetDigest": (p.get("metadata") or {}).get("objectSetDigest") if isinstance(p.get("metadata"), dict) else None,
                "committedAt": p.get("committedAt"),
                "recoverable": bool(p.get("recoverable")),
                "role": "primary" if p.get("targetId") == primary_target else "replica",
            }
            for p in all_pts
            if str(p.get("backupId")) == logical_backup_id and p.get("recoverable")
        ]

    candidate_ids: list[str] = []
    seen: set[str] = set()
    for tid in [primary_target, *replica_targets, *[str(c.get("targetId") or "") for c in copies]]:
        if tid and tid not in seen:
            seen.add(tid)
            candidate_ids.append(tid)

    ordered: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for target_id in candidate_ids:
        reasons: list[str] = []
        rank_reasons: list[str] = []
        copy = next((c for c in copies if str(c.get("targetId")) == target_id), None)
        if copy is None or not copy.get("recoverable"):
            # Still allow primary if ledger has recoverable point under target+policy
            pt, _ = backup_dr_ledger.get_latest_recoverable_point(target_id, policy_id, now=current)
            if pt is None or (backup_id and str(pt.get("backupId")) != logical_backup_id):
                # Check any recoverable row for this backup on target
                pts = [
                    p
                    for p in backup_dr_ledger.list_recovery_points(target_id=target_id, policy_id=policy_id, limit=100)
                    if str(p.get("backupId")) == logical_backup_id and p.get("recoverable")
                ]
                if not pts:
                    rejected.append({"targetId": target_id, "reasons": ["not-recoverable-on-target"]})
                    continue
            reasons.append("recoverable-on-target")
        else:
            reasons.append("logical-copy-recoverable")

        if _target_broken(target_id):
            rejected.append({"targetId": target_id, "reasons": ["target-known-broken"]})
            continue
        if _audit_failed(target_id):
            rejected.append({"targetId": target_id, "reasons": ["audit-known-failed"]})
            continue

        kind = _target_kind(target_id)
        # Compatible storage protocol: object-set and whole-age both ok if ledger says recoverable
        reasons.append(f"target-kind:{kind}")
        reasons.append("credential-compatible")

        tev = backup_dr_ledger.get_target_evidence(target_id)
        is_healthy = bool(tev and tev.get("status") == "ok")
        if is_healthy:
            rank_reasons.append("target-health-ok")

        is_preferred = bool(preferred_target_id and target_id == preferred_target_id)
        if is_preferred:
            rank_reasons.append("explicit-preference")

        is_primary = bool(target_id == primary_target)
        if is_primary:
            rank_reasons.append("primary-target")

        scrub = backup_dr_ledger.get_latest_scrub_outcome(target_id, policy_id, now=current)
        scrub_ok = bool(scrub and scrub.get("result") == "success")
        if scrub_ok:
            rank_reasons.append("scrub-fresh")

        drill = backup_dr_ledger.get_latest_drill_outcome(target_id, policy_id, now=current)
        drill_ok = bool(drill and drill.get("result") == "success")
        if drill_ok:
            rank_reasons.append("drill-fresh")

        rclass = backup_recovery_class.classify_recovery(
            target_kind=kind,
            storage_protocol=storage_protocol,
            logical_bytes=logical_bytes,
            chain_length=chain_length,
        )
        rto = backup_recovery_class.calibrate_rto(
            target_id=target_id,
            logical_bytes=logical_bytes,
            chain_length=chain_length,
            recovery_class=rclass,
        )
        if rto.get("status") == "calibrated":
            rank_reasons.append("matching-rto-evidence")

        # Deterministic Lexicographic Ranking Key
        # 1. Hard health class: healthy (0) vs degraded/unknown (1)
        # 2. Explicit preference: preferred (0) vs unselected (1)
        # 3. Cache/Primary advantage: primary/local (0) vs replica (1)
        # 4. Drill freshness: fresh (0) vs none (1)
        # 5. Scrub freshness: fresh (0) vs none (1)
        # 6. Calibrated RTO P50 seconds (lower is faster)
        # 7. Stable targetId string tiebreak
        rto_seconds = int(rto.get("p50Seconds") or rto.get("estimatedSeconds") or 999999)
        lex_key = (
            0 if is_healthy else 1,
            0 if is_preferred else 1,
            0 if is_primary or target_id == "managed-local" else 1,
            0 if drill_ok else 1,
            0 if scrub_ok else 1,
            rto_seconds,
            str(target_id),
        )

        score = (
            (100 if is_preferred else 0)
            + (40 if is_primary else 0)
            + (20 if is_healthy else 0)
            + (15 if scrub_ok else 0)
            + (15 if drill_ok else 0)
            + (10 if rto.get("status") == "calibrated" else 0)
        )

        ordered.append(
            {
                "targetId": target_id,
                "role": "primary" if target_id == primary_target else "replica",
                "kind": kind,
                "score": score,
                "rankKey": lex_key,
                "filterReasons": reasons,
                "rankReasons": rank_reasons,
                "rtoEstimate": rto,
                "estimatedBytes": logical_bytes,
            }
        )

    ordered.sort(key=lambda item: item["rankKey"])
    if not ordered:
        raise AppError("No compatible recovery target candidates", code=ErrorCode.NOT_FOUND, status=404)

    selected = ordered[0]
    return {
        "schemaVersion": PLAN_SCHEMA_VERSION,
        "policyId": policy_id,
        "logicalRecoveryPoint": {
            "backupId": logical_backup_id,
            "policyId": policy_id,
            "objectSetDigest": object_set_digest or None,
            "committedAt": committed_at or None,
            "chainLength": chain_length,
            "logicalBytes": logical_bytes,
            "storageProtocol": storage_protocol,
        },
        "selectedTargetId": selected["targetId"],
        "orderedCandidates": ordered,
        "rejectedCandidates": rejected,
        "selectionReasons": list(selected.get("rankReasons") or []) + list(selected.get("filterReasons") or []),
        "estimatedBytes": logical_bytes,
        "matchingRtoEvidence": selected.get("rtoEstimate"),
        "maxFailovers": DEFAULT_MAX_FAILOVERS,
        "plannedAt": _utc_iso(current),
        "source": "dr-evidence-ledger",
        "remoteIo": False,
    }


def failover_allowed(phase: str) -> bool:
    phase = str(phase or "")
    if phase in FAILOVER_FORBIDDEN_PHASES:
        return False
    if phase in FAILOVER_ALLOWED_PHASES:
        return True
    # Conservative default: only allow known early phases
    return False


def select_failover_target(
    plan: dict[str, Any],
    *,
    current_target_id: str,
    attempted_targets: list[str] | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any] | None:
    """Pick the next frozen candidate for failover, preventing A→B→A loops."""
    if failure_reason and failure_reason not in RETRYABLE_FAILOVER_REASONS:
        return None
    attempted = set(attempted_targets or [])
    attempted.add(current_target_id)
    max_failovers = int(plan.get("maxFailovers") or DEFAULT_MAX_FAILOVERS)
    if len(attempted) - 1 >= max_failovers:
        return None
    for candidate in list(plan.get("orderedCandidates") or []):
        tid = str(candidate.get("targetId") or "")
        if not tid or tid in attempted:
            continue
        return candidate
    return None
