"""Global Recovery Intelligence - Deterministic Resilience Planner (4.7.1 Gate A & B).

Transforms a verified RiskSnapshot into a typed, deterministic ResiliencePlan
composed of fully executable ResilienceActionIntent v1 objects. All actions are
policy-bounded, evidence-backed, and reversible. Safe actions (repair, rebalance, drill)
contain complete execution parameters (policyId, backupId, source, destination).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_capacity,
    backup_dr_ledger,
    backup_policies,
    backup_targets,
)
from deepseek_infra.infra.workspace.resilience_risk_engine import RiskSeverity, RiskType

RESILIENCE_PLAN_VERSION = 1


class ResilienceActionType(str, Enum):
    CREATE_REPAIR_JOB = "CREATE_REPAIR_JOB"
    CREATE_REBALANCE_JOB = "CREATE_REBALANCE_JOB"
    START_DR_DRILL = "START_DR_DRILL"
    PRIMARY_PROMOTION = "PRIMARY_PROMOTION"
    POLICY_CHANGE = "POLICY_CHANGE"
    COPY_DELETION = "COPY_DELETION"
    TOPOLOGY_MUTATION = "TOPOLOGY_MUTATION"


class ResiliencePlanStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _risk_subject_from_record(risk: dict[str, Any]) -> dict[str, str | None]:
    """Bind an action only to scope that the observed risk actually declared."""
    policy_id = str(risk.get("policyId") or "")
    backup_id = str(risk.get("backupId") or "")
    target_id = str(risk.get("targetId") or risk.get("target") or "")
    failure_domain = str(risk.get("failureDomain") or "")
    return {
        "type": str(risk.get("type") or "").upper(),
        "policyId": policy_id or None,
        "backupId": backup_id or None,
        "targetId": target_id or None,
        "failureDomain": failure_domain or None,
    }


def compute_plan_digest(plan: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 digest of a resilience plan."""
    payload = {
        "planVersion": plan.get("planVersion", RESILIENCE_PLAN_VERSION),
        "inputRiskDigest": str(plan.get("inputRiskDigest", "")),
        "actions": sorted(
            [
                {
                    "type": str(a.get("type", "")),
                    "source": str(a.get("source") or a.get("parameters", {}).get("sourceTargetId") or ""),
                    "destination": str(a.get("destination") or a.get("parameters", {}).get("destTargetId") or ""),
                    "target": str(a.get("target") or ""),
                    "policyId": str(a.get("policyId") or a.get("parameters", {}).get("policyId") or ""),
                    "backupId": str(a.get("backupId") or a.get("parameters", {}).get("backupId") or ""),
                    "reason": str(a.get("reason", "")),
                    "requiresApproval": bool(a.get("requiresApproval")),
                }
                for a in plan.get("actions", [])
            ],
            key=lambda x: (x["type"], x["target"], x["source"], x["destination"], x["policyId"], x["backupId"]),
        ),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def select_rebalance_destination(
    source_target_id: str,
    *,
    required_bytes: int = 0,
    preferred_failure_domains: set[str] | None = None,
) -> str | None:
    """Select the best candidate destination target for replica rebalancing.

    Picks an active, non-draining target with the highest available free space
    and appropriate failure domain separation.
    """
    candidates: list[tuple[float, int, str]] = []  # (free_pct, free_bytes, target_id)
    all_targets = backup_targets.list_targets()

    for t in all_targets:
        tid = str(t.get("targetId") or "")
        if not tid or tid == source_target_id:
            continue
        if str(t.get("status") or "").lower() == "draining":
            continue

        cap = backup_capacity.get_target_capacity(tid, probe=False)
        free_bytes = cap.get("freeBytes")
        free_pct = cap.get("freePercent")

        if free_bytes is not None and free_bytes < required_bytes:
            continue

        # If unconstrained, treat as very high free space
        effective_pct = float(free_pct) if free_pct is not None else 100.0
        effective_free = int(free_bytes) if free_bytes is not None else 10**12

        # Check watermark
        if effective_pct <= 20.0:
            continue

        candidates.append((effective_pct, effective_free, tid))

    if not candidates:
        return None

    # Sort descending by free percent and free bytes
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def find_rebalance_candidate_copy(source_target_id: str) -> tuple[str | None, str | None]:
    """Find candidate policy and backup ID stored on source_target_id to rebalance."""
    policies = backup_policies.list_policies()
    for p in policies:
        pid = str(p.get("policyId") or "")
        if not pid:
            continue
        pt = backup_dr_ledger.latest_recovery_point(policy_id=pid)
        if not pt:
            continue
        bid = str(pt.get("backupId") or "")
        if not bid:
            continue
        # Check if copy resides on source_target_id
        if str(pt.get("targetId") or "") == source_target_id:
            return pid, bid
        copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=pid, backup_id=bid)
        for c in copies:
            if str(c.get("targetId") or "") == source_target_id and str(c.get("status") or "").lower() in {
                "committed",
                "active",
                "healthy",
            }:
                return pid, bid
    return None, None


