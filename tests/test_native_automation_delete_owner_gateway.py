"""Native gateway automation-delete ownership.

``DELETE /api/automation/auto_keep`` is served by the prebuilt
gateway. Deleting a stored automation rewrites
``.automation/automations.json``. While ``DEEPSEEK_RUNTIME_MODE``
is unset, a bearer delete is refused and the seeded bytes stay
identical. After the mode is ``python_disabled``, the same delete
removes that id and a GET reports it missing. A later non-owner
delete is refused and the post-delete bytes stay unchanged. A
missing token is 401. Python's automation deleter is not the
owner of those HTTP responses.
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
TOKEN = "automation-delete-owner-token"
ITEM_PATH = "/api/automation/auto_keep"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
NOT_OWNED = {
    "error": "The automation store is still written by the Python runtime, so this gateway refuses to mutate it.",
    "code": "NATIVE_AUTOMATION_WRITE_NOT_OWNED",
}
NOT_FOUND = {"error": "Automation not found", "code": "not_found"}
SEEDED = (
    '{"automations":[{"automationId":"auto_keep","name":"Keep",'
    '"action":{"type":"save_item"},"createdAt":"stored-created",'
    '"createdAtMs":1700000000000,"updatedAtMs":1700000005000}]}'
)
CLEARED = (
    "AUTH_TOKEN",
    "AUTH_DISABLED",
    "DEEPSEEK_RUNTIME_MODE",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(port: int, method: str, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{ITEM_PATH}",
        data=None,
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
    log_path = tmp_settings / f"gateway-automation-delete-{label}.log"
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


def _seed(tmp_settings: Path) -> Path:
    store = tmp_settings / ".automation" / "automations.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(SEEDED.encode("utf-8"))
    return store


def test_automation_delete_is_owned_by_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_delete(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python delete_automation entered")

    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_automation_router entered")

    monkeypatch.setattr(automation_registry, "delete_automation", _refuse_python_delete)
    monkeypatch.setattr(automation_routes, "create_automation_router", _refuse_python_router)
    store = _seed(tmp_settings)
    generation = tmp_settings / ".workspace-generation"
    projects = tmp_settings / ".projects"
    seeded = store.read_bytes()
    assert seeded == SEEDED.encode("utf-8")
    assert b"auto_keep" in seeded

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        missing_status, missing = _request(denied_port, "DELETE")
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        assert store.read_bytes() == seeded

        status, payload = _request(denied_port, "DELETE", TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert store.read_bytes() == seeded
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
        status, payload = _request(owner_port, "DELETE", TOKEN)
        assert status == 200
        assert payload == {"ok": True, "deleted": 1}
        stored = store.read_bytes()
        assert b"auto_keep" not in stored
        parsed = json.loads(stored.decode("utf-8"))
        assert isinstance(parsed, dict)
        rows = parsed["automations"]
        assert isinstance(rows, list)
        assert rows == []
        read_status, read_back = _request(owner_port, "GET", TOKEN)
        assert read_status == 404
        assert read_back == NOT_FOUND
        assert not projects.exists()
    finally:
        _stop(owner)

    stored_after_delete = store.read_bytes()
    generation_after_delete = generation.read_bytes() if generation.exists() else None
    refused_port = _free_port()
    refused = _serve(tmp_settings, _child_env(tmp_settings, refused_port, ""), refused_port, "refused")
    try:
        status, payload = _request(refused_port, "DELETE", TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert store.read_bytes() == stored_after_delete
        assert not projects.exists()
        if generation_after_delete is None:
            assert not generation.exists()
        else:
            assert generation.read_bytes() == generation_after_delete
    finally:
        _stop(refused)
