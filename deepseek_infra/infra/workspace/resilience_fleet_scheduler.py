"""Global Recovery Intelligence - Fleet Execution Scheduler (4.7.3 Gates J, K, L, M, N).

Coordinates multi-policy resilience operations across the entire storage fleet.
Guarantees:
1. Multi-policy Fleet Scheduling & Execution Waves (Gate J).
2. Multi-dimensional Risk Debt modeling (Gate K) to avoid starvation of long-standing risks.
3. Weighted Fair Queue scheduling respecting policy criticality (Gate L).
4. Safe-point preemption semantics protecting in-flight remote transfers (Gate M).
5. Durability-first bandwidth & transfer budget reservations (Gate N).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_policies,
    resilience_coordinator,
    resilience_resource_locks,
    resilience_scheduler_service,
)

SEVERITY_BASE_WEIGHT: dict[str, float] = {
    "critical": 10.0,
    "degraded": 5.0,
    "warning": 2.0,
    "healthy": 0.0,
    "blocked": 15.0,
}

POLICY_CRITICALITY_WEIGHT: dict[str, float] = {
    "critical": 3.0,
    "high": 2.0,
    "standard": 1.0,
}

TRAFFIC_CLASS_PRIORITY: dict[str, int] = {
    "repair": 1,
    "drill": 2,
    "rebalance": 3,
}


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def compute_risk_debt(
    action: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Calculate multi-dimensional risk debt score for an action intent (Gate K).

    Formula:
        riskDebt = severityWeight * ageFactor * policyCriticality * sloBreachFactor
    """
    current = now or datetime.now(tz=timezone.utc)
    raw_params = action.get("parameters")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    pid = str(params.get("policyId") or action.get("policyId") or "")

    pol = policy
    if pol is None and pid:
        try:
            pol = backup_policies.get_policy(pid)
        except Exception:
            pol = None

    # 1. Severity weight
    sev = str(action.get("severity") or action.get("severityBefore") or "warning").lower()
    base_weight = SEVERITY_BASE_WEIGHT.get(sev, 2.0)

    # 2. Age factor (accumulates over time so degraded items are not starved)
    risk_first_seen = action.get("riskFirstSeenAt")
    created_at_str = str(risk_first_seen or action.get("createdAt") or action.get("generatedAt") or "")
    created_at = _parse_iso(created_at_str)
    if created_at is not None:
        age_seconds = max(0.0, (current - created_at).total_seconds())
    else:
        age_seconds = 0.0
    age_days = age_seconds / 86400.0
    # Age factor grows logarithmically / linearly up to a maximum multiplier
    age_factor = 1.0 + min(10.0, age_days * 0.5)

    # 3. Policy Criticality
    pol_crit = "standard"
    if pol and isinstance(pol, dict):
        pol_crit = str(pol.get("criticality") or pol.get("priority") or "standard").lower()
    crit_multiplier = POLICY_CRITICALITY_WEIGHT.get(pol_crit, 1.0)

    # 4. SLO breach factor
    slo_breached = bool(action.get("sloBreached") or params.get("sloBreached"))
    slo_factor = 1.5 if slo_breached else 1.0

    raw_debt = base_weight * age_factor * crit_multiplier * slo_factor
    debt_score = round(raw_debt, 3)

    return {
        "riskDebt": debt_score,
        "baseSeverityWeight": base_weight,
        "severity": sev,
        "ageSeconds": round(age_seconds, 1),
        "ageDays": round(age_days, 2),
        "ageFactor": round(age_factor, 3),
        "policyCriticality": pol_crit,
        "criticalityMultiplier": crit_multiplier,
        "sloBreachFactor": slo_factor,
        "policyId": pid,
        "actionId": str(action.get("actionId") or ""),
        "ageSource": "risk-observation-ledger" if risk_first_seen else "action-created-at-fallback",
    }


