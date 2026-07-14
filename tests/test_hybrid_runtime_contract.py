from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts import smoke_hybrid_runtime as smoke


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_hybrid_compose_enables_rust_only_in_test_overlay() -> None:
    default = _read("docker-compose.yml")
    hybrid = _read("docker-compose.hybrid-test.yml")

    assert "rust-gateway" not in default
    assert "DEEPSEEK_RUST_" not in default
    assert "deepseek-infra:" in hybrid
    assert "rust-gateway:" in hybrid
    for component in ("GATEWAY", "MCP", "POLICY", "RAG"):
        assert f'DEEPSEEK_RUST_{component}: "1"' in hybrid
        assert f'DEEPSEEK_RUST_{component}_FALLBACK: "1"' in hybrid
    assert 'DEEPSEEK_RUST_RAG_DOCUMENT_PREP: "1"' in hybrid
    assert 'DEEPSEEK_RUST_GATEWAY_URL: "http://rust-gateway:8787"' in hybrid
    assert 'DEEPSEEK_API_URL: "http://upstream-stub:9080/chat/completions"' in hybrid
    assert "stub_deepseek_upstream.py" in hybrid
    assert 'DEEPSEEK_RUST_POLICY_FAILURE_MODE: "fallback"' in hybrid
    assert 'AUTH_DISABLED: "1"' in hybrid
    assert "condition: service_healthy" in hybrid
    assert "dockerfile: rust/Dockerfile" in hybrid
    assert "8787:8787" not in hybrid


def test_ci_runs_hybrid_compose_and_always_cleans_up() -> None:
    workflow = _read(".github/workflows/ci.yml")

    assert "hybrid-runtime-e2e:" in workflow
    assert "docker-compose.hybrid-test.yml" in workflow
    assert "python scripts/smoke_hybrid_runtime.py" in workflow
    assert "logs --no-color" in workflow
    assert "down --volumes --remove-orphans" in workflow
    assert "--cov-report=json:artifacts/coverage.json" in workflow
    assert "--cov-fail-under=95" in workflow


def test_compose_command_keeps_both_files_before_action() -> None:
    command = smoke._compose_command(smoke.DEFAULT_COMPOSE_FILES, "stop", "rust-gateway")

    assert command == [
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.hybrid-test.yml",
        "stop",
        "rust-gateway",
    ]


def test_parse_json_output_uses_last_nonempty_line() -> None:
    assert smoke._parse_json_output('log line\n{"ok":true}\n', "probe") == {"ok": True}

    with pytest.raises(smoke.SmokeFailure, match="invalid JSON"):
        smoke._parse_json_output("not json", "probe")


def test_policy_probe_requires_rust_then_python_fallback() -> None:
    rust_payload = {
        "client": {
            "ok": True,
            "status": 200,
            "allowed": False,
            "code": "localhost_blocked",
            "decision_id": "pd_hybrid",
        },
        "output": {
            "ok": False,
            "code": "localhost_blocked",
            "decision_id": "pd_hybrid",
            "policy": {"reasons": ["rust_policy"]},
        },
    }
    fallback_payload = {
        "client": {"ok": False, "status": 0, "allowed": False},
        "output": {"ok": False, "policy": {"reasons": ["ssrf_blocked:private ip"]}},
    }

    smoke._assert_policy_probe(rust_payload, expect_rust=True)
    smoke._assert_policy_probe(fallback_payload, expect_rust=False)


def test_rag_probe_requires_all_delegations_and_exact_ranking() -> None:
    common = {
        "indexed": 2,
        "normalized": "rust 语言",
        "ranked": [
            {"chunkIndex": 1, "text": "hybrid sentinel exact phrase", "score": 10},
            {"chunkIndex": 0, "text": "hybrid sentinel partial", "score": 1},
        ],
        "citation": f"{'e' * 32}:L10-L20",
    }

    smoke._assert_rag_probe({**common, "delegated": {"normalize": True, "score": True, "citation": True}}, expect_rust=True)
    smoke._assert_rag_probe({**common, "delegated": {"normalize": False, "score": False, "citation": False}}, expect_rust=False)


def test_rag_document_probe_requires_safe_single_delegation_and_identical_fallback() -> None:
    common = {
        "trace": {"calls": 1, "safePayload": True},
        "chunkCount": 2,
        "fingerprint": "a" * 64,
        "readerMatched": True,
        "persistedByPython": True,
    }
    rust = {**common, "diagnostics": {"runtime": "rust", "fallback": False, "fallbackReason": ""}}
    fallback = {
        **common,
        "diagnostics": {"runtime": "python", "fallback": True, "fallbackReason": "rust_backend_unavailable"},
    }

    assert smoke._assert_rag_document_probe(rust, expect_rust=True) == "a" * 64
    assert smoke._assert_rag_document_probe(fallback, expect_rust=False) == "a" * 64


