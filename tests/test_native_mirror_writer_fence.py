"""A native handoff must keep the legacy mirror writer fenced after restart."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
from deepseek_infra.infra.workspace import backup_mirror


@pytest.mark.parametrize("fence_name", [".backup-mirror.native-handoff.json", ".backup-mirror.native-import.json"])
@pytest.mark.parametrize("fence_kind", ["manifest", "corrupt", "directory"])
def test_durable_handoff_fence_denies_before_creating_writer_files(
    tmp_settings: Path, monkeypatch: pytest.MonkeyPatch, fence_kind: str, fence_name: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    fence = tmp_settings / fence_name
    if fence_kind == "directory":
        fence.mkdir()
    else:
        fence.write_text('{}' if fence_kind == "manifest" else 'incomplete publication', encoding="utf-8")
    before = sorted(path.relative_to(tmp_settings).as_posix() for path in tmp_settings.rglob("*"))
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="persistently fenced"):
        backup_mirror.put_frontend_mirror("mirror_main", {}, source_epoch="epoch-1", recipients=[])
    assert sorted(path.relative_to(tmp_settings).as_posix() for path in tmp_settings.rglob("*")) == before


@pytest.mark.parametrize("fence_name", [".backup-mirror.native-handoff.json", ".backup-mirror.native-import.json"])
def test_unreadable_handoff_fence_fails_closed(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch, fence_name: str) -> None:
    fence = tmp_settings / fence_name
    original_lstat = Path.lstat

    def denied_lstat(path: Path) -> os.stat_result:
        if path == fence:
            raise PermissionError("cannot attest absence of the handoff fence")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied_lstat)
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="persistently fenced"):
        backup_mirror.put_frontend_mirror("mirror_main", {}, source_epoch="epoch-1", recipients=[])
    assert not backup_mirror.BACKUP_MIRROR_DIR.exists()


@pytest.mark.parametrize("fence_name", [".backup-mirror.native-handoff.json", ".backup-mirror.native-import.json"])
def test_restarted_python_service_cannot_bypass_persistent_fence(tmp_settings: Path, fence_name: str) -> None:
    mirror_root = tmp_settings / ".backup-mirror"
    (tmp_settings / fence_name).write_text('{}', encoding="utf-8")
    command = """
import sys
from pathlib import Path
from deepseek_infra.infra.workspace import backup_mirror
from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
backup_mirror.BACKUP_MIRROR_DIR = Path(sys.argv[1])
try:
    backup_mirror.put_frontend_mirror('mirror_main', {}, source_epoch='epoch-1', recipients=[])
except PythonWriterMechanicallyDeniedError as error:
    assert 'persistently fenced' in str(error)
    print('PYTHON_MIRROR_SOURCE_FENCED')
else:
    raise AssertionError('the restarted legacy writer was not fenced')
"""
    result = subprocess.run(
        [sys.executable, "-c", command, str(mirror_root)], capture_output=True, text=True, timeout=20,
        env={**os.environ, "PYTHONUTF8": "1", "DEEPSEEK_RUNTIME_MODE": "python_authoritative"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PYTHON_MIRROR_SOURCE_FENCED"
    assert not mirror_root.exists()
