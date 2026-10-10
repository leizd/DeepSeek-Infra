"""Provider abstraction for the LLM gateway.

Every provider consumes the **internal chat payload** (``model`` + OpenAI-style
``messages``) and produces the internal shapes the OpenAI ``/v1`` facade already
understands:

- ``chat(payload)`` returns ``{id?, model, content, usage}``.
- ``stream_chat(payload, emit)`` calls ``emit`` with ``{"type": "content", "text": ...}``
  deltas, then ``{"type": "done"}`` on success or ``{"type": "error", "error": ...}``.

This keeps the gateway's SSE / non-stream translators provider-agnostic.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any


class BaseLLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """Whether this provider can currently serve requests."""

    @abstractmethod
    def models(self) -> list[str]:
        """Model ids this provider serves."""

    @abstractmethod
    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Non-streaming completion -> {id?, model, content, usage}."""

    @abstractmethod
    def stream_chat(
        self,
        payload: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Streaming completion; push internal {type:...} events via ``emit``."""
