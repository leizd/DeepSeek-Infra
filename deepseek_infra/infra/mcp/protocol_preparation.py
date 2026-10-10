"""Deterministic MCP JSON-RPC protocol preparation.

The preparation layer is deliberately side-effect free: it validates and
normalizes one JSON value, then describes which Python-owned route should
handle it.  It never reads credentials, opens transports, manages sessions,
or executes tools/resources/prompts.
"""

from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

MCP_PROTOCOL_PREPARATION_MAX_BYTES = 2_000_000
MCP_PROTOCOL_PREPARATION_MAX_DEPTH = 32

SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-06-18"})
SUPPORTED_REQUEST_METHODS = frozenset(
    {
        "initialize",
        "ping",
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/read",
        "prompts/list",
        "prompts/get",
    }
)
SUPPORTED_NOTIFICATION_METHODS = frozenset({"notifications/initialized"})

_JSONRPC_CODES = {
    "parse_error": -32700,
    "invalid_request": -32600,
    "invalid_jsonrpc_version": -32600,
    "invalid_request_id": -32600,
    "invalid_method": -32600,
    "method_not_supported": -32601,
    "invalid_params": -32602,
    "invalid_initialize_request": -32602,
    "invalid_capabilities": -32602,
    "invalid_protocol_version": -32602,
    "request_too_large": -32600,
    "nesting_limit_exceeded": -32600,
}

logger = logging.getLogger("deepseek_infra.mcp.protocol_preparation")


@dataclass(frozen=True, slots=True)
class McpProtocolDecision:
    """A preparation result plus safe operational diagnostics."""

    preparation: dict[str, Any]
    diagnostics: dict[str, Any]


def _error(
    code: str,
    message: str,
    *,
    notification: bool = False,
    message_type: str = "",
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": False,
        "code": code,
        "jsonRpcCode": _JSONRPC_CODES[code],
        "message": message,
        "notification": notification,
    }
    if message_type:
        response["messageType"] = message_type
    return response


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON number: {token}")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _max_depth(value: Any) -> int:
    if not isinstance(value, (dict, list)) or not value:
        return 1
    stack: list[tuple[Any, int]] = [(value, 1)]
    maximum = 1
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        if depth > MCP_PROTOCOL_PREPARATION_MAX_DEPTH:
            return depth
        children = current.values() if isinstance(current, dict) else current
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    return maximum


