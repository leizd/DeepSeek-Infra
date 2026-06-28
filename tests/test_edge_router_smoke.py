from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_edge_smoke() -> Any:
    path = Path(__file__).resolve().parents[1] / "examples" / "edge_router_smoke.py"
    spec = importlib.util.spec_from_file_location("edge_router_smoke_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_edge_router_evidence_passes_with_ollama_model_and_local_call() -> None:
    smoke = _load_edge_smoke()
    evidence = smoke.build_evidence(
        [
            {"name": "healthz", "status": "pass", "detail": "ok", "data": {}},
            {"name": "edge.status", "status": "warn", "detail": "available=false", "data": {"edgeInference": {"available": False}}},
            {"name": "openai.models", "status": "pass", "detail": "ollama=1", "data": {"ollamaModels": ["ollama/llama3.2"]}},
            {"name": "openai.chat", "status": "pass", "detail": "choices=1", "data": {"model": "ollama/llama3.2", "choiceCount": 1}},
        ],
        base_url="http://127.0.0.1:8000",
        token_used=False,
    )

    assert evidence["schemaVersion"] == "edge-router-smoke-evidence.v1"
    assert evidence["status"] == "PASS"
    assert evidence["checks"] == {
        "ollamaModelsListed": "PASS",
        "openaiCompatibleLocalCall": "PASS",
        "edgeStatusEndpoint": "PASS",
        "fallbackReady": "PASS",
    }
    assert "commit" in evidence
    assert {"os", "python", "ci"}.issubset(evidence["environment"])


def test_edge_router_evidence_warns_without_local_model() -> None:
    smoke = _load_edge_smoke()
    evidence = smoke.build_evidence(
        [
            {"name": "healthz", "status": "pass", "detail": "ok", "data": {}},
            {"name": "edge.status", "status": "warn", "detail": "available=false", "data": {"edgeInference": {"available": False}}},
            {"name": "openai.models", "status": "warn", "detail": "ollama=0", "data": {"ollamaModels": []}},
            {"name": "openai.chat", "status": "warn", "detail": "no ollama model", "data": {}},
        ],
        base_url="http://127.0.0.1:8000",
        token_used=False,
    )

    assert evidence["status"] == "WARNING"
    assert evidence["checks"]["fallbackReady"] == "WARNING"


def test_edge_router_smoke_writes_json_and_markdown(monkeypatch: Any, tmp_path: Path) -> None:
    smoke = _load_edge_smoke()
    out = tmp_path / "edge-router-smoke.json"
    md = tmp_path / "edge-router-smoke.md"

    def fake_request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if url.endswith("/healthz"):
            return {"status": "ok"}
        if url.endswith("/api/edge/status"):
            return {"ok": True, "edgeInference": {"enabled": False, "provider": "llama_cpp", "available": False}}
        if url.endswith("/v1/models"):
            return {"object": "list", "data": [{"id": "ollama/llama3.2", "object": "model"}]}
        if url.endswith("/v1/chat/completions"):
            payload = kwargs.get("payload")
            assert isinstance(payload, dict)
            assert payload["model"] == "ollama/llama3.2"
            return {"choices": [{"message": {"content": "hello"}}]}
        raise AssertionError(url)

    monkeypatch.setattr(smoke, "request_json", fake_request_json)
    monkeypatch.setattr(smoke, "app_version", lambda: "2.4.3")
    monkeypatch.setattr(smoke, "git_value", lambda *args: "abc1234" if args[:2] == ("rev-parse", "--short") else "")
    monkeypatch.setattr(smoke, "utc_now", lambda: "2026-06-28T00:00:00Z")
    monkeypatch.setattr(smoke, "build_environment", lambda: {"os": "Linux", "python": "3.12", "ci": True})

    assert smoke.main(["--base-url", "http://127.0.0.1:8000", "--out", str(out), "--markdown", str(md)]) == 0

    evidence = json.loads(out.read_text(encoding="utf-8"))
    assert evidence["version"] == "2.4.3"
    assert evidence["status"] == "PASS"
    assert evidence["checks"]["openaiCompatibleLocalCall"] == "PASS"
    assert "Edge Router Smoke Evidence" in md.read_text(encoding="utf-8")


def test_edge_router_evidence_schema_tracks_required_checks() -> None:
    schema = json.loads(Path("evals/schemas/edge_router_smoke_evidence.schema.json").read_text(encoding="utf-8"))
    required_checks = schema["properties"]["checks"]["required"]

    assert required_checks == [
        "ollamaModelsListed",
        "openaiCompatibleLocalCall",
        "edgeStatusEndpoint",
        "fallbackReady",
    ]
