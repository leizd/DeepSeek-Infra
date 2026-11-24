"""Native gateway automation run history.

The owner process writes a due ``save_item`` run and then a
``schedule_not_due`` skip. ``GET /api/automation/auto_keep/runs`` returns
both run objects. A missing token, an invalid id, and a non-integer
limit do not rewrite the history or the saved-item file. A later process
with ``DEEPSEEK_RUNTIME_MODE`` unset still lists those runs and refuses
another post with ``NATIVE_AUTOMATION_WRITE_NOT_OWNED``. Python's
automation runner is not the owner of those HTTP responses.
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
LIST_PATH = "/api/automation/auto_keep/runs"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
NOT_OWNED = {
    "error": "The automation store is still written by the Python runtime, so this gateway refuses to mutate it.",
    "code": "NATIVE_AUTOMATION_WRITE_NOT_OWNED",
}
SEEDED_AUTOMATION = (
    '{"automations":[{"automationId":"auto_keep","name":"Keep notes","enabled":true,'
    '"projectId":"proj_ok","condition":{"type":"always"},'
    '"action":{"type":"save_item","title":"Kept note","content":"hello from automation"}}]}'
)
SEEDED_PROJECT = '{"id":"proj_ok","name":"Demo","createdAt":10,"updatedAt":10}\n'
DUE_BODY = b'{"trigger":{"type":"schedule","cron":"34 12 4 10 *"},"now":"2026-10-04T12:34:00+00:00"}'
NOT_DUE_BODY = b'{"trigger":{"type":"schedule","cron":"0 0 * * *"},"now":"2026-10-04T15:00:00+00:00"}'
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


def _exchange(port: int, method: str, path: str, body: bytes | None, token: str) -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
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
    env.update(AUTOMATION_ENV)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    if mode:
        env["DEEPSEEK_RUNTIME_MODE"] = mode
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int, label: str) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / f"gateway-automation-run-list-{label}.log"
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


def test_owner_runs_are_listed_and_reads_do_not_rewrite_history(
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
    seeded_store = store.read_bytes()

    owner_port = _free_port()
    owner = _serve(
        tmp_settings,
        _child_env(tmp_settings, owner_port, "python_disabled"),
        owner_port,
        "owner",
    )
    try:
        owner_names = _assert_no_python(owner[0].pid)
        status, payload = _exchange(owner_port, "POST", RUN_PATH, DUE_BODY, TOKEN)
        assert status == 200
        assert payload["ok"] is True
        success = payload["run"]
        assert isinstance(success, dict)
        assert success["status"] == "success"
        assert success["logs"] == ["saveItem"]
        assert success["automationId"] == "auto_keep"
        items = json.loads(saved.read_text(encoding="utf-8"))["items"]
        assert len(items) == 1
        assert items[0]["title"] == "Kept note"
        saved_after_run = saved.read_bytes()
        project_after_run = project.read_bytes()
        assert store.read_bytes() == seeded_store

        status, payload = _exchange(owner_port, "POST", RUN_PATH, NOT_DUE_BODY, TOKEN)
        assert status == 200
        assert payload["ok"] is True
        skipped = payload["run"]
        assert isinstance(skipped, dict)
        assert skipped["status"] == "skipped"
        assert skipped["skippedReason"] == "schedule_not_due"
        assert skipped["logs"] == ["schedule_not_due"]
        assert skipped["error"] == ""
        assert skipped["automationId"] == "auto_keep"
        assert skipped["runId"] != success["runId"]
        assert saved.read_bytes() == saved_after_run
        assert project.read_bytes() == project_after_run
        assert store.read_bytes() == seeded_store
        history_after_runs = history.read_bytes()

        status, listed = _exchange(owner_port, "GET", LIST_PATH, None, TOKEN)
        assert status == 200
        assert listed["ok"] is True
        runs = listed["runs"]
        assert isinstance(runs, list)
        assert len(runs) == 2
        by_id = {run["runId"]: run for run in runs}
        assert by_id[success["runId"]] == success
        assert by_id[skipped["runId"]] == skipped
        assert runs[0]["startedAtMs"] >= runs[1]["startedAtMs"]
        assert history.read_bytes() == history_after_runs
        assert saved.read_bytes() == saved_after_run
        assert project.read_bytes() == project_after_run
        assert store.read_bytes() == seeded_store

        missing_status, missing = _exchange(owner_port, "GET", LIST_PATH, None, "")
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        bad_status, bad_id = _exchange(owner_port, "GET", "/api/automation/no/runs", None, TOKEN)
        assert bad_status == 400
        assert bad_id == {"error": "Invalid automation id", "code": "invalid_payload"}
        limit_status, bad_limit = _exchange(
            owner_port,
            "GET",
            "/api/automation/auto_keep/runs?limit=abc",
            None,
            TOKEN,
        )
        assert limit_status == 500
        assert bad_limit == {"error": "Server error", "code": "internal"}
        assert history.read_bytes() == history_after_runs
        assert saved.read_bytes() == saved_after_run
        assert project.read_bytes() == project_after_run
        assert store.read_bytes() == seeded_store
        print(
            "RUN_LISTED count=2 success=success skipped=schedule_not_due "
            f"order=desc process={','.join(owner_names)}"
        )
        print(
            "RUN_LIST_DENIED missing=401 bad_id=400 bad_limit=500 history=unchanged"
        )
    finally:
        _stop(owner)

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        denied_names = _assert_no_python(denied[0].pid)
        status, listed = _exchange(denied_port, "GET", LIST_PATH, None, TOKEN)
        assert status == 200
        assert listed["ok"] is True
        assert [run["runId"] for run in listed["runs"]] == [skipped["runId"], success["runId"]] or [
            run["runId"] for run in listed["runs"]
        ] == [success["runId"], skipped["runId"]]
        by_id = {run["runId"]: run for run in listed["runs"]}
        assert by_id[success["runId"]] == success
        assert by_id[skipped["runId"]] == skipped

        status, refused = _exchange(denied_port, "POST", RUN_PATH, b"", TOKEN)
        assert status == 409
        assert refused == NOT_OWNED
        assert history.read_bytes() == history_after_runs
        assert saved.read_bytes() == saved_after_run
        assert project.read_bytes() == project_after_run
        assert store.read_bytes() == seeded_store
        print(
            "RUN_LIST_NOT_OWNED status=409 code=NATIVE_AUTOMATION_WRITE_NOT_OWNED "
            f"history=unchanged process={','.join(denied_names)}"
        )
    finally:
        _stop(denied)