def _valid_request_id(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    return isinstance(value, int) and not isinstance(value, bool) and -(2**63) <= value <= 2**63 - 1


def request_id_type(value: Any, *, present: bool = True) -> str:
    if not present:
        return "absent"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "invalid"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    return "invalid"


def _notification_hint(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    method = value.get("method")
    return "id" not in value or (isinstance(method, str) and method.startswith("notifications/"))


def _normalize_params(message: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
    if "params" not in message:
        return {}
    raw_params = message.get("params")
    if isinstance(raw_params, dict):
        return raw_params
    # This matches the established Python server, which treats non-object
    # params as an empty object before method-level validation.
    normalized["params"] = {}
    return {}


def _validate_initialize(params: dict[str, Any]) -> dict[str, Any] | None:
    if "protocolVersion" in params:
        version = params.get("protocolVersion")
        if not isinstance(version, str) or version not in SUPPORTED_PROTOCOL_VERSIONS:
            return _error(
                "invalid_protocol_version",
                "initialize protocolVersion is not supported",
                message_type="request",
            )
    if "capabilities" in params and not isinstance(params.get("capabilities"), dict):
        return _error(
            "invalid_capabilities",
            "initialize capabilities must be an object",
            message_type="request",
        )
    if "clientInfo" in params:
        client_info = params.get("clientInfo")
        if not isinstance(client_info, dict):
            return _error(
                "invalid_initialize_request",
                "initialize clientInfo must be an object",
                message_type="request",
            )
        name = client_info.get("name")
        if not isinstance(name, str) or not name.strip():
            return _error(
                "invalid_initialize_request",
                "initialize clientInfo.name is required",
                message_type="request",
            )
        version = client_info.get("version")
        if version is not None and not isinstance(version, str):
            return _error(
                "invalid_initialize_request",
                "initialize clientInfo.version must be a string",
                message_type="request",
            )
    return None


def _validate_method_params(
    method: str,
    params: dict[str, Any],
    normalized: dict[str, Any],
    *,
    message_type: str,
) -> dict[str, Any] | None:
    notification = message_type == "notification"
    if method == "initialize":
        error = _validate_initialize(params)
        if error is not None:
            error["notification"] = notification
            error["messageType"] = message_type
        return error
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            return _error(
                "invalid_params",
                "tools/call name must be a non-empty string",
                notification=notification,
                message_type=message_type,
            )
        normalized_params = normalized.get("params")
        if isinstance(normalized_params, dict):
            normalized_params["name"] = name.strip()
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return _error(
                "invalid_params",
                "tools/call arguments must be an object",
                notification=notification,
                message_type=message_type,
            )
    elif method == "resources/read":
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.strip():
            return _error(
                "invalid_params",
                "resources/read uri must be a non-empty string",
                notification=notification,
                message_type=message_type,
            )
        normalized_params = normalized.get("params")
        if isinstance(normalized_params, dict):
            normalized_params["uri"] = uri.strip()
    elif method == "prompts/get":
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            return _error(
                "invalid_params",
                "prompts/get name must be a non-empty string",
                notification=notification,
                message_type=message_type,
            )
        normalized_params = normalized.get("params")
        if isinstance(normalized_params, dict):
            normalized_params["name"] = name.strip()
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return _error(
                "invalid_params",
                "prompts/get arguments must be an object",
                notification=notification,
                message_type=message_type,
            )
    return None


def _prepare_response(message: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
    if "id" not in message or not _valid_request_id(message.get("id")):
        return _error("invalid_request_id", "response id is invalid", message_type="response")
    has_result = "result" in message
    has_error = "error" in message
    if has_result == has_error:
        return _error(
            "invalid_request",
            "response must contain exactly one of result or error",
            message_type="response",
        )
    if has_error and not isinstance(message.get("error"), dict):
        return _error("invalid_request", "response error must be an object", message_type="response")
    return {
        "ok": True,
        "messageType": "response",
        "request": normalized,
        "routing": {"owner": "python", "category": "response"},
    }


def _category(method: str) -> str:
    if method == "initialize" or method.startswith("notifications/"):
        return "lifecycle"
    if method == "ping":
        return "control"
    return method.split("/", 1)[0]


def prepare_mcp_protocol(
    value: Any,
    *,
    payload_size: int | None = None,
) -> dict[str, Any]:
    """Return a normalized protocol descriptor or a stable protocol error."""
    try:
        encoded = _json_bytes(value)
    except (TypeError, ValueError):
        return _error("invalid_request", "message must be safely JSON serializable")
    size = len(encoded) if payload_size is None else max(0, int(payload_size))
    if size > MCP_PROTOCOL_PREPARATION_MAX_BYTES:
        return _error("request_too_large", "MCP request exceeds the preparation budget")
    if _max_depth(value) > MCP_PROTOCOL_PREPARATION_MAX_DEPTH:
        return _error("nesting_limit_exceeded", "MCP request nesting is too deep")
    if not isinstance(value, dict):
        return _error("invalid_request", "MCP message must be a JSON object")

    notification = _notification_hint(value)
    if value.get("jsonrpc") != "2.0":
        return _error(
            "invalid_jsonrpc_version",
            "jsonrpc must be '2.0'",
            notification=notification,
            message_type="notification" if notification else "request",
        )

    normalized = deepcopy(value)
    raw_method = value.get("method")
    if raw_method is None and ("result" in value or "error" in value):
        return _prepare_response(value, normalized)
    if not isinstance(raw_method, str) or not raw_method or raw_method != raw_method.strip():
        return _error(
            "invalid_method",
            "method must be a non-empty normalized string",
            notification=notification,
            message_type="notification" if notification else "request",
        )

    message_type = "notification" if notification else "request"
    if "id" in value and not _valid_request_id(value.get("id")):
        return _error(
            "invalid_request_id",
            "request id must be a string, signed 64-bit integer, or null",
            notification=notification,
            message_type=message_type,
        )

    supported = SUPPORTED_NOTIFICATION_METHODS if raw_method.startswith("notifications/") else SUPPORTED_REQUEST_METHODS
    if raw_method not in supported:
        return _error(
            "method_not_supported",
            "method is not supported by the Python MCP server",
            notification=notification,
            message_type=message_type,
        )

    params = _normalize_params(value, normalized)
    error = _validate_method_params(raw_method, params, normalized, message_type=message_type)
    if error is not None:
        return error

    return {
        "ok": True,
        "messageType": message_type,
        "request": normalized,
        "routing": {"owner": "python", "category": _category(raw_method)},
    }


def prepare_mcp_protocol_json(raw: str | bytes) -> dict[str, Any]:
    """Parse and prepare raw JSON for parity and sidecar contract tests."""
    encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(encoded) > MCP_PROTOCOL_PREPARATION_MAX_BYTES:
        return _error("request_too_large", "MCP request exceeds the preparation budget")
    try:
        text = encoded.decode("utf-8")
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _error("parse_error", "MCP request must contain valid JSON")
    return prepare_mcp_protocol(value, payload_size=len(encoded))


def _safe_payload_size(value: Any) -> int:
    try:
        return len(_json_bytes(value))
    except (TypeError, ValueError):
        return 0


def _diagnostics(
    value: Any,
    preparation: dict[str, Any],
    *,
    python_preparation_us: int,
    total_started_ns: int,
) -> dict[str, Any]:
    method = value.get("method") if isinstance(value, dict) and isinstance(value.get("method"), str) else ""
    present = isinstance(value, dict) and "id" in value
    identifier = value.get("id") if isinstance(value, dict) else None
    return {
        "method": method,
        "messageType": str(preparation.get("messageType") or "invalid"),
        "requestIdType": request_id_type(identifier, present=present),
        "payloadSize": _safe_payload_size(value),
        "runtime": "python",
        "fallback": False,
        "fallbackReason": "",
        "latencyMs": 0,
        "pythonPreparationUs": python_preparation_us,
        "serializationUs": None,
        "transportUs": None,
        "rustProcessingUs": None,
        "pythonValidationUs": None,
        "totalDelegateUs": max(0, (time.perf_counter_ns() - total_started_ns) // 1000),
        "requestBytes": 0,
        "responseBytes": 0,
        "connectionReused": None,
        "connectionCount": None,
        "correlationId": "",
    }


def _validate_rust_success(
    local: dict[str, Any],
    candidate: Any,
) -> tuple[bool, str]:
    if not isinstance(candidate, dict):
        return False, "rust_response_not_object"
    try:
        _json_bytes(candidate)
    except (TypeError, ValueError):
        return False, "rust_response_not_serializable"
    if candidate.get("ok") is not True:
        return False, "rust_contract_invalid"
    if candidate.get("messageType") not in {"request", "notification", "response"}:
        return False, "rust_message_type_invalid"
    routing = candidate.get("routing")
    if not isinstance(routing, dict) or routing.get("owner") != "python":
        return False, "rust_routing_owner_invalid"
    local_request = local.get("request")
    candidate_request = candidate.get("request")
    if isinstance(local_request, dict) and local_request.get("method") == "tools/call":
        local_params = local_request.get("params")
        candidate_params = candidate_request.get("params") if isinstance(candidate_request, dict) else None
        local_arguments = local_params.get("arguments") if isinstance(local_params, dict) else None
        candidate_arguments = candidate_params.get("arguments") if isinstance(candidate_params, dict) else None
        if candidate_arguments != local_arguments:
            return False, "rust_tool_arguments_changed"
    if candidate != local:
        return False, "rust_semantic_divergence"
    return True, ""


def prepare_mcp_protocol_with_optional_rust(value: Any) -> McpProtocolDecision:
    """Prepare locally, optionally verify/adopt the Rust equivalent."""
    total_started_ns = time.perf_counter_ns()
    preparation_started_ns = time.perf_counter_ns()
    local = prepare_mcp_protocol(value)
    python_preparation_us = max(0, (time.perf_counter_ns() - preparation_started_ns) // 1000)
    diagnostics = _diagnostics(
        value,
        local,
        python_preparation_us=python_preparation_us,
        total_started_ns=total_started_ns,
    )
    if local.get("ok") is not True:
        diagnostics["totalDelegateUs"] = max(0, (time.perf_counter_ns() - total_started_ns) // 1000)
        return McpProtocolDecision(local, diagnostics)

    from deepseek_infra.infra.rust_core import mcp_client

    if not mcp_client.rust_mcp_enabled():
        diagnostics["totalDelegateUs"] = max(0, (time.perf_counter_ns() - total_started_ns) // 1000)
        return McpProtocolDecision(local, diagnostics)

    result = mcp_client.prepare_mcp_with_rust(value)
    diagnostics.update(
        serializationUs=getattr(result, "serialization_us", None),
        transportUs=getattr(result, "transport_us", None),
        rustProcessingUs=getattr(result, "rust_processing_us", None),
        requestBytes=int(getattr(result, "request_bytes", 0) or 0),
        responseBytes=int(getattr(result, "response_bytes", 0) or 0),
        connectionReused=getattr(result, "connection_reused", None),
        connectionCount=getattr(result, "connection_count", None),
        correlationId=str(getattr(result, "correlation_id", "") or ""),
    )
    if not result.ok:
        total_us = max(0, (time.perf_counter_ns() - total_started_ns) // 1000)
        diagnostics.update(totalDelegateUs=total_us, latencyMs=max(0, round(total_us / 1000)))
        diagnostics.update(
            runtime="python",
            fallback=True,
            fallbackReason=result.error_kind or "rust_backend_unavailable",
        )
        return McpProtocolDecision(local, diagnostics)

    validation_started_ns = time.perf_counter_ns()
    valid, reason = _validate_rust_success(local, result.body)
    validation_us = max(0, (time.perf_counter_ns() - validation_started_ns) // 1000)
    total_us = max(0, (time.perf_counter_ns() - total_started_ns) // 1000)
    diagnostics.update(
        pythonValidationUs=validation_us,
        totalDelegateUs=total_us,
        latencyMs=max(0, round(total_us / 1000)),
    )
    if not valid:
        diagnostics.update(runtime="python", fallback=True, fallbackReason=reason)
        return McpProtocolDecision(local, diagnostics)

    diagnostics.update(runtime="rust", fallback=False, fallbackReason="")
    return McpProtocolDecision(result.body, diagnostics)


def protocol_error_response(
    preparation: dict[str, Any],
    original: Any,
) -> dict[str, Any] | None:
    """Map an internal preparation error to the established JSON-RPC shape."""
    if preparation.get("notification") is True:
        return None
    message_id = original.get("id") if isinstance(original, dict) and _valid_request_id(original.get("id")) else None
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {
            "code": int(preparation.get("jsonRpcCode") or -32600),
            "message": str(preparation.get("message") or "Invalid request"),
            "data": {"code": str(preparation.get("code") or "invalid_request")},
        },
    }


def log_mcp_protocol_diagnostics(diagnostics: dict[str, Any]) -> None:
    """Emit only allowlisted, redacted preparation metadata."""
    logger.info(
        "mcp_protocol_preparation",
        extra={
            "component": "mcp_prepare",
            "message_type": str(diagnostics.get("messageType") or "invalid"),
            "request_id_type": str(diagnostics.get("requestIdType") or "invalid"),
            "payload_bytes": int(diagnostics.get("payloadSize") or 0),
            "runtime": str(diagnostics.get("runtime") or "python"),
            "fallback": bool(diagnostics.get("fallback")),
            "fallback_reason": str(diagnostics.get("fallbackReason") or ""),
            "duration_us": int(diagnostics.get("totalDelegateUs") or 0),
            "correlation_id": str(diagnostics.get("correlationId") or ""),
        },
    )


def protocol_diagnostic_headers(diagnostics: dict[str, Any]) -> dict[str, str]:
    """Expose safe E2E evidence without changing JSON-RPC response bodies."""
    return {
        "X-DeepSeek-MCP-Preparation-Runtime": str(diagnostics.get("runtime") or "python"),
        "X-DeepSeek-MCP-Preparation-Fallback": "1" if diagnostics.get("fallback") else "0",
        "X-DeepSeek-MCP-Preparation-Fallback-Reason": str(diagnostics.get("fallbackReason") or ""),
        "X-DeepSeek-MCP-Preparation-Latency-Ms": str(max(0, int(diagnostics.get("latencyMs") or 0))),
    }
