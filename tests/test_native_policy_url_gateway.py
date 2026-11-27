"""Native gateway URL policy.

``POST /policy/url`` allows a public https URL and denies loopback, a
missing URL, and a file URL. Python ``evaluate_url_safety`` is not the
owner of those decisions.
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(port: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/policy/url",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    parsed = json.loads(raw.decode("utf-8"))
    assert isinstance(parsed, dict)
    return status, parsed


def _decision_id(body: dict[str, Any]) -> str:
    decision_id = body["decision_id"]
    assert isinstance(decision_id, str)
    assert decision_id.startswith("pd_")
    return decision_id


def test_policy_url_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_url(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        raise AssertionError("python url safety entered")

    monkeypatch.setattr(tool_policy, "evaluate_url_safety", _refuse_python_url)
    port = _free_port()
    log_path = tmp_settings / "gateway-policy-url.log"
    log_handle = log_path.open("wb")
    env = os.environ.copy()
    env.pop("AUTH_DISABLED", None)
    env["AUTH_TOKEN"] = "policy-url-token"
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    process = subprocess.Popen(
        [str(NATIVE_GATEWAY)],
        cwd=str(NATIVE_GATEWAY.parent),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
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
        assert listening, log_path.read_text(encoding="utf-8", errors="replace")
        status, allowed = _post(
            port,
            {"url": "https://example.com", "trace_id": "trace-policy-allow"},
        )
        assert status == 200
        assert allowed["allowed"] is True
        assert allowed["code"] == "allowed"
        assert allowed["reason"] == "policy checks passed"
        assert allowed["capability"] == "NetworkFetch"
        assert allowed["risk_level"] == "High"
        assert allowed["trace_id"] == "trace-policy-allow"
        allow_id = _decision_id(allowed)
        loop_status, blocked = _post(
            port,
            {"url": "http://127.0.0.1/", "trace_id": "trace-policy-loopback"},
        )
        assert loop_status == 200
        assert blocked["allowed"] is False
        assert blocked["code"] == "localhost_blocked"
        assert blocked["reason"] == "private or local ip is not allowed: 127.0.0.1"
        assert blocked["capability"] == "NetworkFetch"
        assert blocked["risk_level"] == "High"
        assert blocked["trace_id"] == "trace-policy-loopback"
        assert _decision_id(blocked) != allow_id
        missing_status, missing = _post(port, {})
        assert missing_status == 200
        assert missing["allowed"] is False
        assert missing["code"] == "invalid_policy_request"
        assert missing["reason"] == "url is required"
        assert missing["capability"] == "ReadFile"
        assert missing["risk_level"] == "High"
        assert "trace_id" not in missing
        file_status, file_body = _post(port, {"url": "file:///etc/passwd"})
        assert file_status == 200
        assert file_body["allowed"] is False
        assert file_body["code"] == "unsupported_scheme"
        assert file_body["reason"] == "scheme not allowed: file"
        assert file_body["capability"] == "NetworkFetch"
        assert file_body["risk_level"] == "High"
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
