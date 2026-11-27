"""Native gateway python_eval successes and refusals.

``POST /mcp`` ``tools/call`` is served by the prebuilt gateway. The
bearer token evaluates factorial, arithmetic, and ``math.sqrt`` and
refuses an empty expression, a whitespace expression, a 1001-character
expression, ``__import__``, and an unknown tool. A missing token is
401. Python's MCP router, message handler, and ``python_eval`` are not
the owners of those HTTP responses.
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
TOKEN = "mcp-eval-token"
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


def _call(request_id: str, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


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
    log_path = tmp_settings / "gateway-mcp-eval.log"
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


def _assert_value(body: dict[str, Any], request_id: str, expression: str, result: str) -> None:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert "error" not in body
    assert body["result"]["isError"] is False
    content = body["result"]["structuredContent"]
    assert content["ok"] is True
    assert content["tool"] == "python_eval"
    assert content["result"]["expression"] == expression
    assert content["result"]["result"] == result


def _assert_refused(
    body: dict[str, Any],
    request_id: str,
    tool: str,
    message: str,
    code: str = "invalid_payload",
) -> None:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert "error" not in body
    assert body["result"]["isError"] is True
    content = body["result"]["structuredContent"]
    assert content["ok"] is False
    assert content["tool"] == tool
    assert content["code"] == code
    assert content["error"] == message
    assert "result" not in content


def test_mcp_eval_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    def _refuse_python_eval(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python python_eval entered")

    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    monkeypatch.setattr(tool_runtime, "python_eval", _refuse_python_eval)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        locked = _call("mcp-locked", "python_eval", {"expression": "factorial(6)"})
        missing_status, missing = _post(port, locked)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        factorial_status, factorial = _post(port, locked, TOKEN)
        assert factorial_status == 200
        _assert_value(factorial, "mcp-locked", "factorial(6)", "720")

        added = _call("mcp-add", "python_eval", {"expression": "1 + 2 * 3"})
        added_status, added_body = _post(port, added, TOKEN)
        assert added_status == 200
        _assert_value(added_body, "mcp-add", "1 + 2 * 3", "7")

        rooted = _call("mcp-sqrt", "python_eval", {"expression": "math.sqrt(4)"})
        rooted_status, rooted_body = _post(port, rooted, TOKEN)
        assert rooted_status == 200
        _assert_value(rooted_body, "mcp-sqrt", "math.sqrt(4)", "2.0")

        empty = _call("mcp-empty", "python_eval", {"expression": ""})
        empty_status, empty_body = _post(port, empty, TOKEN)
        assert empty_status == 200
        _assert_refused(empty_body, "mcp-empty", "python_eval", "python_eval expression is empty")

        blank = _call("mcp-blank", "python_eval", {"expression": " \n\t "})
        blank_status, blank_body = _post(port, blank, TOKEN)
        assert blank_status == 200
        _assert_refused(blank_body, "mcp-blank", "python_eval", "python_eval expression is empty")

        long_expr = "1" * 1001
        too_long = _call("mcp-long", "python_eval", {"expression": long_expr})
        long_status, long_body = _post(port, too_long, TOKEN)
        assert long_status == 200
        _assert_refused(long_body, "mcp-long", "python_eval", "python_eval expression is too long")

        blocked = _call("mcp-import", "python_eval", {"expression": "__import__('os')"})
        blocked_status, blocked_body = _post(port, blocked, TOKEN)
        assert blocked_status == 200
        _assert_refused(blocked_body, "mcp-import", "python_eval", "Function is not allowed: __import__")

        unknown = _call("mcp-unknown", "not_a_tool", {})
        unknown_status, unknown_body = _post(port, unknown, TOKEN)
        assert unknown_status == 200
        _assert_refused(
            unknown_body,
            "mcp-unknown",
            "not_a_tool",
            "Tool 'not_a_tool' was blocked by tool policy (unknown_tool)",
            code="forbidden",
        )
        unknown_content = unknown_body["result"]["structuredContent"]
        assert unknown_content["reason"] == "Unknown tool is not registered in the tool catalog"
        assert unknown_content["risk"] == "high"
        assert unknown_content["suggestion"] == "Use a tool listed by GET /api/tools or /mcp tools/list"
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
