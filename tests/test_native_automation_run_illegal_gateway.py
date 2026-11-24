"""Native gateway automation-run illegal requests.

With ``DEEPSEEK_RUNTIME_MODE`` unset and an empty root, the prebuilt
gateway refuses illegal ``POST /api/automation/{id}/run`` requests: no token
is 401, id ``no`` is 400, an absent automation is 404, and an unparsable
``now`` is 400. A negative ``Content-Length`` is rejected by the HTTP layer
with an empty 400 before the route runs, unlike the in-process router test,
which reaches the 404. None of those requests creates ``.automation``,
``.projects`` or ``.memory``. Python's automation runner is not the owner
of those HTTP responses.
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

from deepseek_infra.infra.automation import actions as automation_actions
from deepseek_infra.infra.automation import runner as automation_runner
from deepseek_infra.web.routes import automation as automation_routes

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "automation-run-owner-token"
MISSING_PATH = "/api/automation/auto_gone/run"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
NOT_FOUND = {"error": "Automation not found", "code": "not_found"}
CLEARED = (
    "AUTH_TOKEN",
    "AUTH_DISABLED",
    "DEEPSEEK_RUNTIME_MODE",
    "AUTOMATION_ENABLED",
    "AUTOMATION_MAX_RUNS_PER_DAY",
    "AUTOMATION_MIN_INTERVAL_SECONDS",
    "AUTOMATION_ALLOW_BROWSER",
    "AUTOMATION_REQUIRE_CONFIRM_FOR_BROWSER_WRITE",
    "AUTOMATION_RUN_TIMEOUT_SECONDS",
)
AUTOMATION_ENV = {
    "AUTOMATION_ENABLED": "1",
    "AUTOMATION_MAX_RUNS_PER_DAY": "50",
    "AUTOMATION_MIN_INTERVAL_SECONDS": "300",
    "AUTOMATION_ALLOW_BROWSER": "0",
    "AUTOMATION_REQUIRE_CONFIRM_FOR_BROWSER_WRITE": "1",
    "AUTOMATION_RUN_TIMEOUT_SECONDS": "1800",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(port: int, path: str, body: bytes = b"", token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"} if body else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
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


def _raw_post(port: int, path: str, content_length: str, body: bytes, token: str) -> tuple[int, bytes]:
    head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Content-Length: {content_length}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    chunks: list[bytes] = []
    with socket.create_connection(("127.0.0.1", port), timeout=20) as sock:
        sock.sendall(head + body)
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    header_block, _, payload = raw.partition(b"\r\n\r\n")
    return int(header_block.split(b" ", 2)[1]), payload


def _child_env(tmp_settings: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    for name in CLEARED:
        env.pop(name, None)
    env.update(AUTOMATION_ENV)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-automation-run-illegal.log"
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


def _stop(served: tuple[subprocess.Popen[bytes], Any]) -> None:
    served[0].kill()
    served[0].wait(timeout=10)
    served[1].close()


def test_illegal_automation_run_requests_are_refused_without_writes(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python run_once entered")

    def _refuse_python_save(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python save_item_action entered")

    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_automation_router entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(automation_runner, "run_once", _refuse_python_run)
    monkeypatch.setattr(automation_actions, "save_item_action", _refuse_python_save)
    monkeypatch.setattr(automation_routes, "create_automation_router", _refuse_python_router)

    def _untouched() -> None:
        assert not (tmp_settings / ".automation").exists()
        assert not (tmp_settings / ".projects").exists()
        assert not (tmp_settings / ".memory").exists()

    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        status, payload = _post(port, MISSING_PATH)
        assert status == 401
        assert payload == UNAUTHORIZED
        _untouched()

        status, payload = _post(port, "/api/automation/no/run", token=TOKEN)
        assert status == 400
        assert payload == {"error": "Invalid automation id", "code": "invalid_payload"}
        _untouched()

        status, payload = _post(port, MISSING_PATH, token=TOKEN)
        assert status == 404
        assert payload == NOT_FOUND
        _untouched()

        status, raw_payload = _raw_post(port, MISSING_PATH, "-1", b"not-json", TOKEN)
        assert status == 400
        assert raw_payload == b""
        _untouched()

        status, payload = _post(port, MISSING_PATH, b'{"now":"yesterday"}', TOKEN)
        assert status == 400
        assert payload == {
            "error": "now must be an ISO timestamp or epoch milliseconds",
            "code": "invalid_payload",
        }
        _untouched()
    finally:
        _stop(served)
