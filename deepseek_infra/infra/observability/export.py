"""Trace export redaction and serialization helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.infra.observability import observability as trace_store

EXPORT_SCHEMA_VERSION = "trace-export.v1"
MAX_EXPORT_STRING_CHARS = 2_000
MAX_PRIVATE_CONTENT_CHARS = 1_200

TOKEN_COUNT_KEYS = {
    "totaltokens",
    "prompttokens",
    "completiontokens",
    "promptcachehittokens",
    "promptcachemisstokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
}
SENSITIVE_EXACT_KEYS = {
    "apikey",
    "api_key",
    "authorization",
    "auth",
    "authtoken",
    "auth_token",
    "bearertoken",
    "token",
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "cookie",
    "setcookie",
    "set_cookie",
    "password",
    "secret",
    "credential",
    "credentials",
    "privatekey",
    "private_key",
    "deepseekapikey",
    "deepseek_api_key",
    "tavilyapikey",
    "tavily_api_key",
}
SENSITIVE_KEY_FRAGMENTS = ("authorization", "cookie", "password", "secret", "credential", "privatekey")
PRIVATE_CONTENT_KEYS = {
    "content",
    "text",
    "prompt",
    "messages",
    "input",
    "output",
    "filecontent",
    "file_content",
    "filetext",
    "file_text",
    "rawcontent",
    "raw_content",
    "pagecontent",
    "page_content",
    "document",
}

BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{8,}")
SECRET_TOKEN_RE = re.compile(r"\b(sk-[a-zA-Z0-9][a-zA-Z0-9_-]{8,})\b")
QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|api_key|apikey|auth_token|access_token|refresh_token)=)([^&\s\"'<>]+)"
)
HEADER_SECRET_RE = re.compile(
    r"(?i)\b(api[-_ ]?key|authorization|auth[-_ ]?token|access[-_ ]?token|refresh[-_ ]?token)\s*[:=]\s*([^,\s\"'<>]{8,})"
)


def export_trace(trace_id: str) -> dict[str, Any] | None:
    """Return a redacted export payload for one trace."""
    trace = trace_store.get_trace(trace_id)
    if trace is None:
        return None
    redacted = redact_trace(trace)
    redacted["_export"] = {
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "exportedAt": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "redaction": {
            "applied": True,
            "stringLimit": MAX_EXPORT_STRING_CHARS,
            "privateContentLimit": MAX_PRIVATE_CONTENT_CHARS,
        },
    }
    return redacted


def redact_trace_for_response(trace: dict[str, Any]) -> dict[str, Any]:
    """Redact trace detail data before it leaves the HTTP API."""
    redacted = redact_value(trace, key="")
    return redacted if isinstance(redacted, dict) else {}


def redact_trace(trace: dict[str, Any]) -> dict[str, Any]:
    redacted = redact_value(trace, key="")
    return redacted if isinstance(redacted, dict) else {}


def redact_value(value: Any, *, key: str) -> Any:
    if is_sensitive_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): redact_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_value(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, key=key) for item in value]
    if isinstance(value, str):
        return clip_text(redact_sensitive_text(value), limit=limit_for_key(key))
    if value is None or isinstance(value, bool | int | float):
        return value
    return clip_text(redact_sensitive_text(str(value)), limit=limit_for_key(key))


def is_sensitive_key(key: str) -> bool:
    normalized = normalize_key(key)
    if not normalized or normalized in TOKEN_COUNT_KEYS:
        return False
    if normalized in SENSITIVE_EXACT_KEYS:
        return True
    if normalized.endswith(("apikey", "secret", "password", "privatekey")):
        return True
    if "token" in normalized and not normalized.endswith("tokens"):
        return True
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(key or "").lower())


def redact_sensitive_text(value: str) -> str:
    text = BEARER_RE.sub(r"\1[redacted]", value)
    text = SECRET_TOKEN_RE.sub("[redacted-secret]", text)
    text = QUERY_SECRET_RE.sub(r"\1[redacted]", text)
    return HEADER_SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)


def limit_for_key(key: str) -> int:
    normalized = normalize_key(key)
    if normalized in PRIVATE_CONTENT_KEYS or normalized.endswith(("content", "text", "prompt", "document")):
        return MAX_PRIVATE_CONTENT_CHARS
    return MAX_EXPORT_STRING_CHARS


def clip_text(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"...[truncated {len(value) - limit} chars]"
