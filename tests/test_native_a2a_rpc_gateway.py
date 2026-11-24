"""Native gateway A2A JSON-RPC refusal.

``POST /a2a`` is served by the prebuilt gateway. Auth is checked before
the mesh flag. A missing or wrong token is 401. ``A2A_ENABLED=false``
with the bearer token is 403. A ``message/send`` with no text parts is
HTTP 200 and JSON-RPC ``-32602``. Python's A2A router and message
handler are not the owners of those HTTP responses. This does not send
a non-empty text part and does not prove a successful task.
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

from deepseek_infra.infra.agent_runtime import a2a as a2a_runtime
from deepseek_infra.web.routes import a2a as a2a_routes

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "a2a-rpc-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
DISABLED = {"error": {"code": "forbidden", "message": "A2A mesh is disabled"}}
SEND = {"jsonrpc": "2.0", "id": "a2a-1", "method": "message/send"}
EMPTY_PARTS = {
    "jsonrpc": "2.0",
    "id": "a2a-empty",
    "method": "message/send",
    "params": {"message": {"parts": []}},
}
CLEARED = (
    "AUTH_TOKEN",
    "AUTH_DISABLED",
    "DEEPSEEK_RUNTIME_MODE",
    "A2A_ENABLED",
    "DEEPSEEK_A2A_CONTROL_URL",
    "DEEPSEEK_A2A_TLS_CA",
    "DEEPSEEK_A2A_TLS_SERVER_NAME",
    "DEEPSEEK_A2A_TLS_CERT",
    "DEEPSEEK_A2A_TLS_KEY",
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
        f"http://127.0.0.1:{port}/a2a",
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


def _child_env(tmp_settings: Path, port: int, *, enabled: str | None) -> dict[str, str]:
    env = os.environ.copy()
    for name in CLEARED:
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    if enabled is not None:
        env["A2A_ENABLED"] = enabled
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-a2a-rpc.log"
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


def _assert_invalid_text(body: dict[str, Any], request_id: str) -> None:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert body["error"]["code"] == -32602
    assert body["error"]["message"] == "message.parts must contain non-empty text"
    assert "result" not in body


def test_a2a_rpc_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python a2a router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python a2a message entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(a2a_routes, "create_a2a_router", _refuse_python_router)
    monkeypatch.setattr(a2a_runtime, "handle_a2a_message", _refuse_python_message)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port, enabled=None), port)
    try:
        missing_status, missing = _post(port, SEND)
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        wrong_status, wrong = _post(port, SEND, "not-the-a2a-token")
        assert wrong_status == 401
        assert wrong == UNAUTHORIZED
        empty_status, empty = _post(port, SEND, TOKEN)
        assert empty_status == 200
        _assert_invalid_text(empty, "a2a-1")
        parts_status, parts = _post(port, EMPTY_PARTS, TOKEN)
        assert parts_status == 200
        _assert_invalid_text(parts, "a2a-empty")
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()

    disabled_port = _free_port()
    disabled = _serve(tmp_settings, _child_env(tmp_settings, disabled_port, enabled="false"), disabled_port)
    try:
        locked_status, locked = _post(disabled_port, SEND)
        assert locked_status == 401
        assert locked == UNAUTHORIZED
        refused_status, refused = _post(disabled_port, SEND, TOKEN)
        assert refused_status == 403
        assert refused == DISABLED
    finally:
        disabled[0].kill()
        disabled[0].wait(timeout=10)
        disabled[1].close()
