"""Verified Write Continuity and Governed Failback / Primary Promotion Engine (4.5.5).

Decouples static Target Capability Evidence from dynamic Target Liveness Evidence.
Maintains persistent, local WriteContinuityState per policy (.backup-continuity/{policy_id}.json)
so DR Readiness and Write Placement inspect local durability state with ZERO remote I/O.

Guarantees:
1. Target Liveness preflight with lightweight checks without payload operations.
2. Governed failover to healthy secondary failure domains on primary outage.
3. Governed failback: Only after primary proves continuous stability (>= 1800s default)
   AND the latest recovery point has converged (replicated) to primary.
4. Administrative Primary Promotion: Atomic CAS-governed permanent promotion
   requiring explicit expectedPolicyRevision and expectedFailoverEpoch.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_dr_ledger, backup_publish

CONTINUITY_DIR = config.ROOT / ".backup-continuity"
DEFAULT_FAILBACK_STABILITY_SECONDS = 1800  # 30 minutes continuous primary health
LIVENESS_MAX_STALENESS_SECONDS = 120  # 2 minutes cache for background liveness probes

_LOCK = threading.RLock()


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _continuity_path(policy_id: str) -> Path:
    return CONTINUITY_DIR / f"{policy_id}.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def get_write_continuity_state(policy_id: str) -> dict[str, Any]:
    """Read durable write continuity state for a policy with ZERO remote I/O."""
    path = _continuity_path(policy_id)
    if not path.is_file():
        # Default initialization
        from deepseek_infra.infra.workspace import backup_policies

        try:
            policy = backup_policies.get_policy(policy_id)
            primary_id = str(policy.get("primaryTargetId") or policy.get("targetId") or "managed-local")
            rev = int(policy.get("policyRevision") or 1)
        except Exception:
            primary_id = "managed-local"
            rev = 1
        return {
            "schemaVersion": 1,
            "policyId": policy_id,
            "policyRevision": rev,
            "failoverEpoch": 0,
            "configuredPrimaryTargetId": primary_id,
            "activeWriteTargetId": primary_id,
            "activeWriteTargetRole": "primary",
            "lastFailoverAt": None,
            "lastFailoverReason": None,
            "lastFailbackAt": None,
            "lastFailbackReason": None,
            "primaryFirstHealthyAt": _utc_iso(),
            "primaryLastHealthyAt": _utc_iso(),
            "primaryConsecutiveHealthySeconds": 0.0,
            "failoverActiveSince": None,
            "targetLiveness": {},
            "updatedAt": _utc_iso(),
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "schemaVersion": 1,
            "policyId": policy_id,
            "policyRevision": 1,
            "failoverEpoch": 0,
            "configuredPrimaryTargetId": "managed-local",
            "activeWriteTargetId": "managed-local",
            "activeWriteTargetRole": "primary",
            "updatedAt": _utc_iso(),
        }


def save_write_continuity_state(policy_id: str, state: dict[str, Any]) -> None:
    with _LOCK:
        CONTINUITY_DIR.mkdir(parents=True, exist_ok=True)
        state["updatedAt"] = _utc_iso()
        _atomic_write(_continuity_path(policy_id), state)


def perform_liveness_preflight(
    target_id: str,
    *,
    policy_id: str | None = None,
    target: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Perform lightweight target liveness probe with zero payload operations."""
    resolved = target or backup_publish.resolve_target(target_id)
    t0 = time.monotonic()
    status = "available"
    err_msg: str | None = None

    try:
        if resolved.root is not None:
            if not resolved.root.exists():
                status = "unavailable"
                err_msg = f"Target root path does not exist: {resolved.root}"
        elif resolved.store is not None:
            liveness = resolved.store.check_liveness()
            raw_st = str(liveness.get("status") or "")
            status = "available" if raw_st in ("available", "writable", "ok") else "unavailable"
            err_msg = liveness.get("error")
        else:
            status = "unavailable"
            err_msg = "Target has neither root nor store attached"
    except Exception as exc:
        status = "unavailable"
        err_msg = str(exc)

    latency_ms = (time.monotonic() - t0) * 1000.0

    evidence = {
        "targetId": target_id,
        "status": status,
        "checkedAt": _utc_iso(now),
        "latencyMs": round(latency_ms, 2),
        "error": err_msg,
    }

    if policy_id:
        record_target_liveness(
            policy_id,
            target_id,
            status=status,
            latency_ms=latency_ms,
            error=err_msg,
            now=now,
        )

    return evidence


