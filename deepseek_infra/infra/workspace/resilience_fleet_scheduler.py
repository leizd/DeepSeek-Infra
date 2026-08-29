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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_policies,
    backup_transfer_budget,
    resilience_action_journal,
    resilience_coordinator,
    resilience_resource_locks,
    resilience_scheduler_service,
    resilience_slo_ledger,
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
        except AppError as exc:
            if exc.status != 404:
                raise
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


def evaluate_maintenance_window(
    action: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate policy-local maintenance time with narrow critical overrides."""
    current = now or datetime.now(tz=timezone.utc)
    raw_params = action.get("parameters")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    policy_id = str(params.get("policyId") or action.get("policyId") or "")
    raw_window = params.get("maintenanceWindow") or action.get("maintenanceWindow")
    if not isinstance(raw_window, dict) and policy_id:
        try:
            policy = backup_policies.get_policy(policy_id)
        except AppError:
            policy = None
        if isinstance(policy, dict):
            raw_placement = policy.get("placement")
            placement: dict[str, Any] = raw_placement if isinstance(raw_placement, dict) else {}
            raw_window = placement.get("maintenanceWindow")
    if not isinstance(raw_window, dict):
        return {"allowed": True, "inWindow": True, "override": False, "reason": "NO_MAINTENANCE_WINDOW"}

    action_type = str(action.get("type") or "").upper()
    severity = str(action.get("severity") or action.get("severityBefore") or "warning").lower()
    if action_type == "CREATE_REPAIR_JOB" and severity in {"critical", "blocked"}:
        return {"allowed": True, "inWindow": False, "override": True, "reason": "CRITICAL_DURABILITY_OVERRIDE"}
    if action_type == "START_DR_DRILL" and (
        severity in {"critical", "blocked"} or params.get("drStalenessCritical") is True
    ):
        return {"allowed": True, "inWindow": False, "override": True, "reason": "CRITICAL_DR_STALENESS_OVERRIDE"}

    timezone_name = str(raw_window.get("timezone") or "UTC")
    start_text = str(raw_window.get("start") or "")
    end_text = str(raw_window.get("end") or "")
    try:
        zone = ZoneInfo(timezone_name)
        start_hour, start_minute = (int(part) for part in start_text.split(":", 1))
        end_hour, end_minute = (int(part) for part in end_text.split(":", 1))
        if not (0 <= start_hour <= 23 and 0 <= start_minute <= 59 and 0 <= end_hour <= 23 and 0 <= end_minute <= 59):
            raise ValueError("maintenance time out of range")
    except (ValueError, ZoneInfoNotFoundError):
        return {
            "allowed": False,
            "inWindow": False,
            "override": False,
            "reason": "INVALID_MAINTENANCE_WINDOW",
            "timezone": timezone_name,
        }
    local = current.astimezone(zone)
    local_minute = local.hour * 60 + local.minute
    start_value = start_hour * 60 + start_minute
    end_value = end_hour * 60 + end_minute
    if start_value == end_value:
        inside = True
    elif start_value < end_value:
        inside = start_value <= local_minute < end_value
    else:
        inside = local_minute >= start_value or local_minute < end_value
    return {
        "allowed": inside,
        "inWindow": inside,
        "override": False,
        "reason": "WITHIN_MAINTENANCE_WINDOW" if inside else "OUTSIDE_MAINTENANCE_WINDOW",
        "timezone": timezone_name,
        "localTime": local.isoformat(timespec="minutes"),
        "start": start_text,
        "end": end_text,
    }


def schedule_fleet_resilience(
    risk_snapshot: dict[str, Any],
    *,
    candidate_actions: list[dict[str, Any]] | None = None,
    action_policy: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Partition every candidate into a real execution wave or typed UNSCHEDULABLE result."""
    current = now or datetime.now(tz=timezone.utc)
    raw_limits = autonomous_action_policy.get_action_rate_limits()
    if action_policy and isinstance(action_policy.get("rateLimits"), dict):
        raw_limits = {**raw_limits, **action_policy["rateLimits"]}
    limits = raw_limits

    # 1. Resolve candidate actions
    coordination_dependencies: list[list[str]] = []
    if candidate_actions is not None:
        actions = list(candidate_actions)
    else:
        coord_plan = resilience_coordinator.plan_coordinated_resilience(risk_snapshot, action_policy=action_policy)
        actions = list(coord_plan.get("actions", []))
        coordination_dependencies = [list(item) for item in coord_plan.get("dependencies", []) if isinstance(item, list)]

    # 2. Fair multi-policy queue ordering based on Risk Debt & Criticality
    sorted_actions = order_actions_fairly(actions, now=current)

    # 3. Build the full dependency graph. Explicit dependsOn edges are
    # authoritative; coordinator invariants are re-derived for direct callers.
    action_id_counts: dict[str, int] = {}
    for action in sorted_actions:
        action_id = str(action.get("actionId") or "")
        if action_id:
            action_id_counts[action_id] = action_id_counts.get(action_id, 0) + 1
    unschedulable_actions: list[dict[str, Any]] = []
    action_by_id: dict[str, dict[str, Any]] = {}
    for action in sorted_actions:
        action_id = str(action.get("actionId") or "")
        if not action_id:
            invalid = dict(action)
            invalid["scheduleStatus"] = "UNSCHEDULABLE"
            invalid["unschedulableReason"] = "MISSING_ACTION_ID"
            unschedulable_actions.append(invalid)
        elif action_id_counts[action_id] > 1:
            invalid = dict(action)
            invalid["scheduleStatus"] = "UNSCHEDULABLE"
            invalid["unschedulableReason"] = "DUPLICATE_ACTION_ID"
            unschedulable_actions.append(invalid)
        else:
            action_by_id[action_id] = action
    dependency_map: dict[str, set[str]] = {action_id: set() for action_id in action_by_id if action_id}
    for action_id, action in action_by_id.items():
        raw_dependencies = action.get("dependsOn")
        if isinstance(raw_dependencies, list):
            dependency_map[action_id].update(str(item) for item in raw_dependencies if str(item))
    for raw_edge in coordination_dependencies:
        if len(raw_edge) == 2 and str(raw_edge[1]) in dependency_map:
            dependency_map[str(raw_edge[1])].add(str(raw_edge[0]))
    valid_actions = list(action_by_id.values())
    for predecessor in valid_actions:
        pred_type = str(predecessor.get("type") or "").upper()
        raw_pred_params = predecessor.get("parameters")
        pred_params: dict[str, Any] = raw_pred_params if isinstance(raw_pred_params, dict) else {}
        pred_policy = str(pred_params.get("policyId") or predecessor.get("policyId") or "")
        pred_backup = str(pred_params.get("backupId") or predecessor.get("backupId") or "")
        for successor in valid_actions:
            if predecessor is successor or str(successor.get("type") or "").upper() != "CREATE_REBALANCE_JOB":
                continue
            raw_succ_params = successor.get("parameters")
            succ_params: dict[str, Any] = raw_succ_params if isinstance(raw_succ_params, dict) else {}
            succ_policy = str(succ_params.get("policyId") or successor.get("policyId") or "")
            succ_backup = str(succ_params.get("backupId") or successor.get("backupId") or "")
            if pred_type == "CREATE_REPAIR_JOB" and pred_policy and pred_policy == succ_policy:
                dependency_map[str(successor.get("actionId") or "")].add(str(predecessor.get("actionId") or ""))
            if pred_type == "START_DR_DRILL" and pred_backup and pred_backup == succ_backup:
                dependency_map[str(successor.get("actionId") or "")].add(str(predecessor.get("actionId") or ""))

    # 4. Partition actions into conflict-free, budget-admitted Execution Waves.
    waves: list[dict[str, Any]] = []
    max_concurrent = int(limits.get("maxConcurrentActions", 3))
    max_per_target = int(limits.get("maxConcurrentPerTarget", 2))
    max_per_policy = int(limits.get("maxConcurrentPerPolicy", 2))
    max_domains = int(limits.get("maxSimultaneousFailureDomainsTouched", 1))
    transfer_manager = backup_transfer_budget.get_global_transfer_budget_manager()
    transfer_budget = transfer_manager.scheduler_reservation_snapshot(sorted_actions)
    pending_ids = list(action_by_id)
    completed_ids: set[str] = set()
    prior_defer_reasons: dict[str, list[str]] = {}

    # Active effects participate in every proposed-wave simulation.
    running_actions = []
    for active_state in ("CLAIMED", "EXECUTING", "RECONCILING", "VERIFYING", "ASSESSING_EFFECT"):
        running_actions.extend(resilience_action_journal.list_actions(state=active_state, limit=500))

    missing_dependency_ids = {
        action_id
        for action_id, dependencies in dependency_map.items()
        if any(dependency not in action_by_id for dependency in dependencies)
    }
    for action_id in list(pending_ids):
        if action_id in missing_dependency_ids:
            action = dict(action_by_id[action_id])
            action["scheduleStatus"] = "UNSCHEDULABLE"
            action["unschedulableReason"] = "MISSING_DEPENDENCY"
            action["missingDependencies"] = sorted(dependency_map[action_id] - set(action_by_id))
            unschedulable_actions.append(action)
            pending_ids.remove(action_id)

    while pending_ids:
        ready_ids = [action_id for action_id in pending_ids if dependency_map[action_id].issubset(completed_ids)]
        if not ready_ids:
            for action_id in pending_ids:
                action = dict(action_by_id[action_id])
                action["scheduleStatus"] = "UNSCHEDULABLE"
                action["unschedulableReason"] = "DEPENDENCY_CYCLE_OR_BLOCKED_PREDECESSOR"
                action["unmetDependencies"] = sorted(dependency_map[action_id] - completed_ids)
                unschedulable_actions.append(action)
            pending_ids.clear()
            break

        wave_actions: list[dict[str, Any]] = []
        used_locks: set[str] = set()
        used_targets: dict[str, int] = {}
        used_policies: dict[str, int] = {}
        used_domains: set[str] = set()
        wave_index = len(waves)
        wave_sim: dict[str, Any] = {}

        for action_id in ready_ids:
            original = action_by_id[action_id]
            action = dict(original)
            maintenance_decision = evaluate_maintenance_window(action, now=current)
            action["maintenanceWindowDecision"] = maintenance_decision
            if maintenance_decision["allowed"] is not True:
                action["scheduleStatus"] = "UNSCHEDULABLE"
                action["unschedulableReason"] = str(maintenance_decision["reason"])
                unschedulable_actions.append(action)
                pending_ids.remove(action_id)
                continue
            if action.get("requiresApproval") is True:
                action["scheduleStatus"] = "UNSCHEDULABLE"
                action["unschedulableReason"] = "APPROVAL_REQUIRED"
                unschedulable_actions.append(action)
                pending_ids.remove(action_id)
                continue
            raw_params = action.get("parameters")
            params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
            raw_subject = action.get("riskSubject")
            subject: dict[str, Any] = raw_subject if isinstance(raw_subject, dict) else {}
            policy_id = str(params.get("policyId") or action.get("policyId") or "")
            source_id = str(params.get("sourceTargetId") or action.get("source") or "")
            dest_id = str(
                params.get("destTargetId")
                or params.get("targetId")
                or action.get("destination")
                or action.get("target")
                or ""
            )
            domain = str(params.get("failureDomain") or subject.get("failureDomain") or "")
            lock_keys = resilience_resource_locks.derive_resource_locks_for_action(action)
            budget_ok, transfer_reason = transfer_manager.can_share_scheduler_wave(wave_actions, action)
            reasons: list[str] = []
            if not budget_ok:
                reasons.append(transfer_reason)
            if any(key in used_locks for key in lock_keys):
                reasons.append("RESOURCE_LOCK_CONFLICT")
            if any(target_id and used_targets.get(target_id, 0) >= max_per_target for target_id in (source_id, dest_id)):
                reasons.append("TARGET_CONCURRENCY_EXCEEDED")
            if policy_id and used_policies.get(policy_id, 0) >= max_per_policy:
                reasons.append("POLICY_CONCURRENCY_EXCEEDED")
            if domain and domain not in used_domains and len(used_domains) >= max_domains:
                reasons.append("FAILURE_DOMAIN_LIMIT_EXCEEDED")
            if len(wave_actions) >= max_concurrent:
                reasons.append("GLOBAL_CONCURRENCY_EXCEEDED")
            if reasons:
                prior_defer_reasons.setdefault(action_id, [])
                for reason in reasons:
                    if reason not in prior_defer_reasons[action_id]:
                        prior_defer_reasons[action_id].append(reason)
                continue

            action["scheduleStatus"] = "ASSIGNED"
            action["waveIndex"] = wave_index
            action["dependencies"] = sorted(dependency_map[action_id])
            action["priorDeferReasons"] = list(prior_defer_reasons.get(action_id, []))
            candidate_passed, candidate_sim = resilience_coordinator.simulate_coordination_wave(
                [*wave_actions, action],
                running_actions=running_actions,
            )
            if not candidate_passed:
                if wave_actions:
                    prior_defer_reasons.setdefault(action_id, [])
                    if "BLAST_RADIUS_WAVE_CONFLICT" not in prior_defer_reasons[action_id]:
                        prior_defer_reasons[action_id].append("BLAST_RADIUS_WAVE_CONFLICT")
                    continue
                action["scheduleStatus"] = "UNSCHEDULABLE"
                action["unschedulableReason"] = "BLAST_RADIUS_VIOLATION"
                action["simulationDetails"] = candidate_sim
                unschedulable_actions.append(action)
                pending_ids.remove(action_id)
                continue
            wave_actions.append(action)
            wave_sim = candidate_sim
            used_locks.update(lock_keys)
            for target_id in (source_id, dest_id):
                if target_id:
                    used_targets[target_id] = used_targets.get(target_id, 0) + 1
            if policy_id:
                used_policies[policy_id] = used_policies.get(policy_id, 0) + 1
            if domain:
                used_domains.add(domain)

        if not wave_actions:
            # Positive policy limits guarantee at least one ready action fits an
            # empty wave; reaching here means the configuration is unusable.
            for action_id in ready_ids:
                if action_id not in pending_ids:
                    continue
                action = dict(action_by_id[action_id])
                action["scheduleStatus"] = "UNSCHEDULABLE"
                action["unschedulableReason"] = "INVALID_ZERO_CAPACITY_BUDGET"
                unschedulable_actions.append(action)
                pending_ids.remove(action_id)
            continue

        waves.append(
            {
                "waveIndex": wave_index,
                "actions": wave_actions,
                "waveStatus": "EXECUTABLE",
                "blastRadiusVerified": True,
                "simulationDetails": wave_sim,
            }
        )
        for action in wave_actions:
            action_id = str(action["actionId"])
            completed_ids.add(action_id)
            pending_ids.remove(action_id)

    assigned_actions = [action for wave in waves for action in wave["actions"]]
    schedule_id = f"fsch_{uuid.uuid4().hex[:16]}"
    schedule = {
        "scheduleId": schedule_id,
        "scheduledAt": _utc_iso(current),
        "riskDigest": str(risk_snapshot.get("riskDigest") or ""),
        "totalCandidateActions": len(sorted_actions),
        "admittedCount": len(assigned_actions),
        "deferredCount": 0,
        "executionWaves": waves,
        "deferredActions": [],
        "unschedulableCount": len(unschedulable_actions),
        "unschedulableActions": unschedulable_actions,
        "transferBudget": transfer_budget,
        "status": "SCHEDULED" if not unschedulable_actions else ("PARTIAL" if waves else "UNSCHEDULABLE"),
    }
    oldest_age_seconds = max(
        (float(action.get("debtInfo", {}).get("ageSeconds") or 0.0) for action in sorted_actions),
        default=0.0,
    )
    resilience_slo_ledger.try_record_sample(
        resilience_slo_ledger.SCHEDULER_STARVATION_AGE_SECONDS,
        oldest_age_seconds,
        observed_at=current,
        sample_key=f"starvation:{schedule_id}",
        metadata={"candidateActions": len(sorted_actions), "unschedulableActions": len(unschedulable_actions)},
    )
    resilience_scheduler_service.record_schedule_result(schedule, assigned_actions, scheduled_at=current)
    return schedule
