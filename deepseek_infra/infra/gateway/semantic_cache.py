"""Local semantic response cache backed by SQLite embeddings."""

from __future__ import annotations

import array
import hashlib
import json
import logging
import math
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from deepseek_infra.core.config import (
    SEMANTIC_CACHE_ATTACHMENTS,
    SEMANTIC_CACHE_DB,
    SEMANTIC_CACHE_DIR,
    SEMANTIC_CACHE_ENABLED,
    SEMANTIC_CACHE_MAX_ITEMS,
    SEMANTIC_CACHE_MAX_PROMPT_CHARS,
    SEMANTIC_CACHE_MAX_RESPONSE_CHARS,
    SEMANTIC_CACHE_MIN_QUALITY,
    SEMANTIC_CACHE_THRESHOLD,
    SEMANTIC_CACHE_TTL_SECONDS,
    SEMANTIC_CACHE_VERSION,
)
from deepseek_infra.core.utils import latest_user_query
from deepseek_infra.infra.gateway.chat_payload import count_payload_attachments
from deepseek_infra.infra.rag.local_rag import cosine_similarity, embed_text, embedding_pipeline
from deepseek_infra.infra.rust_core import rag_client as _rust_rag

# Answers matching these markers are non-answers (fallbacks / refusals) and must not be cached.
LOW_QUALITY_MARKERS = (
    "综合阶段没有返回正文",
    "重新综合最终回答",
    "本轮搜索次数已达上限",
)

logger = logging.getLogger("deepseek_infra.semantic_cache")

CACHE_TABLE = "semantic_cache_items"
EMBEDDING_FORMAT_F64LE_V1 = "f64le-v1"
MAX_EMBEDDING_DIMENSIONS = 4_096

_db_lock = threading.RLock()
_last_error = ""


@dataclass(frozen=True, slots=True)
class CacheLookup:
    diagnostics: dict[str, Any]
    result: dict[str, Any] | None = None

    @property
    def hit(self) -> bool:
        return self.result is not None


