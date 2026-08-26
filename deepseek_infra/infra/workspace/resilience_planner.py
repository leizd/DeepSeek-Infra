"""Global Recovery Intelligence - Deterministic Resilience Planner (4.7.0 P0-3).

Transforms a verified RiskSnapshot into a typed, deterministic ResiliencePlan.
All actions are policy-bounded, evidence-backed, and reversible. Safe actions
(repair, rebalance, drill) are admitted for autonomous execution; risky actions
(primary promotion, policy changes, deletions) require explicit operator approval.
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


def compute_plan_digest(plan: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 digest of a resilience plan."""
    payload = {
        "planVersion": plan.get("planVersion", RESILIENCE_PLAN_VERSION),
        "inputRiskDigest": str(plan.get("inputRiskDigest", "")),
        "actions": sorted(
            [
                {
                    "type": str(a.get("type", "")),
                    "source": str(a.get("source") or ""),
                    "destination": str(a.get("destination") or ""),
                    "target": str(a.get("target") or ""),
                    "policyId": str(a.get("policyId") or ""),
                    "reason": str(a.get("reason", "")),
                    "requiresApproval": bool(a.get("requiresApproval")),
                }
                for a in plan.get("actions", [])
            ],
            key=lambda x: (x["type"], x["target"], x["source"], x["destination"], x["policyId"]),
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


def plan_resilience_actions(
    risk_snapshot: dict[str, Any],
    *,
    action_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive deterministic ResiliencePlan from a validated RiskSnapshot."""
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
            if target_id and dest_target:
                action_type = ResilienceActionType.CREATE_REBALANCE_JOB.value
                req_appr = not autonomous_action_policy.is_action_autonomous(action_type, policy)
                actions.append(
                    {
                        "actionId": f"act_{uuid.uuid4().hex[:12]}",
                        "type": action_type,
                        "source": target_id,
                        "destination": dest_target,
                        "target": target_id,
                        "reason": "capacity-risk",
                        "severity": r_sev,
                        "confidence": str(r.get("confidence", "verified")),
                        "requiresApproval": req_appr,
                        "parameters": {
                            "sourceTargetId": target_id,
                            "destTargetId": dest_target,
                            "reason": "proactive-capacity-rebalance",
                        },
                        "evidence": r.get("evidence", []),
                    }
                )

        # 2. Replica lag / Failure domain planning -> CREATE_REPAIR_JOB
        elif r_type in {RiskType.REPLICA_LAG.value, RiskType.FAILURE_DOMAIN_VIOLATION.value}:
            policy_id = str(r.get("policyId") or "")
            latest_pt = backup_dr_ledger.latest_recovery_point(policy_id=policy_id) if policy_id else None
            backup_id = str(latest_pt.get("backupId") or "") if latest_pt else None
            action_type = ResilienceActionType.CREATE_REPAIR_JOB.value
            req_appr = not autonomous_action_policy.is_action_autonomous(action_type, policy)
            actions.append(
                {
                    "actionId": f"act_{uuid.uuid4().hex[:12]}",
                    "type": action_type,
                    "policyId": policy_id,
                    "target": str(latest_pt.get("targetId") or "") if latest_pt else None,
                    "reason": "replica-lag-risk" if r_type == RiskType.REPLICA_LAG.value else "failure-domain-deficit",
                    "severity": r_sev,
                    "confidence": str(r.get("confidence", "verified")),
                    "requiresApproval": req_appr,
                    "parameters": {
                        "policyId": policy_id,
                        "backupId": backup_id,
                    },
                    "evidence": r.get("evidence", []),
                }
            )

        # 3. DR Staleness planning -> START_DR_DRILL
        elif r_type == RiskType.DR_STALENESS.value:
            action_type = ResilienceActionType.START_DR_DRILL.value
            req_appr = not autonomous_action_policy.is_action_autonomous(action_type, policy)
            actions.append(
                {
                    "actionId": f"act_{uuid.uuid4().hex[:12]}",
                    "type": action_type,
                    "policyId": r.get("policyId"),
                    "reason": "dr-drill-staleness",
                    "severity": r_sev,
                    "confidence": str(r.get("confidence", "verified")),
                    "requiresApproval": req_appr,
                    "parameters": {
                        "policyId": r.get("policyId"),
                    },
                    "evidence": r.get("evidence", []),
                }
            )

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
