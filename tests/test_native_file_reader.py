"""Native gateway file reader over a cache written by ``cache_file_chunks``.

The first requested chunk is the writer's first chunk. A later start returns
the later chunk. A non-numeric start is refused. A missing index returns
the expired-index error before that start check. Python
``file_reader_window`` is not the owner under test.
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
STALE_MESSAGE = "Uploaded file index has expired or is missing"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gateway_reader(port: int, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/file-reader",
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


def test_cached_file_reader_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_reader(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("python file reader entered")

    monkeypatch.setattr(rag_files, "file_reader_window", _refuse_python_reader)
    text = ("alpha " * 1000) + "BETA-MARKER " + ("omega " * 200)
    source = text.encode("utf-8")
    file_id = rag_files.cache_file_chunks(
        "reader.txt",
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
    before = sorted(path.name for path in cache_dir.iterdir())
    assert (cache_dir / f"{file_id}.source").read_bytes() == source
    missing_id = "e" * 32
    assert missing_id != file_id
    port = _free_port()
    log_path = tmp_settings / "gateway-file-reader.log"
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
        first_status, first = _gateway_reader(
            port,
            {"fileId": file_id, "chunkStart": 1, "chunkCount": 1},
        )
        assert first_status == 200
        assert first["ok"] is True
        assert first["file"]["fileId"] == file_id
        assert first["file"]["name"] == "reader.txt"
        assert first["window"]["chunkStart"] == 1
        assert first["window"]["chunkCount"] == 1
        assert first["window"]["totalChunks"] == len(chunks)
        assert first["window"]["hasPrevious"] is False
        assert first["window"]["hasNext"] is True
        assert first["chunks"][0]["text"] == chunks[0]["text"]
        assert first["chunks"][0]["index"] == int(chunks[0]["index"]) + 1
        assert "vector" not in first["chunks"][0]
        assert "executionEpoch" not in first
        second_status, second = _gateway_reader(
            port,
            {"fileId": file_id, "chunkStart": 2, "chunkCount": 1},
        )
        assert second_status == 200
        assert second["window"]["chunkStart"] == 2
        assert second["window"]["hasPrevious"] is True
        assert second["chunks"][0]["text"] == chunks[1]["text"]
        assert "BETA-MARKER" in second["chunks"][0]["text"]
        assert second["chunks"][0]["index"] == int(chunks[1]["index"]) + 1
        refused_status, refused = _gateway_reader(
            port,
            {"fileId": file_id, "chunkStart": "bad"},
        )
        assert refused_status == 400
        assert refused["error"] == "Invalid reader start"
        assert refused["code"] == "invalid_payload"
        assert "BETA-MARKER" not in refused["error"]
        assert "chunks" not in refused
        stale_status, stale = _gateway_reader(
            port,
            {"fileId": missing_id, "chunkStart": "bad"},
        )
        assert stale_status == 410
        assert stale["error"] == STALE_MESSAGE
        assert stale["code"] == "file_index_expired"
        assert stale["error"] != "Invalid reader start"
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
    after = sorted(path.name for path in cache_dir.iterdir())
    assert after == before
    assert (cache_dir / f"{file_id}.source").read_bytes() == source
    assert not (cache_dir / f"{missing_id}.json").exists()
    assert all(not name.endswith(".png") for name in after)