class EmbeddingBlobError(ValueError):
    """Stable embedding-storage failure that never contains vector values."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class NormalizedEmbedding:
    values: tuple[float, ...]
    json_text: str
    blob: bytes
    dimensions: int
    format: str = EMBEDDING_FORMAT_F64LE_V1


@dataclass(frozen=True, slots=True)
class _CandidateEmbedding:
    row: Any
    values: Sequence[float]
    blob: bytes | memoryview | None
    used_blob: bool


def lookup(payload: dict[str, Any], body: dict[str, Any]) -> CacheLookup:
    diagnostics = base_diagnostics()
    reason = skip_reason(payload, body)
    if reason:
        diagnostics["skippedReason"] = reason
        return CacheLookup(diagnostics)

    prompt_text = prompt_text_for_body(body)
    if not prompt_text:
        diagnostics["skippedReason"] = "empty_prompt"
        return CacheLookup(diagnostics)
    if len(prompt_text) > SEMANTIC_CACHE_MAX_PROMPT_CHARS:
        diagnostics["skippedReason"] = "prompt_too_large"
        diagnostics["promptChars"] = len(prompt_text)
        return CacheLookup(diagnostics)

    diagnostics["checked"] = True
    diagnostics["promptChars"] = len(prompt_text)
    prompt_hash = stable_hash(prompt_text)
    version = cache_version()
    scope = scope_for(payload)
    # File/attachment context: exact-prompt match only. The expanded file text
    # dominates the embedding, so fuzzy similarity would falsely match different
    # questions about the same file — exact match keeps reuse correct.
    exact_only = has_attachments(payload)
    model = str(body.get("model") or "")
    now = int(time.time())
    diagnostics["cacheVersion"] = version
    diagnostics["scope"] = scope
    diagnostics["exactMatchOnly"] = exact_only
    _rust_rag.reset_delegate_diagnostics()
    try:
        exact_row = exact_candidate_row(prompt_hash, model, version, scope, now=now)
    except Exception as exc:
        set_last_error(f"semantic cache lookup failed: {exc}")
        diagnostics["skippedReason"] = "lookup_error"
        diagnostics["lastError"] = _last_error
        return CacheLookup(diagnostics)

    if exact_row is not None:
        best_row, best_similarity, ranking_backend = exact_row, 1.0, "exact"
    elif exact_only:
        best_row, best_similarity, ranking_backend = None, 0.0, "python"
    else:
        try:
            query_embedding = embed_text(prompt_text)
            rows = candidate_rows(model, version, scope)
        except Exception as exc:
            set_last_error(f"semantic cache lookup failed: {exc}")
            diagnostics["skippedReason"] = "lookup_error"
            diagnostics["lastError"] = _last_error
            return CacheLookup(diagnostics)
        best_row, best_similarity, ranking_backend = best_candidate(
            query_embedding,
            rows,
            now=now,
            prompt_hash=prompt_hash,
            exact_only=False,
            storage_diagnostics=diagnostics,
        )

    diagnostics["similarity"] = round(best_similarity, 4)
    diagnostics["rankingBackend"] = ranking_backend
    rust_delegate_timing = _rust_rag.last_delegate_diagnostics("rag_vector_rank")
    if rust_delegate_timing:
        diagnostics["rustVectorRanking"] = rust_delegate_timing
    if best_row is None or best_similarity < SEMANTIC_CACHE_THRESHOLD:
        return CacheLookup(diagnostics)

    response = decode_json(best_row["response_json"])
    if not isinstance(response, dict):
        diagnostics["skippedReason"] = "bad_cache_record"
        return CacheLookup(diagnostics)

    cache_id = str(best_row["cache_id"])
    touch_cache(cache_id)
    diagnostics.update(
        {
            "hit": True,
            "cacheId": cache_id,
            "similarity": round(best_similarity, 4),
            "promptHash": prompt_hash,
            "qualityScore": float(best_row["quality_score"] or 0.0),
            "hitCount": int(best_row["hit_count"] or 0) + 1,
            "savedUsage": decode_json(best_row["usage_json"]) if best_row["usage_json"] else {},
        }
    )
    return CacheLookup(diagnostics, cached_result(cache_id, response))


def store(payload: dict[str, Any], body: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"stored": False}
    reason = skip_reason(payload, body)
    if reason:
        diagnostics["storeSkippedReason"] = reason
        return diagnostics

    prompt_text = prompt_text_for_body(body)
    content = str(result.get("content") or "")
    if not prompt_text or not content:
        diagnostics["storeSkippedReason"] = "empty_prompt_or_response"
        return diagnostics
    if len(prompt_text) > SEMANTIC_CACHE_MAX_PROMPT_CHARS:
        diagnostics["storeSkippedReason"] = "prompt_too_large"
        return diagnostics
    if len(content) > SEMANTIC_CACHE_MAX_RESPONSE_CHARS:
        diagnostics["storeSkippedReason"] = "response_too_large"
        return diagnostics
    if result.get("search") or result.get("memorySuggestions"):
        diagnostics["storeSkippedReason"] = "side_effect_response"
        return diagnostics

    score = quality_score(content)
    diagnostics["qualityScore"] = score
    if SEMANTIC_CACHE_MIN_QUALITY > 0 and score < SEMANTIC_CACHE_MIN_QUALITY:
        diagnostics["storeSkippedReason"] = "low_quality"
        return diagnostics

    response = {
        "model": str(result.get("model") or body.get("model") or ""),
        "content": content,
        "reasoning": str(result.get("reasoning") or ""),
    }
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    prompt_hash = stable_hash(prompt_text)
    version = cache_version()
    scope = scope_for(payload)
    query_text = query_text_for(payload)
    now = int(time.time())
    try:
        embedding = embed_text(prompt_text)
        representations = encode_embedding_representations(
            embedding,
            expected_dimensions=embedding_pipeline().dimensions,
        )
        cache_id = existing_cache_id(prompt_hash, str(body.get("model") or ""), version, scope) or uuid.uuid4().hex
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            conn.execute(
                f"""
                INSERT INTO {CACHE_TABLE}
                    (
                        cache_id, prompt_hash, model, prompt_text,
                        embedding, embedding_blob, embedding_dimensions, embedding_format,
                        response_json, usage_json,
                        created_at, updated_at, last_hit_at, hit_count,
                        cache_version, scope, quality_score, query_text
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
                ON CONFLICT(cache_id) DO UPDATE SET
                    prompt_text = excluded.prompt_text,
                    embedding = excluded.embedding,
                    embedding_blob = excluded.embedding_blob,
                    embedding_dimensions = excluded.embedding_dimensions,
                    embedding_format = excluded.embedding_format,
                    response_json = excluded.response_json,
                    usage_json = excluded.usage_json,
                    updated_at = excluded.updated_at,
                    quality_score = excluded.quality_score,
                    query_text = excluded.query_text
                """,
                (
                    cache_id,
                    prompt_hash,
                    str(body.get("model") or ""),
                    prompt_text,
                    representations.json_text,
                    sqlite3.Binary(representations.blob),
                    representations.dimensions,
                    representations.format,
                    json.dumps(response, ensure_ascii=False),
                    json.dumps(usage, ensure_ascii=False),
                    now,
                    now,
                    version,
                    scope,
                    score,
                    query_text,
                ),
            )
            trim_cache(conn)
        diagnostics.update(
            {
                "stored": True,
                "cacheId": cache_id,
                "promptHash": prompt_hash,
                "scope": scope,
                "cacheVersion": version,
                "embeddingStorage": "blob",
                "embeddingDimensions": representations.dimensions,
                "embeddingFormat": representations.format,
            }
        )
    except Exception as exc:
        set_last_error(f"semantic cache store failed: {exc}")
        diagnostics["storeSkippedReason"] = "store_error"
        diagnostics["lastError"] = _last_error
    return diagnostics


def status() -> dict[str, Any]:
    item_count = 0
    hit_count = 0
    if SEMANTIC_CACHE_ENABLED:
        try:
            with _db_lock, connect_db() as conn:
                initialize_schema(conn)
                row = conn.execute(f"SELECT COUNT(*) AS c, COALESCE(SUM(hit_count), 0) AS h FROM {CACHE_TABLE}").fetchone()
                item_count = int(row["c"] or 0)
                hit_count = int(row["h"] or 0)
        except Exception as exc:
            set_last_error(f"semantic cache status failed: {exc}")
    pipeline = embedding_pipeline()
    return {
        "enabled": SEMANTIC_CACHE_ENABLED,
        "databasePath": str(SEMANTIC_CACHE_DB),
        "similarityThreshold": SEMANTIC_CACHE_THRESHOLD,
        "ttlSeconds": SEMANTIC_CACHE_TTL_SECONDS,
        "maxItems": SEMANTIC_CACHE_MAX_ITEMS,
        "items": item_count,
        "hits": hit_count,
        "embeddingProvider": pipeline.active_provider,
        "embeddingDimensions": pipeline.dimensions,
        "cacheVersion": cache_version(),
        "minQualityScore": SEMANTIC_CACHE_MIN_QUALITY,
        "cacheAttachments": SEMANTIC_CACHE_ATTACHMENTS,
        "lastError": _last_error or pipeline.error,
    }


def clear() -> dict[str, Any]:
    try:
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            conn.execute(f"DELETE FROM {CACHE_TABLE}")
    except Exception as exc:
        set_last_error(f"semantic cache clear failed: {exc}")
        return {"ok": False, "semanticCache": status()}
    return {"ok": True, "semanticCache": status()}


def base_diagnostics() -> dict[str, Any]:
    return {
        "enabled": SEMANTIC_CACHE_ENABLED,
        "checked": False,
        "hit": False,
        "threshold": SEMANTIC_CACHE_THRESHOLD,
        "similarity": 0.0,
        "skippedReason": "",
        "cacheId": "",
        "embeddingStorage": "json",
        "blobCandidates": 0,
        "legacyCandidates": 0,
        "invalidBlobCandidates": 0,
    }


def skip_reason(payload: dict[str, Any], body: dict[str, Any]) -> str:
    if not SEMANTIC_CACHE_ENABLED:
        return "disabled"
    if payload.get("semanticCacheEnabled") is False:
        return "request_disabled"
    # File/attachment context is cacheable (v2.0.7) but only via exact-prompt match
    # within its project scope (see lookup/store); when disabled, skip it entirely.
    if not SEMANTIC_CACHE_ATTACHMENTS and count_payload_attachments(payload.get("messages")):
        return "attachments"
    if payload.get("searchEnabled") is True:
        return "search_enabled"
    if body.get("tools"):
        return "tools_enabled"
    tool_choice = body.get("tool_choice")
    if tool_choice and tool_choice != "none":
        return "tool_choice_enabled"
    if body.get("stream_options"):
        return ""
    return ""


def prompt_text_for_body(body: dict[str, Any]) -> str:
    prompt = {
        "model": body.get("model"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "reasoning_effort": body.get("reasoning_effort"),
        "thinking": body.get("thinking"),
        "messages": body.get("messages") or [],
    }
    return json.dumps(prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def cache_version() -> str:
    """Namespace stamp: logic version + embedding provider + dimensions.

    Changing the embedding model/dimensions or bumping ``SEMANTIC_CACHE_VERSION``
    yields a new namespace, so incompatible old entries are simply never matched
    (and age out via TTL / trim) rather than served with the wrong embedding space.
    """
    pipeline = embedding_pipeline()
    return f"{SEMANTIC_CACHE_VERSION}:{pipeline.active_provider}:{pipeline.dimensions}"


def scope_for(payload: dict[str, Any]) -> str:
    """Privacy / project isolation namespace for an entry (memory scope or project)."""
    raw = str(payload.get("memoryScope") or "").strip()
    if raw:
        return raw[:120]
    project_id = str(payload.get("projectId") or payload.get("activeProjectId") or "").strip()
    if project_id:
        return f"project:{project_id}"[:120]
    return "global"


def has_attachments(payload: dict[str, Any]) -> bool:
    return count_payload_attachments(payload.get("messages")) > 0


def query_text_for(payload: dict[str, Any]) -> str:
    return latest_user_query(payload)[:500]


def quality_score(content: str) -> float:
    """Cheap 0..1 answer-quality heuristic; refusals/fallbacks/near-empty score low."""
    text = str(content or "").strip()
    if not text:
        return 0.0
    if any(marker in text for marker in LOW_QUALITY_MARKERS):
        return 0.1
    if len(text) < 4:
        return 0.1
    return round(0.4 + min(1.0, len(text) / 400.0) * 0.6, 3)


def cached_result(cache_id: str, response: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"semantic-cache-{cache_id[:12]}",
        "model": str(response.get("model") or ""),
        "content": str(response.get("content") or ""),
        "reasoning": str(response.get("reasoning") or ""),
        "usage": {},
    }


def candidate_rows(model: str, version: str, scope: str) -> list[sqlite3.Row]:
    with _db_lock, connect_db() as conn:
        initialize_schema(conn)
        rows = conn.execute(
            f"""
            SELECT * FROM {CACHE_TABLE}
            WHERE model = ? AND cache_version = ? AND scope = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (model, version, scope, SEMANTIC_CACHE_MAX_ITEMS),
        ).fetchall()
    return list(rows)


