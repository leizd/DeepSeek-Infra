from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.browser import downloads


def test_filename_from_url_decodes_and_sanitizes() -> None:
    assert downloads.filename_from_url("https://example.com/reports/My%20Report.pdf?token=x") == "My-Report.pdf"


def test_fetch_download_supports_file_urls(tmp_settings: Path) -> None:
    source = tmp_settings / "report.txt"
    source.write_text("report", encoding="utf-8")

    result = downloads.fetch_download("session-file", source.as_uri())

    assert result["filename"] == "report.txt"
    assert Path(result["path"]).read_text(encoding="utf-8") == "report"


class _Response:
    def __init__(self, data: bytes, headers: dict[str, str]) -> None:
        self.data = data
        self.headers = headers

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self.data


def test_fetch_download_rejects_declared_oversize(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "BROWSER_DOWNLOAD_MAX_BYTES", 4)
    response = _Response(b"data", {"Content-Length": "5"})

    with patch.object(downloads.urllib.request, "urlopen", return_value=response), pytest.raises(AppError) as exc_info:
        downloads.fetch_download("session-size", "https://example.com/report.bin")

    assert exc_info.value.code == ErrorCode.UPLOAD_TOO_LARGE


def test_fetch_download_rejects_streamed_oversize(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "BROWSER_DOWNLOAD_MAX_BYTES", 4)
    response = _Response(b"12345", {})

    with patch.object(downloads.urllib.request, "urlopen", return_value=response), pytest.raises(AppError) as exc_info:
        downloads.fetch_download("session-stream", "https://example.com/report.bin")

    assert exc_info.value.code == ErrorCode.UPLOAD_TOO_LARGE


def test_fetch_download_uses_content_disposition_and_unique_names(tmp_settings: Path) -> None:
    response = _Response(b"first", {"Content-Disposition": 'attachment; filename="final.txt"'})

    with patch.object(downloads.urllib.request, "urlopen", return_value=response):
        first = downloads.fetch_download("session-remote", "https://example.com/original.bin")
    with patch.object(downloads.urllib.request, "urlopen", return_value=response):
        second = downloads.fetch_download("session-remote", "https://example.com/original.bin")

    assert first["filename"] == "final.txt"
    assert second["filename"] == "final-2.txt"


def test_fetch_download_uses_url_name_without_content_disposition(tmp_settings: Path) -> None:
    response = _Response(b"report", {})

    with patch.object(downloads.urllib.request, "urlopen", return_value=response):
        result = downloads.fetch_download("session-url-name", "https://example.com/My%20Report.txt")

    assert result["filename"] == "My-Report.txt"


def test_save_download_bytes_rejects_oversize(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "BROWSER_DOWNLOAD_MAX_BYTES", 1)

    with pytest.raises(AppError) as exc_info:
        downloads.save_download_bytes("session-save", "report.txt", b"xx")

    assert exc_info.value.code == ErrorCode.UPLOAD_TOO_LARGE


def test_cleanup_downloads_handles_success_and_oserror(tmp_settings: Path) -> None:
    directory = downloads.isolated_download_dir("session-clean")
    (directory / "report.txt").write_text("report", encoding="utf-8")

    downloads.cleanup_downloads("session-clean")
    assert not directory.exists()

    with patch.object(downloads.shutil, "rmtree", side_effect=OSError("busy")):
        downloads.cleanup_downloads("session-error")
