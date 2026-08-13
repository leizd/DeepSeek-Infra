"""Read-only disaster-recovery readiness aggregation (4.5.0)."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import (
    backup_catalog,
    backup_component_cache,
    backup_incremental,
    backup_publish,
    backup_recovery_telemetry,
    backup_targets,
    backups,
)
from deepseek_infra.infra.workspace.backup_target_store import read_json, receipt_key

SCHEMA_VERSION = 1
RTO_EVIDENCE_WINDOW_DAYS = 30
REQUIRED_RTO_STAGES = ("transfer", "crypto", "materialization")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _nonnegative(value: Any) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _record_key(record: dict[str, Any]) -> tuple[str, str]:
    return str(record.get("targetId") or ""), str(record.get("backupId") or "")


def _active_committed(record: dict[str, Any], committed_points: set[tuple[str, str]]) -> bool:
    return (
        _record_key(record) in committed_points
        and record.get("creationVerified") is True
        and not record.get("deleted")
        and not record.get("trashed")
    )


def _resolve_recoverable_chain(
    candidate: dict[str, Any],
    records_by_id: dict[tuple[str, str, str], dict[str, Any]],
    committed_points: set[tuple[str, str]],
) -> list[dict[str, Any]] | None:
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = candidate
    target_id = str(candidate.get("targetId") or "")
    policy_id = str(candidate.get("policyId") or "")
    while True:
        backup_id = str(current.get("backupId") or "")
        if not backup_id or backup_id in seen or not _active_committed(current, committed_points):
            return None
        if str(current.get("targetId") or "") != target_id or str(current.get("policyId") or "") != policy_id:
            return None
        seen.add(backup_id)
        chain.append(current)
        parent_id = str(current.get("parentBackupId") or "")
        snapshot_kind = str(current.get("snapshotKind") or "full")
        if not parent_id:
            if snapshot_kind == "incremental":
                return None
            break
        parent = records_by_id.get((target_id, policy_id, parent_id))
        if parent is None:
            return None
        current = parent
    chain.reverse()
    return chain


def _latest_recovery_point(
    records: list[dict[str, Any]],
    committed_points: set[tuple[str, str]],
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records_by_id = {
        (str(item.get("targetId") or ""), str(item.get("policyId") or ""), str(item.get("backupId") or "")): item
        for item in records
    }
    ordered = sorted(
        ((committed_at, item) for item in records if (committed_at := _parse_time(item.get("createdAt"))) is not None and committed_at <= now),
        key=lambda item: item[0],
        reverse=True,
    )
    for committed_at, candidate in ordered:
        chain = _resolve_recoverable_chain(candidate, records_by_id, committed_points)
        if chain is None:
            continue
        return (
            {
                "status": "available",
                "backupId": str(candidate.get("backupId") or ""),
                "targetId": str(candidate.get("targetId") or ""),
                "policyId": str(candidate.get("policyId") or ""),
                "snapshotKind": str(candidate.get("snapshotKind") or "full"),
                "chainLength": len(chain),
                "recoveryPointAt": _utc_iso(committed_at),
                "rpoSeconds": max(0, int((now - committed_at).total_seconds())),
                "source": "validated-commit-and-receipt",
            },
            chain,
        )
    return (
        {
            "status": "unavailable",
            "reason": "no-committed-recoverable-point",
            "source": "validated-commit-and-receipt",
        },
        [],
    )


def _rto_estimate(
    recovery_point: dict[str, Any],
    chain: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    if recovery_point.get("status") != "available" or not chain:
        return {"status": "unavailable", "isSla": False, "reason": "recovery-point-unavailable"}
    cutoff = now - timedelta(days=RTO_EVIDENCE_WINDOW_DAYS)
    by_stage: dict[str, list[tuple[int, int]]] = {stage: [] for stage in REQUIRED_RTO_STAGES}
    for sample in samples:
        stage = str(sample.get("stage") or "")
        observed_at = _parse_time(sample.get("observedAt"))
        duration_ms = _nonnegative(sample.get("durationMs"))
        byte_count = _nonnegative(sample.get("bytes"))
        if (
            stage in by_stage
            and sample.get("result") == "success"
            and observed_at is not None
            and cutoff <= observed_at <= now
            and duration_ms > 0
            and byte_count > 0
        ):
            by_stage[stage].append((byte_count, duration_ms))
    missing = [stage for stage in REQUIRED_RTO_STAGES if not by_stage[stage]]
    if missing:
        return {
            "status": "unavailable",
            "isSla": False,
            "reason": "insufficient-recent-stage-throughput",
            "missingStages": missing,
            "evidenceWindowDays": RTO_EVIDENCE_WINDOW_DAYS,
        }
    ciphertext_bytes = sum(_nonnegative(item.get("size")) for item in chain)
    logical_bytes = _nonnegative(chain[-1].get("logicalBytes"))
    missing_workload = [name for name, value in (("ciphertextBytes", ciphertext_bytes), ("logicalBytes", logical_bytes)) if value <= 0]
    if missing_workload:
        return {
            "status": "unavailable",
            "isSla": False,
            "reason": "recovery-point-workload-unavailable",
            "missingWorkload": missing_workload,
            "evidenceWindowDays": RTO_EVIDENCE_WINDOW_DAYS,
        }
    throughput = {
        stage: (sum(item[0] for item in values) * 1_000) / sum(item[1] for item in values)
        for stage, values in by_stage.items()
    }
    workload = {"transfer": ciphertext_bytes, "crypto": ciphertext_bytes, "materialization": logical_bytes}
    stage_seconds = {stage: workload[stage] / throughput[stage] for stage in REQUIRED_RTO_STAGES}
    return {
        "status": "estimated",
        "estimatedSeconds": math.ceil(sum(stage_seconds.values())),
        "isSla": False,
        "method": "recent-successful-stage-throughput",
        "stages": {stage: {"estimatedSeconds": round(stage_seconds[stage], 3)} for stage in REQUIRED_RTO_STAGES},
        "evidence": {
            "windowDays": RTO_EVIDENCE_WINDOW_DAYS,
            "samplesByStage": {stage: len(by_stage[stage]) for stage in sorted(by_stage)},
        },
    }


def _latest_outcome(
    records: list[dict[str, Any]],
    *,
    time_key: str,
    success: Any,
    source: str,
    now: datetime,
) -> dict[str, Any]:
    observed = [
        (parsed, item)
        for item in records
        if (parsed := _parse_time(item.get(time_key))) is not None and parsed <= now
    ]
    if not observed:
        return {"status": "unavailable", "reason": "no-evidence", "source": source}
    latest_time, latest = max(observed, key=lambda item: item[0])
    successful = [item for item in observed if success(item[1])]
    result: dict[str, Any] = {
        "status": "ok" if success(latest) else "error",
        "latestCheckedAt": _utc_iso(latest_time),
        "source": source,
    }
    if successful:
        result["latestSuccessfulAt"] = _utc_iso(max(successful, key=lambda item: item[0])[0])
    else:
        result["latestSuccessfulAt"] = None
    return result


def aggregate_readiness(
    *,
    catalog_records: list[dict[str, Any]],
    committed_points: set[tuple[str, str]],
    stage_samples: list[dict[str, Any]],
    drill_records: list[dict[str, Any]],
    target_health: dict[str, dict[str, Any]],
    index_health: dict[tuple[str, str], dict[str, Any]],
    cache_health: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    recovery_point, chain = _latest_recovery_point(catalog_records, committed_points, current)
    selected_target = str(recovery_point.get("targetId") or "")
    selected_policy = str(recovery_point.get("policyId") or "")
    target = target_health.get(selected_target) or {"status": "unavailable", "reason": "no-target-health-evidence"}
    index = index_health.get((selected_target, selected_policy)) or {"status": "unavailable", "reason": "no-index-health-evidence"}
    committed_records = [item for item in catalog_records if _active_committed(item, committed_points)]
    scrub = _latest_outcome(
        [item for item in committed_records if isinstance(item.get("scrubOk"), bool)],
        time_key="ciphertextScrubbedAt",
        success=lambda item: item.get("scrubOk") is True,
        source="catalog-scrub",
        now=current,
    )
    drill = _latest_outcome(
        drill_records,
        time_key="completedAt",
        success=lambda item: item.get("result") == "success",
        source="isolated-recovery-drill",
        now=current,
    )
    rto = _rto_estimate(recovery_point, chain, stage_samples, current)
    statuses = [str(item.get("status") or "unavailable") for item in (recovery_point, scrub, drill, target, index, cache_health)]
    status = "error" if "error" in statuses else "warning" if any(item != "ok" and item != "available" for item in statuses) or rto["status"] != "estimated" else "ok"
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": status,
        "calculatedAt": _utc_iso(current),
        "recoveryPoint": recovery_point,
        "rtoEstimate": rto,
        "scrub": scrub,
        "drill": drill,
        "health": {"target": target, "index": index, "cache": cache_health},
    }


def _validated_commit_chain(markers: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    valid = [marker for marker in markers if backup_publish.commit_marker_valid(marker)]
    accepted: list[dict[str, Any]] = []
    previous = backup_publish.GENESIS_COMMIT_HASH
    generation = 1
    while True:
        candidates = [
            marker
            for marker in valid
            if int(marker.get("targetGeneration") or 0) == generation
            and str(marker.get("previousCommitHash") or "") == previous
        ]
        if not candidates:
            break
        if len(candidates) != 1:
            return accepted, False
        accepted.append(candidates[0])
        previous = str(candidates[0]["commitHash"])
        generation += 1
    return accepted, len(accepted) == len(markers)


def _merge_validated_receipt(
    receipt: dict[str, Any],
    catalog_record: dict[str, Any] | None,
    *,
    target_id: str,
) -> dict[str, Any]:
    governance_fields = (
        "pinned",
        "ciphertextScrubbedAt",
        "scrubOk",
        "userUnlockVerifiedAt",
        "trashed",
        "trashedAt",
        "deleted",
        "deletedAt",
    )
    merged = {**receipt, "targetId": target_id}
    if catalog_record is not None:
        for field in governance_fields:
            if field in catalog_record:
                merged[field] = catalog_record[field]
    return merged


def _commit_records_for_root(
    root: Path,
    target_id: str,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]], bool]:
    catalog_valid = True
    try:
        catalog = backup_catalog.catalog_state(root) if root.is_dir() else {}
    except Exception:
        catalog = {}
        catalog_valid = False
    marker_paths = sorted((root / "commits").glob("*/*.json")) if (root / "commits").is_dir() else []
    markers: list[dict[str, Any]] = []
    malformed = False
    for path in marker_paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            malformed = True
            continue
        if isinstance(raw, dict):
            markers.append(raw)
        else:
            malformed = True
    chain, chain_valid = _validated_commit_chain(markers)
    records: list[dict[str, Any]] = []
    committed: set[tuple[str, str]] = set()
    receipt_valid = True
    for marker in chain:
        backup_id = str(marker.get("backupId") or "")
        path = root / "receipts" / f"{backup_id}.json"
        try:
            raw_receipt = path.read_bytes()
            receipt = json.loads(raw_receipt)
        except (OSError, json.JSONDecodeError):
            receipt_valid = False
            continue
        if (
            not isinstance(receipt, dict)
            or str(receipt.get("backupId") or "") != backup_id
            or hashlib.sha256(raw_receipt).hexdigest() != str(marker.get("receiptDigest") or "")
        ):
            receipt_valid = False
            continue
        records.append(_merge_validated_receipt(receipt, catalog.get(backup_id), target_id=target_id))
        committed.add((target_id, backup_id))
    return records, committed, catalog_valid and chain_valid and receipt_valid and not malformed


def _commit_records_for_store(
    store: Any,
    target_id: str,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]], bool]:
    catalog_valid = True
    try:
        catalog = backup_catalog.catalog_state_store(store)
    except Exception:
        catalog = {}
        catalog_valid = False
    markers: list[dict[str, Any]] = []
    malformed = False
    cursor = None
    while True:
        page = store.list_objects("commits/", cursor=cursor)
        for meta in page.objects:
            marker = read_json(store, meta.key) if str(meta.key).endswith(".json") else None
            if isinstance(marker, dict):
                markers.append(marker)
            else:
                malformed = True
        if not page.cursor:
            break
        cursor = page.cursor
    chain, chain_valid = _validated_commit_chain(markers)
    records: list[dict[str, Any]] = []
    committed: set[tuple[str, str]] = set()
    receipt_valid = True
    for marker in chain:
        backup_id = str(marker.get("backupId") or "")
        raw_receipt = store.get_bytes(receipt_key(backup_id))
        try:
            receipt = json.loads(raw_receipt) if raw_receipt is not None else None
        except json.JSONDecodeError:
            receipt = None
        if (
            not isinstance(receipt, dict)
            or str(receipt.get("backupId") or "") != backup_id
            or raw_receipt is None
            or hashlib.sha256(raw_receipt).hexdigest() != str(marker.get("receiptDigest") or "")
        ):
            receipt_valid = False
            continue
        records.append(_merge_validated_receipt(receipt, catalog.get(backup_id), target_id=target_id))
        committed.add((target_id, backup_id))
    return records, committed, catalog_valid and chain_valid and receipt_valid and not malformed


def _read_index(records: list[dict[str, Any]], now: datetime) -> dict[tuple[str, str], dict[str, Any]]:
    scopes = {(str(item.get("targetId") or ""), str(item.get("policyId") or "")) for item in records}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    if not backup_incremental.INDEX_DB.is_file():
        return {scope: {"status": "unavailable", "reason": "not-initialized", "source": "local-snapshot-index"} for scope in scopes}
    uri = f"file:{backup_incremental.INDEX_DB.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {scope: {"status": "error", "reason": "index-unreadable", "source": "local-snapshot-index"} for scope in scopes}
    try:
        for record in records:
            row = connection.execute(
                "SELECT logical_bytes FROM snapshot_lineages WHERE target_id = ? AND policy_id = ? AND backup_id = ?",
                (str(record.get("targetId") or ""), str(record.get("policyId") or ""), str(record.get("backupId") or "")),
            ).fetchone()
            if row is not None and int(row["logical_bytes"] or 0) > 0:
                record["logicalBytes"] = int(row["logical_bytes"])
        for scope in scopes:
            marker = backup_incremental._health_marker_path(*scope)
            health = connection.execute(
                "SELECT status, reason, updated_at FROM index_health WHERE target_id = ? AND policy_id = ?",
                scope,
            ).fetchone()
            latest = connection.execute(
                "SELECT backup_id, root_digest FROM snapshot_lineages WHERE target_id = ? AND policy_id = ? ORDER BY committed_at DESC, rowid DESC LIMIT 1",
                scope,
            ).fetchone()
            head = connection.execute(
                "SELECT backup_id, root_digest FROM current_effective_heads WHERE target_id = ? AND policy_id = ?",
                scope,
            ).fetchone()
            checked_at = _utc_iso(now)
            if marker.is_file():
                result[scope] = {"status": "error", "reason": "stale-marker", "source": "local-snapshot-index", "checkedAt": checked_at}
            elif health is not None and str(health["status"]) != "healthy":
                result[scope] = {
                    "status": "error",
                    "reason": str(health["reason"] or "index-stale"),
                    "source": "local-snapshot-index",
                    "checkedAt": str(health["updated_at"] or checked_at),
                }
            elif latest is None:
                result[scope] = {"status": "unavailable", "reason": "scope-not-indexed", "source": "local-snapshot-index"}
            elif head is None and connection.execute(
                "SELECT 1 FROM snapshot_files WHERE target_id = ? AND policy_id = ? LIMIT 1",
                scope,
            ).fetchone() is not None:
                result[scope] = {"status": "ok", "source": "local-snapshot-index", "checkedAt": checked_at}
            elif head is None or tuple(head) != tuple(latest):
                result[scope] = {"status": "error", "reason": "head-mismatch", "source": "local-snapshot-index", "checkedAt": checked_at}
            else:
                result[scope] = {"status": "ok", "source": "local-snapshot-index", "checkedAt": checked_at}
    except sqlite3.Error:
        result = {scope: {"status": "error", "reason": "index-unreadable", "source": "local-snapshot-index"} for scope in scopes}
    finally:
        connection.close()
    return result


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


def _stage_samples() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    if not backups.RESTORE_DIR.is_dir():
        return samples
    for path in backups.RESTORE_DIR.glob("*/remote-fetch.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        telemetry = raw.get("recoveryTelemetry") if isinstance(raw, dict) else None
        for item in telemetry.get("samples") or [] if isinstance(telemetry, dict) else []:
            if isinstance(item, dict) and str(item.get("stage") or "") in backup_recovery_telemetry.STAGES:
                samples.append(item)
    return samples


def _drill_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not backups.RESTORE_DIR.is_dir():
        return records
    for path in backups.RESTORE_DIR.glob("*/drill-result.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            records.append(raw)
    return records


def readiness_status(*, now: datetime | None = None) -> dict[str, Any]:
    """Read committed local/registered targets without probing or mutating them."""
    current = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    records, committed, local_commit_health = _commit_records_for_root(backups.BACKUP_DIR, "managed-local")
    target_health: dict[str, dict[str, Any]] = {
        "managed-local": {
            "status": "error" if not local_commit_health else "ok" if committed else "unavailable",
            "reason": "commit-or-receipt-invalid" if not local_commit_health else None if committed else "no-validated-commit",
            "source": "local-commit-catalog",
            "checkedAt": _utc_iso(current),
        }
    }
    for target in backup_targets.list_targets():
        target_id = str(target.get("targetId") or "")
        probe = target.get("lastProbe") if isinstance(target.get("lastProbe"), dict) else None
        target_health[target_id] = (
            {
                "status": "ok" if probe.get("scheduledBackupReady") else "error",
                "source": "persisted-target-probe",
                "checkedAt": probe.get("probedAt"),
                "reason": None if probe.get("scheduledBackupReady") else str(probe.get("status") or "target-not-ready"),
            }
            if probe is not None
            else {"status": "unavailable", "reason": "never-probed", "source": "persisted-target-probe"}
        )
        try:
            kind = str(target.get("kind") or "filesystem")
            if kind == "filesystem":
                path_value = str(target.get("path") or "")
                if not path_value:
                    raise ValueError("filesystem target path is unavailable")
                target_records, target_committed, commit_health = _commit_records_for_root(Path(path_value), target_id)
            else:
                store = backup_targets.open_target_store(target_id, write_intent=False)
                target_records, target_committed, commit_health = _commit_records_for_store(store, target_id)
            records.extend(target_records)
            committed.update(target_committed)
            if not commit_health:
                target_health[target_id] = {
                    "status": "error",
                    "reason": "commit-or-receipt-invalid",
                    "source": "read-only-target-catalog",
                    "checkedAt": _utc_iso(current),
                }
        except Exception:
            target_health[target_id] = {
                "status": "error",
                "reason": "target-catalog-unreadable",
                "source": "read-only-target-catalog",
                "checkedAt": _utc_iso(current),
            }
    index_health = _read_index(records, current)
    return aggregate_readiness(
        catalog_records=records,
        committed_points=committed,
        stage_samples=_stage_samples(),
        drill_records=_drill_records(),
        target_health=target_health,
        index_health=index_health,
        cache_health=_cache_health(current),
        now=current,
    )
