"""Native gateway memory-store ownership.

``POST /api/memory`` and ``GET /api/memory`` are served by the prebuilt
gateway. While ``DEEPSEEK_RUNTIME_MODE`` is unset, a bearer add is
refused and ``.memory`` stays absent. After the mode is
``python_disabled``, the same add persists one memory and the list
returns it. A later non-owner delete is refused and the stored bytes
stay unchanged. A missing token is 401. Python's memory router and
store writers are not the owners of those HTTP responses.
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

from deepseek_infra.infra.data import memory as legacy_memory
from deepseek_infra.infra.memory import store as memory_store
from deepseek_infra.web.routes import memory as memory_routes

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "memory-owner-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
NOT_OWNED = {
    "error": "The memory store is still written by the Python runtime, so this gateway refuses to mutate it.",
    "code": "NATIVE_MEMORY_WRITE_NOT_OWNED",
}
ADD = {"action": "add", "content": "likes dark mode", "category": "preference"}
CLEARED = (
    "AUTH_TOKEN",
    "AUTH_DISABLED",
    "DEEPSEEK_RUNTIME_MODE",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    port: int,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str = "",
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
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
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    if mode:
        env["DEEPSEEK_RUNTIME_MODE"] = mode
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int, label: str) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / f"gateway-memory-owner-{label}.log"
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


def test_memory_add_is_owned_by_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python memory router entered")

    def _refuse_python_add(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python add_memory entered")

    def _refuse_python_save(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python save_memories entered")

    def _refuse_python_upsert(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python upsert_memory entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(memory_routes, "create_memory_router", _refuse_python_router)
    monkeypatch.setattr(memory_store, "add_memory", _refuse_python_add)
    monkeypatch.setattr(legacy_memory, "save_memories", _refuse_python_save)
    monkeypatch.setattr(legacy_memory, "upsert_memory", _refuse_python_upsert)
    memory_file = tmp_settings / ".memory" / "memories.json"
    generation = tmp_settings / ".workspace-generation"

    denied_port = _free_port()
    denied = _serve(tmp_settings, _child_env(tmp_settings, denied_port, ""), denied_port, "denied")
    try:
        missing_status, missing = _request(denied_port, "POST", "/api/memory", ADD)
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        assert not memory_file.exists()

        status, payload = _request(denied_port, "POST", "/api/memory", ADD, TOKEN)
        assert status == 409
        assert payload == NOT_OWNED
        assert not memory_file.exists()
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
        status, payload = _request(owner_port, "POST", "/api/memory", ADD, TOKEN)
        assert status == 200
        assert payload["ok"] is True
        memory = payload["memory"]
        assert memory["content"] == "likes dark mode"
        assert memory["type"] == "preference"
        assert memory["category"] == "preference"
        assert memory["scope"] == "global"
        assert memory["legacyScope"] == "global"
        memory_id = memory["memoryId"]
        assert isinstance(memory_id, str)
        assert len(memory_id) == 20
        assert memory["id"] == memory_id

        listed_status, listed = _request(owner_port, "GET", "/api/memory", token=TOKEN)
        assert listed_status == 200
        rows = listed["memories"]
        assert isinstance(rows, list)
        assert len(rows) == 1
        assert rows[0]["memoryId"] == memory_id
        assert rows[0]["content"] == "likes dark mode"
        stored = memory_file.read_bytes()
        assert b"likes dark mode" in stored
        assert generation.read_text(encoding="utf-8").strip() == "4"
    finally:
        _stop(owner)

    stored_after_add = memory_file.read_bytes()
    generation_after_add = generation.read_bytes()
    refused_port = _free_port()
    refused = _serve(tmp_settings, _child_env(tmp_settings, refused_port, ""), refused_port, "refused")
    try:
        status, payload = _request(
            refused_port,
            "POST",
            "/api/memory",
            {"action": "delete", "query": "likes dark mode"},
            TOKEN,
        )
        assert status == 409
        assert payload == NOT_OWNED
        assert memory_file.read_bytes() == stored_after_add
        assert generation.read_bytes() == generation_after_add
    finally:
        _stop(refused)
