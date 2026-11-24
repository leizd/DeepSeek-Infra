"""Native gateway automation-template ownership.

``POST /api/automation/templates/daily_project_summary`` is served
by the prebuilt gateway. Creating from that template writes
``.automation``. While ``DEEPSEEK_RUNTIME_MODE`` is unset, a bearer
create is refused and the directory stays absent. After the mode is
``python_disabled``, the same body persists one automation and a
GET returns it. A later non-owner create is refused and the stored
bytes stay unchanged. A missing token is 401. Python's automation
writer is not the owner of those HTTP responses.
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

from deepseek_infra.infra.automation import registry as automation_registry
from deepseek_infra.web.routes import automation as automation_routes

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "automation-template-owner-token"
CREATE_PATH = "/api/automation/templates/daily_project_summary"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
NOT_OWNED = {
    "error": "The automation store is still written by the Python runtime, so this gateway refuses to mutate it.",
    "code": "NATIVE_AUTOMATION_WRITE_NOT_OWNED",
}
CLEARED = (
    "AUTH_TOKEN",
    "AUTH_DISABLED",
    "DEEPSEEK_RUNTIME_MODE",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(port: int, method: str, path: str, body: bytes | None, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers=headers,
        method=method,
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


def _child_env(tmp_settings: Path, port: int, mode: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in CLEARED:
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    if mode:
        env["DEEPSEEK_RUNTIME_MODE"] = mode
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int, label: str) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / f"gateway-automation-template-{label}.log"
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


def test_automation_template_create_is_owned_by_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_template(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_from_template entered")

    def _refuse_python_create(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_automation entered")

    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_automation_router entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(automation_registry, "create_from_template", _refuse_python_template)
    monkeypatch.setattr(automation_registry, "create_automation", _refuse_python_create)
    monkeypatch.setattr(automation_routes, "create_automation_router", _refuse_python_router)
    automation_dir = tmp_settings / ".automation"
    store = automation_dir / "automations.json"
    generation = tmp_settings / ".workspace-generation"
    projects = tmp_settings / ".projects"

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        missing_status, missing = _request(denied_port, "POST", CREATE_PATH, b"{}")
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        assert not automation_dir.exists()

        status, payload = _request(denied_port, "POST", CREATE_PATH, b"{}", TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert not automation_dir.exists()
        assert not generation.exists()
        assert not projects.exists()
    finally:
        _stop(denied)

    owner_port = _free_port()
    owner = _serve(
        tmp_settings,
        _child_env(tmp_settings, owner_port, "python_disabled"),
        owner_port,
        "owner",
    )
    try:
        status, payload = _request(owner_port, "POST", CREATE_PATH, b"{}", TOKEN)
        assert status == 200
        assert payload["ok"] is True
        automation = payload["automation"]
        assert isinstance(automation, dict)
        automation_id = automation["automationId"]
        assert isinstance(automation_id, str)
        assert automation["id"] == automation_id
        assert automation_id.startswith("auto_")
        suffix = automation_id.removeprefix("auto_")
        assert len(suffix) == 16
        assert suffix == suffix.lower()
        assert all(character in "0123456789abcdef" for character in suffix)
        assert automation["name"] == "Daily Project Summary"
        assert automation["trigger"]["type"] == "schedule"
        assert automation["action"]["type"] == "project_summary"
        assert automation["projectId"] == ""

        read_status, read_back = _request(owner_port, "GET", f"/api/automation/{automation_id}", None, TOKEN)
        assert read_status == 200
        assert read_back == payload
        stored = store.read_bytes()
        assert automation_id.encode("ascii") in stored
        assert not projects.exists()
    finally:
        _stop(owner)

    stored_after_create = store.read_bytes()
    generation_after_create = generation.read_bytes() if generation.exists() else None
    refused_port = _free_port()
    refused = _serve(tmp_settings, _child_env(tmp_settings, refused_port, ""), refused_port, "refused")
    try:
        status, payload = _request(refused_port, "POST", CREATE_PATH, b"{}", TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert store.read_bytes() == stored_after_create
        assert not projects.exists()
        if generation_after_create is None:
            assert not generation.exists()
        else:
            assert generation.read_bytes() == generation_after_create
    finally:
        _stop(refused)
