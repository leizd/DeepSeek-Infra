"""Provider registry + router for the gateway."""

from __future__ import annotations

import time
from typing import Any

from deepseek_infra.core.config import settings
from deepseek_infra.infra.gateway.providers.base import BaseLLMProvider
from deepseek_infra.infra.gateway.providers.deepseek import DeepSeekProvider
from deepseek_infra.infra.gateway.providers.ollama import OLLAMA_MODEL_PREFIX, OllamaProvider

_deepseek = DeepSeekProvider()
_ollama = OllamaProvider()


def deepseek_provider() -> DeepSeekProvider:
    return _deepseek


def ollama_provider() -> OllamaProvider:
    return _ollama


def resolve_provider(model: str) -> BaseLLMProvider:
    """Pick the provider for a requested model.

    Ollama is chosen only when enabled and the model is an explicit ``ollama/...``
    id or a discovered local tag; known DeepSeek models short-circuit without any
    network probe. Everything else falls back to DeepSeek (the default cloud runtime).
    """
    name = str(model or "")
    if not settings.ollama.enabled:
        return _deepseek
    if name.startswith(OLLAMA_MODEL_PREFIX):
        return _ollama
    if name in _deepseek.models():
        return _deepseek
    if _ollama.handles(name):
        return _ollama
    return _deepseek


def model_catalog() -> list[dict[str, Any]]:
    """OpenAI ``/v1/models`` entries across all active providers."""
    created = int(time.time())
    entries: list[dict[str, Any]] = [
        {"id": model_id, "object": "model", "created": created, "owned_by": "deepseek-infra"}
        for model_id in _deepseek.models()
    ]
    if settings.ollama.enabled:
        for model_id in _ollama.models():
            entries.append(
                {"id": f"{OLLAMA_MODEL_PREFIX}{model_id}", "object": "model", "created": created, "owned_by": "ollama"}
            )
    return entries


def providers_status() -> dict[str, Any]:
    """Compact provider status for /api/config and diagnostics."""
    return {
        "default": _deepseek.name,
        "deepseek": {"models": _deepseek.models()},
        "ollama": {
            "enabled": bool(settings.ollama.enabled),
            "baseUrl": settings.ollama.base_url,
            "available": _ollama.available(),
            "models": _ollama.models(),
        },
    }
