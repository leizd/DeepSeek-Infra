"""Native gateway public runtime config.

``GET /api/config`` is served by the prebuilt gateway. Auth enabled refuses
a missing token and a wrong bearer token. The configured bearer token, and
``AUTH_DISABLED=1`` with no token, return the public runtime document.
Python's status router is not the owner of those HTTP reads.

The 2026-10-07 gateway binary omits ``ocr``. Current ``api_config`` in
``deepseek-gateway`` includes it. This process proof does not treat that
source field as executed.
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
TOKEN = "config-auth-token"
EXPECTED = {
    "version": "4.8.0",
    "hasServerKey": False,
    "hasSearch": False,
    "defaultModel": "deepseek-v4-pro",
    "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
    "modelRoutes": {"fast": "deepseek-v4-flash", "expert": "deepseek-v4-pro"},
    "searchModes": ["off", "auto", "on"],
    "uploadLimits": {
        "fileMaxBytes": 200_000_000,
        "requestMaxBytes": 220_000_000,
        "maxFiles": 20,
    },
    "mcp": {"enabled": True, "protocolVersion": "2025-06-18", "endpoint": "/mcp"},
    "a2a": {"enabled": True, "protocolVersion": "0.3.0", "endpoint": "/a2a"},
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/config",
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


def _child_env(tmp_settings: Path, port: int, *, auth_disabled: bool) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "DEEPSEEK_API_KEY",
        "TAVILY_API_KEY",
        "DEEPSEEK_DEFAULT_MODEL",
        "OCR_ENABLED",
        "OCR_MODE",
        "AUTH_TOKEN",
        "AUTH_DISABLED",
    ):
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    if auth_disabled:
        env["AUTH_DISABLED"] = "1"
    else:
        env["AUTH_TOKEN"] = TOKEN
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-config.log"
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


def test_public_config_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_config(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python status config entered")

    monkeypatch.setattr(status_routes, "create_status_router", _refuse_python_config)
    disabled_port = _free_port()
    disabled = _serve(
        tmp_settings,
        _child_env(tmp_settings, disabled_port, auth_disabled=True),
        disabled_port,
    )
    try:
        status, body = _get(disabled_port)
        assert status == 200
        assert "ocr" not in body
        assert body == EXPECTED
    finally:
        disabled[0].kill()
        disabled[0].wait(timeout=10)
        disabled[1].close()

    auth_port = _free_port()
    authed = _serve(
        tmp_settings,
        _child_env(tmp_settings, auth_port, auth_disabled=False),
        auth_port,
    )
    try:
        missing_status, missing = _get(auth_port)
        assert missing_status == 401
        assert missing == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        wrong_status, wrong = _get(auth_port, "not-the-config-token")
        assert wrong_status == 401
        assert wrong == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        ok_status, ok_body = _get(auth_port, TOKEN)
        assert ok_status == 200
        assert ok_body == EXPECTED
    finally:
        authed[0].kill()
        authed[0].wait(timeout=10)
        authed[1].close()