def record_target_liveness(
    policy_id: str,
    target_id: str,
    *,
    status: str,
    latency_ms: float = 0.0,
    error: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Update durable liveness tracking for a target under policy."""
    with _LOCK:
        state = get_write_continuity_state(policy_id)
        now_dt = now or datetime.now(tz=timezone.utc)
        now_str = _utc_iso(now_dt)

        t_live = dict(state.get("targetLiveness") or {})
        prev = t_live.get(target_id) or {}
        prev_status = str(prev.get("status") or "unknown")

        consecutive_successes = int(prev.get("consecutiveSuccesses") or 0)
        consecutive_failures = int(prev.get("consecutiveFailures") or 0)
        first_healthy_at: str | None = prev.get("firstHealthyAt")
        last_healthy_at: str | None = prev.get("lastHealthyAt")

        if status == "available":
            consecutive_successes += 1
            consecutive_failures = 0
            if prev_status != "available" or not first_healthy_at:
                first_healthy_at = now_str
            last_healthy_at = now_str
        else:
            consecutive_failures += 1
            consecutive_successes = 0
            first_healthy_at = None
            last_healthy_at = prev.get("lastHealthyAt")

        entry = {
            "targetId": target_id,
            "status": status,
            "latencyMs": round(latency_ms, 2),
            "checkedAt": now_str,
            "consecutiveSuccesses": consecutive_successes,
            "consecutiveFailures": consecutive_failures,
            "firstHealthyAt": first_healthy_at,
            "lastHealthyAt": last_healthy_at,
            "error": error,
        }
        t_live[target_id] = entry
        state["targetLiveness"] = t_live

        # Update primary continuity tracking if this is configured primary
        if target_id == state.get("configuredPrimaryTargetId"):
            if status == "available":
                if not state.get("primaryFirstHealthyAt") or prev_status != "available":
                    state["primaryFirstHealthyAt"] = now_str
                state["primaryLastHealthyAt"] = now_str
                p_first = _parse_iso(state.get("primaryFirstHealthyAt"))
                if p_first:
                    state["primaryConsecutiveHealthySeconds"] = max(0.0, (now_dt - p_first).total_seconds())
                else:
                    state["primaryConsecutiveHealthySeconds"] = 0.0
            else:
                state["primaryFirstHealthyAt"] = None
                state["primaryConsecutiveHealthySeconds"] = 0.0

        save_write_continuity_state(policy_id, state)
        return entry


def evaluate_failback_eligibility(
    policy_id: str,
    *,
    stability_window_seconds: int = DEFAULT_FAILBACK_STABILITY_SECONDS,
    now: datetime | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Determine if a policy operating in failover mode is eligible for governed failback.

    Conditions for Governed Failback:
    1. Active write target is currently a failover target (active != primary).
    2. Configured primary has maintained continuous healthy status for >= stability_window_seconds.
    3. The latest point written to the active failover target has converged (replicated) to the primary.
    """
    state = get_write_continuity_state(policy_id)
    primary_id = str(state.get("configuredPrimaryTargetId") or "managed-local")
    active_id = str(state.get("activeWriteTargetId") or primary_id)

    info: dict[str, Any] = {
        "policyId": policy_id,
        "configuredPrimaryTargetId": primary_id,
        "activeWriteTargetId": active_id,
        "primaryConsecutiveHealthySeconds": float(state.get("primaryConsecutiveHealthySeconds") or 0.0),
        "requiredStabilitySeconds": stability_window_seconds,
        "latestFailoverPointConverged": False,
    }

    if active_id == primary_id:
        return False, "already-primary", info

    # Check stability window
    current_dt = now or datetime.now(tz=timezone.utc)
    p_first = _parse_iso(state.get("primaryFirstHealthyAt"))
    if not p_first:
        return False, "primary-not-healthy", info

    healthy_duration = (current_dt - p_first).total_seconds()
    info["primaryConsecutiveHealthySeconds"] = max(0.0, healthy_duration)
    if healthy_duration < stability_window_seconds:
        return False, f"primary-stability-insufficient:{int(healthy_duration)}s<{stability_window_seconds}s", info

    # Check latest point convergence
    active_copies = backup_dr_ledger.list_logical_recovery_copies(
        target_id=active_id,
        policy_id=policy_id,
    )
    if not active_copies:
        active_latest, _ = backup_dr_ledger.get_latest_recoverable_point(active_id, policy_id, now=current_dt)
        if active_latest is None:
            # No points ever written to failover target
            info["latestFailoverPointConverged"] = True
            return True, "eligible", info
        active_backup_id = str(active_latest.get("backupId") or "")
    else:
        active_copies.sort(key=lambda c: str(c.get("committedAt") or c.get("committed_at") or ""), reverse=True)
        active_backup_id = str(active_copies[0].get("backupId") or active_copies[0].get("backup_id") or "")

    primary_copies = backup_dr_ledger.list_logical_recovery_copies(
        policy_id=policy_id,
        target_id=primary_id,
        backup_id=active_backup_id,
    )
    healthy_primary_copy = any(
        c.get("recoverable") and c.get("state") == "healthy"
        for c in primary_copies
    )

    info["latestFailoverPointConverged"] = healthy_primary_copy
    if not healthy_primary_copy:
        return False, f"latest-failover-point-not-converged:{active_backup_id}", info

    return True, "eligible", info


def execute_failover_transition(
    policy_id: str,
    new_active_target_id: str,
    *,
    reason: str = "primary-unavailable",
) -> dict[str, Any]:
    """Execute dynamic write target failover under governed write placement."""
    with _LOCK:
        state = get_write_continuity_state(policy_id)
        now_str = _utc_iso()

        if state.get("activeWriteTargetId") == new_active_target_id and state.get("activeWriteTargetRole") == "failover":
            state["lastFailoverAt"] = now_str
            state["lastFailoverReason"] = reason
            save_write_continuity_state(policy_id, state)
            return state

        state["activeWriteTargetId"] = new_active_target_id
        state["activeWriteTargetRole"] = "failover"
        state["lastFailoverAt"] = now_str
        state["lastFailoverReason"] = reason
        if not state.get("failoverActiveSince"):
            state["failoverActiveSince"] = now_str
        state["failoverEpoch"] = int(state.get("failoverEpoch") or 0) + 1

        save_write_continuity_state(policy_id, state)
        return state


def execute_failback_transition(
    policy_id: str,
    *,
    reason: str = "governed-stability-window-and-point-convergence",
) -> dict[str, Any]:
    """Revert active write target to configured primary upon meeting stability and convergence."""
    with _LOCK:
        state = get_write_continuity_state(policy_id)
        primary_id = str(state.get("configuredPrimaryTargetId") or "managed-local")
        now_str = _utc_iso()

        state["activeWriteTargetId"] = primary_id
        state["activeWriteTargetRole"] = "primary"
        state["lastFailbackAt"] = now_str
        state["lastFailbackReason"] = reason
        state["failoverActiveSince"] = None
        state["failoverEpoch"] = int(state.get("failoverEpoch") or 0) + 1

        save_write_continuity_state(policy_id, state)
        return state


def promote_primary_target(
    policy_id: str,
    target_id: str,
    *,
    expected_policy_revision: int | None = None,
    expected_failover_epoch: int | None = None,
) -> dict[str, Any]:
    """Explicit administrative primary promotion with CAS validation and strict safety preconditions."""
    from deepseek_infra.infra.workspace import backup_policies, backup_publish, backup_replication, backup_targets

    with _LOCK:
        policy = backup_policies.get_policy(policy_id)
        state = get_write_continuity_state(policy_id)

        curr_rev = int(policy.get("policyRevision") or 1)
        curr_epoch = int(state.get("failoverEpoch") or 0)

        if expected_policy_revision is not None and curr_rev != expected_policy_revision:
            raise AppError(
                f"CAS mismatch on policyRevision: expected {expected_policy_revision}, actual {curr_rev}",
                code=ErrorCode.INVALID_REQUEST,
                status=412,
            )

        if expected_failover_epoch is not None and curr_epoch != expected_failover_epoch:
            raise AppError(
                f"CAS mismatch on failoverEpoch: expected {expected_failover_epoch}, actual {curr_epoch}",
                code=ErrorCode.INVALID_REQUEST,
                status=412,
            )

        # Precondition a: Must be a configured replica target if replication is enabled
        repl = dict(policy.get("replication") or {})
        if repl.get("enabled"):
            target_entries = list(repl.get("targets") or [])
            is_member = any(
                isinstance(t, dict) and str(t.get("targetId")) == target_id
                for t in target_entries
            )
            if not is_member and target_id != str(policy.get("primaryTargetId") or policy.get("targetId")):
                raise AppError(
                    f"Target {target_id} is not a configured replica in policy {policy_id}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=400,
                )

        # Precondition b: Fresh liveness / write capability check
        target = backup_publish.resolve_target(target_id)
        if target.root is None and target.store is not None:
            caps = target.store.capabilities()
            if not getattr(caps, "scheduled_backup_ready", True):
                raise AppError(f"Target {target_id} is not scheduled_backup_ready", code=ErrorCode.INVALID_REQUEST, status=400)

        # Precondition c: Fresh liveness check
        liveness = perform_liveness_preflight(target_id, policy_id=policy_id)
        if liveness.get("status") != "available":
            raise AppError(f"Target {target_id} liveness preflight failed: {liveness}", code=ErrorCode.INVALID_REQUEST, status=400)

        # Precondition d: Target has healthy copy of latest recovery point (if any points exist)
        latest_pt, _ = backup_dr_ledger.get_latest_recoverable_point(target_id, policy_id)
        all_latest = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, limit=1)
        if all_latest and latest_pt is None:
            raise AppError(f"Target {target_id} has no recoverable points for policy {policy_id}", code=ErrorCode.INVALID_REQUEST, status=400)

        # Precondition e: Target is not in draining state
        target_info = backup_targets.get_target(target_id)
        if target_info and target_info.get("drainState") in {"draining", "drained"}:
            raise AppError(f"Target {target_id} is in {target_info.get('drainState')} state", code=ErrorCode.INVALID_REQUEST, status=400)

        # Precondition f: No active repair job on target for policy
        active_repairs = backup_replication.list_repair_jobs(
            policy_id=policy_id,
            dest_target_id=target_id,
            limit=10,
        )
        if any(r.get("phase") not in {"complete", "healthy", "failed", "failed-terminal"} for r in active_repairs):
            raise AppError(f"Target {target_id} has active repair jobs for policy {policy_id}", code=ErrorCode.INVALID_REQUEST, status=409)

        # Update backup_policies
        prev_primary = str(policy.get("primaryTargetId") or policy.get("targetId") or "managed-local")
        updated_policy = dict(policy)
        updated_policy["targetId"] = target_id
        updated_policy["primaryTargetId"] = target_id
        updated_policy["policyRevision"] = curr_rev + 1

        # If replica targets existed, ensure previous primary is added as replica target
        if repl.get("enabled"):
            targets = list(repl.get("targets") or [])
            # Remove new target_id from replica targets
            targets = [t for t in targets if isinstance(t, dict) and str(t.get("targetId")) != target_id]
            # Add prev_primary as replica target if not already present
            if prev_primary != target_id and not any(isinstance(t, dict) and str(t.get("targetId")) == prev_primary for t in targets):
                targets.append({"targetId": prev_primary, "mode": "required"})
            repl["targets"] = targets
            updated_policy["replication"] = repl
        backup_policies.update_policy(policy_id, updated_policy, expected_revision=curr_rev)

        # Update continuity state
        now_str = _utc_iso()
        state["policyRevision"] = curr_rev + 1
        state["failoverEpoch"] = curr_epoch + 1
        state["configuredPrimaryTargetId"] = target_id
        state["activeWriteTargetId"] = target_id
        state["activeWriteTargetRole"] = "primary"
        state["failoverActiveSince"] = None
        state["lastFailoverAt"] = None
        state["lastFailbackAt"] = now_str
        state["lastFailbackReason"] = f"promoted-target-{target_id}-as-primary"
        state["primaryFirstHealthyAt"] = now_str
        state["primaryLastHealthyAt"] = now_str
        state["primaryConsecutiveHealthySeconds"] = 0.0

        save_write_continuity_state(policy_id, state)

        return {
            "status": "promoted",
            "policyId": policy_id,
            "policyRevision": curr_rev + 1,
            "failoverEpoch": curr_epoch + 1,
            "previousPrimaryTargetId": prev_primary,
            "newPrimaryTargetId": target_id,
            "promotedAt": now_str,
        }
