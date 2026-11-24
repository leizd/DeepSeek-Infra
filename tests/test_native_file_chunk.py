"""Native gateway file chunk over a cache written by ``cache_file_chunks``.

Chunk index 1 is the writer's first chunk. The response returns that raw
chunk, including its vector. An index past the end is ``Chunk not found``.
The index is parsed before the cache is loaded, so a missing 32-hex id with
a numeric index returns the expired-index error. Python ``load_cached_file``
is the read the Python ``/api/file-chunk`` body performs. There is no
separate ``file_chunk`` helper. That read is not the owner under test.
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

import pytest

from deepseek_infra.infra.rag import files as rag_files

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
STALE_MESSAGE = "Uploaded file index has expired or is missing"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gateway_chunk(port: int, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/file-chunk",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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


def test_cached_file_chunk_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_chunk(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("python file chunk entered")

    assert NATIVE_GATEWAY.is_file()
    text = ("alpha " * 1000) + "BETA-MARKER " + ("omega " * 200)
    source = text.encode("utf-8")
    file_id = rag_files.cache_file_chunks(
        "chunk.txt",
        "text/plain",
        len(source),
        "text",
        text,
        rag_files.chunk_text(text),
        source_bytes=source,
        page_count=0,
    )
    cache_dir = tmp_settings / ".file-cache"
    stored = json.loads((cache_dir / f"{file_id}.json").read_text(encoding="utf-8"))
    chunks = stored["chunks"]
    assert isinstance(chunks, list)
    assert len(chunks) >= 2
    assert any("BETA-MARKER" in str(chunk.get("text") or "") for chunk in chunks)
    assert "vector" in chunks[0]
    assert "vector" in chunks[1]
    monkeypatch.setattr(rag_files, "load_cached_file", _refuse_python_chunk)
    before = sorted(path.name for path in cache_dir.iterdir())
    assert (cache_dir / f"{file_id}.source").read_bytes() == source
    missing_id = "f" * 32
    assert missing_id != file_id
    port = _free_port()
    log_path = tmp_settings / "gateway-file-chunk.log"
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
        first_status, first = _gateway_chunk(port, {"fileId": file_id, "chunkIndex": 1})
        assert first_status == 200
        assert first["file"]["fileId"] == file_id
        assert first["file"]["name"] == "chunk.txt"
        assert first["file"]["kind"] == "text"
        assert first["file"]["projectId"] == ""
        assert first["chunk"] == chunks[0]
        assert first["chunk"]["text"] == chunks[0]["text"]
        assert "vector" in first["chunk"]
        assert "executionEpoch" not in first
        assert "ok" not in first
        second_status, second = _gateway_chunk(port, {"fileId": file_id, "chunkIndex": 2})
        assert second_status == 200
        assert second["chunk"] == chunks[1]
        assert second["chunk"]["text"] == chunks[1]["text"]
        assert "BETA-MARKER" in second["chunk"]["text"]
        assert "vector" in second["chunk"]
        missing_status, missing = _gateway_chunk(
            port,
            {"fileId": file_id, "chunkIndex": len(chunks) + 1},
        )
        assert missing_status == 404
        assert missing["error"] == "Chunk not found"
        assert missing["code"] == "not_found"
        assert "chunk" not in missing
        assert "BETA-MARKER" not in missing["error"]
        stale_status, stale = _gateway_chunk(port, {"fileId": missing_id, "chunkIndex": 1})
        assert stale_status == 410
        assert stale["error"] == STALE_MESSAGE
        assert stale["code"] == "file_index_expired"
        assert stale["error"] != "Chunk not found"
        assert "chunk" not in stale
        refused_status, refused = _gateway_chunk(
            port,
            {"fileId": missing_id, "chunkIndex": "bad"},
        )
        assert refused_status == 400
        assert refused["error"] == "Invalid chunk index"
        assert refused["code"] == "invalid_payload"
        assert refused["error"] != STALE_MESSAGE
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
    after = sorted(path.name for path in cache_dir.iterdir())
    assert after == before
    assert (cache_dir / f"{file_id}.source").read_bytes() == source
    assert not (cache_dir / f"{missing_id}.json").exists()
    assert all(not name.endswith(".png") for name in after)