def exact_candidate_row(
    prompt_hash: str,
    model: str,
    version: str,
    scope: str,
    *,
    now: int,
) -> sqlite3.Row | None:
    """Load an exact hit without selecting either embedding representation."""
    ttl_clause = ""
    params: list[Any] = [prompt_hash, model, version, scope]
    if SEMANTIC_CACHE_TTL_SECONDS > 0:
        ttl_clause = "AND (updated_at <= 0 OR updated_at >= ?)"
        params.append(now - SEMANTIC_CACHE_TTL_SECONDS)
    with _db_lock, connect_db() as conn:
        initialize_schema(conn)
        row = conn.execute(
            f"""
            SELECT
                cache_id, prompt_hash, model, prompt_text, response_json, usage_json,
                created_at, updated_at, last_hit_at, hit_count,
                cache_version, scope, quality_score, query_text
            FROM {CACHE_TABLE}
            WHERE prompt_hash = ? AND model = ? AND cache_version = ? AND scope = ?
            {ttl_clause}
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
    return row


def existing_cache_id(prompt_hash: str, model: str, version: str, scope: str) -> str:
    with _db_lock, connect_db() as conn:
        initialize_schema(conn)
        row = conn.execute(
            f"SELECT cache_id FROM {CACHE_TABLE} WHERE prompt_hash = ? AND model = ? AND cache_version = ? AND scope = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (prompt_hash, model, version, scope),
        ).fetchone()
    return str(row["cache_id"]) if row else ""


def touch_cache(cache_id: str) -> None:
    try:
        now = int(time.time())
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            conn.execute(
                f"UPDATE {CACHE_TABLE} SET hit_count = hit_count + 1, last_hit_at = ?, updated_at = ? WHERE cache_id = ?",
                (now, now, cache_id),
            )
    except Exception as exc:
        set_last_error(f"semantic cache touch failed: {exc}")


def trim_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        DELETE FROM {CACHE_TABLE}
        WHERE cache_id IN (
            SELECT cache_id FROM {CACHE_TABLE}
            ORDER BY updated_at DESC
            LIMIT -1 OFFSET ?
        )
        """,
        (SEMANTIC_CACHE_MAX_ITEMS,),
    )


