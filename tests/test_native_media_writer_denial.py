"""The retained Python oracle cannot write a native-owned media library."""
from pathlib import Path

import pytest

from deepseek_infra.infra.media import library
from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError


def test_media_library_writer_denial_retains_existing_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(library, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    library._write_store([])
    before = library.store_path().read_bytes()
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    with pytest.raises(PythonWriterMechanicallyDeniedError):
        library._write_store([{"mediaId": "media_forbidden"}])
    assert library.store_path().read_bytes() == before


def test_media_writer_denial_does_not_create_a_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "absent"
    monkeypatch.setattr(library, "MEDIA_DIR", directory)
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
    with pytest.raises(PythonWriterMechanicallyDeniedError):
        library._write_store([])
    assert not directory.exists()
