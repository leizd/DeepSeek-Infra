"""Native gateway capability policy.

``POST /policy/capability`` allows a granted read and denies a missing
shell grant, a shell call over the risk limit, and a missing request.
Python ``check_capability`` is not the owner of those decisions.
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

from deepseek_infra.infra.rust_core import policy_client

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post(port: int, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/policy/capability",
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


def test_policy_capability_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_capability(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python capability policy entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(policy_client, "check_capability", _refuse_python_capability)
    port = _free_port()
    log_path = tmp_settings / "gateway-policy-capability.log"
    log_handle = log_path.open("wb")
    env = os.environ.copy()
    env.pop("AUTH_DISABLED", None)
    env["AUTH_TOKEN"] = "policy-capability-token"
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
            {
                "requested": "ReadFile",
                "granted": ["ReadFile"],
                "max_risk": "Low",
                "trace_id": "trace-policy-capability-allow",
            },
        )
        assert status == 200
        assert allowed["allowed"] is True
        assert allowed["code"] == "allowed"
        assert allowed["reason"] == "policy checks passed"
        assert allowed["capability"] == "ReadFile"
        assert allowed["risk_level"] == "Low"
        assert allowed["trace_id"] == "trace-policy-capability-allow"
        allow_id = _decision_id(allowed)
        missing_grant_status, missing_grant = _post(
            port,
            {
                "requested": "ShellExec",
                "granted": ["ReadFile"],
                "max_risk": "Critical",
                "trace_id": "trace-policy-capability-missing",
            },
        )
        assert missing_grant_status == 200
        assert missing_grant["allowed"] is False
        assert missing_grant["code"] == "missing_capability"
        assert missing_grant["reason"] == "required capability was not granted"
        assert missing_grant["capability"] == "ShellExec"
        assert missing_grant["risk_level"] == "Critical"
        assert missing_grant["trace_id"] == "trace-policy-capability-missing"
        assert _decision_id(missing_grant) != allow_id
        risk_status, risk = _post(
            port,
            {
                "requested": "ShellExec",
                "granted": ["ShellExec"],
                "max_risk": "Low",
            },
        )
        assert risk_status == 200
        assert risk["allowed"] is False
        assert risk["code"] == "risk_limit_exceeded"
        assert risk["reason"] == "capability risk exceeds the configured limit"
        assert risk["capability"] == "ShellExec"
        assert risk["risk_level"] == "Critical"
        empty_status, empty = _post(port, {})
        assert empty_status == 200
        assert empty["allowed"] is False
        assert empty["code"] == "invalid_policy_request"
        assert empty["reason"] == "requested capability is invalid"
        assert empty["capability"] == "ReadFile"
        assert empty["risk_level"] == "High"
        assert "trace_id" not in empty
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
