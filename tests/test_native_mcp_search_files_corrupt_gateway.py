"""Native gateway search_files skips corrupt caches.

``POST /mcp`` ``tools/call`` ``search_files`` is served by the prebuilt
gateway. A non-JSON cache file and a JSON array are skipped. The one
real project chunk is returned. The seeded bytes stay unchanged and
``.local-rag`` is not created. A missing token is 401. Python's MCP
router, message handler, and ``search_files`` are not the owners of
those HTTP responses.
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

from deepseek_infra.infra.mcp import server as mcp_server
from deepseek_infra.infra.tool_runtime import tools as tool_runtime
from deepseek_infra.web.routes import mcp as mcp_routes

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
TOKEN = "mcp-search-corrupt-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
FILE_ID = "c" * 32
PROJECT_BODY = (
    '{"id":"file","name":"notes","kind":"text","chunks":[null,'
    '{"index":0,"text":"needle text","lineStart":1,"lineEnd":2}]}'
)
CLEARED = (
    "AUTH_TOKEN",
    "AUTH_DISABLED",
    "DEEPSEEK_RUNTIME_MODE",
    "MCP_CAPABILITY",
    "MCP_EXPOSE_RESOURCES",
    "MCP_EXPOSE_PROMPTS",
    "MCP_ENABLED",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _call() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "mcp-search-corrupt",
        "method": "tools/call",
        "params": {"name": "search_files", "arguments": {"query": "needle"}},
    }


def _post(port: int, body: dict[str, Any], token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
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


def _child_env(tmp_settings: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    for name in CLEARED:
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-mcp-search-corrupt.log"
    log_handle = log_path.open("wb")
    process = subprocess.Popen(
        [str(NATIVE_GATEWAY)],
        cwd=str(NATIVE_GATEWAY.parent),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
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
    if not listening:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
        raise AssertionError(log_path.read_text(encoding="utf-8", errors="replace"))
    return process, log_handle


def test_mcp_search_files_corrupt_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    def _refuse_python_search(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python search_files entered")

    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    monkeypatch.setattr(tool_runtime, "search_files", _refuse_python_search)
    cache = tmp_settings / ".file-cache"
    cache.mkdir()
    broken = cache / "not-json.json"
    broken.write_text("not json", encoding="utf-8")
    array_file = cache / "array.json"
    array_file.write_text("[]", encoding="utf-8")
    project_dir = tmp_settings / ".projects" / "proj" / "files"
    project_dir.mkdir(parents=True)
    project_file = project_dir / f"{FILE_ID}.json"
    project_file.write_text(PROJECT_BODY, encoding="utf-8")
    broken_bytes = broken.read_bytes()
    array_bytes = array_file.read_bytes()
    project_bytes = project_file.read_bytes()
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        body = _call()
        missing_status, missing = _post(port, body)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        status, payload = _post(port, body, TOKEN)
        assert status == 200
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] == "mcp-search-corrupt"
        assert "error" not in payload
        assert payload["result"]["isError"] is False
        content = payload["result"]["structuredContent"]
        assert content["ok"] is True
        assert content["tool"] == "search_files"
        result = content["result"]
        assert result["query"] == "needle"
        assert result["searchedFiles"] == 1
        matches = result["matches"]
        assert isinstance(matches, list)
        assert len(matches) == 1
        match = matches[0]
        assert match["fileId"] == "file"
        assert match["projectId"] == "proj"
        assert match["name"] == "notes"
        assert match["kind"] == "text"
        assert match["chunkIndex"] == 1
        assert match["lineStart"] == 1
        assert match["lineEnd"] == 2
        assert match["snippet"] == "needle text"
        assert match["retrieval"]["source"] == "json_hybrid"

        assert broken.read_bytes() == broken_bytes
        assert array_file.read_bytes() == array_bytes
        assert project_file.read_bytes() == project_bytes
        assert not (tmp_settings / ".local-rag").exists()
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
