"""Native gateway run when history.json is a directory.

``POST /api/automation/auto_keep/run`` with no automation registry returns
404 ``Automation not found``. The ``history.json`` directory stays a
directory, and ``automations.json`` is not created. A missing token is
401. The gateway process is not Python.
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


def _post(port: int, token: str) -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{RUN_PATH}",
        data=b"",
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
    log_path = tmp_settings / "gateway-automation-run-history-dir.log"
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


def _assert_untouched(history: Path) -> None:
    assert history.is_dir()
    assert not (history.parent / "automations.json").exists()
    root = history.parent.parent
    assert not (root / ".memory").exists()
    assert not (root / ".projects").exists()


def test_run_against_a_history_directory_is_not_found(
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
    history.mkdir(parents=True)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        names = _assert_no_python(served[0].pid)
        missing_status, missing = _post(port, "")
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        _assert_untouched(history)

        status, body = _post(port, TOKEN)
        assert status == 404
        assert body == NOT_FOUND
        _assert_untouched(history)
        print(
            "RUN_HISTORY_DIR_POST missing=401 status=404 code=not_found "
            f"dir=kept registry=absent process={','.join(names)}"
        )
    finally:
        _stop(served)
