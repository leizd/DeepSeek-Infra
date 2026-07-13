"""Deterministic Gateway request preparation with optional Rust delegation.

This boundary accepts only the already assembled, credential-free upstream
request body. It is deliberately pure apart from the optional sidecar call:
provider selection, HTTP, retries, streaming, tools, cache policy, and tracing
remain owned by Python.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

from deepseek_infra.core.config import SUPPORTED_MODELS
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import normalize_model_name

MAX_REQUEST_BYTES = 16_000_000
MAX_REQUEST_DEPTH = 32
MAX_TOKENS = 131_072
ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})
ALLOWED_REQUEST_FIELDS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "max_tokens",
        "reasoning_effort",
        "thinking",
    }
)
PREPARATION_ERROR_CODES = frozenset(
    {
        ErrorCode.INVALID_REQUEST.value,
        ErrorCode.UNSUPPORTED_MODEL.value,
        ErrorCode.INVALID_MESSAGES.value,
        ErrorCode.INVALID_MESSAGE_ROLE.value,
        ErrorCode.INVALID_MESSAGE_CONTENT.value,
        ErrorCode.INVALID_TOOLS.value,
        ErrorCode.INVALID_TOOL_CHOICE.value,
        ErrorCode.INVALID_TEMPERATURE.value,
        ErrorCode.INVALID_MAX_TOKENS.value,
        ErrorCode.REQUEST_TOO_LARGE.value,
    }
)
FORBIDDEN_TOP_LEVEL_FIELDS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "apiKey".lower(),
        "deepseek_api_key",
        "localbaseurl",
        "local_base_url",
        "file_path",
        "filepath",
    }
)


@dataclass(frozen=True)
class PreparedGatewayRequest:
    request: dict[str, Any]
    diagnostics: dict[str, Any]


def _error(code: ErrorCode, message: str) -> AppError:
    return AppError(message, code=code)


def _json_size(value: Any) -> int:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _error(ErrorCode.INVALID_REQUEST, "request must be safely JSON serializable") from exc
    return len(encoded)


def _depth(value: Any, current: int = 0) -> int:
    if current > MAX_REQUEST_DEPTH:
        return current
    if isinstance(value, dict):
        return max(( _depth(item, current + 1) for item in value.values()), default=current)
    if isinstance(value, list):
        return max(( _depth(item, current + 1) for item in value), default=current)
    return current


def _normalize_content(value: Any, *, allow_empty: bool) -> str | list[dict[str, Any]]:
    if isinstance(value, str):
        content = value.strip()
        if not content and not allow_empty:
            raise _error(ErrorCode.INVALID_MESSAGE_CONTENT, "message content must not be empty")
        return content
    if not isinstance(value, list) or not value:
        raise _error(ErrorCode.INVALID_MESSAGE_CONTENT, "message content must be a string or non-empty content array")
    parts: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, dict):
            raise _error(ErrorCode.INVALID_MESSAGE_CONTENT, "message content parts must be objects")
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str) and part["text"].strip():
            parts.append({"type": "text", "text": part["text"].strip()})
            continue
        image = part.get("image_url")
        if kind == "image_url" and isinstance(image, dict) and isinstance(image.get("url"), str) and image["url"].strip():
            normalized_image: dict[str, Any] = {"url": image["url"].strip()}
            detail = image.get("detail")
            if detail in {"auto", "low", "high"}:
                normalized_image["detail"] = detail
            parts.append({"type": "image_url", "image_url": normalized_image})
            continue
        raise _error(ErrorCode.INVALID_MESSAGE_CONTENT, "unsupported message content part")
    return parts


def _normalize_tool_calls(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise _error(ErrorCode.INVALID_MESSAGE_CONTENT, "assistant tool_calls must be a non-empty array")
    calls: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise _error(ErrorCode.INVALID_MESSAGE_CONTENT, "assistant tool calls must be objects")
        function = item.get("function")
        if not isinstance(function, dict):
            raise _error(ErrorCode.INVALID_MESSAGE_CONTENT, "assistant tool call function must be an object")
        name = function.get("name")
        arguments = function.get("arguments", "")
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, str):
            raise _error(ErrorCode.INVALID_MESSAGE_CONTENT, "assistant tool call name and arguments are invalid")
        call: dict[str, Any] = {
            "type": "function",
            "function": {"name": name.strip(), "arguments": arguments},
        }
        call_id = item.get("id")
        if isinstance(call_id, str) and call_id.strip():
            call["id"] = call_id.strip()
        calls.append(call)
    return calls


def _normalize_messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise _error(ErrorCode.INVALID_MESSAGES, "messages must be a non-empty array")
    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise _error(ErrorCode.INVALID_MESSAGES, "each message must be an object")
        role = item.get("role")
        if not isinstance(role, str) or role not in ALLOWED_ROLES:
            raise _error(ErrorCode.INVALID_MESSAGE_ROLE, "unsupported message role")
        has_tool_calls = role == "assistant" and "tool_calls" in item
        message: dict[str, Any] = {
            "role": role,
            "content": _normalize_content(item.get("content"), allow_empty=has_tool_calls),
        }
        if has_tool_calls:
            message["tool_calls"] = _normalize_tool_calls(item.get("tool_calls"))
        if role == "tool":
            tool_call_id = item.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                raise _error(ErrorCode.INVALID_MESSAGE_CONTENT, "tool messages require tool_call_id")
            message["tool_call_id"] = tool_call_id.strip()
        messages.append(message)
    return messages


def _normalize_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _error(ErrorCode.INVALID_TOOLS, "tools must be an array")
    tools: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "function":
            raise _error(ErrorCode.INVALID_TOOLS, "tools must be function definitions")
        function = item.get("function")
        if not isinstance(function, dict):
            raise _error(ErrorCode.INVALID_TOOLS, "tool function must be an object")
        name = function.get("name")
        parameters = function.get("parameters", {})
        if not isinstance(name, str) or not name.strip():
            raise _error(ErrorCode.INVALID_TOOLS, "tool function name is required")
        if not isinstance(parameters, dict):
            raise _error(ErrorCode.INVALID_TOOLS, "tool parameters must be an object")
        normalized_function: dict[str, Any] = {"name": name.strip(), "parameters": parameters}
        description = function.get("description")
        if isinstance(description, str) and description.strip():
            normalized_function["description"] = description.strip()
        strict = function.get("strict")
        if isinstance(strict, bool):
            normalized_function["strict"] = strict
        tools.append({"type": "function", "function": normalized_function})
    return tools


def _normalize_tool_choice(value: Any, tool_names: set[str]) -> str | dict[str, Any]:
    if isinstance(value, str) and value in {"auto", "none", "required"}:
        return value
    if isinstance(value, dict) and value.get("type") == "function":
        function = value.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name.strip() and name.strip() in tool_names:
            return {"type": "function", "function": {"name": name.strip()}}
    raise _error(ErrorCode.INVALID_TOOL_CHOICE, "invalid tool_choice")


def _finite_number(value: Any, *, code: ErrorCode, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(code, f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise _error(code, f"{name} is outside the supported range")
    return number


def prepare_gateway_request(value: Any) -> dict[str, Any]:
    """Validate and normalize one credential-free, non-streaming request body."""
    if not isinstance(value, dict):
        raise _error(ErrorCode.INVALID_REQUEST, "request must be a JSON object")
    if _json_size(value) > MAX_REQUEST_BYTES or _depth(value) > MAX_REQUEST_DEPTH:
        raise _error(ErrorCode.REQUEST_TOO_LARGE, "request exceeds the preparation budget")
    lowered = {str(key).lower() for key in value}
    if lowered & FORBIDDEN_TOP_LEVEL_FIELDS:
        raise _error(ErrorCode.INVALID_REQUEST, "credentials and local paths are not accepted")

    raw_model = value.get("model")
    if not isinstance(raw_model, str) or not raw_model.strip():
        raise _error(ErrorCode.UNSUPPORTED_MODEL, "model must be a non-empty string")
    model = normalize_model_name(raw_model)
    if model not in SUPPORTED_MODELS:
        raise _error(ErrorCode.UNSUPPORTED_MODEL, "unsupported model")

    request: dict[str, Any] = {"model": model, "messages": _normalize_messages(value.get("messages"))}
    if value.get("stream") is True:
        raise _error(ErrorCode.INVALID_REQUEST, "streaming requests stay on the Python path")
    if "stream" in value:
        if value.get("stream") is not False:
            raise _error(ErrorCode.INVALID_REQUEST, "stream must be a boolean")
        request["stream"] = False

    tools: list[dict[str, Any]] = []
    if "tools" in value:
        tools = _normalize_tools(value.get("tools"))
        if tools:
            request["tools"] = tools
    if "tool_choice" in value:
        choice = _normalize_tool_choice(value.get("tool_choice"), {tool["function"]["name"] for tool in tools})
        if tools or choice == "none":
            request["tool_choice"] = choice

    if "temperature" in value:
        request["temperature"] = _finite_number(
            value.get("temperature"), code=ErrorCode.INVALID_TEMPERATURE, name="temperature", minimum=0.0, maximum=2.0
        )
    if "top_p" in value:
        request["top_p"] = _finite_number(
            value.get("top_p"), code=ErrorCode.INVALID_REQUEST, name="top_p", minimum=0.0, maximum=1.0
        )
    if "max_tokens" in value:
        max_tokens = value.get("max_tokens")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= MAX_TOKENS:
            raise _error(ErrorCode.INVALID_MAX_TOKENS, "max_tokens is outside the supported range")
        request["max_tokens"] = max_tokens
    if "reasoning_effort" in value:
        effort = value.get("reasoning_effort")
        if effort not in {"minimal", "low", "medium", "high", "max"}:
            raise _error(ErrorCode.INVALID_REQUEST, "invalid reasoning_effort")
        request["reasoning_effort"] = effort
    if "thinking" in value:
        if value.get("thinking") != {"type": "enabled"}:
            raise _error(ErrorCode.INVALID_REQUEST, "invalid thinking configuration")
        request["thinking"] = {"type": "enabled"}
    return request


def _fallback(
    baseline: dict[str, Any], *, reason: str, started: float, details: Any = None
) -> PreparedGatewayRequest:
    from deepseek_infra.infra.rust_core.gateway_client import fallback_to_python_enabled

    if not fallback_to_python_enabled():
        raise AppError(f"Rust Gateway request preparation unavailable: {details or reason}", code=ErrorCode.UPSTREAM_FAILURE, status=502)
    return PreparedGatewayRequest(
        baseline,
        {
            "runtime": "python",
            "fallback": True,
            "fallbackReason": reason,
            "latencyMs": max(0, round((time.perf_counter() - started) * 1000)),
        },
    )


def prepare_request_with_optional_rust(value: Any) -> PreparedGatewayRequest:
    """Return the normalized request and safe runtime diagnostics."""
    baseline = prepare_gateway_request(value)
    from deepseek_infra.infra.rust_core.gateway_client import prepare_request_with_rust, rust_gateway_enabled

    if not rust_gateway_enabled():
        return PreparedGatewayRequest(baseline, {"runtime": "python", "fallback": False})

    started = time.perf_counter()
    result = prepare_request_with_rust(value)
    if not result.ok:
        reason = result.error_kind or "rust_backend_unavailable"
        return _fallback(baseline, reason=reason, started=started, details=result.body)
    if result.error_kind:
        return _fallback(baseline, reason=result.error_kind, started=started, details=result.body)
    response = result.body
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        return _fallback(baseline, reason="rust_invalid_shape", started=started, details=response)
    if response["ok"] is False:
        code = response.get("code")
        message = response.get("message")
        if isinstance(code, str) and code in PREPARATION_ERROR_CODES and isinstance(message, str):
            raise _error(ErrorCode(code), message)
        return _fallback(baseline, reason="rust_invalid_shape", started=started, details=response)
    candidate = response.get("request")
    if not isinstance(candidate, dict):
        return _fallback(baseline, reason="rust_invalid_shape", started=started, details=response)
    try:
        normalized_candidate = prepare_gateway_request(candidate)
        _json_size(normalized_candidate)
    except AppError:
        return _fallback(baseline, reason="rust_defensive_validation_failed", started=started, details=response)
    if normalized_candidate != baseline or set(candidate) - ALLOWED_REQUEST_FIELDS:
        return _fallback(baseline, reason="rust_defensive_validation_failed", started=started, details=response)
    return PreparedGatewayRequest(
        candidate,
        {
            "runtime": "rust",
            "fallback": False,
            "latencyMs": max(0, round((time.perf_counter() - started) * 1000)),
        },
    )
