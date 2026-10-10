"""Generate short conversation titles with DeepSeek."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from deepseek_infra.core.config import (
    CONTEXT_COMPRESS_MODEL,
    DEEPSEEK_TIMEOUT_SECONDS,
    DEEPSEEK_URL,
    SUPPORTED_MODELS,
    settings,
)
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import format_upstream_error, normalize_model_name

TITLE_MAX_CHARS = 24
TITLE_RATE_LIMIT_COUNT = 12
TITLE_RATE_LIMIT_WINDOW_SECONDS = 60
TITLE_SYSTEM_PROMPT = (
    "你是一个对话标题生成器。根据用户首轮提问和助手首轮回复摘要生成短标题。\n"
    "要求：只返回标题纯文本；中文不超过14个汉字，英文不超过6个单词；"
    "不要加引号、句号、标签或 emoji；抓住具体话题，避免空话；"
    "闲聊或问候返回“闲聊”；优先使用用户主要语言。"
)
TITLE_PREFIXES = ("标题：", "标题:", "Title:", "title:")
TITLE_STRIP_CHARS = "「」『』《》\"'“”‘’` \t\n\r"
TITLE_TRAILING_PUNCTUATION = ("。", ".", "，", ",", "！", "!", "？", "?", "；", ";", "：", ":")

_TITLE_RATE_LIMIT_LOCK = threading.RLock()
_TITLE_RATE_LIMITS: dict[str, list[float]] = {}


def generate_title_payload(payload: dict[str, Any]) -> dict[str, str]:
    api_key = str(payload.get("apiKey") or settings.deepseek_api_key or "").strip()
    if not api_key:
        raise AppError("Missing DeepSeek API Key.", code=ErrorCode.MISSING_API_KEY)

    user_text = _truncate(str(payload.get("userMessage") or ""), 1200)
    assistant_text = _truncate(str(payload.get("assistantMessage") or ""), 600)
    if not user_text.strip():
        return {"title": ""}

    check_title_rate_limit(api_key)

    model = normalize_model_name(payload.get("titleModel") or CONTEXT_COMPRESS_MODEL)
    if model not in SUPPORTED_MODELS:
        model = CONTEXT_COMPRESS_MODEL

    request_body = {
        "model": model,
        "stream": False,
        "thinking": {"type": "disabled"},
        "temperature": 0.3,
        "max_tokens": 60,
        "messages": [
            {"role": "system", "content": TITLE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"用户首轮提问:\n{user_text}\n\n"
                    f"助手首轮回复摘要:\n{assistant_text or '（暂无）'}\n\n"
                    "请直接给出标题。"
                ),
            },
        ],
    }

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
        with urllib.request.urlopen(request, timeout=min(DEEPSEEK_TIMEOUT_SECONDS, 20)) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AppError(format_upstream_error(detail), code=ErrorCode.UPSTREAM_FAILURE, status=min(exc.code, 502)) from exc
    except urllib.error.URLError as exc:
        code = ErrorCode.UPSTREAM_TIMEOUT if "timed out" in str(exc.reason).lower() else ErrorCode.UPSTREAM_FAILURE
        raise AppError(f"Cannot reach DeepSeek API: {exc.reason}", code=code, status=502) from exc

    choices = response_json.get("choices") or []
    raw_title = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        if isinstance(message, dict):
            raw_title = str(message.get("content") or "")
    return {"title": _sanitize_title(raw_title)}


def check_title_rate_limit(api_key: str) -> None:
    now = time.monotonic()
    key = hashlib.sha256(api_key.encode("utf-8", errors="ignore")).hexdigest()[:16]
    with _TITLE_RATE_LIMIT_LOCK:
        recent = [item for item in _TITLE_RATE_LIMITS.get(key, []) if now - item < TITLE_RATE_LIMIT_WINDOW_SECONDS]
        if len(recent) >= TITLE_RATE_LIMIT_COUNT:
            _TITLE_RATE_LIMITS[key] = recent
            raise AppError("Title generation is temporarily rate limited.", code=ErrorCode.RATE_LIMITED, status=429)
        recent.append(now)
        _TITLE_RATE_LIMITS[key] = recent


def _truncate(value: str, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}..."


def _sanitize_title(value: str) -> str:
    title = str(value or "").strip()
    title = title.strip(TITLE_STRIP_CHARS).strip()
    for prefix in TITLE_PREFIXES:
        if title.startswith(prefix):
            title = title[len(prefix):].strip()
    title = title.replace("\n", " ").replace("\r", " ")
    title = " ".join(title.split())
    while title.endswith(TITLE_TRAILING_PUNCTUATION):
        title = title[:-1].strip()
    return title[:TITLE_MAX_CHARS]
