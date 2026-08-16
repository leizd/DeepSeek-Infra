"""Read-only disaster-recovery readiness aggregation (4.5.1 Gate D & F).

Zero remote I/O on GET /disaster-recovery/status. Reads exclusively from local
durable read model (.backup-dr/evidence.sqlite3), evaluates scope-aware
readiness (targetId, policyId), enforces recovery objectives, and calculates
calibrated P50/P90 RTO based on low-cardinality RecoveryClass buckets.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_component_cache,
    backup_dr_ledger,
    backup_incremental,
    backup_policies,
    backup_publish,
    backup_recovery_class,
    backup_recovery_keeper,
    backup_targets,
    backups,
)
from deepseek_infra.infra.workspace.backup_target_store import read_json

SCHEMA_VERSION = 2
RTO_EVIDENCE_WINDOW_DAYS = 30
REQUIRED_RTO_STAGES = ("transfer", "crypto", "materialization")

__all__ = [
    "backup_catalog",
    "backups",
    "readiness_status",
    "evaluate_scope_readiness",
    "aggregate_readiness",
]


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or value == "not-a-time":
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        if "+" not in cleaned and "-" not in cleaned[10:]:
            return None  # Require explicit timezone offset or Z
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _resolve_target_kind(target_id: str) -> str:
    """Resolve target kind from the real target registry — never from id prefix."""
    if target_id == "managed-local":
        return "managed-local"
    try:
        target = backup_targets.get_target(target_id)
    except Exception:
        target = None
    if isinstance(target, dict):
        kind = str(target.get("kind") or "").strip().lower()
        if kind in {"s3", "filesystem", "managed-local"}:
            return kind
        if kind:
            return kind
    return "filesystem"


def _replication_summary(
    target_id: str,
    policy_id: str,
    latest_pt: dict[str, Any] | None,
    *,
    policy: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """Summarize logical recovery-point copy counts across replica targets."""
    del now
    replication_cfg = (policy or {}).get("replication") if isinstance(policy, dict) else None
    if not isinstance(replication_cfg, dict) or not replication_cfg.get("enabled"):
        copies = 1 if latest_pt is not None else 0
        return {
            "enabled": False,
            "committedCopies": copies,
            "healthyCopies": copies,
            "requiredCopies": 1 if latest_pt is not None else 0,
            "compliance": "healthy" if latest_pt is not None else "unavailable",
            "copies": [],
        }
    backup_id = str((latest_pt or {}).get("backupId") or "")
    object_set_digest = str((latest_pt or {}).get("objectSetDigest") or (latest_pt or {}).get("chainDigest") or "")
    required = int(replication_cfg.get("minCommittedCopies") or 1)
    logical = backup_dr_ledger.list_logical_recovery_copies(
        policy_id=policy_id or None,
        backup_id=backup_id or None,
        object_set_digest=object_set_digest or None,
    ) if backup_id else []
    committed = [c for c in logical if c.get("recoverable") and c.get("state") == "healthy"]
    healthy = [c for c in committed if str(c.get("targetId") or "")]
    compliance = "healthy" if len(committed) >= required else "degraded"
    reasons: list[str] = []
    if len(committed) < required:
        reasons.append("required-copy-objective-breached")

    max_lag = (policy or {}).get("recoveryObjectives", {}).get("maxReplicaLagSeconds") or replication_cfg.get("maxReplicaLagSeconds")
    targets_info: list[dict[str, Any]] = []
    from deepseek_infra.infra.workspace import backup_replication

    for t_entry in list(replication_cfg.get("targets") or []):
        if isinstance(t_entry, dict) and t_entry.get("targetId"):
            tid = str(t_entry["targetId"])
            lag = backup_replication.calculate_replica_lag(policy_id, tid, primary_target_id=target_id)
            targets_info.append({
                "targetId": tid,
                "mode": str(t_entry.get("mode") or "required"),
                "lagRecoveryPoints": lag.get("lagRecoveryPoints", 0),
                "lagSeconds": lag.get("lagSeconds", 0),
            })
            if max_lag is not None and lag.get("lagSeconds", 0) > int(max_lag):
                compliance = "degraded"
                reasons.append(f"replica-lag-exceeded:{tid}")

    return {
        "enabled": True,
        "committedCopies": len(committed),
        "healthyCopies": len(healthy),
        "requiredCopies": required,
        "compliance": compliance if latest_pt is not None else "unavailable",
        "reason": reasons[0] if reasons else None,
        "reasons": reasons,
        "primaryTargetId": target_id,
        "targets": targets_info,
        "copies": [
            {
                "targetId": c.get("targetId"),
                "backupId": c.get("backupId"),
                "recoverable": bool(c.get("recoverable")),
                "state": c.get("state", "healthy"),
                "committedAt": c.get("committedAt"),
            }
            for c in logical
        ],
    }


def _cache_health(now: datetime) -> dict[str, Any]:
    root = backup_component_cache.CACHE_DIR
    if not root.is_dir():
        return {"status": "unavailable", "reason": "not-initialized", "source": "local-ciphertext-cache"}
    entries = bytes_used = partials = 0
    for path in root.glob("sha256/*/*"):
        if path.is_file() and path.name.endswith(".age"):
            entries += 1
            try:
                bytes_used += path.stat().st_size
            except OSError:
                pass
        elif path.is_file() and path.name.endswith(".partial"):
            partials += 1
    pinned: set[str] = set()
    try:
        for path in (root / "pins").glob("*.json") if (root / "pins").is_dir() else ():
            raw = json.loads(path.read_text(encoding="utf-8"))
            values = raw.get("digests") if isinstance(raw, dict) and raw.get("schemaVersion") == 1 else None
            if not isinstance(values, list) or any(
                not isinstance(item, str)
                or len(item) != 64
                or any(char not in "0123456789abcdef" for char in item)
                for item in values
            ):
                raise ValueError("invalid pin metadata")
            pinned.update(values)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "error", "reason": "pin-metadata-invalid", "source": "local-ciphertext-cache", "checkedAt": _utc_iso(now)}
    status = "warning" if bytes_used > backup_component_cache.DEFAULT_QUOTA_BYTES else "ok"
    return {
        "status": status,
        "source": "local-ciphertext-cache",
        "checkedAt": _utc_iso(now),
        "entries": entries,
        "bytes": bytes_used,
        "partialFiles": partials,
        "pinnedEntries": len(pinned),
        "quotaBytes": backup_component_cache.DEFAULT_QUOTA_BYTES,
        "verification": "sha256-on-hit",
    }


def evaluate_scope_readiness(
    target_id: str,
    policy_id: str = "",
    *,
    recovery_objectives: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    samples: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate disaster-recovery readiness for a specific (targetId, policyId) scope."""
    current = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    reasons: list[str] = []
    scope_status = "available"

    # 1. Recovery Point from local ledger (0 remote I/O) — strict policy scope, no cross-policy fallback
    latest_pt, chain = backup_dr_ledger.get_latest_recoverable_point(
        target_id,
        policy_id if policy_id else None,
        now=current,
    )

    if latest_pt is not None:
        committed_at = _parse_time(latest_pt.get("committedAt"))
        rpo_seconds = max(0, int((current - committed_at).total_seconds())) if committed_at else 0
        recovery_point = {
            "status": "available",
            "backupId": str(latest_pt.get("backupId") or ""),
            "targetId": target_id,
            "policyId": policy_id or str(latest_pt.get("policyId") or ""),
            "snapshotKind": str(latest_pt.get("snapshotKind") or "full"),
            "chainLength": int(latest_pt.get("chainLength") or 1),
            "recoveryPointAt": latest_pt.get("committedAt"),
            "rpoSeconds": rpo_seconds,
            "source": "dr-evidence-ledger",
        }
    else:
        rp_reason = "no-policy-recovery-point" if policy_id else "no-committed-recoverable-point"
        recovery_point = {
            "status": "unavailable",
            "reason": rp_reason,
            "source": "dr-evidence-ledger",
        }
        reasons.append(rp_reason if policy_id else "no-recoverable-points")
        scope_status = "blocked"

    # 2. Recovery Objectives check
    objectives = recovery_objectives if recovery_objectives is not None else ((policy.get("recoveryObjectives") or {}) if policy else {})
    max_rpo = objectives.get("maxRpoSeconds")
    max_scrub = objectives.get("maxScrubAgeSeconds")
    max_drill = objectives.get("maxDrillAgeSeconds")

    if latest_pt is not None and max_rpo is not None:
        rpo_val = int(recovery_point.get("rpoSeconds") or 0)
        if rpo_val > max_rpo:
            reasons.append("rpo-objective-breached")
            if scope_status == "available":
                scope_status = "objective-breached"

    # 3. Scrub Outcome & Age — strict policy scope
    scrub_record = backup_dr_ledger.get_latest_scrub_outcome(target_id, policy_id if policy_id else None, now=current)

    if scrub_record is not None:
        observed_at = _parse_time(scrub_record.get("observedAt"))
        scrub_age = max(0, int((current - observed_at).total_seconds())) if observed_at else 0
        scrub_ok = scrub_record.get("result") == "success"
        scrub = {
            "status": "ok" if scrub_ok else "error",
            "latestCheckedAt": scrub_record.get("observedAt"),
            "latestSuccessfulAt": scrub_record.get("observedAt") if scrub_ok else None,
            "ageSeconds": scrub_age,
            "source": "dr-evidence-ledger",
        }
        if not scrub_ok:
            reasons.append("scrub-failed")
            scope_status = "degraded" if scope_status == "available" else scope_status
        elif max_scrub is not None and scrub_age > max_scrub:
            reasons.append("scrub-overdue")
            if scope_status == "available":
                scope_status = "objective-breached"
    else:
        scrub_reason = "no-policy-scrub-evidence" if policy_id else "no-evidence"
        scrub = {"status": "unavailable", "reason": scrub_reason, "source": "dr-evidence-ledger"}
        if max_scrub is not None and latest_pt is not None:
            reasons.append("scrub-overdue" if scrub_reason == "no-evidence" else scrub_reason)
            if scope_status == "available":
                scope_status = "objective-breached"

    # 4. Drill Outcome & Age — strict policy scope
    drill_record = backup_dr_ledger.get_latest_drill_outcome(target_id, policy_id if policy_id else None, now=current)

    if drill_record is not None:
        observed_at = _parse_time(drill_record.get("observedAt"))
        drill_age = max(0, int((current - observed_at).total_seconds())) if observed_at else 0
        drill_result = drill_record.get("result")
        drill_ok = drill_result == "success"
        drill = {
            "status": "ok" if drill_ok else "error" if drill_result == "failed" else "blocked",
            "latestCheckedAt": drill_record.get("observedAt"),
            "latestSuccessfulAt": drill_record.get("observedAt") if drill_ok else None,
            "ageSeconds": drill_age,
            "drillKind": drill_record.get("drillKind"),
            "source": "dr-evidence-ledger",
        }
        if not drill_ok:
            reasons.append("drill-failed" if drill_result == "failed" else "drill-blocked")
            if drill_result == "failed":
                scope_status = "degraded" if scope_status == "available" else scope_status
            elif scope_status == "available":
                scope_status = "objective-breached"
        elif max_drill is not None and drill_age > max_drill:
            reasons.append("drill-overdue")
            if scope_status == "available":
                scope_status = "objective-breached"
    else:
        drill_reason = "no-policy-drill-evidence" if policy_id else "no-evidence"
        drill = {"status": "unavailable", "reason": drill_reason, "source": "dr-evidence-ledger"}
        if max_drill is not None and latest_pt is not None:
            reasons.append("drill-overdue" if drill_reason == "no-evidence" else drill_reason)
            if scope_status == "available":
                scope_status = "objective-breached"

    # 5. Target Health
    target_ev = backup_dr_ledger.get_target_evidence(target_id)
    if target_ev is not None:
        t_ok = target_ev.get("status") == "ok" or target_ev.get("scheduledReady")
        target_health = {
            "status": "ok" if t_ok else "error",
            "source": "dr-evidence-ledger",
            "checkedAt": target_ev.get("observedAt"),
        }
        if not t_ok:
            reasons.append("target-unhealthy")
            scope_status = "blocked"
    else:
        target_health = {"status": "ok", "source": "persisted-target-probe"}

    # 6. RTO Calibration (RecoveryClass-aware) — kind from target registry, never id-prefix heuristics
    if latest_pt is not None:
        logical_bytes = int(latest_pt.get("logicalBytes") or latest_pt.get("ciphertextBytes") or 0)
        chain_len = int(latest_pt.get("chainLength") or 1)
        storage_proto = str(latest_pt.get("storageProtocol") or "object-set-v1")
        t_kind = _resolve_target_kind(target_id)

        rclass = backup_recovery_class.classify_recovery(
            target_kind=t_kind,
            storage_protocol=storage_proto,
            logical_bytes=logical_bytes,
            chain_length=chain_len,
        )
        rto_estimate = backup_recovery_class.calibrate_rto(
            target_id=target_id,
            logical_bytes=logical_bytes,
            chain_length=chain_len,
            recovery_class=rclass,
            samples=samples,
        )
    else:
        rto_estimate = {
            "status": "unavailable",
            "isSla": False,
            "reason": "recovery-point-unavailable",
            "sampleCount": 0,
        }

    # 7. Replica / replication compliance from ledger (logical recovery point copies)
    replication = _replication_summary(target_id, policy_id, latest_pt, policy=policy, now=current)
    if replication.get("compliance") == "degraded":
        reasons.append(str(replication.get("reason") or "replication-objective-breached"))
        if scope_status == "available":
            scope_status = "degraded"

    # 8. Lease keeper health can degrade readiness
    lease_h = backup_recovery_keeper.get_recovery_lease_health()
    if lease_h.get("status") == "degraded":
        reasons.append(str(lease_h.get("reason") or "recovery-lease-keeper-degraded"))
        if scope_status == "available":
            scope_status = "degraded"

    return {
        "scope": {"targetId": target_id, "policyId": policy_id},
        "targetId": target_id,
        "policyId": policy_id,
        "status": scope_status,
        "reason": reasons[0] if reasons else None,
        "reasons": reasons,
        "recoverable": scope_status != "blocked",
        "recoveryPoint": recovery_point,
        "rtoEstimate": rto_estimate,
        "scrub": scrub,
        "drill": drill,
        "replication": replication,
        "committedCopies": replication.get("committedCopies"),
        "healthyCopies": replication.get("healthyCopies"),
        "requiredCopies": replication.get("requiredCopies"),
        "replicationCompliance": replication.get("compliance"),
        "health": {
            "target": target_health,
            "recoveryLeases": lease_h,
        },
        "evaluatedAt": _utc_iso(current),
        "source": "dr-evidence-ledger",
    }


