"""Global Recovery Intelligence - Scoped Outcome & Risk Reduction Verifier (4.7.2 Gate E & F).

Guarantees:
1. Real Outcome Verification:
   - Repair: executed to complete, authenticated with Receipt/Commit, failure domain objective met.
   - Rebalance: executed to complete (not just jobId creation), destination committed & authenticated.
   - DR Drill: valid DR readiness proof.
2. Scoped Risk Effect Verification:
   - Matches exact riskSubject (type, policyId, backupId, targetId).
   - Derives effectObserved = (severityAfter < severityBefore).
   - Fails outcome gate if target risk unchanged or worsened.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_policies,
    backup_publish,
    backup_replication,
    backup_targets,
    evidence_proof,
)
from deepseek_infra.infra.workspace.resilience_risk_engine import SEVERITY_ORDER, RiskSeverity


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_target_safely(target_id: str) -> Any:
    """Resolve target safely across test and production environments."""
    if not target_id:
        return None
    try:
        return backup_publish.resolve_target(target_id, write_intent=False)
    except Exception:
        try:
            target_dict = backup_targets.get_target(target_id)
            if target_dict:
                p_str = str(target_dict.get("path") or "")
                root_path = Path(p_str) if p_str else None
                return backup_publish.ResolvedTarget(
                    target_id=target_id,
                    root=root_path,
                    managed=False,
                    kind=str(target_dict.get("kind") or "filesystem"),
                )
        except Exception:
            pass
    return backup_publish.ResolvedTarget(
        target_id=target_id,
        root=None,
        managed=False,
        kind="unknown",
    )


def verify_action_outcome(
    action: dict[str, Any],
    execution_result: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Verify real underlying outcome post-conditions (Gate E)."""
    act_type = str(action.get("type", "")).upper()
    raw_params = action.get("parameters")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    if act_type == "CREATE_REPAIR_JOB":
        raw_repair_job = execution_result.get("job")
        repair_job = raw_repair_job if isinstance(raw_repair_job, dict) else {}
        if repair_job.get("status") == "failed" or repair_job.get("phase") in {"failed", "failed-terminal"} or execution_result.get("status") == "failed":
            return False, {"executionVerified": False, "error": repair_job.get("error") or execution_result.get("error") or "repair-job-failed"}

        pid = str(params.get("policyId") or action.get("policyId") or "")
        bid = str(params.get("backupId") or action.get("backupId") or "")
        dst = str(params.get("destTargetId") or action.get("destination") or action.get("target") or "")

        # Execute or resume repair synchronously if not complete
        repair_id = repair_job.get("repairId") or execution_result.get("repairId")
        if repair_id:
            cur_job = backup_replication.read_repair_job(repair_id)
            if cur_job and cur_job.get("phase") not in {"complete", "healthy"}:
                try:
                    rep_res = backup_replication.execute_replica_repair(
                        policy_id=pid,
                        backup_id=bid,
                        dest_target_id=dst,
                        run_id=repair_id,
                    )
                    if rep_res.get("phase") not in {"complete", "healthy"} and rep_res.get("status") not in {"success", "complete"}:
                        return False, {"executionVerified": False, "error": f"repair-phase-not-complete:{rep_res.get('phase') or rep_res.get('status')}"}
                except Exception as exc:
                    return False, {"executionVerified": False, "error": f"repair-execution-failed:{exc}"}

        # Check ledger for committed copies
        copies = backup_dr_ledger.list_logical_recovery_copies(policy_id=pid, backup_id=bid)
        committed = [c for c in copies if str(c.get("status") or c.get("state") or "").lower() in {"committed", "active", "healthy"}]
        try:
            policy = backup_policies.get_policy(pid) or {}
        except Exception:
            policy = {}
        repl_cfg = policy.get("replication", {}) if isinstance(policy, dict) else {}
        min_copies = int(repl_cfg.get("minCommittedCopies") or 1)
        min_fds = int(repl_cfg.get("minFailureDomains") or 1)

        if len(committed) < min_copies:
            return False, {
                "executionVerified": False,
                "error": f"committed-copies-insufficient:{len(committed)}<{min_copies}",
                "committedCopies": len(committed),
            }

        # Authenticate destination copy if target is specified
        if dst:
            dest_target = _resolve_target_safely(dst)
            try:
                auth_status, receipt, commit = backup_replication.authenticate_committed_copy(dest_target, pid, bid)
                if auth_status != "authenticated" or receipt is None or commit is None:
                    return False, {
                        "executionVerified": False,
                        "error": f"repair-destination-authentication-failed:{auth_status}",
                    }
            except Exception as exc:
                return False, {"executionVerified": False, "error": f"destination-target-auth-error:{exc}"}

        # Verify failure domain objective
        all_targets = {t["targetId"]: t for t in backup_targets.list_targets()}
        domains = {
            str((all_targets.get(str(c.get("targetId"))) or {}).get("failureDomain") or c.get("targetId") or "default")
            for c in committed
        }
        if len(domains) < min_fds:
            return False, {
                "executionVerified": False,
                "error": f"failure-domain-objective-unsatisfied:{len(domains)}<{min_fds}",
                "failureDomains": sorted(list(domains)),
            }

        return True, {
            "executionVerified": True,
            "committedCopies": len(committed),
            "destinationTarget": dst,
            "failureDomains": sorted(list(domains)),
            "verifiedAt": _utc_iso(),
        }

    elif act_type == "CREATE_REBALANCE_JOB":
        raw_reb_job = execution_result.get("job")
        reb_job = raw_reb_job if isinstance(raw_reb_job, dict) else {}
        if reb_job.get("status") == "failed" or reb_job.get("phase") in {"failed", "failed-terminal"} or execution_result.get("status") == "failed":
            return False, {"executionVerified": False, "error": reb_job.get("error") or execution_result.get("error") or "rebalance-job-failed"}

        src = str(params.get("sourceTargetId") or action.get("source") or action.get("target") or "")
        dst = str(params.get("destTargetId") or action.get("destination") or "")
        pid = str(params.get("policyId") or action.get("policyId") or "")
        bid = str(params.get("backupId") or action.get("backupId") or "")

        job_id = reb_job.get("jobId") or execution_result.get("jobId")
        if not job_id:
            return False, {"executionVerified": False, "error": "rebalance-job-id-missing"}

        # Execute rebalance job if not yet completed
        cur_job = backup_replication.read_rebalance_job(job_id)
        if cur_job and cur_job.get("phase") != "complete":
            try:
                exec_res = backup_replication.execute_rebalance_job(job_id)
                if exec_res.get("status") != "success":
                    return False, {"executionVerified": False, "error": f"rebalance-execution-failed:{exec_res.get('error')}"}
            except Exception as exc:
                return False, {"executionVerified": False, "error": f"rebalance-execution-exception:{exc}"}

        # Verify destination copy is committed & authenticated
        if dst and pid and bid:
            dest_target = _resolve_target_safely(dst)
            try:
                d_status, d_receipt, d_commit = backup_replication.authenticate_committed_copy(dest_target, pid, bid)
                if d_status != "authenticated" or d_receipt is None or d_commit is None:
                    return False, {"executionVerified": False, "error": f"rebalance-destination-not-authenticated:{d_status}"}
            except Exception as exc:
                return False, {"executionVerified": False, "error": f"rebalance-destination-auth-error:{exc}"}

        return True, {
            "executionVerified": True,
            "rebalanceJobId": job_id,
            "sourceTargetId": src,
            "destTargetId": dst,
            "verifiedAt": _utc_iso(),
        }

    elif act_type == "START_DR_DRILL":
        success = execution_result.get("success") is True or str(execution_result.get("status") or "").lower() in {"pass", "success"}
        if not success:
            return False, {
                "executionVerified": False,
                "error": execution_result.get("error") or "dr-drill-failed",
            }
        raw_proof = execution_result.get("proof")
        if not isinstance(raw_proof, dict) or not raw_proof:
            return False, {"executionVerified": False, "error": "dr-drill-proof-required"}
        proof = raw_proof
        proof_errors = evidence_proof.validate_dr_readiness_proof(proof, "drReadinessProofValid")
        if proof_errors:
            return False, {
                "executionVerified": False,
                "error": "dr-drill-proof-invalid:" + ",".join(proof_errors),
                "proofErrors": proof_errors,
            }

        action_id = str(action.get("actionId") or "")
        result_drill_id = str(execution_result.get("drillId") or "")
        action_backup_id = str(params.get("backupId") or action.get("backupId") or "")
        result_backup_id = str(execution_result.get("backupId") or execution_result.get("testedBackupId") or "")
        proof_bindings = (
            ("resilienceActionId", action_id),
            ("drillId", result_drill_id),
            ("backupId", action_backup_id or result_backup_id),
        )
        for field, expected in proof_bindings:
            observed = str(proof.get(field) or "")
            if not expected or observed != expected:
                return False, {
                    "executionVerified": False,
                    "error": f"dr-drill-proof-{field}-mismatch:expected={expected or '<missing>'},observed={observed or '<missing>'}",
                }
        if action_backup_id and result_backup_id and action_backup_id != result_backup_id:
            return False, {
                "executionVerified": False,
                "error": f"dr-drill-result-backupId-mismatch:action={action_backup_id},result={result_backup_id}",
            }

        return True, {
            "executionVerified": True,
            "drillId": result_drill_id,
            "backupId": str(proof["backupId"]),
            "resilienceActionId": action_id,
            "proofSchema": str(proof["schema"]),
            "verifiedAt": _utc_iso(),
        }

    return False, {"executionVerified": False, "error": f"unsupported-verification-type:{act_type}"}


