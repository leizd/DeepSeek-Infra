"""Native gateway MCP resources and prompts switched off.

``POST /mcp`` with ``MCP_EXPOSE_RESOURCES=false`` and
``MCP_EXPOSE_PROMPTS=false`` is served by the prebuilt gateway.
``initialize`` keeps tools and omits the other two capabilities. List
calls return empty arrays. Read and get calls return JSON-RPC
``-32602``. A missing token is 401. Python's MCP router and message
handler are not the owners of those HTTP responses.
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
from deepseek_infra.web.routes import mcp as mcp_routes

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "mcp-expose-off-token"
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


def _call(request_id: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


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
    env["MCP_EXPOSE_RESOURCES"] = "false"
    env["MCP_EXPOSE_PROMPTS"] = "false"
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-mcp-expose-off.log"
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


def _error(body: dict[str, Any], request_id: str, message: str) -> None:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert "result" not in body
    assert body["error"]["code"] == -32602
    assert body["error"]["message"] == message
    assert body["error"]["data"] == {"code": "forbidden"}


def test_mcp_expose_off_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    initialize = _call("mcp-init-hidden", "initialize")
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, initialize)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        init_status, init_body = _post(port, initialize, TOKEN)
        assert init_status == 200
        assert init_body["jsonrpc"] == "2.0"
        assert init_body["id"] == "mcp-init-hidden"
        assert "error" not in init_body
        capabilities = init_body["result"]["capabilities"]
        assert capabilities["tools"] == {"listChanged": False}
        assert "resources" not in capabilities
        assert "prompts" not in capabilities

        listed = _call("mcp-resources-hidden", "resources/list")
        listed_status, listed_body = _post(port, listed, TOKEN)
        assert listed_status == 200
        assert listed_body["result"] == {"resources": []}

        read = _call(
            "mcp-resource-read",
            "resources/read",
            {"uri": "runtime://capabilities"},
        )
        read_status, read_body = _post(port, read, TOKEN)
        assert read_status == 200
        _error(read_body, "mcp-resource-read", "MCP resources are disabled")

        prompts = _call("mcp-prompts-hidden", "prompts/list")
        prompts_status, prompts_body = _post(port, prompts, TOKEN)
        assert prompts_status == 200
        assert prompts_body["result"] == {"prompts": []}

        prompt = _call("mcp-prompt-get", "prompts/get", {"name": "slides-outline"})
        prompt_status, prompt_body = _post(port, prompt, TOKEN)
        assert prompt_status == 200
        _error(prompt_body, "mcp-prompt-get", "MCP prompts are disabled")
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
