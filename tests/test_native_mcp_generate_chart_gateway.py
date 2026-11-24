"""Native gateway generate_chart.

``POST /mcp`` ``tools/call`` ``generate_chart`` is served by the prebuilt
gateway. A line chart titled ``Revenue`` returns the shipped markdown
table. An empty data list and a point with no label are tool errors. A
missing token is 401. Python's MCP router, message handler, and
``generate_chart`` are not the owners of those HTTP responses.
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
TOKEN = "mcp-chart-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
MARKDOWN = "| label | value |\n|---|---:|\n| Q1 | 1.0 |\n| Q2 | 2.5 |"
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


def _call(request_id: str, data: Any, chart_type: str = "line", title: str = "Revenue") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "generate_chart",
            "arguments": {"type": chart_type, "title": title, "data": data},
        },
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
    log_path = tmp_settings / "gateway-mcp-chart.log"
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


def _refusal(body: dict[str, Any], request_id: str, message: str) -> None:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert "error" not in body
    assert body["result"]["isError"] is True
    refused = body["result"]["structuredContent"]
    assert refused["ok"] is False
    assert refused["tool"] == "generate_chart"
    assert refused["code"] == "invalid_payload"
    assert refused["error"] == message
    assert "result" not in refused


def test_mcp_generate_chart_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    def _refuse_python_chart(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python generate_chart entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    monkeypatch.setattr(tool_runtime, "generate_chart", _refuse_python_chart)
    chart = _call(
        "mcp-chart",
        [{"label": "Q1", "value": 1}, {"label": "Q2", "value": 2.5}],
    )
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, chart)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        chart_status, chart_body = _post(port, chart, TOKEN)
        assert chart_status == 200
        assert chart_body["jsonrpc"] == "2.0"
        assert chart_body["id"] == "mcp-chart"
        assert "error" not in chart_body
        assert chart_body["result"]["isError"] is False
        content = chart_body["result"]["structuredContent"]
        assert content["ok"] is True
        assert content["tool"] == "generate_chart"
        result = content["result"]
        assert result["type"] == "line"
        assert result["title"] == "Revenue"
        assert result["data"][0]["label"] == "Q1"
        assert result["data"][0]["value"] == 1.0
        assert result["data"][1]["label"] == "Q2"
        assert result["data"][1]["value"] == 2.5
        assert result["markdownTable"] == MARKDOWN

        empty = _call("mcp-empty-chart", [])
        empty_status, empty_body = _post(port, empty, TOKEN)
        assert empty_status == 200
        _refusal(empty_body, "mcp-empty-chart", "Chart data must be a non-empty list")

        useless = _call("mcp-useless-chart", [{"value": 1}])
        useless_status, useless_body = _post(port, useless, TOKEN)
        assert useless_status == 200
        _refusal(useless_body, "mcp-useless-chart", "Chart data has no valid points")
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
