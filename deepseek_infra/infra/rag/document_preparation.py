"""Deterministic, side-effect-free RAG document preparation.

The module accepts text that Python has already extracted from an uploaded
file.  It normalizes and chunks that text, but deliberately has no access to
uploads, paths, OCR, embeddings, caches, databases, or indexes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any

RAG_DOCUMENT_MAX_CHARACTERS = 8_000_000
RAG_DOCUMENT_MAX_REQUEST_BYTES = 40_000_000
RAG_DOCUMENT_MAX_NESTING = 24
RAG_DOCUMENT_MAX_ID_CHARACTERS = 256
RAG_DOCUMENT_MAX_CHUNK_CHARACTERS = 1_000_000
DEFAULT_CHUNK_CHARACTERS = 6000
DEFAULT_CHUNK_OVERLAP = 400

_ALLOWED_TOP_LEVEL = frozenset({"documentId", "text", "metadata", "chunking"})
_ALLOWED_CHUNKING = frozenset({"chunkChars", "chunkOverlap"})
_ALLOWED_METADATA = frozenset({"displayName", "sourceType", "kind"})
_SENSITIVE_KEY_PARTS = (
    "absolutepath",
    "temporarypath",
    "uploadpath",
    "cachepath",
    "authorization",
    "apikey",
    "token",
    "rawfilebytes",
    "databaselocation",
    "workspacesecret",
    "filesystempath",
)

logger = logging.getLogger("deepseek_infra.rag.document_preparation")


@dataclass(frozen=True, slots=True)
class RagDocumentPreparationDecision:
    preparation: dict[str, Any]
    normalized_text: str
    diagnostics: dict[str, Any]


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message}


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
    maximum = 1
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        maximum = max(maximum, depth)
        if depth > RAG_DOCUMENT_MAX_NESTING:
            return depth
        children = current.values() if isinstance(current, dict) else current
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    return maximum


def _normalized_key(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def _sensitive_key(value: Any) -> bool:
    normalized = _normalized_key(value)
    return normalized.endswith("path") or any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def normalize_document_text(value: str) -> str:
    """Match ``files.normalize_extracted_text`` exactly."""
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def chunk_content_hash(text: str) -> str:
    """Match the existing Local RAG BLAKE2b-96 chunk lineage hash."""
    return hashlib.blake2b(str(text or "").strip().encode("utf-8", errors="ignore"), digest_size=12).hexdigest()


def document_content_hash(chunk_hash_by_index: dict[int, str]) -> str:
    """Match the existing Local RAG content-addressed document version."""
    digest = hashlib.blake2b(digest_size=12)
    for index in sorted(chunk_hash_by_index):
        digest.update(f"{index}:{chunk_hash_by_index[index]}\0".encode("utf-8", errors="ignore"))
    return digest.hexdigest()


def _normalize_metadata(value: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return None, _error("invalid_metadata", "metadata must be an object")
    for key in value:
        if _sensitive_key(key):
            return None, _error("invalid_metadata", "metadata contains a path or credential field")
    normalized: dict[str, Any] = {}
    for key in sorted(_ALLOWED_METADATA):
        if key not in value:
            continue
        item = value[key]
        if item is None or isinstance(item, (str, bool, int)):
            normalized[key] = item
        elif isinstance(item, float) and math.isfinite(item):
            normalized[key] = item
        else:
            return None, _error("invalid_metadata", f"metadata.{key} must be a JSON scalar")
    return normalized, None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _chunk_text(
    text: str,
    *,
    document_id: str,
    chunk_chars: int,
    chunk_overlap: int,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        end = min(start + chunk_chars, text_length)
        if end < text_length:
            boundary = max(text.rfind("\n\n", start, end), text.rfind("\n", start, end))
            if boundary > start + chunk_chars // 2:
                end = boundary
        chunk_body = text[start:end].strip()
        if chunk_body:
            content_hash = chunk_content_hash(chunk_body)
            index = len(chunks)
            chunks.append(
                {
                    "index": index,
                    "chunkId": f"{document_id}:{index}:{content_hash}",
                    "text": chunk_body,
                    "start": start,
                    "end": end,
                    "lineStart": text.count("\n", 0, start) + 1,
                    "lineEnd": text.count("\n", 0, end) + 1,
                    "contentHash": content_hash,
                }
            )
        if end >= text_length:
            break
        start = max(end - chunk_overlap, start + 1)
    return chunks


def prepare_rag_document(
    value: Any,
    *,
    payload_size: int | None = None,
) -> dict[str, Any]:
    """Return a normalized document descriptor or a stable error object."""
    try:
        encoded = _json_bytes(value)
    except (TypeError, ValueError):
        return _error("invalid_request", "request must be safely JSON serializable")
    size = len(encoded) if payload_size is None else max(0, int(payload_size))
    if size > RAG_DOCUMENT_MAX_REQUEST_BYTES:
        return _error("request_too_large", "document preparation request is too large")
    if _max_depth(value) > RAG_DOCUMENT_MAX_NESTING:
        return _error("nesting_limit_exceeded", "document preparation request is too deeply nested")
    if not isinstance(value, dict):
        return _error("invalid_request", "request must be a JSON object")
    if any(_sensitive_key(key) for key in value):
        return _error("invalid_request", "request contains a path or credential field")
    if set(value) - _ALLOWED_TOP_LEVEL:
        return _error("invalid_request", "request contains unsupported fields")

    document_id = value.get("documentId")
    if (
        not isinstance(document_id, str)
        or not document_id
        or document_id != document_id.strip()
        or len(document_id) > RAG_DOCUMENT_MAX_ID_CHARACTERS
        or any(ord(character) < 32 for character in document_id)
    ):
        return _error("invalid_document_id", "documentId must be a normalized non-empty string")

    raw_text = value.get("text")
    if not isinstance(raw_text, str):
        return _error("invalid_text", "text must be a string")
    normalized_text = normalize_document_text(raw_text)
    if not normalized_text:
        return _error("invalid_text", "text must contain readable content")
    if len(normalized_text) > RAG_DOCUMENT_MAX_CHARACTERS:
        return _error("document_too_large", "normalized document exceeds the character limit")

    metadata, metadata_error = _normalize_metadata(value.get("metadata", {}))
    if metadata_error is not None:
        return metadata_error
    assert metadata is not None

    chunking = value.get("chunking")
    if not isinstance(chunking, dict):
        return _error("invalid_request", "chunking must be an object")
    if any(_sensitive_key(key) for key in chunking) or set(chunking) - _ALLOWED_CHUNKING:
        return _error("invalid_request", "chunking contains unsupported fields")
    chunk_chars = _integer(chunking.get("chunkChars"))
    if chunk_chars is None or chunk_chars <= 0 or chunk_chars > RAG_DOCUMENT_MAX_CHUNK_CHARACTERS:
        return _error("invalid_chunk_size", "chunkChars must be a positive bounded integer")
    chunk_overlap = _integer(chunking.get("chunkOverlap"))
    if chunk_overlap is None or chunk_overlap < 0:
        return _error("invalid_chunk_overlap", "chunkOverlap must be a non-negative integer")
    if chunk_overlap >= chunk_chars:
        return _error("chunk_overlap_too_large", "chunkOverlap must be smaller than chunkChars")

    chunks = _chunk_text(
        normalized_text,
        document_id=document_id,
        chunk_chars=chunk_chars,
        chunk_overlap=chunk_overlap,
    )
    hash_by_index = {int(chunk["index"]): str(chunk["contentHash"]) for chunk in chunks}
    return {
        "ok": True,
        "document": {
            "documentId": document_id,
            "contentHash": document_content_hash(hash_by_index),
            "characterCount": len(normalized_text),
            "chunkCount": len(chunks),
            "metadata": metadata,
        },
        "chunks": chunks,
        "chunking": {"chunkChars": chunk_chars, "chunkOverlap": chunk_overlap},
        "diagnostics": {"normalized": normalized_text != raw_text},
    }


def prepare_rag_document_json(raw: str | bytes) -> dict[str, Any]:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(encoded) > RAG_DOCUMENT_MAX_REQUEST_BYTES:
        return _error("request_too_large", "document preparation request is too large")
    try:
        value = json.loads(encoded.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _error("invalid_request", "request must contain valid JSON")
    return prepare_rag_document(value, payload_size=len(encoded))


def _diagnostics(value: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    document_id = value.get("documentId") if isinstance(value.get("documentId"), str) else ""
    chunking_value = value.get("chunking")
    chunking: dict[str, Any] = chunking_value if isinstance(chunking_value, dict) else {}
    document_value = local.get("document")
    document: dict[str, Any] = document_value if isinstance(document_value, dict) else {}
    return {
        "documentIdHash": hashlib.sha256(document_id.encode("utf-8", errors="ignore")).hexdigest()[:16] if document_id else "",
        "characterCount": int(document.get("characterCount") or 0),
        "chunkCount": int(document.get("chunkCount") or 0),
        "chunkSize": int(chunking.get("chunkChars") or 0),
        "chunkOverlap": int(chunking.get("chunkOverlap") or 0),
        "runtime": "python",
        "fallback": False,
        "fallbackReason": "",
        "latencyMs": 0,
    }


def _contains_sensitive_key(value: Any) -> bool:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if any(_sensitive_key(key) for key in current):
                return True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _validate_rust_success(local: dict[str, Any], candidate: Any, normalized_text: str) -> tuple[bool, str]:
    if not isinstance(candidate, dict):
        return False, "rust_response_not_object"
    try:
        _json_bytes(candidate)
    except (TypeError, ValueError):
        return False, "rust_response_not_serializable"
    if candidate.get("ok") is not True:
        return False, "rust_contract_invalid"
    if _contains_sensitive_key(candidate):
        return False, "rust_sensitive_field_injected"
    document = candidate.get("document")
    chunks = candidate.get("chunks")
    if not isinstance(document, dict) or not isinstance(chunks, list):
        return False, "rust_contract_invalid"
    local_document_value = local.get("document")
    local_document: dict[str, Any] = local_document_value if isinstance(local_document_value, dict) else {}
    if document.get("documentId") != local_document.get("documentId"):
        return False, "rust_document_id_changed"
    seen: set[str] = set()
    for expected_index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            return False, "rust_chunk_invalid"
        if chunk.get("index") != expected_index:
            return False, "rust_chunk_index_invalid"
        chunk_id = chunk.get("chunkId")
        if not isinstance(chunk_id, str) or not chunk_id or chunk_id in seen:
            return False, "rust_chunk_id_invalid"
        seen.add(chunk_id)
        start = chunk.get("start")
        end = chunk.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 0 <= start <= end <= len(normalized_text)
        ):
            return False, "rust_chunk_offset_invalid"
        if chunk.get("text") != normalized_text[start:end].strip():
            return False, "rust_chunk_text_mismatch"
        content_hash = chunk.get("contentHash")
        if not isinstance(content_hash, str) or len(content_hash) != 24 or any(c not in "0123456789abcdef" for c in content_hash):
            return False, "rust_content_hash_invalid"
        if content_hash != chunk_content_hash(str(chunk.get("text") or "")):
            return False, "rust_content_hash_mismatch"
    if candidate != local:
        return False, "rust_semantic_divergence"
    return True, ""


def rag_document_preparation_enabled() -> bool:
    value = os.environ.get("DEEPSEEK_RUST_RAG_DOCUMENT_PREP", "0")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def prepare_rag_document_with_optional_rust(value: dict[str, Any]) -> RagDocumentPreparationDecision:
    local = prepare_rag_document(value)
    text_value = value.get("text")
    normalized_text = normalize_document_text(text_value) if isinstance(text_value, str) else ""
    diagnostics = _diagnostics(value, local)
    if local.get("ok") is not True or not rag_document_preparation_enabled():
        return RagDocumentPreparationDecision(local, normalized_text, diagnostics)

    from deepseek_infra.infra.rust_core import rag_client

    safe_payload = {
        "documentId": value.get("documentId"),
        "text": value.get("text"),
        "metadata": local.get("document", {}).get("metadata", {}),
        "chunking": local.get("chunking", {}),
    }
    started = time.perf_counter()
    result = rag_client.prepare_document(safe_payload)
    diagnostics["latencyMs"] = max(result.latency_ms, int((time.perf_counter() - started) * 1000))
    if not result.ok:
        diagnostics.update(runtime="python", fallback=True, fallbackReason=result.error_kind or "rust_backend_unavailable")
        return RagDocumentPreparationDecision(local, normalized_text, diagnostics)
    valid, reason = _validate_rust_success(local, result.body, normalized_text)
    if not valid:
        diagnostics.update(runtime="python", fallback=True, fallbackReason=reason)
        return RagDocumentPreparationDecision(local, normalized_text, diagnostics)
    diagnostics.update(runtime="rust", fallback=False, fallbackReason="")
    return RagDocumentPreparationDecision(result.body, normalized_text, diagnostics)


def log_rag_document_preparation(diagnostics: dict[str, Any]) -> None:
    logger.info(
        "rag_document_preparation",
        extra={
            "document_id_hash": str(diagnostics.get("documentIdHash") or ""),
            "character_count": int(diagnostics.get("characterCount") or 0),
            "chunk_count": int(diagnostics.get("chunkCount") or 0),
            "chunk_size": int(diagnostics.get("chunkSize") or 0),
            "chunk_overlap": int(diagnostics.get("chunkOverlap") or 0),
            "runtime": str(diagnostics.get("runtime") or "python"),
            "fallback": bool(diagnostics.get("fallback")),
            "fallback_reason": str(diagnostics.get("fallbackReason") or ""),
            "latency_ms": int(diagnostics.get("latencyMs") or 0),
        },
    )


def public_rag_document_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
    return {
        "runtime": str(diagnostics.get("runtime") or "python"),
        "fallback": bool(diagnostics.get("fallback")),
        "fallbackReason": str(diagnostics.get("fallbackReason") or ""),
        "characterCount": int(diagnostics.get("characterCount") or 0),
        "chunkCount": int(diagnostics.get("chunkCount") or 0),
        "latencyMs": int(diagnostics.get("latencyMs") or 0),
    }
