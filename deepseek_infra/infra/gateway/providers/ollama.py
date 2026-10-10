"""Local Ollama provider (http://127.0.0.1:11434 by default).

Plain chat + streaming over Ollama's REST API. DeepSeek-specific features
(tools, web search, multi-agent, semantic cache, RAG) are NOT routed here — this
provider does straight model inference only.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from deepseek_infra.core.config import settings
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.gateway.providers.base import BaseLLMProvider

OLLAMA_MODEL_PREFIX = "ollama/"
_TAGS_TTL_SECONDS = 30.0
# Status/discovery probe must stay snappy so /api/config never hangs on an
# enabled-but-unreachable Ollama; the chat timeout is used only for generation.
_TAGS_TIMEOUT_SECONDS = 3.0


class OllamaProvider(BaseLLMProvider):
    name = "ollama"

    def __init__(self) -> None:
        self._tags: list[str] = []
        self._tags_fetched_at = 0.0

    def available(self) -> bool:
        return bool(settings.ollama.enabled) and bool(self.models())

    @staticmethod
    def strip_prefix(model: str) -> str:
        return model[len(OLLAMA_MODEL_PREFIX):] if model.startswith(OLLAMA_MODEL_PREFIX) else model

    def _open(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        url = settings.ollama.base_url.rstrip("/") + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        effective_timeout = settings.ollama.timeout_seconds if timeout is None else timeout
        return urllib.request.urlopen(request, timeout=effective_timeout)  # noqa: S310 - local Ollama only

    def models(self) -> list[str]:
        if not settings.ollama.enabled:
            return []
        now = time.monotonic()
        # Cache by fetch time (not truthiness) so an unreachable Ollama — which
        # yields an empty list — is not re-probed on every /api/config call.
        if self._tags_fetched_at and now - self._tags_fetched_at < _TAGS_TTL_SECONDS:
            return self._tags
        try:
            with self._open("/api/tags", timeout=_TAGS_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            names = [str(item.get("name")) for item in (payload.get("models") or []) if item.get("name")]
        except Exception:  # noqa: BLE001 - unreachable Ollama just means no local models
            names = []
        self._tags = names
        self._tags_fetched_at = now
        return names

    def handles(self, model: str) -> bool:
        if not settings.ollama.enabled:
            return False
        if model.startswith(OLLAMA_MODEL_PREFIX):
            return True
        return model in self.models()

    @staticmethod
    def _clean_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
        cleaned: list[dict[str, str]] = []
        for message in payload.get("messages") or []:
            if not isinstance(message, dict) or not message.get("role"):
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            cleaned.append({"role": str(message["role"]), "content": text})
        return cleaned

    @staticmethod
    def _options(payload: dict[str, Any]) -> dict[str, Any]:
        options: dict[str, Any] = {}
        temperature = payload.get("temperature")
        if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
            options["temperature"] = float(temperature)
        return options

    def _body(self, payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
        model = self.strip_prefix(str(payload.get("model") or ""))
        body: dict[str, Any] = {"model": model, "messages": self._clean_messages(payload), "stream": stream}
        options = self._options(payload)
        if options:
            body["options"] = options
        return body

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._body(payload, stream=False)
        try:
            with self._open("/api/chat", method="POST", body=body) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise AppError(
                f"Cannot reach Ollama at {settings.ollama.base_url}: {exc.reason}",
                code=ErrorCode.UPSTREAM_FAILURE,
                status=502,
            ) from exc
        message = data.get("message") or {}
        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        completion_tokens = int(data.get("eval_count") or 0)
        return {
            "id": None,
            "model": f"{OLLAMA_MODEL_PREFIX}{data.get('model') or body['model']}",
            "content": str(message.get("content") or ""),
            "reasoning": "",
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def stream_chat(
        self,
        payload: dict[str, Any],
        emit: Callable[[dict[str, Any]], None],
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        body = self._body(payload, stream=True)
        try:
            with self._open("/api/chat", method="POST", body=body) as response:
                for raw_line in response:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = str((chunk.get("message") or {}).get("content") or "")
                    if text:
                        emit({"type": "content", "text": text})
                    if chunk.get("done"):
                        emit({"type": "done"})
                        return
            emit({"type": "done"})
        except urllib.error.URLError as exc:
            emit({"type": "error", "error": f"Cannot reach Ollama at {settings.ollama.base_url}: {exc.reason}"})