def cache_expired(row: sqlite3.Row, now: int) -> bool:
    updated_at = int(row["updated_at"] or 0)
    return SEMANTIC_CACHE_TTL_SECONDS > 0 and updated_at > 0 and now - updated_at > SEMANTIC_CACHE_TTL_SECONDS


def connect_db() -> sqlite3.Connection:
    SEMANTIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SEMANTIC_CACHE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_MIGRATION_COLUMNS = (
    ("cache_version", "TEXT NOT NULL DEFAULT ''"),
    ("scope", "TEXT NOT NULL DEFAULT 'global'"),
    ("quality_score", "REAL NOT NULL DEFAULT 0"),
    ("query_text", "TEXT NOT NULL DEFAULT ''"),
    ("embedding_blob", "BLOB"),
    ("embedding_dimensions", "INTEGER NOT NULL DEFAULT 0"),
    ("embedding_format", "TEXT NOT NULL DEFAULT ''"),
)


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CACHE_TABLE} (
            cache_id TEXT PRIMARY KEY,
            prompt_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_text TEXT NOT NULL,
            embedding TEXT NOT NULL,
            embedding_blob BLOB,
            embedding_dimensions INTEGER NOT NULL DEFAULT 0,
            embedding_format TEXT NOT NULL DEFAULT '',
            response_json TEXT NOT NULL,
            usage_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_hit_at INTEGER NOT NULL,
            hit_count INTEGER NOT NULL,
            cache_version TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'global',
            quality_score REAL NOT NULL DEFAULT 0,
            query_text TEXT NOT NULL DEFAULT ''
        )
        """
    )
    _ensure_columns(conn)
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{CACHE_TABLE}_model ON {CACHE_TABLE}(model, updated_at)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{CACHE_TABLE}_ns ON {CACHE_TABLE}(model, cache_version, scope, updated_at)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{CACHE_TABLE}_hash ON {CACHE_TABLE}(prompt_hash, model)")


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add missing metadata columns only; never scan or rewrite cache rows."""
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({CACHE_TABLE})").fetchall()}
    for name, definition in _MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE {CACHE_TABLE} ADD COLUMN {name} {definition}")