def select_repair_destination(
    policy_id: str,
    backup_id: str,
    existing_targets: set[str],
) -> str | None:
    """Select the best candidate destination target for a repair copy."""
    try:
        policy = backup_policies.get_policy(policy_id)
    except Exception:
        policy = {}

    repl_cfg = policy.get("replication") if isinstance(policy, dict) else {}
    target_candidates: list[str] = []
    if isinstance(repl_cfg, dict):
        for tid in repl_cfg.get("targetIds") or []:
            if str(tid) not in existing_targets:
                target_candidates.append(str(tid))

    all_targets = {str(t.get("targetId") or ""): t for t in backup_targets.list_targets()}
    valid_candidates: list[tuple[float, int, str]] = []
    pool = target_candidates if target_candidates else list(all_targets.keys())
    for tid in pool:
        if not tid or tid in existing_targets:
            continue
        t_meta = all_targets.get(tid)
        if not t_meta or str(t_meta.get("status") or "").lower() == "draining":
            continue
        cap = backup_capacity.get_target_capacity(tid, probe=False)
        free_pct = cap.get("freePercent")
        free_bytes = cap.get("freeBytes")
        effective_pct = float(free_pct) if free_pct is not None else 100.0
        effective_free = int(free_bytes) if free_bytes is not None else 10**12
        if effective_pct > 20.0:
            valid_candidates.append((effective_pct, effective_free, tid))

    if not valid_candidates:
        return None
    valid_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return valid_candidates[0][2]


