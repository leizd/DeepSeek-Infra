"""Native gateway automation run list limits.

``GET /api/automation/{id}/runs`` on the prebuilt gateway matches Python
``list_runs`` for an empty store and for seeded history. A missing limit,
``0``, a negative limit, ``2001``, ``1_0``, ``+2``, and a padded id are
covered. The last 2000 stored records are the only ones a negative limit
can return, so an older ``auto_keep`` row behind 2000 ``auto_other`` rows
is absent. The history file bytes stay unchanged. Python ``list_runs`` is
the oracle. The gateway process is not Python.
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
from urllib.parse import quote

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
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
SERVER_ERROR = {"error": "Server error", "code": "internal"}
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


def _runs_path(automation_id: str, limit: str | None, extra: str = "") -> str:
    path = "/api/automation/" + quote(automation_id, safe="") + "/runs"
    query: list[str] = []
    if limit is not None:
        query.append("limit=" + quote(limit, safe=""))
    if extra:
        query.append(extra)
    if not query:
        return path
    return path + "?" + "&".join(query)


def _get(port: int, path: str, token: str) -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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
    log_path = tmp_settings / "gateway-automation-run-limits.log"
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


def _run(index: int, automation_id: str, started_at_ms: int) -> dict[str, Any]:
    return {
        "runId": f"run_{index:04}",
        "automationId": automation_id,
        "projectId": "proj_ok",
        "status": "success",
        "startedAtMs": started_at_ms,
        "finishedAtMs": started_at_ms,
        "startedAt": "kept",
        "finishedAt": "kept",
    }


def _write_history(path: Path, runs: list[dict[str, Any]]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"runs": runs}, separators=(",", ":")).encode("utf-8")
    path.write_bytes(payload)
    return payload


def _route_limit(raw: str | None) -> int:
    if raw is None or raw == "":
        return 100
    return int(raw)


def _oracle(automation_id: str, raw: str | None) -> dict[str, Any]:
    runs = list_runs(automation_id=automation_id, limit=_route_limit(raw))
    return {"ok": True, "runs": runs}


def test_seeded_history_limit_shapes_match_list_runs(
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
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        names = _assert_no_python(served[0].pid)
        missing_status, missing = _get(port, _runs_path("auto_keep", None), "")
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        assert not (tmp_settings / ".automation").exists()

        status, empty = _get(port, _runs_path("auto_keep", None), TOKEN)
        assert status == 200
        assert empty == {"ok": True, "runs": []}
        assert not (tmp_settings / ".automation").exists()

        seeded = [_run(index, "auto_keep", index + 1) for index in range(150)]
        seeded_bytes = _write_history(history, seeded)
        cases = (
            ("auto_keep", None, 100, "run_0149", "run_0050"),
            ("auto_keep", "", 100, "run_0149", "run_0050"),
            ("auto_keep", "0", 100, "run_0149", "run_0050"),
            ("auto_keep", "-1", 150, "run_0149", "run_0000"),
            ("auto_keep", "2001", 150, "run_0149", "run_0000"),
            ("auto_keep", "1_0", 10, "run_0149", "run_0140"),
            ("auto_keep", "+2", 2, "run_0149", "run_0148"),
            (" auto_keep ", None, 100, "run_0149", "run_0050"),
        )
        for automation_id, raw_limit, count, first_id, last_id in cases:
            before = history.read_bytes()
            assert before == seeded_bytes
            status, body = _get(port, _runs_path(automation_id, raw_limit), TOKEN)
            assert status == 200
            assert history.read_bytes() == seeded_bytes
            expected = _oracle(automation_id, raw_limit)
            assert history.read_bytes() == seeded_bytes
            assert body == expected
            runs = body["runs"]
            assert isinstance(runs, list)
            assert len(runs) == count
            assert runs[0]["runId"] == first_id
            assert runs[-1]["runId"] == last_id

        ignored_status, ignored = _get(
            port,
            _runs_path("auto_keep", "-1", "projectId=other&status=failed"),
            TOKEN,
        )
        assert ignored_status == 200
        assert history.read_bytes() == seeded_bytes
        assert ignored == _oracle("auto_keep", "-1")
        assert len(ignored["runs"]) == 150

        other_status, other = _get(port, _runs_path("auto_other", "10"), TOKEN)
        assert other_status == 200
        assert other == {"ok": True, "runs": []}
        limit_status, bad_limit = _get(port, _runs_path("auto_keep", "abc"), TOKEN)
        assert limit_status == 500
        assert bad_limit == SERVER_ERROR
        assert history.read_bytes() == seeded_bytes
        assert not (history.parent / "automations.json").exists()
        print(
            "RUN_LIMITS empty=0 zero=100 negative=150 cap=150 underscore=10 plus=2 "
            f"padded=100 bad_limit=500 history=unchanged process={','.join(names)}"
        )

        window = [_run(0, "auto_keep", 1)]
        window[0]["runId"] = "run_drop"
        window.extend(_run(index, "auto_other", index + 1) for index in range(2000))
        window_bytes = _write_history(history, window)
        keep_status, keep = _get(port, _runs_path("auto_keep", "-1"), TOKEN)
        assert keep_status == 200
        assert history.read_bytes() == window_bytes
        assert keep == _oracle("auto_keep", "-1")
        assert keep["runs"] == []
        other_status, retained = _get(port, _runs_path("auto_other", "-1"), TOKEN)
        assert other_status == 200
        assert history.read_bytes() == window_bytes
        assert retained == _oracle("auto_other", "-1")
        retained_runs = retained["runs"]
        assert isinstance(retained_runs, list)
        assert len(retained_runs) == 2000
        assert retained_runs[0]["runId"] == "run_1999"
        assert retained_runs[-1]["runId"] == "run_0000"
        assert all(run["automationId"] == "auto_other" for run in retained_runs)
        assert not (history.parent / "automations.json").exists()
        print(
            "RUN_LIMIT_WINDOW keep=0 other=2000 history=unchanged "
            f"process={','.join(names)}"
        )
    finally:
        _stop(served)
