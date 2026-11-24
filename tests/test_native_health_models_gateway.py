"""Native gateway liveness, readiness, and model catalog.

``GET /healthz`` and ``GET /readyz`` do not require a bearer token.
``GET /v1/models`` does when auth is enabled. No API key leaves the
provider unconfigured. The catalog lists the two shipped models. Python's
health helpers and chat router are not the owners of those HTTP reads.
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

from deepseek_infra.infra.observability import health
from deepseek_infra.web.routes import chat as chat_routes

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "health-models-token"
UNAUTHORIZED = {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, path: str, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
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


def _child_env(tmp_settings: Path, port: int, *, auth_disabled: bool, api_key: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("AUTH_TOKEN", "AUTH_DISABLED", "DEEPSEEK_RUNTIME_MODE", "DEEPSEEK_API_KEY"):
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    if auth_disabled:
        env["AUTH_DISABLED"] = "1"
    else:
        env["AUTH_TOKEN"] = TOKEN
    if api_key:
        env["DEEPSEEK_API_KEY"] = api_key
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-health-models.log"
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


def _assert_catalog(body: dict[str, Any]) -> None:
    assert body["object"] == "list"
    assert "error" not in body
    data = body["data"]
    assert isinstance(data, list)
    assert [item["id"] for item in data] == ["deepseek-v4-pro", "deepseek-v4-flash"]
    now = int(time.time())
    for item in data:
        assert item["object"] == "model"
        assert item["owned_by"] == "deepseek-infra"
        created = item["created"]
        assert isinstance(created, int)
        assert 0 < created <= now + 5


def test_health_ready_and_models_use_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_health(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python health probe entered")

    def _refuse_python_chat(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python chat router entered")

    assert NATIVE_GATEWAY.is_file()
    monkeypatch.setattr(health, "healthz", _refuse_python_health)
    monkeypatch.setattr(health, "readyz", _refuse_python_health)
    monkeypatch.setattr(chat_routes, "create_chat_router", _refuse_python_chat)
    port = _free_port()
    served = _serve(
        tmp_settings,
        _child_env(tmp_settings, port, auth_disabled=False, api_key=""),
        port,
    )
    try:
        health_status, health_body = _get(port, "/healthz")
        assert health_status == 200
        assert health_body == {
            "ok": True,
            "service": "deepseek-gateway-rs",
            "status": "ok",
            "version": "4.8.0",
            "runtime": "local",
            "provider": "deepseek",
            "auth_enabled": True,
        }
        ready_status, ready_body = _get(port, "/readyz")
        assert ready_status == 200
        assert ready_body == {
            "status": "ready",
            "version": "4.8.0",
            "checks": {"tracing": "disabled", "model_provider": "unconfigured"},
        }
        missing_status, missing = _get(port, "/v1/models")
        assert missing_status == 401
        assert missing == UNAUTHORIZED
        catalog_status, catalog = _get(port, "/v1/models", TOKEN)
        assert catalog_status == 200
        _assert_catalog(catalog)
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()

    open_port = _free_port()
    opened = _serve(
        tmp_settings,
        _child_env(tmp_settings, open_port, auth_disabled=True, api_key="probe-not-a-real-key"),
        open_port,
    )
    try:
        open_health_status, open_health = _get(open_port, "/healthz")
        assert open_health_status == 200
        assert open_health["auth_enabled"] is False
        assert open_health["ok"] is True
        open_ready_status, open_ready = _get(open_port, "/readyz")
        assert open_ready_status == 200
        assert open_ready["checks"]["model_provider"] == "configured"
        open_models_status, open_models = _get(open_port, "/v1/models")
        assert open_models_status == 200
        _assert_catalog(open_models)
    finally:
        opened[0].kill()
        opened[0].wait(timeout=10)
        opened[1].close()
