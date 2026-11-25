"""Native gateway HTML filename case and whitespace refusal.

``page.HTM`` keeps kind ``htm``. A whitespace-only HTML file is refused
before a cache file appears. Python ``extract_uploaded_file`` and
``cache_file_chunks`` are not the owners under test.
"""

from __future__ import annotations

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
# The native gateway is a prebuilt Rust binary. A developer checkout and the
# Linux CI runner do not share a path, so resolve it from an explicit override,
# then the conventional build outputs, and skip the module when none exists. A
# missing binary must never be reported as a product failure.
GATEWAY_BINARY_ENV = "DEEPSEEK_GATEWAY_BINARY"
GATEWAY_BINARY_CANDIDATES = (
    ROOT / "bin" / "deepseek-gateway",
    ROOT / "bin" / "deepseek-gateway.exe",
    ROOT / "rust" / "target" / "release" / "deepseek-gateway",
    ROOT / "rust" / "target" / "release" / "deepseek-gateway.exe",
)


def _resolve_native_gateway() -> Path | None:
    override = os.environ.get(GATEWAY_BINARY_ENV, "").strip()
    candidates = (Path(override),) if override else GATEWAY_BINARY_CANDIDATES
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# Kept separate so the skip decision can test for absence while NATIVE_GATEWAY
# stays a plain Path for the call sites and the type checker.
_GATEWAY_BINARY = _resolve_native_gateway()
NATIVE_GATEWAY = _GATEWAY_BINARY or Path("deepseek-gateway-not-built")

pytestmark = pytest.mark.skipif(
    _GATEWAY_BINARY is None,
    reason=(
        "prebuilt native gateway is required; set "
        f"{GATEWAY_BINARY_ENV} or build rust/ -p deepseek-gateway --release"
    ),
)
BOUNDARY = "DeepseekBoundary"
HTML = b"<p>Hello</p><script>secret()</script>"
WHITESPACE = b"<p>   </p>"
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
    parsed = json_loads(payload)
    assert isinstance(parsed, dict)
    return status, parsed


def json_loads(payload: bytes) -> Any:
    import json

    return json.loads(payload.decode("utf-8"))


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


def test_upper_htm_upload_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_extract(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("python file text entered")

    def _refuse_python_cache(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("python file cache entered")

    monkeypatch.setattr(rag_files, "extract_uploaded_file", _refuse_python_extract)
    monkeypatch.setattr(rag_files, "cache_file_chunks", _refuse_python_cache)
    cache_dir = tmp_settings / ".file-cache"
    port = _free_port()
    log_path = tmp_settings / "gateway-file-text-htm-case.log"
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
        blank_type, blank_body = _multipart("blank.html", "text/html", WHITESPACE)
        blank_status, blank = _post(port, blank_type, blank_body)
        assert blank_status == 422
        assert blank["error"] == "No readable text found in this file"
        assert blank["code"] == "invalid_payload"
        assert _cache_names(cache_dir) == []
        content_type, body = _multipart("page.HTM", "text/html", HTML)
        status, uploaded = _post(port, content_type, body)
        assert status == 200
        assert uploaded["errors"] == []
        assert uploaded["file"]["name"] == "page.HTM"
        assert uploaded["file"]["kind"] == "htm"
        assert uploaded["file"]["kind"] != "html"
        assert uploaded["file"]["text"] == "Hello"
        assert "secret" not in uploaded["file"]["text"]
        file_id = uploaded["file"]["fileId"]
        assert isinstance(file_id, str)
        assert FILE_ID.fullmatch(file_id)
        assert (cache_dir / f"{file_id}.source").read_bytes() == HTML
        source_status, source_body = _source(port, file_id)
        assert source_status == 200
        assert source_body == HTML
        assert b"secret()" in source_body
        names = _cache_names(cache_dir)
        assert all(not name.endswith(".png") for name in names)
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
