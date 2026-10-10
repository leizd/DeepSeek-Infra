"""Shared helpers for model names, token scoring, filenames, timestamps, and local IP detection."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from deepseek_infra.core.config import MODEL_ALIASES

def format_upstream_error(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500] or "DeepSeek API error"

    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("type")
        if message:
            return str(message)
    return raw[:500] or "DeepSeek API error"

_CONTENT_RISK_SIGNATURES = (
    "content exists risk",
    "content_filter",
    "contentfilter",
    "content policy",
    "content management",
)

def is_content_risk_error(message: str | None) -> bool:
    """判断上游错误是否为 DeepSeek 内容安全拦截（而非网络、限流、鉴权等）。"""
    raw = str(message or "")
    lowered = raw.lower()
    if not lowered.strip():
        return False
    if any(signature in lowered for signature in _CONTENT_RISK_SIGNATURES):
        return True
    has_content = "content" in lowered or "内容" in raw
    has_risk = "risk" in lowered or "风险" in raw or "敏感" in raw
    return has_content and has_risk

def humanize_upstream_error(message: str | None) -> str:
    """把上游错误转成清晰、可操作的中文说明；内容安全拦截以外的错误原样返回。"""
    raw = str(message or "").strip()
    if not raw:
        return "DeepSeek API error"
    if is_content_risk_error(raw):
        signal = raw if len(raw) <= 100 else raw[:100] + "…"
        return (
            "内容安全提示：DeepSeek 判定本轮内容存在风险（原始返回："
            f"{signal}），已中止生成。这类拦截常见于时政、新闻等敏感话题，"
            "尤其当联网搜索带回相关内容时。可以换个问法、把问题缩小到具体主题，"
            "或关闭联网搜索后重试。"
        )
    return raw

def normalize_model_name(value: Any) -> str:
    raw = str(value or "").strip()
    key = raw.lower().replace("_", "-").replace(" ", "")
    return MODEL_ALIASES.get(key, raw)

def latest_user_query(payload: dict[str, Any]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def query_tokens(query: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", query.lower())
    tokens = set(re.findall(r"[a-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", normalized))
    for cjk_run in re.findall(r"[\u4e00-\u9fff]{3,}", normalized):
        for index in range(0, len(cjk_run) - 1):
            tokens.add(cjk_run[index : index + 2])
    return sorted(tokens, key=len, reverse=True)[:80]

def score_chunk(text: str, tokens: list[str]) -> int:
    if not tokens:
        return 0
    lowered = text.lower()
    score = 0
    for token in tokens:
        count = lowered.count(token)
        if count:
            score += count * max(2, min(len(token), 10))
    if re.search(r"^#{1,6}\s+", text, flags=re.MULTILINE):
        score += 2
    return score

def multipart_filename(disposition: str) -> str:
    star_match = re.search(r"filename\*=([^']*)''([^;]+)", disposition, flags=re.IGNORECASE)
    if star_match:
        return clean_filename(unquote(star_match.group(2)))

    match = re.search(r'filename="([^"]*)"', disposition, flags=re.IGNORECASE)
    if match:
        return clean_filename(match.group(1))

    match = re.search(r"filename=([^;]+)", disposition, flags=re.IGNORECASE)
    if match:
        return clean_filename(match.group(1).strip())

    return ""

def clean_filename(value: str) -> str:
    name = re.split(r"[\\/]", value.strip().strip('"'))[-1]
    return name[:180] or "uploaded-file"

def url_with_token(url: str, token: str) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("token", token))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

LOCAL_IP_CACHE_TTL_SECONDS = 30.0
_local_ip_cache: tuple[str, float] | None = None


def local_ip() -> str:
    global _local_ip_cache
    now = time.monotonic()
    if _local_ip_cache is not None:
        cached_ip, cached_at = _local_ip_cache
        if now - cached_at < LOCAL_IP_CACHE_TTL_SECONDS:
            return cached_ip

    detected = detect_local_ip()
    _local_ip_cache = (detected, now)
    return detected


def clear_local_ip_cache() -> None:
    global _local_ip_cache
    _local_ip_cache = None


local_ip.cache_clear = clear_local_ip_cache  # type: ignore[attr-defined]


def detect_local_ip() -> str:
    ipconfig_ip = local_ip_from_ipconfig()
    if ipconfig_ip:
        return ipconfig_ip

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            candidate = sock.getsockname()[0]
            if is_lan_ip(candidate):
                return candidate
    except OSError:
        pass
    return "127.0.0.1"

def local_ip_from_ipconfig() -> str | None:
    if os.name != "nt":
        return None

    try:
        output = subprocess.check_output(
            ["ipconfig"],
            text=True,
            encoding="gbk",
            errors="ignore",
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None

    candidates = re.findall(r"IPv4 [^:\r\n]*:\s*([0-9.]+)", output)
    for candidate in candidates:
        if is_rfc1918_ip(candidate):
            return candidate

    for candidate in candidates:
        if is_lan_ip(candidate):
            return candidate
    return None

def hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}

    kwargs: dict[str, Any] = {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags

    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        if hasattr(subprocess, "SW_HIDE"):
            startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE")
        kwargs["startupinfo"] = startupinfo

    return kwargs

def is_rfc1918_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False

    return (
        address.version == 4
        and (
            value.startswith("10.")
            or value.startswith("192.168.")
            or any(value.startswith(f"172.{second}.") for second in range(16, 32))
        )
        and is_lan_ip(value)
    )

def is_lan_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False

    last_octet = int(value.rsplit(".", 1)[-1])
    return (
        address.version == 4
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_unspecified
        and last_octet not in {0, 255}
    )


