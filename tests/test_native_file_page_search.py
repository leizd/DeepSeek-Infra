"""Native gateway search over a cache written by ``cache_file_chunks``.

A blank query is refused before page text is scanned. A real query returns
the matches from the prebuilt gateway. Python ``file_page_search`` is not
the owner under test.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gateway_search(port: int, file_id: str, query: str) -> tuple[int, dict[str, Any]]:
    encoded = urllib.parse.urlencode({"fileId": file_id, "query": query})
    url = f"http://127.0.0.1:{port}/api/file-page-search?{encoded}"
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read()
    body = json.loads(payload.decode("utf-8"))
    assert isinstance(body, dict)
    return status, body


def test_cached_file_page_search_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_search(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("python search entered")

    monkeypatch.setattr(rag_files, "file_page_search", _refuse_python_search)
    source = b"%PDF-search"
    file_id = rag_files.cache_file_chunks(
        "search.pdf",
        "application/pdf",
        len(source),
        "pdf",
        "alpha beta\nsecond beta",
        rag_files.chunk_text("alpha beta\nsecond beta"),
        source_bytes=source,
        page_count=2,
        page_texts=[
            {"page": 1, "text": "alpha beta"},
            {"page": 2, "text": "second beta See Straße and Page"},
        ],
    )
    cache_dir = tmp_settings / ".file-cache"
    before = sorted(path.name for path in cache_dir.iterdir())
    assert (cache_dir / f"{file_id}.source").read_bytes() == source
    port = _free_port()
    log_path = tmp_settings / "gateway-search.log"
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
        for query in ("", "   "):
            status, body = _gateway_search(port, file_id, query)
            assert status == 400
            assert body["error"] == "Search query is required"
            assert body["code"] == "invalid_payload"
            assert "matches" not in body
            assert "beta" not in body["error"]
            assert "Straße" not in body["error"]
            assert "executionEpoch" not in body
        found_status, found = _gateway_search(port, file_id, "beta")
        assert found_status == 200
        assert found["ok"] is True
        assert found["query"] == "beta"
        assert found["pageCount"] == 2
        assert found["truncated"] is False
        assert found["file"]["fileId"] == file_id
        matches = found["matches"]
        assert isinstance(matches, list)
        assert [item["page"] for item in matches] == [1, 2]
        assert [item["text"] for item in matches] == ["beta", "beta"]
        assert "beta" in matches[0]["snippet"]
        assert "beta" in matches[1]["snippet"]
        folded_status, folded = _gateway_search(port, file_id, "strasse")
        assert folded_status == 200
        assert folded["query"] == "strasse"
        assert folded["truncated"] is False
        folded_matches = folded["matches"]
        assert isinstance(folded_matches, list)
        assert folded_matches
        assert folded_matches[0]["page"] == 2
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
    after = sorted(path.name for path in cache_dir.iterdir())
    assert after == before
    assert (cache_dir / f"{file_id}.source").read_bytes() == source
    assert all(not name.endswith(".png") for name in after)
