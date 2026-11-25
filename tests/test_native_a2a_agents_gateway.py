"""Native gateway A2A agent list.

``GET /a2a/agents`` is served by the prebuilt gateway. Auth is checked
before the mesh flag. A missing token is 401. The bearer token returns
the five shipped agent cards. ``A2A_ENABLED=false`` with that token
returns 403. Python's A2A router and agent-card list are not the owners
of those HTTP reads.
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
TOKEN = "a2a-agents-token"
AGENTS = (
    ("orchestrator", "DeepSeek Infra Orchestrator"),
    ("researcher", "资料检索 Agent"),
    ("coder", "代码分析 Agent"),
    ("reasoner", "逻辑推理 Agent"),
    ("critic", "反驳审查 Agent"),
)
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
DISABLED = {"error": {"code": "forbidden", "message": "A2A mesh is disabled"}}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/a2a/agents",
        headers=headers,
        method="GET",
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
    log_path = tmp_settings / "gateway-a2a-agents.log"
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


def _assert_agents(body: dict[str, Any]) -> None:
    assert body["ok"] is True
    agents = body["agents"]
    assert isinstance(agents, list)
    assert len(agents) == len(AGENTS)
    for agent, (agent_id, name) in zip(agents, AGENTS, strict=True):
        assert agent["name"] == name
        assert agent["protocolVersion"] == "0.3.0"
        assert agent["version"] == "4.8.0"
        assert agent["url"] == f"http://127.0.0.1:8000/a2a/agents/{agent_id}"
        assert agent["preferredTransport"] == "JSONRPC"


def test_a2a_agents_use_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python a2a router entered")

    def _refuse_python_cards(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python agent cards entered")

    monkeypatch.setattr(a2a_routes, "create_a2a_router", _refuse_python_router)
    monkeypatch.setattr(a2a_runtime, "agent_cards", _refuse_python_cards)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port, enabled=None), port)
    try:
        missing_status, missing = _get(port)
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        wrong_status, wrong = _get(port, "not-the-a2a-token")
        assert wrong_status == 401
        assert wrong == UNAUTHORIZED
        ok_status, ok_body = _get(port, TOKEN)
        assert ok_status == 200
        _assert_agents(ok_body)
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()

    disabled_port = _free_port()
    disabled = _serve(tmp_settings, _child_env(tmp_settings, disabled_port, enabled="false"), disabled_port)
    try:
        locked_status, locked = _get(disabled_port)
        assert locked_status == 401
        assert locked == UNAUTHORIZED
        refused_status, refused = _get(disabled_port, TOKEN)
        assert refused_status == 403
        assert refused == DISABLED
    finally:
        disabled[0].kill()
        disabled[0].wait(timeout=10)
        disabled[1].close()
