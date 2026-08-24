"""Disaster recovery for local Storage Control Authority (4.6.4 production activation).

When control.sqlite3 is missing/corrupt, enter fail-closed read-only recovery,
rebuild a fresh DB from secretless control-authority-v1 checkpoints, advance
boot epoch, and never resurrect ephemeral leases/fences.

Formal truth rebuild only accepts Commit v4 → receiptDigest → Receipt bindings;
orphan receipts never become recovery authority.

Startup must obtain an Authority Verdict before workers run. All mutation
primitives call assert_control_mutations_allowed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_control, backup_control_authority

RECOVERY_ACTIVE = "active"
RECOVERY_REQUIRED = "control-recovery-required"
RECOVERY_IN_PROGRESS = "recovering"
STATE_GENESIS_REQUIRED = "genesis-required"
STATE_AUTHORITY_DIVERGENT = "authority-divergent"
STATE_AUTHORITY_UNAVAILABLE = "authority-unavailable"
STATE_RECOVERING_FORMAL_TRUTH = "recovering-formal-truth"

MUTATION_ALLOWED_STATES = frozenset({RECOVERY_ACTIVE})

# Mutations blocked unless authority verdict is ACTIVE.
BLOCKED_MUTATION_OPERATIONS = frozenset(
    {
        "backup-publish",
        "formal-mutation",
        "destructive-gc",
        "retirement",
        "rebalance",
        "tier-migration",
        "primary-promotion",
        "drain-start",
        "drain-cancel",
        "drain-complete",
        "placement-mutation",
        "policy-mutation",
        "policy-topology-mutation",
        "drain-policy-mutation",
        "target-topology-mutation",
        "chain-migration",
        "scheduler-backup-execution",
    }
)

ALLOWED_DURING_RECOVERY = frozenset(
    {
        "inspect",
        "target-probe",
        "restore-discovery",
        "control-recovery",
        "authority-verdict",
    }
)


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_control_recovery_state() -> dict[str, Any]:
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._ensure_boot_state_row(conn)  # noqa: SLF001
        row = conn.execute(
            "SELECT boot_epoch, recovery_state, reason, updated_at FROM control_boot_state WHERE id = 1"
        ).fetchone()
    assert row is not None
    return {
        "bootEpoch": int(row["boot_epoch"]),
        "recoveryState": str(row["recovery_state"]),
        "reason": str(row["reason"]) if row["reason"] else None,
        "updatedAt": str(row["updated_at"]),
    }


def enter_control_recovery_required(*, reason: str) -> dict[str, Any]:
    now = _utc_iso()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        backup_control._ensure_boot_state_row(conn, now=now)  # noqa: SLF001
        conn.execute(
            """
            UPDATE control_boot_state
            SET recovery_state = ?, reason = ?, updated_at = ?
            WHERE id = 1
            """,
            (RECOVERY_REQUIRED, str(reason), now),
        )
        conn.execute("COMMIT")
    return get_control_recovery_state()


def assert_control_mutations_allowed(*, operation: str) -> None:
    """Central mutation barrier — production paths must call before side effects."""
    op = str(operation or "").strip() or "unknown"
    if op in ALLOWED_DURING_RECOVERY:
        return
    state = get_control_recovery_state()
    recovery_state = str(state["recoveryState"])
    if recovery_state not in MUTATION_ALLOWED_STATES:
        raise AppError(
            f"control-authority-barrier:blocked:{op}:state={recovery_state}",
            code=ErrorCode.INVALID_REQUEST,
            status=503,
        )
    # Pending RPO=0 outbox / prepared intents freeze further non-rebuildable mutations.
    if backup_control_authority.pending_authority_outbox_count() > 0:
        raise AppError(
            f"authority-anchor-pending:blocked:{op}",
            code=ErrorCode.INVALID_REQUEST,
            status=503,
        )


def set_control_recovery_state(*, recovery_state: str, reason: str | None = None) -> dict[str, Any]:
    now = _utc_iso()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        backup_control._ensure_boot_state_row(conn, now=now)  # noqa: SLF001
        conn.execute(
            """
            UPDATE control_boot_state
            SET recovery_state = ?, reason = ?, updated_at = ?
            WHERE id = 1
            """,
            (str(recovery_state), str(reason) if reason else None, now),
        )
        conn.execute("COMMIT")
    return get_control_recovery_state()


def local_control_db_present() -> bool:
    return Path(backup_control.CONTROL_DB).is_file()


def local_control_db_healthy() -> bool:
    """True only when control DB opens and passes integrity without raising."""
    if not local_control_db_present():
        return False
    try:
        with backup_control._connect() as conn:  # noqa: SLF001
            row = conn.execute("PRAGMA quick_check").fetchone()
            result = str(row[0] if row is not None else "unknown")
            return result.casefold() == "ok"
    except Exception:
        return False


def resolve_startup_authority_verdict() -> dict[str, Any]:
    """Classify Control Authority before any worker may start (4.6.4 Gate A).

    Never silently creates an ACTIVE authority when remote Authority history exists
    but the local DB is missing — that path is RECOVERY_REQUIRED.
    """
    from deepseek_infra.infra.workspace import backup_control as ctrl

    present = local_control_db_present()
    healthy = local_control_db_healthy() if present else False
    anchors = backup_control_authority.authority_anchors_configured()
    remote: dict[str, dict[str, Any]] = {}
    remote_error: str | None = None
    if anchors:
        try:
            remote = {
                **discover_authority_replicas(
                    [str(p) for p in backup_control_authority.get_authority_anchor_roots()]
                ),
                **discover_authority_replicas_from_stores(backup_control_authority.get_authority_anchor_stores()),
            }
        except AppError as exc:
            remote_error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            remote_error = str(exc)

    # Missing/corrupt local + remote history → recovery, never auto-genesis ACTIVE.
    if (not present or not healthy) and remote:
        if present and not healthy:
            _quarantine_corrupt_db(Path(ctrl.CONTROL_DB))
        enter_control_recovery_required(reason="local-missing-or-corrupt-remote-authority-present")
        return {
            "verdict": RECOVERY_REQUIRED,
            "allowWorkers": False,
            "allowMutations": False,
            "localPresent": present,
            "localHealthy": healthy,
            "remoteReplicaCount": len(remote),
            "reason": "local-missing-or-corrupt-remote-authority-present",
        }

    if (not present or not healthy) and anchors and not remote:
        if present and not healthy:
            _quarantine_corrupt_db(Path(ctrl.CONTROL_DB))
        set_control_recovery_state(
            recovery_state=STATE_AUTHORITY_UNAVAILABLE,
            reason=remote_error or "authority-replicas-configured-but-unreachable",
        )
        return {
            "verdict": STATE_AUTHORITY_UNAVAILABLE,
            "allowWorkers": False,
            "allowMutations": False,
            "localPresent": present,
            "localHealthy": healthy,
            "remoteReplicaCount": 0,
            "reason": remote_error or "authority-replicas-configured-but-unreachable",
        }

    if not present:
        # First install: opening control DB creates empty ACTIVE shell.
        with ctrl._connect():  # noqa: SLF001
            pass
        set_control_recovery_state(recovery_state=RECOVERY_ACTIVE, reason="genesis-empty-control-db")
        return {
            "verdict": RECOVERY_ACTIVE,
            "allowWorkers": True,
            "allowMutations": True,
            "localPresent": True,
            "localHealthy": True,
            "remoteReplicaCount": 0,
            "reason": "genesis-empty-control-db",
        }

    # Local healthy: drain pending anchors; refuse ACTIVE if pending remains with anchors.
    ready = ctrl.ensure_control_authority_ready()
    state = get_control_recovery_state()
    if state["recoveryState"] != RECOVERY_ACTIVE:
        return {
            "verdict": state["recoveryState"],
            "allowWorkers": False,
            "allowMutations": False,
            "localPresent": True,
            "localHealthy": True,
            "remoteReplicaCount": len(remote),
            "outbox": ready,
            "reason": state.get("reason"),
        }
    if int(ready.get("pending") or 0) > 0:
        return {
            "verdict": RECOVERY_REQUIRED,
            "allowWorkers": False,
            "allowMutations": False,
            "localPresent": True,
            "localHealthy": True,
            "remoteReplicaCount": len(remote),
            "outbox": ready,
            "reason": "pending-authority-outbox",
        }
    return {
        "verdict": RECOVERY_ACTIVE,
        "allowWorkers": True,
        "allowMutations": True,
        "localPresent": True,
        "localHealthy": True,
        "remoteReplicaCount": len(remote),
        "outbox": ready,
        "reason": "local-authority-active",
    }


def workers_allowed_by_verdict(verdict: dict[str, Any] | None = None) -> bool:
    current = verdict if verdict is not None else resolve_startup_authority_verdict()
    return bool(current.get("allowWorkers"))


def _clear_ephemeral_tables(conn: Any) -> None:
    for table in sorted(backup_control.EPHEMERAL_RECOVERY_TABLES):
        conn.execute(f"DELETE FROM {table}")


def _advance_boot_epoch(conn: Any, *, reason: str) -> int:
    now = _utc_iso()
    backup_control._ensure_boot_state_row(conn, now=now)  # noqa: SLF001
    row = conn.execute("SELECT boot_epoch FROM control_boot_state WHERE id = 1").fetchone()
    epoch = int(row["boot_epoch"] if row is not None else 0) + 1
    conn.execute(
        """
        UPDATE control_boot_state
        SET boot_epoch = ?, recovery_state = ?, reason = ?, updated_at = ?
        WHERE id = 1
        """,
        (epoch, RECOVERY_ACTIVE, reason, now),
    )
    return epoch


def _quarantine_corrupt_db(db_path: Path) -> Path | None:
    if not db_path.is_file():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = db_path.with_name(f"{db_path.name}.corrupt.{stamp}")
    shutil.move(str(db_path), str(dest))
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.is_file():
            shutil.move(str(side), str(dest) + suffix)
    return dest


def discover_authority_replicas(recovery_targets: list[Path | str]) -> dict[str, dict[str, Any]]:
    replicas: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(recovery_targets):
        root = Path(raw)
        replica_id = f"replica-{index}:{root.name}"
        try:
            bundle = backup_control_authority.load_authority_bundle(root)
        except AppError:
            continue
        tip = bundle.get("checkpoint")
        head = bundle["head"]
        if tip is None:
            continue
        replicas[replica_id] = {
            "generation": int(head.get("authorityGeneration") or tip.get("authorityGeneration") or 0),
            "digest": str(head.get("digest") or tip.get("digest") or ""),
            "checkpoint": tip,
            "history": bundle.get("history") or [],
            "root": str(root),
        }
    return replicas


def discover_authority_replicas_from_stores(stores: list[Any]) -> dict[str, dict[str, Any]]:
    """Discover authority heads from Target stores (real MinIO/S3)."""
    replicas: dict[str, dict[str, Any]] = {}
    for index, store in enumerate(stores):
        replica_id = f"store-{index}"
        try:
            bundle = backup_control_authority.load_authority_bundle_from_store(store, replica_id=replica_id)
        except AppError:
            continue
        tip = bundle.get("checkpoint")
        head = bundle["head"]
        if tip is None:
            continue
        replicas[replica_id] = {
            "generation": int(head.get("authorityGeneration") or tip.get("authorityGeneration") or 0),
            "digest": str(head.get("digest") or tip.get("digest") or ""),
            "checkpoint": tip,
            "history": bundle.get("history") or [],
            "storeIndex": index,
        }
    return replicas


def reconstruct_control_authority(
    recovery_targets: list[Path | str] | None = None,
    *,
    recovery_stores: list[Any] | None = None,
    bootstrap_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild a fresh control.sqlite3 from secretless authority checkpoints.

    Skeleton scope (4.6.3):
    - select unique hash-chained authority tip
    - quarantine corrupt/missing DB
    - replay non-rebuildable policies/targets/epochs
    - clear ephemeral leases/fences
    - advance boot epoch
    - leave rebuildable projections empty for later formal-truth rebuild
    """
    del bootstrap_profile  # reserved for credential/bootstrap wiring (never stored in checkpoints)
    replicas = discover_authority_replicas(list(recovery_targets or []))
    replicas.update(discover_authority_replicas_from_stores(list(recovery_stores or [])))
    if not replicas:
        raise AppError(
            "control-authority-no-usable-replicas",
            code=ErrorCode.INVALID_REQUEST,
            status=404,
        )
    selected = backup_control_authority.select_authority_heads(replicas)
    checkpoint = selected["checkpoint"]
    if not isinstance(checkpoint, dict) or "policies" not in checkpoint:
        # Head-only stub is insufficient for reconstruction.
        raise AppError(
            "control-authority-checkpoint-incomplete",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    backup_control_authority.verify_authority_checkpoint_integrity(checkpoint)

    control_dir = Path(backup_control.CONTROL_DIR)
    control_db = Path(backup_control.CONTROL_DB)
    control_dir.mkdir(parents=True, exist_ok=True)
    quarantined = _quarantine_corrupt_db(control_db) if control_db.is_file() else None

    # Fresh DB via normal connect/migrate path.
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            UPDATE control_boot_state
            SET recovery_state = ?, reason = ?, updated_at = ?
            WHERE id = 1
            """,
            (RECOVERY_IN_PROGRESS, "reconstruct-from-authority", _utc_iso()),
        )
        _clear_ephemeral_tables(conn)
        # Clear durable tables we are about to replay (fresh node should be empty).
        for table in (
            "control_policies",
            "control_targets",
            "target_receipt_mutations",
            "control_authority_head",
            "lifecycle_intents",
            "chain_migration_jobs",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("COMMIT")

    backup_control_authority.apply_authority_checkpoint_to_fresh_db(checkpoint)

    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        _clear_ephemeral_tables(conn)
        boot_epoch = _advance_boot_epoch(conn, reason="authority-reconstructed")
        conn.execute("COMMIT")

    return {
        "status": "recovered",
        "bootEpoch": boot_epoch,
        "authorityGeneration": int(checkpoint["authorityGeneration"]),
        "authorityDigest": str(checkpoint["digest"]),
        "quarantinedPath": str(quarantined) if quarantined is not None else None,
        "replicaCount": int(selected["replicaCount"]),
        "laggingReplicas": list(selected.get("laggingReplicas") or []),
        "rebuildableProjections": "pending-formal-truth-rebuild",
    }


def _read_json_bytes(raw: bytes | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None
    return value if isinstance(value, dict) else None


def _iter_commit_markers(target: Any) -> list[dict[str, Any]]:
    """Return Commit marker dicts discovered on the target."""
    from deepseek_infra.infra.workspace import backup_publish

    if getattr(target, "root", None) is not None:
        return list(backup_publish.read_commit_markers(Path(target.root)))
    store = getattr(target, "store", None)
    if store is None:
        return []
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = store.list_objects("commits/", cursor=cursor, limit=200)
        for meta in page.objects:
            try:
                raw = store.get_bytes(meta.key)
            except Exception:
                continue
            marker = _read_json_bytes(raw)
            if marker is not None and marker.get("commitHash"):
                found.append(marker)
        if page.cursor is None:
            break
        cursor = page.cursor
    return found


def _load_receipt_bytes(target: Any, backup_id: str) -> bytes | None:
    from deepseek_infra.infra.workspace.backup_target_store import receipt_key

    if getattr(target, "root", None) is not None:
        path = Path(target.root) / "receipts" / f"{backup_id}.json"
        if not path.is_file():
            # nested layouts
            candidates = list((Path(target.root) / "receipts").rglob(f"{backup_id}.json")) if (Path(target.root) / "receipts").is_dir() else []
            if not candidates:
                return None
            path = candidates[0]
        try:
            return path.read_bytes()
        except OSError:
            return None
    store = getattr(target, "store", None)
    if store is None:
        return None
    try:
        return store.get_bytes(receipt_key(backup_id))
    except Exception:
        return None


def _list_receipt_backup_ids(target: Any) -> set[str]:
    ids: set[str] = set()
    if getattr(target, "root", None) is not None:
        receipts_dir = Path(target.root) / "receipts"
        if not receipts_dir.is_dir():
            return ids
        for path in receipts_dir.rglob("*.json"):
            ids.add(path.stem)
        return ids
    store = getattr(target, "store", None)
    if store is None:
        return ids
    cursor: str | None = None
    while True:
        page = store.list_objects("receipts/", cursor=cursor, limit=200)
        for meta in page.objects:
            name = Path(str(meta.key)).stem
            if name:
                ids.add(name)
        if page.cursor is None:
            break
        cursor = page.cursor
    return ids


def authenticate_committed_receipt(target: Any, marker: dict[str, Any]) -> dict[str, Any] | None:
    """Return receipt only when Commit hash + receiptDigest (+ object-set) bind.

    Orphan / unbound receipts return None and must not become recovery authority.
    """
    from deepseek_infra.infra.workspace import backup_object_set, backup_publish

    if not backup_publish.commit_marker_valid(marker):
        return None
    backup_id = str(marker.get("backupId") or "").strip()
    if not backup_id:
        return None
    receipt_bytes = _load_receipt_bytes(target, backup_id)
    if receipt_bytes is None:
        return None
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    if str(marker.get("receiptDigest") or "") != receipt_digest:
        return None
    receipt = _read_json_bytes(receipt_bytes)
    if receipt is None:
        return None
    # object-set-v1: Commit must bind objectSetDigest + controlObjectDigest to Receipt.
    if str(receipt.get("storageProtocol") or "") == backup_object_set.OBJECT_SET_V1:
        if int(marker.get("schemaVersion") or 0) != backup_publish.COMMIT_SCHEMA_VERSION:
            return None
        if str(marker.get("objectSetDigest") or "") != str(receipt.get("objectSetDigest") or ""):
            return None
        if str(marker.get("controlObjectDigest") or "") != str(receipt.get("controlObjectDigest") or ""):
            return None
        try:
            backup_object_set.committed_object_inventory(receipt)
        except AppError:
            return None
    elif int(receipt.get("schemaVersion") or 0) >= 4:
        # Receipt v4 without object-set still requires digest binding above.
        pass
    return {
        "backupId": backup_id,
        "receipt": receipt,
        "receiptBytes": receipt_bytes,
        "receiptDigest": receipt_digest,
        "commit": marker,
    }


def rebuild_formal_truth_from_authenticated_commits(target: Any) -> dict[str, Any]:
    """Rebuild index/lineage projections from Commit-authenticated Receipts only.

    Orphan receipts (present under receipts/ without a binding Commit) are counted
    as ``orphan-control-metadata`` and never indexed as recovery authority.
    """
    from deepseek_infra.infra.workspace import backup_object_index, backup_retirement

    target_id = str(getattr(target, "target_id", "") or "")
    if not target_id:
        raise AppError("target_id required for formal truth rebuild", code=ErrorCode.INVALID_REQUEST, status=400)

    mutation_gen = backup_control.begin_index_rebuild_clear(target_id)
    commits_seen = 0
    authenticated = 0
    invalid_commits = 0
    orphan_receipts = 0
    live = 0
    retired = 0
    indexed = 0
    authenticated_backup_ids: set[str] = set()

    backup_control.set_target_index_coverage(
        target_id,
        state="scanning",
        formal_receipt_count=0,
        source_receipt_mutation_generation=mutation_gen,
        reason="formal-truth-commit-scan",
    )

    for marker in _iter_commit_markers(target):
        commits_seen += 1
        bound = authenticate_committed_receipt(target, marker)
        if bound is None:
            invalid_commits += 1
            continue
        authenticated += 1
        receipt_obj = bound["receipt"]
        receipt_bytes_obj = bound["receiptBytes"]
        if not isinstance(receipt_obj, dict) or not isinstance(receipt_bytes_obj, (bytes, bytearray)):
            invalid_commits += 1
            continue
        receipt: dict[str, Any] = receipt_obj
        receipt_bytes = bytes(receipt_bytes_obj)
        backup_id = str(bound["backupId"])
        authenticated_backup_ids.add(backup_id)
        policy_id = str(receipt.get("policyId") or marker.get("policyId") or "")
        if not policy_id:
            invalid_commits += 1
            continue
        is_retired = backup_retirement._receipt_has_valid_retirement_marker(target, receipt_bytes, receipt)
        ref_state = "retired" if is_retired else "live"
        backup_object_index.index_receipt_objects(
            target_id=target_id,
            policy_id=policy_id,
            backup_id=backup_id,
            receipt=receipt,
            ref_state=ref_state,
        )
        indexed += 1
        if is_retired:
            retired += 1
        else:
            live += 1
        # Lineage tip from authenticated commit fields when present.
        parent = marker.get("parentBackupId") or receipt.get("parentBackupId")
        backup_control.upsert_recovery_lineage(
            policy_id=policy_id,
            backup_id=backup_id,
            parent_backup_id=str(parent) if parent else None,
            snapshot_kind=str(receipt.get("snapshotKind") or marker.get("snapshotKind") or "full"),
            chain_depth=int(receipt.get("chainDepth") or marker.get("chainDepth") or 0),
            object_set_digest=str(receipt.get("objectSetDigest") or marker.get("objectSetDigest") or "") or None,
            committed_at=str(marker.get("committedAt") or receipt.get("createdAt") or "") or None,
        )

    all_receipt_ids = _list_receipt_backup_ids(target)
    orphan_receipts = len(all_receipt_ids - authenticated_backup_ids)

    end_mutation = backup_control.get_target_receipt_mutation_generation(target_id)
    clean = (
        invalid_commits == 0
        and authenticated == indexed
        and end_mutation == mutation_gen
        and commits_seen == authenticated
    )
    # Orphans do not block completeness of authenticated truth, but are reported.
    state = "complete" if clean else "incomplete"
    reason = None if clean else (
        "invalid-or-unbound-commits"
        if invalid_commits
        else "receipt-mutation-during-rebuild"
        if end_mutation != mutation_gen
        else "authenticated-index-mismatch"
    )
    backup_control.set_target_index_coverage(
        target_id,
        state=state,
        formal_receipt_count=authenticated,
        enumerated_receipts=commits_seen,
        parsed_receipts=authenticated,
        indexed_receipts=indexed,
        parse_failures=invalid_commits,
        read_failures=0,
        source_receipt_mutation_generation=end_mutation,
        reason=reason or "formal-truth-authenticated",
    )
    return {
        "targetId": target_id,
        "coverageState": state,
        "commitsSeen": commits_seen,
        "authenticatedRecoveryPoints": authenticated,
        "invalidCommits": invalid_commits,
        "orphanControlMetadata": orphan_receipts,
        "indexedReceipts": indexed,
        "live": live,
        "retired": retired,
        "source": "commit-authenticated-receipts",
    }
