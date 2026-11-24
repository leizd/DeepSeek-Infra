"""Native gateway list_reminders read.

``POST /mcp`` ``tools/call`` ``list_reminders`` is served by the prebuilt
gateway. It reads a seeded ``.reminders/reminders.json`` and filters by
status. An unknown status becomes ``active``. A missing token is 401.
The file bytes stay unchanged. Python's MCP router, message handler,
and ``list_reminders_tool`` are not the owners of those HTTP responses.
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
TOKEN = "mcp-reminders-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
SEEDED = '[{"id": "n1", "notified": true}, {"id": "a1", "notified": false}]'
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


def _call(request_id: str, status: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "list_reminders",
            "arguments": {"status": status},
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
    log_path = tmp_settings / "gateway-mcp-reminders.log"
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


def _result(body: dict[str, Any], request_id: str) -> dict[str, Any]:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert "error" not in body
    assert body["result"]["isError"] is False
    content = body["result"]["structuredContent"]
    assert content["ok"] is True
    assert content["tool"] == "list_reminders"
    result = content["result"]
    assert isinstance(result, dict)
    return result


def test_mcp_list_reminders_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    def _refuse_python_list(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python list_reminders entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    monkeypatch.setattr(tool_runtime, "list_reminders_tool", _refuse_python_list)
    store = tmp_settings / ".reminders"
    store.mkdir()
    reminder_file = store / "reminders.json"
    reminder_file.write_text(SEEDED, encoding="utf-8")
    seeded = reminder_file.read_bytes()
    active = _call("mcp-reminders-active", "active")
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, active)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        active_status, active_body = _post(port, active, TOKEN)
        assert active_status == 200
        active_result = _result(active_body, "mcp-reminders-active")
        assert active_result["status"] == "active"
        assert active_result["count"] == 1
        assert active_result["reminders"][0]["id"] == "a1"

        notified = _call("mcp-reminders-notified", "  NOTIFIED  ")
        notified_status, notified_body = _post(port, notified, TOKEN)
        assert notified_status == 200
        notified_result = _result(notified_body, "mcp-reminders-notified")
        assert notified_result["status"] == "notified"
        assert notified_result["count"] == 1
        assert notified_result["reminders"][0]["id"] == "n1"

        bogus = _call("mcp-reminders-bogus", "bogus")
        bogus_status, bogus_body = _post(port, bogus, TOKEN)
        assert bogus_status == 200
        bogus_result = _result(bogus_body, "mcp-reminders-bogus")
        assert bogus_result["status"] == "active"
        assert bogus_result["count"] == 1

        everything = _call("mcp-reminders-all", "all")
        all_status, all_body = _post(port, everything, TOKEN)
        assert all_status == 200
        all_result = _result(all_body, "mcp-reminders-all")
        assert all_result["status"] == "all"
        assert all_result["count"] == 2
        assert reminder_file.read_bytes() == seeded
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
