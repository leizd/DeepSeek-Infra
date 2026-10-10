from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_targets, backups


@pytest.fixture(autouse=True)
def _tempdir_outside_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # pytest tmp_path lives under the system temp dir, which targets reject.
    fake_temp = tmp_path / ".sys-temp"
    fake_temp.mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def _target_dir(tmp_path: Path, name: str = "usb") -> Path:
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_init_and_probe_target(tmp_settings: Path, tmp_path: Path) -> None:
    directory = _target_dir(tmp_path)
    record = backup_targets.init_target(directory, label="USB SSD")
    assert record["targetId"].startswith("target_")
    marker = json.loads((directory / backup_targets.TARGET_MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["targetId"] == record["targetId"]
    assert marker["targetNonce"] == record["targetNonce"]
    assert marker["ownerInstallationId"] == backup_targets.installation_id()
    probe = backup_targets.probe_target(record["targetId"])
    assert probe["ready"] is True
    assert backup_targets.verify_target_ready(record["targetId"]) == directory.resolve()


def test_init_existing_marker_returns_same_target(tmp_settings: Path, tmp_path: Path) -> None:
    directory = _target_dir(tmp_path)
    first = backup_targets.init_target(directory)
    second = backup_targets.init_target(directory)
    assert first["targetId"] == second["targetId"]
    assert len(backup_targets.list_targets()) == 1


def test_marker_swap_blocks_writes(tmp_settings: Path, tmp_path: Path) -> None:
    directory = _target_dir(tmp_path)
    record = backup_targets.init_target(directory)
    marker = directory / backup_targets.TARGET_MARKER_NAME
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["targetNonce"] = "0" * 32
    marker.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AppError, match="blocked-target-unavailable"):
        backup_targets.verify_target_ready(record["targetId"])
    probe = backup_targets.probe_target(record["targetId"])
    assert probe["ready"] is False
    assert probe["status"] == "blocked-target-unavailable"


def test_missing_marker_blocks(tmp_settings: Path, tmp_path: Path) -> None:
    directory = _target_dir(tmp_path)
    record = backup_targets.init_target(directory)
    (directory / backup_targets.TARGET_MARKER_NAME).unlink()
    with pytest.raises(AppError, match="marker is missing"):
        backup_targets.verify_target_ready(record["targetId"])


def test_target_inside_runtime_root_rejected(tmp_settings: Path, tmp_path: Path) -> None:
    inside = config.ROOT / "external-disk"
    inside.mkdir(parents=True, exist_ok=True)
    with pytest.raises(AppError, match="runtime root"):
        backup_targets.init_target(inside)


def test_target_inside_restore_staging_and_backup_dir_rejected(tmp_settings: Path, tmp_path: Path) -> None:
    for base in (backups.RESTORE_DIR, backups.BACKUP_DIR):
        base.mkdir(parents=True, exist_ok=True)
        with pytest.raises(AppError):
            backup_targets.init_target(base)


def test_target_inside_temp_dir_rejected(tmp_settings: Path, tmp_path: Path) -> None:
    inside = tmp_path / ".sys-temp" / "disk"
    inside.mkdir()
    with pytest.raises(AppError, match="temporary"):
        backup_targets.init_target(inside)


def test_target_inside_contributor_root_rejected(tmp_settings: Path, tmp_path: Path) -> None:
    inside = config.PROJECTS_DIR / "disk"
    inside.mkdir(parents=True)
    with pytest.raises(AppError, match="contributor root"):
        backup_targets.init_target(inside)


def test_overlapping_targets_rejected(tmp_settings: Path, tmp_path: Path) -> None:
    parent = _target_dir(tmp_path, "parent")
    backup_targets.init_target(parent)
    child = parent / "child"
    child.mkdir()
    with pytest.raises(AppError, match="overlaps"):
        backup_targets.init_target(child)
    sibling = _target_dir(tmp_path, "sibling")
    record = backup_targets.init_target(sibling)
    assert record["targetId"]


def test_relative_and_missing_paths_rejected(tmp_settings: Path, tmp_path: Path) -> None:
    with pytest.raises(AppError, match="absolute"):
        backup_targets.init_target(Path("relative/dir"))
    with pytest.raises(AppError, match="does not exist"):
        backup_targets.init_target(tmp_path / "missing")


def test_symlink_target_rejected(tmp_settings: Path, tmp_path: Path) -> None:
    real = _target_dir(tmp_path, "real")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as symlink_error:
        if os.name != "nt":
            raise
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(real)],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        assert completed.returncode == 0, f"junction creation failed after symlink denial: {symlink_error}; {completed.stdout}"
    try:
        with pytest.raises(AppError, match="symlink|reparse"):
            backup_targets.init_target(link)
    finally:
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            link.rmdir()
    assert real.is_dir()


def test_get_list_delete_target(tmp_settings: Path, tmp_path: Path) -> None:
    directory = _target_dir(tmp_path)
    record = backup_targets.init_target(directory)
    loaded = backup_targets.get_target(record["targetId"])
    assert loaded["path"] == str(directory.resolve())
    assert [item["targetId"] for item in backup_targets.list_targets()] == [record["targetId"]]
    with pytest.raises(AppError) as missing:
        backup_targets.get_target("target_missing")
    assert missing.value.status == 404
    result = backup_targets.delete_target(record["targetId"])
    assert result == {"deleted": True, "targetId": record["targetId"]}
    assert backup_targets.list_targets() == []


def test_installation_id_is_stable(tmp_settings: Path) -> None:
    first = backup_targets.installation_id()
    second = backup_targets.installation_id()
    assert first == second
    assert first.startswith("inst_")


def test_unreadable_marker_rejected(tmp_settings: Path, tmp_path: Path) -> None:
    directory = _target_dir(tmp_path)
    (directory / backup_targets.TARGET_MARKER_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(AppError, match="unreadable"):
        backup_targets.init_target(directory)
    (directory / backup_targets.TARGET_MARKER_NAME).write_text('{"schemaVersion":1}', encoding="utf-8")
    with pytest.raises(AppError, match="invalid"):
        backup_targets.init_target(directory)
