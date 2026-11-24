"""Native gateway stores an interval skill without network as not executed.

Action ``run_skill`` with no ``allowNetwork`` and body
``{"trigger":{"type":"interval","intervalSeconds":300}}`` returns status
``failed`` and error ``run_skill is not executed by the native gateway
yet``. The same owner process posts that body again and stores
``interval_not_due``, because a failed run starts the interval clock.
A later process with ``DEEPSEEK_RUNTIME_MODE`` unset refuses another
post and leaves the history bytes unchanged. The gateway process is not
Python.
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
ERROR = "run_skill is not executed by the native gateway yet"
REASON = "scheduled_skill_network_requires_confirmation"
INTERVAL_BODY = b'{"trigger":{"type":"interval","intervalSeconds":300}}'
TRIGGER = {"type": "interval", "intervalSeconds": 300}
SEEDED_AUTOMATION = (
    '{"automations":[{"automationId":"auto_keep","name":"Keep notes","enabled":true,'
    '"projectId":"proj_ok","condition":{"type":"always"},'
    '"action":{"type":"run_skill"}}]}'
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
    log_path = tmp_settings / f"gateway-automation-run-skill-interval-off-{label}.log"
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


def test_interval_skill_without_network_starts_the_clock(
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
    assert b"allowNetwork" not in seeded_store

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
            "RUN_SKILL_INTERVAL_OFF_PRE denied=409 history=absent "
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
        assert payload["ok"] is True
        run = payload["run"]
        assert isinstance(run, dict)
        assert run["status"] == "failed"
        assert run["error"] == ERROR
        assert run["logs"] == [f"attempt 1 failed: {ERROR}"]
        assert run["skippedReason"] == ""
        assert run["attempts"] == 1
        assert run["traceId"] == ""
        assert run["trigger"] == TRIGGER
        assert run["automationId"] == "auto_keep"
        decision = run["evidence"]["policy"]
        assert isinstance(decision, dict)
        assert decision["verdict"] == "allow"
        assert decision["risk"] == "low"
        assert decision["reasons"] == []
        assert decision["policy"] == STORED_POLICY
        failed_id = _run_id(run["runId"])
        assert not saved.exists()
        assert not memory.exists()
        assert not (tmp_settings / ".traces").exists()
        assert store.read_bytes() == seeded_store
        assert project.read_bytes() == seeded_project
        failed_history = history.read_bytes()
        assert failed_id.encode("utf-8") in failed_history
        assert ERROR.encode("utf-8") in failed_history
        assert REASON.encode("utf-8") not in failed_history
        assert b"requires_confirmation" not in failed_history
        assert b"interval_not_due" not in failed_history
        print(
            "RUN_SKILL_INTERVAL_OFF status=failed error=run_skill verdict=allow "
            f"reason=absent saved=absent memory=absent process={','.join(owner_names)}"
        )

        status, payload = _post(owner_port, INTERVAL_BODY, TOKEN)
        assert status == 200
        assert payload["ok"] is True
        skipped = payload["run"]
        assert isinstance(skipped, dict)
        assert skipped["status"] == "skipped"
        assert skipped["skippedReason"] == "interval_not_due"
        assert skipped["logs"] == ["interval_not_due"]
        assert skipped["error"] == ""
        assert skipped["attempts"] == 1
        assert skipped["traceId"] == ""
        assert skipped["trigger"] == TRIGGER
        assert skipped["automationId"] == "auto_keep"
        assert skipped["evidence"] == {}
        skipped_id = _run_id(skipped["runId"])
        assert skipped_id != failed_id
        assert not saved.exists()
        assert not memory.exists()
        assert store.read_bytes() == seeded_store
        assert project.read_bytes() == seeded_project
        history_bytes = history.read_bytes()
        stored = json.loads(history_bytes)
        stored_runs = stored["runs"]
        assert isinstance(stored_runs, list)
        assert len(stored_runs) == 2
        assert {item["runId"] for item in stored_runs} == {failed_id, skipped_id}
        assert REASON.encode("utf-8") not in history_bytes
        assert b"requires_confirmation" not in history_bytes
        assert b"interval_not_due" in history_bytes
        print(
            "RUN_SKILL_INTERVAL_OFF_AGAIN status=skipped reason=interval_not_due "
            f"process={','.join(owner_names)}"
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
            "RUN_SKILL_INTERVAL_OFF_DENIED status=409 code=NATIVE_AUTOMATION_WRITE_NOT_OWNED "
            f"history=unchanged process={','.join(later_names)}"
        )
    finally:
        _stop(later)
