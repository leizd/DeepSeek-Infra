from __future__ import annotations

import ast
import copy
import json
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import backup_replication as br
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "a37735c68398fc8f795babaa269e2de6a5acd567"
NOW = "2026-09-05T15:00:00Z"
CORPUS_REL = "compat/native-runtime/v22/transfer/repair_job_phase_vector.json"
AST_NAMES = (
    "REPAIR_JOB_SCHEMA_VERSION",
    "REPAIR_TERMINAL_PHASES",
    "REPAIR_ACTIVE_PHASES",
    "REBALANCE_JOB_SCHEMA_VERSION",
    "create_repair_job",
    "_set_repair_phase",
    "cancel_repair_job",
    "_compute_repair_backoff_seconds",
    "execute_repair_job_instance",
    "process_pending_repairs",
    "create_rebalance_job",
    "_set_rebalance_phase",
    "cancel_rebalance_job",
)


def _dump_named(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.dump(node)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.dump(node)
    raise AssertionError(name)


def test_repair_job_helpers_ast_match_frozen_4_8_0() -> None:
    current = ast.parse((ROOT / "deepseek_infra/infra/workspace/backup_replication.py").read_text(encoding="utf-8"))
    frozen = ast.parse(
        subprocess.check_output(
            ["git", "show", f"{SOURCE_COMMIT}:deepseek_infra/infra/workspace/backup_replication.py"],
            encoding="utf-8",
        )
    )
    assert isinstance(current, ast.Module)
    assert isinstance(frozen, ast.Module)
    for name in AST_NAMES:
        assert _dump_named(current, name) == _dump_named(frozen, name), name


def _set_phase(job: dict[str, Any], phase: Any, extra: dict[str, Any], now: str) -> dict[str, Any]:
    job = dict(job)
    job["phase"] = phase
    job["updatedAt"] = now
    for key, value in extra.items():
        job[key] = value
    return job


def _int_or(value: Any, fallback: int) -> int:
    return int(value or fallback)


def replay(case: dict[str, Any], now: str = NOW) -> dict[str, Any]:
    op = case["op"]
    if op == "backoff":
        return {"seconds": br._compute_repair_backoff_seconds(int(case["attempt"]))}
    if op == "set-repair-phase":
        return {"job": _set_phase(copy.deepcopy(case["job"]), case["phase"], copy.deepcopy(case.get("extra") or {}), now)}
    if op == "set-rebalance-phase":
        return {"job": _set_phase(copy.deepcopy(case["job"]), case["phase"], copy.deepcopy(case.get("extra") or {}), now)}
    if op == "create-repair":
        return _create_repair(case, now)
    if op == "create-rebalance":
        return _create_rebalance(case, now)
    if op == "cancel-repair":
        return _cancel_repair(case, now)
    if op == "cancel-rebalance":
        return _cancel_rebalance(case, now)
    if op == "claim-repair":
        return _claim_repair(case, now)
    if op == "classify-pending":
        return _classify_pending(case, now)
    if op == "route-error":
        return _route_error(case, now)
    raise AssertionError(op)


def _create_repair(case: dict[str, Any], now: str) -> dict[str, Any]:
    fields = copy.deepcopy(case["fields"])
    action = fields.get("resilienceActionId")
    if action:
        for job in case.get("existing_jobs") or []:
            if job.get("resilienceActionId") == action:
                return {"status": "existing", "job": copy.deepcopy(job)}
    if "trafficClass" in fields:
        traffic: Any = fields["trafficClass"]
    else:
        traffic = 2
    try:
        traffic_i = int(traffic)
    except (TypeError, ValueError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__}
    job = {
        "schemaVersion": br.REPAIR_JOB_SCHEMA_VERSION,
        "repairId": fields.get("repairId"),
        "resilienceActionId": action,
        "policyId": fields.get("policyId"),
        "backupId": fields.get("backupId"),
        "sourceTargetId": fields.get("sourceTargetId"),
        "destTargetId": fields.get("destTargetId"),
        "objectSetDigest": fields.get("objectSetDigest"),
        "repairMode": "auto",
        "trafficClass": traffic_i,
        "phase": "queued",
        "components": {},
        "bytesRepaired": 0,
        "attempt": 0,
        "maxAttempts": 5,
        "nextAttemptAt": None,
        "holdId": None,
        "createdAt": now,
        "updatedAt": now,
        "error": None,
    }
    return {"status": "created", "job": job}


def _create_rebalance(case: dict[str, Any], now: str) -> dict[str, Any]:
    fields = copy.deepcopy(case["fields"])
    action = fields.get("resilienceActionId")
    for job in case.get("existing_jobs") or []:
        if action and job.get("resilienceActionId") == action:
            return {"status": "existing", "job": copy.deepcopy(job)}
        if job.get("phase") not in {"complete", "failed", "cancelled"}:
            return {"status": "existing", "job": copy.deepcopy(job)}
    body = {
        "schemaVersion": br.REBALANCE_JOB_SCHEMA_VERSION,
        "jobId": fields.get("jobId"),
        "resilienceActionId": action,
        "policyId": fields.get("policyId"),
        "backupId": fields.get("backupId"),
        "sourceTargetId": fields.get("sourceTargetId"),
        "destTargetId": fields.get("destTargetId"),
        "reason": fields["reason"] if "reason" in fields else "failure-domain-rebalance",
        "pruneSourceAfter": fields["pruneSourceAfter"] if "pruneSourceAfter" in fields else False,
        "phase": "pending",
        "bytesTransferred": 0,
        "createdAt": now,
        "updatedAt": now,
    }
    return {"status": "created", "job": body}


def _cancel_repair(case: dict[str, Any], now: str) -> dict[str, Any]:
    repair_id = str(case["repair_id"])
    job = case.get("job")
    reason = case["reason"] if "reason" in case else "resilience-action-compensation"
    if job is None:
        return {"status": "unknown", "repairId": repair_id, "reason": "repair-job-not-found"}
    job = copy.deepcopy(job)
    phase = str(job.get("phase") or "")
    if phase == "cancelled":
        return {"status": "cancelled", "repairId": repair_id, "phase": phase, "job": job}
    if phase != "queued":
        return {"status": "not-cancelable", "repairId": repair_id, "phase": phase, "job": job}
    cancelled = _set_phase(job, "cancelled", {"cancellationReason": reason, "cancelledAt": now}, now)
    if "observed" in case:
        observed = case["observed"]
    else:
        observed = cancelled
    if not observed or str(observed.get("phase") or "") != "cancelled":
        return {"status": "unknown", "repairId": repair_id, "reason": "repair-cancellation-not-observed"}
    return {"status": "cancelled", "repairId": repair_id, "phase": "cancelled", "job": cancelled}


def _cancel_rebalance(case: dict[str, Any], now: str) -> dict[str, Any]:
    job_id = str(case["job_id"])
    job = case.get("job")
    reason = case["reason"] if "reason" in case else "resilience-action-compensation"
    if job is None:
        return {"status": "unknown", "jobId": job_id, "reason": "rebalance-job-not-found"}
    job = copy.deepcopy(job)
    phase = str(job.get("phase") or "")
    if phase == "cancelled":
        return {"status": "cancelled", "jobId": job_id, "phase": phase, "job": job}
    if phase != "pending":
        return {"status": "not-cancelable", "jobId": job_id, "phase": phase, "job": job}
    cancelled = _set_phase(job, "cancelled", {"cancellationReason": reason, "cancelledAt": now}, now)
    if "observed" in case:
        observed = case["observed"]
    else:
        observed = cancelled
    if not observed or str(observed.get("phase") or "") != "cancelled":
        return {"status": "unknown", "jobId": job_id, "reason": "rebalance-cancellation-not-observed"}
    return {"status": "cancelled", "jobId": job_id, "phase": "cancelled", "job": cancelled}


def _claim_repair(case: dict[str, Any], now: str) -> dict[str, Any]:
    repair_id = case["repair_id"]
    job = case.get("job")
    if job is None:
        return {"status": "error", "error": "not-found", "repairId": repair_id}
    job = copy.deepcopy(job)
    if str(job.get("phase") or "") in br.REPAIR_TERMINAL_PHASES:
        status = "success" if job.get("phase") == "healthy" else str(job.get("phase"))
        return {"status": status, "repairId": repair_id, "job": job}
    try:
        attempt = _int_or(job.get("attempt"), 0) + 1
        max_attempts = _int_or(job.get("maxAttempts"), 5)
    except (TypeError, ValueError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__, "repairId": repair_id, "job": job}
    if attempt > max_attempts:
        job = _set_phase(job, "failed-terminal", {"error": "max-attempts-exceeded", "attempt": attempt}, now)
        return {"status": "error", "error": "max-attempts-exceeded", "repairId": repair_id, "job": job}
    job = _set_phase(job, "selecting-source", {"attempt": attempt}, now)
    return {"status": "claimed", "repairId": repair_id, "job": job}


def _classify_pending(case: dict[str, Any], now: str) -> dict[str, Any]:
    job = copy.deepcopy(case["job"])
    phase = str(job.get("phase") or "")
    if phase not in br.REPAIR_ACTIVE_PHASES and phase != "queued":
        return {"decision": "skip-inactive", "job": job}
    next_at = br._parse_iso(job.get("nextAttemptAt"))
    current = br._parse_iso(now)
    assert current is not None
    if next_at is not None and current < next_at:
        return {"decision": "skip-backoff", "job": job}
    try:
        max_attempts = _int_or(job.get("maxAttempts"), 5)
        attempt = _int_or(job.get("attempt"), 0)
    except (TypeError, ValueError) as exc:
        return {"decision": "error", "oracle_exception": type(exc).__name__, "job": job}
    if attempt >= max_attempts and phase in {"retry-wait", "queued"}:
        job = _set_phase(job, "failed-terminal", {"error": "max-attempts-exceeded"}, now)
        return {"decision": "fail-max-attempts", "job": job}
    return {"decision": "pending", "job": job}


def _route_error(case: dict[str, Any], now: str) -> dict[str, Any]:
    job = copy.deepcopy(case["job"])
    err_msg = str(case.get("error") or "")
    kind = str(case.get("kind") or "generic")
    try:
        attempt = _int_or(job.get("attempt"), 0)
        max_attempts = _int_or(job.get("maxAttempts"), 5)
    except (TypeError, ValueError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__, "job": job}
    current = br._parse_iso(now)
    assert current is not None

    def retry() -> dict[str, Any]:
        backoff = br._compute_repair_backoff_seconds(attempt)
        next_at = br._utc_iso(current + timedelta(seconds=backoff))
        updated = _set_phase(job, "retry-wait", {"error": err_msg, "nextAttemptAt": next_at}, now)
        return {"status": "retry-wait", "job": updated}

    if kind == "lease-lost":
        return retry()
    if "source component corrupt" in err_msg.casefold():
        updated = _set_phase(job, "selecting-source", {"sourceTargetId": None, "error": err_msg}, now)
        return {"status": "selecting-source", "job": updated}
    if "cas mismatch" in err_msg.casefold():
        updated = _set_phase(job, "scanning-destination", {"error": err_msg}, now)
        return {"status": "scanning-destination", "job": updated}
    if attempt >= max_attempts:
        updated = _set_phase(job, "failed-terminal", {"error": err_msg}, now)
        return {"status": "failed-terminal", "job": updated}
    return retry()


def _repair(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "schemaVersion": 2,
        "repairId": "repair_frozen_1",
        "resilienceActionId": "action-frozen-1",
        "policyId": "policy-frozen-1",
        "backupId": "backup-frozen-1",
        "sourceTargetId": "target-a",
        "destTargetId": "target-b",
        "objectSetDigest": "a" * 64,
        "repairMode": "auto",
        "trafficClass": 2,
        "phase": "queued",
        "components": {},
        "bytesRepaired": 0,
        "attempt": 0,
        "maxAttempts": 5,
        "nextAttemptAt": None,
        "holdId": None,
        "createdAt": NOW,
        "updatedAt": NOW,
        "error": None,
    }
    job.update(overrides)
    return job


def _rebalance(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "schemaVersion": 1,
        "jobId": "rebalance_frozen_1",
        "resilienceActionId": "action-frozen-1",
        "policyId": "policy-frozen-1",
        "backupId": "backup-frozen-1",
        "sourceTargetId": "target-a",
        "destTargetId": "target-c",
        "reason": "failure-domain-rebalance",
        "pruneSourceAfter": False,
        "phase": "pending",
        "bytesTransferred": 0,
        "createdAt": NOW,
        "updatedAt": NOW,
    }
    job.update(overrides)
    return job


def _cases() -> list[dict[str, Any]]:
    digest = "a" * 64
    create_fields = {
        "repairId": "repair_frozen_1",
        "resilienceActionId": "action-frozen-1",
        "policyId": "policy-frozen-1",
        "backupId": "backup-frozen-1",
        "sourceTargetId": "target-a",
        "destTargetId": "target-b",
        "objectSetDigest": digest,
    }
    rebalance_fields = {
        "jobId": "rebalance_frozen_1",
        "resilienceActionId": "action-frozen-1",
        "policyId": "policy-frozen-1",
        "backupId": "backup-frozen-1",
        "sourceTargetId": "target-a",
        "destTargetId": "target-c",
    }
    active = sorted(br.REPAIR_ACTIVE_PHASES)
    terminal = sorted(br.REPAIR_TERMINAL_PHASES)
    cases: list[dict[str, Any]] = [
        {"name": "backoff-attempt-0", "op": "backoff", "attempt": 0},
        {"name": "backoff-attempt-1", "op": "backoff", "attempt": 1},
        {"name": "backoff-attempt-2", "op": "backoff", "attempt": 2},
        {"name": "backoff-attempt-3", "op": "backoff", "attempt": 3},
        {"name": "backoff-attempt-4", "op": "backoff", "attempt": 4},
        {"name": "backoff-attempt-5", "op": "backoff", "attempt": 5},
        {"name": "backoff-attempt-6", "op": "backoff", "attempt": 6},
        {"name": "backoff-attempt-100", "op": "backoff", "attempt": 100},
        {"name": "backoff-attempt-negative", "op": "backoff", "attempt": -3},
        {
            "name": "create-repair-fresh",
            "op": "create-repair",
            "fields": create_fields,
            "existing_jobs": [],
        },
        {
            "name": "create-repair-idempotent-action",
            "op": "create-repair",
            "fields": {**create_fields, "destTargetId": "target-other"},
            "existing_jobs": [_repair(destTargetId="target-b")],
        },
        {
            "name": "create-repair-action-type-mismatch",
            "op": "create-repair",
            "fields": create_fields,
            "existing_jobs": [_repair(resilienceActionId=1)],
        },
        {
            "name": "create-repair-falsy-action-skips-scan",
            "op": "create-repair",
            "fields": {**create_fields, "resilienceActionId": ""},
            "existing_jobs": [_repair(resilienceActionId="")],
        },
        {
            "name": "create-repair-numeric-action-match",
            "op": "create-repair",
            "fields": {**create_fields, "resilienceActionId": 1},
            "existing_jobs": [_repair(resilienceActionId=1, repairId="repair_existing")],
        },
        {
            "name": "create-repair-traffic-zero",
            "op": "create-repair",
            "fields": {**create_fields, "trafficClass": 0},
            "existing_jobs": [],
        },
        {
            "name": "create-repair-traffic-unlisted",
            "op": "create-repair",
            "fields": {**create_fields, "trafficClass": 7},
            "existing_jobs": [],
        },
        {
            "name": "create-repair-traffic-true",
            "op": "create-repair",
            "fields": {**create_fields, "trafficClass": True},
            "existing_jobs": [],
        },
        {
            "name": "type-create-repair-traffic-null",
            "op": "create-repair",
            "fields": {**create_fields, "trafficClass": None},
            "existing_jobs": [],
        },
        {
            "name": "type-create-repair-traffic-object",
            "op": "create-repair",
            "fields": {**create_fields, "trafficClass": {"p": 2}},
            "existing_jobs": [],
        },
        {
            "name": "type-create-repair-traffic-array",
            "op": "create-repair",
            "fields": {**create_fields, "trafficClass": [2]},
            "existing_jobs": [],
        },
        {
            "name": "create-repair-unicode-policy",
            "op": "create-repair",
            "fields": {**create_fields, "policyId": "策略-１", "objectSetDigest": None},
            "existing_jobs": [],
        },
        {
            "name": "create-rebalance-fresh",
            "op": "create-rebalance",
            "fields": rebalance_fields,
            "existing_jobs": [],
        },
        {
            "name": "create-rebalance-action-match",
            "op": "create-rebalance",
            "fields": rebalance_fields,
            "existing_jobs": [_rebalance(phase="complete")],
        },
        {
            "name": "create-rebalance-reuse-active",
            "op": "create-rebalance",
            "fields": {**rebalance_fields, "resilienceActionId": None},
            "existing_jobs": [_rebalance(resilienceActionId=None, phase="transferring")],
        },
        {
            "name": "create-rebalance-complete-allows-new",
            "op": "create-rebalance",
            "fields": {**rebalance_fields, "resilienceActionId": None, "reason": "custom-reason"},
            "existing_jobs": [_rebalance(phase="complete", resilienceActionId=None)],
        },
        {
            "name": "create-rebalance-failed-allows-new",
            "op": "create-rebalance",
            "fields": {**rebalance_fields, "resilienceActionId": None, "pruneSourceAfter": True},
            "existing_jobs": [_rebalance(phase="failed", resilienceActionId=None)],
        },
        {
            "name": "create-rebalance-cancelled-allows-new",
            "op": "create-rebalance",
            "fields": {**rebalance_fields, "resilienceActionId": None},
            "existing_jobs": [_rebalance(phase="cancelled", resilienceActionId=None)],
        },
        {
            "name": "set-repair-phase-claim-boundary",
            "op": "set-repair-phase",
            "job": _repair(),
            "phase": "selecting-source",
            "extra": {"attempt": 1},
        },
        {
            "name": "set-repair-phase-unknown-accepted",
            "op": "set-repair-phase",
            "job": _repair(),
            "phase": "made-up-phase",
            "extra": {},
        },
        {
            "name": "set-repair-phase-unicode",
            "op": "set-repair-phase",
            "job": _repair(),
            "phase": "排队",
            "extra": {"holdId": "hold-1"},
        },
        {
            "name": "set-repair-phase-extra-overwrites-updated-at",
            "op": "set-repair-phase",
            "job": _repair(),
            "phase": "retry-wait",
            "extra": {"updatedAt": "1999-01-01T00:00:00Z", "error": "acquire-hold-failed"},
        },
        {
            "name": "set-repair-phase-nested-components",
            "op": "set-repair-phase",
            "job": _repair(),
            "phase": "transferring-components",
            "extra": {
                "components": {
                    digest: {"digest": digest, "size": 8, "state": "pending", "transferredBytes": 0},
                },
                "bytesRepaired": 0,
            },
        },
        {
            "name": "set-rebalance-phase-transferring",
            "op": "set-rebalance-phase",
            "job": _rebalance(),
            "phase": "transferring",
            "extra": {"bytesTransferred": 12},
        },
        {
            "name": "cancel-repair-missing",
            "op": "cancel-repair",
            "repair_id": "repair_missing",
            "job": None,
        },
        {
            "name": "cancel-repair-queued",
            "op": "cancel-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(),
        },
        {
            "name": "cancel-repair-queued-custom-reason",
            "op": "cancel-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(),
            "reason": "operator-abort",
        },
        {
            "name": "cancel-repair-already-cancelled",
            "op": "cancel-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(phase="cancelled", cancellationReason="old", cancelledAt=NOW),
        },
        {
            "name": "cancel-repair-not-observed",
            "op": "cancel-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(),
            "observed": None,
        },
        {
            "name": "cancel-repair-observed-wrong-phase",
            "op": "cancel-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(),
            "observed": _repair(phase="selecting-source"),
        },
        {
            "name": "cancel-repair-missing-phase",
            "op": "cancel-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(phase=None),
        },
        {
            "name": "type-cancel-repair-phase-false",
            "op": "cancel-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(phase=False),
        },
        {
            "name": "type-cancel-repair-phase-true",
            "op": "cancel-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(phase=True),
        },
        {
            "name": "type-cancel-repair-phase-zero",
            "op": "cancel-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(phase=0),
        },
        {
            "name": "cancel-repair-queued-with-spaces",
            "op": "cancel-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(phase=" queued"),
        },
        {
            "name": "cancel-rebalance-missing",
            "op": "cancel-rebalance",
            "job_id": "rebalance_missing",
            "job": None,
        },
        {
            "name": "cancel-rebalance-pending",
            "op": "cancel-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(),
        },
        {
            "name": "cancel-rebalance-already-cancelled",
            "op": "cancel-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(phase="cancelled"),
        },
        {
            "name": "cancel-rebalance-transferring",
            "op": "cancel-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(phase="transferring"),
        },
        {
            "name": "cancel-rebalance-complete",
            "op": "cancel-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(phase="complete"),
        },
        {
            "name": "cancel-rebalance-not-observed",
            "op": "cancel-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(),
            "observed": None,
        },
        {
            "name": "claim-repair-missing",
            "op": "claim-repair",
            "repair_id": "repair_missing",
            "job": None,
        },
        {
            "name": "claim-repair-queued",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(),
        },
        {
            "name": "claim-repair-resume-transferring",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(phase="transferring-components", attempt=2),
        },
        {
            "name": "claim-repair-retry-wait",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(phase="retry-wait", attempt=3, error="writer-lease-contention"),
        },
        {
            "name": "claim-repair-max-attempts-queued",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=5, maxAttempts=5),
        },
        {
            "name": "claim-repair-max-attempts-equal-after-increment",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=4, maxAttempts=5),
        },
        {
            "name": "claim-repair-max-attempts-zero-falsy-falls-back",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=0, maxAttempts=0),
        },
        {
            "name": "claim-repair-max-attempts-string-zero",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=0, maxAttempts="0"),
        },
        {
            "name": "claim-repair-attempt-string",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt="2"),
        },
        {
            "name": "claim-repair-attempt-fullwidth",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt="２"),
        },
        {
            "name": "claim-repair-attempt-arabic-indic",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt="٣"),
        },
        {
            "name": "claim-repair-attempt-true",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=True),
        },
        {
            "name": "claim-repair-attempt-false-falsy",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=False),
        },
        {
            "name": "claim-repair-attempt-float",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=1.9),
        },
        {
            "name": "claim-repair-attempt-negative",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=-2),
        },
        {
            "name": "claim-repair-huge-int",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=9007199254740992),
        },
        {
            "name": "type-claim-repair-attempt-array",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=[1]),
        },
        {
            "name": "type-claim-repair-attempt-object",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt={"n": 1}),
        },
        {
            "name": "type-claim-repair-attempt-empty-array-falsy",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=[]),
        },
        {
            "name": "type-claim-repair-attempt-bad-string",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt="1.5"),
        },
        {
            "name": "type-claim-repair-attempt-blank-string-falsy",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(attempt=""),
        },
        {
            "name": "claim-repair-phase-null-claims",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(phase=None),
        },
        {
            "name": "claim-repair-terminal-healthy-ignores-bad-attempt",
            "op": "claim-repair",
            "repair_id": "repair_frozen_1",
            "job": _repair(phase="healthy", attempt={"bad": True}),
        },
        {
            "name": "classify-pending-queued",
            "op": "classify-pending",
            "job": _repair(),
        },
        {
            "name": "classify-pending-retry-wait-due",
            "op": "classify-pending",
            "job": _repair(phase="retry-wait", attempt=1, nextAttemptAt="2026-09-05T14:59:59Z"),
        },
        {
            "name": "classify-pending-retry-wait-equal-now",
            "op": "classify-pending",
            "job": _repair(phase="retry-wait", attempt=1, nextAttemptAt=NOW),
        },
        {
            "name": "classify-pending-retry-wait-future",
            "op": "classify-pending",
            "job": _repair(phase="retry-wait", attempt=1, nextAttemptAt="2026-09-05T15:00:01Z"),
        },
        {
            "name": "classify-pending-invalid-next-attempt",
            "op": "classify-pending",
            "job": _repair(phase="retry-wait", attempt=1, nextAttemptAt="not-a-time"),
        },
        {
            "name": "classify-pending-invalid-month",
            "op": "classify-pending",
            "job": _repair(phase="retry-wait", attempt=1, nextAttemptAt="2026-13-01T00:00:00Z"),
        },
        {
            "name": "type-classify-next-attempt-true",
            "op": "classify-pending",
            "job": _repair(phase="queued", nextAttemptAt=True),
        },
        {
            "name": "classify-pending-max-queued",
            "op": "classify-pending",
            "job": _repair(phase="queued", attempt=5, maxAttempts=5),
        },
        {
            "name": "classify-pending-max-retry-wait",
            "op": "classify-pending",
            "job": _repair(phase="retry-wait", attempt=5, maxAttempts=5, nextAttemptAt="2026-09-05T14:00:00Z"),
        },
        {
            "name": "classify-pending-max-selecting-source-still-pending",
            "op": "classify-pending",
            "job": _repair(phase="selecting-source", attempt=5, maxAttempts=5),
        },
        {
            "name": "classify-pending-future-skips-bad-attempt",
            "op": "classify-pending",
            "job": _repair(phase="queued", attempt={"bad": True}, nextAttemptAt="2026-09-05T15:00:01Z"),
        },
        {
            "name": "type-classify-attempt-object",
            "op": "classify-pending",
            "job": _repair(phase="queued", attempt={"bad": True}),
        },
        {
            "name": "classify-pending-unknown-phase",
            "op": "classify-pending",
            "job": _repair(phase="made-up-phase"),
        },
        {
            "name": "route-error-source-corrupt",
            "op": "route-error",
            "job": _repair(phase="selecting-source", attempt=1),
            "error": "source component corrupt at objects/aa/bb.age",
            "kind": "generic",
        },
        {
            "name": "route-error-source-corrupt-casefold",
            "op": "route-error",
            "job": _repair(phase="transferring-components", attempt=2),
            "error": "SOURCE COMPONENT CORRUPT",
            "kind": "generic",
        },
        {
            "name": "route-error-source-corrupted-substring",
            "op": "route-error",
            "job": _repair(phase="transferring-components", attempt=2),
            "error": "the source component corrupted",
            "kind": "generic",
        },
        {
            "name": "route-error-cas-mismatch",
            "op": "route-error",
            "job": _repair(phase="transferring-components", attempt=2),
            "error": "cas mismatch on dest object",
            "kind": "generic",
        },
        {
            "name": "route-error-cas-mismatch-casefold",
            "op": "route-error",
            "job": _repair(phase="scanning-destination", attempt=1),
            "error": "CAS MISMATCH",
            "kind": "generic",
        },
        {
            "name": "route-error-corrupt-beats-cas",
            "op": "route-error",
            "job": _repair(phase="transferring-components", attempt=2),
            "error": "source component corrupt and cas mismatch",
            "kind": "generic",
        },
        {
            "name": "route-error-generic-retry",
            "op": "route-error",
            "job": _repair(phase="acquiring-source-hold", attempt=1),
            "error": "acquire-hold-failed: timeout",
            "kind": "generic",
        },
        {
            "name": "route-error-generic-max-attempts",
            "op": "route-error",
            "job": _repair(phase="acquiring-source-hold", attempt=5, maxAttempts=5),
            "error": "acquire-hold-failed: timeout",
            "kind": "generic",
        },
        {
            "name": "route-error-lease-lost",
            "op": "route-error",
            "job": _repair(phase="transferring-components", attempt=2),
            "error": "repair lease lost",
            "kind": "lease-lost",
        },
        {
            "name": "route-error-empty",
            "op": "route-error",
            "job": _repair(phase="selecting-source", attempt=1),
            "error": "",
            "kind": "generic",
        },
        {
            "name": "route-error-unicode-generic",
            "op": "route-error",
            "job": _repair(phase="selecting-source", attempt=1),
            "error": "远端校验失败",
            "kind": "generic",
        },
        {
            "name": "route-error-attempt-2-backoff-15",
            "op": "route-error",
            "job": _repair(phase="validating-source-control", attempt=2),
            "error": "writer-lease-contention: held",
            "kind": "generic",
        },
        {
            "name": "route-error-attempt-4-backoff-120",
            "op": "route-error",
            "job": _repair(phase="validating-source-control", attempt=4),
            "error": "writer-lease-contention: held",
            "kind": "generic",
        },
        {
            "name": "route-error-attempt-5-backoff-300-under-max",
            "op": "route-error",
            "job": _repair(phase="validating-source-control", attempt=5, maxAttempts=6),
            "error": "writer-lease-contention: held",
            "kind": "generic",
        },
    ]
    for phase in active:
        cases.append(
            {
                "name": f"cancel-repair-active-{phase}",
                "op": "cancel-repair",
                "repair_id": "repair_frozen_1",
                "job": _repair(phase=phase),
            }
        )
        cases.append(
            {
                "name": f"classify-pending-active-{phase}",
                "op": "classify-pending",
                "job": _repair(phase=phase, attempt=1),
            }
        )
    for phase in terminal:
        cases.append(
            {
                "name": f"cancel-repair-terminal-{phase}",
                "op": "cancel-repair",
                "repair_id": "repair_frozen_1",
                "job": _repair(phase=phase),
            }
        )
        cases.append(
            {
                "name": f"claim-repair-terminal-{phase}",
                "op": "claim-repair",
                "repair_id": "repair_frozen_1",
                "job": _repair(phase=phase, attempt=2),
            }
        )
        cases.append(
            {
                "name": f"classify-pending-terminal-{phase}",
                "op": "classify-pending",
                "job": _repair(phase=phase, attempt=2),
            }
        )
    names = [item["name"] for item in cases]
    assert len(names) == len(set(names))
    return cases


