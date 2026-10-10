"""Global Recovery Intelligence - Recovery What-if Simulator (4.7.0 P1-2).

Simulates disaster recovery scenarios (AZ failure, target outages, primary
data corruption) against current recovery point topologies to evaluate
survivability, surviving copies, and projected RTO before real outages occur.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_policies,
    backup_targets,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _format_duration_human(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    remaining_sec = int(seconds % 60)
    if remaining_sec == 0:
        return f"{minutes}m"
    return f"{minutes}m {remaining_sec}s"


def simulate_recovery(
    scenario: str = "AZ_FAILURE",
    *,
    excluded_targets: list[str] | None = None,
    policy_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Simulate recovery survivability when specified targets or availability zones fail."""
    current = now or datetime.now(tz=timezone.utc)
    all_targets = backup_targets.list_targets()
    all_target_ids = ["managed-local"] + [str(t.get("targetId") or "") for t in all_targets]

    # Default to excluding the primary target or first target if none specified
    if not excluded_targets:
        if len(all_target_ids) > 1:
            excluded = [all_target_ids[1]]
        else:
            excluded = [all_target_ids[0]]
    else:
        excluded = [str(t) for t in excluded_targets]

    surviving_targets = [t for t in all_target_ids if t and t not in excluded]

    # Evaluate recovery points across surviving targets
    policies = backup_policies.list_policies()
    target_policies = [p for p in policies if not policy_id or str(p.get("policyId")) == policy_id]

    lost_copies_count = 0
    surviving_copies_count = 0
    survivable = True
    viable_targets: set[str] = set()
    bottlenecks: list[str] = []

    # Calculate estimated RTO from surviving targets
    estimated_rto_seconds = 0.0

    for pol in target_policies:
        pid = str(pol.get("policyId") or "")
        latest_pt = backup_dr_ledger.latest_recovery_point(policy_id=pid)
        if not latest_pt:
            continue

        backup_id = str(latest_pt.get("backupId") or "")
        copies = backup_dr_ledger.list_logical_recovery_copies(
            policy_id=pid,
            backup_id=backup_id,
        )

        committed_copies = [c for c in copies if str(c.get("status") or c.get("state") or "").lower() in {"committed", "active", "healthy"}]
        surviving_committed = [c for c in committed_copies if str(c.get("targetId") or "") in surviving_targets]
        lost_committed = [c for c in committed_copies if str(c.get("targetId") or "") in excluded]

        lost_copies_count += len(lost_committed)
        surviving_copies_count += len(surviving_committed)

        for c in surviving_committed:
            tid = str(c.get("targetId") or "")
            if tid:
                viable_targets.add(tid)

        if not surviving_committed:
            # If no committed replica survives on remaining targets, check if primary survives
            primary_target = str(pol.get("targetId") or "managed-local")
            if primary_target in excluded:
                survivable = False
                bottlenecks.append(f"policy-{pid}-has-no-surviving-copies")

    if not surviving_targets or not viable_targets:
        if not survivable or not surviving_targets:
            survivable = False
            estimated_rto_seconds = 0.0
            human_rto = "unavailable"
        else:
            estimated_rto_seconds = 600.0  # default baseline
            human_rto = _format_duration_human(estimated_rto_seconds)
    else:
        # Base transfer + materialization duration: 300s baseline + 60s per surviving target
        estimated_rto_seconds = 300.0 + (len(surviving_targets) * 30.0)
        human_rto = _format_duration_human(estimated_rto_seconds)

    report = {
        "scenario": scenario,
        "targetExcluded": excluded[0] if len(excluded) == 1 else excluded,
        "excludedTargets": excluded,
        "survivable": survivable,
        "estimatedRTO": human_rto,
        "estimatedRTOSeconds": int(estimated_rto_seconds),
        "lostCopies": lost_copies_count,
        "survivingCopies": surviving_copies_count,
        "viableTargets": sorted(list(viable_targets)),
        "bottlenecks": bottlenecks,
        "simulationPassed": survivable and len(bottlenecks) == 0,
        "generatedAt": _utc_iso(current),
    }
    return report


def run_comprehensive_simulation_suite(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run full test suite of standard disaster recovery simulations."""
    scenarios = [
        ("AZ_FAILURE", None),
        ("TARGET_OUTAGE", None),
        ("PRIMARY_CORRUPTION", None),
    ]

    results: list[dict[str, Any]] = []
    all_passed = True

    for sc_name, sc_excluded in scenarios:
        res = simulate_recovery(sc_name, excluded_targets=sc_excluded, now=now)
        results.append(res)
        if not res.get("simulationPassed"):
            all_passed = False

    return {
        "suitePassed": all_passed,
        "simulations": results,
        "generatedAt": _utc_iso(now),
    }
