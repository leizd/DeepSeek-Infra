"""Targeted test coverage boosters for backup_remote_restore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_remote_restore, backups


def test_remote_restore_session_controls(tmp_settings: Path) -> None:
    # 1. Nonexistent restore session returns 404 for pause/resume/abort
    with pytest.raises(AppError):
        backup_remote_restore.request_restore_pause("nonexistent_session_1")

    with pytest.raises(AppError):
        backup_remote_restore.resume_restore_session("nonexistent_session_2")

    with pytest.raises(AppError):
        backup_remote_restore.request_restore_abort("nonexistent_session_3")

    assert backup_remote_restore.read_restore_session("nonexistent_session_4") is None

    # 2. Setup a dummy session in backups.RESTORE_DIR
    restore_id = "test_restore_ctrl"
    s_dir = backups.RESTORE_DIR / restore_id
    s_dir.mkdir(parents=True, exist_ok=True)
    s_file = s_dir / "remote-fetch.json"
    session_data = {
        "restoreId": restore_id,
        "phase": "fetching-components",
        "schemaVersion": 4,
    }
    s_file.write_text(json.dumps(session_data), encoding="utf-8")

    # 3. Request pause
    pause_res = backup_remote_restore.request_restore_pause(restore_id)
    assert pause_res["phase"] == "paused"

    # Pausing again returns current phase
    assert backup_remote_restore.request_restore_pause(restore_id)["phase"] == "paused"

    # 4. Resume session
    resume_res = backup_remote_restore.resume_restore_session(restore_id)
    assert resume_res["phase"] == "fetching-components"

    # Resuming non-paused session returns current phase
    assert backup_remote_restore.resume_restore_session(restore_id)["phase"] == "fetching-components"

    # 5. Request abort
    abort_res = backup_remote_restore.request_restore_abort(restore_id)
    assert abort_res["phase"] in {"aborted", "rolled-back", "recovery-required"}


def test_remote_restore_helpers(tmp_settings: Path) -> None:
    # 1. Assert live restore allowed for drillOnly
    dummy_dir = tmp_settings / "drill_test"
    dummy_dir.mkdir(parents=True, exist_ok=True)

    with pytest.raises(AppError) as exc_info:
        backup_remote_restore._assert_live_restore_allowed(dummy_dir, {"drillOnly": True})
    assert "Recovery Drill job cannot enter live restore" in str(exc_info.value)

    # 2. Manifest work computation
    manifest = {
        "files": [
            {"size": 100},
            {"size": 250},
            {"path": "file3.txt"},  # no size
        ]
    }
    total_bytes, count = backup_remote_restore._manifest_work(manifest)
    assert count == 3
    assert total_bytes == 350

    # 3. Normalized full manifest
    norm = backup_remote_restore._normalized_full_manifest({"entries": [{"path": "a.txt"}]})
    assert "entries" in norm

    # 4. sha256_path
    sample_file = tmp_settings / "sample.bin"
    sample_file.write_bytes(b"hello world")
    digest = backup_remote_restore._sha256_path(sample_file)
    assert len(digest) == 64
