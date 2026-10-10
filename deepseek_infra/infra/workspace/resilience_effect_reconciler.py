"""Global Recovery Intelligence - Crash Recovery & Effect Reconciler (4.7.2 Gate C).

Inspects in-flight effectHandle and subsystem records when recovering from
worker crash or lease expiration. Avoids blind re-execution of remote side effects.
Transitions uncertain outcomes into EFFECT_UNKNOWN / NEEDS_OPERATOR.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_dr_readiness,
    backup_replication,
)


def _utc_iso(dt: datetime | None = None) -> str:
    current = dt or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def reconcile_action_effect(
    action: dict[str, Any],
    *,
    instance_id: str = "reconciliation-worker",
) -> tuple[str, dict[str, Any]]:
    """Inspect persistent effectHandle and underlying subsystem to reconcile action state."""
    act_type = str(action.get("type", "")).upper()
    act_id = str(action.get("actionId", ""))
    raw_effect_handle = action.get("effectHandle")
    effect_handle = raw_effect_handle if isinstance(raw_effect_handle, dict) else {}

    cur_state = str(action.get("state", "")).upper()
    if cur_state == "CLAIMED":
        return "RESUME_SIMULATING", {}

    try:
        if act_type == "CREATE_REPAIR_JOB":
            repair_id = effect_handle.get("repairId")
            job = None
            if repair_id:
                job = backup_replication.read_repair_job(repair_id)
            if not job:
                # Search by resilienceActionId
                repairs = backup_replication.list_repair_jobs()
                for r in repairs:
                    if r.get("resilienceActionId") == act_id:
                        job = r
                        break

            if not job:
                if repair_id or str(action.get("effectClass") or "") not in {"", "NO_EFFECT"}:
                    return "EFFECT_UNKNOWN", {"error": "repair-effect-handle-not-observed", "repairId": repair_id}
                return "RECREATE_EFFECT", {}

            phase = str(job.get("phase", "")).lower()
            if phase in {"complete", "healthy"}:
                return "ADVANCE_TO_VERIFYING", {"job": job, "repairId": job.get("repairId")}
            elif phase in {"failed", "failed-terminal"}:
                return "TRIGGER_COMPENSATION", {"error": job.get("error") or "repair-job-failed", "job": job}
            else:
                return "RESUME_EXECUTION", {"job": job, "repairId": job.get("repairId")}

        elif act_type == "CREATE_REBALANCE_JOB":
            job_id = effect_handle.get("jobId")
            reb_job = None
            if job_id:
                reb_job = backup_replication.read_rebalance_job(job_id)
            if not reb_job:
                # Search by resilienceActionId
                rebalances = backup_replication.list_rebalance_jobs()
                for rb in rebalances:
                    if rb.get("resilienceActionId") == act_id:
                        reb_job = rb
                        break

            if not reb_job:
                if job_id or str(action.get("effectClass") or "") not in {"", "NO_EFFECT"}:
                    return "EFFECT_UNKNOWN", {"error": "rebalance-effect-handle-not-observed", "jobId": job_id}
                return "RECREATE_EFFECT", {}

            phase = str(reb_job.get("phase", "")).lower()
            if phase == "complete":
                return "ADVANCE_TO_VERIFYING", {"job": reb_job, "jobId": reb_job.get("jobId")}
            elif phase == "failed":
                return "TRIGGER_COMPENSATION", {"error": reb_job.get("error") or "rebalance-job-failed", "job": reb_job}
            else:
                return "RESUME_EXECUTION", {"job": reb_job, "jobId": reb_job.get("jobId")}

        elif act_type == "START_DR_DRILL":
            # Search by resilienceActionId
            drill_rec = None
            for d in backup_dr_readiness._drill_records():  # noqa: SLF001
                if d.get("resilienceActionId") == act_id or (
                    isinstance(d.get("proof"), dict) and d["proof"].get("resilienceActionId") == act_id
                ):
                    drill_rec = d
                    break

            if not drill_rec:
                if effect_handle or str(action.get("effectClass") or "") not in {"", "NO_EFFECT"}:
                    return "EFFECT_UNKNOWN", {"error": "drill-effect-handle-not-observed"}
                return "RECREATE_EFFECT", {}

            if not isinstance(drill_rec, dict):
                return "RECREATE_EFFECT", {}

            if drill_rec.get("result") == "success" or drill_rec.get("status") == "success":
                raw_proof = drill_rec.get("proof")
                proof: dict[str, Any] = dict(raw_proof) if isinstance(raw_proof, dict) else {}
                return "ADVANCE_TO_VERIFYING", {
                    "status": "success",
                    "drillId": drill_rec.get("drillId"),
                    "backupId": drill_rec.get("backupId") or drill_rec.get("testedBackupId") or proof.get("backupId"),
                    "testedBackupId": drill_rec.get("backupId") or drill_rec.get("testedBackupId") or proof.get("backupId"),
                    "resilienceActionId": act_id,
                    "proof": proof,
                }
            else:
                return "TRIGGER_COMPENSATION", {"error": drill_rec.get("error") or "dr-drill-failed"}

        else:
            return "EFFECT_UNKNOWN", {"error": f"unrecognized-action-type:{act_type}"}

    except Exception as exc:
        return "EFFECT_UNKNOWN", {"error": f"reconciliation-exception:{exc}"}