def find_matching_risk(
    risk_subject: dict[str, Any],
    risk_snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    """Find a risk whose declared scope exactly matches RiskSubject v1."""
    subject_scope = {
        "type": str(risk_subject.get("type") or "").upper(),
        "policyId": str(risk_subject.get("policyId") or ""),
        "backupId": str(risk_subject.get("backupId") or ""),
        "targetId": str(risk_subject.get("targetId") or risk_subject.get("target") or ""),
        "failureDomain": str(risk_subject.get("failureDomain") or ""),
    }

    for r in risk_snapshot.get("risks", []):
        risk_scope = {
            "type": str(r.get("type") or "").upper(),
            "policyId": str(r.get("policyId") or ""),
            "backupId": str(r.get("backupId") or ""),
            "targetId": str(r.get("targetId") or r.get("target") or ""),
            "failureDomain": str(r.get("failureDomain") or ""),
        }
        if any(expected and risk_scope[field] != expected for field, expected in subject_scope.items()):
            continue
        return r
    return None


def verify_scoped_risk_reduction(
    action: dict[str, Any],
    risk_before_snapshot: dict[str, Any],
    risk_after_snapshot: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Verify scoped risk reduction for the specific subject targeted by the action (Gate F)."""
    raw_subj = action.get("riskSubject")
    raw_params = action.get("parameters")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    act_type = str(action.get("type") or "").upper()

    # Synthesize riskSubject if missing from legacy actions
    if not isinstance(raw_subj, dict) or not raw_subj.get("type"):
        if act_type == "CREATE_REPAIR_JOB":
            risk_subject = {
                "type": "REPLICA_LAG",
                "policyId": str(params.get("policyId") or action.get("policyId") or ""),
                "backupId": str(params.get("backupId") or action.get("backupId") or ""),
                "targetId": str(params.get("destTargetId") or action.get("destination") or ""),
            }
        elif act_type == "CREATE_REBALANCE_JOB":
            risk_subject = {
                "type": "CAPACITY_EXHAUSTION",
                "targetId": str(params.get("sourceTargetId") or action.get("source") or action.get("target") or ""),
                "policyId": str(params.get("policyId") or action.get("policyId") or ""),
            }
        elif act_type == "START_DR_DRILL":
            risk_subject = {
                "type": "DR_STALENESS",
                "policyId": str(params.get("policyId") or action.get("policyId") or ""),
            }
        else:
            risk_subject = {"type": act_type}
    else:
        risk_subject = dict(raw_subj)

    risk_before = find_matching_risk(risk_subject, risk_before_snapshot)
    risk_after = find_matching_risk(risk_subject, risk_after_snapshot)

    if risk_before is None:
        return False, {
            "riskSubject": risk_subject,
            "effectObserved": False,
            "reason": "target-risk-subject-not-observed-before",
        }

    sev_before = str(risk_before.get("severity") if risk_before else action.get("severityBefore") or action.get("severity") or "warning").lower()
    sev_after = str(risk_after.get("severity") if risk_after else RiskSeverity.HEALTHY.value).lower()

    order_before = SEVERITY_ORDER.get(sev_before, 2)
    order_after = SEVERITY_ORDER.get(sev_after, 1)

    details = {
        "riskSubject": risk_subject,
        "severityBefore": sev_before,
        "severityAfter": sev_after,
        "orderBefore": order_before,
        "orderAfter": order_after,
    }

    # If the risk cleared completely from after snapshot -> healthy
    if risk_after is None or sev_after == RiskSeverity.HEALTHY.value:
        details["effectObserved"] = True
        details["reason"] = "target-risk-cleared-or-healthy"
        return True, details

    # If severity decreased (e.g. critical -> degraded / warning)
    if order_after < order_before:
        details["effectObserved"] = True
        details["reason"] = f"target-risk-severity-decreased:{sev_before}->{sev_after}"
        return True, details

    # Target risk remained unchanged or worsened -> fail closed!
    details["effectObserved"] = False
    details["reason"] = f"target-risk-not-improved:before={sev_before},after={sev_after}"
    return False, details