def stable_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", errors="ignore"), digest_size=16).hexdigest()


def _embedding_byte_count(dimensions: int) -> int:
    if not isinstance(dimensions, int) or isinstance(dimensions, bool):
        raise EmbeddingBlobError("invalid_embedding_dimensions")
    if not 0 < dimensions <= MAX_EMBEDDING_DIMENSIONS or dimensions > sys.maxsize // 8:
        raise EmbeddingBlobError("invalid_embedding_dimensions")
    return dimensions * 8


def _f64le_bytes(values: Sequence[float], *, expected_dimensions: int | None = None) -> bytes:
    dimensions = len(values)
    byte_count = _embedding_byte_count(dimensions)
    if expected_dimensions is not None and dimensions != expected_dimensions:
        raise EmbeddingBlobError("embedding_dimension_mismatch")
    try:
        encoded = array.array("d", values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingBlobError("invalid_embedding_values") from exc
    if encoded.itemsize != 8:
        raise EmbeddingBlobError("invalid_f64_width")
    if not all(math.isfinite(value) for value in encoded):
        raise EmbeddingBlobError("non_finite_embedding")
    if sys.byteorder == "big":
        encoded.byteswap()
    elif sys.byteorder != "little":
        raise EmbeddingBlobError("invalid_host_byteorder")
    blob = encoded.tobytes()
    if len(blob) != byte_count:
        raise EmbeddingBlobError("embedding_blob_length_mismatch")
    return blob


def encode_embedding_representations(
    vector: Sequence[float],
    *,
    expected_dimensions: int | None = None,
) -> NormalizedEmbedding:
    try:
        values = tuple(round(float(item), 6) for item in vector)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingBlobError("invalid_embedding_values") from exc
    _embedding_byte_count(len(values))
    if expected_dimensions is not None and len(values) != expected_dimensions:
        raise EmbeddingBlobError("embedding_dimension_mismatch")
    if not all(math.isfinite(value) for value in values):
        raise EmbeddingBlobError("non_finite_embedding")
    return NormalizedEmbedding(
        values=values,
        json_text=json.dumps(values, separators=(",", ":"), allow_nan=False),
        blob=_f64le_bytes(values, expected_dimensions=expected_dimensions),
        dimensions=len(values),
    )


def encode_embedding(vector: list[float]) -> str:
    return encode_embedding_representations(vector).json_text


def decode_embedding(value: Any) -> list[float]:
    try:
        data = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    result: list[float] = []
    for item in data:
        try:
            result.append(float(item))
        except (TypeError, ValueError):
            result.append(0.0)
    return result


def decode_embedding_blob(
    value: Any,
    dimensions: int,
    *,
    expected_dimensions: int | None = None,
) -> array.array[float]:
    byte_count = _embedding_byte_count(dimensions)
    if expected_dimensions is not None and dimensions != expected_dimensions:
        raise EmbeddingBlobError("embedding_dimension_mismatch")
    try:
        view = memoryview(value)
        if not view.contiguous:
            raise EmbeddingBlobError("invalid_embedding_blob_buffer")
        byte_view = view.cast("B")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, EmbeddingBlobError):
            raise
        raise EmbeddingBlobError("invalid_embedding_blob_buffer") from exc
    if byte_view.nbytes != byte_count:
        raise EmbeddingBlobError("embedding_blob_length_mismatch")
    decoded = array.array("d")
    try:
        decoded.frombytes(byte_view.tobytes())
    except (BufferError, MemoryError, TypeError, ValueError) as exc:
        raise EmbeddingBlobError("invalid_embedding_blob_buffer") from exc
    if decoded.itemsize != 8 or len(decoded) != dimensions:
        raise EmbeddingBlobError("embedding_blob_length_mismatch")
    if sys.byteorder == "big":
        decoded.byteswap()
    elif sys.byteorder != "little":
        raise EmbeddingBlobError("invalid_host_byteorder")
    if not all(math.isfinite(value) for value in decoded):
        raise EmbeddingBlobError("non_finite_embedding")
    return decoded


