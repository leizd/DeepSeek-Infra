"""Local semantic response cache backed by SQLite embeddings."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

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

# Answers matching these markers are non-answers (fallbacks / refusals) and must not be cached.
LOW_QUALITY_MARKERS = (
    "综合阶段没有返回正文",
    "重新综合最终回答",
    "本轮搜索次数已达上限",
)

logger = logging.getLogger("deepseek_infra.semantic_cache")

CACHE_TABLE = "semantic_cache_items"

_db_lock = threading.RLock()
_last_error = ""


@dataclass(frozen=True, slots=True)
class CacheLookup:
    diagnostics: dict[str, Any]
    result: dict[str, Any] | None = None

    @property
    def hit(self) -> bool:
        return self.result is not None


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
    diagnostics["cacheVersion"] = version
    diagnostics["scope"] = scope
    diagnostics["exactMatchOnly"] = exact_only
    try:
        query_embedding = embed_text(prompt_text)
        rows = candidate_rows(str(body.get("model") or ""), version, scope)
    except Exception as exc:
        set_last_error(f"semantic cache lookup failed: {exc}")
        diagnostics["skippedReason"] = "lookup_error"
        diagnostics["lastError"] = _last_error
        return CacheLookup(diagnostics)

    now = int(time.time())
    best_row: sqlite3.Row | None = None
    best_similarity = 0.0
    for row in rows:
        if cache_expired(row, now):
            continue
        exact = row["prompt_hash"] == prompt_hash
        if exact_only and not exact:
            continue
        similarity = 1.0 if exact else cosine_similarity(query_embedding, decode_embedding(row["embedding"]))
        if similarity > best_similarity:
            best_similarity = similarity
            best_row = row

    diagnostics["similarity"] = round(best_similarity, 4)
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
        cache_id = existing_cache_id(prompt_hash, str(body.get("model") or ""), version, scope) or uuid.uuid4().hex
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            conn.execute(
                f"""
                INSERT INTO {CACHE_TABLE}
                    (
                        cache_id, prompt_hash, model, prompt_text, embedding, response_json, usage_json,
                        created_at, updated_at, last_hit_at, hit_count,
                        cache_version, scope, quality_score, query_text
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
                ON CONFLICT(cache_id) DO UPDATE SET
                    prompt_text = excluded.prompt_text,
                    embedding = excluded.embedding,
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
                    encode_embedding(embedding),
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
        diagnostics.update({"stored": True, "cacheId": cache_id, "promptHash": prompt_hash, "scope": scope, "cacheVersion": version})
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
    """Add v2.0.7 columns to caches created by older versions (idempotent)."""
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({CACHE_TABLE})").fetchall()}
    for name, definition in _MIGRATION_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE {CACHE_TABLE} ADD COLUMN {name} {definition}")


def stable_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", errors="ignore"), digest_size=16).hexdigest()


def encode_embedding(vector: list[float]) -> str:
    return json.dumps([round(float(item), 6) for item in vector], separators=(",", ":"))


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


def decode_json(value: Any) -> Any:
    try:
        return json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}


def set_last_error(message: str) -> None:
    global _last_error
    _last_error = message
    logger.warning("semantic_cache_error", extra={"detail": message})
