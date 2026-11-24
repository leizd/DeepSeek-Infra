"""Native gateway A2A agent card.

``GET /.well-known/agent-card.json`` is served by the prebuilt gateway.
It does not require a bearer token. With ``A2A_ENABLED`` unset the
orchestrator card is returned. ``A2A_ENABLED=false`` refuses the mesh.
Auth stays enabled, so ``GET /api/config`` without a token is still 401.
Python's A2A router and agent-card helper are not the owners of those
HTTP reads.
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
TOKEN = "agent-card-token"
DESCRIPTION = (
    "You are DeepSeek Infra's general-purpose assistant Agent with the full local tool surface."
)
CARD = {
    "protocolVersion": "0.3.0",
    "name": "DeepSeek Infra Orchestrator",
    "description": DESCRIPTION,
    "url": "http://127.0.0.1:8000/a2a/agents/orchestrator",
    "preferredTransport": "JSONRPC",
    "version": "4.8.0",
    "capabilities": {
        "streaming": True,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    },
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "orchestrator.respond",
            "name": "DeepSeek Infra Orchestrator",
            "description": DESCRIPTION,
            "tags": ["orchestrator", "full_tool_surface"],
        }
    ],
}
DISABLED = {"error": {"code": "forbidden", "message": "A2A mesh is disabled"}}
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, path: str) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
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
    for name in ("AUTH_TOKEN", "AUTH_DISABLED", "DEEPSEEK_RUNTIME_MODE", "A2A_ENABLED"):
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    if enabled is not None:
        env["A2A_ENABLED"] = enabled
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-agent-card.log"
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


def test_agent_card_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python a2a router entered")

    def _refuse_python_card(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python agent card entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(a2a_routes, "create_a2a_router", _refuse_python_router)
    monkeypatch.setattr(a2a_runtime, "agent_card", _refuse_python_card)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port, enabled=None), port)
    try:
        status, body = _get(port, "/.well-known/agent-card.json")
        assert status == 200
        assert body == CARD
        locked_status, locked = _get(port, "/api/config")
        assert locked_status == 401
        assert locked == UNAUTHORIZED
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()

    disabled_port = _free_port()
    disabled = _serve(tmp_settings, _child_env(tmp_settings, disabled_port, enabled="false"), disabled_port)
    try:
        disabled_status, disabled_body = _get(disabled_port, "/.well-known/agent-card.json")
        assert disabled_status == 403
        assert disabled_body == DISABLED
    finally:
        disabled[0].kill()
        disabled[0].wait(timeout=10)
        disabled[1].close()
