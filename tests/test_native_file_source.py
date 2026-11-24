"""Native gateway file source over a cache written by ``cache_file_chunks``.

A legal id returns the writer's original bytes. ``download=false`` stays
inline. ``download=1`` is an attachment. A missing id returns the expired
index error. An index whose source file is gone returns the expired original
error. Python ``cached_file_source`` is not the owner under test.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from deepseek_infra.infra.rag import files as rag_files

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
INDEX_STALE = "Uploaded file index has expired or is missing"
SOURCE_STALE = "Original uploaded file has expired or is missing"
INLINE = 'inline; filename="source.txt"; filename*=UTF-8\'\'source.txt'
ATTACHMENT = 'attachment; filename="source.txt"; filename*=UTF-8\'\'source.txt'


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gateway_source(port: int, params: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/file-source?{urlencode(params)}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            headers = {key.lower(): value for key, value in response.headers.items()}
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = {key.lower(): value for key, value in exc.headers.items()}
        payload = exc.read()
    return status, headers, payload


def _error(payload: bytes) -> dict[str, Any]:
    parsed = json.loads(payload.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_cached_file_source_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_source(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], Path]:
        raise AssertionError("python file source entered")

    assert NATIVE_GATEWAY.is_file()
    source = b"SOURCE-BYTES-MARKER\nsecond line\n"
    text = source.decode("utf-8")
    file_id = rag_files.cache_file_chunks(
        "source.txt",
        "text/plain",
        len(source),
        "text",
        text,
        rag_files.chunk_text(text),
        source_bytes=source,
        page_count=0,
    )
    cache_dir = tmp_settings / ".file-cache"
    index_path = cache_dir / f"{file_id}.json"
    source_path = cache_dir / f"{file_id}.source"
    index_bytes = index_path.read_bytes()
    assert source_path.read_bytes() == source
    assert index_bytes != source
    monkeypatch.setattr(rag_files, "cached_file_source", _refuse_python_source)
    before = sorted(path.name for path in cache_dir.iterdir())
    missing_id = "a" * 32
    assert missing_id != file_id
    port = _free_port()
    log_path = tmp_settings / "gateway-file-source.log"
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
        status, headers, body = _gateway_source(port, {"fileId": file_id})
        assert status == 200
        assert body == source
        assert headers["content-type"] == "text/plain; charset=utf-8"
        assert headers["content-disposition"] == INLINE
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["cache-control"] == "no-store"
        inline_status, inline_headers, inline_body = _gateway_source(
            port,
            {"fileId": file_id, "download": "false"},
        )
        assert inline_status == 200
        assert inline_body == source
        assert inline_headers["content-disposition"] == INLINE
        attachment_status, attachment_headers, attachment_body = _gateway_source(
            port,
            {"fileId": file_id, "download": "1"},
        )
        assert attachment_status == 200
        assert attachment_body == source
        assert attachment_headers["content-disposition"] == ATTACHMENT
        stale_status, _stale_headers, stale_body = _gateway_source(
            port,
            {"fileId": missing_id},
        )
        stale = _error(stale_body)
        assert stale_status == 410
        assert stale["error"] == INDEX_STALE
        assert stale["code"] == "file_index_expired"
        assert stale["error"] != SOURCE_STALE
        source_path.unlink()
        gone_status, _gone_headers, gone_body = _gateway_source(port, {"fileId": file_id})
        gone = _error(gone_body)
        assert gone_status == 410
        assert gone["error"] == SOURCE_STALE
        assert gone["code"] == "file_index_expired"
        assert gone["error"] != INDEX_STALE
        assert source not in gone_body
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
    assert index_path.read_bytes() == index_bytes
    assert not source_path.exists()
    assert not (cache_dir / f"{missing_id}.json").exists()
    after = sorted(path.name for path in cache_dir.iterdir())
    assert source_path.name not in after
    assert all(not name.endswith(".png") for name in after)
    assert set(before) - set(after) == {source_path.name}
