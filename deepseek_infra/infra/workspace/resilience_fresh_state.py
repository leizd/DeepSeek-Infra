"""Authoritative production fresh-state bundle for predictive control (4.7.6 Gate A)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_ACTION_STATES = ("CLAIMED", "EXECUTING", "RECONCILING", "VERIFYING", "ASSESSING_EFFECT")


class FreshStateUnavailable(RuntimeError):
    """A mandatory production observation could not be obtained or validated."""

    def __init__(self, reason: str, *, source: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.source = source
        self.detail = detail or reason


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _read_authority_state() -> dict[str, Any] | None:
    from deepseek_infra.infra.workspace import backup_control_recovery

    return backup_control_recovery.authority_health_snapshot()


def _read_risk_snapshot(*, now: datetime) -> dict[str, Any] | None:
    from deepseek_infra.infra.workspace import resilience_risk_engine

    return resilience_risk_engine.assess_risks(probe=True, now=now)


def _action_target_ids(actions: list[dict[str, Any]]) -> list[str]:
    target_ids: set[str] = set()
    for action in actions:
        raw_params = action.get("parameters")
        params = raw_params if isinstance(raw_params, dict) else {}
        for value in (
            params.get("sourceTargetId"),
            params.get("destTargetId"),
            params.get("targetId"),
            action.get("source"),
            action.get("destination"),
            action.get("target"),
        ):
            target_id = str(value or "").strip()
            if target_id:
                target_ids.add(target_id)
    return sorted(target_ids)


def _read_capacity_snapshot(actions: list[dict[str, Any]], *, now: datetime) -> dict[str, Any] | None:
    del now
    from deepseek_infra.infra.workspace import backup_targets

    target_ids = _action_target_ids(actions)
    if not target_ids:
        target_ids = sorted(str(item.get("targetId") or "") for item in backup_targets.list_targets())
        target_ids = [target_id for target_id in target_ids if target_id]
    if not target_ids:
        return None
    return {"targets": [backup_targets.probe_target_capacity(target_id) for target_id in target_ids]}


def _read_running_effects() -> list[dict[str, Any]] | None:
    from deepseek_infra.infra.workspace import resilience_action_journal

    actions: list[dict[str, Any]] = []
    for state in _ACTIVE_ACTION_STATES:
        actions.extend(resilience_action_journal.list_actions(state=state, limit=500))
    return sorted(actions, key=lambda item: str(item.get("actionId") or ""))


def _action_scope(action: dict[str, Any]) -> tuple[str, set[str], str]:
    raw_params = action.get("parameters")
    params = raw_params if isinstance(raw_params, dict) else {}
    policy_id = str(params.get("policyId") or action.get("policyId") or "")
    targets = set(_action_target_ids([action]))
    raw_subject = action.get("riskSubject")
    subject = raw_subject if isinstance(raw_subject, dict) else {}
    failure_domain = str(params.get("failureDomain") or subject.get("failureDomain") or "")
    return policy_id, targets, failure_domain


def _read_budget_snapshot(
    actions: list[dict[str, Any]],
    running_effects: list[dict[str, Any]],
) -> dict[str, Any] | None:
    from deepseek_infra.infra.workspace import autonomous_action_policy, backup_transfer_budget

    limits = autonomous_action_policy.get_action_rate_limits()
    manager = backup_transfer_budget.get_global_transfer_budget_manager()
    transfer_budget = manager.scheduler_reservation_snapshot(actions)
    transfer_state = manager.transfer_control_summary()
    reasons: list[str] = []
    active_and_proposed = [*running_effects, *actions]
    if len(active_and_proposed) > int(limits.get("maxConcurrentActions") or 0):
        reasons.append("GLOBAL_CONCURRENCY_EXCEEDED")

    target_counts: dict[str, int] = {}
    policy_counts: dict[str, int] = {}
    failure_domains: set[str] = set()
    for action in active_and_proposed:
        policy_id, target_ids, failure_domain = _action_scope(action)
        if policy_id:
            policy_counts[policy_id] = policy_counts.get(policy_id, 0) + 1
        for target_id in target_ids:
            target_counts[target_id] = target_counts.get(target_id, 0) + 1
        if failure_domain:
            failure_domains.add(failure_domain)
    if any(count > int(limits.get("maxConcurrentPerTarget") or 0) for count in target_counts.values()):
        reasons.append("TARGET_CONCURRENCY_EXCEEDED")
    if any(count > int(limits.get("maxConcurrentPerPolicy") or 0) for count in policy_counts.values()):
        reasons.append("POLICY_CONCURRENCY_EXCEEDED")
    if len(failure_domains) > int(limits.get("maxSimultaneousFailureDomainsTouched") or 0):
        reasons.append("FAILURE_DOMAIN_LIMIT_EXCEEDED")
    if transfer_budget.get("rebalanceBlockedByRepairReserve") is True:
        reasons.append("TRANSFER_BUDGET_CLASS_CONFLICT")
    return {
        "admitted": not reasons,
        "reasons": reasons,
        "actionLimits": limits,
        "activeActionCount": len(running_effects),
        "proposedActionCount": len(actions),
        "targetCounts": target_counts,
        "policyCounts": policy_counts,
        "failureDomains": sorted(failure_domains),
        "transferBudget": transfer_budget,
        "transferState": transfer_state,
    }


def _read_maintenance_decisions(actions: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]] | None:
    from deepseek_infra.infra.workspace import resilience_fleet_scheduler

    return [
        {
            "actionId": str(action.get("actionId") or ""),
            **resilience_fleet_scheduler.evaluate_maintenance_window(action, now=now),
        }
        for action in actions
    ]


def _read_blast_simulation(
    actions: list[dict[str, Any]],
    running_effects: list[dict[str, Any]],
) -> dict[str, Any] | None:
    from deepseek_infra.infra.workspace import resilience_coordinator

    passed, details = resilience_coordinator.simulate_coordination_wave(actions, running_actions=running_effects)
    return {"passed": bool(passed), **details}


def _read_required(
    reader: Callable[[], Any],
    *,
    source: str,
    reason: str,
) -> Any:
    try:
        value = reader()
    except Exception as exc:
        raise FreshStateUnavailable(reason, source=source, detail=f"{type(exc).__name__}: {exc}") from exc
    if value is None:
        raise FreshStateUnavailable(reason, source=source)
    return value


def _validate_capacity_snapshot(snapshot: Any) -> bool:
    if not isinstance(snapshot, dict):
        return False
    targets = snapshot.get("targets")
    if not isinstance(targets, list) or not targets:
        return False
    for target in targets:
        if not isinstance(target, dict):
            return False
        if not str(target.get("targetId") or "") or not str(target.get("observedAt") or ""):
            return False
        for field in ("usedBytes", "freeBytes", "totalBytes"):
            value = target.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return False
        if int(target["usedBytes"]) + int(target["freeBytes"]) > int(target["totalBytes"]):
            return False
        if str(target.get("source") or "").lower() == "unknown":
            return False
    return True


def _coverage_complete(snapshot: dict[str, Any]) -> bool:
    coverage = snapshot.get("coverage")
    if not isinstance(coverage, dict) or not coverage:
        return False
    return all(isinstance(item, dict) and item.get("complete") is True for item in coverage.values())


def build_fresh_state_bundle(
    schedule: dict[str, Any],
    wave_actions: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read and bind every mandatory production source; never infer safety from absence."""
    del schedule
    current = now or datetime.now(tz=timezone.utc)
    authority = _read_required(
        _read_authority_state,
        source="authority",
        reason="AUTHORITY_OBSERVATION_UNAVAILABLE",
    )
    if not isinstance(authority, dict) or not _is_sha256(authority.get("canonicalDigest")):
        raise FreshStateUnavailable("AUTHORITY_OBSERVATION_UNAVAILABLE", source="authority")

    risk = _read_required(
        lambda: _read_risk_snapshot(now=current),
        source="risk",
        reason="RISK_SNAPSHOT_UNAVAILABLE",
    )
    if not isinstance(risk, dict) or not _is_sha256(risk.get("riskDigest")):
        raise FreshStateUnavailable("RISK_SNAPSHOT_UNAVAILABLE", source="risk")
    if not _coverage_complete(risk):
        raise FreshStateUnavailable("RISK_SNAPSHOT_INCOMPLETE", source="risk")

    capacity = _read_required(
        lambda: _read_capacity_snapshot(wave_actions, now=current),
        source="capacity",
        reason="CAPACITY_SNAPSHOT_UNAVAILABLE",
    )
    if not _validate_capacity_snapshot(capacity):
        raise FreshStateUnavailable("CAPACITY_SNAPSHOT_UNAVAILABLE", source="capacity")

    running_effects = _read_required(
        _read_running_effects,
        source="running-effects",
        reason="RUNNING_EFFECTS_UNAVAILABLE",
    )
    if not isinstance(running_effects, list):
        raise FreshStateUnavailable("RUNNING_EFFECTS_UNAVAILABLE", source="running-effects")

    budgets = _read_required(
        lambda: _read_budget_snapshot(wave_actions, running_effects),
        source="budgets",
        reason="BUDGET_SNAPSHOT_UNAVAILABLE",
    )
    if not isinstance(budgets, dict) or not isinstance(budgets.get("admitted"), bool):
        raise FreshStateUnavailable("BUDGET_SNAPSHOT_UNAVAILABLE", source="budgets")

    maintenance = _read_required(
        lambda: _read_maintenance_decisions(wave_actions, now=current),
        source="maintenance",
        reason="MAINTENANCE_DECISION_UNAVAILABLE",
    )
    if not isinstance(maintenance, list) or any(not isinstance(item, dict) or not isinstance(item.get("allowed"), bool) for item in maintenance):
        raise FreshStateUnavailable("MAINTENANCE_DECISION_UNAVAILABLE", source="maintenance")

    blast = _read_required(
        lambda: _read_blast_simulation(wave_actions, running_effects),
        source="blast-simulation",
        reason="BLAST_SIMULATION_UNAVAILABLE",
    )
    if not isinstance(blast, dict) or not isinstance(blast.get("passed"), bool):
        raise FreshStateUnavailable("BLAST_SIMULATION_UNAVAILABLE", source="blast-simulation")

    bundle: dict[str, Any] = {
        "authorityHeadDigest": str(authority["canonicalDigest"]),
        "riskDigest": str(risk["riskDigest"]),
        "capacitySnapshotDigest": canonical_digest(capacity),
        "runningEffectsDigest": canonical_digest(running_effects),
        "budgetRevision": canonical_digest(budgets),
        "maintenanceDecisionDigest": canonical_digest(maintenance),
        "blastSimulationDigest": canonical_digest(blast),
        "observedAt": _utc_iso(current),
        "authorityState": authority,
        "riskSnapshot": risk,
        "capacitySnapshot": capacity,
        "runningEffects": running_effects,
        "budgets": budgets,
        "maintenanceDecisions": maintenance,
        "blastSimulation": blast,
    }
    bundle["freshStateBundleDigest"] = canonical_digest(
        {
            "authorityHeadDigest": bundle["authorityHeadDigest"],
            "riskDigest": bundle["riskDigest"],
            "capacitySnapshotDigest": bundle["capacitySnapshotDigest"],
            "runningEffectsDigest": bundle["runningEffectsDigest"],
            "budgetRevision": bundle["budgetRevision"],
            "maintenanceDecisionDigest": bundle["maintenanceDecisionDigest"],
            "blastSimulationDigest": bundle["blastSimulationDigest"],
            "observedAt": bundle["observedAt"],
        }
    )
    return bundle
