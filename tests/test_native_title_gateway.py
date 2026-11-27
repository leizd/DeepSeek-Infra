"""Native gateway conversation titles.

``POST /api/title`` is served by the prebuilt gateway. Auth enabled refuses
a missing token. A non-empty message with no API key is refused before any
upstream call. An API key with an empty or whitespace message returns an
empty title and does not call upstream. Python's chat router and title
generator are not the owners of those HTTP reads.
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

from deepseek_infra.infra.gateway import title_generator
from deepseek_infra.web.routes import chat as chat_routes

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
TOKEN = "title-auth-token"
DEAD_UPSTREAM = "http://127.0.0.1:1/v1/chat/completions"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(port: int, body: dict[str, Any] | None, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/title",
        data=data,
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
    for name in (
        "AUTH_TOKEN",
        "AUTH_DISABLED",
        "DEEPSEEK_RUNTIME_MODE",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_API_URL",
    ):
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    env["DEEPSEEK_API_URL"] = DEAD_UPSTREAM
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-title.log"
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


def test_title_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_chat(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python chat router entered")

    def _refuse_python_title(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python title generator entered")

    monkeypatch.setattr(chat_routes, "create_chat_router", _refuse_python_chat)
    monkeypatch.setattr(title_generator, "generate_title_payload", _refuse_python_title)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, {"userMessage": "hello"})
        assert missing_status == 401
        assert missing == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        wrong_status, wrong = _post(port, {"userMessage": "hello"}, "not-the-title-token")
        assert wrong_status == 401
        assert wrong == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        refused_status, refused = _post(port, {"userMessage": "hello"}, TOKEN)
        assert refused_status == 400
        assert refused == {"error": "Missing DeepSeek API Key.", "code": "missing_api_key"}
        empty_status, empty = _post(port, {"apiKey": "title-probe-key", "userMessage": ""}, TOKEN)
        assert empty_status == 200
        assert empty == {"title": ""}
        blank_status, blank = _post(
            port,
            {"apiKey": "title-probe-key", "userMessage": "   "},
            TOKEN,
        )
        assert blank_status == 200
        assert blank == {"title": ""}
        absent_status, absent = _post(port, {"apiKey": "title-probe-key"}, TOKEN)
        assert absent_status == 200
        assert absent == {"title": ""}
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
