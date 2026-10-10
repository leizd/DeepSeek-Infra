"""Default cloud provider: the full DeepSeek runtime.

Tools, search, multi-agent, semantic cache and RAG all stay on this path — the
provider simply delegates to the existing ``call_deepseek`` / ``stream_deepseek``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from deepseek_infra.core.config import settings
from deepseek_infra.infra.gateway.deepseek_client import call_deepseek, stream_deepseek
from deepseek_infra.infra.gateway.providers.base import BaseLLMProvider


class DeepSeekProvider(BaseLLMProvider):
    name = "deepseek"

    def available(self) -> bool:
        # Default provider; the upstream key is validated downstream in call_deepseek.
        return True

    def models(self) -> list[str]:
        return list(settings.supported_models)

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return call_deepseek(payload)

    def stream_chat(
        self,
        payload: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        stream_deepseek(payload, emit, cancel_event=cancel_event)
