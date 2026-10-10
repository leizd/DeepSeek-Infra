"""Legacy pure DR readiness aggregator."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from deepseek_infra.infra.workspace.backup_dr_readiness import (
    REQUIRED_RTO_STAGES,
    RTO_EVIDENCE_WINDOW_DAYS,
    _parse_time,
)

def aggregate_readiness(
    *,
    catalog_records: list[dict[str, Any]],
    committed_points: set[tuple[str, str]],
    stage_samples: list[dict[str, Any]],
    drill_records: list[dict[str, Any]],
    target_health: dict[str, dict[str, Any]],
    index_health: dict[tuple[str, str], dict[str, Any]],
    cache_health: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Pure readiness aggregation for testing and backward compatibility."""
    valid_records = [
        r for r in catalog_records
        if (str(r.get("targetId")), str(r.get("backupId"))) in committed_points
    ]
    by_id = {str(r.get("backupId")): r for r in valid_records}
    best_record = None
    best_chain = []
    for r in sorted(valid_records, key=lambda x: str(x.get("createdAt") or ""), reverse=True):
        curr = r
        chain = [curr]
        ok = True
        while curr.get("snapshotKind") == "incremental":
            parent_id = str(curr.get("parentBackupId") or "")
            if parent_id in by_id:
                curr = by_id[parent_id]
                chain.append(curr)
            else:
                ok = False
                break
        if ok:
            best_record = r
            best_chain = list(reversed(chain))
            break

    if best_record is not None:
        created_at = _parse_time(best_record.get("createdAt"))
        rpo_seconds = max(0, int((now - created_at).total_seconds())) if created_at else 0
        recovery_point = {
            "status": "available",
            "backupId": str(best_record.get("backupId") or ""),
            "targetId": str(best_record.get("targetId") or ""),
            "policyId": str(best_record.get("policyId") or ""),
            "snapshotKind": str(best_record.get("snapshotKind") or "full"),
            "chainLength": len(best_chain),
            "recoveryPointAt": best_record.get("createdAt"),
            "rpoSeconds": rpo_seconds,
            "source": "validated-commit-and-receipt",
        }
    else:
        recovery_point = {
            "status": "unavailable",
            "reason": "no-committed-recoverable-point",
            "source": "validated-commit-and-receipt",
        }

    missing_workload = []
    if not best_record or int(best_record.get("size") or 0) <= 0:
        missing_workload.append("ciphertextBytes")
    if not best_record or int(best_record.get("logicalBytes") or 0) <= 0:
        missing_workload.append("logicalBytes")

    cutoff = now.timestamp() - (RTO_EVIDENCE_WINDOW_DAYS * 86400)
    recent_samples = []
    for s in stage_samples:
        obs = _parse_time(s.get("observedAt"))
        if obs and cutoff <= obs.timestamp() <= now.timestamp():
            recent_samples.append(s)

    samples_by_stage: dict[str, int] = {}
    speeds_by_stage: dict[str, list[float]] = {}
    for req in REQUIRED_RTO_STAGES:
        st_samples = [s for s in recent_samples if s.get("stage") == req and s.get("result") == "success"]
        samples_by_stage[req] = len(st_samples)
        speeds = [
            int(s.get("bytes") or s.get("bytesTransferred") or 1) / (max(1.0, float(s.get("durationMs") or 1.0)) / 1000.0)
            for s in st_samples
        ]
        speeds_by_stage[req] = speeds

    missing_stages = [req for req in REQUIRED_RTO_STAGES if samples_by_stage.get(req, 0) == 0]

    if best_record is None:
        rto_estimate = {
            "status": "unavailable",
            "isSla": False,
            "reason": "recovery-point-unavailable",
        }
    elif missing_workload:
        rto_estimate = {
            "status": "unavailable",
            "isSla": False,
            "reason": "recovery-point-workload-unavailable",
            "missingWorkload": missing_workload,
        }
    elif missing_stages:
        rto_estimate = {
            "status": "unavailable",
            "isSla": False,
            "reason": "insufficient-recent-stage-throughput",
            "missingStages": missing_stages,
            "evidenceWindowDays": RTO_EVIDENCE_WINDOW_DAYS,
        }
    else:
        total_ciphertext_bytes = sum(int(r.get("size") or 0) for r in best_chain)
        logical_bytes = int(best_record.get("logicalBytes") or best_record.get("size") or 1000)

        t_speed = sum(speeds_by_stage["transfer"]) / len(speeds_by_stage["transfer"]) if speeds_by_stage["transfer"] else 1.0
        c_speed = sum(speeds_by_stage["crypto"]) / len(speeds_by_stage["crypto"]) if speeds_by_stage["crypto"] else 1.0
        m_speed = sum(speeds_by_stage["materialization"]) / len(speeds_by_stage["materialization"]) if speeds_by_stage["materialization"] else 1.0

        t_time = total_ciphertext_bytes / t_speed
        c_time = total_ciphertext_bytes / c_speed
        m_time = logical_bytes / m_speed

        total_time = t_time + c_time + m_time
        rto_estimate = {
            "status": "estimated",
            "estimatedSeconds": int(round(total_time)),
            "isSla": False,
            "evidence": {
                "samplesByStage": samples_by_stage,
            },
        }

    scrub_ok_samples = [
        r for r in catalog_records
        if r.get("scrubOk") or (r.get("ciphertextScrubbedAt") and r.get("scrubOk") is not False)
    ]
    successful_scrub_at = None
    if scrub_ok_samples:
        scrub_sorted = sorted(scrub_ok_samples, key=lambda x: str(x.get("ciphertextScrubbedAt") or x.get("createdAt") or ""), reverse=True)
        successful_scrub_at = scrub_sorted[0].get("ciphertextScrubbedAt") or scrub_sorted[0].get("createdAt")

    scrub_with_date = [r for r in catalog_records if r.get("ciphertextScrubbedAt")]
    if scrub_with_date:
        scrub_sorted_all = sorted(scrub_with_date, key=lambda x: str(x.get("ciphertextScrubbedAt") or ""), reverse=True)
        latest_scrub = scrub_sorted_all[0]
        scrub_status = "ok" if latest_scrub.get("scrubOk") else "error"
        scrub = {
            "status": scrub_status,
            "latestSuccessfulAt": successful_scrub_at,
            "source": "persisted-backup-scrub",
        }
    else:
        scrub = {
            "status": "unavailable",
            "source": "persisted-backup-scrub",
        }

    valid_drills = []
    for d in drill_records:
        obs = _parse_time(d.get("completedAt"))
        if obs and obs <= now:
            valid_drills.append(d)

    successful_drills = [d for d in valid_drills if d.get("result") == "success"]
    latest_drill_success = None
    if successful_drills:
        drill_sorted = sorted(successful_drills, key=lambda x: str(x.get("completedAt") or ""), reverse=True)
        latest_drill_success = drill_sorted[0].get("completedAt")

    if valid_drills:
        drill_sorted_all = sorted(valid_drills, key=lambda x: str(x.get("completedAt") or ""), reverse=True)
        latest_drill = drill_sorted_all[0]
        drill_status = "ok" if latest_drill.get("result") == "success" else "error"
        drill = {
            "status": drill_status,
            "latestSuccessfulAt": latest_drill_success,
            "source": "isolated-recovery-drill",
        }
    else:
        drill = {
            "status": "unavailable",
            "reason": "no-evidence",
            "source": "isolated-recovery-drill",
        }

    overall_status = "ok"
    if recovery_point["status"] != "available" or scrub["status"] == "error" or drill["status"] == "error" or rto_estimate["status"] != "estimated":
        overall_status = "warning" if recovery_point["status"] == "available" else "error"

    return {
        "recoveryPoint": recovery_point,
        "rtoEstimate": rto_estimate,
        "scrub": scrub,
        "drill": drill,
        "health": {
            "target": target_health,
            "index": index_health,
            "cache": cache_health,
        },
        "status": overall_status,
    }


