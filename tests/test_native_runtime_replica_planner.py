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
CORPUS_REL = "compat/native-runtime/v29/transfer/replica_planner_vector.json"
AST_NAMES = (
    "reconcile_policy_replicas",
    "rebalance_policy_replicas",
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


def test_replica_planner_ast_matches_frozen_4_8_0() -> None:
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


def replay(case: dict[str, Any]) -> dict[str, Any]:
    op = case["op"]
    try:
        if op == "reconcile-plan":
            return _reconcile_plan(case)
        if op == "rebalance-plan":
            return _rebalance_plan(case)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return {"status": "error", "oracle_exception": type(exc).__name__}
    raise AssertionError(op)


def _reconcile_plan(case: dict[str, Any]) -> dict[str, Any]:
    policy_id = case.get("policy_id", "pol-1")
    raw_policy = case.get("policy")
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    copies = list(case.get("copies") or [])
    retired = {str(item) for item in list(case.get("retired") or [])}
    max_points = case["max_points"] if "max_points" in case else 20
    max_repairs = case["max_repairs"] if "max_repairs" in case else 2
    after_committed_at = case.get("after_committed_at")
    after_logical_id = case.get("after_logical_id")

    replication = policy.get("replication") if isinstance(policy.get("replication"), dict) else {}
    if not replication or not replication.get("enabled"):
        return {"status": "skipped", "reason": "replication-disabled", "policyId": policy_id}

    target_entries = list(replication.get("targets") or [])
    if not target_entries:
        return {"status": "noop", "policyId": policy_id}

    expected_targets = [str(e["targetId"]) for e in target_entries if isinstance(e, dict) and e.get("targetId")]
    configured_primary = str(policy.get("primaryTargetId") or policy.get("targetId") or "managed-local")
    if configured_primary not in expected_targets:
        expected_targets.append(configured_primary)

    if not expected_targets:
        return {"status": "noop", "policyId": policy_id}

    by_backup: dict[str, list[dict[str, Any]]] = {}
    for copy_item in copies:
        by_backup.setdefault(str(copy_item["backupId"]), []).append(copy_item)

    def _point_sort_key(b_id: str) -> tuple[str, str]:
        c_list = by_backup[b_id]
        cat = min((str(c.get("committedAt") or "") for c in c_list if c.get("committedAt")), default="")
        return (cat, b_id)

    sorted_backup_ids = sorted(by_backup.keys(), key=_point_sort_key)

    filtered_backup_ids: list[str] = []
    if after_committed_at or after_logical_id:
        target_tuple = (str(after_committed_at or ""), str(after_logical_id or ""))
        for b_id in sorted_backup_ids:
            if _point_sort_key(b_id) > target_tuple:
                filtered_backup_ids.append(b_id)
    else:
        filtered_backup_ids = list(sorted_backup_ids)

    wrapped = False
    if not filtered_backup_ids and sorted_backup_ids:
        filtered_backup_ids = list(sorted_backup_ids)
        wrapped = True

    scanned = 0
    repairs_triggered = 0
    last_scanned_backup_id: str | None = None
    last_scanned_committed_at: str | None = None
    planned: list[dict[str, str]] = []

    for backup_id in filtered_backup_ids:
        if scanned >= max_points:
            break
        copy_list = by_backup[backup_id]
        if backup_id in retired:
            continue
        scanned += 1
        last_scanned_backup_id = backup_id
        last_scanned_committed_at = str(copy_list[0].get("committedAt") or "")

        existing_targets = {str(c["targetId"]): c for c in copy_list}
        healthy_sources = [c for c in copy_list if c.get("recoverable") and c.get("state") == "healthy"]
        if not healthy_sources:
            continue
        source_target_id = str(healthy_sources[0]["targetId"])

        for dest_tid in expected_targets:
            if repairs_triggered >= max_repairs:
                break
            existing = existing_targets.get(dest_tid)
            needs_repair = False
            if existing is None or not existing.get("recoverable") or existing.get("state") != "healthy":
                needs_repair = True
            if needs_repair:
                repairs_triggered += 1
                planned.append(
                    {
                        "backupId": backup_id,
                        "destTargetId": dest_tid,
                        "sourceTargetId": source_target_id,
                    }
                )

    cursor_written = last_scanned_backup_id is not None
    return {
        "status": "completed",
        "policyId": policy_id,
        "scannedPoints": scanned,
        "repairsTriggered": repairs_triggered,
        "wrappedAround": wrapped,
        "cursorWritten": cursor_written,
        "afterCommittedAt": last_scanned_committed_at,
        "afterLogicalId": last_scanned_backup_id,
        "plannedRepairs": planned,
    }


def _rebalance_plan(case: dict[str, Any]) -> dict[str, Any]:
    raw_policy = case.get("policy")
    policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    copies = list(case.get("copies") or [])
    target_records = list(case.get("targets") or [])
    max_jobs = case["max_jobs"] if "max_jobs" in case else 5
    now = case.get("now") or NOW
    current = datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)

    replication = policy.get("replication") if isinstance(policy.get("replication"), dict) else {}
    if not replication or not replication.get("enabled"):
        return {"status": "skipped", "reason": "replication-disabled"}

    if not br.is_inside_maintenance_window(policy, now=current):
        return {"status": "skipped", "reason": "outside-maintenance-window"}

    min_fd = int(replication.get("minFailureDomains") or 1)
    placement = policy.get("placement") or {}
    max_copies_per_fd = placement.get("maxCopiesPerFailureDomain") or replication.get("maxCopiesPerFailureDomain")
    soft_watermark = float(placement.get("softWatermarkPercent") or 80.0)

    target_entries = list(replication.get("targets") or [])
    all_target_records = {t["targetId"]: t for t in target_records}

    active_targets = [
        str(e["targetId"])
        for e in target_entries
        if isinstance(e, dict)
        and e.get("targetId")
        and (all_target_records.get(str(e["targetId"])) or {}).get("drainState") != "draining"
    ]

    by_backup: dict[str, list[dict[str, Any]]] = {}
    for copy_item in copies:
        by_backup.setdefault(str(copy_item["backupId"]), []).append(copy_item)

    jobs_created = 0
    planned: list[dict[str, Any]] = []
    for backup_id, copy_list in by_backup.items():
        if jobs_created >= max_jobs:
            break
        healthy = [c for c in copy_list if c.get("recoverable") and c.get("state") == "healthy"]
        if not healthy:
            continue
        healthy_target_ids = {str(c["targetId"]) for c in healthy}
        current_fds = {
            str((all_target_records.get(tid) or {}).get("failureDomain") or "default") for tid in healthy_target_ids
        }

        has_draining = any(
            (all_target_records.get(tid) or {}).get("drainState") == "draining" for tid in healthy_target_ids
        )

        has_capacity_pressure = False
        constrained_source_tid = None
        for tid in healthy_target_ids:
            cap = all_target_records.get(tid) or {}
            free_pct = cap.get("freePercent")
            if free_pct is not None and (100.0 - float(free_pct)) >= soft_watermark:
                has_capacity_pressure = True
                constrained_source_tid = tid
                break

        needs_rebalance = (len(current_fds) < min_fd) or has_draining or has_capacity_pressure
        if needs_rebalance:
            for cand_tid in active_targets:
                if cand_tid not in healthy_target_ids:
                    cand_fd = str((all_target_records.get(cand_tid) or {}).get("failureDomain") or "default")
                    existing_in_fd = sum(
                        1
                        for tid in healthy_target_ids
                        if str((all_target_records.get(tid) or {}).get("failureDomain") or "default") == cand_fd
                    )
                    if max_copies_per_fd is not None and int(max_copies_per_fd) > 0:
                        if existing_in_fd + 1 > int(max_copies_per_fd):
                            continue
                    cand_cap = all_target_records.get(cand_tid) or {}
                    cand_free_pct = cand_cap.get("freePercent")
                    if cand_free_pct is not None and (100.0 - float(cand_free_pct)) >= soft_watermark:
                        continue
                    if (cand_fd not in current_fds) or has_draining or has_capacity_pressure:
                        src_tid = constrained_source_tid or str(healthy[0]["targetId"])
                        reason = (
                            "drain-migration"
                            if has_draining
                            else ("proactive-capacity-rebalance" if has_capacity_pressure else "failure-domain-diversity")
                        )
                        planned.append(
                            {
                                "backupId": backup_id,
                                "destTargetId": cand_tid,
                                "sourceTargetId": src_tid,
                                "reason": reason,
                                "pruneSourceAfter": bool(has_draining or has_capacity_pressure),
                            }
                        )
                        jobs_created += 1
                        break

    return {"status": "completed", "jobsCreated": jobs_created, "plannedJobs": planned}