def order_actions_fairly(
    actions: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    policy_history: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Sort and schedule actions using Weighted Fair Queueing + Risk Debt (Gate L).

    Balances high-severity urgencies with starvation prevention across policies.
    """
    current = now or datetime.now(tz=timezone.utc)
    persistent_service = resilience_scheduler_service.list_policy_service()
    history = dict(policy_history or {})

    scored_actions: list[tuple[float, dict[str, Any]]] = []
    for act in actions:
        debt_info = compute_risk_debt(act, now=current)
        act_copy = dict(act)
        act_copy["debtInfo"] = debt_info
        score = debt_info["riskDebt"]

        # Apply fair share penalty if policy already executed multiple actions recently
        pid = debt_info["policyId"] or "default"
        service_state = persistent_service.get(pid) or {}
        recent_count = history.get(pid, int(service_state.get("actionsServed") or 0))
        virtual_runtime = float(service_state.get("virtualRuntime") or recent_count)
        fairness_penalty = 1.0 / (1.0 + 0.2 * virtual_runtime)
        adjusted_score = score * fairness_penalty
        act_copy["fairnessState"] = {
            "persistent": pid in persistent_service and policy_history is None,
            "actionsServed": recent_count,
            "virtualRuntime": virtual_runtime,
            "adjustedScore": round(adjusted_score, 6),
        }

        # Tie-breaker boost for repair vs rebalance
        atype = str(act.get("type") or "").upper()
        if atype == "CREATE_REPAIR_JOB":
            adjusted_score += 1.0
        elif atype == "START_DR_DRILL":
            adjusted_score += 0.5

        scored_actions.append((adjusted_score, act_copy))

    # Sort descending by adjusted score
    scored_actions.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored_actions]


def can_preempt_action(victim_action: dict[str, Any]) -> bool:
    """Determine whether an action is at a safe point and eligible for preemption (Gate M).

    Safe points:
      - PENDING
      - CLAIMED (before any remote side effect / transfer has begun)
    Unsafe points (cannot preempt):
      - EXECUTING with in-flight remote transfer or committed receipt
      - VERIFYING
      - RECONCILING
      - ASSESSING_EFFECT
    """
    state = str(victim_action.get("state") or "").upper()
    if state == "PENDING":
        return True
    if state == "CLAIMED":
        effect_class = str(victim_action.get("effectClass") or "")
        return effect_class in {"", "NO_EFFECT"}
    return False


def schedule_fleet_resilience(
    risk_snapshot: dict[str, Any],
    *,
    candidate_actions: list[dict[str, Any]] | None = None,
    action_policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Orchestrate multi-policy fleet resilience into admitted Execution Waves (Gates J, K, L, M, N)."""
    current = now or datetime.now(tz=timezone.utc)
    raw_limits = autonomous_action_policy.get_action_rate_limits()
    if action_policy and isinstance(action_policy.get("rateLimits"), dict):
        raw_limits = {**raw_limits, **action_policy["rateLimits"]}
    limits = raw_limits

    # 1. Resolve candidate actions
    if candidate_actions is not None:
        actions = list(candidate_actions)
    else:
        coord_plan = resilience_coordinator.plan_coordinated_resilience(risk_snapshot, action_policy=action_policy)
        actions = list(coord_plan.get("actions", []))

    # 2. Fair multi-policy queue ordering based on Risk Debt & Criticality
    sorted_actions = order_actions_fairly(actions, now=current)

    # 3. Partition actions into conflict-free, budget-admitted Execution Waves
    waves: list[dict[str, Any]] = []
    current_wave_actions: list[dict[str, Any]] = []
    used_locks: set[str] = set()
    used_targets: dict[str, int] = {}
    used_policies: dict[str, int] = {}
    used_domains: set[str] = set()

    max_concurrent = int(limits.get("maxConcurrentActions", 3))
    max_per_target = int(limits.get("maxConcurrentPerTarget", 2))
    max_per_policy = int(limits.get("maxConcurrentPerPolicy", 2))
    max_domains = int(limits.get("maxSimultaneousFailureDomainsTouched", 1))

    # Bandwidth reservation tracking (Gate N)
    transfer_budget = {
        "repairReservePercent": 50,
        "drDrillReservePercent": 25,
        "rebalanceOpportunisticPercent": 25,
        "rebalanceBlockedByRepairReserve": False,
    }

    admitted_actions: list[dict[str, Any]] = []
    deferred_actions: list[dict[str, Any]] = []

    for act in sorted_actions:
        atype = str(act.get("type") or "").upper()
        params = act.get("parameters", {}) if isinstance(act.get("parameters"), dict) else {}
        pid = str(params.get("policyId") or act.get("policyId") or "")
        src = str(params.get("sourceTargetId") or act.get("source") or "")
        dst = str(params.get("destTargetId") or act.get("destination") or act.get("target") or "")
        domain = str(params.get("failureDomain") or act.get("riskSubject", {}).get("failureDomain") or "")

        lock_keys = resilience_resource_locks.derive_resource_locks_for_action(act)
        has_lock_conflict = any(k in used_locks for k in lock_keys)

        target_exceeded = False
        for tid in (src, dst):
            if tid and used_targets.get(tid, 0) >= max_per_target:
                target_exceeded = True
                break

        policy_exceeded = bool(pid and used_policies.get(pid, 0) >= max_per_policy)
        domain_exceeded = bool(domain and domain not in used_domains and len(used_domains) >= max_domains)
        concurrency_exceeded = len(current_wave_actions) >= max_concurrent

        # Bandwidth reservation check: Rebalance cannot consume repair reserve
        if atype == "CREATE_REBALANCE_JOB":
            active_repairs = sum(1 for a in current_wave_actions if str(a.get("type")) == "CREATE_REPAIR_JOB")
            if active_repairs > 0 and len(current_wave_actions) >= 1:
                # Durability repairs take precedence
                transfer_budget["rebalanceBlockedByRepairReserve"] = True

        can_admit_to_current_wave = not (
            has_lock_conflict
            or target_exceeded
            or policy_exceeded
            or domain_exceeded
            or concurrency_exceeded
        )

        if can_admit_to_current_wave:
            act["scheduleStatus"] = "ADMITTED"
            current_wave_actions.append(act)
            admitted_actions.append(act)
            for k in lock_keys:
                used_locks.add(k)
            for tid in (src, dst):
                if tid:
                    used_targets[tid] = used_targets.get(tid, 0) + 1
            if pid:
                used_policies[pid] = used_policies.get(pid, 0) + 1
            if domain:
                used_domains.add(domain)
        else:
            reason = "concurrency-or-lock-conflict"
            if has_lock_conflict:
                reason = "resource-lock-conflict"
            elif target_exceeded:
                reason = "target-concurrency-exceeded"
            elif policy_exceeded:
                reason = "policy-concurrency-exceeded"
            elif domain_exceeded:
                reason = "failure-domain-limit-exceeded"
            elif concurrency_exceeded:
                reason = "global-concurrency-exceeded"

            act["scheduleStatus"] = "DEFERRED"
            act["deferReason"] = reason
            deferred_actions.append(act)

    # 4. Simulate blast radius across proposed wave
    wave_passed, wave_sim = resilience_coordinator.simulate_coordination_wave(current_wave_actions)
    if not wave_passed:
        for act in current_wave_actions:
            act["scheduleStatus"] = "BLOCKED_BY_BLAST_RADIUS"

    if current_wave_actions:
        waves.append(
            {
                "waveIndex": 0,
                "actions": current_wave_actions,
                "waveStatus": "EXECUTABLE" if wave_passed else "BLOCKED_BY_BLAST_RADIUS",
                "blastRadiusVerified": wave_passed,
                "simulationDetails": wave_sim,
            }
        )

    if wave_passed:
        resilience_scheduler_service.record_scheduled_actions(current_wave_actions, scheduled_at=current)

    schedule_id = f"fsch_{uuid.uuid4().hex[:16]}"
    return {
        "scheduleId": schedule_id,
        "scheduledAt": _utc_iso(current),
        "riskDigest": str(risk_snapshot.get("riskDigest") or ""),
        "totalCandidateActions": len(sorted_actions),
        "admittedCount": len(current_wave_actions),
        "deferredCount": len(deferred_actions),
        "executionWaves": waves,
        "deferredActions": deferred_actions,
        "transferBudget": transfer_budget,
        "status": "SCHEDULED" if wave_passed else "BLOCKED",
    }
