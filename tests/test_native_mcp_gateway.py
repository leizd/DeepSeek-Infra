"""Native gateway MCP hub success and refusal.

``POST /mcp`` is served by the prebuilt gateway. A missing or wrong
token is 401. The bearer token initializes the hub, answers ping, lists
``python_eval``, evaluates ``2+2`` inside that process, and refuses a
``tools/call`` that has no name. Python's MCP router, message handler,
and ``python_eval`` are not the owners of those HTTP responses.
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
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
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
TOKEN = "mcp-gateway-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": "mcp-init",
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "native-gateway-test", "version": "1"},
    },
}
PING = {"jsonrpc": "2.0", "id": "mcp-ping", "method": "ping"}
LIST = {"jsonrpc": "2.0", "id": "mcp-list", "method": "tools/list"}
EVAL = {
    "jsonrpc": "2.0",
    "id": "mcp-eval",
    "method": "tools/call",
    "params": {"name": "python_eval", "arguments": {"expression": "2+2"}},
}
MISSING = {"jsonrpc": "2.0", "id": "mcp-missing", "method": "tools/call", "params": {}}
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
    log_path = tmp_settings / "gateway-mcp.log"
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


def test_mcp_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    def _refuse_python_eval(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python python_eval entered")

    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    monkeypatch.setattr(tool_runtime, "python_eval", _refuse_python_eval)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, PING)
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        wrong_status, wrong = _post(port, PING, "not-the-mcp-token")
        assert wrong_status == 401
        assert wrong == UNAUTHORIZED

        init_status, init = _post(port, INITIALIZE, TOKEN)
        assert init_status == 200
        assert init["jsonrpc"] == "2.0"
        assert init["id"] == "mcp-init"
        assert "error" not in init
        assert init["result"]["protocolVersion"] == "2025-06-18"
        assert init["result"]["serverInfo"]["name"] == "deepseek-infra"
        assert init["result"]["serverInfo"]["version"] == VERSION
        assert "Tool Hub" in init["result"]["instructions"]

        ping_status, ping = _post(port, PING, TOKEN)
        assert ping_status == 200
        assert ping["jsonrpc"] == "2.0"
        assert ping["id"] == "mcp-ping"
        assert ping["result"] == {}
        assert "error" not in ping

        list_status, listed = _post(port, LIST, TOKEN)
        assert list_status == 200
        assert listed["jsonrpc"] == "2.0"
        assert listed["id"] == "mcp-list"
        assert "error" not in listed
        names = [tool["name"] for tool in listed["result"]["tools"]]
        assert "python_eval" in names
        assert "web_search" in names
        assert "create_pptx" in names

        eval_status, evaluated = _post(port, EVAL, TOKEN)
        assert eval_status == 200
        assert evaluated["jsonrpc"] == "2.0"
        assert evaluated["id"] == "mcp-eval"
        assert "error" not in evaluated
        assert evaluated["result"]["isError"] is False
        assert evaluated["result"]["structuredContent"]["result"]["result"] == "4"

        missing_name_status, missing_name = _post(port, MISSING, TOKEN)
        assert missing_name_status == 200
        assert missing_name["jsonrpc"] == "2.0"
        assert missing_name["id"] == "mcp-missing"
        assert missing_name["error"]["code"] == -32602
        assert missing_name["error"]["message"] == "name is required"
        assert "result" not in missing_name
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
