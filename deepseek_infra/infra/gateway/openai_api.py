"""OpenAI-compatible gateway facade.

A thin translation layer that exposes ``POST /v1/chat/completions`` and
``GET /v1/models`` over the local DeepSeek runtime (``call_deepseek`` /
``stream_deepseek``). Any OpenAI SDK or compatible tool can point ``base_url`` at
this local gateway.

Auth model (local-first): the OpenAI ``api_key`` carries the **local** access
token (validated by ``require_api_auth`` at the route layer); the upstream
DeepSeek key is taken from the server config (``DEEPSEEK_API_KEY``). The facade
never asks the client for a provider key.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from typing import Any, Generator

from deepseek_infra.core.config import settings
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import normalize_model_name
from deepseek_infra.infra.gateway.providers.registry import model_catalog, resolve_provider


def openai_to_internal_payload(body: dict[str, Any], *, local_base_url: str = "") -> dict[str, Any]:
    """Translate an OpenAI chat-completions request into the internal chat payload."""
    if not isinstance(body, dict):
        raise AppError("Request body must be a JSON object", code=ErrorCode.INVALID_PAYLOAD)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise AppError("messages must be a non-empty array", code=ErrorCode.INVALID_PAYLOAD)
    model = normalize_model_name(body.get("model") or settings.default_model)
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": bool(body.get("stream")),
        # Standard OpenAI endpoint: deterministic content only, no reasoning tokens.
        "thinkingEnabled": False,
        "localBaseUrl": local_base_url,
    }
    temperature = body.get("temperature")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        payload["temperature"] = float(temperature)
    return payload


def openai_models_list() -> dict[str, Any]:
    """OpenAI ``GET /v1/models`` payload across all active providers (DeepSeek + Ollama)."""
    return {"object": "list", "data": model_catalog()}


def openai_completion_response(result: dict[str, Any], model: str) -> dict[str, Any]:
    """Translate a non-streaming ``call_deepseek`` result into a chat.completion object."""
    raw_usage = result.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    response: dict[str, Any] = {
        "id": result.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.get("content") or ""},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, dict):
        response["diagnostics"] = diagnostics
    return response


def openai_chat_completion(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """Non-streaming /v1/chat/completions via the resolved provider (DeepSeek or Ollama)."""
    result = resolve_provider(model).chat(payload)
    return openai_completion_response(result, model)


def _sse(obj: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"


def openai_chat_stream(payload: dict[str, Any], model: str) -> Generator[bytes, None, None]:
    """Run the internal streaming pipeline and re-emit it as OpenAI chat.completion.chunk SSE."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    cancel_event = threading.Event()
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def emit(data: dict[str, Any]) -> None:
        events.put(data)

    provider = resolve_provider(model)

    def worker() -> None:
        try:
            provider.stream_chat(payload, emit, cancel_event=cancel_event)
        except Exception as exc:  # noqa: BLE001 - surface upstream failures as an OpenAI error chunk
            events.put({"type": "error", "error": str(exc)})
        finally:
            events.put(None)

    threading.Thread(target=worker, name="openai-gateway-stream", daemon=True).start()

    def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
        return _sse(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
        )

    finished = False
    try:
        yield chunk({"role": "assistant"})
        while True:
            item = events.get()
            if item is None:
                break
            kind = item.get("type")
            if kind == "content":
                text = str(item.get("text") or "")
                if text:
                    yield chunk({"content": text})
            elif kind == "done":
                yield chunk({}, finish="stop")
                finished = True
            elif kind == "error":
                yield _sse({"error": {"message": str(item.get("error") or "stream error"), "type": "upstream_error"}})
                finished = True
                break
    finally:
        cancel_event.set()
    if not finished:
        yield chunk({}, finish="stop")
    yield b"data: [DONE]\n\n"