def readiness_status(*, now: datetime | None = None) -> dict[str, Any]:
    """Workspace-level readiness aggregating scopes and rolling up worst status."""
    current = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    policies = backup_policies.list_policies()
    known_scopes = set(backup_dr_ledger.list_scopes())

    for pol in policies:
        t_id = str(pol.get("targetId") or "managed-local")
        p_id = str(pol.get("policyId") or "")
        if p_id:
            known_scopes.add((t_id, p_id))

    if not known_scopes:
        known_scopes.add(("managed-local", ""))

    policy_by_id = {str(p.get("policyId")): p for p in policies}
    evaluated_scopes: list[dict[str, Any]] = []

    status_severity = {"blocked": 4, "degraded": 3, "objective-breached": 2, "available": 1, "ok": 1}
    worst_severity = 1
    worst_status = "available"
    worst_reason: str | None = None

    for target_id, policy_id in sorted(known_scopes):
        pol_obj = policy_by_id.get(policy_id)
        scope_res = evaluate_scope_readiness(
            target_id,
            policy_id,
            policy=pol_obj,
            now=current,
        )
        evaluated_scopes.append(scope_res)
        s_stat = scope_res["status"]
        sev = status_severity.get(s_stat, 2)
        if sev > worst_severity:
            worst_severity = sev
            worst_status = s_stat
            worst_reason = scope_res.get("reason")

    cache_h = _cache_health(current)
    lease_h = backup_recovery_keeper.get_recovery_lease_health()

    targets = backup_targets.list_targets()
    if not targets:
        overall_target_health = "unavailable"
    else:
        overall_target_health = "ok"
        for target in targets:
            probe = target.get("lastProbe") or {}
            if probe.get("status") in ("failed", "error") or probe.get("scheduledBackupReady") is False:
                overall_target_health = "error"

    primary_scope = evaluated_scopes[0] if evaluated_scopes else {}
    primary_rp = primary_scope.get("recoveryPoint") or {"status": "unavailable", "reason": "no-committed-recoverable-point"}
    primary_rto = primary_scope.get("rtoEstimate") or {"status": "unavailable", "isSla": False}
    primary_scrub = primary_scope.get("scrub") or {"status": "unavailable"}
    primary_drill = primary_scope.get("drill") or {"status": "unavailable"}
    primary_health = primary_scope.get("health") or {}

    final_status = worst_status
    if not policies and all(s.get("recoveryPoint", {}).get("status") != "available" for s in evaluated_scopes):
        final_status = "warning"

    return {
        "status": final_status,
        "reason": worst_reason,
        "evaluatedAt": _utc_iso(current),
        "source": "dr-evidence-ledger",
        "scopes": evaluated_scopes,
        "recoveryPoint": primary_rp,
        "rtoEstimate": primary_rto,
        "scrub": primary_scrub,
        "drill": primary_drill,
        "health": {
            **primary_health,
            "target": {"status": overall_target_health, "source": "persisted-target-probe"},
            "cache": cache_h,
        },
        "cache": cache_h,
        "recoveryLeases": lease_h,
    }


