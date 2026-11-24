"""Native gateway data_transform csv_summary.

``POST /mcp`` ``tools/call`` ``data_transform`` ``csv_summary`` is served
by the prebuilt gateway. Input ``name,value`` / ``x,1`` / ``y,2.5``
returns two rows, those columns, and one numeric column. An empty input
returns zero rows and no columns. A missing token is 401. Python's MCP
router, message handler, and ``data_transform`` are not the owners of
those HTTP responses.
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
TOKEN = "mcp-csv-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
TABLE = "name,value\nx,1\ny,2.5\n"
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


def _call(request_id: str, text: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "data_transform",
            "arguments": {"operation": "csv_summary", "input": text},
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
    log_path = tmp_settings / "gateway-mcp-csv.log"
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


def test_mcp_csv_summary_uses_the_native_gateway(
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
    table = _call("mcp-csv", TABLE)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, table)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        table_status, table_body = _post(port, table, TOKEN)
        assert table_status == 200
        assert table_body["jsonrpc"] == "2.0"
        assert table_body["id"] == "mcp-csv"
        assert "error" not in table_body
        assert table_body["result"]["isError"] is False
        content = table_body["result"]["structuredContent"]
        assert content["ok"] is True
        assert content["tool"] == "data_transform"
        result = content["result"]
        assert result["operation"] == "csv_summary"
        assert result["rows"] == 2
        assert result["columns"] == ["name", "value"]
        assert len(result["numericColumns"]) == 1
        column = result["numericColumns"][0]
        assert column["label"] == "value"
        assert column["count"] == 2
        assert column["sum"] == 3.5

        empty = _call("mcp-empty-csv", "")
        empty_status, empty_body = _post(port, empty, TOKEN)
        assert empty_status == 200
        assert empty_body["jsonrpc"] == "2.0"
        assert empty_body["id"] == "mcp-empty-csv"
        assert "error" not in empty_body
        assert empty_body["result"]["isError"] is False
        empty_content = empty_body["result"]["structuredContent"]
        assert empty_content["ok"] is True
        assert empty_content["tool"] == "data_transform"
        empty_result = empty_content["result"]
        assert empty_result["operation"] == "csv_summary"
        assert empty_result["rows"] == 0
        assert empty_result["columns"] == []
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
