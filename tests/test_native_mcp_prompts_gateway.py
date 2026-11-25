"""Native gateway MCP prompts/get.

``POST /mcp`` ``prompts/get`` is served by the prebuilt gateway. The
bearer token returns the shipped slide and research prompts. An empty
name, an unknown prompt, and non-object arguments are JSON-RPC errors.
A missing token is 401. Python's MCP router and message handler are
not the owners of those HTTP responses.
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
TOKEN = "mcp-prompts-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
SLIDES_DESCRIPTION = "围绕一个主题规划 6-10 页演示文稿大纲，并调用 create_pptx 生成真实的 .pptx 文件。"
RESEARCH_DESCRIPTION = "用 web_search / fetch_url 检索一个主题，输出带 [^Wn] 引用标记的事实简报。"
SLIDES_TEXT = (
    "请为主题「季度回顾」规划一份 6-10 页的演示文稿大纲。目标听众：工程团队。"
    "每页一个结论式标题，3-6 个 lead：detail 形式的要点，并为关键页选择 "
    "cards/process/comparison/summary 版式；随后调用 create_pptx 工具生成真实的 .pptx 文件并返回下载链接。"
)
RESEARCH_TEXT = (
    "请围绕主题「Rust 网关」做联网调研：先用 web_search 检索（必要时 fetch_url 读取关键页面），"
    "然后输出一份事实简报：核心结论、关键事实（每条后跟来源的 [^Wn] 引用标记）、不确定之处。"
)
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


def _get(request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": "prompts/get", "params": params}


def _post(port: int, body: dict[str, Any], token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
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
    log_path = tmp_settings / "gateway-mcp-prompts.log"
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


def _assert_prompt(body: dict[str, Any], request_id: str, description: str, text: str) -> None:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert "error" not in body
    assert body["result"]["description"] == description
    message = body["result"]["messages"]
    assert len(message) == 1
    assert message[0]["role"] == "user"
    assert message[0]["content"] == {"type": "text", "text": text}


def _assert_error(
    body: dict[str, Any],
    request_id: str,
    message: str,
    data: dict[str, str] | None = None,
) -> None:
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    assert body["error"]["code"] == -32602
    assert body["error"]["message"] == message
    if data is None:
        assert "data" not in body["error"]
    else:
        assert body["error"]["data"] == data
    assert "result" not in body


def test_mcp_prompts_use_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_router(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp router entered")

    def _refuse_python_message(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python mcp message entered")

    monkeypatch.setattr(mcp_routes, "create_mcp_router", _refuse_python_router)
    monkeypatch.setattr(mcp_server, "handle_mcp_message", _refuse_python_message)
    slides = _get(
        "mcp-slides",
        {"name": "slides-outline", "arguments": {"topic": "季度回顾", "audience": "工程团队"}},
    )
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, slides)
        assert missing_status == 401
        assert missing == UNAUTHORIZED

        slides_status, slides_body = _post(port, slides, TOKEN)
        assert slides_status == 200
        _assert_prompt(slides_body, "mcp-slides", SLIDES_DESCRIPTION, SLIDES_TEXT)

        research = _get(
            "mcp-research",
            {"name": "research-brief", "arguments": {"topic": "Rust 网关"}},
        )
        research_status, research_body = _post(port, research, TOKEN)
        assert research_status == 200
        _assert_prompt(research_body, "mcp-research", RESEARCH_DESCRIPTION, RESEARCH_TEXT)

        empty = _get("mcp-empty-name", {"name": "  "})
        empty_status, empty_body = _post(port, empty, TOKEN)
        assert empty_status == 200
        _assert_error(empty_body, "mcp-empty-name", "name is required")

        unknown = _get("mcp-missing-prompt", {"name": "missing-prompt"})
        unknown_status, unknown_body = _post(port, unknown, TOKEN)
        assert unknown_status == 200
        _assert_error(unknown_body, "mcp-missing-prompt", "Prompt not found", {"code": "not_found"})

        bad_args = _get("mcp-bad-args", {"name": "slides-outline", "arguments": ["topic"]})
        bad_status, bad_body = _post(port, bad_args, TOKEN)
        assert bad_status == 200
        _assert_error(bad_body, "mcp-bad-args", "arguments must be an object")
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