def validate_embedding_blob(
    value: Any,
    dimensions: int,
    *,
    expected_dimensions: int | None = None,
) -> bool:
    try:
        decode_embedding_blob(value, dimensions, expected_dimensions=expected_dimensions)
    except EmbeddingBlobError:
        return False
    return True


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        if key in row.keys():
            return row[key]
    except (AttributeError, KeyError, TypeError):
        return default
    return default


def _json_candidate(value: Any, *, expected_dimensions: int) -> list[float] | None:
    decoded = decode_embedding(value)
    if len(decoded) != expected_dimensions or not all(math.isfinite(item) for item in decoded):
        return None
    return decoded


def _decode_candidate(
    row: Any,
    *,
    expected_dimensions: int,
    prefer_blob: bool,
) -> tuple[_CandidateEmbedding | None, bool]:
    embedding_format = str(_row_value(row, "embedding_format", "") or "")
    embedding_blob = _row_value(row, "embedding_blob")
    embedding_dimensions = _row_value(row, "embedding_dimensions", 0)
    has_blob_metadata = bool(embedding_format or embedding_blob is not None or embedding_dimensions)
    invalid_blob = False
    if prefer_blob and embedding_format == EMBEDDING_FORMAT_F64LE_V1:
        try:
            values = decode_embedding_blob(
                embedding_blob,
                embedding_dimensions,
                expected_dimensions=expected_dimensions,
            )
        except EmbeddingBlobError:
            invalid_blob = True
        else:
            raw_blob = embedding_blob if isinstance(embedding_blob, (bytes, memoryview)) else memoryview(embedding_blob)
            return _CandidateEmbedding(row=row, values=values, blob=raw_blob, used_blob=True), False
    elif prefer_blob and has_blob_metadata:
        invalid_blob = True

    json_values = _json_candidate(_row_value(row, "embedding"), expected_dimensions=expected_dimensions)
    if json_values is None:
        return None, invalid_blob
    return _CandidateEmbedding(row=row, values=json_values, blob=None, used_blob=False), invalid_blob


