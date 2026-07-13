from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts import smoke_rust_sidecar as smoke


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_rust_dockerfile_is_multistage_locked_and_non_root() -> None:
    dockerfile = _read("rust/Dockerfile")

    assert "FROM rust:1.85-bookworm AS builder" in dockerfile
    assert "cargo build" in dockerfile
    assert "--locked" in dockerfile
    assert "-p deepseek-gateway" in dockerfile
    assert "FROM debian:bookworm-slim" in dockerfile
    assert "COPY rust ./rust" in dockerfile
    assert "GATEWAY_BIND_ADDR=0.0.0.0:8787" in dockerfile
    assert "USER deepseek" in dockerfile
    assert "10001" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "http://127.0.0.1:8787/healthz" in dockerfile
    assert "requirements.txt" not in dockerfile
    assert "COPY deepseek_infra" not in dockerfile
    assert "DEEPSEEK_RUST_BIND" not in dockerfile


def test_optional_compose_does_not_change_default_python_deployment() -> None:
    default_compose = _read("docker-compose.yml")
    rust_compose = _read("docker-compose.rust.yml")

    assert "deepseek-infra:" in default_compose
    assert "rust-gateway" not in default_compose
    assert "rust-gateway:" in rust_compose
    assert "dockerfile: rust/Dockerfile" in rust_compose
    assert '"127.0.0.1:8787:8787"' in rust_compose
    assert "GATEWAY_BIND_ADDR: 0.0.0.0:8787" in rust_compose
    assert "DEEPSEEK_RUST_GATEWAY=" not in rust_compose
    assert "deepseek-infra:" not in rust_compose


def test_example_environment_keeps_all_rust_components_disabled() -> None:
    env_example = _read(".env.example")

    for component in ("GATEWAY", "MCP", "POLICY", "RAG"):
        assert f"DEEPSEEK_RUST_{component}=0" in env_example
        assert f"DEEPSEEK_RUST_{component}=1" not in env_example


def test_ci_builds_and_smokes_rust_image_in_independent_job() -> None:
    workflow = _read(".github/workflows/ci.yml")

    assert "rust-docker:" in workflow
    assert "docker build -f rust/Dockerfile -t deepseek-rust-gateway:test ." in workflow
    assert "python scripts/smoke_rust_sidecar.py" in workflow
    assert "docker rm --force deepseek-rust-gateway || true" in workflow
    assert "--cov-report=json:artifacts/coverage.json" in workflow
    assert "--cov-fail-under=95" in workflow


class _SidecarHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send({"ok": True, "service": "deepseek-gateway-rs"})
            return
        if self.path == "/v1/models":
            self._send({"object": "list", "data": [{"id": "deepseek-v4-pro", "object": "model"}]})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.path == "/v1/chat/completions":
            assert request["stream"] is False
            self._send(
                {
                    "object": "chat.completion",
                    "choices": [{"message": {"role": "assistant", "content": "offline stub"}}],
                }
            )
            return
        if self.path == "/mcp/request/prepare":
            assert request["method"] == "tools/call"
            self._send(
                {
                    "ok": True,
                    "messageType": "request",
                    "request": request,
                    "routing": {"owner": "python", "category": "tools"},
                }
            )
            return
        if self.path == "/policy/url":
            assert request["url"].startswith("http://localhost")
            self._send(
                {
                    "allowed": False,
                    "code": "localhost_blocked",
                    "reason": "localhost is blocked",
                    "decision_id": "pd_test_001",
                    "capability": "NetworkFetch",
                    "risk_level": "High",
                }
            )
            return
        if self.path == "/rag/query/normalize":
            assert "语言" in request["query"]
            self._send({"normalized": "rust 语言", "tokens": ["rust", "语言"]})
            return
        if self.path == "/rag/vectors/rank":
            assert request["query"] == [1.0, 0.0]
            assert request["candidates"] == [[0.25, 0.0], [1.0, 0.0]]
            self._send({"index": 1, "similarity": 1.0})
            return
        self.send_error(404)


@pytest.fixture
def sidecar_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SidecarHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_smoke_exercises_all_offline_sidecar_contracts(sidecar_url: str) -> None:
    checks = smoke.run_smoke(sidecar_url, wait_seconds=1, timeout=1)

    assert [check.name for check in checks] == [
        "health",
        "models",
        "chat",
        "mcp_protocol_preparation",
        "policy",
        "rag",
        "rag_vector_rank",
    ]


def test_smoke_rejects_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class _MalformedResponse:
        status = 200

        def __enter__(self) -> _MalformedResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b"not-json"

    monkeypatch.setattr(smoke, "urlopen", lambda *args, **kwargs: _MalformedResponse())

    with pytest.raises(smoke.SmokeFailure, match="invalid JSON"):
        smoke._request_json("http://127.0.0.1:8787", "GET", "/healthz")
