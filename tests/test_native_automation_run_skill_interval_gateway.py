"""Native gateway holds an interval skill that allows network.

The body ``{"trigger":{"type":"interval","intervalSeconds":300}}`` on a
``run_skill`` with ``allowNetwork`` true returns
``requires_confirmation`` and logs
``scheduled_skill_network_requires_confirmation``. A confirmation hold
is outside the interval clock, so the same owner process posts that
body again and stores a second hold, not ``interval_not_due``. A later
process with ``DEEPSEEK_RUNTIME_MODE`` unset refuses another post and
leaves the history bytes unchanged. The gateway process is not Python.
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
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
REASON = "scheduled_skill_network_requires_confirmation"
NOT_EXECUTED = "run_skill is not executed by the native gateway yet"
INTERVAL_BODY = b'{"trigger":{"type":"interval","intervalSeconds":300}}'
TRIGGER = {"type": "interval", "intervalSeconds": 300}
SEEDED_AUTOMATION = (
    '{"automations":[{"automationId":"auto_keep","name":"Keep notes","enabled":true,'
    '"projectId":"proj_ok","condition":{"type":"always"},'
    '"action":{"type":"run_skill","allowNetwork":true}}]}'
)
SEEDED_PROJECT = '{"id":"proj_ok","name":"Demo","createdAt":10,"updatedAt":10}\n'
STORED_POLICY = {
    "requiresConfirmation": False,
    "maxRunsPerDay": 50,
    "timeoutSeconds": 1800,
    "allowBrowser": False,
    "browserMode": "read_only",
    "allowNetwork": False,
    "allowPrivateHosts": False,
    "retry": {"maxAttempts": 1, "backoffSeconds": 0},
}
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


def _post(port: int, body: bytes, token: str) -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{RUN_PATH}",
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
    log_path = tmp_settings / f"gateway-automation-run-skill-interval-{label}.log"
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


def _run_id(value: object) -> str:
    assert isinstance(value, str)
    assert value.startswith("auto_run_")
    suffix = value.removeprefix("auto_run_")
    assert len(suffix) == 16
    assert suffix == suffix.lower()
    assert all(character in "0123456789abcdef" for character in suffix)
    return value


def _assert_hold(payload: dict[str, Any]) -> str:
    assert payload["ok"] is True
    held = payload["run"]
    assert isinstance(held, dict)
    assert held["status"] == "requires_confirmation"
    assert held["skippedReason"] == "policy_requires_confirmation"
    assert held["logs"] == [REASON]
    assert held["error"] == ""
    assert held["attempts"] == 1
    assert held["traceId"] == ""
    assert held["trigger"] == TRIGGER
    assert held["automationId"] == "auto_keep"
    decision = held["evidence"]["policy"]
    assert isinstance(decision, dict)
    assert decision["verdict"] == "needs_confirmation"
    assert decision["risk"] == "medium"
    assert decision["reasons"] == [REASON]
    assert decision["policy"] == STORED_POLICY
    return _run_id(held["runId"])


def test_interval_skill_network_requires_confirmation(
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
    history = store.parent / "history.json"
    saved = project.parent / "saved-items.json"
    memory = tmp_settings / ".memory" / "memories.json"
    seeded_store = store.read_bytes()
    seeded_project = project.read_bytes()

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        denied_names = _assert_no_python(denied[0].pid)
        missing_status, missing = _post(denied_port, INTERVAL_BODY, "")
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        status, refused = _post(denied_port, INTERVAL_BODY, TOKEN)
        assert status == 409
        assert refused == NOT_OWNED
        assert not history.exists()
        assert not saved.exists()
        assert not memory.exists()
        assert store.read_bytes() == seeded_store
        assert project.read_bytes() == seeded_project
        print(
            "RUN_SKILL_INTERVAL_PRE denied=409 history=absent "
            f"process={','.join(denied_names)}"
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
        status, payload = _post(owner_port, INTERVAL_BODY, TOKEN)
        assert status == 200
        held_id = _assert_hold(payload)
        assert not saved.exists()
        assert not memory.exists()
        assert not (tmp_settings / ".traces").exists()
        assert store.read_bytes() == seeded_store
        assert project.read_bytes() == seeded_project
        held_history = history.read_bytes()
        assert held_id.encode("utf-8") in held_history
        assert REASON.encode("utf-8") in held_history
        assert NOT_EXECUTED.encode("utf-8") not in held_history
        assert b"interval_not_due" not in held_history
        print(
            "RUN_SKILL_INTERVAL hold=requires_confirmation "
            f"reason={REASON} saved=absent memory=absent "
            f"process={','.join(owner_names)}"
        )

        status, payload = _post(owner_port, INTERVAL_BODY, TOKEN)
        assert status == 200
        again_id = _assert_hold(payload)
        assert again_id != held_id
        assert not saved.exists()
        assert not memory.exists()
        assert store.read_bytes() == seeded_store
        assert project.read_bytes() == seeded_project
        history_bytes = history.read_bytes()
        stored = json.loads(history_bytes)
        stored_runs = stored["runs"]
        assert isinstance(stored_runs, list)
        assert len(stored_runs) == 2
        assert {item["runId"] for item in stored_runs} == {held_id, again_id}
        assert all(item["status"] == "requires_confirmation" for item in stored_runs)
        assert all(item["logs"] == [REASON] for item in stored_runs)
        assert NOT_EXECUTED.encode("utf-8") not in history_bytes
        assert b"interval_not_due" not in history_bytes
        print(
            "RUN_SKILL_INTERVAL_AGAIN status=requires_confirmation "
            f"reason={REASON} verdict=needs_confirmation "
            f"saved=absent memory=absent process={','.join(owner_names)}"
        )
    finally:
        _stop(owner)

    later_port = _free_port()
    later = _serve(tmp_settings, _child_env(tmp_settings, later_port, ""), later_port, "later")
    try:
        later_names = _assert_no_python(later[0].pid)
        status, refused = _post(later_port, INTERVAL_BODY, TOKEN)
        assert status == 409
        assert refused == NOT_OWNED
        assert history.read_bytes() == history_bytes
        assert not saved.exists()
        assert not memory.exists()
        assert project.read_bytes() == seeded_project
        assert store.read_bytes() == seeded_store
        print(
            "RUN_SKILL_INTERVAL_DENIED status=409 code=NATIVE_AUTOMATION_WRITE_NOT_OWNED "
            f"history=unchanged process={','.join(later_names)}"
        )
    finally:
        _stop(later)
