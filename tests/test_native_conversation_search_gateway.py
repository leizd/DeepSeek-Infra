"""Native gateway conversation search.

``POST /api/conversations/search`` is served by the prebuilt gateway. Auth
enabled refuses a missing token. An empty query returns no results before
the conversation list is checked. A non-list ``conversations`` field is
refused. A title hit and a message hit return those conversations. Python's
chat router and search helpers are not the owners of those HTTP reads.
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

from deepseek_infra.web import server as web_server
from deepseek_infra.web.routes import chat as chat_routes

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
TOKEN = "conversation-search-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(port: int, body: dict[str, Any], token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/conversations/search",
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
    for name in ("AUTH_TOKEN", "AUTH_DISABLED", "DEEPSEEK_RUNTIME_MODE"):
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-conversation-search.log"
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


def test_conversation_search_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_chat(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python chat router entered")

    def _refuse_python_search(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python conversation search entered")

    monkeypatch.setattr(chat_routes, "create_chat_router", _refuse_python_chat)
    monkeypatch.setattr(web_server, "conversation_search", _refuse_python_search)
    monkeypatch.setattr(web_server, "conversation_search_matches", _refuse_python_search)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _post(port, {"query": "beta", "conversations": []})
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        wrong_status, wrong = _post(
            port,
            {"query": "beta", "conversations": []},
            "not-the-search-token",
        )
        assert wrong_status == 401
        assert wrong == UNAUTHORIZED
        empty_status, empty = _post(port, {"query": "   ", "conversations": "not-a-list"}, TOKEN)
        assert empty_status == 200
        assert empty == {"results": []}
        blank_status, blank = _post(port, {"query": ""}, TOKEN)
        assert blank_status == 200
        assert blank == {"results": []}
        refused_status, refused = _post(port, {"query": "beta", "conversations": {"id": "nope"}}, TOKEN)
        assert refused_status == 400
        assert refused == {"error": "conversations must be a list", "code": "invalid_payload"}
        hit_status, hit = _post(
            port,
            {
                "query": "  BETA  ",
                "conversations": [
                    "skip-me",
                    {
                        "id": "conv-1",
                        "title": "Beta marker",
                        "updatedAt": 7,
                        "favorite": True,
                        "tags": ["notes"],
                        "messages": [],
                    },
                    {
                        "id": "conv-msg",
                        "title": "Other",
                        "updatedAt": None,
                        "favorite": False,
                        "tags": [],
                        "messages": [
                            {
                                "id": "m1",
                                "role": "user",
                                "content": "see BETA-MARKER here",
                                "reasoning": "",
                            }
                        ],
                    },
                ],
            },
            TOKEN,
        )
        assert hit_status == 200
        assert hit == {
            "results": [
                {
                    "id": "conv-1",
                    "title": "Beta marker",
                    "updatedAt": 7,
                    "favorite": True,
                    "tags": ["notes"],
                    "matches": [
                        {"kind": "title", "messageId": "", "snippet": "Beta marker"},
                    ],
                },
                {
                    "id": "conv-msg",
                    "title": "Other",
                    "updatedAt": None,
                    "favorite": False,
                    "tags": [],
                    "matches": [
                        {
                            "kind": "message",
                            "messageId": "m1",
                            "snippet": "user see BETA-MARKER here",
                        }
                    ],
                },
            ]
        }
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
