"""Native gateway MCP resources, prompts, and protocol refusals.

``POST /mcp`` is served by the prebuilt gateway. The bearer token lists
and reads the runtime capability resource, lists the two shipped
prompts, and accepts a notification with HTTP 202. An empty resource
URI and an unknown method are JSON-RPC errors. A missing token is 401.
Python's MCP router and message handler are not the owners of those
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

from deepseek_infra.infra.mcp import server as mcp_server
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
TOKEN = "mcp-resources-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
LIST = {"jsonrpc": "2.0", "id": "mcp-resources", "method": "resources/list"}
READ = {
    "jsonrpc": "2.0",
    "id": "mcp-read",
    "method": "resources/read",
    "params": {"uri": "runtime://capabilities"},
}
EMPTY_URI = {
    "jsonrpc": "2.0",
    "id": "mcp-empty-uri",
    "method": "resources/read",
    "params": {"uri": ""},
}
PROMPTS = {"jsonrpc": "2.0", "id": "mcp-prompts", "method": "prompts/list"}
UNKNOWN = {"jsonrpc": "2.0", "id": "mcp-unknown-method", "method": "nope/method"}
NOTICE = {"jsonrpc": "2.0", "method": "notifications/initialized"}
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


def _request(port: int, body: dict[str, Any], token: str = "") -> tuple[int, bytes]:
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
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(port: int, body: dict[str, Any], token: str = "") -> tuple[int, dict[str, Any]]:
    status, payload = _request(port, body, token)
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
    log_path = tmp_settings / "gateway-mcp-resources.log"
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


def test_mcp_resources_use_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, LIST)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        list_status, listed = _post(port, LIST, TOKEN)
        assert list_status == 200
        assert listed["jsonrpc"] == "2.0"
        assert listed["id"] == "mcp-resources"
        assert "error" not in listed
        resources = listed["result"]["resources"]
        runtime = next(item for item in resources if item["uri"] == "runtime://capabilities")
        assert runtime["name"] == "runtime-capabilities"
        assert runtime["title"] == "DeepSeek Infra tool policy & capability profiles"
        assert runtime["mimeType"] == "application/json"
        if not (tmp_settings / ".generated").exists():
            assert resources == [runtime]

        read_status, read = _post(port, READ, TOKEN)
        assert read_status == 200
        assert read["jsonrpc"] == "2.0"
        assert read["id"] == "mcp-read"
        assert "error" not in read
        contents = read["result"]["contents"]
        assert len(contents) == 1
        assert contents[0]["uri"] == "runtime://capabilities"
        assert contents[0]["mimeType"] == "application/json"
        document = json.loads(contents[0]["text"])
        assert document == {"capability": "full", "toolPolicy": {"capability": "full"}}

        empty_status, empty = _post(port, EMPTY_URI, TOKEN)
        assert empty_status == 200
        assert empty["jsonrpc"] == "2.0"
        assert empty["id"] == "mcp-empty-uri"
        assert empty["error"]["code"] == -32602
        assert empty["error"]["message"] == "uri is required"
        assert "result" not in empty

        prompt_status, prompts = _post(port, PROMPTS, TOKEN)
        assert prompt_status == 200
        assert prompts["jsonrpc"] == "2.0"
        assert prompts["id"] == "mcp-prompts"
        assert "error" not in prompts
        names = [item["name"] for item in prompts["result"]["prompts"]]
        assert names == ["slides-outline", "research-brief"]
        assert prompts["result"]["prompts"][0]["title"] == "生成 PPT 大纲并落成文件"
        assert prompts["result"]["prompts"][1]["title"] == "联网检索并输出带引用的简报"

        unknown_status, unknown = _post(port, UNKNOWN, TOKEN)
        assert unknown_status == 200
        assert unknown["jsonrpc"] == "2.0"
        assert unknown["id"] == "mcp-unknown-method"
        assert unknown["error"]["code"] == -32601
        assert unknown["error"]["message"] == "Method not found: nope/method"
        assert "result" not in unknown

        notice_status, notice_body = _request(port, NOTICE, TOKEN)
        assert notice_status == 202
        assert notice_body == b""
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
