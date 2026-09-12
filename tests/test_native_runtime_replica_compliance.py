from __future__ import annotations

import ast
import copy
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import backup_replication as br
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "a37735c68398fc8f795babaa269e2de6a5acd567"
NOW = "2026-09-05T15:00:00Z"
CORPUS_REL = "compat/native-runtime/v27/transfer/replica_compliance_vector.json"
AST_NAMES = (
    "calculate_replica_lag",
    "replication_compliance",
    "is_inside_maintenance_window",
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


def test_replica_compliance_ast_matches_frozen_4_8_0() -> None:
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


def replay(case: dict[str, Any], now: str = NOW) -> dict[str, Any]:
    op = case["op"]
    if op == "lag":
        return _lag(case)
    if op == "compliance":
        return _compliance(case)
    if op == "maintenance-window":
        return {"inside": _window(case, now)}
    raise AssertionError(op)


def _lag(case: dict[str, Any]) -> dict[str, Any]:
    copies = list(case.get("copies") or [])
    primary_target_id = case["primary_target_id"] if "primary_target_id" in case else "managed-local"
    replica_target_id = case["replica_target_id"]
    try:
        p_copies = [c for c in copies if str(c.get("targetId")) == primary_target_id and c.get("recoverable")]
        r_copies = [c for c in copies if str(c.get("targetId")) == replica_target_id and c.get("recoverable")]
        if "primary_pt" in case:
            primary_pt = case["primary_pt"]
        else:
            primary_pt = None
        if primary_pt is None and p_copies:
            primary_pt = p_copies[0]
        if "replica_pt" in case:
            replica_pt = case["replica_pt"]
        else:
            replica_pt = None
        if replica_pt is None and r_copies:
            replica_pt = r_copies[0]
        if primary_pt is None:
            return {"lagRecoveryPoints": 0, "lagSeconds": 0, "status": "no-primary"}
        if replica_pt is None:
            return {"lagRecoveryPoints": 999, "lagSeconds": 999999, "status": "no-replica"}
        p_time = br._parse_iso(primary_pt.get("committedAt"))
        r_time = br._parse_iso(replica_pt.get("committedAt"))
        lag_seconds = 0
        if p_time and r_time:
            lag_seconds = max(0, int((p_time - r_time).total_seconds()))
        p_backups = {str(c["backupId"]) for c in p_copies}
        r_backups = {str(c["backupId"]) for c in r_copies}
        lag_points = max(0, len(p_backups - r_backups))
        return {
            "lagRecoveryPoints": lag_points,
            "lagSeconds": lag_seconds,
            "primaryCommittedAt": primary_pt.get("committedAt"),
            "replicaCommittedAt": replica_pt.get("committedAt"),
            "status": "calculated",
        }
    except (TypeError, ValueError, KeyError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__}


def _compliance(case: dict[str, Any]) -> dict[str, Any]:
    policy = case.get("policy") or {}
    copies = list(case.get("copies") or [])
    jobs = list(case.get("jobs") or [])
    replication = policy.get("replication") if isinstance(policy.get("replication"), dict) else {}
    if not replication or not replication.get("enabled"):
        return {"enabled": False, "compliance": "healthy", "committedCopies": 1, "requiredCopies": 1}
    try:
        required = int(replication.get("minCommittedCopies") or 1)
        primary_target = str(policy.get("targetId") or "managed-local")
        committed = [c for c in copies if c.get("recoverable") and c.get("state") == "healthy"]
        open_required = [
            j for j in jobs if str(j.get("mode")) == "required" and str(j.get("phase") or "") not in br.TERMINAL_PHASES
        ]
        failed_required = [
            j
            for j in jobs
            if str(j.get("mode")) == "required" and str(j.get("phase") or "") in {"failed", "failed-terminal"}
        ]
        compliance = "healthy"
        reasons: list[str] = []
        if len(committed) < required:
            compliance = "degraded"
            reasons.append("insufficient-committed-copies")
        if open_required:
            compliance = "degraded"
            reasons.append("open-required-jobs")
        if failed_required:
            compliance = "degraded"
            reasons.append("failed-required-jobs")
        max_lag = (policy.get("recoveryObjectives") or {}).get("maxReplicaLagSeconds") or replication.get(
            "maxReplicaLagSeconds"
        )
        if max_lag is not None:
            for t_entry in list(replication.get("targets") or []):
                if isinstance(t_entry, dict) and t_entry.get("targetId"):
                    t_id = str(t_entry["targetId"])
                    lag_info = _lag(
                        {
                            "copies": copies,
                            "replica_target_id": t_id,
                            "primary_target_id": primary_target,
                            **(
                                {"primary_pt": case["primary_pt"]}
                                if "primary_pt" in case
                                else {}
                            ),
                            **({"replica_pt": case["replica_pt"]} if "replica_pt" in case else {}),
                        }
                    )
                    if lag_info.get("lagSeconds", 0) > int(max_lag):
                        compliance = "degraded"
                        reasons.append(f"replica-lag-exceeded:{t_id}")
        return {
            "enabled": True,
            "compliance": compliance,
            "reasons": reasons,
            "committedCopies": len(committed),
            "requiredCopies": required,
            "healthyCopies": len(committed),
            "openRequiredJobs": len(open_required),
            "failedRequiredJobs": len(failed_required),
            "available": len(committed) >= 1,
        }
    except (TypeError, ValueError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__}


def _window(case: dict[str, Any], now: str) -> bool:
    raw = case.get("policy")
    policy: dict[str, Any] = raw if isinstance(raw, dict) else {}
    current = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)
    return br.is_inside_maintenance_window(policy, now=current)