def _copy(
    target_id: Any,
    backup_id: Any,
    *,
    recoverable: Any = True,
    state: Any = "healthy",
    committed_at: Any = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "targetId": target_id,
        "backupId": backup_id,
        "recoverable": recoverable,
        "state": state,
    }
    if committed_at is not None:
        body["committedAt"] = committed_at
    return body


def _target(target_id: Any, *, fd: Any = "default", drain: Any = None, free: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {"targetId": target_id, "failureDomain": fd}
    if drain is not None:
        body["drainState"] = drain
    if free is not None:
        body["freePercent"] = free
    return body


def _policy(*, enabled: Any = True, targets: Any = None, primary: Any = None, target_id: Any = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    replication: dict[str, Any] = {"enabled": enabled}
    if targets is not None:
        replication["targets"] = targets
    body: dict[str, Any] = {"replication": replication}
    if primary is not None:
        body["primaryTargetId"] = primary
    if target_id is not None:
        body["targetId"] = target_id
    if extra:
        body.update(extra)
    return body


def _enabled_policy(targets: list[Any], **kwargs: Any) -> dict[str, Any]:
    return _policy(enabled=True, targets=targets, **kwargs)


def _cases() -> list[dict[str, Any]]:
    t_local = {"targetId": "managed-local"}
    t_b = {"targetId": "target-b"}
    t_c = {"targetId": "target-c"}
    healthy_pair = [
        _copy("managed-local", "bk-1", committed_at="2026-09-05T14:00:00Z"),
        _copy("target-b", "bk-1", committed_at="2026-09-05T14:00:00Z"),
    ]
    two_points = [
        _copy("managed-local", "bk-1", committed_at="2026-09-05T10:00:00Z"),
        _copy("target-b", "bk-1", committed_at="2026-09-05T10:00:00Z"),
        _copy("managed-local", "bk-2", committed_at="2026-09-05T12:00:00Z"),
        _copy("target-b", "bk-2", committed_at="2026-09-05T12:00:00Z"),
        _copy("managed-local", "bk-3", committed_at="2026-09-05T14:00:00Z"),
        _copy("target-b", "bk-3", committed_at="2026-09-05T14:00:00Z"),
    ]
    fd_targets = [
        _target("managed-local", fd="fd-a", free=50),
        _target("target-b", fd="fd-b", free=50),
        _target("target-c", fd="fd-c", free=50),
    ]
    window_inside = {"placement": {"maintenanceWindow": {"timezone": "UTC", "start": "14:00", "end": "16:00"}}}
    window_outside = {"placement": {"maintenanceWindow": {"timezone": "UTC", "start": "16:00", "end": "18:00"}}}
    cases: list[dict[str, Any]] = [
        {
            "name": "reconcile-disabled",
            "op": "reconcile-plan",
            "policy": _policy(enabled=False, targets=[t_local, t_b]),
            "copies": healthy_pair,
        },
        {
            "name": "reconcile-missing-replication",
            "op": "reconcile-plan",
            "policy": {"policyId": "p1"},
            "copies": healthy_pair,
        },
        {
            "name": "reconcile-empty-targets-noop",
            "op": "reconcile-plan",
            "policy": _enabled_policy([]),
            "copies": healthy_pair,
        },
        {
            "name": "reconcile-missing-targets-noop",
            "op": "reconcile-plan",
            "policy": {"replication": {"enabled": True}},
            "copies": healthy_pair,
        },
        {
            "name": "reconcile-healthy-no-repair",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": healthy_pair,
        },
        {
            "name": "reconcile-missing-dest-needs-repair",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b, t_c], primary="managed-local"),
            "copies": healthy_pair,
        },
        {
            "name": "reconcile-unrecoverable-dest",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": [
                _copy("managed-local", "bk-1", committed_at="2026-09-05T14:00:00Z"),
                _copy("target-b", "bk-1", recoverable=False, committed_at="2026-09-05T14:00:00Z"),
            ],
        },
        {
            "name": "reconcile-unhealthy-state-dest",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": [
                _copy("managed-local", "bk-1", committed_at="2026-09-05T14:00:00Z"),
                _copy("target-b", "bk-1", state="degraded", committed_at="2026-09-05T14:00:00Z"),
            ],
        },
        {
            "name": "reconcile-no-healthy-source-scans",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": [
                _copy("managed-local", "bk-1", recoverable=False, committed_at="2026-09-05T14:00:00Z"),
                _copy("target-b", "bk-1", state="degraded", committed_at="2026-09-05T14:00:00Z"),
            ],
        },
        {
            "name": "reconcile-retired-skips-without-scan",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": two_points,
            "retired": ["bk-1", "bk-2"],
        },
        {
            "name": "reconcile-all-retired-no-cursor",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": healthy_pair,
            "retired": ["bk-1"],
        },
        {
            "name": "reconcile-primary-appended",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_b], primary="managed-local"),
            "copies": [_copy("target-b", "bk-1", committed_at="2026-09-05T14:00:00Z")],
        },
        {
            "name": "reconcile-primary-already-present",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": [_copy("managed-local", "bk-1", committed_at="2026-09-05T14:00:00Z")],
        },
        {
            "name": "reconcile-primary-falls-back-to-targetId",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_b], primary=0, target_id="managed-local"),
            "copies": [_copy("target-b", "bk-1", committed_at="2026-09-05T14:00:00Z")],
        },
        {
            "name": "reconcile-primary-default-managed-local",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_b]),
            "copies": [_copy("target-b", "bk-1", committed_at="2026-09-05T14:00:00Z")],
        },
        {
            "name": "reconcile-falsy-targetId-filtered",
            "op": "reconcile-plan",
            "policy": _enabled_policy([{"targetId": 0}, t_b], primary="managed-local"),
            "copies": [_copy("managed-local", "bk-1", committed_at="2026-09-05T14:00:00Z")],
        },
        {
            "name": "reconcile-cursor-filters-after",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": two_points,
            "after_committed_at": "2026-09-05T10:00:00Z",
            "after_logical_id": "bk-1",
            "max_points": 10,
        },
        {
            "name": "reconcile-cursor-wraps-past-end",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": two_points,
            "after_committed_at": "2026-09-05T14:00:00Z",
            "after_logical_id": "bk-3",
            "max_points": 1,
        },
        {
            "name": "reconcile-cursor-empty-copies-no-wrap",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": [],
            "after_committed_at": "2026-09-05T14:00:00Z",
            "after_logical_id": "bk-3",
        },
        {
            "name": "reconcile-after-committed-at-zero-falsy-no-filter",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": two_points,
            "after_committed_at": 0,
            "after_logical_id": "",
            "max_points": 1,
        },
        {
            "name": "reconcile-after-logical-id-without-time-does-not-filter-iso",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": two_points,
            "after_logical_id": "zz",
            "max_points": 10,
        },
        {
            "name": "reconcile-sort-min-committed-then-backup-id",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local], primary="managed-local"),
            "copies": [
                _copy("managed-local", "bk-b", committed_at="2026-09-05T12:00:00Z"),
                _copy("managed-local", "bk-b", committed_at="2026-09-05T10:00:00Z"),
                _copy("managed-local", "bk-a", committed_at="2026-09-05T11:00:00Z"),
            ],
            "max_points": 10,
            "max_repairs": 0,
        },
        {
            "name": "reconcile-last-scanned-uses-first-append-not-min",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local], primary="managed-local"),
            "copies": [
                _copy("managed-local", "bk-1", committed_at="2026-09-05T12:00:00Z"),
                _copy("managed-local", "bk-1", committed_at="2026-09-05T10:00:00Z"),
            ],
            "max_repairs": 0,
        },
        {
            "name": "reconcile-max-points-stops-outer",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b, t_c], primary="managed-local"),
            "copies": two_points,
            "max_points": 1,
            "max_repairs": 10,
        },
        {
            "name": "reconcile-max-repairs-breaks-inner-only",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b, t_c], primary="managed-local"),
            "copies": [
                _copy("managed-local", "bk-1", committed_at="2026-09-05T10:00:00Z"),
                _copy("managed-local", "bk-2", committed_at="2026-09-05T12:00:00Z"),
                _copy("managed-local", "bk-3", committed_at="2026-09-05T14:00:00Z"),
            ],
            "max_points": 10,
            "max_repairs": 1,
        },
        {
            "name": "reconcile-max-points-zero-scans-nothing",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": two_points,
            "max_points": 0,
        },
        {
            "name": "reconcile-duplicate-target-last-wins",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local, t_b], primary="managed-local"),
            "copies": [
                _copy("target-b", "bk-1", recoverable=False, committed_at="2026-09-05T14:00:00Z"),
                _copy("managed-local", "bk-1", committed_at="2026-09-05T14:00:00Z"),
                _copy("target-b", "bk-1", recoverable=True, state="healthy", committed_at="2026-09-05T14:00:00Z"),
            ],
        },
        {
            "name": "type-reconcile-missing-backupId",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local], primary="managed-local"),
            "copies": [{"targetId": "managed-local", "recoverable": True, "state": "healthy"}],
        },
        {
            "name": "type-reconcile-missing-targetId-on-copy",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local], primary="managed-local"),
            "copies": [{"backupId": "bk-1", "recoverable": True, "state": "healthy", "committedAt": "2026-09-05T14:00:00Z"}],
        },
        {
            "name": "type-reconcile-max-points-object",
            "op": "reconcile-plan",
            "policy": _enabled_policy([t_local], primary="managed-local"),
            "copies": healthy_pair,
            "max_points": {"n": 1},
        },
        {
            "name": "rebalance-disabled",
            "op": "rebalance-plan",
            "policy": _policy(enabled=False, targets=[t_local, t_b]),
            "targets": fd_targets,
            "copies": healthy_pair,
        },
        {
            "name": "rebalance-outside-window",
            "op": "rebalance-plan",
            "policy": _enabled_policy([t_local, t_b, t_c], extra={"placement": window_outside["placement"], "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 2}}),
            "targets": fd_targets,
            "copies": healthy_pair,
        },
        {
            "name": "rebalance-fd-diversity",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b, t_c],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 2},
                },
            ),
            "targets": fd_targets,
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-same-fd-candidate-skipped-without-drain-or-capacity",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 2},
                },
            ),
            "targets": [_target("managed-local", fd="fd-a", free=50), _target("target-b", fd="fd-a", free=50)],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-drain-reason-wins",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b, t_c],
                extra={
                    "placement": {**window_inside["placement"], "softWatermarkPercent": 80.0},
                    "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 1},
                },
            ),
            "targets": [
                _target("managed-local", fd="fd-a", drain="draining", free=10),
                _target("target-b", fd="fd-b", free=50),
                _target("target-c", fd="fd-c", free=50),
            ],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-capacity-reason",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b, t_c],
                extra={
                    "placement": {**window_inside["placement"], "softWatermarkPercent": 80.0},
                    "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 1},
                },
            ),
            "targets": [
                _target("managed-local", fd="fd-a", free=10),
                _target("target-b", fd="fd-b", free=50),
                _target("target-c", fd="fd-c", free=50),
            ],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-watermark-equal-is-pressure",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": {**window_inside["placement"], "softWatermarkPercent": 80.0},
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 1},
                },
            ),
            "targets": [_target("managed-local", fd="fd-a", free=20), _target("target-b", fd="fd-b", free=50)],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-candidate-over-watermark-skipped",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": {**window_inside["placement"], "softWatermarkPercent": 80.0},
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 2},
                },
            ),
            "targets": [_target("managed-local", fd="fd-a", free=50), _target("target-b", fd="fd-b", free=10)],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-skip-candidate-already-healthy",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 2},
                },
            ),
            "targets": [_target("managed-local", fd="fd-a", free=50), _target("target-b", fd="fd-b", free=50)],
            "copies": [_copy("managed-local", "bk-1"), _copy("target-b", "bk-1")],
        },
        {
            "name": "rebalance-max-copies-per-fd-blocks-candidate",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b, t_c],
                extra={
                    "placement": {**window_inside["placement"], "maxCopiesPerFailureDomain": 1},
                    "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 2},
                },
            ),
            "targets": [
                _target("managed-local", fd="fd-a", free=50),
                _target("target-b", fd="fd-a", free=50),
                _target("target-c", fd="fd-c", free=50),
            ],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-constrained-source-first-pressured-target",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b, t_c],
                extra={
                    "placement": {**window_inside["placement"], "softWatermarkPercent": 80.0},
                    "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 1},
                },
            ),
            "targets": [
                _target("managed-local", fd="fd-a", free=50),
                _target("target-b", fd="fd-b", free=5),
                _target("target-c", fd="fd-c", free=50),
            ],
            "copies": [_copy("managed-local", "bk-1"), _copy("target-b", "bk-1")],
        },
        {
            "name": "rebalance-source-falls-back-to-first-healthy",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b, t_c],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 2},
                },
            ),
            "targets": fd_targets,
            "copies": [_copy("target-b", "bk-1"), _copy("managed-local", "bk-1", recoverable=False)],
        },
        {
            "name": "rebalance-draining-excluded-from-active-dest",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b, t_c],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 2},
                },
            ),
            "targets": [
                _target("managed-local", fd="fd-a", free=50),
                _target("target-b", fd="fd-b", drain="draining", free=50),
                _target("target-c", fd="fd-c", free=50),
            ],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-max-jobs-caps-plans",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b, t_c],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 2},
                },
            ),
            "targets": fd_targets,
            "copies": [_copy("managed-local", "bk-1"), _copy("managed-local", "bk-2"), _copy("managed-local", "bk-3")],
            "max_jobs": 1,
        },
        {
            "name": "rebalance-no-healthy-copies-skip-backup",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 2},
                },
            ),
            "targets": [_target("managed-local", fd="fd-a", free=50), _target("target-b", fd="fd-b", free=50)],
            "copies": [_copy("managed-local", "bk-1", recoverable=False)],
        },
        {
            "name": "rebalance-insertion-order-by-backup",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b, t_c],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 2},
                },
            ),
            "targets": fd_targets,
            "copies": [_copy("managed-local", "bk-z"), _copy("managed-local", "bk-a")],
            "max_jobs": 2,
        },
        {
            "name": "rebalance-min-fd-zero-falsy-fallback-1",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 0},
                },
            ),
            "targets": [_target("managed-local", fd="fd-a", free=50), _target("target-b", fd="fd-b", free=50)],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-watermark-zero-falsy-fallback-80",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": {**window_inside["placement"], "softWatermarkPercent": 0},
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 1},
                },
            ),
            "targets": [_target("managed-local", fd="fd-a", free=10), _target("target-b", fd="fd-b", free=50)],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-window-three-part-fail-open",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b, t_c],
                extra={
                    "placement": {"maintenanceWindow": {"timezone": "UTC", "start": "14:00:00", "end": "16:00"}},
                    "replication": {"enabled": True, "targets": [t_local, t_b, t_c], "minFailureDomains": 2},
                },
            ),
            "targets": fd_targets,
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-freePercent-none-skips-capacity",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": {**window_inside["placement"], "softWatermarkPercent": 80.0},
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 1},
                },
            ),
            "targets": [_target("managed-local", fd="fd-a"), _target("target-b", fd="fd-b", free=50)],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "rebalance-numeric-record-id-misses-str-lookup",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 2},
                },
            ),
            "targets": [_target(1, fd="fd-a", free=50), _target("target-b", fd="fd-b", free=50)],
            "copies": [_copy("1", "bk-1")],
        },
        {
            "name": "rebalance-max-copies-false-falls-through-to-replication",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": {**window_inside["placement"], "maxCopiesPerFailureDomain": False},
                    "replication": {
                        "enabled": True,
                        "targets": [t_local, t_b],
                        "minFailureDomains": 2,
                        "maxCopiesPerFailureDomain": 1,
                    },
                },
            ),
            "targets": [_target("managed-local", fd="fd-a", free=50), _target("target-b", fd="fd-a", free=50)],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "type-rebalance-placement-string",
            "op": "rebalance-plan",
            "policy": {"replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 2}, "placement": "always"},
            "targets": [_target("managed-local", fd="fd-a", free=50), _target("target-b", fd="fd-b", free=50)],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "type-rebalance-min-fd-object",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": {"n": 2}},
                },
            ),
            "targets": [_target("managed-local", fd="fd-a", free=50), _target("target-b", fd="fd-b", free=50)],
            "copies": [_copy("managed-local", "bk-1")],
        },
        {
            "name": "type-rebalance-missing-backupId",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 2},
                },
            ),
            "targets": [_target("managed-local", fd="fd-a", free=50), _target("target-b", fd="fd-b", free=50)],
            "copies": [{"targetId": "managed-local", "recoverable": True, "state": "healthy"}],
        },
        {
            "name": "type-rebalance-missing-targetId-on-record",
            "op": "rebalance-plan",
            "policy": _enabled_policy(
                [t_local, t_b],
                extra={
                    "placement": window_inside["placement"],
                    "replication": {"enabled": True, "targets": [t_local, t_b], "minFailureDomains": 2},
                },
            ),
            "targets": [{"failureDomain": "fd-a", "freePercent": 50}],
            "copies": [_copy("managed-local", "bk-1")],
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


def test_v29_replica_planner_matches_python_4_8_0() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v29/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == SOURCE_COMMIT
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert fixture["ast_matches_source_commit"] == {name: True for name in AST_NAMES}
    for case in fixture["cases"]:
        assert replay(case) == case["expected"], case["name"]


if __name__ == "__main__":
    written = write_corpus()
    print(written)
