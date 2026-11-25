"""Native gateway A2A task reads.

``POST /a2a`` task methods are served by the prebuilt gateway. Auth is
checked before the mesh flag. ``tasks/list`` on a fresh process returns
an empty task list. ``tasks/get`` and ``tasks/cancel`` with no id are
JSON-RPC ``-32602``. An unknown task id is ``-32001``. Python's A2A
router and message handler are not the owners of those HTTP responses.
This does not send a text part and does not prove ``message/send``.
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
TOKEN = "a2a-tasks-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
DISABLED = {"error": {"code": "forbidden", "message": "A2A mesh is disabled"}}
LIST = {"jsonrpc": "2.0", "id": "a2a-list", "method": "tasks/list"}
EMPTY_GET = {"jsonrpc": "2.0", "id": "a2a-get-empty", "method": "tasks/get", "params": {"id": "  "}}
MISSING_GET = {
    "jsonrpc": "2.0",
    "id": "a2a-get-missing",
    "method": "tasks/get",
    "params": {"id": "task_missing"},
}
EMPTY_CANCEL = {
    "jsonrpc": "2.0",
    "id": "a2a-cancel-empty",
    "method": "tasks/cancel",
    "params": {},
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
    log_path = tmp_settings / "gateway-a2a-tasks.log"
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


def _assert_error(body: dict[str, Any], request_id: str, code: int, message: str) -> None:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert body["error"]["code"] == code
    assert body["error"]["message"] == message
    assert "result" not in body


def test_a2a_tasks_use_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python a2a router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python a2a message entered")

    monkeypatch.setattr(a2a_routes, "create_a2a_router", _refuse_python_router)
    monkeypatch.setattr(a2a_runtime, "handle_a2a_message", _refuse_python_message)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port, enabled=None), port)
    try:
        missing_status, missing = _post(port, LIST)
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        wrong_status, wrong = _post(port, LIST, "not-the-a2a-token")
        assert wrong_status == 401
        assert wrong == UNAUTHORIZED

        list_status, listed = _post(port, LIST, TOKEN)
        assert list_status == 200
        assert listed["jsonrpc"] == "2.0"
        assert listed["id"] == "a2a-list"
        assert listed["result"] == {"tasks": []}
        assert "error" not in listed

        empty_status, empty = _post(port, EMPTY_GET, TOKEN)
        assert empty_status == 200
        _assert_error(empty, "a2a-get-empty", -32602, "id is required")

        missing_task_status, missing_task = _post(port, MISSING_GET, TOKEN)
        assert missing_task_status == 200
        _assert_error(missing_task, "a2a-get-missing", -32001, "Task not found")

        cancel_status, cancel = _post(port, EMPTY_CANCEL, TOKEN)
        assert cancel_status == 200
        _assert_error(cancel, "a2a-cancel-empty", -32602, "id is required")
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()

    disabled_port = _free_port()
    disabled = _serve(tmp_settings, _child_env(tmp_settings, disabled_port, enabled="false"), disabled_port)
    try:
        locked_status, locked = _post(disabled_port, LIST)
        assert locked_status == 401
        assert locked == UNAUTHORIZED
        refused_status, refused = _post(disabled_port, LIST, TOKEN)
        assert refused_status == 403
        assert refused == DISABLED
    finally:
        disabled[0].kill()
        disabled[0].wait(timeout=10)
        disabled[1].close()
