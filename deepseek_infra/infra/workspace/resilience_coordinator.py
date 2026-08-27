"""Global Recovery Intelligence - Multi-Risk Resilience Coordinator (4.7.2 Gate H, I, J, K).

Orchestrates multi-risk recovery graphs into typed ResilienceCoordinationPlan v1.
Guarantees:
1. Multi-risk DAG dependencies (e.g. Repair before drain/rebalance).
2. Resource conflict serialization.
3. Authority-blocked global circuit breaking.
4. Blast-radius invariant validation (never drops below minCommittedCopies / minFailureDomains).
5. Atomic global & per-target safety budget constraints.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    autonomous_action_policy,
    backup_policies,
    resilience_planner,
)
from deepseek_infra.infra.workspace.resilience_risk_engine import RiskSeverity, RiskType

COORDINATION_PLAN_VERSION = 1


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compute_coordination_plan_digest(plan: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 digest of a coordination plan."""
    payload = {
        "coordinationPlanVersion": plan.get("coordinationPlanVersion", COORDINATION_PLAN_VERSION),
        "riskDigest": str(plan.get("riskDigest", "")),
        "objectives": sorted([str(o) for o in plan.get("objectives", [])]),
        "actions": sorted(
            [
                {
                    "actionId": str(a.get("actionId", "")),
                    "type": str(a.get("type", "")),
                    "policyId": str(a.get("policyId") or ""),
                    "backupId": str(a.get("backupId") or ""),
                    "source": str(a.get("source") or ""),
                    "destination": str(a.get("destination") or ""),
                    "requiresApproval": bool(a.get("requiresApproval")),
                }
                for a in plan.get("actions", [])
            ],
            key=lambda x: (x["type"], x["policyId"], x["backupId"], x["source"], x["destination"]),
        ),
        "dependencies": sorted([sorted([str(d[0]), str(d[1])]) for d in plan.get("dependencies", [])]),
        "conflicts": sorted([sorted([str(c[0]), str(c[1])]) for c in plan.get("conflicts", [])]),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def plan_coordinated_resilience(
    risk_snapshot: dict[str, Any],
    *,
    action_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a coordinated multi-risk recovery plan DAG (Gate H & I)."""
    risk_digest = str(risk_snapshot.get("riskDigest") or "")
    overall_risk = str(risk_snapshot.get("overallRisk", "healthy")).lower()
    base_plan = resilience_planner.plan_resilience_actions(risk_snapshot, action_policy=action_policy)
    actions = list(base_plan.get("actions", []))

    coordination_id = f"coord_{uuid.uuid4().hex[:16]}"
    objectives: list[str] = []
    dependencies: list[list[str]] = []
    conflicts: list[list[str]] = []

    # 1. Check Authority circuit-breaker
    auth_risk = next((r for r in risk_snapshot.get("risks", []) if str(r.get("type")) == RiskType.AUTHORITY_DEGRADATION.value), None)
    auth_blocked = auth_risk is not None and str(auth_risk.get("severity")).lower() in {RiskSeverity.CRITICAL.value, RiskSeverity.BLOCKED.value}

    if auth_blocked or overall_risk in {"blocked"}:
        objectives.append("authority-circuit-breaker-engaged")
        # Mark all mutations as requiring manual approval
        for act in actions:
            act["requiresApproval"] = True
            act["reason"] = f"blocked-by-authority-risk:{act.get('reason')}"
    else:
        for act in actions:
            act_type = str(act.get("type", ""))
            if act_type == "CREATE_REPAIR_JOB":
                objectives.append("restore-replica-durability")
            elif act_type == "CREATE_REBALANCE_JOB":
                objectives.append("relieve-capacity-pressure")
            elif act_type == "START_DR_DRILL":
                objectives.append("verify-dr-readiness")

    objectives = sorted(list(set(objectives)))

    # 2. Build Dependency & Conflict Graph across actions
    repair_actions = [a for a in actions if str(a.get("type")) == "CREATE_REPAIR_JOB"]
    rebalance_actions = [a for a in actions if str(a.get("type")) == "CREATE_REBALANCE_JOB"]
    drill_actions = [a for a in actions if str(a.get("type")) == "START_DR_DRILL"]

    # Invariant: Repair must precede Rebalance on the same policy / backup
    for rep in repair_actions:
        rep_pid = str(rep.get("policyId") or "")
        rep_bid = str(rep.get("backupId") or "")
        for reb in rebalance_actions:
            reb_pid = str(reb.get("policyId") or "")
            reb_bid = str(reb.get("backupId") or "")
            if rep_pid == reb_pid:
                # Same policy: repair must happen first to guarantee minimum replicas
                dependencies.append([str(rep["actionId"]), str(reb["actionId"])])
                if rep_bid == reb_bid and rep_bid:
                    conflicts.append([str(rep["actionId"]), str(reb["actionId"])])

    # Conflict: DR Drill conflicts with moving the selected backup copy
    for dr in drill_actions:
        dr_bid = str(dr.get("backupId") or "")
        for reb in rebalance_actions:
            reb_bid = str(reb.get("backupId") or "")
            if dr_bid and dr_bid == reb_bid:
                dependencies.append([str(dr["actionId"]), str(reb["actionId"])])
                conflicts.append([str(dr["actionId"]), str(reb["actionId"])])

    # 3. Blast-radius safety check (Gate F)
    wave_passed, wave_sim = simulate_coordination_wave(actions)
    for act in actions:
        pid = str(act.get("policyId") or act.get("parameters", {}).get("policyId") or "")
        bid = str(act.get("backupId") or act.get("parameters", {}).get("backupId") or "")
        key = f"{pid}:{bid}"
        eval_info = wave_sim.get("evaluations", {}).get(key)
        if eval_info and not eval_info.get("passed"):
            act["blastRadiusVerified"] = False
            act["requiresApproval"] = True
            act["reason"] = f"BLOCKED_BY_BLAST_RADIUS:{eval_info.get('reason')}"
        elif not wave_passed and not auth_blocked:
            act["blastRadiusVerified"] = False
            act["requiresApproval"] = True
            act["reason"] = f"BLOCKED_BY_BLAST_RADIUS:{wave_sim.get('reason')}"
        else:
            act["blastRadiusVerified"] = True
        if pid:
            try:
                pol = backup_policies.get_policy(pid) or {}
            except Exception:
                pol = {}
            repl_cfg = pol.get("replication", {}) if isinstance(pol, dict) else {}
            act["minCommittedCopies"] = int(repl_cfg.get("minCommittedCopies") or 1)

    # 4. Atomic global and target safety budget
    limits = autonomous_action_policy.get_action_rate_limits()
    budget = {
        "maxConcurrentActions": limits["maxConcurrentActions"],
        "maxConcurrentActionsPerTarget": 2,
        "maxConcurrentActionsPerPolicy": 2,
        "maxSimultaneousFailureDomainsTouched": 1,
        "repairReserve": 1,
        "drReserve": 1,
    }

    expected_risk_vector = {
        "overallRiskTarget": RiskSeverity.HEALTHY.value if not auth_blocked else "blocked",
        "targetSeverity": "healthy",
    }

    plan = {
        "coordinationPlanVersion": COORDINATION_PLAN_VERSION,
        "coordinationPlanId": coordination_id,
        "generatedAt": _utc_iso(),
        "riskDigest": risk_digest,
        "overallRisk": overall_risk,
        "objectives": objectives,
        "actions": actions,
        "dependencies": dependencies,
        "conflicts": conflicts,
        "budget": budget,
        "expectedRiskVector": expected_risk_vector,
        "blastRadiusSimulation": wave_sim,
        "status": "PROPOSED",
    }
    plan["planDigest"] = compute_coordination_plan_digest(plan)
    return plan


def simulate_coordination_wave(
    actions: list[dict[str, Any]],
    *,
    current_copies: dict[Any, list[dict[str, Any]]] | None = None,
    failure_domains: dict[str, str] | None = None,
    running_actions: list[dict[str, Any]] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Simulate transient replica copies and failure domains for a coordination wave (Gate F).

    Verifies that during execution:
      copiesDuring >= minCommittedCopies
      failureDomainsDuring >= minFailureDomains
    """
    from deepseek_infra.infra.workspace import (
        backup_dr_ledger,
        backup_policies,
        backup_targets,
    )

    try:
        all_targets = {t["targetId"]: t for t in backup_targets.list_targets()}
    except Exception:
        all_targets = {}

    domain_map: dict[str, str] = dict(failure_domains or {})
    for tid, t in all_targets.items():
        if tid not in domain_map:
            domain_map[tid] = str(t.get("failureDomain") or tid)

    policies_touched: dict[str, set[str]] = {}
    for act in (running_actions or []) + actions:
        pid = str(act.get("policyId") or act.get("parameters", {}).get("policyId") or "")
        bid = str(act.get("backupId") or act.get("parameters", {}).get("backupId") or "")
        if pid:
            policies_touched.setdefault(pid, set())
            if bid:
                policies_touched[pid].add(bid)

    policy_evaluations: dict[str, Any] = {}
    all_passed = True
    failure_reason = ""

    for pid, bids in policies_touched.items():
        try:
            pol = backup_policies.get_policy(pid) or {}
        except Exception:
            pol = {}
        repl_cfg = pol.get("replication", {}) if isinstance(pol, dict) else {}
        min_copies = int(repl_cfg.get("minCommittedCopies") or 1)
        min_fds = int(repl_cfg.get("minFailureDomains") or 1)

        effective_bids = set(bids)
        if not effective_bids:
            try:
                pt = backup_dr_ledger.latest_recovery_point(policy_id=pid)
            except Exception:
                pt = None
            if pt and pt.get("backupId"):
                effective_bids.add(str(pt["backupId"]))

        for bid in effective_bids:
            if current_copies and (pid, bid) in current_copies:
                raw_copies = current_copies[(pid, bid)]
            elif current_copies and bid in current_copies:
                raw_copies = current_copies[bid]
            else:
                try:
                    raw_copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=pid, backup_id=bid)
                except Exception:
                    raw_copies = []

            committed_before = [
                c for c in raw_copies
                if str(c.get("status") or c.get("state") or "").lower() in {"committed", "active", "healthy"}
            ]
            copies_before = len(committed_before)
            domains_before = {
                domain_map.get(str(c.get("targetId")), str(c.get("failureDomain") or c.get("targetId") or "default"))
                for c in committed_before
            }

            transient_copy_loss = 0
            transient_domain_loss: set[str] = set()

            policy_wave_actions = [
                a for a in actions
                if str(a.get("policyId") or a.get("parameters", {}).get("policyId") or "") == pid
                and (
                    not str(a.get("backupId") or a.get("parameters", {}).get("backupId") or "")
                    or str(a.get("backupId") or a.get("parameters", {}).get("backupId") or "") == bid
                )
            ]

            added_copies = 0
            added_domains: set[str] = set()

            for act in policy_wave_actions:
                atype = str(act.get("type") or "").upper()
                params = act.get("parameters", {}) if isinstance(act.get("parameters"), dict) else {}
                if atype == "CREATE_REPAIR_JOB":
                    dst = str(params.get("destTargetId") or act.get("destination") or "")
                    added_copies += 1
                    if dst:
                        added_domains.add(domain_map.get(dst, dst))
                elif atype == "CREATE_REBALANCE_JOB":
                    src = str(params.get("sourceTargetId") or act.get("source") or "")
                    dst = str(params.get("destTargetId") or act.get("destination") or "")
                    src_target = all_targets.get(src, {})
                    if str(src_target.get("status") or src_target.get("drainState") or "").lower() in {"draining", "retiring", "failed", "drained"}:
                        transient_copy_loss += 1
                        if src in domain_map:
                            transient_domain_loss.add(domain_map[src])
                    if dst:
                        added_domains.add(domain_map.get(dst, dst))

            copies_during = max(0, copies_before - transient_copy_loss)
            domains_during = domains_before - transient_domain_loss
            copies_after = copies_during + added_copies
            domains_after = domains_during | added_domains

            eval_entry = {
                "policyId": pid,
                "backupId": bid,
                "minCommittedCopies": min_copies,
                "minFailureDomains": min_fds,
                "copiesBefore": copies_before,
                "copiesDuring": copies_during,
                "copiesAfter": copies_after,
                "failureDomainsBefore": sorted(list(domains_before)),
                "failureDomainsDuring": sorted(list(domains_during)),
                "failureDomainsAfter": sorted(list(domains_after)),
                "passed": True,
            }

            # Invariant: If there were already sufficient copies, during execution we must not violate minimum
            if copies_before >= min_copies and copies_during < min_copies:
                eval_entry["passed"] = False
                eval_entry["reason"] = f"copies-during-insufficient:{copies_during}<{min_copies}"
                all_passed = False
                failure_reason = str(eval_entry["reason"])

            if len(domains_before) >= min_fds and len(domains_during) < min_fds:
                eval_entry["passed"] = False
                eval_entry["reason"] = f"failure-domains-during-insufficient:{len(domains_during)}<{min_fds}"
                all_passed = False
                failure_reason = str(eval_entry["reason"])

            policy_evaluations[f"{pid}:{bid}"] = eval_entry

    return all_passed, {
        "passed": all_passed,
        "evaluations": policy_evaluations,
        "reason": failure_reason if not all_passed else "blast-radius-invariants-satisfied",
    }

