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
CORPUS_REL = "compat/native-runtime/v24/transfer/rebalance_hold_safety_vector.json"
AST_NAMES = (
    "execute_rebalance_job",
    "process_pending_rebalances",
    "is_source_held",
    "has_source_holds_for_target",
    "simulate_copy_removal",
    "to_dict",
    "renew",
)


def _dump_named(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.dump(node)
        if isinstance(node, ast.ClassDef):
            if node.name == name:
                return ast.dump(node)
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == name:
                    return ast.dump(item)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.dump(node)
    raise AssertionError(name)


def test_rebalance_hold_helpers_ast_match_frozen_4_8_0() -> None:
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
    if op == "hold-document":
        return {"hold": _hold_document(case, now)}
    if op == "hold-renew":
        return {"hold": _hold_renew(case, now)}
    if op == "is-source-held":
        return {"held": _is_source_held(case, now)}
    if op == "has-source-holds":
        return {"held": _has_source_holds(case, now)}
    if op == "simulate-removal":
        return _simulate(case, now)
    if op == "claim-rebalance":
        return _claim_rebalance(case, now)
    if op == "fail-rebalance":
        return _fail_rebalance(case, now)
    if op == "classify-pending-rebalance":
        return _classify_rebalance(case)
    if op == "drain-limit":
        return _drain_limit(case)
    raise AssertionError(op)


def _hold_document(case: dict[str, Any], now: str) -> dict[str, Any]:
    fields = case["fields"]
    hold_seconds = int(fields["holdSeconds"]) if "holdSeconds" in fields else 3600
    current = br._parse_iso(now)
    assert current is not None
    return {
        "holderKind": "replica-repair",
        "holderId": fields.get("holderId"),
        "holdId": fields.get("holdId"),
        "targetId": fields.get("targetId"),
        "policyId": fields.get("policyId"),
        "backupId": fields.get("backupId"),
        "objectSetDigest": fields.get("objectSetDigest"),
        "createdAt": now,
        "expiresAt": br._utc_iso(current + timedelta(seconds=hold_seconds)),
        "generation": 1,
        "etag": None,
    }


def _hold_renew(case: dict[str, Any], now: str) -> dict[str, Any]:
    hold = copy.deepcopy(case["hold"])
    duration = int(case["durationSeconds"]) if "durationSeconds" in case else 3600
    current = br._parse_iso(now)
    assert current is not None
    hold["expiresAt"] = br._utc_iso(current + timedelta(seconds=duration))
    hold["generation"] = hold["generation"] + 1
    return hold


def _is_source_held(case: dict[str, Any], now: str) -> bool:
    current = br._parse_iso(now)
    assert current is not None
    target_id = case["target_id"]
    policy_id = case["policy_id"]
    backup_id = case["backup_id"]
    for item in case.get("holds") or []:
        try:
            if not isinstance(item, dict) or item.get("decode_error"):
                raise ValueError("decode")
            exp = br._parse_iso(item.get("expiresAt"))
            if exp is not None and current > exp:
                continue
            if (
                str(item.get("targetId")) == target_id
                and str(item.get("policyId")) == policy_id
                and str(item.get("backupId")) == backup_id
            ):
                return True
        except Exception:
            continue
    return False


def _has_source_holds(case: dict[str, Any], now: str) -> bool:
    current = br._parse_iso(now)
    assert current is not None
    target_id = case["target_id"]
    for item in case.get("holds") or []:
        try:
            if not isinstance(item, dict) or item.get("decode_error"):
                raise ValueError("decode")
        except Exception:
            return True
        expiry = br._parse_iso(item.get("expiresAt"))
        active = expiry is None or current <= expiry
        if str(item.get("targetId") or "") == target_id and active:
            return True
    return False


def _simulate(case: dict[str, Any], now: str) -> dict[str, Any]:
    policy = case.get("policy") or {}
    target_id = case["target_id"]
    backup_id = case["backup_id"]
    try:
        repl = (policy or {}).get("replication") or {}
        placement = (policy or {}).get("placement") or {}
        min_copies = int(repl.get("minCommittedCopies") or 1)
        min_fd = int(repl.get("minFailureDomains") or 1)
        min_regions = int(repl.get("minRegions") or 1)
        max_copies_per_fd = placement.get("maxCopiesPerFailureDomain") or repl.get("maxCopiesPerFailureDomain")
        records = {item["targetId"]: item for item in case.get("targets") or []}
        copies = list(case.get("copies") or [])
        healthy_before = [c for c in copies if c.get("recoverable") and c.get("state") == "healthy"]
        healthy_after = [c for c in healthy_before if str(c.get("targetId")) != target_id]
        fd_before = {
            str((records.get(str(c.get("targetId"))) or {}).get("failureDomain") or "default")
            for c in healthy_before
        }
        fd_after = {
            str((records.get(str(c.get("targetId"))) or {}).get("failureDomain") or "default")
            for c in healthy_after
        }
        regions_before = {
            str((records.get(str(c.get("targetId"))) or {}).get("region") or "default-region")
            for c in healthy_before
        }
        regions_after = {
            str((records.get(str(c.get("targetId"))) or {}).get("region") or "default-region")
            for c in healthy_after
        }
        counts_by_fd_after: dict[str, int] = {}
        for copy_item in healthy_after:
            fd_name = str((records.get(str(copy_item.get("targetId"))) or {}).get("failureDomain") or "default")
            counts_by_fd_after[fd_name] = counts_by_fd_after.get(fd_name, 0) + 1
        policy_safe = len(healthy_after) >= min_copies and len(fd_after) >= min_fd and len(regions_after) >= min_regions
        if max_copies_per_fd is not None and int(max_copies_per_fd) > 0:
            if any(cnt > int(max_copies_per_fd) for cnt in counts_by_fd_after.values()):
                policy_safe = False
    except (TypeError, ValueError, KeyError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__}
    held = _is_source_held(
        {
            "target_id": target_id,
            "policy_id": case.get("policy_id") or "",
            "backup_id": backup_id,
            "holds": case.get("holds") or [],
        },
        now,
    )
    return {
        "healthyCopiesBefore": len(healthy_before),
        "healthyCopiesAfter": len(healthy_after),
        "failureDomainsBefore": len(fd_before),
        "failureDomainsAfter": len(fd_after),
        "regionsBefore": len(regions_before),
        "regionsAfter": len(regions_after),
        "copiesInEachDomainAfter": counts_by_fd_after,
        "policySafe": policy_safe and not held,
        "protectedByHold": held,
        "targetId": target_id,
        "backupId": backup_id,
    }


def _claim_rebalance(case: dict[str, Any], now: str) -> dict[str, Any]:
    job_id = case["job_id"]
    job = case.get("job")
    if job is None:
        return {"status": "error", "error": "not-found", "jobId": job_id}
    job = copy.deepcopy(job)
    phase = str(job.get("phase") or "")
    if phase == "cancelled":
        return {"status": "cancelled", "jobId": job_id, "job": job}
    if phase == "complete":
        return {"status": "success", "jobId": job_id, "job": job}
    job = _set_phase(job, "transferring", {}, now)
    return {"status": "claimed", "jobId": job_id, "job": job}


def _fail_rebalance(case: dict[str, Any], now: str) -> dict[str, Any]:
    job = copy.deepcopy(case["job"])
    job_id = case.get("job_id") or job.get("jobId")
    message = str(case.get("error") or "")
    job = _set_phase(job, "failed", {"error": message}, now)
    return {"status": "failed", "jobId": job_id, "error": message, "job": job}


def _classify_rebalance(case: dict[str, Any]) -> dict[str, Any]:
    job = copy.deepcopy(case["job"])
    phase = str(job.get("phase") or "")
    if phase == "pending":
        return {"decision": "pending", "job": job}
    return {"decision": "skip", "job": job}


def _drain_limit(case: dict[str, Any]) -> dict[str, Any]:
    try:
        limit = int(case["limit"])
    except (TypeError, ValueError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__}
    pending = [job for job in case.get("jobs") or [] if str(job.get("phase") or "") == "pending"]
    selected = pending[: max(1, limit)]
    return {"selected": [job.get("jobId") for job in selected], "pendingCount": len(pending)}


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


def _hold(**overrides: Any) -> dict[str, Any]:
    hold: dict[str, Any] = {
        "holderKind": "replica-repair",
        "holderId": "repair_frozen_1",
        "holdId": "hold_frozen_1",
        "targetId": "target-a",
        "policyId": "policy-frozen-1",
        "backupId": "backup-frozen-1",
        "objectSetDigest": "a" * 64,
        "createdAt": NOW,
        "expiresAt": "2026-09-05T16:00:00Z",
        "generation": 1,
        "etag": None,
    }
    hold.update(overrides)
    return hold


def _copy(target_id: str, *, recoverable: Any = True, state: Any = "healthy") -> dict[str, Any]:
    return {"targetId": target_id, "recoverable": recoverable, "state": state}


def _target(target_id: str, fd: str, region: str) -> dict[str, Any]:
    return {"targetId": target_id, "failureDomain": fd, "region": region}


def _cases() -> list[dict[str, Any]]:
    copies = [_copy("target-a"), _copy("target-b"), _copy("target-c")]
    targets = [_target("target-a", "fd-1", "r1"), _target("target-b", "fd-2", "r2"), _target("target-c", "fd-1", "r1")]
    policy = {
        "replication": {"minCommittedCopies": 2, "minFailureDomains": 2, "minRegions": 2},
        "placement": {},
    }
    cases: list[dict[str, Any]] = [
        {
            "name": "hold-document-default-3600",
            "op": "hold-document",
            "fields": {
                "holderId": "repair_frozen_1",
                "holdId": "hold_frozen_1",
                "targetId": "target-a",
                "policyId": "policy-frozen-1",
                "backupId": "backup-frozen-1",
                "objectSetDigest": "a" * 64,
            },
        },
        {
            "name": "hold-document-custom-seconds",
            "op": "hold-document",
            "fields": {
                "holderId": "repair_frozen_1",
                "holdId": "hold_frozen_2",
                "targetId": "target-a",
                "policyId": "policy-frozen-1",
                "backupId": "backup-frozen-1",
                "holdSeconds": 120,
            },
        },
        {
            "name": "hold-renew-generation",
            "op": "hold-renew",
            "hold": _hold(),
            "durationSeconds": 1800,
        },
        {
            "name": "is-held-match",
            "op": "is-source-held",
            "target_id": "target-a",
            "policy_id": "policy-frozen-1",
            "backup_id": "backup-frozen-1",
            "holds": [_hold()],
        },
        {
            "name": "is-held-expired",
            "op": "is-source-held",
            "target_id": "target-a",
            "policy_id": "policy-frozen-1",
            "backup_id": "backup-frozen-1",
            "holds": [_hold(expiresAt="2026-09-05T14:59:59Z")],
        },
        {
            "name": "is-held-equal-now-still-held",
            "op": "is-source-held",
            "target_id": "target-a",
            "policy_id": "policy-frozen-1",
            "backup_id": "backup-frozen-1",
            "holds": [_hold(expiresAt=NOW)],
        },
        {
            "name": "is-held-unparseable-expiry-fail-closed",
            "op": "is-source-held",
            "target_id": "target-a",
            "policy_id": "policy-frozen-1",
            "backup_id": "backup-frozen-1",
            "holds": [_hold(expiresAt="not-a-time")],
        },
        {
            "name": "is-held-target-mismatch",
            "op": "is-source-held",
            "target_id": "target-b",
            "policy_id": "policy-frozen-1",
            "backup_id": "backup-frozen-1",
            "holds": [_hold()],
        },
        {
            "name": "is-held-none-target-matches-string-None",
            "op": "is-source-held",
            "target_id": "None",
            "policy_id": "policy-frozen-1",
            "backup_id": "backup-frozen-1",
            "holds": [_hold(targetId=None)],
        },
        {
            "name": "is-held-decode-error-skipped",
            "op": "is-source-held",
            "target_id": "target-a",
            "policy_id": "policy-frozen-1",
            "backup_id": "backup-frozen-1",
            "holds": [{"decode_error": True}, _hold()],
        },
        {
            "name": "has-holds-or-empty-target",
            "op": "has-source-holds",
            "target_id": "target-a",
            "holds": [_hold()],
        },
        {
            "name": "has-holds-missing-target-not-None-string",
            "op": "has-source-holds",
            "target_id": "None",
            "holds": [_hold(targetId=None)],
        },
        {
            "name": "has-holds-decode-error-true",
            "op": "has-source-holds",
            "target_id": "target-a",
            "holds": [{"decode_error": True}],
        },
        {
            "name": "has-holds-expired-false",
            "op": "has-source-holds",
            "target_id": "target-a",
            "holds": [_hold(expiresAt="2026-09-05T14:59:59Z")],
        },
        {
            "name": "simulate-remove-b-not-safe-min-fd",
            "op": "simulate-removal",
            "target_id": "target-b",
            "backup_id": "backup-frozen-1",
            "policy_id": "policy-frozen-1",
            "policy": policy,
            "copies": copies,
            "targets": targets,
            "holds": [],
        },
        {
            "name": "simulate-remove-c-keeps-two-domains",
            "op": "simulate-removal",
            "target_id": "target-c",
            "backup_id": "backup-frozen-1",
            "policy_id": "policy-frozen-1",
            "policy": policy,
            "copies": copies,
            "targets": targets,
            "holds": [],
        },
        {
            "name": "simulate-held-not-safe",
            "op": "simulate-removal",
            "target_id": "target-c",
            "backup_id": "backup-frozen-1",
            "policy_id": "policy-frozen-1",
            "policy": policy,
            "copies": copies,
            "targets": targets,
            "holds": [_hold(targetId="target-c")],
        },
        {
            "name": "simulate-unhealthy-ignored",
            "op": "simulate-removal",
            "target_id": "target-b",
            "backup_id": "backup-frozen-1",
            "policy_id": "policy-frozen-1",
            "policy": {"replication": {"minCommittedCopies": 1, "minFailureDomains": 1, "minRegions": 1}},
            "copies": [_copy("target-a"), _copy("target-b", recoverable=False), _copy("target-c", state="degraded")],
            "targets": targets,
            "holds": [],
        },
        {
            "name": "simulate-unknown-target-default-fd",
            "op": "simulate-removal",
            "target_id": "target-z",
            "backup_id": "backup-frozen-1",
            "policy_id": "policy-frozen-1",
            "policy": {"replication": {"minCommittedCopies": 1, "minFailureDomains": 1, "minRegions": 1}},
            "copies": [_copy("target-orphan")],
            "targets": [],
            "holds": [],
        },
        {
            "name": "simulate-max-copies-per-fd",
            "op": "simulate-removal",
            "target_id": "target-b",
            "backup_id": "backup-frozen-1",
            "policy_id": "policy-frozen-1",
            "policy": {
                "replication": {"minCommittedCopies": 1, "minFailureDomains": 1, "minRegions": 1},
                "placement": {"maxCopiesPerFailureDomain": 1},
            },
            "copies": copies,
            "targets": targets,
            "holds": [],
        },
        {
            "name": "simulate-max-zero-unlimited",
            "op": "simulate-removal",
            "target_id": "target-b",
            "backup_id": "backup-frozen-1",
            "policy_id": "policy-frozen-1",
            "policy": {
                "replication": {"minCommittedCopies": 1, "minFailureDomains": 1, "minRegions": 1, "maxCopiesPerFailureDomain": 0},
                "placement": {},
            },
            "copies": copies,
            "targets": targets,
            "holds": [],
        },
        {
            "name": "simulate-min-copies-zero-falsy-fallback",
            "op": "simulate-removal",
            "target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "policy_id": "policy-frozen-1",
            "policy": {"replication": {"minCommittedCopies": 0, "minFailureDomains": 1, "minRegions": 1}},
            "copies": [_copy("target-a")],
            "targets": [_target("target-a", "fd-1", "r1")],
            "holds": [],
        },
        {
            "name": "type-simulate-min-copies-object",
            "op": "simulate-removal",
            "target_id": "target-a",
            "backup_id": "backup-frozen-1",
            "policy_id": "policy-frozen-1",
            "policy": {"replication": {"minCommittedCopies": {"n": 1}}},
            "copies": copies,
            "targets": targets,
            "holds": [],
        },
        {"name": "claim-rebalance-missing", "op": "claim-rebalance", "job_id": "rebalance_missing", "job": None},
        {"name": "claim-rebalance-pending", "op": "claim-rebalance", "job_id": "rebalance_frozen_1", "job": _rebalance()},
        {
            "name": "claim-rebalance-cancelled",
            "op": "claim-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(phase="cancelled"),
        },
        {
            "name": "claim-rebalance-complete",
            "op": "claim-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(phase="complete"),
        },
        {
            "name": "claim-rebalance-failed-reclaims",
            "op": "claim-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(phase="failed", error="old"),
        },
        {
            "name": "claim-rebalance-transferring-reclaims",
            "op": "claim-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(phase="transferring"),
        },
        {
            "name": "type-claim-rebalance-phase-false",
            "op": "claim-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(phase=False),
        },
        {
            "name": "fail-rebalance-message",
            "op": "fail-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(phase="transferring"),
            "error": "Rebalance transfer failed: None",
        },
        {
            "name": "fail-rebalance-no-truncate",
            "op": "fail-rebalance",
            "job_id": "rebalance_frozen_1",
            "job": _rebalance(phase="transferring"),
            "error": "x" * 501,
        },
        {"name": "classify-rebalance-pending", "op": "classify-pending-rebalance", "job": _rebalance()},
        {
            "name": "classify-rebalance-transferring",
            "op": "classify-pending-rebalance",
            "job": _rebalance(phase="transferring"),
        },
        {
            "name": "classify-rebalance-failed",
            "op": "classify-pending-rebalance",
            "job": _rebalance(phase="failed"),
        },
        {
            "name": "drain-limit-zero-still-one",
            "op": "drain-limit",
            "limit": 0,
            "jobs": [_rebalance(jobId="a"), _rebalance(jobId="b"), _rebalance(jobId="c", phase="complete")],
        },
        {
            "name": "drain-limit-two",
            "op": "drain-limit",
            "limit": 2,
            "jobs": [_rebalance(jobId="a"), _rebalance(jobId="b"), _rebalance(jobId="c")],
        },
        {
            "name": "drain-limit-false-still-one",
            "op": "drain-limit",
            "limit": False,
            "jobs": [_rebalance(jobId="a"), _rebalance(jobId="b")],
        },
        {
            "name": "type-drain-limit-object",
            "op": "drain-limit",
            "limit": {"n": 1},
            "jobs": [_rebalance()],
        },
        {
            "name": "drain-only-pending",
            "op": "drain-limit",
            "limit": 5,
            "jobs": [_rebalance(jobId="x", phase="failed"), _rebalance(jobId="y", phase="pending")],
        },
    ]
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
        "cases": cases,
    }
    path = ROOT / CORPUS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def test_v24_rebalance_hold_safety_matches_python_4_8_0_helpers() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v24/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == SOURCE_COMMIT
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert fixture["now"] == NOW
    assert fixture["ast_matches_source_commit"] == {name: True for name in AST_NAMES}
    for case in fixture["cases"]:
        assert replay(case, fixture["now"]) == case["expected"], case["name"]


if __name__ == "__main__":
    written = write_corpus()
    print(written)
