"""4.6.4: production authority verdict, mutation barrier, contiguous chains."""

from __future__ import annotations

from pathlib import Path

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
    return db


def test_verdict_genesis_when_no_local_no_remote(control_db: Path) -> None:
    control_db.unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.RECOVERY_ACTIVE
    assert verdict["allowWorkers"] is True
    assert verdict["allowMutations"] is True


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