def write_corpus() -> Path:
    cases = []
    for case in _cases():
        item = copy.deepcopy(case)
        item["expected"] = replay(case)
        cases.append(item)
    payload = {
        "schema_version": 1,
        "source_version": "4.8.0",
        "source_commit": SOURCE_COMMIT,
        "scope": "validator-parity-only-not-provider-execution-evidence",
        "now": NOW,
        "ast_matches_source_commit": {name: True for name in AST_NAMES},
        "repair_terminal_phases": sorted(br.REPAIR_TERMINAL_PHASES),
        "repair_active_phases": sorted(br.REPAIR_ACTIVE_PHASES),
        "cases": cases,
    }
    path = ROOT / CORPUS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def test_v22_repair_job_phase_matches_python_4_8_0_helpers() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v22/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == SOURCE_COMMIT
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert fixture["now"] == NOW
    assert fixture["ast_matches_source_commit"] == {name: True for name in AST_NAMES}
    assert set(fixture["repair_terminal_phases"]) == set(br.REPAIR_TERMINAL_PHASES)
    assert set(fixture["repair_active_phases"]) == set(br.REPAIR_ACTIVE_PHASES)
    assert "queued" in br.REPAIR_ACTIVE_PHASES
    for case in fixture["cases"]:
        assert replay(case, fixture["now"]) == case["expected"], case["name"]


if __name__ == "__main__":
    written = write_corpus()
    print(written)
