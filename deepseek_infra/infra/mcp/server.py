"""MCP JSON-RPC 2.0 server: the protocol front of the local Tool Hub.

Implements the Streamable-HTTP style single-endpoint exchange (one JSON-RPC
message in, one JSON response out; notifications return nothing) for the MCP
methods the hub supports::

    initialize / notifications/initialized / ping
    tools/list      tools/call
    resources/list  resources/read
    prompts/list    prompts/get

Standard JSON-RPC error codes are used (-32700 parse, -32600 invalid request,
-32601 method not found, -32602 invalid params, -32603 internal). Transport
auth is the host app's local token (the ``/mcp`` route is auth-gated), so the
hub is never exposed beyond the device unless the user shares the URL+token.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from deepseek_infra.core.config import APP_VERSION, MCP_ENABLED, MCP_EXPOSE_PROMPTS, MCP_EXPOSE_RESOURCES, settings
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.mcp.adapters import call_hub_tool
from deepseek_infra.infra.mcp.permissions import hub_capability
from deepseek_infra.infra.mcp.registry import get_mcp_prompt, mcp_prompts, mcp_resources, mcp_tools, read_mcp_resource

logger = logging.getLogger("deepseek_infra.mcp")

MCP_PROTOCOL_VERSION = "2025-06-18"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

SERVER_INSTRUCTIONS = (
    "DeepSeek Infra 本地 Tool Hub：搜索、抓取、本地文件检索、Python 计算、图表、思维导图、"
    "PPT/Word/PDF 生成、记忆与提醒。所有工具调用都经过本地 Tool Policy 安全闸门。"
)


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    capabilities: dict[str, Any] = {"tools": {"listChanged": False}}
    if MCP_EXPOSE_RESOURCES:
        capabilities["resources"] = {"subscribe": False, "listChanged": False}
    if MCP_EXPOSE_PROMPTS:
        capabilities["prompts"] = {"listChanged": False}
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": capabilities,
        "serverInfo": {
            "name": "deepseek-infra",
            "title": "DeepSeek Infra MCP Tool Hub",
            "version": APP_VERSION,
        },
        "instructions": SERVER_INSTRUCTIONS,
    }


def _tools_list(params: dict[str, Any]) -> dict[str, Any]:
    return {"tools": mcp_tools()}


def _tools_call(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "").strip()
    if not name:
        raise _InvalidParams("name is required")
    arguments = params.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        raise _InvalidParams("arguments must be an object")
    return call_hub_tool(name, arguments, meta=params.get("_meta"))


def _resources_list(params: dict[str, Any]) -> dict[str, Any]:
    return {"resources": mcp_resources()}


def _resources_read(params: dict[str, Any]) -> dict[str, Any]:
    uri = str(params.get("uri") or "").strip()
    if not uri:
        raise _InvalidParams("uri is required")
    return {"contents": read_mcp_resource(uri)}


def _prompts_list(params: dict[str, Any]) -> dict[str, Any]:
    return {"prompts": mcp_prompts()}


def _prompts_get(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "").strip()
    if not name:
        raise _InvalidParams("name is required")
    arguments = params.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        raise _InvalidParams("arguments must be an object")
    return get_mcp_prompt(name, arguments)


def _ping(params: dict[str, Any]) -> dict[str, Any]:
    return {}


class _InvalidParams(Exception):
    pass


_METHODS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "initialize": _initialize,
    "ping": _ping,
    "tools/list": _tools_list,
    "tools/call": _tools_call,
    "resources/list": _resources_list,
    "resources/read": _resources_read,
    "prompts/list": _prompts_list,
    "prompts/get": _prompts_get,
}


def handle_mcp_message(message: Any) -> dict[str, Any] | None:
    """Dispatch one JSON-RPC message. Returns ``None`` for notifications."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Request must be a JSON object")
    message_id = message.get("id")
    is_notification = "id" not in message
    if message.get("jsonrpc") != "2.0":
        return None if is_notification else _error(message_id, INVALID_REQUEST, "jsonrpc must be '2.0'")
    method = str(message.get("method") or "")
    if not method:
        return None if is_notification else _error(message_id, INVALID_REQUEST, "method is required")
    if method.startswith("notifications/"):
        return None
    handler = _METHODS.get(method)
    if handler is None:
        return None if is_notification else _error(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    raw_params = message.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    try:
        result = handler(params)
    except _InvalidParams as exc:
        return None if is_notification else _error(message_id, INVALID_PARAMS, str(exc))
    except AppError as exc:
        return None if is_notification else _error(message_id, INVALID_PARAMS, str(exc), data={"code": exc.code.value})
    except Exception:
        logger.exception("mcp_method_failed", extra={"method": method})
        return None if is_notification else _error(message_id, INTERNAL_ERROR, "Internal error")
    return None if is_notification else _result(message_id, result)


def mcp_status() -> dict[str, Any]:
    """Status block for ``/api/config`` and ``GET /api/mcp``."""
    return {
        "enabled": MCP_ENABLED,
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "endpoint": "/mcp",
        "capability": hub_capability(),
        "toolCount": len(mcp_tools()) if MCP_ENABLED else 0,
        "exposeResources": MCP_EXPOSE_RESOURCES,
        "exposePrompts": MCP_EXPOSE_PROMPTS,
        "client": {
            "enabled": settings.mcp.client_enabled,
            "servers": [{"name": name, "url": url} for name, url in settings.mcp.client_servers],
        },
    }