def _copy(target_id: str, backup_id: str, *, recoverable: Any = True, state: Any = "healthy", committed_at: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "targetId": target_id,
        "backupId": backup_id,
        "recoverable": recoverable,
        "state": state,
    }
    if committed_at is not None:
        body["committedAt"] = committed_at
    return body


def _job(mode: Any, phase: str) -> dict[str, Any]:
    return {"mode": mode, "phase": phase, "jobId": f"{mode}-{phase}"}


def _cases() -> list[dict[str, Any]]:
    copies = [
        _copy("managed-local", "bk-1", committed_at="2026-09-05T15:00:00Z"),
        _copy("managed-local", "bk-2", committed_at="2026-09-05T14:00:00Z"),
        _copy("target-b", "bk-1", committed_at="2026-09-05T14:00:00Z"),
    ]
    cases: list[dict[str, Any]] = [
        {
            "name": "lag-calculated",
            "op": "lag",
            "copies": copies,
            "replica_target_id": "target-b",
            "primary_target_id": "managed-local",
        },
        {
            "name": "lag-no-primary",
            "op": "lag",
            "copies": [_copy("target-b", "bk-1")],
            "replica_target_id": "target-b",
            "primary_target_id": "managed-local",
        },
        {
            "name": "lag-no-replica",
            "op": "lag",
            "copies": [_copy("managed-local", "bk-1")],
            "replica_target_id": "target-b",
            "primary_target_id": "managed-local",
        },
        {
            "name": "lag-uses-first-recoverable-not-latest",
            "op": "lag",
            "copies": [
                _copy("managed-local", "bk-old", committed_at="2026-09-05T10:00:00Z"),
                _copy("managed-local", "bk-new", committed_at="2026-09-05T15:00:00Z"),
                _copy("target-b", "bk-old", committed_at="2026-09-05T10:00:00Z"),
            ],
            "replica_target_id": "target-b",
            "primary_target_id": "managed-local",
        },
        {
            "name": "lag-explicit-latest-overrides-list-order",
            "op": "lag",
            "copies": copies,
            "replica_target_id": "target-b",
            "primary_target_id": "managed-local",
            "primary_pt": {"committedAt": "2026-09-05T15:00:00Z", "backupId": "bk-1"},
            "replica_pt": {"committedAt": "2026-09-05T14:59:00Z", "backupId": "bk-1"},
        },
        {
            "name": "lag-replica-ahead-clamped-zero",
            "op": "lag",
            "copies": [],
            "replica_target_id": "target-b",
            "primary_target_id": "managed-local",
            "primary_pt": {"committedAt": "2026-09-05T14:00:00Z"},
            "replica_pt": {"committedAt": "2026-09-05T15:00:00Z"},
        },
        {
            "name": "lag-unparseable-times-zero-seconds",
            "op": "lag",
            "copies": [_copy("managed-local", "bk-1"), _copy("target-b", "bk-1")],
            "replica_target_id": "target-b",
            "primary_pt": {"committedAt": "not-a-time"},
            "replica_pt": {"committedAt": "2026-09-05T14:00:00Z"},
        },
        {
            "name": "lag-ignores-unrecoverable",
            "op": "lag",
            "copies": [
                _copy("managed-local", "bk-1", recoverable=False, committed_at="2026-09-05T15:00:00Z"),
                _copy("target-b", "bk-1", recoverable=True, committed_at="2026-09-05T14:00:00Z"),
            ],
            "replica_target_id": "target-b",
        },
        {
            "name": "lag-degraded-state-still-counts",
            "op": "lag",
            "copies": [
                _copy("managed-local", "bk-1", state="degraded", committed_at="2026-09-05T15:00:00Z"),
                _copy("target-b", "bk-1", state="degraded", committed_at="2026-09-05T14:00:00Z"),
            ],
            "replica_target_id": "target-b",
        },
        {
            "name": "type-lag-missing-backupId",
            "op": "lag",
            "copies": [{"targetId": "managed-local", "recoverable": True}],
            "replica_target_id": "target-b",
            "primary_pt": {"committedAt": NOW},
            "replica_pt": {"committedAt": NOW},
        },
        {
            "name": "compliance-disabled",
            "op": "compliance",
            "policy": {"policyId": "p1", "replication": {"enabled": False}},
            "copies": copies,
            "jobs": [],
        },
        {
            "name": "compliance-missing-replication",
            "op": "compliance",
            "policy": {"policyId": "p1"},
            "copies": copies,
            "jobs": [],
        },
        {
            "name": "compliance-healthy",
            "op": "compliance",
            "policy": {
                "policyId": "p1",
                "targetId": "managed-local",
                "replication": {"enabled": True, "minCommittedCopies": 2, "targets": [{"targetId": "target-b"}]},
            },
            "copies": copies,
            "jobs": [_job("best-effort", "queued")],
        },
        {
            "name": "compliance-insufficient-copies",
            "op": "compliance",
            "policy": {
                "policyId": "p1",
                "replication": {"enabled": True, "minCommittedCopies": 5},
            },
            "copies": copies,
            "jobs": [],
        },
        {
            "name": "compliance-open-required",
            "op": "compliance",
            "policy": {
                "policyId": "p1",
                "replication": {"enabled": True, "minCommittedCopies": 1},
            },
            "copies": copies,
            "jobs": [_job("required", "queued")],
        },
        {
            "name": "compliance-failed-required",
            "op": "compliance",
            "policy": {
                "policyId": "p1",
                "replication": {"enabled": True, "minCommittedCopies": 1},
            },
            "copies": copies,
            "jobs": [_job("required", "failed-terminal")],
        },
        {
            "name": "compliance-mode-none-not-required",
            "op": "compliance",
            "policy": {
                "policyId": "p1",
                "replication": {"enabled": True, "minCommittedCopies": 1},
            },
            "copies": copies,
            "jobs": [_job(None, "queued")],
        },
        {
            "name": "compliance-min-copies-zero-falsy-fallback",
            "op": "compliance",
            "policy": {
                "policyId": "p1",
                "replication": {"enabled": True, "minCommittedCopies": 0},
            },
            "copies": [_copy("managed-local", "bk-1")],
            "jobs": [],
        },
        {
            "name": "compliance-lag-exceeded",
            "op": "compliance",
            "policy": {
                "policyId": "p1",
                "targetId": "managed-local",
                "replication": {
                    "enabled": True,
                    "minCommittedCopies": 1,
                    "maxReplicaLagSeconds": 10,
                    "targets": [{"targetId": "target-b"}],
                },
            },
            "copies": copies,
            "jobs": [],
            "primary_pt": {"committedAt": "2026-09-05T15:00:00Z"},
            "replica_pt": {"committedAt": "2026-09-05T14:00:00Z"},
        },
        {
            "name": "compliance-lag-within",
            "op": "compliance",
            "policy": {
                "policyId": "p1",
                "targetId": "managed-local",
                "recoveryObjectives": {"maxReplicaLagSeconds": 7200},
                "replication": {"enabled": True, "minCommittedCopies": 1, "targets": [{"targetId": "target-b"}]},
            },
            "copies": copies,
            "jobs": [],
            "primary_pt": {"committedAt": "2026-09-05T15:00:00Z"},
            "replica_pt": {"committedAt": "2026-09-05T14:00:00Z"},
        },
        {
            "name": "compliance-unhealthy-state-not-committed",
            "op": "compliance",
            "policy": {
                "policyId": "p1",
                "replication": {"enabled": True, "minCommittedCopies": 1},
            },
            "copies": [_copy("managed-local", "bk-1", state="degraded")],
            "jobs": [],
        },
        {
            "name": "type-compliance-min-copies-object",
            "op": "compliance",
            "policy": {"policyId": "p1", "replication": {"enabled": True, "minCommittedCopies": {"n": 1}}},
            "copies": copies,
            "jobs": [],
        },
        {
            "name": "window-missing-always-inside",
            "op": "maintenance-window",
            "policy": {"placement": {}},
        },
        {
            "name": "window-utc-inside",
            "op": "maintenance-window",
            "policy": {"placement": {"maintenanceWindow": {"timezone": "UTC", "start": "14:00", "end": "16:00"}}},
        },
        {
            "name": "window-utc-outside",
            "op": "maintenance-window",
            "policy": {"placement": {"maintenanceWindow": {"timezone": "UTC", "start": "16:00", "end": "18:00"}}},
        },
        {
            "name": "window-inclusive-start",
            "op": "maintenance-window",
            "policy": {"placement": {"maintenanceWindow": {"timezone": "UTC", "start": "15:00", "end": "16:00"}}},
        },
        {
            "name": "window-wrap-evening-outside",
            "op": "maintenance-window",
            "policy": {"placement": {"maintenanceWindow": {"timezone": "UTC", "start": "22:00", "end": "02:00"}}},
        },
        {
            "name": "window-wrap-late-inside",
            "op": "maintenance-window",
            "now": "2026-09-05T23:00:00Z",
            "policy": {"placement": {"maintenanceWindow": {"timezone": "UTC", "start": "22:00", "end": "02:00"}}},
        },
        {
            "name": "window-wrap-early-inside",
            "op": "maintenance-window",
            "now": "2026-09-05T01:00:00Z",
            "policy": {"placement": {"maintenanceWindow": {"timezone": "UTC", "start": "22:00", "end": "02:00"}}},
        },
        {
            "name": "window-invalid-timezone-utc-fallback",
            "op": "maintenance-window",
            "policy": {"placement": {"maintenanceWindow": {"timezone": "Not/AZone", "start": "14:00", "end": "16:00"}}},
        },
        {
            "name": "window-invalid-clock-fail-open",
            "op": "maintenance-window",
            "policy": {"placement": {"maintenanceWindow": {"timezone": "UTC", "start": "25:00", "end": "26:00"}}},
        },
        {
            "name": "window-three-part-fail-open",
            "op": "maintenance-window",
            "policy": {"placement": {"maintenanceWindow": {"timezone": "UTC", "start": "14:00:00", "end": "16:00"}}},
        },
        {
            "name": "window-non-dict-fail-open",
            "op": "maintenance-window",
            "policy": {"placement": {"maintenanceWindow": "always"}},
        },
    ]
    names = [item["name"] for item in cases]
    assert len(names) == len(set(names))
    return cases


def write_corpus() -> Path:
    cases = []
    for case in _cases():
        item = copy.deepcopy(case)
        item["expected"] = replay(case, case.get("now") or NOW)
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


def test_v27_replica_compliance_matches_python_4_8_0() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v27/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == SOURCE_COMMIT
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert fixture["ast_matches_source_commit"] == {name: True for name in AST_NAMES}
    for case in fixture["cases"]:
        assert replay(case, case.get("now") or fixture["now"]) == case["expected"], case["name"]


if __name__ == "__main__":
    written = write_corpus()
    print(written)
