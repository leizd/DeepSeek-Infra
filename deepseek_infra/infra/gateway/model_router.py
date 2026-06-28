"""Policy-driven model router and cascade-inference planner.

Existing routing primitives (fast/expert aliases, image→vision model, edge /
privacy / offline routing in ``edge_inference``, cloud-fail→edge fallback,
multi-provider registry) are unified here into an explicit *Model Router* that
chooses the cloud model tier by capability, cost, latency and task complexity,
plus a *cascade* planner (cheap draft → quality gate → expensive refine).

This module is pure: it makes *decisions* and *scores* answers. The actual
upstream calls (and the optional Judge-model scoring) live in ``deepseek_client``.
The router only auto-selects a model when a request opts in (``autoRoute`` /
``model="auto"``); an explicit model choice is always respected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from deepseek_infra.core.config import (
    DEFAULT_MODEL,
    MODEL_ROUTER_CASCADE_ENABLED,
    MODEL_ROUTER_CASCADE_MIN_CHARS,
    MODEL_ROUTER_COST_BUDGET_TOKENS,
    MODEL_ROUTER_DRAFT_MODEL,
    MODEL_ROUTER_ENABLED,
    MODEL_ROUTER_JUDGE_ENABLED,
    MODEL_ROUTER_JUDGE_MODEL,
    MODEL_ROUTER_JUDGE_THRESHOLD,
    MODEL_ROUTER_REFINE_MODEL,
    SUPPORTED_MODELS,
)
from deepseek_infra.core.utils import latest_user_query, normalize_model_name
from deepseek_infra.infra.gateway.context_engine import estimate_tokens
from deepseek_infra.infra.gateway.edge_inference import (
    ARTIFACT_QUERY_RE,
    COMPLEX_QUERY_RE,
    SIMPLE_TASK_RE,
    has_image_attachment,
)

# Strong uncertainty / refusal phrases (not bare "可能"/"也许", which are common in
# good answers) that signal a draft worth escalating in cascade mode.
UNCERTAINTY_MARKERS = (
    "我不确定",
    "无法确定",
    "不太确定",
    "可能不准确",
    "仅供参考",
    "i'm not sure",
    "i am not sure",
    "i am not certain",
    "not entirely sure",
    "i'm uncertain",
)
REFUSAL_MARKERS = (
    "无法回答",
    "抱歉，我无法",
    "抱歉，我不能",
    "无法提供",
    "i cannot help",
    "i can't help",
    "i'm unable to",
    "as an ai language model",
)
_CITATION_RE = re.compile(r"\[\^[WF]\d", re.IGNORECASE)


def router_status() -> dict[str, Any]:
    """Public Model Router capabilities for ``/api/config``."""
    draft_model = MODEL_ROUTER_DRAFT_MODEL
    return {
        "enabled": MODEL_ROUTER_ENABLED,
        "cascadeEnabled": MODEL_ROUTER_ENABLED and MODEL_ROUTER_CASCADE_ENABLED,
        "judgeEnabled": MODEL_ROUTER_JUDGE_ENABLED,
        "draftModel": draft_model,
        "draftProvider": "ollama" if draft_model.startswith("ollama/") else "deepseek",
        "refineModel": MODEL_ROUTER_REFINE_MODEL,
        "judgeModel": MODEL_ROUTER_JUDGE_MODEL,
        "costBudgetTokens": MODEL_ROUTER_COST_BUDGET_TOKENS,
    }


def is_auto_request(payload: dict[str, Any]) -> bool:
    if not MODEL_ROUTER_ENABLED:
        return False
    if str(payload.get("model") or "").strip().lower() == "auto":
        return True
    return payload.get("autoRoute") is True


def cascade_requested(payload: dict[str, Any]) -> bool:
    return MODEL_ROUTER_ENABLED and MODEL_ROUTER_CASCADE_ENABLED and payload.get("cascade") is True


def query_complexity(query: str) -> str:
    text = str(query or "").strip()
    if not text:
        return "neutral"
    if COMPLEX_QUERY_RE.search(text) or ARTIFACT_QUERY_RE.search(text):
        return "complex"
    if len(text) > 1200:
        return "complex"
    if SIMPLE_TASK_RE.search(text) and len(text) <= 400:
        return "simple"
    if len(text) <= 120:
        return "simple"
    return "neutral"


def _estimate_payload_tokens(payload: dict[str, Any]) -> int:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return 0
    parts: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
    return estimate_tokens("\n".join(parts)[:200_000])


@dataclass(frozen=True)
class RouteDecision:
    model: str
    tier: str
    auto: bool
    capability: str
    fallback_model: str
    estimated_prompt_tokens: int
    reasons: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tier": self.tier,
            "auto": self.auto,
            "capability": self.capability,
            "fallbackModel": self.fallback_model,
            "estimatedPromptTokens": self.estimated_prompt_tokens,
            "reasons": list(self.reasons),
        }


def route_request(payload: dict[str, Any], *, budget_used: int = 0) -> RouteDecision:
    """Decide the cloud model tier for a request (capability/cost/latency)."""
    draft = MODEL_ROUTER_DRAFT_MODEL
    refine = MODEL_ROUTER_REFINE_MODEL
    query = latest_user_query(payload)
    image = has_image_attachment(payload)
    capability = "vision" if image else "text"
    prompt_tokens = _estimate_payload_tokens(payload)
    reasons: list[dict[str, str]] = []

    if not is_auto_request(payload):
        base = normalize_model_name(payload.get("model") or DEFAULT_MODEL)
        if base not in SUPPORTED_MODELS:
            base = DEFAULT_MODEL
        reasons.append({"router": "explicit", "decision": base})
        if image and base != refine:
            base = refine
            reasons.append({"router": "capability", "decision": f"vision->{refine}"})
        model = base
    else:
        complexity = query_complexity(query)
        if image:
            model = refine
            reasons.append({"router": "capability", "decision": f"vision->{refine}"})
        elif complexity == "complex":
            model = refine
            reasons.append({"router": "capability", "decision": f"complex->{refine}"})
        elif MODEL_ROUTER_COST_BUDGET_TOKENS > 0 and (budget_used + prompt_tokens) > MODEL_ROUTER_COST_BUDGET_TOKENS:
            model = draft
            reasons.append({"router": "cost", "decision": f"over_budget->{draft}"})
        elif complexity == "simple":
            model = draft
            reasons.append({"router": "latency", "decision": f"simple->{draft}"})
        else:
            model = DEFAULT_MODEL
            reasons.append({"router": "default", "decision": DEFAULT_MODEL})

    fallback = draft if model == refine else refine
    tier = "fast" if model == draft else "expert" if model == refine else model
    return RouteDecision(
        model=model,
        tier=tier,
        auto=is_auto_request(payload),
        capability=capability,
        fallback_model=fallback,
        estimated_prompt_tokens=prompt_tokens,
        reasons=reasons,
    )


@dataclass(frozen=True)
class CascadePlan:
    enabled: bool
    draft_model: str
    refine_model: str
    draft_provider: str
    judge: bool
    judge_model: str
    judge_threshold: float
    min_chars: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "draftModel": self.draft_model,
            "refineModel": self.refine_model,
            "draftProvider": self.draft_provider,
            "judge": self.judge,
            "judgeModel": self.judge_model,
            "judgeThreshold": self.judge_threshold,
            "minChars": self.min_chars,
        }


def cascade_plan(payload: dict[str, Any]) -> CascadePlan:
    """Plan cascade inference. Disabled for vision/agent turns (those need pro directly)."""
    enabled = (
        cascade_requested(payload)
        and payload.get("agentMode") is not True
        and not has_image_attachment(payload)
    )
    judge = enabled and (MODEL_ROUTER_JUDGE_ENABLED or payload.get("judge") is True)
    draft_model = MODEL_ROUTER_DRAFT_MODEL
    draft_provider = "ollama" if draft_model.startswith("ollama/") else "deepseek"
    return CascadePlan(
        enabled=enabled,
        draft_model=draft_model,
        refine_model=MODEL_ROUTER_REFINE_MODEL,
        draft_provider=draft_provider,
        judge=judge,
        judge_model=MODEL_ROUTER_JUDGE_MODEL,
        judge_threshold=MODEL_ROUTER_JUDGE_THRESHOLD,
        min_chars=MODEL_ROUTER_CASCADE_MIN_CHARS,
    )


@dataclass(frozen=True)
class GateResult:
    passed: bool
    score: float
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "score": self.score, "reasons": list(self.reasons)}


def quality_gate(
    content: str,
    *,
    min_chars: int = MODEL_ROUTER_CASCADE_MIN_CHARS,
    require_citations: bool = False,
) -> GateResult:
    """Heuristic answer-quality gate driving cascade escalation.

    Fails (→ escalate) on: empty / too short, a refusal, multiple uncertainty
    markers, or missing citations when the turn required sources.
    """
    text = str(content or "").strip()
    if not text:
        return GateResult(False, 0.0, ["empty"])
    reasons: list[str] = []
    if len(text) < max(1, int(min_chars)):
        reasons.append("too_short")
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in REFUSAL_MARKERS):
        reasons.append("refusal")
    if sum(1 for marker in UNCERTAINTY_MARKERS if marker.lower() in lowered) >= 2:
        reasons.append("uncertain")
    if require_citations and not _CITATION_RE.search(text):
        reasons.append("missing_citation")
    passed = not reasons
    score = round(max(0.0, 1.0 - 0.34 * len(reasons)), 3)
    return GateResult(passed, score, reasons)
