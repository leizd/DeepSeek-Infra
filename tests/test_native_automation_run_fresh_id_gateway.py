"""Native gateway normalises a missing run id only in the response.

A history row with an empty ``runId`` is returned as ``auto_run_`` plus
16 lowercase hex characters. A second GET mints a different id. The
seeded ``history.json`` bytes stay unchanged, including after a missing
token. Python ``list_runs`` may mint its own id. It is not the writer,
and the gateway process is not Python.
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
from deepseek_infra.infra.automation.history import list_runs
from deepseek_infra.infra.workspace.schema import timestamp_ms_to_iso
from deepseek_infra.web.routes import automation as automation_routes

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "automation-run-owner-token"
LIST_PATH = "/api/automation/auto_keep/runs"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
SEEDED = (
    b'{"runs":[{"automationId":"auto_keep","status":"success",'
    b'"finishedAtMs":4000,"startedAt":"","finishedAt":""}]}'
)
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


def _get(port: int, token: str) -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{LIST_PATH}",
        headers=headers,
        method="GET",
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
    for name in CLEARED:
        env.pop(name, None)
    env.update(AUTOMATION_ENV)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-automation-run-fresh-id.log"
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


def _run_id_suffix(run_id: str) -> str:
    assert run_id.startswith("auto_run_")
    suffix = run_id.removeprefix("auto_run_")
    assert len(suffix) == 16
    assert suffix == suffix.lower()
    assert all(character in "0123456789abcdef" for character in suffix)
    return suffix


def _assert_normalised(run: dict[str, Any]) -> str:
    run_id = run["runId"]
    assert isinstance(run_id, str)
    _run_id_suffix(run_id)
    started = run["startedAtMs"]
    assert isinstance(started, int)
    assert not isinstance(started, bool)
    assert abs(int(time.time() * 1000) - started) < 15_000
    assert str(run["startedAt"]).endswith("Z")
    assert run["finishedAtMs"] == 4000
    assert run["finishedAt"] == timestamp_ms_to_iso(4000)
    assert run["durationMs"] == 0
    assert run["status"] == "success"
    assert run["automationId"] == "auto_keep"
    assert run["projectId"] == ""
    assert run["trigger"] == {"type": "manual"}
    return run_id


def test_empty_run_id_is_normalised_only_in_the_response(
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

    history = tmp_settings / ".automation" / "history.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_bytes(SEEDED)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        names = _assert_no_python(served[0].pid)
        missing_status, missing = _get(port, "")
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        assert history.read_bytes() == SEEDED

        status, first_body = _get(port, TOKEN)
        assert status == 200
        assert first_body["ok"] is True
        assert history.read_bytes() == SEEDED
        first_runs = first_body["runs"]
        assert isinstance(first_runs, list)
        assert len(first_runs) == 1
        first_id = _assert_normalised(first_runs[0])

        status, second_body = _get(port, TOKEN)
        assert status == 200
        assert second_body["ok"] is True
        assert history.read_bytes() == SEEDED
        second_runs = second_body["runs"]
        assert isinstance(second_runs, list)
        assert len(second_runs) == 1
        second_id = _assert_normalised(second_runs[0])
        assert second_id != first_id
        assert not (history.parent / "automations.json").exists()

        oracle_runs = list_runs(automation_id="auto_keep")
        assert history.read_bytes() == SEEDED
        assert len(oracle_runs) == 1
        oracle_id = oracle_runs[0]["runId"]
        assert isinstance(oracle_id, str)
        _run_id_suffix(oracle_id)
        assert oracle_id not in {first_id, second_id}
        assert oracle_runs[0]["finishedAt"] == timestamp_ms_to_iso(4000)
        assert oracle_runs[0]["finishedAtMs"] == 4000
        print(
            "RUN_FRESH_ID first=auto_run second=different finishedAtMs=4000 "
            f"history=unchanged process={','.join(names)}"
        )
        print(
            "RUN_FRESH_ID_DENIED missing=401 history=unchanged "
            f"process={','.join(names)}"
        )
    finally:
        _stop(served)
