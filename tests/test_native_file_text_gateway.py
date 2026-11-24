"""Native gateway plain-text upload through ``POST /api/file-text``.

A text file is extracted, cached, and readable again from
``GET /api/file-source``. An empty file and a non-multipart body are
refused before any cache file appears. Python ``extract_uploaded_file``
and ``cache_file_chunks`` are not the owners under test.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.rag import files as rag_files

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
BOUNDARY = "DeepseekBoundary"
TEXT = b"hello\nworld"
FILE_ID = re.compile(r"[0-9a-f]{32}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _multipart(filename: str, content_type: str, data: bytes) -> tuple[str, bytes]:
    head = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{BOUNDARY}--\r\n".encode("utf-8")
    return f"multipart/form-data; boundary={BOUNDARY}", head + data + tail


def _post(port: int, content_type: str, body: bytes) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/file-text",
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read()
    parsed = json.loads(payload.decode("utf-8"))
    assert isinstance(parsed, dict)
    return status, parsed


def _source(port: int, file_id: str) -> tuple[int, bytes]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/file-source?fileId={file_id}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _cache_names(cache_dir: Path) -> list[str]:
    if not cache_dir.exists():
        return []
    return sorted(path.name for path in cache_dir.iterdir())


def test_plain_text_upload_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_extract(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("python file text entered")

    def _refuse_python_cache(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("python file cache entered")

    monkeypatch.setattr(rag_files, "extract_uploaded_file", _refuse_python_extract)
    monkeypatch.setattr(rag_files, "cache_file_chunks", _refuse_python_cache)
    assert NATIVE_GATEWAY.is_file()
    cache_dir = tmp_settings / ".file-cache"
    port = _free_port()
    log_path = tmp_settings / "gateway-file-text.log"
    log_handle = log_path.open("wb")
    env = os.environ.copy()
    env["AUTH_DISABLED"] = "1"
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env.pop("AUTH_TOKEN", None)
    process = subprocess.Popen(
        [str(NATIVE_GATEWAY)],
        cwd=str(NATIVE_GATEWAY.parent),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 20
        listening = False
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    listening = True
                    break
            except OSError:
                time.sleep(0.1)
        assert listening, log_path.read_text(encoding="utf-8", errors="replace")
        wrong_status, wrong = _post(port, "application/json", b'{"x":1}')
        assert wrong_status == 400
        assert wrong["error"] == "Expected multipart/form-data"
        assert wrong["code"] == "invalid_payload"
        assert _cache_names(cache_dir) == []
        empty_type, empty_body = _multipart("empty.txt", "text/plain", b"")
        empty_status, empty = _post(port, empty_type, empty_body)
        assert empty_status == 400
        assert empty["error"] == "Uploaded file is empty: empty.txt"
        assert empty["code"] == "invalid_payload"
        assert _cache_names(cache_dir) == []
        content_type, body = _multipart("notes.txt", "text/plain", TEXT)
        status, uploaded = _post(port, content_type, body)
        assert status == 200
        assert uploaded["errors"] == []
        uploaded_file = uploaded["file"]
        assert uploaded_file["name"] == "notes.txt"
        assert uploaded_file["kind"] == "txt"
        assert uploaded_file["text"] == "hello\nworld"
        assert uploaded_file["preview"] == "hello\nworld"
        assert uploaded_file["charCount"] == 11
        assert uploaded_file["chunkCount"] == 1
        assert uploaded_file["chunked"] is False
        assert uploaded_file["sourceAvailable"] is True
        assert uploaded["files"][0]["text"] == "hello\nworld"
        file_id = uploaded_file["fileId"]
        assert isinstance(file_id, str)
        assert FILE_ID.fullmatch(file_id)
        assert "executionEpoch" not in uploaded
        names = _cache_names(cache_dir)
        assert f"{file_id}.json" in names
        assert f"{file_id}.source" in names
        assert (cache_dir / f"{file_id}.source").read_bytes() == TEXT
        source_status, source_body = _source(port, file_id)
        assert source_status == 200
        assert source_body == TEXT
        assert all(not name.endswith(".png") for name in names)
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
