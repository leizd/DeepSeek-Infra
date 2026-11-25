"""Native gateway context-taint status.

``GET /api/taint`` is served by the prebuilt gateway. Auth enabled refuses
a missing token. The configured bearer token returns the shipped taint
block. ``TAINT_ENABLED=false`` turns the block off. Python's status router
and taint status helper are not the owners of those HTTP reads.
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

from deepseek_infra.infra.gateway import context_taint
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
TOKEN = "taint-status-token"
SOURCES = [
    "trusted_system",
    "trusted_user",
    "trusted_memory",
    "trusted_tool",
    "untrusted_web",
    "untrusted_browser",
    "untrusted_file",
    "untrusted_media",
    "untrusted_rag",
    "untrusted_tool_result",
]
TAINT_ENV = (
    "TAINT_ENABLED",
    "TAINT_HARDEN_SEARCH_CONTEXT",
    "TAINT_HARDEN_FILE_CONTEXT",
    "TAINT_ESCALATE_CONFIRM",
    "TAINT_MAX_SEGMENTS",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/taint",
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
    for name in ("AUTH_TOKEN", "AUTH_DISABLED", "DEEPSEEK_RUNTIME_MODE", *TAINT_ENV):
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    if enabled is not None:
        env["TAINT_ENABLED"] = enabled
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-taint.log"
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


def _assert_taint(body: dict[str, Any], *, enabled: bool) -> None:
    assert body["ok"] is True
    taint = body["contextTaint"]
    assert isinstance(taint, dict)
    assert taint["enabled"] is enabled
    assert taint["hardenSearchContext"] is True
    assert taint["hardenFileContext"] is True
    assert taint["escalateConfirm"] is True
    assert taint["trustLevels"] == ["trusted", "untrusted"]
    assert taint["sources"] == SOURCES
    assert taint["exfiltrationPatterns"] == 3
    assert taint["toolDirectivePatterns"] == 3
    names = taint["sensitiveToolNames"]
    assert isinstance(names, list)
    assert names
    assert all(isinstance(name, str) and name for name in names)
    assert "fetch_url" in names


def test_taint_status_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_status(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python status router entered")

    def _refuse_python_taint(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python taint status entered")

    monkeypatch.setattr(status_routes, "create_status_router", _refuse_python_status)
    monkeypatch.setattr(context_taint, "taint_status", _refuse_python_taint)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port, enabled=None), port)
    try:
        missing_status, missing = _get(port)
        assert missing_status == 401
        assert missing == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        wrong_status, wrong = _get(port, "not-the-taint-token")
        assert wrong_status == 401
        assert wrong == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        ok_status, ok_body = _get(port, TOKEN)
        assert ok_status == 200
        _assert_taint(ok_body, enabled=True)
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()

    disabled_port = _free_port()
    disabled = _serve(tmp_settings, _child_env(tmp_settings, disabled_port, enabled="false"), disabled_port)
    try:
        disabled_status, disabled_body = _get(disabled_port, TOKEN)
        assert disabled_status == 200
        _assert_taint(disabled_body, enabled=False)
    finally:
        disabled[0].kill()
        disabled[0].wait(timeout=10)
        disabled[1].close()
