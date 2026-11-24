"""Native gateway data_transform json_path.

``POST /mcp`` ``tools/call`` ``data_transform`` ``json_path`` is served
by the prebuilt gateway. A nested index is returned. A missing path is
``not_found``. A wildcard path is ``invalid_payload``. A missing token
is 401. Python's MCP router, message handler, and ``data_transform``
are not the owners of those HTTP responses.
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
TOKEN = "mcp-json-path-token"
DOCUMENT = '{"a": {"b": [10, 20]}}'
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


def _call(request_id: str, path: str, text: str = DOCUMENT) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "data_transform",
            "arguments": {"operation": "json_path", "input": text, "path": path},
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
    log_path = tmp_settings / "gateway-mcp-json-path.log"
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


def _value(body: dict[str, Any], request_id: str, path: str) -> Any:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert "error" not in body
    assert body["result"]["isError"] is False
    content = body["result"]["structuredContent"]
    assert content["ok"] is True
    assert content["tool"] == "data_transform"
    result = content["result"]
    assert result["operation"] == "json_path"
    assert result["path"] == path
    return result["value"]


def _assert_refused(body: dict[str, Any], request_id: str, code: str, message: str) -> None:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert "error" not in body
    assert body["result"]["isError"] is True
    content = body["result"]["structuredContent"]
    assert content["ok"] is False
    assert content["tool"] == "data_transform"
    assert content["code"] == code
    assert content["error"] == message
    assert "result" not in content


def test_mcp_json_path_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    def _refuse_python_transform(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python data_transform entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    monkeypatch.setattr(tool_runtime, "data_transform", _refuse_python_transform)
    nested = _call("mcp-nested", "$.a.b[1]")
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, nested)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        nested_status, nested_body = _post(port, nested, TOKEN)
        assert nested_status == 200
        assert _value(nested_body, "mcp-nested", "$.a.b[1]") == 20

        first = _call("mcp-first", "a.b[0]")
        first_status, first_body = _post(port, first, TOKEN)
        assert first_status == 200
        assert _value(first_body, "mcp-first", "a.b[0]") == 10

        absent = _call("mcp-absent", "$.nope", '{"a": 1}')
        absent_status, absent_body = _post(port, absent, TOKEN)
        assert absent_status == 200
        _assert_refused(absent_body, "mcp-absent", "not_found", "JSON path not found")

        wildcard = _call("mcp-wildcard", "$.a[*]", '{"a": 1}')
        wildcard_status, wildcard_body = _post(port, wildcard, TOKEN)
        assert wildcard_status == 200
        _assert_refused(wildcard_body, "mcp-wildcard", "invalid_payload", "Unsupported JSON path")
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
