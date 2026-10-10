"""Disaster recovery for local Storage Control Authority (4.6.5 completion).

When control.sqlite3 is missing/corrupt, enter fail-closed read-only recovery,
rebuild a fresh DB from secretless control-authority-v1 checkpoints, advance
boot epoch only after formal truth validation, and never resurrect ephemeral leases/fences.

Formal truth rebuild only accepts Commit v4 → receiptDigest → Receipt bindings;
orphan receipts never become recovery authority.

Startup must resolve AuthorityReplicaProvider before Verdict. All mutation
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
STATE_AUTHORITY_CONFIGURATION_REQUIRED = "authority-configuration-required"
STATE_AUTHORITY_BOOTSTRAP_FAILED = "authority-bootstrap-failed"
STATE_RECOVERING_AUTHORITY = "recovering-authority"
STATE_RECOVERING_FORMAL_TRUTH = "recovering-formal-truth"
STATE_VALIDATING = "validating"
STATE_ANCHORING_NEW_EPOCH = "anchoring-new-epoch"

# In-process Formal Truth validation attestations (4.6.7) — not forgeable via coverage alone.
_FORMAL_TRUTH_ATTESTATIONS: dict[str, dict[str, Any]] = {}

# Table classification (4.6.5 Gate H).
DURABLE_AUTHORITY_TABLES = frozenset(
    {
        "control_policies",
        "control_targets",
        "control_authority_head",
        "control_authority_outbox",
        "control_authority_mutations",
        "control_boot_state",
        "target_receipt_mutations",
        "schema_migrations",
    }
)
REBUILDABLE_PROJECTION_TABLES = frozenset(backup_control.REBUILDABLE_TABLES)
RECONCILABLE_INTENT_TABLES = frozenset(
    {
        "lifecycle_intents",
        "chain_migration_jobs",
    }
)
EPHEMERAL_OWNERSHIP_TABLES = frozenset(backup_control.EPHEMERAL_RECOVERY_TABLES)

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
    # Genesis ceremony is the only path that may create the first authority.
    if op == "control-genesis":
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


def _ensure_provider_before_verdict() -> dict[str, Any] | None:
    """Resolve AuthorityReplicaProvider before verdict (4.6.7 fail-closed bootstrap).

    Returns optional bootstrap-failure verdict payload; None means continue.
    """
    from deepseek_infra.infra.workspace import backup_authority_provider
    from deepseek_infra.infra.workspace import backup_control as ctrl

    if backup_authority_provider.get_authority_replica_provider() is not None:
        return None
    # Legacy in-process roots/stores still win when tests configure them.
    if backup_control_authority.authority_anchors_configured():
        backup_authority_provider.sync_provider_from_legacy_globals()
        return None
    # Production fresh process: bootstrap + production S3 store factory (no secrets).
    try:
        backup_authority_provider.install_provider_from_bootstrap(
            store_factory=backup_authority_provider.production_authority_store_factory
        )
    except AppError as exc:
        if not local_control_db_present():
            with ctrl._connect():  # noqa: SLF001
                pass
        reason = f"authority-bootstrap-failed:{exc}"
        set_control_recovery_state(recovery_state=STATE_AUTHORITY_BOOTSTRAP_FAILED, reason=reason)
        return {
            "verdict": STATE_AUTHORITY_BOOTSTRAP_FAILED,
            "allowWorkers": False,
            "allowMutations": False,
            "localPresent": local_control_db_present(),
            "localHealthy": local_control_db_healthy() if local_control_db_present() else False,
            "remoteReplicaCount": 0,
            "configuredReplicaCount": 0,
            "resolvedReplicaCount": 0,
            "reason": reason,
        }
    except Exception as exc:  # noqa: BLE001 — must fail closed, never pretend no config
        if not local_control_db_present():
            with ctrl._connect():  # noqa: SLF001
                pass
        reason = f"unexpected-authority-bootstrap-failure:{type(exc).__name__}:{exc}"
        set_control_recovery_state(recovery_state=STATE_AUTHORITY_BOOTSTRAP_FAILED, reason=reason)
        return {
            "verdict": STATE_AUTHORITY_BOOTSTRAP_FAILED,
            "allowWorkers": False,
            "allowMutations": False,
            "localPresent": local_control_db_present(),
            "localHealthy": local_control_db_healthy() if local_control_db_present() else False,
            "remoteReplicaCount": 0,
            "configuredReplicaCount": 0,
            "resolvedReplicaCount": 0,
            "reason": reason,
        }
    return None


def resolve_startup_authority_verdict() -> dict[str, Any]:
    """Classify Control Authority before any worker may start (4.6.7).

    - replicated + configured=0 + no local DB → AUTHORITY_CONFIGURATION_REQUIRED
    - configured but unresolved → AUTHORITY_UNAVAILABLE
    - bootstrap exception → AUTHORITY_BOOTSTRAP_FAILED
    - local-only ACTIVE only when mode=local-only (explicit opt-in)
    """
    from deepseek_infra.infra.workspace import backup_authority_provider
    from deepseek_infra.infra.workspace import backup_control as ctrl

    bootstrap_fail = _ensure_provider_before_verdict()
    if bootstrap_fail is not None:
        return bootstrap_fail
    status = backup_authority_provider.provider_status()
    configured_n = int(status.get("configuredReplicaCount") or 0)
    resolved_n = int(status.get("resolvedReplicaCount") or 0)
    mode = str(status.get("mode") or backup_authority_provider.MODE_REPLICATED)

    present = local_control_db_present()
    healthy = local_control_db_healthy() if present else False
    anchors = backup_control_authority.authority_anchors_configured()
    remote: dict[str, dict[str, Any]] = {}
    remote_error: str | None = None

    # Configured but unresolved (e.g. S3 factory failed) — fail closed, never local-only.
    if configured_n > 0 and resolved_n == 0 and mode != backup_authority_provider.MODE_LOCAL_ONLY:
        if not local_control_db_present():
            with ctrl._connect():  # noqa: SLF001
                pass
        reason = "authority-replicas-configured-but-unresolved"
        errs = status.get("resolveErrors") or []
        if errs:
            reason = f"{reason}:{errs[0]}"
        set_control_recovery_state(recovery_state=STATE_AUTHORITY_UNAVAILABLE, reason=reason)
        return {
            "verdict": STATE_AUTHORITY_UNAVAILABLE,
            "allowWorkers": False,
            "allowMutations": False,
            "localPresent": present,
            "localHealthy": healthy,
            "remoteReplicaCount": 0,
            "configuredReplicaCount": configured_n,
            "resolvedReplicaCount": 0,
            "reason": reason,
            "provider": status,
        }

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
        # Need a DB shell to record recovery state.
        if not local_control_db_present():
            with ctrl._connect():  # noqa: SLF001
                pass
        enter_control_recovery_required(reason="local-missing-or-corrupt-remote-authority-present")
        return {
            "verdict": RECOVERY_REQUIRED,
            "allowWorkers": False,
            "allowMutations": False,
            "localPresent": present,
            "localHealthy": healthy,
            "remoteReplicaCount": len(remote),
            "configuredReplicaCount": configured_n,
            "resolvedReplicaCount": resolved_n,
            "reason": "local-missing-or-corrupt-remote-authority-present",
            "provider": status,
        }

    if (not present or not healthy) and (anchors or configured_n > 0) and not remote:
        if present and not healthy:
            _quarantine_corrupt_db(Path(ctrl.CONTROL_DB))
        if not local_control_db_present():
            with ctrl._connect():  # noqa: SLF001
                pass
        # Configured replicas but empty remote history → explicit genesis ceremony.
        if remote_error:
            set_control_recovery_state(
                recovery_state=STATE_AUTHORITY_UNAVAILABLE,
                reason=remote_error,
            )
            return {
                "verdict": STATE_AUTHORITY_UNAVAILABLE,
                "allowWorkers": False,
                "allowMutations": False,
                "localPresent": local_control_db_present(),
                "localHealthy": False,
                "remoteReplicaCount": 0,
                "configuredReplicaCount": configured_n,
                "resolvedReplicaCount": resolved_n,
                "reason": remote_error,
                "provider": status,
            }
        set_control_recovery_state(
            recovery_state=STATE_GENESIS_REQUIRED,
            reason="authority-replicas-configured-empty-history",
        )
        return {
            "verdict": STATE_GENESIS_REQUIRED,
            "allowWorkers": False,
            "allowMutations": False,
            "localPresent": True,
            "localHealthy": True,
            "remoteReplicaCount": 0,
            "configuredReplicaCount": configured_n,
            "resolvedReplicaCount": resolved_n,
            "reason": "authority-replicas-configured-empty-history",
            "provider": status,
        }

    if not present:
        # 4.6.7: replicated mode never auto local-only; must opt-in mode=local-only.
        if mode != backup_authority_provider.MODE_LOCAL_ONLY:
            if not local_control_db_present():
                with ctrl._connect():  # noqa: SLF001
                    pass
            if configured_n > 0:
                reason = "authority-replicas-configured-forbid-implicit-local-genesis"
                state = STATE_AUTHORITY_UNAVAILABLE
            else:
                reason = "authority-configuration-required"
                state = STATE_AUTHORITY_CONFIGURATION_REQUIRED
            set_control_recovery_state(recovery_state=state, reason=reason)
            return {
                "verdict": state,
                "allowWorkers": False,
                "allowMutations": False,
                "localPresent": False,
                "localHealthy": False,
                "remoteReplicaCount": 0,
                "configuredReplicaCount": configured_n,
                "resolvedReplicaCount": resolved_n,
                "reason": reason,
                "provider": status,
                "mode": mode,
            }
        with ctrl._connect():  # noqa: SLF001
            pass
        set_control_recovery_state(recovery_state=RECOVERY_ACTIVE, reason="genesis-local-only")
        return {
            "verdict": RECOVERY_ACTIVE,
            "allowWorkers": True,
            "allowMutations": True,
            "localPresent": True,
            "localHealthy": True,
            "remoteReplicaCount": 0,
            "configuredReplicaCount": configured_n,
            "resolvedReplicaCount": resolved_n,
            "reason": "genesis-local-only",
            "provider": status,
            "mode": mode,
        }

    # Local healthy: drain pending anchors; refuse ACTIVE if pending remains with anchors.
    ready = ctrl.ensure_control_authority_ready()
    boot_state = get_control_recovery_state()
    if boot_state["recoveryState"] != RECOVERY_ACTIVE:
        return {
            "verdict": boot_state["recoveryState"],
            "allowWorkers": False,
            "allowMutations": False,
            "localPresent": True,
            "localHealthy": True,
            "remoteReplicaCount": len(remote),
            "outbox": ready,
            "reason": boot_state.get("reason"),
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


def initialize_control_authority(*, reason: str = "explicit-genesis") -> dict[str, Any]:
    """Explicit genesis ceremony (4.6.5 Gate B): durable generation-1 authority.

    When replicas are configured, generation 1 must be written before ACTIVE.
    Local-only mode records a local head without remote anchors.
    """
    from deepseek_infra.infra.workspace import backup_control as ctrl

    _ensure_provider_before_verdict()
    if not local_control_db_present():
        with ctrl._connect():  # noqa: SLF001
            pass
    set_control_recovery_state(recovery_state=STATE_GENESIS_REQUIRED, reason=reason)
    # Build empty generation-1 checkpoint with boot epoch 1.
    with ctrl._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        backup_control._ensure_boot_state_row(conn)  # noqa: SLF001
        conn.execute(
            """
            UPDATE control_boot_state
            SET boot_epoch = 1, recovery_state = ?, reason = ?, updated_at = ?
            WHERE id = 1
            """,
            (STATE_GENESIS_REQUIRED, reason, _utc_iso()),
        )
        prepared = None
        if backup_control_authority.authority_anchors_configured():
            prepared = backup_control_authority.prepare_authority_mutation_in_tx(
                conn, kind="control-genesis"
            )
        else:
            checkpoint = backup_control_authority._snapshot_authority_from_conn(conn)  # noqa: SLF001
            backup_control_authority.record_local_authority_head(checkpoint, conn=conn)
        conn.execute("COMMIT")
    if prepared is not None:
        backup_control_authority.anchor_non_rebuildable_mutation(
            kind="control-genesis",
            prepared=prepared,
            rpo_zero=True,
        )
    set_control_recovery_state(recovery_state=RECOVERY_ACTIVE, reason=f"genesis-complete:{reason}")
    head = None
    with ctrl._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT authority_generation, authority_digest FROM control_authority_head WHERE id = 1"
        ).fetchone()
        if row is not None:
            head = {
                "authorityGeneration": int(row["authority_generation"]),
                "authorityDigest": str(row["authority_digest"]),
            }
    return {
        "status": "genesis-complete",
        "recoveryState": RECOVERY_ACTIVE,
        "head": head,
        "reason": reason,
    }


def workers_allowed_by_verdict(verdict: dict[str, Any] | None = None) -> bool:
    current = verdict if verdict is not None else resolve_startup_authority_verdict()
    return bool(current.get("allowWorkers"))


def _clear_ephemeral_tables(conn: Any) -> None:
    for table in sorted(backup_control.EPHEMERAL_RECOVERY_TABLES):
        conn.execute(f"DELETE FROM {table}")


def _advance_boot_epoch(
    conn: Any,
    *,
    reason: str,
    recovery_state: str = RECOVERY_ACTIVE,
    minimum_epoch: int | None = None,
) -> int:
    now = _utc_iso()
    backup_control._ensure_boot_state_row(conn, now=now)  # noqa: SLF001
    row = conn.execute("SELECT boot_epoch FROM control_boot_state WHERE id = 1").fetchone()
    current = int(row["boot_epoch"] if row is not None else 0)
    epoch = current + 1
    if minimum_epoch is not None:
        epoch = max(epoch, int(minimum_epoch))
    conn.execute(
        """
        UPDATE control_boot_state
        SET boot_epoch = ?, recovery_state = ?, reason = ?, updated_at = ?
        WHERE id = 1
        """,
        (epoch, recovery_state, reason, now),
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
    activate: bool = False,
) -> dict[str, Any]:
    """Rebuild a fresh control.sqlite3 from secretless authority checkpoints.

    4.6.6: defaults to ``activate=False`` — plane stays RECOVERING_FORMAL_TRUTH until
    ``activate_control_after_formal_truth`` after Commit-authenticated rebuild.
    Auto-activate only when ``activate=True`` and zero registered targets remain.
    """
    del bootstrap_profile  # reserved for credential/bootstrap wiring (never stored in checkpoints)
    bootstrap_fail = _ensure_provider_before_verdict()
    if bootstrap_fail is not None:
        raise AppError(
            str(bootstrap_fail.get("reason") or "authority-bootstrap-failed"),
            code=ErrorCode.INVALID_REQUEST,
            status=503,
        )
    clear_formal_truth_attestations()
    replicas = discover_authority_replicas(list(recovery_targets or []))
    # Prefer explicit stores; else provider-discovered stores (production S3 bootstrap).
    store_list = list(recovery_stores or [])
    if not store_list:
        from deepseek_infra.infra.workspace import backup_authority_provider

        provider = backup_authority_provider.get_authority_replica_provider()
        if provider is not None:
            for item in provider.discover():
                if item.store is not None:
                    store_list.append(item.store)
        if not store_list:
            store_list = list(backup_control_authority.get_authority_anchor_stores())
    store_by_index = {f"store-{i}": s for i, s in enumerate(store_list)}
    replicas.update(discover_authority_replicas_from_stores(store_list))
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

    # Anti-entropy: repair lagging ancestor replicas (filesystem + S3 stores).
    history_views = selected.get("historyViews") or {}
    canonical_id = str(selected.get("canonicalReplicaId") or "")
    canonical_view = history_views.get(canonical_id) if isinstance(history_views, dict) else None
    canonical_history = list((canonical_view or {}).get("history") or [])
    repair_report: dict[str, Any] = {"repaired": [], "errors": []}
    if canonical_history and selected.get("laggingReplicas"):
        lagging_handles: list[dict[str, Any]] = []
        for rid in selected.get("laggingReplicas") or []:
            src = replicas.get(str(rid)) or {}
            handle: dict[str, Any] = {
                "replicaId": str(rid),
                "tipGeneration": int(src.get("generation") or 0),
            }
            if src.get("root"):
                handle["root"] = src["root"]
            store_index = src.get("storeIndex")
            if store_index is not None and f"store-{int(store_index)}" in store_by_index:
                handle["store"] = store_by_index[f"store-{int(store_index)}"]
            elif str(rid) in store_by_index:
                handle["store"] = store_by_index[str(rid)]
            lagging_handles.append(handle)
        repairable = [h for h in lagging_handles if h.get("root") or h.get("store")]
        if repairable:
            try:
                repair_report = backup_control_authority.repair_lagging_authority_replicas(
                    canonical_history=canonical_history,
                    lagging=repairable,
                )
            except AppError as exc:
                repair_report = {"repaired": [], "errors": [str(exc)], "status": "failed"}

    control_dir = Path(backup_control.CONTROL_DIR)
    control_db = Path(backup_control.CONTROL_DB)
    control_dir.mkdir(parents=True, exist_ok=True)
    quarantined = _quarantine_corrupt_db(control_db) if control_db.is_file() else None

    remote_boot = checkpoint.get("controlBootEpoch")
    remote_boot_epoch = int(remote_boot) if remote_boot is not None else None

    # Fresh DB via normal connect/migrate path.
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            UPDATE control_boot_state
            SET recovery_state = ?, reason = ?, updated_at = ?
            WHERE id = 1
            """,
            (STATE_RECOVERING_AUTHORITY, "reconstruct-from-authority", _utc_iso()),
        )
        _clear_ephemeral_tables(conn)
        # Clear durable tables we are about to replay (fresh node should be empty).
        # Reconcilable intents are cleared then left for target-truth reconcile (not durable authority).
        for table in (
            "control_policies",
            "control_targets",
            "target_receipt_mutations",
            "control_authority_head",
            "control_authority_mutations",
            "control_authority_outbox",
            "lifecycle_intents",
            "chain_migration_jobs",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("COMMIT")

    backup_control_authority.apply_authority_checkpoint_to_fresh_db(checkpoint)

    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        _clear_ephemeral_tables(conn)
        # Seed boot epoch from durable authority when present; do not ACTIVE yet.
        if remote_boot_epoch is not None:
            conn.execute(
                """
                UPDATE control_boot_state
                SET boot_epoch = ?, recovery_state = ?, reason = ?, updated_at = ?
                WHERE id = 1
                """,
                (
                    int(remote_boot_epoch),
                    STATE_RECOVERING_FORMAL_TRUTH,
                    "authority-replayed-awaiting-formal-truth",
                    _utc_iso(),
                ),
            )
            boot_epoch = int(remote_boot_epoch)
        else:
            conn.execute(
                """
                UPDATE control_boot_state
                SET recovery_state = ?, reason = ?, updated_at = ?
                WHERE id = 1
                """,
                (STATE_RECOVERING_FORMAL_TRUTH, "authority-replayed-awaiting-formal-truth", _utc_iso()),
            )
            row = conn.execute("SELECT boot_epoch FROM control_boot_state WHERE id = 1").fetchone()
            boot_epoch = int(row["boot_epoch"] if row is not None else 1)
        conn.execute("COMMIT")

    result = {
        "status": "authority-restored",
        "bootEpoch": boot_epoch,
        "authorityGeneration": int(checkpoint["authorityGeneration"]),
        "authorityDigest": str(checkpoint["digest"]),
        "quarantinedPath": str(quarantined) if quarantined is not None else None,
        "replicaCount": int(selected["replicaCount"]),
        "laggingReplicas": list(selected.get("laggingReplicas") or []),
        "rebuildableProjections": "pending-formal-truth-rebuild",
        "recoveryState": STATE_RECOVERING_FORMAL_TRUTH,
        "controlBootEpochFromAuthority": remote_boot_epoch,
        "antiEntropy": repair_report,
    }
    # Count targets after authority apply.
    with backup_control._connect() as conn:  # noqa: SLF001
        target_n = int(conn.execute("SELECT COUNT(*) AS c FROM control_targets").fetchone()["c"])
    if activate and target_n == 0:
        # Zero-target authority may activate (no Formal Truth surface).
        activated = activate_control_after_formal_truth(reason="authority-reconstructed-zero-targets")
        result.update(
            {
                "status": "recovered",
                "bootEpoch": activated["bootEpoch"],
                "recoveryState": RECOVERY_ACTIVE,
                "activation": activated,
            }
        )
    elif activate and target_n > 0:
        # Refuse silent bypass — caller must rebuild Formal Truth first.
        result["activation"] = {
            "status": "blocked",
            "reason": "formal-truth-required-before-activate",
            "targetCount": target_n,
        }
    return result


def activate_control_after_formal_truth(
    *,
    reason: str = "formal-truth-validated",
    require_complete_coverage: bool | None = None,
) -> dict[str, Any]:
    """Anchor a new durable boot epoch and enter ACTIVE after formal truth rebuild.

    4.6.7: when targets exist, requires Formal Truth VALID attestation per target
    (coverage complete is necessary but not sufficient — attestation must exist).
    """
    set_control_recovery_state(recovery_state=STATE_VALIDATING, reason=reason)
    with backup_control._connect() as conn:  # noqa: SLF001
        targets = conn.execute("SELECT target_id FROM control_targets").fetchall()
        target_ids = [str(row["target_id"]) for row in targets]
    must_check = bool(target_ids) or (require_complete_coverage is True)
    if must_check and target_ids:
        with backup_control._connect() as conn:  # noqa: SLF001
            head = conn.execute(
                "SELECT authority_generation FROM control_authority_head WHERE id = 1"
            ).fetchone()
            current_gen = int(head["authority_generation"]) if head is not None else None
            for tid in target_ids:
                cov = conn.execute(
                    "SELECT state FROM target_index_coverage WHERE target_id = ?",
                    (tid,),
                ).fetchone()
                state = str(cov["state"]) if cov is not None else "missing"
                if state != "complete":
                    raise AppError(
                        f"formal-truth-incomplete:{tid}:state={state}",
                        code=ErrorCode.INVALID_REQUEST,
                        status=503,
                    )
                att = get_formal_truth_validation(tid)
                if att is None or str(att.get("status") or "") != "VALID":
                    raise AppError(
                        f"formal-truth-attestation-missing-or-invalid:{tid}",
                        code=ErrorCode.INVALID_REQUEST,
                        status=503,
                    )
                if not att.get("indexCoverageComplete"):
                    raise AppError(
                        f"formal-truth-attestation-coverage-incomplete:{tid}",
                        code=ErrorCode.INVALID_REQUEST,
                        status=503,
                    )
                if int(att.get("invalidCommitCount") or 0) > 0:
                    raise AppError(
                        f"formal-truth-attestation-invalid-commits:{tid}",
                        code=ErrorCode.INVALID_REQUEST,
                        status=503,
                    )
                if not att.get("lineageValid"):
                    raise AppError(
                        f"formal-truth-attestation-lineage-invalid:{tid}",
                        code=ErrorCode.INVALID_REQUEST,
                        status=503,
                    )
                if not att.get("retirementReconciled"):
                    raise AppError(
                        f"formal-truth-attestation-retirement-unreconciled:{tid}",
                        code=ErrorCode.INVALID_REQUEST,
                        status=503,
                    )
                mut = conn.execute(
                    "SELECT generation FROM target_receipt_mutations WHERE target_id = ?",
                    (tid,),
                ).fetchone()
                # Match get_target_receipt_mutation_generation(): missing row == 0.
                current_mut = int(mut["generation"]) if mut is not None else 0
                att_gen = att.get("authorityGeneration")
                if att_gen is not None and (
                    current_gen is None or int(att_gen) != int(current_gen)
                ):
                    raise AppError(
                        f"formal-truth-attestation-stale-authority:{tid}",
                        code=ErrorCode.INVALID_REQUEST,
                        status=503,
                    )
                att_mut = att.get("sourceReceiptMutationGeneration")
                if att_mut is not None and int(att_mut) != int(current_mut):
                    raise AppError(
                        f"formal-truth-attestation-stale-mutation:{tid}",
                        code=ErrorCode.INVALID_REQUEST,
                        status=503,
                    )
    if backup_control_authority.pending_authority_outbox_count() > 0:
        raise AppError(
            "formal-truth-blocked:unresolved-authority-mutation",
            code=ErrorCode.INVALID_REQUEST,
            status=503,
        )

    set_control_recovery_state(recovery_state=STATE_ANCHORING_NEW_EPOCH, reason=reason)
    remote_floor: int | None = None
    with backup_control._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT authority_generation, authority_digest FROM control_authority_head WHERE id = 1"
        ).fetchone()
        # Prefer controlBootEpoch from last applied checkpoint tip if present in journal/outbox — use local+1.
        boot_row = conn.execute("SELECT boot_epoch FROM control_boot_state WHERE id = 1").fetchone()
        current_epoch = int(boot_row["boot_epoch"] if boot_row is not None else 1)
        remote_floor = current_epoch + 1

    prepared: dict[str, Any] | None = None
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        boot_epoch = _advance_boot_epoch(
            conn,
            reason=reason,
            recovery_state=STATE_ANCHORING_NEW_EPOCH,
            minimum_epoch=remote_floor,
        )
        if backup_control_authority.authority_anchors_configured():
            prepared = backup_control_authority.prepare_authority_mutation_in_tx(
                conn, kind="boot-epoch-activation"
            )
        else:
            # Local-only: still record head carrying new boot epoch when possible.
            try:
                ckpt = backup_control_authority._snapshot_authority_from_conn(conn)  # noqa: SLF001
                # Only advance local head when we can form next gen from existing tip.
                if row is not None or int(ckpt.get("authorityGeneration") or 0) == 1:
                    backup_control_authority.record_local_authority_head(ckpt, conn=conn)
            except AppError:
                pass
        conn.execute("COMMIT")

    if prepared is not None:
        try:
            backup_control_authority.anchor_non_rebuildable_mutation(
                kind="boot-epoch-activation",
                prepared=prepared,
                rpo_zero=True,
            )
        except AppError:
            # Keep non-ACTIVE if RPO=0 anchor fails.
            set_control_recovery_state(
                recovery_state=RECOVERY_REQUIRED,
                reason="boot-epoch-anchor-failed",
            )
            raise

    set_control_recovery_state(recovery_state=RECOVERY_ACTIVE, reason=reason)
    return {
        "status": "active",
        "bootEpoch": boot_epoch,
        "recoveryState": RECOVERY_ACTIVE,
        "reason": reason,
        "authorityHead": (
            {
                "authorityGeneration": int(row["authority_generation"]),
                "authorityDigest": str(row["authority_digest"]),
            }
            if row is not None
            else None
        ),
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
    result = {
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
    # Bind attestation to current authority tip + target mutation generation.
    authority_gen = None
    try:
        with backup_control._connect() as conn:  # noqa: SLF001
            head = conn.execute(
                "SELECT authority_generation FROM control_authority_head WHERE id = 1"
            ).fetchone()
            if head is not None:
                authority_gen = int(head["authority_generation"])
    except Exception:
        authority_gen = None
    record_formal_truth_validation(
        target_id=target_id,
        status="VALID" if state == "complete" and invalid_commits == 0 else "INVALID",
        authenticated_commit_count=authenticated,
        invalid_commit_count=invalid_commits,
        orphan_receipt_count=orphan_receipts,
        indexed_receipt_count=indexed,
        lineage_valid=(invalid_commits == 0 and authenticated == indexed),
        retirement_reconciled=(invalid_commits == 0),
        index_coverage_complete=(state == "complete"),
        source_receipt_mutation_generation=end_mutation,
        authority_generation=authority_gen,
    )
    result["formalTruthAttestation"] = get_formal_truth_validation(target_id)
    return result


def clear_formal_truth_attestations() -> None:
    """Test helper / recovery start: drop in-process attestations."""
    _FORMAL_TRUTH_ATTESTATIONS.clear()


def record_formal_truth_validation(
    *,
    target_id: str,
    status: str,
    authenticated_commit_count: int = 0,
    invalid_commit_count: int = 0,
    orphan_receipt_count: int = 0,
    indexed_receipt_count: int = 0,
    lineage_valid: bool = False,
    retirement_reconciled: bool = False,
    index_coverage_complete: bool = False,
    source_receipt_mutation_generation: int | None = None,
    authority_generation: int | None = None,
) -> dict[str, Any]:
    """Record Formal Truth validation attestation for activation gate."""
    entry = {
        "targetId": str(target_id),
        "status": str(status),
        "authenticatedCommitCount": int(authenticated_commit_count),
        "invalidCommitCount": int(invalid_commit_count),
        "orphanReceiptCount": int(orphan_receipt_count),
        "indexedReceiptCount": int(indexed_receipt_count),
        "lineageValid": bool(lineage_valid),
        "retirementReconciled": bool(retirement_reconciled),
        "indexCoverageComplete": bool(index_coverage_complete),
        "sourceReceiptMutationGeneration": source_receipt_mutation_generation,
        "authorityGeneration": int(authority_generation) if authority_generation is not None else None,
        "validatedAt": _utc_iso(),
    }
    _FORMAL_TRUTH_ATTESTATIONS[str(target_id)] = entry
    return dict(entry)


def get_formal_truth_validation(target_id: str) -> dict[str, Any] | None:
    item = _FORMAL_TRUTH_ATTESTATIONS.get(str(target_id))
    return dict(item) if item is not None else None


def list_formal_truth_validations() -> list[dict[str, Any]]:
    return [dict(v) for v in _FORMAL_TRUTH_ATTESTATIONS.values()]


def authority_health_snapshot() -> dict[str, Any]:
    """Read-only Authority health for operators (available during recovery)."""
    from deepseek_infra.infra.workspace import backup_authority_provider

    provider = backup_authority_provider.provider_status()
    boot: dict[str, Any] = {
        "bootEpoch": None,
        "recoveryState": None,
        "reason": None,
    }
    if local_control_db_present() and local_control_db_healthy():
        try:
            boot = get_control_recovery_state()
        except Exception:
            pass
    head_gen = None
    head_digest = None
    if local_control_db_present() and local_control_db_healthy():
        try:
            with backup_control._connect() as conn:  # noqa: SLF001
                row = conn.execute(
                    "SELECT authority_generation, authority_digest FROM control_authority_head WHERE id = 1"
                ).fetchone()
                if row is not None:
                    head_gen = int(row["authority_generation"])
                    head_digest = str(row["authority_digest"])
        except Exception:
            pass
    pending = 0
    try:
        if local_control_db_present() and local_control_db_healthy():
            pending = backup_control_authority.pending_authority_outbox_count()
    except Exception:
        pending = 0
    formal = {"targetCount": 0, "completeTargets": 0, "incompleteTargets": 0}
    if local_control_db_present() and local_control_db_healthy():
        try:
            with backup_control._connect() as conn:  # noqa: SLF001
                targets = conn.execute("SELECT target_id FROM control_targets").fetchall()
                formal["targetCount"] = len(targets)
                complete = 0
                for row in targets:
                    cov = conn.execute(
                        "SELECT state FROM target_index_coverage WHERE target_id = ?",
                        (str(row["target_id"]),),
                    ).fetchone()
                    if cov is not None and str(cov["state"]) == "complete":
                        complete += 1
                formal["completeTargets"] = complete
                formal["incompleteTargets"] = int(formal["targetCount"]) - complete
        except Exception:
            pass
    recovery_state = boot.get("recoveryState")
    workers = recovery_state == RECOVERY_ACTIVE and pending == 0
    return {
        "verdict": recovery_state,
        "recoveryState": recovery_state,
        "controlBootEpoch": boot.get("bootEpoch"),
        "canonicalGeneration": head_gen,
        "canonicalDigest": head_digest,
        "configuredReplicaCount": provider.get("configuredReplicaCount"),
        "resolvedReplicaCount": provider.get("resolvedReplicaCount"),
        "resolveErrors": provider.get("resolveErrors") or [],
        "minDurableReplicas": provider.get("minDurableReplicas"),
        "unresolvedMutationCount": pending,
        "formalTruth": formal,
        "workersAllowed": workers,
        "mutationsAllowed": workers,
        "providerMode": provider.get("mode"),
        "reason": boot.get("reason"),
    }


def authority_verify() -> dict[str, Any]:
    """Read-only verify: open every configured replica and check history/ancestry/durability."""
    health = authority_health_snapshot()
    issues: list[str] = []
    replica_reports: list[dict[str, Any]] = []
    roots = backup_control_authority.get_authority_anchor_roots()
    stores = backup_control_authority.get_authority_anchor_stores()
    discovered: dict[str, dict[str, Any]] = {}
    discovered.update(discover_authority_replicas([str(p) for p in roots]))
    discovered.update(discover_authority_replicas_from_stores(list(stores)))
    selected: dict[str, Any] | None = None
    cross_replica_divergent = False
    if discovered:
        try:
            selected = backup_control_authority.select_authority_heads(discovered)
        except AppError as exc:
            msg = str(exc)
            issues.append(f"divergent-or-invalid:{msg}")
            if "divergent" in msg.casefold() or "non-ancestor" in msg.casefold() or "fork" in msg.casefold():
                cross_replica_divergent = True
                issues.append("cross-replica-divergent")
            selected = None
    for rid, meta in discovered.items():
        gen = int(meta.get("generation") or 0)
        digest = str(meta.get("digest") or "")
        history = list(meta.get("history") or [])
        history_valid = True
        try:
            if history:
                backup_control_authority.verify_authority_chain(history)
        except AppError:
            history_valid = False
            issues.append(f"history-invalid:{rid}")
        lag = 0
        if selected is not None:
            lag = max(0, int(selected.get("generation") or 0) - gen)
        replica_reports.append(
            {
                "replicaId": rid,
                "reachable": True,
                "tipGeneration": gen,
                "tipDigest": digest,
                "historyValid": history_valid,
                "lagGenerations": lag,
                "repairable": lag > 0 and history_valid and selected is not None and rid in list(
                    selected.get("laggingReplicas") or []
                ),
                # Cross-replica fork marks every involved tip divergent even if local chain is valid.
                "divergent": (not history_valid) or cross_replica_divergent,
            }
        )
    configured = int(health.get("configuredReplicaCount") or 0)
    resolved = int(health.get("resolvedReplicaCount") or 0)
    if configured > resolved:
        issues.append("configured-exceeds-resolved")
    if int(health.get("formalTruth", {}).get("incompleteTargets") or 0) > 0:
        issues.append("formal-truth-incomplete")
    if int(health.get("unresolvedMutationCount") or 0) > 0:
        issues.append("unresolved-authority-mutations")
    min_durable = int(health.get("minDurableReplicas") or 1)
    reachable = sum(1 for r in replica_reports if r.get("reachable") and r.get("historyValid"))
    if configured > 0 and reachable < min_durable:
        issues.append("durability-unsatisfied")
    if cross_replica_divergent or any(r.get("divergent") for r in replica_reports):
        overall = "DIVERGENT"
    elif "durability-unsatisfied" in issues:
        overall = "DURABILITY_UNSATISFIED"
    elif configured > 0 and resolved == 0:
        overall = "UNAVAILABLE"
    elif issues:
        overall = "DEGRADED"
    else:
        overall = "HEALTHY"
    return {
        "status": "ok" if overall == "HEALTHY" else "degraded",
        "overall": overall,
        "issues": issues,
        "health": health,
        "replicas": replica_reports,
        "canonicalGeneration": (selected or {}).get("generation"),
        "canonicalDigest": (selected or {}).get("digest"),
        "readOnly": True,
    }


def authority_audit_once() -> dict[str, Any]:
    """Read-only continuous DR audit skeleton (no destructive repair)."""
    verify = authority_verify()
    health = verify.get("health") or {}
    return {
        "status": verify.get("overall") or verify.get("status"),
        "verifiedAt": _utc_iso(),
        "configuredReplicaCount": health.get("configuredReplicaCount"),
        "resolvedReplicaCount": health.get("resolvedReplicaCount"),
        "unresolvedMutations": health.get("unresolvedMutationCount"),
        "formalTruth": health.get("formalTruth"),
        "replicas": verify.get("replicas") or [],
        "issues": verify.get("issues") or [],
        "destructiveRepair": False,
    }
