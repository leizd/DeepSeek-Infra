"""Shared schema normalization and redaction helpers for Workspace Core."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import mutation_gate

PROJECT_ID_RE = re.compile(r"[a-zA-Z0-9_-]{4,64}")
WORKSPACE_ID_RE = re.compile(r"[a-zA-Z0-9_-]{4,80}")
SAVED_ITEM_TYPES = {
    "chat_snippet",
    "assistant_answer",
    "file_quote",
    "rag_citation",
    "artifact",
    "webpage",
    "media",
    "trace",
    "eval_result",
}
SAVED_ITEM_PURPOSES = {"reference", "memory_candidate", "export_fragment"}
ARTIFACT_TYPES = {"pptx", "docx", "pdf", "svg", "markdown", "md", "csv", "json", "html", "txt"}
EXPORT_FORMATS = {"markdown", "md", "html", "json", "zip"}
MAX_TITLE_CHARS = 160
MAX_DESCRIPTION_CHARS = 2_000
MAX_CONTENT_CHARS = 200_000
MAX_SOURCE_REF_VALUE_CHARS = 2_000
MAX_TAGS = 24
MAX_TAG_CHARS = 40

SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[-_ ]?key|authorization|auth[-_ ]?token|access[-_ ]?token|refresh[-_ ]?token|password|secret)\s*[:=]\s*([^,\s\"'<>]{4,})"
)
BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{8,}")
SECRET_TOKEN_RE = re.compile(r"\b(sk-[a-zA-Z0-9][a-zA-Z0-9_-]{8,})\b")
QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:token|api_key|apikey|auth_token|access_token|refresh_token)=)([^&\s\"'<>]+)")
SENSITIVE_KEY_RE = re.compile(r"(?i)(api[_-]?key|authorization|auth[_-]?token|access[_-]?token|refresh[_-]?token|password|secret|cookie)$")


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp_ms_to_iso(value: Any) -> str:
    try:
        timestamp = int(value or 0) / 1000
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    safe_prefix = re.sub(r"[^a-z0-9_]", "", str(prefix or "").lower()).strip("_") or "item"
    return f"{safe_prefix}_{secrets.token_hex(8)}"


def validate_project_id(project_id: str) -> str:
    value = str(project_id or "").strip()
    if not PROJECT_ID_RE.fullmatch(value):
        raise AppError("Invalid project id", code=ErrorCode.INVALID_PAYLOAD, status=400)
    return value


def validate_workspace_id(value: str, *, label: str = "id") -> str:
    safe = str(value or "").strip()
    if not WORKSPACE_ID_RE.fullmatch(safe):
        raise AppError(f"Invalid {label}", code=ErrorCode.INVALID_PAYLOAD, status=400)
    return safe


def normalize_title(value: Any, default: str = "Untitled") -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    return title[:MAX_TITLE_CHARS] or default


def normalize_description(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").strip()[:MAX_DESCRIPTION_CHARS]


def normalize_content(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").strip()[:MAX_CONTENT_CHARS]


def normalize_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = re.sub(r"\s+", " ", str(item or "")).strip()[:MAX_TAG_CHARS]
        key = tag.lower()
        if not tag or key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def normalize_source_ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        safe_key = re.sub(r"[^a-zA-Z0-9_.:-]", "", str(key or ""))[:80]
        if not safe_key:
            continue
        if isinstance(item, dict):
            nested = normalize_source_ref(item)
            if nested:
                result[safe_key] = nested
            continue
        if isinstance(item, list):
            cleaned = [str(child)[:MAX_SOURCE_REF_VALUE_CHARS] for child in item[:20] if child is not None]
            if cleaned:
                result[safe_key] = cleaned
            continue
        if item is None or isinstance(item, bool | int | float):
            result[safe_key] = item
        else:
            result[safe_key] = str(item)[:MAX_SOURCE_REF_VALUE_CHARS]
    return result


def normalize_saved_type(value: Any) -> str:
    item_type = str(value or "").strip().lower()
    if item_type not in SAVED_ITEM_TYPES:
        raise AppError("Unsupported saved item type", code=ErrorCode.INVALID_PAYLOAD, status=400)
    return item_type


def normalize_saved_purpose(value: Any) -> str:
    purpose = str(value or "").strip().lower()
    return purpose if purpose in SAVED_ITEM_PURPOSES else "reference"


def normalize_artifact_type(value: Any, *, path: str = "") -> str:
    artifact_type = str(value or "").strip().lower().lstrip(".")
    if artifact_type == "md":
        artifact_type = "markdown"
    if not artifact_type and path:
        suffix = Path(path).suffix.lower().lstrip(".")
        artifact_type = "markdown" if suffix == "md" else suffix
    if artifact_type not in ARTIFACT_TYPES:
        raise AppError("Unsupported artifact type", code=ErrorCode.INVALID_PAYLOAD, status=400)
    return artifact_type


def normalize_export_format(value: Any) -> str:
    export_format = str(value or "zip").strip().lower().lstrip(".")
    if export_format == "md":
        export_format = "markdown"
    if export_format not in EXPORT_FORMATS:
        raise AppError("Unsupported export format", code=ErrorCode.INVALID_PAYLOAD, status=400)
    return export_format


def read_json_file(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default or {})
    return data if isinstance(data, dict) else dict(default or {})


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    with mutation_gate.mutation_scope(root=mutation_gate.workspace_root_for_path(path)):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


def safe_filename(value: str, default: str = "item") -> str:
    name = re.sub(r"[^\w.-]+", "-", str(value or ""), flags=re.UNICODE).strip(".-")
    return name[:80] or default


def runtime_relative_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise AppError("Artifact path is required", code=ErrorCode.INVALID_PAYLOAD, status=400)
    candidate = Path(raw)
    if candidate.is_absolute():
        for base in (config.GENERATED_DIR, config.PROJECTS_DIR, config.ROOT):
            try:
                return candidate.resolve().relative_to(base.resolve()).as_posix() if base == config.ROOT else f"{base.name}/{candidate.resolve().relative_to(base.resolve()).as_posix()}"
            except ValueError:
                continue
        raise AppError("Artifact path must stay inside the workspace runtime root", code=ErrorCode.INVALID_PAYLOAD, status=400)
    normalized = raw.replace("\\", "/")
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise AppError("Artifact path must not escape the workspace", code=ErrorCode.INVALID_PAYLOAD, status=400)
    return PurePosixPath(*parts).as_posix()


def resolve_runtime_path(value: str) -> Path:
    rel = runtime_relative_path(value)
    if rel == config.GENERATED_DIR.name or rel.startswith(config.GENERATED_DIR.name + "/"):
        return (config.GENERATED_DIR / rel.removeprefix(config.GENERATED_DIR.name).lstrip("/")).resolve()
    if rel == config.PROJECTS_DIR.name or rel.startswith(config.PROJECTS_DIR.name + "/"):
        return (config.PROJECTS_DIR / rel.removeprefix(config.PROJECTS_DIR.name).lstrip("/")).resolve()
    return (config.ROOT / rel).resolve()


def redact_sensitive_text(value: str) -> str:
    text = BEARER_RE.sub(r"\1[redacted]", str(value or ""))
    text = SECRET_TOKEN_RE.sub("[redacted-secret]", text)
    text = QUERY_SECRET_RE.sub(r"\1[redacted]", text)
    return SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)


def redact_value(value: Any, *, key: str = "") -> Any:
    if key and SENSITIVE_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): redact_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_value(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, key=key) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return redact_sensitive_text(str(value))


def contains_secret(value: str | bytes) -> bool:
    text = value.decode("utf-8", errors="ignore") if isinstance(value, bytes) else str(value or "")
    if SECRET_TOKEN_RE.search(text) or BEARER_RE.search(text):
        return True
    return SECRET_VALUE_RE.search(text) is not None or QUERY_SECRET_RE.search(text) is not None
