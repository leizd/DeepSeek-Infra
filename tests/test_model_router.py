from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import deepseek_infra.infra.gateway.model_router as model_router
from deepseek_infra.infra.gateway.deepseek_client import build_deepseek_request, call_deepseek_cascade


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _response_bytes(content: str) -> bytes:
    return json.dumps(
        {"id": "r", "model": "deepseek-v4-pro", "choices": [{"message": {"content": content}}], "usage": {}}
    ).encode("utf-8")


def _msg(content: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": content}]


def _cascade_payload(**extra: Any) -> dict[str, Any]:
    # Disable the (process-global) semantic cache so each cascade call really hits
    # the mocked upstream and tests don't cross-contaminate via cached drafts.
    payload: dict[str, Any] = {
        "apiKey": "k",
        "model": "deepseek-v4-pro",
        "toolsEnabled": False,
        "semanticCacheEnabled": False,
        "messages": _msg("explain caching"),
    }
    payload.update(extra)
    return payload


def test_route_request_respects_explicit_model() -> None:
    decision = model_router.route_request({"model": "deepseek-v4-flash", "messages": _msg("hi")})
    assert decision.auto is False
    assert decision.model == "deepseek-v4-flash"
    assert decision.reasons[0]["router"] == "explicit"


def test_route_request_auto_latency_and_capability() -> None:
    simple = model_router.route_request({"autoRoute": True, "messages": _msg("你好")})
    assert simple.model == "deepseek-v4-flash"
    assert simple.tier == "fast"
    assert simple.reasons[-1]["router"] == "latency"

    complex_decision = model_router.route_request(
        {"autoRoute": True, "messages": _msg("请写一段代码来调试这个算法的边界条件并证明其正确性")}
    )
    assert complex_decision.model == "deepseek-v4-pro"
    assert complex_decision.reasons[-1]["router"] == "capability"
    assert complex_decision.fallback_model == "deepseek-v4-flash"


def test_route_request_cost_router_prefers_cheap_over_budget(monkeypatch) -> None:
    monkeypatch.setattr(model_router, "MODEL_ROUTER_COST_BUDGET_TOKENS", 5)
    # A neutral (not simple, not complex) query that exceeds the tiny token budget.
    decision = model_router.route_request({"autoRoute": True, "messages": _msg("请综合评估这个中等长度的开放式问题并给出一个均衡的结论说明")})
    assert decision.model == "deepseek-v4-flash"
    assert decision.reasons[-1]["router"] == "cost"


def test_quality_gate_flags_short_refusal_and_uncertainty() -> None:
    assert model_router.quality_gate("").passed is False
    assert model_router.quality_gate("too short").passed is False  # under min_chars
    long_ok = "This is a sufficiently detailed and confident answer that comfortably clears the minimum length bar for the quality gate."
    assert model_router.quality_gate(long_ok).passed is True
    refusal = model_router.quality_gate("抱歉，我无法回答这个问题，因为它超出了我的能力范围，我无法提供帮助。")
    assert refusal.passed is False
    assert "refusal" in refusal.reasons
    gate = model_router.quality_gate(long_ok, require_citations=True)
    assert "missing_citation" in gate.reasons


def test_build_deepseek_request_auto_routes_model_and_records_diagnostics() -> None:
    prepared = build_deepseek_request(
        {"apiKey": "k", "model": "auto", "messages": _msg("你好")},
        stream=False,
    )
    assert prepared.body["model"] == "deepseek-v4-flash"
    router_diag = prepared.diagnostics["modelRouter"]
    assert router_diag["auto"] is True
    assert router_diag["model"] == "deepseek-v4-flash"


def test_cascade_returns_draft_when_gate_passes() -> None:
    payload = _cascade_payload(cascade=True)
    good = "Caching stores computed results so repeated requests return instantly, cutting latency and cost; it works best for deterministic, frequently repeated queries."
    with patch("urllib.request.urlopen", return_value=FakeResponse(_response_bytes(good))) as urlopen:
        result = call_deepseek_cascade(payload)
    # Draft passed the gate -> only one upstream call, no escalation.
    assert urlopen.call_count == 1
    cascade = result["diagnostics"]["modelCascade"]
    assert cascade["escalated"] is False
    assert cascade["draftModel"] == "deepseek-v4-flash"


def test_cascade_escalates_when_gate_fails() -> None:
    payload = _cascade_payload(cascade=True)
    responses = [FakeResponse(_response_bytes("抱歉，我无法回答")), FakeResponse(_response_bytes("A thorough refined answer that is long and confident enough to satisfy the quality gate without any refusal."))]
    with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
        result = call_deepseek_cascade(payload)
    # Draft failed the gate -> escalate to the refine model (two upstream calls).
    assert urlopen.call_count == 2
    cascade = result["diagnostics"]["modelCascade"]
    assert cascade["escalated"] is True
    assert cascade["refineModel"] == "deepseek-v4-pro"
    assert "refusal" in cascade["gate"]["reasons"]


def test_cascade_disabled_falls_back_to_plain_call() -> None:
    payload = _cascade_payload(messages=_msg("hello, please greet me back warmly"))
    with patch("urllib.request.urlopen", return_value=FakeResponse(_response_bytes("hi there friend"))) as urlopen:
        result = call_deepseek_cascade(payload)
    assert urlopen.call_count == 1
    assert "modelCascade" not in result["diagnostics"]