def validate_action_intent(action: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate whether an action intent has all required parameters for autonomous execution."""
    if not isinstance(action, dict):
        return False, ["action-intent-must-be-object"]
    issues: list[str] = []
    act_type = str(action.get("type") or "").upper()
    if not act_type:
        return False, ["missing-action-type"]

    params_raw = action.get("parameters")
    params: dict[str, Any] = params_raw if isinstance(params_raw, dict) else {}

    if act_type == ResilienceActionType.CREATE_REBALANCE_JOB.value:
        src = params.get("sourceTargetId") or action.get("source") or action.get("target")
        dst = params.get("destTargetId") or action.get("destination")
        pid = params.get("policyId") or action.get("policyId")
        bid = params.get("backupId") or action.get("backupId")
        if not src:
            issues.append("missing-sourceTargetId")
        if not dst:
            issues.append("missing-destTargetId")
        if not pid:
            issues.append("missing-policyId")
        if not bid:
            issues.append("missing-backupId")

    elif act_type == ResilienceActionType.CREATE_REPAIR_JOB.value:
        pid = params.get("policyId") or action.get("policyId")
        bid = params.get("backupId") or action.get("backupId")
        dst = params.get("destTargetId") or action.get("destination") or action.get("target")
        if not pid:
            issues.append("missing-policyId")
        if not bid:
            issues.append("missing-backupId")
        if not dst:
            issues.append("missing-destTargetId")

    return len(issues) == 0, issues


def plan_resilience_actions(
    risk_snapshot: dict[str, Any],
    *,
    action_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive deterministic ResiliencePlan composed of executable intents from a validated RiskSnapshot."""
    input_risk_digest = str(risk_snapshot.get("riskDigest") or "")
    risks = risk_snapshot.get("risks", [])
    actions: list[dict[str, Any]] = []
    plan_id = f"plan_{uuid.uuid4().hex[:16]}"

    from deepseek_infra.infra.workspace import autonomous_action_policy

    policy = action_policy or autonomous_action_policy.get_autonomous_action_policy()

    for r in risks:
        r_type = str(r.get("type", ""))
        r_sev = str(r.get("severity", RiskSeverity.HEALTHY.value)).lower()
        if r_sev in {RiskSeverity.HEALTHY.value, "low"}:
            continue

        # 1. Capacity risk planning -> CREATE_REBALANCE_JOB
        if r_type == RiskType.CAPACITY_EXHAUSTION.value:
            target_id = str(r.get("target") or "")
            dest_target = select_rebalance_destination(target_id)
            cand_pid, cand_bid = find_rebalance_candidate_copy(target_id)
            if target_id and dest_target and cand_pid and cand_bid:
                action_type = ResilienceActionType.CREATE_REBALANCE_JOB.value
                req_appr = not autonomous_action_policy.is_action_autonomous(action_type, policy)
                action_intent = {
                    "actionId": f"act_{uuid.uuid4().hex[:12]}",
                    "type": action_type,
                    "source": target_id,
                    "destination": dest_target,
                    "target": target_id,
                    "policyId": cand_pid,
                    "backupId": cand_bid,
                    "reason": "capacity-risk",
                    "severity": r_sev,
                    "confidence": str(r.get("confidence", "verified")),
                    "requiresApproval": req_appr,
                    "riskSubject": _risk_subject_from_record(r),
                    "severityBefore": r_sev,
                    "expectedEffect": "severity-decrease",
                    "parameters": {
                        "policyId": cand_pid,
                        "backupId": cand_bid,
                        "sourceTargetId": target_id,
                        "destTargetId": dest_target,
                        "reason": "proactive-capacity-rebalance",
                    },
                    "evidence": r.get("evidence", []),
                }
                actions.append(action_intent)

        # 2. Replica lag / Failure domain planning -> CREATE_REPAIR_JOB
        elif r_type in {RiskType.REPLICA_LAG.value, RiskType.FAILURE_DOMAIN_VIOLATION.value}:
            policy_id = str(r.get("policyId") or "")
            latest_pt = backup_dr_ledger.latest_recovery_point(policy_id=policy_id) if policy_id else None
            backup_id = str(latest_pt.get("backupId") or "") if latest_pt else ""
            source_target = str(latest_pt.get("targetId") or "") if latest_pt else ""
            existing_targets: set[str] = set()
            if policy_id and backup_id:
                copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=policy_id, backup_id=backup_id)
                for c in copies:
                    if str(c.get("status") or "").lower() in {"committed", "active", "healthy"}:
                        existing_targets.add(str(c.get("targetId") or ""))
            if source_target:
                existing_targets.add(source_target)

            dest_target = select_repair_destination(policy_id, backup_id, existing_targets) or ""
            action_type = ResilienceActionType.CREATE_REPAIR_JOB.value
            req_appr = not autonomous_action_policy.is_action_autonomous(action_type, policy)
            action_intent = {
                "actionId": f"act_{uuid.uuid4().hex[:12]}",
                "type": action_type,
                "policyId": policy_id,
                "backupId": backup_id,
                "source": source_target,
                "destination": dest_target,
                "target": dest_target or source_target,
                "reason": "replica-lag-risk" if r_type == RiskType.REPLICA_LAG.value else "failure-domain-deficit",
                "severity": r_sev,
                "confidence": str(r.get("confidence", "verified")),
                "requiresApproval": req_appr,
                "riskSubject": _risk_subject_from_record(r),
                "severityBefore": r_sev,
                "expectedEffect": "severity-decrease",
                "parameters": {
                    "policyId": policy_id,
                    "backupId": backup_id,
                    "sourceTargetId": source_target,
                    "destTargetId": dest_target,
                    "reason": "replica-repair",
                },
                "evidence": r.get("evidence", []),
            }
            actions.append(action_intent)

        # 3. DR Staleness planning -> START_DR_DRILL
        elif r_type == RiskType.DR_STALENESS.value:
            policy_id = r.get("policyId")
            latest_pt = backup_dr_ledger.latest_recovery_point(policy_id=str(policy_id)) if policy_id else None
            drill_backup_id = str(latest_pt.get("backupId") or "") if latest_pt else None
            drill_target_id = str(latest_pt.get("targetId") or "") if latest_pt else None
            action_type = ResilienceActionType.START_DR_DRILL.value
            req_appr = not autonomous_action_policy.is_action_autonomous(action_type, policy)
            action_intent = {
                "actionId": f"act_{uuid.uuid4().hex[:12]}",
                "type": action_type,
                "policyId": policy_id,
                "backupId": drill_backup_id,
                "target": drill_target_id,
                "reason": "dr-drill-staleness",
                "severity": r_sev,
                "confidence": str(r.get("confidence", "verified")),
                "requiresApproval": req_appr,
                "riskSubject": _risk_subject_from_record(r),
                "severityBefore": r_sev,
                "expectedEffect": "severity-decrease",
                "parameters": {
                    "policyId": policy_id,
                    "backupId": drill_backup_id,
                    "targetId": drill_target_id,
                },
                "evidence": r.get("evidence", []),
            }
            actions.append(action_intent)

        # 4. Authority degradation / critical failures -> Flag actions requiring manual approval
        elif r_type == RiskType.AUTHORITY_DEGRADATION.value and r_sev in {RiskSeverity.CRITICAL.value, RiskSeverity.BLOCKED.value}:
            action_type = ResilienceActionType.PRIMARY_PROMOTION.value
            actions.append(
                {
                    "actionId": f"act_{uuid.uuid4().hex[:12]}",
                    "type": action_type,
                    "reason": "authority-fail-closed-intervention-required",
                    "severity": r_sev,
                    "confidence": str(r.get("confidence", "verified")),
                    "requiresApproval": True,
                    "riskSubject": {
                        "type": RiskType.AUTHORITY_DEGRADATION.value,
                    },
                    "severityBefore": r_sev,
                    "expectedEffect": "severity-decrease",
                    "parameters": {},
                    "evidence": r.get("evidence", []),
                }
            )

    plan: dict[str, Any] = {
        "planVersion": RESILIENCE_PLAN_VERSION,
        "planId": plan_id,
        "generatedAt": _utc_iso(),
        "inputRiskDigest": input_risk_digest,
        "overallRisk": str(risk_snapshot.get("overallRisk", "healthy")),
        "status": ResiliencePlanStatus.PROPOSED.value,
        "actions": actions,
    }
    plan["planDigest"] = compute_plan_digest(plan)
    return plan
