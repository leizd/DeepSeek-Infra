"""MCP catalogs: map the local runtime onto MCP Tools / Resources / Prompts.

* **Tools** — every tool from ``available_tool_definitions()`` (OpenAI function
  format) is re-described as an MCP tool ``{name, description, inputSchema,
  annotations}``, filtered to the hub's capability slice. Annotations come from
  the Tool Policy risk cards so clients can render read-only / open-world hints.
* **Resources** — generated artifacts (``.generated`` pptx/docx/pdf/svg) under
  ``generated://<fileId>`` plus a ``runtime://capabilities`` status document.
* **Prompts** — small reusable prompt templates for common runtime workflows.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from deepseek_infra.core.config import GENERATED_DIR, MCP_EXPOSE_PROMPTS, MCP_EXPOSE_RESOURCES
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.mcp.permissions import allowed_tool_names, hub_capability
from deepseek_infra.infra.tool_runtime.generated_files import GENERATED_MEDIA_TYPES, resolve_generated_file
from deepseek_infra.infra.tool_runtime.tool_policy import TOOL_METADATA, tool_policy_status
from deepseek_infra.infra.tool_runtime.tools import available_tool_definitions

GENERATED_URI_PREFIX = "generated://"
RUNTIME_CAPABILITIES_URI = "runtime://capabilities"

# Tools that mutate local state; everything else is advertised read-only.
_MUTATING_TOOLS = {"create_pptx", "create_document", "create_mindmap", "create_reminder", "suggest_memory", "forget_memory"}


def mcp_tools() -> list[dict[str, Any]]:
    """The hub's tool catalog in MCP shape, scoped to the configured capability."""
    allowed = set(allowed_tool_names())
    tools: list[dict[str, Any]] = []
    for definition in available_tool_definitions():
        function = definition.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        if not name or name not in allowed:
            continue
        meta = TOOL_METADATA.get(name)
        parameters = function.get("parameters")
        tool: dict[str, Any] = {
            "name": name,
            "description": str(function.get("description") or ""),
            "inputSchema": parameters if isinstance(parameters, dict) else {"type": "object"},
        }
        if meta is not None:
            tool["annotations"] = {
                "title": name,
                "readOnlyHint": name not in _MUTATING_TOOLS,
                "destructiveHint": bool(meta.requires_confirm),
                "openWorldHint": bool(meta.network),
            }
        tools.append(tool)
    return tools


def mcp_resources() -> list[dict[str, Any]]:
    """Generated artifacts + the runtime capability document as MCP resources."""
    if not MCP_EXPOSE_RESOURCES:
        return []
    resources: list[dict[str, Any]] = [
        {
            "uri": RUNTIME_CAPABILITIES_URI,
            "name": "runtime-capabilities",
            "title": "DeepSeek Infra tool policy & capability profiles",
            "mimeType": "application/json",
        }
    ]
    try:
        if GENERATED_DIR.exists():
            for ext, media_type in GENERATED_MEDIA_TYPES.items():
                for path in sorted(GENERATED_DIR.glob(f"*.{ext}")):
                    resources.append(
                        {
                            "uri": f"{GENERATED_URI_PREFIX}{path.stem}",
                            "name": path.name,
                            "title": f"Generated artifact {path.name}",
                            "mimeType": media_type,
                        }
                    )
    except OSError:
        pass
    return resources


def read_mcp_resource(uri: str) -> list[dict[str, Any]]:
    """Resolve one resource URI into MCP ``contents`` (text or base64 blob)."""
    value = str(uri or "").strip()
    if not MCP_EXPOSE_RESOURCES:
        raise AppError("MCP resources are disabled", code=ErrorCode.FORBIDDEN, status=403)
    if value == RUNTIME_CAPABILITIES_URI:
        document = {"capability": hub_capability(), "toolPolicy": tool_policy_status()}
        return [
            {
                "uri": value,
                "mimeType": "application/json",
                "text": json.dumps(document, ensure_ascii=False, sort_keys=True),
            }
        ]
    if value.startswith(GENERATED_URI_PREFIX):
        file_id = value[len(GENERATED_URI_PREFIX) :]
        path = resolve_generated_file(file_id)
        if path is None:
            raise AppError("Resource not found", code=ErrorCode.NOT_FOUND, status=404)
        media_type = GENERATED_MEDIA_TYPES.get(path.suffix.lower().lstrip("."), "application/octet-stream")
        if media_type == "image/svg+xml":
            return [{"uri": value, "mimeType": media_type, "text": path.read_text(encoding="utf-8")}]
        return [
            {
                "uri": value,
                "mimeType": media_type,
                "blob": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        ]
    raise AppError("Resource not found", code=ErrorCode.NOT_FOUND, status=404)


# --- Prompts ---------------------------------------------------------------------

_PROMPTS: dict[str, dict[str, Any]] = {
    "slides-outline": {
        "name": "slides-outline",
        "title": "生成 PPT 大纲并落成文件",
        "description": "围绕一个主题规划 6-10 页演示文稿大纲，并调用 create_pptx 生成真实的 .pptx 文件。",
        "arguments": [
            {"name": "topic", "description": "演示文稿主题", "required": True},
            {"name": "audience", "description": "目标听众（可选）", "required": False},
        ],
    },
    "research-brief": {
        "name": "research-brief",
        "title": "联网检索并输出带引用的简报",
        "description": "用 web_search / fetch_url 检索一个主题，输出带 [^Wn] 引用标记的事实简报。",
        "arguments": [
            {"name": "topic", "description": "要调研的主题", "required": True},
        ],
    },
}


def mcp_prompts() -> list[dict[str, Any]]:
    if not MCP_EXPOSE_PROMPTS:
        return []
    return [dict(prompt) for prompt in _PROMPTS.values()]


def get_mcp_prompt(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    if not MCP_EXPOSE_PROMPTS:
        raise AppError("MCP prompts are disabled", code=ErrorCode.FORBIDDEN, status=403)
    prompt = _PROMPTS.get(str(name or "").strip())
    if prompt is None:
        raise AppError("Prompt not found", code=ErrorCode.NOT_FOUND, status=404)
    args = arguments if isinstance(arguments, dict) else {}
    topic = str(args.get("topic") or "").strip() or "（未提供主题）"
    if prompt["name"] == "slides-outline":
        audience = str(args.get("audience") or "").strip()
        audience_line = f"目标听众：{audience}。" if audience else ""
        text = (
            f"请为主题「{topic}」规划一份 6-10 页的演示文稿大纲。{audience_line}"
            "每页一个结论式标题，3-6 个 lead：detail 形式的要点，并为关键页选择 cards/process/comparison/summary 版式；"
            "随后调用 create_pptx 工具生成真实的 .pptx 文件并返回下载链接。"
        )
    else:
        text = (
            f"请围绕主题「{topic}」做联网调研：先用 web_search 检索（必要时 fetch_url 读取关键页面），"
            "然后输出一份事实简报：核心结论、关键事实（每条后跟来源的 [^Wn] 引用标记）、不确定之处。"
        )
    return {
        "description": str(prompt.get("description") or ""),
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }
