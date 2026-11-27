"""Native gateway list_project_files.

``POST /mcp`` ``tools/call`` ``list_project_files`` is served by the
prebuilt gateway. An empty root lists nothing and does not create
``.projects``. A seeded project is returned for its id and for a
padded id. A missing id and a too-short id are refused. The seeded
``project.json`` bytes stay unchanged. A missing token is 401.
Python's MCP router, message handler, and ``list_project_files_tool``
are not the owners of those HTTP responses.
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

from deepseek_infra.infra.mcp import server as mcp_server
from deepseek_infra.infra.tool_runtime import tools as tool_runtime
from deepseek_infra.web.routes import mcp as mcp_routes

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
TOKEN = "mcp-project-files-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
PROJECT_ID = "proj"
FILE_ID = "a" * 32
SEEDED = {
    "name": "Notes",
    "documents": [
        {
            "name": "notes.txt",
            "fileId": FILE_ID,
            "projectId": PROJECT_ID,
            "kind": "text",
            "preview": "hello",
            "pageCount": 1,
            "charCount": 5,
            "chunkCount": 1,
        }
    ],
}
EXPECTED_FILE = {
    "name": "notes.txt",
    "fileId": FILE_ID,
    "projectId": PROJECT_ID,
    "kind": "text",
    "pageCount": 1,
    "charCount": 5,
    "chunkCount": 1,
    "preview": "hello",
}
CLEARED = (
    "AUTH_TOKEN",
    "AUTH_DISABLED",
    "DEEPSEEK_RUNTIME_MODE",
    "MCP_CAPABILITY",
    "MCP_EXPOSE_RESOURCES",
    "MCP_EXPOSE_PROMPTS",
    "MCP_ENABLED",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _call(rpc_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": "list_project_files", "arguments": arguments},
    }


def _post(port: int, body: dict[str, Any], token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(body).encode("utf-8"),
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
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-mcp-list-project-files.log"
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


def _success(payload: dict[str, Any], rpc_id: str) -> dict[str, Any]:
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == rpc_id
    assert "error" not in payload
    assert payload["result"]["isError"] is False
    content = payload["result"]["structuredContent"]
    assert content["ok"] is True
    assert content["tool"] == "list_project_files"
    result = content["result"]
    assert isinstance(result, dict)
    return result


def _failure(payload: dict[str, Any], rpc_id: str) -> dict[str, Any]:
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == rpc_id
    assert "error" not in payload
    assert payload["result"]["isError"] is True
    content = payload["result"]["structuredContent"]
    assert content["ok"] is False
    assert content["tool"] == "list_project_files"
    assert "result" not in content
    return content


def test_mcp_list_project_files_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    def _refuse_python_list(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python list_project_files entered")

    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    monkeypatch.setattr(tool_runtime, "list_project_files_tool", _refuse_python_list)
    projects = tmp_settings / ".projects"
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        empty_body = _call("mcp-project-files-empty", {})
        missing_status, missing = _post(port, empty_body)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        status, payload = _post(port, empty_body, TOKEN)
        assert status == 200
        result = _success(payload, "mcp-project-files-empty")
        assert result["projects"] == []
        assert result["count"] == 0
        assert not projects.exists()

        project_dir = projects / PROJECT_ID
        project_dir.mkdir(parents=True)
        project_file = project_dir / "project.json"
        project_file.write_text(json.dumps(SEEDED), encoding="utf-8")
        seeded = project_file.read_bytes()
        expected = {"id": PROJECT_ID, "name": "Notes", "files": [EXPECTED_FILE]}

        status, payload = _post(port, _call("mcp-project-files-hit", {"projectId": PROJECT_ID}), TOKEN)
        assert status == 200
        result = _success(payload, "mcp-project-files-hit")
        assert result["projects"] == [expected]
        assert result["count"] == 1

        status, payload = _post(port, _call("mcp-project-files-trim", {"projectId": "  proj  "}), TOKEN)
        assert status == 200
        result = _success(payload, "mcp-project-files-trim")
        assert result["projects"] == [expected]
        assert result["count"] == 1

        status, payload = _post(port, _call("mcp-project-files-missing", {"projectId": "nope"}), TOKEN)
        assert status == 200
        failure = _failure(payload, "mcp-project-files-missing")
        assert failure["code"] == "not_found"
        assert failure["error"] == "Project not found"

        status, payload = _post(port, _call("mcp-project-files-bad", {"projectId": "x"}), TOKEN)
        assert status == 200
        failure = _failure(payload, "mcp-project-files-bad")
        assert failure["code"] == "invalid_payload"
        assert failure["error"] == "Invalid project id"

        assert project_file.read_bytes() == seeded
        assert sorted(path.name for path in projects.iterdir()) == [PROJECT_ID]
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
