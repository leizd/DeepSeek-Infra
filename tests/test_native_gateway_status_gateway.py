"""Native gateway runtime status.

``GET /api/gateway/status`` is served by the prebuilt gateway. Auth enabled
refuses a missing token. The configured bearer token returns the context
manager defaults and the honest unported queue and scheduler gaps.
``GATEWAY_CONTEXT_MANAGER_ENABLED=false`` turns the manager off.
``GATEWAY_CONTEXT_WINDOW_MESSAGES=12`` changes the window. Python's status
router and gateway status helper are not the owners of those HTTP reads.
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

from deepseek_infra.infra.gateway import resiliency
from deepseek_infra.web.routes import status as status_routes

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
TOKEN = "gateway-status-token"
QUEUE_REASON = "native request queue is not ported"
SCHEDULER_REASON = "python job scheduler is not ported"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/gateway/status",
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


def _child_env(
    tmp_settings: Path,
    port: int,
    *,
    enabled: str | None,
    window: str | None,
) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "AUTH_TOKEN",
        "AUTH_DISABLED",
        "DEEPSEEK_RUNTIME_MODE",
        "GATEWAY_CONTEXT_MANAGER_ENABLED",
        "GATEWAY_CONTEXT_WINDOW_MESSAGES",
        "CONTEXT_ENGINE_ENABLED",
        "CONTEXT_ENGINE_TOKEN_AWARE_TRIM",
    ):
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    if enabled is not None:
        env["GATEWAY_CONTEXT_MANAGER_ENABLED"] = enabled
    if window is not None:
        env["GATEWAY_CONTEXT_WINDOW_MESSAGES"] = window
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-status.log"
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


def _assert_status(body: dict[str, Any], *, enabled: bool, window: int) -> None:
    assert body["ok"] is True
    gateway = body["gateway"]
    assert isinstance(gateway, dict)
    manager = gateway["contextManager"]
    assert manager["enabled"] is enabled
    assert manager["stableJson"] is enabled
    assert manager["toolOrder"] == "function.name"
    assert manager["slidingWindowMessages"] == window
    queue = gateway["requestQueue"]
    assert queue["enabled"] is False
    assert queue["ported"] is False
    assert queue["reason"] == QUEUE_REASON
    assert "counts" not in queue
    scheduler = gateway["scheduler"]
    assert scheduler["ported"] is False
    assert scheduler["reason"] == SCHEDULER_REASON


def test_gateway_status_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_status(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python status router entered")

    def _refuse_python_gateway(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python gateway status entered")

    monkeypatch.setattr(status_routes, "create_status_router", _refuse_python_status)
    monkeypatch.setattr(resiliency, "gateway_status", _refuse_python_gateway)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port, enabled=None, window=None), port)
    try:
        missing_status, missing = _get(port)
        assert missing_status == 401
        assert missing == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        wrong_status, wrong = _get(port, "not-the-gateway-status-token")
        assert wrong_status == 401
        assert wrong == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        ok_status, ok_body = _get(port, TOKEN)
        assert ok_status == 200
        _assert_status(ok_body, enabled=True, window=36)
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()

    tuned_port = _free_port()
    tuned = _serve(
        tmp_settings,
        _child_env(tmp_settings, tuned_port, enabled="false", window="12"),
        tuned_port,
    )
    try:
        tuned_status, tuned_body = _get(tuned_port, TOKEN)
        assert tuned_status == 200
        _assert_status(tuned_body, enabled=False, window=12)
    finally:
        tuned[0].kill()
        tuned[0].wait(timeout=10)
        tuned[1].close()
