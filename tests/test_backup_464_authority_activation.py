"""Production authority verdict, mutation barrier, and contiguous chains."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_control_authority,
    backup_control_recovery,
)
from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore


@pytest.fixture
def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    db = root / "control.sqlite3"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", db)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    from deepseek_infra.infra.workspace import backup_authority_provider

    backup_authority_provider.reset_authority_replica_provider()
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")
    return db


def test_verdict_genesis_when_no_local_no_remote(control_db: Path) -> None:
    control_db.unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    # Local-only install (no authority replicas) remains ACTIVE for single-node.
    assert verdict["verdict"] == backup_control_recovery.RECOVERY_ACTIVE
    assert verdict["allowWorkers"] is True
    assert verdict["allowMutations"] is True
    assert verdict["reason"] in {"genesis-local-only", "genesis-empty-control-db"}


def test_verdict_recovery_when_local_missing_remote_present(control_db: Path) -> None:
    store = MemoryTargetStore()
    backup_control_authority.configure_authority_anchor_stores([store])
    backup_control.create_policy({"policyId": "p-rem", "policyRevision": 1, "enabled": True})
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.RECOVERY_REQUIRED
    assert verdict["allowWorkers"] is False
    assert verdict["allowMutations"] is False
    with pytest.raises(AppError, match="control-authority-barrier|control-recovery"):
        backup_control.create_policy({"policyId": "blocked", "policyRevision": 1, "enabled": True})


def test_barrier_blocks_formal_and_destructive_when_recovery_required(control_db: Path) -> None:
    backup_control_recovery.enter_control_recovery_required(reason="unit")
    with pytest.raises(AppError, match="barrier|recovery"):
        with backup_control.begin_formal_metadata_mutation("t1", operation_id="op"):
            pass
    with pytest.raises(AppError, match="barrier|recovery"):
        with backup_control.begin_destructive_metadata_fence("t1", operation_id="gc"):
            pass


def test_chain_rejects_generation_gap(control_db: Path) -> None:
    a = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    c = backup_control_authority.build_authority_checkpoint(
        generation=3,
        previous_digest=str(a["digest"]),
        policies=[{"policyId": "p", "policyRevision": 1}],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=7,
    )
    with pytest.raises(AppError, match="gap"):
        backup_control_authority.verify_authority_chain([a, c])


def test_workers_allowed_helper(control_db: Path) -> None:
    assert backup_control_recovery.workers_allowed_by_verdict(
        {"allowWorkers": False, "verdict": "control-recovery-required"}
    ) is False
    assert backup_control_recovery.workers_allowed_by_verdict(
        {"allowWorkers": True, "verdict": "active"}
    ) is True


def test_local_db_healthy_false_when_missing(control_db: Path) -> None:
    control_db.unlink(missing_ok=True)
    assert backup_control_recovery.local_control_db_present() is False
    assert backup_control_recovery.local_control_db_healthy() is False


def test_local_db_healthy_false_on_connect_error(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_control.schema_version()
    assert backup_control_recovery.local_control_db_healthy() is True

    def _boom() -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(backup_control, "_connect", _boom)
    assert backup_control_recovery.local_control_db_healthy() is False


def test_verdict_authority_unavailable_when_anchors_unreachable(
    control_db: Path, tmp_path: Path
) -> None:
    # Configure roots that exist but have no authority head → explicit genesis.
    empty = tmp_path / "empty-auth"
    empty.mkdir()
    backup_control_authority.configure_authority_anchor_roots([empty])
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.STATE_GENESIS_REQUIRED
    assert verdict["allowWorkers"] is False


def test_verdict_remote_discovery_apperror(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_control_authority.configure_authority_anchor_stores([MemoryTargetStore()])

    def _boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise AppError("discover-failed", code=__import__("deepseek_infra.core.errors", fromlist=["ErrorCode"]).ErrorCode.INTERNAL)

    monkeypatch.setattr(backup_control_recovery, "discover_authority_replicas_from_stores", _boom)
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.STATE_AUTHORITY_UNAVAILABLE
    assert "discover-failed" in str(verdict.get("reason") or "")


def test_verdict_pending_outbox_blocks_workers(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control.create_policy({"policyId": "p-pend", "policyRevision": 1, "enabled": True})
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority._enqueue_authority_outbox(kind="pending", checkpoint=ckpt)
    # Anchors configured but write will fail → pending remains after ensure ready fails open path
    backup_control_authority.configure_authority_anchor_roots(None)
    # Force recovery state non-active first
    backup_control_recovery.enter_control_recovery_required(reason="unit-pending")
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["allowWorkers"] is False
    assert verdict["verdict"] == backup_control_recovery.RECOVERY_REQUIRED


def test_verdict_pending_outbox_while_active(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_control.schema_version()
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.RECOVERY_ACTIVE,
        reason="test",
    )
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority._enqueue_authority_outbox(kind="pend2", checkpoint=ckpt)

    def _ready() -> dict[str, Any]:
        return {"status": "pending-without-anchors", "pending": 1, "drained": 0, "failed": 0}

    monkeypatch.setattr(backup_control, "ensure_control_authority_ready", _ready)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.RECOVERY_REQUIRED
    assert verdict["reason"] == "pending-authority-outbox"
    assert verdict["allowWorkers"] is False


def test_backup_worker_gates(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra import backup_worker

    monkeypatch.setattr(
        backup_control_recovery,
        "resolve_startup_authority_verdict",
        lambda: {"allowWorkers": False, "verdict": "control-recovery-required"},
    )
    assert backup_worker._startup_control_authority_allows_workers() is False
    monkeypatch.setenv("DEEPSEEK_BACKUP_WORKER", "embedded")
    assert backup_worker.start_embedded_worker() is None
    assert backup_worker.main() == 2

    monkeypatch.setattr(
        backup_control_recovery,
        "resolve_startup_authority_verdict",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert backup_worker._startup_control_authority_allows_workers() is False


def test_app_ensure_startup_skips_workers(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra import app as app_mod

    calls: list[str] = []

    monkeypatch.setattr(app_mod, "multipart_module", object())
    monkeypatch.setattr(app_mod, "supported_multipart_module", lambda m: True)
    monkeypatch.setattr(app_mod, "recover_interrupted_restores", lambda: {"recoveryRequired": []})

    def _keeper(**k: Any) -> None:
        calls.append("keeper")

    def _worker() -> None:
        calls.append("worker")

    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.backup_recovery_keeper.start_global_recovery_keeper",
        _keeper,
        raising=False,
    )
    # Patch import path used inside ensure_startup_dependencies
    import deepseek_infra.backup_worker as bw

    monkeypatch.setattr(bw, "start_embedded_worker", _worker)
    app_mod.ensure_startup_dependencies(authority_verdict={"allowWorkers": False})
    assert calls == []
