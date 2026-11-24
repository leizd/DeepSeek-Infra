"""Native gateway framed automation-run bodies.

The prebuilt gateway, with ``DEEPSEEK_RUNTIME_MODE`` unset and a seeded
``auto_keep``, refuses the framed bodies covered by the in-process router
test: a zero length is the ownership conflict, a truncated object and a
JSON array are 400, a non-UTF-8 byte is 500, and 2000001 bytes are 413.
A non-integer ``Content-Length`` is an empty HTTP 400 from the server
framing layer, not the in-process router's 500 JSON envelope. None of
those requests creates history, saved
items, or ``.memory``. The owner process then accepts the 12:34 schedule
post. Python's automation runner is not the owner of those HTTP responses.
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
RUN_PATH = "/api/automation/auto_keep/run"
NOT_OWNED = {
    "error": "The automation store is still written by the Python runtime, so this gateway refuses to mutate it.",
    "code": "NATIVE_AUTOMATION_WRITE_NOT_OWNED",
}
NOT_OBJECT = {"error": "Request body must be a JSON object", "code": "invalid_payload"}
SERVER_ERROR = {"error": "Server error", "code": "internal"}
TOO_LARGE = {"error": "Request body is too large", "code": "upload_too_large"}
SEEDED_AUTOMATION = (
    '{"automations":[{"automationId":"auto_keep","name":"Keep notes","enabled":true,'
    '"projectId":"proj_ok","condition":{"type":"always"},'
    '"action":{"type":"save_item","title":"Kept note","content":"hello from automation"}}]}'
)
SEEDED_PROJECT = '{"id":"proj_ok","name":"Demo","createdAt":10,"updatedAt":10}\n'
DUE_BODY = b'{"trigger":{"type":"schedule","cron":"34 12 4 10 *"},"now":"2026-10-04T12:34:00+00:00"}'
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


def _raw(port: int, content_length: str, body: bytes) -> tuple[int, bytes]:
    head = (
        f"POST {RUN_PATH} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Authorization: Bearer {TOKEN}\r\n"
        f"Content-Length: {content_length}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    chunks: list[bytes] = []
    with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
        sock.settimeout(30)
        sock.sendall(head + body)
        while True:
            try:
                chunk = sock.recv(65536)
            except TimeoutError as exc:
                raise AssertionError(f"timed out after status bytes {len(b''.join(chunks))}") from exc
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    header_block, _, payload = raw.partition(b"\r\n\r\n")
    return int(header_block.split(b" ", 2)[1]), payload


def _raw_json(port: int, content_length: str, body: bytes) -> tuple[int, dict[str, Any]]:
    status, payload = _raw(port, content_length, body)
    text = payload.decode("utf-8")
    parsed, _end = json.JSONDecoder().raw_decode(text)
    assert isinstance(parsed, dict)
    return status, parsed


def _due(port: int) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{RUN_PATH}",
        data=DUE_BODY,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
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
    env.update(AUTOMATION_ENV)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    if mode:
        env["DEEPSEEK_RUNTIME_MODE"] = mode
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int, label: str) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / f"gateway-automation-run-framed-{label}.log"
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


def _process_tree(pid: int) -> list[str]:
    script = (
        "$queue = [System.Collections.Generic.Queue[int]]::new(); "
        f"$queue.Enqueue({pid}); "
        "$names = New-Object System.Collections.Generic.List[string]; "
        "while ($queue.Count -gt 0) { "
        "$id = $queue.Dequeue(); "
        "$proc = Get-Process -Id $id -ErrorAction SilentlyContinue; "
        "if ($null -eq $proc) { continue }; "
        "$names.Add($proc.ProcessName); "
        "Get-CimInstance Win32_Process -Filter \"ParentProcessId=$id\" | "
        "ForEach-Object { $queue.Enqueue([int]$_.ProcessId) } "
        "}; $names -join \"`n\""
    )
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _assert_no_python(pid: int) -> list[str]:
    names = _process_tree(pid)
    lowered = [name.lower() for name in names]
    assert names
    assert lowered[0] == "deepseek-gateway"
    assert not any(name.startswith("python") for name in lowered)
    return names


def test_framed_run_bodies_are_refused_without_writes(
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

    store = tmp_settings / ".automation" / "automations.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(SEEDED_AUTOMATION.encode("utf-8"))
    project = tmp_settings / ".projects" / "proj_ok" / "project.json"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_bytes(SEEDED_PROJECT.encode("utf-8"))
    history = tmp_settings / ".automation" / "history.json"
    saved = project.parent / "saved-items.json"
    memories = tmp_settings / ".memory"
    seeded_store = store.read_bytes()
    seeded_project = project.read_bytes()

    def _untouched() -> None:
        assert store.read_bytes() == seeded_store
        assert project.read_bytes() == seeded_project
        assert not history.exists()
        assert not saved.exists()
        assert not memories.exists()

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        denied_names = _assert_no_python(denied[0].pid)
        status, payload = _raw_json(denied_port, "0", b"{")
        assert status == 409
        assert payload == NOT_OWNED
        _untouched()

        status, payload = _raw_json(denied_port, "1", b"{")
        assert status == 400
        assert payload["code"] == "invalid_payload"
        assert str(payload["error"]).startswith("Invalid JSON:")
        _untouched()

        status, payload = _raw_json(denied_port, "2", b"[]")
        assert status == 400
        assert payload == NOT_OBJECT
        _untouched()

        status, payload = _raw_json(denied_port, "1", b"\xff")
        assert status == 500
        assert payload == SERVER_ERROR
        _untouched()

        status, raw_payload = _raw(denied_port, "nope", b"{}")
        assert status == 400
        assert raw_payload == b""
        _untouched()

        status, payload = _raw_json(denied_port, "2000001", b"x" * 2_000_001)
        assert status == 413
        assert payload == TOO_LARGE
        _untouched()
        print(
            "RUN_BODY_REFUSALS zero=409 json=400 object=400 utf8=500 "
            f"length=400-empty large=413 history=absent process={','.join(denied_names)}"
        )
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
        owner_names = _assert_no_python(owner[0].pid)
        status, payload = _due(owner_port)
        assert status == 200
        assert payload["ok"] is True
        run = payload["run"]
        assert isinstance(run, dict)
        assert run["status"] == "success"
        assert run["logs"] == ["saveItem"]
        assert run["automationId"] == "auto_keep"
        items = json.loads(saved.read_text(encoding="utf-8"))["items"]
        assert len(items) == 1
        assert items[0]["title"] == "Kept note"
        assert store.read_bytes() == seeded_store
        print(
            "RUN_DUE status=success title=Kept note count=1 "
            f"process={','.join(owner_names)}"
        )
    finally:
        _stop(owner)
