"""Native gateway search_files trimmed integer-string limit.

``POST /mcp`` ``tools/call`` ``search_files`` is served by the prebuilt
gateway. Eleven matching global caches and string ``limit`` ``"  7  "``
trim to seven names. ``searchedFiles`` stays 11. The seeded bytes stay
unchanged and ``.local-rag`` is not created. A missing token is 401.
Python's MCP router, message handler, and ``search_files`` are not the
owners of those HTTP responses.
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
TOKEN = "mcp-search-padded-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
EXPECTED_NAMES = [f"n{index:02d}" for index in range(7)]
CLEARED = (
    "AUTH_TOKEN",
    "AUTH_DISABLED",
    "DEEPSEEK_RUNTIME_MODE",
    "MCP_CAPABILITY",
    "MCP_EXPOSE_RESOURCES",
    "MCP_EXPOSE_PROMPTS",
    "MCP_ENABLED",
)


def _body(index: int) -> str:
    name = f"n{index:02d}"
    file_id = f"{index:032x}"
    return (
        '{"id":"' + file_id + '","name":"' + name + '","kind":"text",'
        '"chunks":[{"index":0,"text":"needle","lineStart":1,"lineEnd":1}]}'
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _call() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "mcp-search-padded",
        "method": "tools/call",
        "params": {
            "name": "search_files",
            "arguments": {"query": "needle", "limit": "  7  "},
        },
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
    log_path = tmp_settings / "gateway-mcp-search-padded.log"
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


def test_mcp_search_files_padded_limit_uses_the_native_gateway(
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
    cache = tmp_settings / ".file-cache"
    cache.mkdir()
    seeded: dict[Path, bytes] = {}
    for index in range(11):
        path = cache / f"{index:032x}.json"
        path.write_text(_body(index), encoding="utf-8")
        seeded[path] = path.read_bytes()
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
        assert payload["id"] == "mcp-search-padded"
        assert "error" not in payload
        assert payload["result"]["isError"] is False
        content = payload["result"]["structuredContent"]
        assert content["ok"] is True
        assert content["tool"] == "search_files"
        result = content["result"]
        assert result["query"] == "needle"
        assert result["searchedFiles"] == 11
        matches = result["matches"]
        assert isinstance(matches, list)
        assert [item["name"] for item in matches] == EXPECTED_NAMES
        for item in matches:
            assert item["projectId"] == ""
            assert item["kind"] == "text"
            assert item["chunkIndex"] == 1
            assert item["snippet"] == "needle"
            assert item["retrieval"]["source"] == "json_hybrid"
        for path, raw in seeded.items():
            assert path.read_bytes() == raw
        assert not (tmp_settings / ".local-rag").exists()
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
