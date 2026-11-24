"""Native gateway second automation memory write.

After a due ``save_item`` success, a bodyless manual post on the same
owner process saves a second ``Kept note`` and keeps one memory sentence.
``source.runId`` moves to the new run id. A later process with
``DEEPSEEK_RUNTIME_MODE`` unset refuses another post with
``NATIVE_AUTOMATION_WRITE_NOT_OWNED`` and leaves those memory bytes
unchanged. Python's automation runner is not the owner of those HTTP
responses.
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
SENTENCE = "Automation Keep notes completed successfully with 1 saved items."
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


def _post(port: int, body: bytes | None, token: str) -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{RUN_PATH}",
        data=b"" if body is None else body,
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
    log_path = tmp_settings / f"gateway-automation-run-memory-replay-{label}.log"
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


def _sentences(value: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        if value.get("content") == SENTENCE:
            found.append(value)
        for child in value.values():
            _sentences(child, found)
    elif isinstance(value, list):
        for child in value:
            _sentences(child, found)


def _one_source(memory_file: Path) -> dict[str, Any]:
    stored = json.loads(memory_file.read_text(encoding="utf-8"))
    found: list[dict[str, Any]] = []
    _sentences(stored, found)
    assert len(found) == 1
    source = found[0]["source"]
    assert isinstance(source, dict)
    return source


def test_second_success_updates_the_same_memory_row(
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
    memory_file = tmp_settings / ".memory" / "memories.json"
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
        status, payload = _post(owner_port, DUE_BODY, TOKEN)
        assert status == 200
        assert payload["ok"] is True
        first = payload["run"]
        assert isinstance(first, dict)
        assert first["status"] == "success"
        first_id = first["runId"]
        assert isinstance(first_id, str)
        assert _one_source(memory_file)["runId"] == first_id

        status, payload = _post(owner_port, None, TOKEN)
        assert status == 200
        assert payload["ok"] is True
        second = payload["run"]
        assert isinstance(second, dict)
        assert second["status"] == "success"
        assert second["trigger"] == {"type": "manual"}
        assert second["logs"] == ["saveItem"]
        assert second["traceId"] == ""
        assert second["automationId"] == "auto_keep"
        second_id = second["runId"]
        assert isinstance(second_id, str)
        assert second_id != first_id
        assert second_id.startswith("auto_run_")
        suffix = second_id.removeprefix("auto_run_")
        assert len(suffix) == 16
        assert suffix == suffix.lower()
        assert all(character in "0123456789abcdef" for character in suffix)
        started = second["startedAtMs"]
        assert isinstance(started, int)
        assert not isinstance(started, bool)
        assert abs(int(time.time() * 1000) - started) < 15_000

        source = _one_source(memory_file)
        assert source["runId"] == second_id
        assert source["kind"] == "automation"
        assert source["automationId"] == "auto_keep"
        assert source["runId"] != first_id
        items = json.loads(saved.read_text(encoding="utf-8"))["items"]
        assert isinstance(items, list)
        assert len(items) == 2
        assert [item["title"] for item in items] == ["Kept note", "Kept note"]
        assert items[0]["savedId"] != items[1]["savedId"]
        assert store.read_bytes() == seeded_store
        assert not (tmp_settings / ".traces").exists()
        memory_after_second = memory_file.read_bytes()
        print(
            "RUN_MEMORY_REPLAY count=1 source=moved saved=2 "
            f"process={','.join(owner_names)}"
        )
    finally:
        _stop(owner)

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        denied_names = _assert_no_python(denied[0].pid)
        status, refused = _post(denied_port, None, TOKEN)
        assert status == 409
        assert refused == NOT_OWNED
        assert memory_file.read_bytes() == memory_after_second
        assert len(json.loads(saved.read_text(encoding="utf-8"))["items"]) == 2
        assert store.read_bytes() == seeded_store
        print(
            "RUN_MEMORY_REPLAY_DENIED status=409 "
            "code=NATIVE_AUTOMATION_WRITE_NOT_OWNED memory=unchanged "
            f"process={','.join(denied_names)}"
        )
    finally:
        _stop(denied)
