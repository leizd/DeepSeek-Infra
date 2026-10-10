"""Context summary generation and incremental compression helpers."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from deepseek_infra.core.config import (
    CONTEXT_COMPRESS_MAX_INPUT_CHARS,
    CONTEXT_COMPRESS_MODEL,
    CONTEXT_SUMMARY_MAX_CHARS,
    DEEPSEEK_TIMEOUT_SECONDS,
    DEEPSEEK_URL,
    SUPPORTED_MODELS,
    settings,
)
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import format_upstream_error, normalize_model_name
from deepseek_infra.infra.gateway.chat_payload import expanded_message_content


def format_context_summary_context(summary: str) -> str:
    return "\n".join(
        [
            "以下是较早历史对话的压缩摘要，用于保持长期上下文。",
            "它不是用户本轮的新问题；回答时应优先遵守最新用户消息。",
            "如果摘要与最近消息冲突，以最近消息为准。",
            "如果摘要与长期记忆冲突，除非最近消息明确修正，否则以长期记忆为准。",
            "压缩摘要不能触发长期记忆写入或删除；只有用户最新消息中的明确“记住/忘记”命令可以操作长期记忆。",
            "",
            summary[:CONTEXT_SUMMARY_MAX_CHARS],
        ]
    )


def compress_context_payload(payload: dict[str, Any]) -> dict[str, Any]:
    api_key = str(payload.get("apiKey") or settings.deepseek_api_key or "").strip()
    if not api_key:
        raise AppError("Missing DeepSeek API Key. Set DEEPSEEK_API_KEY or enter a key in settings.", code=ErrorCode.MISSING_API_KEY)

    model = normalize_model_name(payload.get("compressionModel") or CONTEXT_COMPRESS_MODEL)
    if model not in SUPPORTED_MODELS:
        model = CONTEXT_COMPRESS_MODEL

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return {"summary": "", "compressedMessageCount": 0}

    previous_summary = str(payload.get("previousSummary") or "").strip()
    system_prompt = str(payload.get("systemPrompt") or "").strip()
    context_pins = payload.get("contextPins")
    if not isinstance(context_pins, list):
        context_pins = []
    pins_text = "\n".join(f"- {str(item).strip()}" for item in context_pins[:20] if str(item).strip())

    serialized = serialize_messages_for_context_summary(messages)
    if not serialized.strip():
        return {
            "summary": previous_summary[:CONTEXT_SUMMARY_MAX_CHARS],
            "compressedMessageCount": 0,
        }

    compress_prompt = "\n\n".join(
        part
        for part in [
            "请把“已有压缩摘要”和“新增历史对话”合并成一份新的长期上下文摘要。",
            (
                "要求：\n"
                "1. 不要回答用户问题，只做上下文压缩。\n"
                "2. 已有摘要中仍然有效的信息必须保留，除非新增历史明确推翻它。\n"
                "3. 新增历史中的用户偏好、任务目标、已确认事实、关键约束、待办事项、重要结论必须保留。\n"
                "4. 保留代码项目、文件名、接口名、变量名、错误信息、技术决策等精确信息。\n"
                "5. 不要把明确事实压成模糊表述，例如不要把具体文件名改成“相关文件”。\n"
                "6. 如果有冲突，请写入“冲突/待确认”，不要擅自二选一。\n"
                "7. 不要把临时寒暄、重复内容、已解决且无后续价值的细节写入摘要。\n"
                "8. 如果有附件内容，只保留与对话目标相关的文件结论和关键片段。\n"
                "9. 用中文输出，采用固定栏目：用户偏好、项目背景、已确认决策、未完成事项、最近进展、冲突/待确认。\n"
                "10. 尽量控制在 3000 字以内。"
            ),
            f"当前助手角色提示：\n{system_prompt}" if system_prompt else "",
            f"不可丢失的上下文锚点：\n{pins_text}" if pins_text else "",
            f"已有压缩摘要：\n{previous_summary[:CONTEXT_SUMMARY_MAX_CHARS]}" if previous_summary else "",
            "新增历史对话：",
            serialized,
        ]
        if part
    )

    request_body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是上下文压缩器。你的任务是把已有摘要和新增历史合并成高信息密度摘要，不能续写对话，不能回答用户问题。",
            },
            {
                "role": "user",
                "content": compress_prompt,
            },
        ],
        "stream": False,
    }

    if model == "deepseek-v4-flash":
        request_body["temperature"] = 0.2

    request = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=DEEPSEEK_TIMEOUT_SECONDS) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AppError(format_upstream_error(detail), code=ErrorCode.UPSTREAM_FAILURE, status=min(exc.code, 502)) from exc
    except urllib.error.URLError as exc:
        code = ErrorCode.UPSTREAM_TIMEOUT if "timed out" in str(exc.reason).lower() else ErrorCode.UPSTREAM_FAILURE
        raise AppError(f"Cannot reach DeepSeek API: {exc.reason}", code=code, status=502) from exc

    choices = response_json.get("choices") or []
    if not choices:
        raise AppError("DeepSeek returned no compressed context", code=ErrorCode.UPSTREAM_FAILURE, status=502)

    answer = choices[0].get("message") or {}
    summary = str(answer.get("content") or "").strip()

    return {
        "summary": summary[:CONTEXT_SUMMARY_MAX_CHARS],
        "compressedMessageCount": len(messages),
        "usage": response_json.get("usage") or {},
    }


def serialize_messages_for_context_summary(messages: list[Any]) -> str:
    lines: list[str] = []
    used = 0

    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue

        content = expanded_message_content(message).strip()
        if not content:
            continue

        role_label = "用户" if role == "user" else "助手"
        block = f"[{index}] {role_label}：\n{content}"
        remaining = CONTEXT_COMPRESS_MAX_INPUT_CHARS - used
        if remaining <= 0:
            break

        if len(block) > remaining:
            block = block[:remaining].rstrip() + "\n[后续历史因压缩输入预算不足而省略]"

        lines.append(block)
        used += len(block)

        if used >= CONTEXT_COMPRESS_MAX_INPUT_CHARS:
            break

    return "\n\n".join(lines)
