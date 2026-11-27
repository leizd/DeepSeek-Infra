"""Native gateway reads of a malformed automation history file.

``GET /api/automation/auto_keep/runs`` on the prebuilt gateway returns an
empty run list for ``{``, ``[]``, ``{"runs": {}}``, and a directory named
``history.json``. Invalid UTF-8 is 500. ``POST`` on that path is 405.
None of those requests rewrite the seeded bytes or create
``automations.json``. Python ``list_runs`` is the oracle for the shapes
it can read. The gateway process is not Python.
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
LIST_PATH = "/api/automation/auto_keep/runs"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
SERVER_ERROR = {"error": "Server error", "code": "internal"}
EMPTY = {"ok": True, "runs": []}
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


def _exchange(port: int, method: str, token: str) -> tuple[int, str, bytes]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{LIST_PATH}",
        data=b"" if method == "POST" else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            allow = response.headers.get("Allow", "")
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        allow = exc.headers.get("Allow", "")
        payload = exc.read()
    return status, allow, payload


def _json_body(payload: bytes) -> dict[str, Any]:
    parsed = json.loads(payload.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed


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
    log_path = tmp_settings / "gateway-automation-run-history-shape.log"
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


def _oracle() -> dict[str, Any]:
    return {"ok": True, "runs": list_runs(automation_id="auto_keep")}


def test_malformed_history_reads_do_not_rewrite_the_file(
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

    history = tmp_settings / ".automation" / "history.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        names = _assert_no_python(served[0].pid)
        shapes = (b"{", b"[]", b'{"runs":{}}')
        for seeded in shapes:
            history.write_bytes(seeded)
            missing_status, _allow, missing_body = _exchange(port, "GET", "")
            assert missing_status == 401
            assert _json_body(missing_body) == UNAUTHORIZED
            assert history.read_bytes() == seeded
            status, _allow, payload = _exchange(port, "GET", TOKEN)
            assert status == 200
            assert history.read_bytes() == seeded
            assert _json_body(payload) == EMPTY
            assert _json_body(payload) == _oracle()
            assert history.read_bytes() == seeded
        print(
            "RUN_HISTORY_SHAPE brace=200 array=200 object=200 "
            f"history=unchanged process={','.join(names)}"
        )

        bad_utf8 = bytes([0xFF, 0xFE, 0x7B])
        history.write_bytes(bad_utf8)
        status, _allow, payload = _exchange(port, "GET", TOKEN)
        assert status == 500
        assert _json_body(payload) == SERVER_ERROR
        assert history.read_bytes() == bad_utf8
        with pytest.raises(UnicodeDecodeError):
            list_runs(automation_id="auto_keep")
        assert history.read_bytes() == bad_utf8
        print(
            "RUN_HISTORY_UTF8 status=500 code=internal bytes=unchanged "
            f"process={','.join(names)}"
        )

        history.unlink()
        history.mkdir()
        status, _allow, payload = _exchange(port, "GET", TOKEN)
        assert status == 200
        assert history.is_dir()
        assert _json_body(payload) == EMPTY
        assert _json_body(payload) == _oracle()
        assert history.is_dir()
        assert not (history.parent / "automations.json").exists()
        post_status, allow, post_body = _exchange(port, "POST", TOKEN)
        assert post_status == 405
        assert allow == "GET,HEAD"
        assert post_body == b""
        assert history.is_dir()
        assert not (history.parent / "automations.json").exists()
        print(
            "RUN_HISTORY_DIR status=200 dir=kept post=405 allow=GET,HEAD "
            f"process={','.join(names)}"
        )
    finally:
        _stop(served)
