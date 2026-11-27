"""Native gateway reminders-store ownership.

``POST /api/reminders`` is served by the prebuilt gateway. While
``DEEPSEEK_RUNTIME_MODE`` is unset, a bearer create is refused and
``.reminders`` stays absent. After the mode is ``python_disabled``,
the same create persists one reminder and a list returns it. A later
non-owner delete is refused and the stored bytes stay unchanged. A
missing token is 401. Python's reminder action and store writer are
not the owners of those HTTP responses.
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

from deepseek_infra.infra.data import reminders as reminder_store
from deepseek_infra.infra.tool_runtime import tools as tool_runtime
from deepseek_infra.web import server as web_server

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
TOKEN = "reminders-owner-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
NOT_OWNED = {
    "error": "The reminders store is still written by the Python runtime, so this gateway refuses to mutate it.",
    "code": "NATIVE_REMINDERS_WRITE_NOT_OWNED",
}
CREATE = {
    "action": "create",
    "title": "喝水",
    "content": "多喝水",
    "dueAt": "2026-01-01T00:00:00Z",
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


def _post(port: int, body: dict[str, Any], token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/reminders",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
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
    log_path = tmp_settings / f"gateway-reminders-owner-{label}.log"
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


def test_reminder_create_is_owned_by_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_action(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python reminder_action entered")

    def _refuse_python_create(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_reminder entered")

    def _refuse_python_tool(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_reminder_tool entered")

    monkeypatch.setattr(web_server, "reminder_action", _refuse_python_action)
    monkeypatch.setattr(reminder_store, "create_reminder", _refuse_python_create)
    monkeypatch.setattr(tool_runtime, "create_reminder_tool", _refuse_python_tool)
    reminder_file = tmp_settings / ".reminders" / "reminders.json"
    generation = tmp_settings / ".workspace-generation"

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        missing_status, missing = _post(denied_port, CREATE)
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        assert not reminder_file.exists()

        status, payload = _post(denied_port, CREATE, TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert not reminder_file.exists()
        assert not generation.exists()
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
        status, payload = _post(owner_port, CREATE, TOKEN)
        assert status == 200
        assert payload["ok"] is True
        reminder = payload["reminder"]
        assert reminder["title"] == "喝水"
        assert reminder["content"] == "多喝水"
        assert reminder["dueAt"] == "2026-01-01T00:00:00+00:00"
        assert reminder["notified"] is False
        reminder_id = reminder["id"]
        assert isinstance(reminder_id, str)
        assert len(reminder_id) == 16
        assert reminder_id == reminder_id.lower()
        assert all(character in "0123456789abcdef" for character in reminder_id)

        listed_status, listed = _post(owner_port, {"action": "list"}, TOKEN)
        assert listed_status == 200
        rows = listed["reminders"]
        assert isinstance(rows, list)
        assert len(rows) == 1
        assert rows[0]["id"] == reminder_id
        assert rows[0]["title"] == "喝水"
        stored = reminder_file.read_bytes()
        assert "喝水".encode() in stored
        assert reminder_id.encode() in stored
        assert generation.read_text(encoding="utf-8").strip() == "2"
    finally:
        _stop(owner)

    stored_after_create = reminder_file.read_bytes()
    generation_after_create = generation.read_bytes()
    refused_port = _free_port()
    refused = _serve(tmp_settings, _child_env(tmp_settings, refused_port, ""), refused_port, "refused")
    try:
        status, payload = _post(
            refused_port,
            {"action": "delete", "id": json.loads(stored_after_create.decode("utf-8"))[0]["id"]},
            TOKEN,
        )
        assert status == 409
        assert payload == NOT_OWNED
        assert reminder_file.read_bytes() == stored_after_create
        assert generation.read_bytes() == generation_after_create
    finally:
        _stop(refused)
