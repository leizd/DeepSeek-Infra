"""Native gateway data_transform tool.

``POST /mcp`` ``tools/call`` ``data_transform`` is served by the prebuilt
gateway. ``number_summary`` returns the shipped statistics. An unknown
operation and an empty regex pattern are tool errors. A missing token
is 401. Python's MCP router, message handler, and ``data_transform``
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
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "mcp-transform-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
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


def _call(request_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "data_transform", "arguments": arguments},
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
    log_path = tmp_settings / "gateway-mcp-transform.log"
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


def _result(body: dict[str, Any], request_id: str) -> dict[str, Any]:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert "error" not in body
    assert body["result"]["isError"] is False
    content = body["result"]["structuredContent"]
    assert content["ok"] is True
    assert content["tool"] == "data_transform"
    result = content["result"]
    assert isinstance(result, dict)
    return result


def _assert_refused(body: dict[str, Any], request_id: str, message: str) -> None:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert "error" not in body
    assert body["result"]["isError"] is True
    content = body["result"]["structuredContent"]
    assert content["ok"] is False
    assert content["tool"] == "data_transform"
    assert content["code"] == "invalid_payload"
    assert content["error"] == message
    assert "result" not in content


def test_mcp_transform_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    def _refuse_python_transform(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python data_transform entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    monkeypatch.setattr(tool_runtime, "data_transform", _refuse_python_transform)
    summary = _call("mcp-summary", {"operation": "number_summary", "input": "1 2 3 4"})
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, summary)
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        wrong_status, wrong = _post(port, summary, "not-the-mcp-token")
        assert wrong_status == 401
        assert wrong == UNAUTHORIZED

        summary_status, summary_body = _post(port, summary, TOKEN)
        assert summary_status == 200
        stats = _result(summary_body, "mcp-summary")
        assert stats["operation"] == "number_summary"
        assert stats["label"] == "numbers"
        assert stats["count"] == 4
        assert stats["min"] == 1.0
        assert stats["max"] == 4.0
        assert stats["sum"] == 10.0
        assert stats["mean"] == 2.5
        assert stats["median"] == 2.5

        empty = _call("mcp-empty-numbers", {"operation": "number_summary", "input": "no digits here"})
        empty_status, empty_body = _post(port, empty, TOKEN)
        assert empty_status == 200
        empty_stats = _result(empty_body, "mcp-empty-numbers")
        assert empty_stats["operation"] == "number_summary"
        assert empty_stats["label"] == "numbers"
        assert empty_stats["count"] == 0
        assert "min" not in empty_stats

        unknown = _call("mcp-unknown-op", {"operation": "nope", "input": "x"})
        unknown_status, unknown_body = _post(port, unknown, TOKEN)
        assert unknown_status == 200
        _assert_refused(unknown_body, "mcp-unknown-op", "Unsupported data_transform operation")

        blank = _call("mcp-blank-regex", {"operation": "extract_regex", "input": "abc", "pattern": ""})
        blank_status, blank_body = _post(port, blank, TOKEN)
        assert blank_status == 200
        _assert_refused(blank_body, "mcp-blank-regex", "Regex pattern is required")
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
