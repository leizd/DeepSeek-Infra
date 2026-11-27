"""Native gateway automation-run ownership.

``POST /api/automation/auto_keep/run`` is served by the prebuilt
gateway. A due ``save_item`` run writes the project saved-item
store. While ``DEEPSEEK_RUNTIME_MODE`` is unset, that post is
refused and the seeded automation file, ``project.json``, history,
and saved items stay unchanged. After the mode is
``python_disabled``, the 2026-10-04 12:34 schedule trigger returns
``success`` and persists a saved item titled ``Kept note``. A later
non-owner post is refused and those bytes stay unchanged. A missing
token is 401. Python's automation runner is not the owner of those
HTTP responses.
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
TOKEN = "automation-run-owner-token"
RUN_PATH = "/api/automation/auto_keep/run"
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
BODY = b'{"trigger":{"type":"schedule","cron":"34 12 4 10 *"},"now":"2026-10-04T12:34:00+00:00"}'
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


def _request(port: int, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{RUN_PATH}",
        data=BODY,
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
    log_path = tmp_settings / f"gateway-automation-run-{label}.log"
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


def _seed(tmp_settings: Path) -> tuple[Path, Path, Path, Path]:
    store = tmp_settings / ".automation" / "automations.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(SEEDED_AUTOMATION.encode("utf-8"))
    project = tmp_settings / ".projects" / "proj_ok" / "project.json"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_bytes(SEEDED_PROJECT.encode("utf-8"))
    history = tmp_settings / ".automation" / "history.json"
    saved = project.parent / "saved-items.json"
    return store, project, history, saved


def _optional_bytes(path: Path) -> bytes | None:
    if path.exists():
        return path.read_bytes()
    return None


def test_automation_run_is_owned_by_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python run_once entered")

    def _refuse_python_save(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python save_item_action entered")

    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python create_automation_router entered")

    monkeypatch.setattr(automation_runner, "run_once", _refuse_python_run)
    monkeypatch.setattr(automation_actions, "save_item_action", _refuse_python_save)
    monkeypatch.setattr(automation_routes, "create_automation_router", _refuse_python_router)
    store, project, history, saved = _seed(tmp_settings)
    generation = tmp_settings / ".workspace-generation"
    memories = tmp_settings / ".memory" / "memories.json"
    seeded_store = store.read_bytes()
    seeded_project = project.read_bytes()
    assert seeded_store == SEEDED_AUTOMATION.encode("utf-8")
    assert seeded_project == SEEDED_PROJECT.encode("utf-8")

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        missing_status, missing = _request(denied_port)
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        assert store.read_bytes() == seeded_store
        assert project.read_bytes() == seeded_project

        status, payload = _request(denied_port, TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert store.read_bytes() == seeded_store
        assert project.read_bytes() == seeded_project
        assert not history.exists()
        assert not saved.exists()
        assert not memories.exists()
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
        status, payload = _request(owner_port, TOKEN)
        assert status == 200
        assert payload["ok"] is True
        run = payload["run"]
        assert isinstance(run, dict)
        assert run["status"] == "success"
        assert run["skippedReason"] == ""
        assert run["error"] == ""
        assert run["traceId"] == ""
        assert run["logs"] == ["saveItem"]
        assert run["automationId"] == "auto_keep"
        run_id = run["runId"]
        assert isinstance(run_id, str)
        assert run_id.startswith("auto_run_")
        run_suffix = run_id.removeprefix("auto_run_")
        assert len(run_suffix) == 16
        assert run_suffix == run_suffix.lower()
        assert all(character in "0123456789abcdef" for character in run_suffix)

        stored_items = json.loads(saved.read_text(encoding="utf-8"))
        assert isinstance(stored_items, dict)
        items = stored_items["items"]
        assert isinstance(items, list)
        assert len(items) == 1
        item = items[0]
        assert item["title"] == "Kept note"
        assert item["content"] == "hello from automation"
        assert item["type"] == "assistant_answer"
        assert item["purpose"] == "reference"
        assert item["tags"] == ["automation"]
        assert item["projectId"] == "proj_ok"
        saved_id = item["savedId"]
        assert isinstance(saved_id, str)
        assert saved_id.startswith("save_")
        saved_suffix = saved_id.removeprefix("save_")
        assert len(saved_suffix) == 16
        assert saved_suffix == saved_suffix.lower()
        assert all(character in "0123456789abcdef" for character in saved_suffix)
        assert run["outputs"]["savedItemIds"] == [saved_id]
        assert run_id.encode("ascii") in history.read_bytes()
        assert store.read_bytes() == seeded_store
        updated_project = json.loads(project.read_text(encoding="utf-8"))
        assert updated_project["id"] == "proj_ok"
        assert updated_project["name"] == "Demo"
        assert isinstance(updated_project["updatedAt"], int)
        assert not isinstance(updated_project["updatedAt"], bool)
        assert updated_project["updatedAt"] > 10
    finally:
        _stop(owner)

    stored_after_run = store.read_bytes()
    project_after_run = project.read_bytes()
    history_after_run = history.read_bytes()
    saved_after_run = saved.read_bytes()
    generation_after_run = _optional_bytes(generation)
    memories_after_run = _optional_bytes(memories)
    refused_port = _free_port()
    refused = _serve(tmp_settings, _child_env(tmp_settings, refused_port, ""), refused_port, "refused")
    try:
        status, payload = _request(refused_port, TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert store.read_bytes() == stored_after_run
        assert project.read_bytes() == project_after_run
        assert history.read_bytes() == history_after_run
        assert saved.read_bytes() == saved_after_run
        assert _optional_bytes(generation) == generation_after_run
        assert _optional_bytes(memories) == memories_after_run
    finally:
        _stop(refused)
