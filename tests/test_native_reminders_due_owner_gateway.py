"""Native gateway reminders due-poll ownership.

``POST /api/reminders/due`` is served by the prebuilt gateway. The
poll marks a due reminder notified, so it is a write. While
``DEEPSEEK_RUNTIME_MODE`` is unset, a bearer poll is refused and the
seeded file stays byte-identical. After the mode is
``python_disabled``, the same poll returns that reminder with
``notified`` true and the store contains the mark. A later non-owner
poll is refused and those bytes stay unchanged. A missing token is
401. Python's due writer is not the owner of those HTTP responses.
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
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "reminders-due-owner-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
NOT_OWNED = {
    "error": "The reminders store is still written by the Python runtime, so this gateway refuses to mutate it.",
    "code": "NATIVE_REMINDERS_WRITE_NOT_OWNED",
}
SEEDED = (
    '[{"id":"abc","title":"喝水","content":"","dueAt":"2000-01-01T00:00:00+00:00",'
    '"createdAt":1,"notified":false}]'
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


def _post(port: int, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/reminders/due",
        data=b"{}",
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
    log_path = tmp_settings / f"gateway-reminders-due-{label}.log"
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
    reminder_file = tmp_settings / ".reminders" / "reminders.json"
    reminder_file.parent.mkdir(parents=True, exist_ok=True)
    reminder_file.write_bytes(SEEDED.encode("utf-8"))
    return reminder_file


def test_reminder_due_poll_is_owned_by_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_due(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python due_reminders entered")

    def _refuse_python_action(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python reminder_action entered")

    def _refuse_python_create(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_reminder entered")

    def _refuse_python_tool(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_reminder_tool entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(web_server, "due_reminders", _refuse_python_due)
    monkeypatch.setattr(reminder_store, "due_reminders", _refuse_python_due)
    monkeypatch.setattr(web_server, "reminder_action", _refuse_python_action)
    monkeypatch.setattr(reminder_store, "create_reminder", _refuse_python_create)
    monkeypatch.setattr(tool_runtime, "create_reminder_tool", _refuse_python_tool)
    reminder_file = _seed(tmp_settings)
    generation = tmp_settings / ".workspace-generation"
    seeded = reminder_file.read_bytes()
    assert seeded == SEEDED.encode("utf-8")

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        missing_status, missing = _post(denied_port)
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        assert reminder_file.read_bytes() == seeded

        status, payload = _post(denied_port, TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert reminder_file.read_bytes() == seeded
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
        status, payload = _post(owner_port, TOKEN)
        assert status == 200
        reminders = payload["reminders"]
        assert isinstance(reminders, list)
        assert len(reminders) == 1
        due = reminders[0]
        assert isinstance(due, dict)
        assert due["id"] == "abc"
        assert due["title"] == "喝水"
        assert due["dueAt"] == "2000-01-01T00:00:00+00:00"
        assert due["notified"] is True
        notified_at = due["notifiedAt"]
        assert isinstance(notified_at, int) and not isinstance(notified_at, bool)
        stored = reminder_file.read_bytes()
        assert b'"notified": true' in stored
        parsed = json.loads(stored.decode("utf-8"))
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["id"] == "abc"
        assert parsed[0]["notified"] is True
        assert parsed[0]["notifiedAt"] == notified_at
    finally:
        _stop(owner)

    stored_after_poll = reminder_file.read_bytes()
    generation_after_poll = generation.read_bytes() if generation.exists() else None
    refused_port = _free_port()
    refused = _serve(tmp_settings, _child_env(tmp_settings, refused_port, ""), refused_port, "refused")
    try:
        status, payload = _post(refused_port, TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert reminder_file.read_bytes() == stored_after_poll
        if generation_after_poll is None:
            assert not generation.exists()
        else:
            assert generation.read_bytes() == generation_after_poll
    finally:
        _stop(refused)
