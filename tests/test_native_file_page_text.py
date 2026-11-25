"""Native gateway page text over a cache written by ``cache_file_chunks``.

A stored page comes back as stored. A page past the end clamps to the last
stored page. A file id with no index returns the expired-index error.
Python ``file_page_text`` is not the owner under test.
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


def _gateway_page_text(port: int, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/file-page-text",
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


def test_cached_file_page_text_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_page_text(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("python page text entered")

    monkeypatch.setattr(rag_files, "file_page_text", _refuse_python_page_text)
    source = b"%PDF-page-text"
    file_id = rag_files.cache_file_chunks(
        "two-pages.pdf",
        "application/pdf",
        len(source),
        "pdf",
        "page one\npage two",
        rag_files.chunk_text("page one\npage two"),
        source_bytes=source,
        page_count=2,
        page_texts=[{"page": 1, "text": "page one"}, {"page": 2, "text": "page two"}],
    )
    cache_dir = tmp_settings / ".file-cache"
    before = sorted(path.name for path in cache_dir.iterdir())
    assert (cache_dir / f"{file_id}.source").read_bytes() == source
    missing_id = "d" * 32
    assert missing_id != file_id
    assert not (cache_dir / f"{missing_id}.json").exists()
    port = _free_port()
    log_path = tmp_settings / "gateway-page-text.log"
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
        first_status, first = _gateway_page_text(port, {"fileId": file_id, "page": 1})
        assert first_status == 200
        assert first["ok"] is True
        assert first["file"]["fileId"] == file_id
        assert first["file"]["name"] == "two-pages.pdf"
        assert first["page"]["index"] == 1
        assert first["page"]["pageCount"] == 2
        assert first["page"]["text"] == "page one"
        assert first["page"]["hasText"] is True
        assert "executionEpoch" not in first
        second_status, second = _gateway_page_text(port, {"fileId": file_id, "page": 2})
        assert second_status == 200
        assert second["page"]["index"] == 2
        assert second["page"]["pageCount"] == 2
        assert second["page"]["text"] == "page two"
        assert second["page"]["hasText"] is True
        clamped_status, clamped = _gateway_page_text(port, {"fileId": file_id, "page": 9})
        assert clamped_status == 200
        assert clamped["page"]["index"] == 2
        assert clamped["page"]["pageCount"] == 2
        assert clamped["page"]["text"] == "page two"
        assert clamped["page"]["text"] != "page one"
        stale_status, stale = _gateway_page_text(port, {"fileId": missing_id, "page": 1})
        assert stale_status == 410
        assert stale["error"] == STALE_MESSAGE
        assert stale["code"] == "file_index_expired"
        assert "page one" not in stale["error"]
        assert "page two" not in stale["error"]
        assert "matches" not in stale
        assert "executionEpoch" not in stale
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
    after = sorted(path.name for path in cache_dir.iterdir())
    assert after == before
    assert (cache_dir / f"{file_id}.source").read_bytes() == source
    assert not (cache_dir / f"{missing_id}.json").exists()
    assert all(not name.endswith(".png") for name in after)
