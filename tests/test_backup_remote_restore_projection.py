"""Selection-freeze contracts for the durable remote restore session (4.4.13)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_projection, backup_remote_restore, backups


def _write_session(restore_id: str, *, selection: dict[str, object] | None = None, digest: str | None = None, phase: str = "fetched") -> None:
    payload = {
        "schemaVersion": 3,
        "restoreId": restore_id,
        "source": "remote-target",
        "targetId": "target-t",
        "backupId": "backup-b",
        "snapshotKind": "full",
        "objectDigest": "0" * 64,
        "holdKey": f"holds/{restore_id}",
        "holdKeys": [],
        "selection": selection,
        "selectionDigest": digest,
        "phase": phase,
        "ciphertextPath": str(backup_remote_restore._session_dir(restore_id) / "backup-b.age"),
    }
    backup_remote_restore._atomic_write_json(backup_remote_restore._session_path(restore_id), payload)


def _selection(*, contributors: tuple[str, ...] = ("projects",), project_ids: tuple[str, ...] = ("p1",)) -> backup_projection.RestoreSelection:
    return backup_projection.RestoreSelection(contributors=contributors, project_ids=project_ids)


def test_resume_with_matching_selection_succeeds(tmp_settings: Path) -> None:
    selection = _selection()
    digest = backup_projection.selection_digest(selection)
    _write_session("restore_match", selection=selection.canonical(), digest=digest)
    result = backup_remote_restore.create_restore_from_target(
        target_id="target-t",
        backup_id="backup-b",
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
        restore_id="restore_match",
    )
    assert result["restoreId"] == "restore_match"
    assert result["selectionDigest"] == digest
    assert result["phase"] == "fetched"


def test_resume_with_changed_selection_rejected_409(tmp_settings: Path) -> None:
    selection = _selection()
    _write_session("restore_frozen", selection=selection.canonical(), digest=backup_projection.selection_digest(selection))
    with pytest.raises(AppError) as excinfo:
        backup_remote_restore.create_restore_from_target(
            target_id="target-t",
            backup_id="backup-b",
            selection={"contributors": ["projects"], "projectIds": ["p2"]},
            restore_id="restore_frozen",
        )
    assert excinfo.value.status == 409
    assert "frozen session selection" in str(excinfo.value)
    stored = backup_remote_restore.read_restore_session("restore_frozen")
    assert stored is not None and stored["selectionDigest"] == backup_projection.selection_digest(selection)


def test_resume_unknown_restore_id_404(tmp_settings: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        backup_remote_restore.create_restore_from_target(
            target_id="target-t",
            backup_id="backup-b",
            selection={"contributors": ["projects"], "projectIds": ["p1"]},
            restore_id="restore_missing",
        )
    assert excinfo.value.status == 404


def test_first_resume_freezes_selection_digest(tmp_settings: Path) -> None:
    _write_session("restore_unfrozen", selection=None, digest=None)
    selection = _selection()
    result = backup_remote_restore.create_restore_from_target(
        target_id="target-t",
        backup_id="backup-b",
        selection={"contributors": ["projects"], "projectIds": ["p1"]},
        restore_id="restore_unfrozen",
    )
    expected = backup_projection.selection_digest(selection)
    assert result["selectionDigest"] == expected
    stored = backup_remote_restore.read_restore_session("restore_unfrozen")
    assert stored is not None
    assert stored["selectionDigest"] == expected
    assert stored["selection"] == selection.canonical()


def test_resume_without_selection_keeps_frozen_digest(tmp_settings: Path) -> None:
    selection = _selection()
    digest = backup_projection.selection_digest(selection)
    _write_session("restore_kept", selection=selection.canonical(), digest=digest)
    result = backup_remote_restore.create_restore_from_target(target_id="target-t", backup_id="backup-b", restore_id="restore_kept")
    assert result["selectionDigest"] == digest
    assert result["selection"] == selection.canonical()


def test_session_schema_version_is_three() -> None:
    assert backup_remote_restore.SESSION_SCHEMA_VERSION == 3
    assert backups.capabilities()["restoreProjection"]["projects"]["granularity"] == "project"
