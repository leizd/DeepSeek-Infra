"""Frontend message expansion and attachment counting."""

from __future__ import annotations

from typing import Any

from deepseek_infra.infra.rag.files import build_attachment_context


def expanded_message_content(message: dict[str, Any]) -> str:
    content = str(message.get("content") or "").strip()
    attachments = message.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return content

    attachment_context = build_attachment_context(attachments, content)
    if not attachment_context:
        return content
    return f"{content or '请根据附件内容回答。'}\n\n{attachment_context}".strip()


def count_payload_attachments(messages: Any) -> int:
    if not isinstance(messages, list):
        return 0
    count = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        attachments = message.get("attachments")
        if isinstance(attachments, list):
            count += sum(1 for item in attachments if isinstance(item, dict))
    return count
