"""Native gateway search_files on an empty root.

``POST /mcp`` ``tools/call`` ``search_files`` is served by the prebuilt
gateway. A blank query is refused. A non-empty query on a root with no
cache returns no matches and does not create the search directories.
A missing token is 401. Python's MCP router, message handler, and
``search_files`` are not the owners of those HTTP responses.
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
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "mcp-search-files-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
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


def _call(rpc_id: str, query: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": "search_files", "arguments": {"query": query}},
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
    log_path = tmp_settings / "gateway-mcp-search-files.log"
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


def test_mcp_search_files_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    def _refuse_python_search(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python search_files entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    monkeypatch.setattr(tool_runtime, "search_files", _refuse_python_search)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        blank = _call("mcp-search-files-blank", "   ")
        missing_status, missing = _post(port, blank)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        status, payload = _post(port, blank, TOKEN)
        assert status == 200
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] == "mcp-search-files-blank"
        assert "error" not in payload
        assert payload["result"]["isError"] is True
        content = payload["result"]["structuredContent"]
        assert content["ok"] is False
        assert content["tool"] == "search_files"
        assert content["code"] == "invalid_payload"
        assert content["error"] == "search_files query is empty"
        assert "result" not in content

        status, payload = _post(port, _call("mcp-search-files-empty", "  sky  "), TOKEN)
        assert status == 200
        assert payload["jsonrpc"] == "2.0"
        assert payload["id"] == "mcp-search-files-empty"
        assert "error" not in payload
        assert payload["result"]["isError"] is False
        content = payload["result"]["structuredContent"]
        assert content["ok"] is True
        assert content["tool"] == "search_files"
        result = content["result"]
        assert result["query"] == "sky"
        assert result["matches"] == []
        assert result["searchedFiles"] == 0
        assert not (tmp_settings / ".file-cache").exists()
        assert not (tmp_settings / ".projects").exists()
        assert not (tmp_settings / ".local-rag").exists()
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