def test_http_contracts_identify_rust_mcp_preparation_with_python_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(
        base_url: str,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> smoke.HttpResult:
        del base_url, method, timeout
        if path == "/api/rust/status":
            body: dict[str, Any] = {
                "ok": True,
                "rust": {
                    "enabled": {"gateway": True, "mcp": True, "policy": True, "rag": True},
                    "components": {"gateway": {"enabled": True, "healthy": True}},
                },
            }
        elif path == "/v1/models":
            body = {"object": "list", "data": [{"id": "deepseek-v4-pro", "owned_by": "deepseek-infra"}]}
        elif path == "/v1/chat/completions":
            body = {
                "id": "chatcmpl-hybrid-upstream",
                "choices": [{"message": {"role": "assistant", "content": "hybrid upstream stub"}}],
                "diagnostics": {"gatewayRequestPreparation": {"runtime": "rust", "fallback": False}},
            }
        elif path == "/mcp" and payload is not None:
            request_id = payload["id"]
            mcp_method = payload["method"]
            headers = {
                "x-deepseek-mcp-preparation-runtime": "rust",
                "x-deepseek-mcp-preparation-fallback": "0",
            }
            result: dict[str, Any]
            if mcp_method == "initialize":
                result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "deepseek-infra"}}
            elif mcp_method == "tools/list":
                result = {"tools": [{"name": "data_transform"}]}
            elif payload.get("params") == {}:
                body = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "invalid", "data": {"code": "invalid_params"}},
                }
                headers = {
                    "x-deepseek-mcp-preparation-runtime": "python",
                    "x-deepseek-mcp-preparation-fallback": "0",
                }
                return smoke.HttpResult(200, body, "", headers)
            else:
                result = {"structuredContent": {"ok": True, "result": {"count": 4}}, "isError": False}
            body = {"jsonrpc": "2.0", "id": request_id, "result": result}
        else:
            raise AssertionError(f"unexpected request: {path}")
        return smoke.HttpResult(200, body, "", headers if path == "/mcp" else {})

    monkeypatch.setattr(smoke, "_request", request)

    smoke.check_rust_status("http://test", expect_healthy=True)
    smoke.check_gateway_request_preparation("http://test")
    smoke.check_mcp_protocol_preparation("http://test", expect_rust=True)


def test_http_contracts_identify_python_gateway_and_mcp_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(
        base_url: str,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> smoke.HttpResult:
        del base_url, method, timeout
        if path == "/v1/models":
            return smoke.HttpResult(200, {"data": [{"id": "deepseek-v4-pro", "owned_by": "deepseek-infra"}]}, "")
        if path == "/v1/chat/completions":
            return smoke.HttpResult(
                200,
                {
                    "id": "chatcmpl-hybrid-upstream",
                    "choices": [{"message": {"content": "hybrid upstream stub"}}],
                    "diagnostics": {
                        "gatewayRequestPreparation": {
                            "runtime": "python",
                            "fallback": True,
                            "fallbackReason": "rust_backend_unavailable",
                        }
                    },
                },
                "",
            )
        if path == "/mcp" and payload is not None:
            request_id = payload["id"]
            mcp_method = payload["method"]
            headers = {
                "x-deepseek-mcp-preparation-runtime": "python",
                "x-deepseek-mcp-preparation-fallback": "1",
                "x-deepseek-mcp-preparation-fallback-reason": "rust_backend_unavailable",
            }
            result: dict[str, Any]
            if mcp_method == "initialize":
                result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "deepseek-infra"}}
            elif mcp_method == "tools/list":
                result = {"tools": [{"name": "data_transform"}]}
            elif payload.get("params") == {}:
                body = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "invalid", "data": {"code": "invalid_params"}},
                }
                headers = {
                    "x-deepseek-mcp-preparation-runtime": "python",
                    "x-deepseek-mcp-preparation-fallback": "0",
                }
                return smoke.HttpResult(200, body, "", headers)
            else:
                result = {"structuredContent": {"ok": True, "result": {"count": 4}}, "isError": False}
            return smoke.HttpResult(200, {"jsonrpc": "2.0", "id": request_id, "result": result}, "", headers)
        raise AssertionError(f"unexpected request: {path}")

    monkeypatch.setattr(smoke, "_request", request)

    smoke._check_gateway_fallback("http://test", timeout=1)
    smoke._check_mcp_fallback("http://test", timeout=1)


def test_run_smoke_stops_sidecar_before_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    monkeypatch.setattr(smoke, "wait_for_service", lambda *args, **kwargs: smoke.CheckResult("health", "ok"))
    monkeypatch.setattr(
        smoke,
        "check_rust_status",
        lambda *args, **kwargs: smoke.CheckResult("status", "healthy") if kwargs["expect_healthy"] else smoke.CheckResult("status", "down"),
    )
    monkeypatch.setattr(smoke, "check_gateway_request_preparation", lambda *args, **kwargs: smoke.CheckResult("gateway", "ok"))
    monkeypatch.setattr(smoke, "check_mcp_protocol_preparation", lambda *args, **kwargs: smoke.CheckResult("mcp", "ok"))
    monkeypatch.setattr(smoke, "check_policy_integration", lambda *args, **kwargs: smoke.CheckResult("policy", "ok"))
    monkeypatch.setattr(smoke, "check_rag_integration", lambda *args, **kwargs: smoke.CheckResult("rag", "ok"))
    monkeypatch.setattr(
        smoke,
        "check_rag_vector_binary",
        lambda *args, **kwargs: (smoke.CheckResult("rag-vector-binary", "ok"), ("binary-127", 0.9)),
    )
    monkeypatch.setattr(
        smoke,
        "check_rag_document_preparation",
        lambda *args, **kwargs: (smoke.CheckResult("rag-document", "ok"), "a" * 64),
    )

    def stop(*args: object, **kwargs: object) -> smoke.CheckResult:
        events.append("stop")
        return smoke.CheckResult("stop", "ok")

    def fallback(*args: object, **kwargs: object) -> list[smoke.CheckResult]:
        events.append("fallback")
        return [smoke.CheckResult("fallback", "ok")]

    monkeypatch.setattr(smoke, "stop_sidecar", stop)
    monkeypatch.setattr(smoke, "check_fallbacks", fallback)

    checks = smoke.run_smoke("http://127.0.0.1:8000")

    assert events == ["stop", "fallback"]
    assert checks[-1].name == "fallback"
