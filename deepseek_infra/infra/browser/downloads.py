"""Isolated Browser download storage."""

from __future__ import annotations

import mimetypes
import shutil
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace.schema import safe_filename

EXECUTABLE_SUFFIXES = {".bat", ".cmd", ".com", ".exe", ".msi", ".ps1", ".scr", ".sh"}


def isolated_download_dir(session_id: str) -> Path:
    safe = safe_filename(session_id, default="browser")
    directory = config.BROWSER_DOWNLOADS_DIR / safe
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def is_executable_filename(value: str) -> bool:
    return Path(str(value or "")).suffix.lower() in EXECUTABLE_SUFFIXES


def filename_from_url(url: str) -> str:
    path = unquote(urlsplit(str(url or "")).path)
    name = Path(path).name
    return safe_filename(name, default="download.bin")


def fetch_download(session_id: str, url: str) -> dict[str, Any]:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme == "file":
        source = Path(urllib.request.url2pathname(parsed.path))
        data = source.read_bytes()
        filename = source.name or "download.bin"
        return save_download_bytes(session_id, filename, data, source_url=url)
    with urllib.request.urlopen(url, timeout=30) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > config.BROWSER_DOWNLOAD_MAX_BYTES:
            raise AppError("Browser download exceeds the configured byte limit", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
        data = response.read(config.BROWSER_DOWNLOAD_MAX_BYTES + 1)
        if len(data) > config.BROWSER_DOWNLOAD_MAX_BYTES:
            raise AppError("Browser download exceeds the configured byte limit", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
        filename = filename_from_url(url)
        disposition = response.headers.get("Content-Disposition", "")
        if "filename=" in disposition:
            filename = safe_filename(disposition.rsplit("filename=", 1)[-1].strip("\"' "), default=filename)
    return save_download_bytes(session_id, filename, data, source_url=url)


def save_download_bytes(session_id: str, filename: str, data: bytes, *, source_url: str = "") -> dict[str, Any]:
    raw = data if isinstance(data, bytes) else b""
    if len(raw) > config.BROWSER_DOWNLOAD_MAX_BYTES:
        raise AppError("Browser download exceeds the configured byte limit", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
    safe_name = safe_filename(filename, default="download.bin")
    directory = isolated_download_dir(session_id)
    target = unique_download_path(directory, safe_name)
    target.write_bytes(raw)
    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return {
        "filename": target.name,
        "path": str(target),
        "bytes": len(raw),
        "mimeType": mime_type,
        "sourceUrl": source_url,
        "isolated": True,
        "executable": is_executable_filename(target.name),
    }


def unique_download_path(directory: Path, filename: str) -> Path:
    stem = Path(filename).stem or "download"
    suffix = Path(filename).suffix or ".bin"
    candidate = directory / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def cleanup_downloads(session_id: str) -> None:
    try:
        shutil.rmtree(isolated_download_dir(session_id), ignore_errors=True)
    except OSError:
        pass