def aggregate_readiness(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Backward-compatible wrapper around the legacy pure aggregator."""
    from deepseek_infra.infra.workspace.backup_dr_readiness_legacy import aggregate_readiness as _impl

    return _impl(*args, **kwargs)


# ── Backwards-Compatible Helpers for Existing Tests ─────────────────────────


def _nonnegative(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _read_index(records: list[dict[str, Any]], now: datetime) -> dict[tuple[str, str], dict[str, Any]]:
    health: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        target_id = str(record.get("targetId") or "")
        policy_id = str(record.get("policyId") or "")
        key = (target_id, policy_id)
        if key in health:
            continue
        marker_path = backup_incremental._health_marker_path(target_id, policy_id)
        if marker_path.is_file():
            health[key] = {"status": "warning", "reason": "stale-marker", "source": "local-snapshot-index", "checkedAt": _utc_iso(now)}
            continue
        if not backup_incremental.INDEX_DB.exists():
            health[key] = {"status": "unavailable", "reason": "not-initialized", "source": "local-snapshot-index", "checkedAt": _utc_iso(now)}
            continue
        try:
            conn = sqlite3.connect(backup_incremental.INDEX_DB)
            try:
                conn.row_factory = sqlite3.Row
                try:
                    ih_row = conn.execute(
                        "SELECT status, reason FROM index_health WHERE target_id = ? AND policy_id = ?",
                        (target_id, policy_id),
                    ).fetchone()
                except sqlite3.OperationalError:
                    ih_row = None

                if ih_row is not None:
                    h_stat = "error" if ih_row["status"] in ("stale", "error") else str(ih_row["status"])
                    health[key] = {
                        "status": h_stat,
                        "reason": str(ih_row["reason"] or ""),
                        "source": "local-snapshot-index",
                        "checkedAt": _utc_iso(now),
                    }
                    continue

                row = conn.execute(
                    "SELECT backup_id FROM current_effective_heads WHERE target_id = ? AND policy_id = ?",
                    (target_id, policy_id),
                ).fetchone()
                if row is None:
                    try:
                        sf_row = conn.execute(
                            "SELECT backup_id FROM snapshot_files WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
                            (target_id, policy_id, record.get("backupId")),
                        ).fetchone()
                    except sqlite3.OperationalError:
                        sf_row = None
                    if sf_row is not None:
                        health[key] = {"status": "ok", "source": "local-snapshot-index", "checkedAt": _utc_iso(now)}
                        continue
                    health[key] = {"status": "unavailable", "reason": "scope-not-indexed", "source": "local-snapshot-index", "checkedAt": _utc_iso(now)}
                    continue
                if row["backup_id"] != record.get("backupId"):
                    health[key] = {"status": "error", "reason": "head-mismatch", "source": "local-snapshot-index", "checkedAt": _utc_iso(now)}
                    continue
                try:
                    l_row = conn.execute(
                        "SELECT logical_bytes FROM snapshot_lineages WHERE backup_id = ?",
                        (record.get("backupId"),),
                    ).fetchone()
                    if l_row and l_row["logical_bytes"]:
                        record["logicalBytes"] = int(l_row["logical_bytes"])
                except sqlite3.OperationalError:
                    pass
                health[key] = {"status": "ok", "source": "local-snapshot-index", "checkedAt": _utc_iso(now)}
            finally:
                conn.close()
        except Exception:
            health[key] = {"status": "error", "reason": "index-unreadable", "source": "local-snapshot-index", "checkedAt": _utc_iso(now)}
    return health


def _commit_records_for_root(root: Path, target_id: str) -> tuple[list[dict[str, Any]], set[tuple[str, str]], bool]:
    commits_dir = root / "commits"
    receipts_dir = root / "receipts"
    if not commits_dir.is_dir():
        return [], set(), True
    records: list[dict[str, Any]] = []
    committed: set[tuple[str, str]] = set()
    healthy = True
    for c_path in sorted(commits_dir.glob("*/*.json")):
        try:
            commit_data = json.loads(c_path.read_text(encoding="utf-8"))
            if not backup_publish.commit_marker_valid(commit_data):
                healthy = False
                continue
            backup_id = str(commit_data.get("backupId") or "")
            r_path = receipts_dir / f"{backup_id}.json"
            if not r_path.is_file():
                healthy = False
                continue
            r_data = json.loads(r_path.read_text(encoding="utf-8"))
            if not r_data.get("backupId"):
                healthy = False
                continue
            records.append({**r_data, "targetId": target_id, "committedAt": commit_data.get("committedAt")})
            committed.add((target_id, backup_id))
        except Exception:
            healthy = False
    return records, committed, healthy


def _commit_records_for_store(store: Any, target_id: str) -> tuple[list[dict[str, Any]], set[tuple[str, str]], bool]:
    records: list[dict[str, Any]] = []
    committed: set[tuple[str, str]] = set()
    healthy = True
    try:
        catalog_state = backup_catalog.catalog_state_store(store)
    except Exception:
        healthy = False
        catalog_state = {}

    cursor: str | None = None
    while True:
        try:
            page = store.list_objects("commits/", cursor=cursor)
        except Exception:
            healthy = False
            break
        for meta in page.objects:
            if not str(meta.key).endswith(".json"):
                healthy = False
                continue
            commit = read_json(store, meta.key)
            if not isinstance(commit, dict) or not backup_publish.commit_marker_valid(commit):
                healthy = False
                continue
            b_id = str(commit.get("backupId") or "")
            if not b_id:
                healthy = False
                continue
            receipt = read_json(store, f"receipts/{b_id}.json")
            if not isinstance(receipt, dict) or not receipt.get("backupId"):
                healthy = False
                continue
            cat_entry = catalog_state.get(b_id, {}) if isinstance(catalog_state, dict) else {}
            records.append({**receipt, **cat_entry, "targetId": target_id, "committedAt": commit.get("committedAt")})
            committed.add((target_id, b_id))
        if not page.cursor:
            break
        cursor = page.cursor
    return records, committed, healthy


def _validated_commit_chain(markers: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    if not markers:
        return [], True
    accepted: list[dict[str, Any]] = []
    for i, marker in enumerate(markers):
        if not isinstance(marker, dict) or not backup_publish.commit_marker_valid(marker):
            return [], False
        parent = marker.get("parentCommitHash")
        if i == 0:
            if parent is not None:
                return [], False
        else:
            prev_hash = accepted[-1].get("commitHash")
            if parent != prev_hash:
                return [], False
        accepted.append(marker)
    return accepted, True


def _merge_validated_receipt(
    receipt: dict[str, Any],
    catalog_entry: dict[str, Any] | None,
    *,
    target_id: str,
) -> dict[str, Any]:
    merged = dict(receipt)
    if catalog_entry and isinstance(catalog_entry, dict):
        if "pinned" in catalog_entry:
            merged["pinned"] = catalog_entry["pinned"]
        if "scrubOk" in catalog_entry:
            merged["scrubOk"] = catalog_entry["scrubOk"]
        if "ciphertextScrubbedAt" in catalog_entry:
            merged["ciphertextScrubbedAt"] = catalog_entry["ciphertextScrubbedAt"]
    merged["targetId"] = target_id
    return merged


def _stage_samples(records: list[dict[str, Any]] | None = None, window_days: int = 30) -> list[dict[str, Any]]:
    samples = list(backup_dr_ledger.list_stage_samples())
    root = getattr(backups, "RESTORE_DIR", config.ROOT / ".restore-staging")
    if root.is_dir():
        for path in root.glob("*/remote-fetch.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    telemetry = data.get("recoveryTelemetry")
                    if isinstance(telemetry, dict) and isinstance(telemetry.get("samples"), list):
                        for s in telemetry["samples"]:
                            if isinstance(s, dict) and s.get("stage") in REQUIRED_RTO_STAGES:
                                samples.append(s)
            except (OSError, json.JSONDecodeError):
                continue
    return samples


def _drill_records(root: Path | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    # From ledger
    results.extend(backup_dr_ledger.get_drill_evidence())
    # Also scan restore staging directory
    staging_root = root if root is not None else getattr(backups, "RESTORE_DIR", config.ROOT / ".restore-staging")
    if staging_root.is_dir():
        for path in staging_root.glob("*/drill-result.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    results.append(data)
            except (OSError, json.JSONDecodeError):
                continue
    return results


def _resolve_recoverable_chain(
    item_or_records: Any,
    records_or_id: Any = None,
    committed_or_head: Any = None,
) -> list[dict[str, Any]] | None:
    if isinstance(item_or_records, dict) and isinstance(records_or_id, dict):
        item = item_or_records
        records = records_or_id
        committed = committed_or_head or set()
        target_id = str(item.get("targetId") or "")
        backup_id = str(item.get("backupId") or "")
        if (target_id, backup_id) not in committed:
            return None
        chain = [item]
        curr = item
        seen = {backup_id}
        while curr.get("parentBackupId") or curr.get("snapshotKind") == "incremental":
            parent_id = str(curr.get("parentBackupId") or "")
            if not parent_id or parent_id in seen:
                return None
            key = (target_id, str(curr.get("policyId") or ""), parent_id)
            if key not in records or (target_id, parent_id) not in committed:
                return None
            curr = records[key]
            if str(curr.get("targetId") or "") != target_id:
                return None
            if str(curr.get("policyId") or "") != str(item.get("policyId") or ""):
                return None
            chain.append(curr)
            seen.add(parent_id)
        if chain[-1].get("snapshotKind") == "incremental":
            return None
        return list(reversed(chain))
    if isinstance(item_or_records, list):
        target_b_id = str(records_or_id or "")
        return [r for r in item_or_records if r.get("backupId") == target_b_id]
    return None


def _latest_outcome(
    records: list[dict[str, Any]],
    *,
    time_key: str = "observedAt",
    success: Any = None,
    source: str = "dr-evidence-ledger",
    now: datetime | None = None,
) -> dict[str, Any]:
    if not records:
        return {"status": "unavailable", "reason": "no-evidence", "source": source}
    latest = records[0]
    is_ok = True
    if callable(success):
        is_ok = bool(success(latest))
    elif "result" in latest:
        is_ok = latest.get("result") == "success"
    return {
        "status": "ok" if is_ok else "error",
        "latestCheckedAt": latest.get(time_key) or latest.get("observedAt"),
        "latestSuccessfulAt": latest.get(time_key) if is_ok else None,
        "source": source,
    }
