"""Native gateway HTML upload through ``POST /api/file-text``.

Readable HTML returns the visible text and keeps the script in the stored
source. ``page.htm`` uses kind ``htm``. A script-only upload is refused
before a cache file appears. Python ``extract_uploaded_file`` and
``cache_file_chunks`` are not the owners under test.
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
HTML = b"<p>Hello</p><script>secret()</script>"
SCRIPT_ONLY = b"<script>secret()</script>"
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


def test_html_upload_uses_the_native_gateway(
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
    log_path = tmp_settings / "gateway-file-text-html.log"
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
        script_type, script_body = _multipart("page.html", "text/html", SCRIPT_ONLY)
        script_status, script = _post(port, script_type, script_body)
        assert script_status == 422
        assert script["error"] == "No readable text found in this file"
        assert script["code"] == "invalid_payload"
        assert "secret" not in script["error"]
        assert _cache_names(cache_dir) == []
        html_type, html_body = _multipart("page.html", "text/html", HTML)
        html_status, html = _post(port, html_type, html_body)
        assert html_status == 200
        assert html["errors"] == []
        assert html["file"]["name"] == "page.html"
        assert html["file"]["kind"] == "html"
        assert html["file"]["text"] == "Hello"
        assert html["file"]["preview"] == "Hello"
        assert "secret" not in html["file"]["text"]
        assert html["files"][0]["text"] == "Hello"
        html_id = html["file"]["fileId"]
        assert isinstance(html_id, str)
        assert FILE_ID.fullmatch(html_id)
        assert (cache_dir / f"{html_id}.source").read_bytes() == HTML
        html_source_status, html_source = _source(port, html_id)
        assert html_source_status == 200
        assert html_source == HTML
        assert b"secret()" in html_source
        htm_type, htm_body = _multipart("page.htm", "text/html", HTML)
        htm_status, htm = _post(port, htm_type, htm_body)
        assert htm_status == 200
        assert htm["file"]["name"] == "page.htm"
        assert htm["file"]["kind"] == "htm"
        assert htm["file"]["text"] == "Hello"
        assert "secret" not in htm["file"]["text"]
        htm_id = htm["file"]["fileId"]
        assert isinstance(htm_id, str)
        assert FILE_ID.fullmatch(htm_id)
        assert htm_id != html_id
        assert (cache_dir / f"{htm_id}.source").read_bytes() == HTML
        htm_source_status, htm_source = _source(port, htm_id)
        assert htm_source_status == 200
        assert htm_source == HTML
        names = _cache_names(cache_dir)
        assert all(not name.endswith(".png") for name in names)
        assert "secret" not in json.dumps(html["file"]["text"])
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
