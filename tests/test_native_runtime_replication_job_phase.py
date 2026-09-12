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
CORPUS_REL = "compat/native-runtime/v23/transfer/replication_job_phase_vector.json"
AST_NAMES = (
    "JOB_SCHEMA_VERSION",
    "TERMINAL_PHASES",
    "ACTIVE_PHASES",
    "MODES",
    "enqueue_replica_jobs",
    "_set_phase",
    "execute_replication_job",
    "_fail_job",
    "process_pending_jobs",
    "has_open_required_jobs",
)
RECEIPT_SNAPSHOT_KEYS = (
    "snapshotKind",
    "parentBackupId",
    "baseBackupId",
    "lineageId",
    "chainDepth",
    "chunkProtocol",
    "logicalBytes",
    "size",
    "storageProtocol",
    "creationVerified",
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


def test_replication_job_helpers_ast_match_frozen_4_8_0() -> None:
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


def replay(case: dict[str, Any], now: str = NOW) -> dict[str, Any]:
    op = case["op"]
    if op == "backoff":
        return {"seconds": min(300, 2 ** min(int(case["attempts"]), 8))}
    if op == "set-phase":
        return {"job": _set_phase(copy.deepcopy(case["job"]), case["phase"], copy.deepcopy(case.get("extra") or {}), now)}
    if op == "enqueue":
        return _enqueue(case, now)
    if op == "claim":
        return _claim(case, now)
    if op == "fail":
        return _fail(case, now)
    if op == "classify-pending":
        return _classify(case, now)
    if op == "has-open-required":
        return _has_open(case)
    raise AssertionError(op)


def _enqueue(case: dict[str, Any], now: str) -> dict[str, Any]:
    policy = copy.deepcopy(case["policy"])
    primary_target_id = str(case["primary_target_id"])
    replication = policy.get("replication") if isinstance(policy.get("replication"), dict) else {}
    if not replication or not replication.get("enabled"):
        return {"jobs": []}
    targets = list(replication.get("targets") or [])
    configured_primary_id = str(policy.get("primaryTargetId") or policy.get("targetId") or "").strip()
    configured_ids = {str(entry.get("targetId") or "") for entry in targets if isinstance(entry, dict)}
    if configured_primary_id and configured_primary_id != primary_target_id and configured_primary_id not in configured_ids:
        targets.append({"targetId": configured_primary_id, "mode": "required", "role": "failback-catchup"})
    if not targets:
        return {"jobs": []}
    policy_id = str(policy.get("policyId") or "")
    object_set_digest = ""
    control_digest = ""
    objects: list[Any] = []
    primary_receipt = case.get("primary_receipt")
    if primary_receipt:
        object_set_digest = str(primary_receipt.get("objectSetDigest") or "")
        control_digest = str(primary_receipt.get("controlObjectDigest") or primary_receipt.get("objectDigest") or "")
        objects = list(primary_receipt.get("objects") or []) if isinstance(primary_receipt.get("objects"), list) else []
    snapshot = (
        {key: primary_receipt.get(key) for key in RECEIPT_SNAPSHOT_KEYS if isinstance(primary_receipt, dict) and key in primary_receipt}
        if isinstance(primary_receipt, dict)
        else {}
    )
    existing_jobs = list(case.get("existing_jobs") or [])
    job_ids = list(case.get("job_ids") or [])
    created: list[dict[str, Any]] = []
    id_index = 0
    for entry in targets:
        if not isinstance(entry, dict):
            continue
        replica_id = str(entry.get("targetId") or "").strip()
        mode = str(entry.get("mode") or "required")
        if not replica_id or replica_id == primary_target_id:
            continue
        existing = [
            job
            for job in existing_jobs
            if str(job.get("replicaTargetId") or "") == replica_id and str(job.get("phase") or "") not in br.TERMINAL_PHASES
        ]
        if existing:
            created.append(copy.deepcopy(existing[0]))
            continue
        job_id = job_ids[id_index]
        id_index += 1
        created.append(
            {
                "schemaVersion": br.JOB_SCHEMA_VERSION,
                "jobId": job_id,
                "policyId": policy_id,
                "backupId": case.get("backup_id"),
                "primaryTargetId": primary_target_id,
                "replicaTargetId": replica_id,
                "mode": mode if mode in br.MODES else "required",
                "phase": "queued",
                "runId": case.get("run_id"),
                "scheduleSlot": case.get("schedule_slot"),
                "slotDigest": case.get("slot_digest"),
                "objectSetDigest": object_set_digest,
                "controlObjectDigest": control_digest,
                "objects": copy.deepcopy(objects),
                "primaryReceiptSnapshot": copy.deepcopy(snapshot),
                "createdAt": now,
                "updatedAt": now,
                "error": None,
                "attempts": 0,
                "maxAttempts": 5,
            }
        )
    return {"jobs": created}


def _claim(case: dict[str, Any], now: str) -> dict[str, Any]:
    job_id = case["job_id"]
    job = case.get("job")
    if job is None:
        return {"status": "error", "error": "not-found", "jobId": job_id}
    job = copy.deepcopy(job)
    if str(job.get("phase") or "") in br.TERMINAL_PHASES:
        return {"status": "terminal", "job": job}
    try:
        attempts = int(job.get("attempts") or 0) + 1
    except (TypeError, ValueError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__, "jobId": job_id, "job": job}
    job = _set_phase(job, "checking-target", {"attempts": attempts}, now)
    return {"status": "claimed", "job": job}


def _fail(case: dict[str, Any], now: str) -> dict[str, Any]:
    job = copy.deepcopy(case["job"])
    message = str(case.get("error") or "")[:500]
    mode = case["mode"] if "mode" in case else str(job.get("mode") or "required")
    try:
        attempts = int(job.get("attempts") or 1)
        max_attempts = int(job.get("maxAttempts") or 5)
    except (TypeError, ValueError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__, "job": job}
    current = br._parse_iso(now)
    assert current is not None
    if mode == "required":
        if "spool" in message.casefold() and "missing" in message.casefold():
            job = _set_phase(job, "repair-needed", {"error": message, "attempts": attempts}, now)
            return {"status": "repair-needed", "job": job}
        if attempts < max_attempts:
            backoff_sec = min(300, 2 ** min(attempts, 8))
            next_retry = br._utc_iso(current + timedelta(seconds=backoff_sec))
            job = _set_phase(
                job,
                "retry-wait",
                {"error": message, "attempts": attempts, "nextRetryAt": next_retry},
                now,
            )
            return {"status": "retry-wait", "job": job}
        job = _set_phase(job, "failed-terminal", {"error": message, "attempts": attempts}, now)
        return {"status": "failed-terminal", "job": job}
    job = _set_phase(job, "failed", {"error": message, "attempts": attempts}, now)
    return {"status": "failed", "job": job}


def _classify(case: dict[str, Any], now: str) -> dict[str, Any]:
    job = copy.deepcopy(case["job"])
    phase = str(job.get("phase") or "")
    if phase not in br.ACTIVE_PHASES and phase != "queued":
        return {"decision": "skip-inactive", "job": job}
    if phase == "retry-wait":
        next_retry = job.get("nextRetryAt")
        parsed = br._parse_iso(next_retry)
        current = br._parse_iso(now)
        assert current is not None
        if next_retry and parsed and parsed > current:
            return {"decision": "skip-backoff", "job": job}
    return {"decision": "pending", "job": job}


def _has_open(case: dict[str, Any]) -> dict[str, Any]:
    policy_id = case["policy_id"]
    backup_id = case.get("backup_id")
    slot_digest = case.get("slot_digest")
    for job in case.get("jobs") or []:
        if not isinstance(job, dict) or not str(job.get("jobId", "")):
            continue
        if str(job.get("policyId") or "") != policy_id:
            continue
        if backup_id and str(job.get("backupId") or "") != backup_id:
            continue
        if str(job.get("mode") or "") != "required":
            continue
        if str(job.get("phase") or "") in br.TERMINAL_PHASES:
            continue
        if slot_digest and str(job.get("slotDigest") or "") not in {"", slot_digest}:
            continue
        return {"open": True}
    return {"open": False}


def _job(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "schemaVersion": 2,
        "jobId": "repl_frozen_1",
        "policyId": "policy-frozen-1",
        "backupId": "backup-frozen-1",
        "primaryTargetId": "target-a",
        "replicaTargetId": "target-b",
        "mode": "required",
        "phase": "queued",
        "runId": "run-frozen-1",
        "scheduleSlot": "replica/backup-frozen-1",
        "slotDigest": "b" * 64,
        "objectSetDigest": "c" * 64,
        "controlObjectDigest": "d" * 64,
        "objects": [],
        "primaryReceiptSnapshot": {},
        "createdAt": NOW,
        "updatedAt": NOW,
        "error": None,
        "attempts": 0,
        "maxAttempts": 5,
    }
    job.update(overrides)
    return job


def _policy(*, enabled: Any = True, targets: Any = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "policyId": "policy-frozen-1",
        "primaryTargetId": "target-a",
        "replication": {
            "enabled": enabled,
            "targets": targets if targets is not None else [{"targetId": "target-b", "mode": "required"}],
        },
    }
    body.update(extra)
    return body


def _cases() -> list[dict[str, Any]]:
    digest = "c" * 64
    cases: list[dict[str, Any]] = [
        {"name": "backoff-attempts-0", "op": "backoff", "attempts": 0},
        {"name": "backoff-attempts-1", "op": "backoff", "attempts": 1},
        {"name": "backoff-attempts-2", "op": "backoff", "attempts": 2},
        {"name": "backoff-attempts-3", "op": "backoff", "attempts": 3},
        {"name": "backoff-attempts-4", "op": "backoff", "attempts": 4},
        {"name": "backoff-attempts-5", "op": "backoff", "attempts": 5},
        {"name": "backoff-attempts-8", "op": "backoff", "attempts": 8},
        {"name": "backoff-attempts-9", "op": "backoff", "attempts": 9},
        {"name": "backoff-attempts-negative", "op": "backoff", "attempts": -3},
        {
            "name": "enqueue-fresh-required",
            "op": "enqueue",
            "policy": _policy(),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-1",
            "schedule_slot": "replica/backup-frozen-1",
            "slot_digest": "b" * 64,
            "primary_receipt": {
                "objectSetDigest": digest,
                "controlObjectDigest": "d" * 64,
                "objects": [{"digest": digest, "size": 8}],
                "snapshotKind": "full",
                "size": 8,
                "ignored": "no",
            },
            "existing_jobs": [],
            "job_ids": ["repl_frozen_1"],
        },
        {
            "name": "enqueue-disabled",
            "op": "enqueue",
            "policy": _policy(enabled=False),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-1",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "existing_jobs": [],
            "job_ids": ["repl_frozen_1"],
        },
        {
            "name": "enqueue-missing-replication",
            "op": "enqueue",
            "policy": {"policyId": "policy-frozen-1"},
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-1",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "existing_jobs": [],
            "job_ids": ["repl_frozen_1"],
        },
        {
            "name": "enqueue-skip-self-primary",
            "op": "enqueue",
            "policy": _policy(targets=[{"targetId": "target-a", "mode": "required"}]),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-1",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "existing_jobs": [],
            "job_ids": ["repl_frozen_1"],
        },
        {
            "name": "enqueue-idempotent-open-job",
            "op": "enqueue",
            "policy": _policy(),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-new",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "existing_jobs": [_job(phase="retry-wait")],
            "job_ids": ["repl_unused"],
        },
        {
            "name": "enqueue-terminal-allows-new",
            "op": "enqueue",
            "policy": _policy(),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-2",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "existing_jobs": [_job(phase="committed")],
            "job_ids": ["repl_frozen_2"],
        },
        {
            "name": "enqueue-unknown-mode-becomes-required",
            "op": "enqueue",
            "policy": _policy(targets=[{"targetId": "target-b", "mode": "BEST-EFFORT"}]),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-1",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "existing_jobs": [],
            "job_ids": ["repl_frozen_1"],
        },
        {
            "name": "enqueue-best-effort",
            "op": "enqueue",
            "policy": _policy(targets=[{"targetId": "target-b", "mode": "best-effort"}]),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-1",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "existing_jobs": [],
            "job_ids": ["repl_frozen_1"],
        },
        {
            "name": "enqueue-failback-catchup",
            "op": "enqueue",
            "policy": _policy(targets=[], primaryTargetId="target-old"),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-1",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "existing_jobs": [],
            "job_ids": ["repl_frozen_1"],
        },
        {
            "name": "enqueue-empty-receipt",
            "op": "enqueue",
            "policy": _policy(),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-1",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "primary_receipt": {},
            "existing_jobs": [],
            "job_ids": ["repl_frozen_1"],
        },
        {
            "name": "enqueue-control-digest-fallback",
            "op": "enqueue",
            "policy": _policy(),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-1",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "primary_receipt": {"objectDigest": "e" * 64, "objects": "not-list"},
            "existing_jobs": [],
            "job_ids": ["repl_frozen_1"],
        },
        {
            "name": "enqueue-skip-non-dict-target",
            "op": "enqueue",
            "policy": _policy(targets=["target-b", {"targetId": "target-c"}]),
            "primary_target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "run_id": "run-frozen-1",
            "schedule_slot": "slot",
            "slot_digest": "b" * 64,
            "existing_jobs": [],
            "job_ids": ["repl_frozen_1"],
        },
        {
            "name": "set-phase-unguarded-unknown",
            "op": "set-phase",
            "job": _job(),
            "phase": "made-up-phase",
            "extra": {"attempts": 2},
        },
        {"name": "claim-missing", "op": "claim", "job_id": "repl_missing", "job": None},
        {"name": "claim-queued", "op": "claim", "job_id": "repl_frozen_1", "job": _job()},
        {
            "name": "claim-resume-transferring",
            "op": "claim",
            "job_id": "repl_frozen_1",
            "job": _job(phase="transferring-components", attempts=2),
        },
        {
            "name": "claim-repair-needed-reclaims",
            "op": "claim",
            "job_id": "repl_frozen_1",
            "job": _job(phase="repair-needed", attempts=1),
        },
        {
            "name": "claim-attempts-zero-falsy",
            "op": "claim",
            "job_id": "repl_frozen_1",
            "job": _job(attempts=0),
        },
        {
            "name": "claim-attempts-fullwidth",
            "op": "claim",
            "job_id": "repl_frozen_1",
            "job": _job(attempts="２"),
        },
        {
            "name": "type-claim-attempts-object",
            "op": "claim",
            "job_id": "repl_frozen_1",
            "job": _job(attempts={"n": 1}),
        },
        {
            "name": "fail-required-retry",
            "op": "fail",
            "job": _job(phase="checking-target", attempts=1),
            "error": "target resolve failed",
            "mode": "required",
        },
        {
            "name": "fail-required-max",
            "op": "fail",
            "job": _job(phase="checking-target", attempts=5),
            "error": "target resolve failed",
            "mode": "required",
        },
        {
            "name": "fail-required-spool-missing",
            "op": "fail",
            "job": _job(phase="checking-target", attempts=1),
            "error": "replication spool package missing; cannot re-encrypt",
            "mode": "required",
        },
        {
            "name": "fail-required-spool-missing-casefold",
            "op": "fail",
            "job": _job(phase="checking-target", attempts=8),
            "error": "SPOOL FILE MISSING",
            "mode": "required",
        },
        {
            "name": "fail-required-spool-without-missing",
            "op": "fail",
            "job": _job(phase="checking-target", attempts=1),
            "error": "spool locked",
            "mode": "required",
        },
        {
            "name": "fail-best-effort-always-failed",
            "op": "fail",
            "job": _job(phase="checking-target", attempts=1, mode="best-effort"),
            "error": "replication spool package missing",
            "mode": "best-effort",
        },
        {
            "name": "fail-attempts-zero-falsy-becomes-one",
            "op": "fail",
            "job": _job(phase="checking-target", attempts=0),
            "error": "boom",
            "mode": "required",
        },
        {
            "name": "fail-message-truncated",
            "op": "fail",
            "job": _job(phase="checking-target", attempts=5),
            "error": "x" * 501,
            "mode": "required",
        },
        {
            "name": "fail-backoff-attempts-8",
            "op": "fail",
            "job": _job(phase="checking-target", attempts=8, maxAttempts=9),
            "error": "writer busy",
            "mode": "required",
        },
        {
            "name": "type-fail-attempts-array",
            "op": "fail",
            "job": _job(attempts=[1]),
            "error": "boom",
            "mode": "required",
        },
        {
            "name": "classify-queued",
            "op": "classify-pending",
            "job": _job(),
        },
        {
            "name": "classify-retry-wait-future",
            "op": "classify-pending",
            "job": _job(phase="retry-wait", nextRetryAt="2026-09-05T15:00:01Z"),
        },
        {
            "name": "classify-retry-wait-equal-now",
            "op": "classify-pending",
            "job": _job(phase="retry-wait", nextRetryAt=NOW),
        },
        {
            "name": "classify-retry-wait-past",
            "op": "classify-pending",
            "job": _job(phase="retry-wait", nextRetryAt="2026-09-05T14:59:59Z"),
        },
        {
            "name": "classify-retry-wait-invalid",
            "op": "classify-pending",
            "job": _job(phase="retry-wait", nextRetryAt="not-a-time"),
        },
        {
            "name": "classify-repair-needed-pending",
            "op": "classify-pending",
            "job": _job(phase="repair-needed"),
        },
        {
            "name": "has-open-required-true",
            "op": "has-open-required",
            "policy_id": "policy-frozen-1",
            "backup_id": "backup-frozen-1",
            "jobs": [_job()],
        },
        {
            "name": "has-open-required-terminal-false",
            "op": "has-open-required",
            "policy_id": "policy-frozen-1",
            "jobs": [_job(phase="committed")],
        },
        {
            "name": "has-open-best-effort-false",
            "op": "has-open-required",
            "policy_id": "policy-frozen-1",
            "jobs": [_job(mode="best-effort")],
        },
        {
            "name": "has-open-empty-slot-matches",
            "op": "has-open-required",
            "policy_id": "policy-frozen-1",
            "slot_digest": "b" * 64,
            "jobs": [_job(slotDigest="")],
        },
        {
            "name": "has-open-slot-mismatch",
            "op": "has-open-required",
            "policy_id": "policy-frozen-1",
            "slot_digest": "b" * 64,
            "jobs": [_job(slotDigest="f" * 64)],
        },
        {
            "name": "has-open-missing-job-id-skipped",
            "op": "has-open-required",
            "policy_id": "policy-frozen-1",
            "jobs": [_job(jobId="")],
        },
    ]
    for phase in sorted(br.ACTIVE_PHASES):
        cases.append({"name": f"classify-active-{phase}", "op": "classify-pending", "job": _job(phase=phase, attempts=1)})
    for phase in sorted(br.TERMINAL_PHASES):
        cases.append({"name": f"claim-terminal-{phase}", "op": "claim", "job_id": "repl_frozen_1", "job": _job(phase=phase, attempts=2)})
        cases.append({"name": f"classify-terminal-{phase}", "op": "classify-pending", "job": _job(phase=phase)})
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
        "terminal_phases": sorted(br.TERMINAL_PHASES),
        "active_phases": sorted(br.ACTIVE_PHASES),
        "modes": sorted(br.MODES),
        "cases": cases,
    }
    path = ROOT / CORPUS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def test_v23_replication_job_phase_matches_python_4_8_0_helpers() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v23/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == SOURCE_COMMIT
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert fixture["now"] == NOW
    assert fixture["ast_matches_source_commit"] == {name: True for name in AST_NAMES}
    assert set(fixture["terminal_phases"]) == set(br.TERMINAL_PHASES)
    assert set(fixture["active_phases"]) == set(br.ACTIVE_PHASES)
    assert set(fixture["modes"]) == set(br.MODES)
    for case in fixture["cases"]:
        assert replay(case, fixture["now"]) == case["expected"], case["name"]


if __name__ == "__main__":
    written = write_corpus()
    print(written)
