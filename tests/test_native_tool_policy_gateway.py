"""Native gateway tool-policy status.

``GET /api/tool-policy`` is served by the prebuilt gateway. Auth enabled
refuses a missing token. The configured bearer token returns the enabled
policy, ``enforceSchema`` false, the tool catalog, and the audit tail read
from the isolated root. A non-numeric ``limit`` still returns ``ok`` true.
Python's status router and tool-policy helpers are not the owners of those
HTTP reads.
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

from deepseek_infra.infra.tool_runtime import tool_policy
from deepseek_infra.web.routes import status as status_routes

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "tool-policy-token"
AUDIT_ENTRY = {"probe": "tool-policy-gateway"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, token: str = "", query: str = "") -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tool-policy{query}",
        headers=headers,
        method="GET",
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
    for name in (
        "AUTH_TOKEN",
        "AUTH_DISABLED",
        "DEEPSEEK_RUNTIME_MODE",
        "TOOL_POLICY_ENABLED",
        "TOOL_POLICY_ENFORCE_SCHEMA",
        "TOOL_POLICY_REQUIRE_CONFIRM",
        "TOOL_POLICY_SANITIZE_RESULTS",
        "TOOL_POLICY_AUDIT_ENABLED",
    ):
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-tool-policy.log"
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


def _assert_policy(body: dict[str, Any]) -> None:
    assert body["ok"] is True
    policy = body["toolPolicy"]
    assert isinstance(policy, dict)
    assert policy["enabled"] is True
    assert policy["enforceSchema"] is False
    tools = policy["tools"]
    assert isinstance(tools, list)
    assert len(tools) >= 28
    assert tools[0]["name"] == "web_search"
    names = [tool["name"] for tool in tools]
    assert "browser_close_session" in names
    audit = body["audit"]
    assert isinstance(audit, list)
    assert AUDIT_ENTRY in audit


def test_tool_policy_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_status(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python status router entered")

    def _refuse_python_policy(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python tool policy entered")

    assert NATIVE_GATEWAY.is_file()
    audit_dir = tmp_settings / ".tool-audit"
    audit_dir.mkdir()
    audit_log = audit_dir / "audit.jsonl"
    audit_log.write_text(json.dumps(AUDIT_ENTRY, ensure_ascii=False) + "\n", encoding="utf-8")
    before = audit_log.read_bytes()
    monkeypatch.setattr(status_routes, "create_status_router", _refuse_python_status)
    monkeypatch.setattr(tool_policy, "tool_policy_status", _refuse_python_policy)
    monkeypatch.setattr(tool_policy, "read_recent_audit", _refuse_python_policy)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port), port)
    try:
        missing_status, missing = _get(port)
        assert missing_status == 401
        assert missing == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        wrong_status, wrong = _get(port, "not-the-tool-policy-token")
        assert wrong_status == 401
        assert wrong == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        ok_status, ok_body = _get(port, TOKEN)
        assert ok_status == 200
        _assert_policy(ok_body)
        limited_status, limited = _get(port, TOKEN, "?limit=not-a-number")
        assert limited_status == 200
        _assert_policy(limited)
        assert audit_log.read_bytes() == before
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()