def best_candidate(
    query_embedding: Sequence[float],
    rows: Sequence[Any],
    *,
    now: int,
    prompt_hash: str,
    exact_only: bool,
    storage_diagnostics: dict[str, Any] | None = None,
) -> tuple[Any | None, float, str]:
    """Rank eligible cache rows with Rust when enabled, preserving Python parity."""
    eligible: list[Any] = []
    for row in rows:
        if cache_expired(row, now):
            continue
        if _row_value(row, "prompt_hash") == prompt_hash:
            return row, 1.0, "exact"
        if not exact_only:
            eligible.append(row)

    if exact_only or not eligible:
        return None, 0.0, "python"

    dimensions = len(query_embedding)
    prefer_blob = _rust_rag.rust_rag_vector_transport() == "binary"
    candidates: list[_CandidateEmbedding] = []
    invalid_blob_candidates = 0
    for row in eligible:
        candidate, invalid_blob = _decode_candidate(
            row,
            expected_dimensions=dimensions,
            prefer_blob=prefer_blob,
        )
        if invalid_blob:
            invalid_blob_candidates += 1
        if candidate is not None:
            candidates.append(candidate)

    blob_candidates = sum(candidate.used_blob for candidate in candidates)
    legacy_candidates = len(candidates) - blob_candidates
    embedding_storage = "mixed" if blob_candidates and legacy_candidates else "blob" if blob_candidates else "json"
    if storage_diagnostics is not None:
        storage_diagnostics.update(
            embeddingStorage=embedding_storage,
            blobCandidates=blob_candidates,
            legacyCandidates=legacy_candidates,
            invalidBlobCandidates=invalid_blob_candidates,
        )
    if not candidates:
        return None, 0.0, "python"

    if prefer_blob and blob_candidates:
        candidate_blobs = [
            candidate.blob
            if candidate.blob is not None
            else _f64le_bytes(candidate.values, expected_dimensions=dimensions)
            for candidate in candidates
        ]
        ranked, used_rust = _rust_rag.rank_vectors_from_blobs(
            query_embedding,
            candidate_blobs,
            dimensions=dimensions,
            blobs_validated=True,
        )
    else:
        ranked, used_rust = _rust_rag.rank_vectors(
            list(query_embedding),
            [list(candidate.values) for candidate in candidates],
        )
    validation_started_ns = time.perf_counter_ns()
    best_row: Any | None = None
    best_similarity = 0.0
    best_index: int | None = None
    for index, candidate in enumerate(candidates):
        similarity = cosine_similarity(query_embedding, candidate.values)
        if similarity > best_similarity:
            best_similarity = similarity
            best_row = candidate.row
            best_index = index
    validation_us = max(0, (time.perf_counter_ns() - validation_started_ns) // 1000)
    if used_rust and ranked is not None:
        rust_index, rust_similarity = ranked
        parity = rust_index == best_index and math.isclose(rust_similarity, best_similarity, rel_tol=1e-9, abs_tol=1e-12)
        current = _rust_rag.last_delegate_diagnostics("rag_vector_rank")
        _rust_rag.update_delegate_diagnostics(
            "rag_vector_rank",
            pythonValidationUs=int(current.get("pythonValidationUs") or 0) + validation_us,
            totalDelegateUs=int(current.get("totalDelegateUs") or 0) + validation_us,
            runtime="rust" if parity else "python",
            fallback=not parity,
            fallbackReason="" if parity else "rust_semantic_divergence",
        )
        if parity:
            return best_row, rust_similarity, "rust"
    return best_row, best_similarity, "python"


def decode_json(value: Any) -> Any:
    try:
        return json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}


def set_last_error(message: str) -> None:
    global _last_error
    _last_error = message
    logger.warning("semantic_cache_error", extra={"detail": message})
