"""Native gateway local RAG status.

``GET /api/rag/status`` is served by the prebuilt gateway. Auth enabled
refuses a missing token. The configured bearer token returns the read-only
index snapshot. A missing database stays missing. ``LOCAL_RAG_ENABLED``
false still returns ``ok`` and does not create the database. Python's
status router and local RAG status helper are not the owners of those
HTTP reads.
"""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.rag import local_rag
from deepseek_infra.web.routes import status as status_routes

ROOT = Path(__file__).resolve().parents[1]
NATIVE_GATEWAY = Path(
    r"D:\CodexNativeVerification\native-20261006\desktop-platform-next\bin\deepseek-gateway.exe"
)
TOKEN = "rag-status-token"
RAG_ENV = (
    "LOCAL_RAG_ENABLED",
    "LOCAL_RAG_BACKEND",
    "LOCAL_RAG_EMBEDDING_PROVIDER",
    "LOCAL_RAG_ONNX_MODEL_PATH",
    "LOCAL_RAG_TOKENIZER_PATH",
    "LOCAL_RAG_INCREMENTAL",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(port: int, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/rag/status",
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


def _child_env(tmp_settings: Path, port: int, *, enabled: str | None) -> dict[str, str]:
    env = os.environ.copy()
    for name in ("AUTH_TOKEN", "AUTH_DISABLED", "DEEPSEEK_RUNTIME_MODE", *RAG_ENV):
        env.pop(name, None)
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env["AUTH_TOKEN"] = TOKEN
    if enabled is not None:
        env["LOCAL_RAG_ENABLED"] = enabled
    return env


def _serve(tmp_settings: Path, env: dict[str, str], port: int) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = tmp_settings / "gateway-rag-status.log"
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


def _assert_snapshot(body: dict[str, Any], database: Path, *, enabled: bool) -> None:
    assert body["ok"] is True
    rag = body["localRag"]
    assert isinstance(rag, dict)
    assert rag["enabled"] is enabled
    assert rag["backend"] == "sqlite_vec"
    assert Path(str(rag["databasePath"])) == database
    assert rag["sqliteVecAvailable"] is False
    assert rag["embeddingProvider"] == "hash"
    assert rag["embeddingProviderRequested"] == "hash"
    assert rag["embeddingDimensions"] == 64
    assert rag["embeddingModelPathConfigured"] is False
    assert rag["tokenizerPathConfigured"] is False
    assert rag["hybridSearch"] == "bm25+vector"
    assert math.isclose(rag["bm25K1"], 1.5, abs_tol=1e-9)
    assert math.isclose(rag["bm25B"], 0.75, abs_tol=1e-9)
    assert rag["incremental"] is True
    assert rag["lastError"] == ""
    assert rag["indexedItems"] == 0
    assert rag["indexedFiles"] == 0
    assert rag["indexedMedia"] == 0
    assert rag["indexedMemories"] == 0
    assert rag["vectorTableAvailable"] is False


def test_rag_status_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_status(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python status router entered")

    def _refuse_python_rag(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("python local rag status entered")

    assert NATIVE_GATEWAY.is_file()
    database = tmp_settings / ".local-rag" / "rag.sqlite3"
    assert not database.exists()
    monkeypatch.setattr(status_routes, "create_status_router", _refuse_python_status)
    monkeypatch.setattr(local_rag, "status", _refuse_python_rag)
    port = _free_port()
    served = _serve(tmp_settings, _child_env(tmp_settings, port, enabled=None), port)
    try:
        missing_status, missing = _get(port)
        assert missing_status == 401
        assert missing == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        wrong_status, wrong = _get(port, "not-the-rag-status-token")
        assert wrong_status == 401
        assert wrong == {"error": {"code": "UNAUTHORIZED", "message": "Auth required"}}
        ok_status, ok_body = _get(port, TOKEN)
        assert ok_status == 200
        _assert_snapshot(ok_body, database, enabled=True)
        assert not database.exists()
    finally:
        served[0].kill()
        served[0].wait(timeout=10)
        served[1].close()

    disabled_port = _free_port()
    disabled = _serve(tmp_settings, _child_env(tmp_settings, disabled_port, enabled="false"), disabled_port)
    try:
        disabled_status, disabled_body = _get(disabled_port, TOKEN)
        assert disabled_status == 200
        _assert_snapshot(disabled_body, database, enabled=False)
        assert not database.exists()
        assert not database.parent.exists()
    finally:
        disabled[0].kill()
        disabled[0].wait(timeout=10)
        disabled[1].close()
