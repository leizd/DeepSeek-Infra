"""Local RAG index, embeddings, and SQLite vector retrieval."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import math
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepseek_infra.core.config import (
    FILE_CACHE_DIR,
    LOCAL_RAG_BACKEND,
    LOCAL_RAG_BM25_B,
    LOCAL_RAG_BM25_K1,
    LOCAL_RAG_DB,
    LOCAL_RAG_DIR,
    LOCAL_RAG_EMBEDDING_DIMENSIONS,
    LOCAL_RAG_EMBEDDING_MAX_TOKENS,
    LOCAL_RAG_EMBEDDING_PROVIDER,
    LOCAL_RAG_ENABLED,
    LOCAL_RAG_INCREMENTAL,
    LOCAL_RAG_ONNX_MODEL_PATH,
    LOCAL_RAG_SEARCH_LIMIT,
    LOCAL_RAG_TOKENIZER_PATH,
    MEMORY_FILE,
    PROJECTS_DIR,
)
from deepseek_infra.core.utils import query_tokens

logger = logging.getLogger("deepseek_infra.local_rag")

COLLECTION_FILES = "files"
COLLECTION_MEMORY = "memory"
COLLECTION_MEDIA = "media"
VECTOR_TABLE = "rag_vec"
ITEM_TABLE = "rag_items"
META_TABLE = "rag_meta"
MAX_RAG_TEXT_CHARS = 12_000

_db_lock = threading.RLock()
_embedding_lock = threading.RLock()
_embedding_pipeline: "EmbeddingPipeline | None" = None
_last_error = ""


@dataclass(frozen=True, slots=True)
class RAGSearchResult:
    item_id: str
    collection: str
    source_id: str
    project_id: str
    chunk_index: int
    name: str
    kind: str
    scope: str
    text: str
    score: int
    vector_score: float
    keyword_score: int
    metadata: dict[str, Any]


class EmbeddingPipeline:
    def __init__(self) -> None:
        self.requested_provider = LOCAL_RAG_EMBEDDING_PROVIDER
        self.active_provider = "hash"
        self.dimensions = LOCAL_RAG_EMBEDDING_DIMENSIONS
        self.model_path = LOCAL_RAG_ONNX_MODEL_PATH
        self.tokenizer_path = LOCAL_RAG_TOKENIZER_PATH
        self.error = ""
        self._session: Any | None = None
        self._tokenizer: Any | None = None
        if self.requested_provider == "onnx":
            self._load_onnx()

    def embed(self, text: str) -> list[float]:
        if self.active_provider == "onnx" and self._session is not None and self._tokenizer is not None:
            try:
                return normalize_vector(self._embed_onnx(text), self.dimensions)
            except Exception as exc:  # pragma: no cover - optional runtime path
                self.error = f"onnx embedding failed: {exc}"
                logger.warning("local_rag_onnx_embedding_failed", extra={"detail": self.error})
        return hash_text_embedding(text, dimensions=self.dimensions)

    def _load_onnx(self) -> None:
        if not self.model_path or not Path(self.model_path).exists():
            self.error = "LOCAL_RAG_ONNX_MODEL_PATH is not configured or does not exist"
            return
        if not self.tokenizer_path or not Path(self.tokenizer_path).exists():
            self.error = "LOCAL_RAG_TOKENIZER_PATH is not configured or does not exist"
            return
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ModuleNotFoundError as exc:
            self.error = f"optional embedding dependency is missing: {exc.name}"
            return
        self._np = np
        self._session = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])
        self._tokenizer = Tokenizer.from_file(self.tokenizer_path)
        self.active_provider = "onnx"

    def _embed_onnx(self, text: str) -> list[float]:  # pragma: no cover - requires optional model files
        np = self._np
        session = self._session
        tokenizer = self._tokenizer
        if session is None or tokenizer is None:
            return [0.0] * self.dimensions
        encoded = tokenizer.encode(str(text or "")[:MAX_RAG_TEXT_CHARS])
        input_ids = list(encoded.ids[:LOCAL_RAG_EMBEDDING_MAX_TOKENS])
        if not input_ids:
            return [0.0] * self.dimensions
        attention_mask = list(getattr(encoded, "attention_mask", []) or [1] * len(input_ids))[: len(input_ids)]
        token_type_ids = list(getattr(encoded, "type_ids", []) or [0] * len(input_ids))[: len(input_ids)]
        feeds: dict[str, Any] = {}
        input_names = {item.name for item in session.get_inputs()}
        if "input_ids" in input_names:
            feeds["input_ids"] = np.asarray([input_ids], dtype=np.int64)
        if "attention_mask" in input_names:
            feeds["attention_mask"] = np.asarray([attention_mask], dtype=np.int64)
        if "token_type_ids" in input_names:
            feeds["token_type_ids"] = np.asarray([token_type_ids], dtype=np.int64)
        if not feeds:
            first_input = session.get_inputs()[0].name
            feeds[first_input] = np.asarray([input_ids], dtype=np.int64)
        outputs = session.run(None, feeds)
        output = outputs[0]
        if getattr(output, "ndim", 0) == 3:
            mask = np.asarray(attention_mask, dtype=np.float32).reshape(1, -1, 1)
            masked = output[:, : len(attention_mask), :] * mask
            vector = masked.sum(axis=1) / np.maximum(mask.sum(axis=1), 1.0)
            return vector[0].astype(float).tolist()
        if getattr(output, "ndim", 0) == 2:
            return output[0].astype(float).tolist()
        return output.astype(float).reshape(-1).tolist()


def embedding_pipeline() -> EmbeddingPipeline:
    global _embedding_pipeline
    with _embedding_lock:
        if _embedding_pipeline is None:
            _embedding_pipeline = EmbeddingPipeline()
        return _embedding_pipeline


def reset_embedding_pipeline() -> None:
    global _embedding_pipeline
    with _embedding_lock:
        _embedding_pipeline = None


def embed_text(text: str) -> list[float]:
    return embedding_pipeline().embed(text)


def hash_text_embedding(text: str, *, dimensions: int = LOCAL_RAG_EMBEDDING_DIMENSIONS) -> list[float]:
    vector = [0.0] * max(1, int(dimensions or 1))
    value = str(text or "").lower()
    for feature in query_tokens(value):
        digest = hashlib.blake2b(feature.encode("utf-8", errors="ignore"), digest_size=4).digest()
        number = int.from_bytes(digest, "big")
        index = number % len(vector)
        sign = -1.0 if number & 1 else 1.0
        vector[index] += sign
    return normalize_vector(vector, len(vector))


def normalize_vector(vector: list[Any], dimensions: int = LOCAL_RAG_EMBEDDING_DIMENSIONS) -> list[float]:
    cleaned: list[float] = []
    for item in vector[: max(1, int(dimensions or 1))]:
        try:
            cleaned.append(float(item))
        except (TypeError, ValueError):
            cleaned.append(0.0)
    while len(cleaned) < max(1, int(dimensions or 1)):
        cleaned.append(0.0)
    norm = math.sqrt(sum(item * item for item in cleaned))
    if norm <= 0:
        return cleaned
    return [round(item / norm, 6) for item in cleaned]


def cosine_similarity(left: list[Any], right: list[Any]) -> float:
    if not left or not right:
        return 0.0
    total = 0.0
    for left_value, right_value in zip(left, right):
        try:
            total += float(left_value) * float(right_value)
        except (TypeError, ValueError):
            continue
    return max(0.0, min(1.0, total))


def vector_blob(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *[float(item) for item in vector])


def stable_vector_id(item_id: str) -> int:
    digest = hashlib.blake2b(item_id.encode("utf-8", errors="ignore"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


def chunk_hash(text: str) -> str:
    """Content hash of a chunk; drives lineage + incremental change detection."""
    return hashlib.blake2b(str(text or "").strip().encode("utf-8", errors="ignore"), digest_size=12).hexdigest()


def doc_version(chunk_hash_by_index: dict[int, str]) -> str:
    """Content-addressed document version: hash of its chunk-index → chunk-hash map."""
    digest = hashlib.blake2b(digest_size=12)
    for index in sorted(chunk_hash_by_index):
        digest.update(f"{index}:{chunk_hash_by_index[index]}\0".encode("utf-8", errors="ignore"))
    return digest.hexdigest()


def bm25_scores(
    query_terms: list[str],
    docs_terms: list[list[str]],
    *,
    k1: float = LOCAL_RAG_BM25_K1,
    b: float = LOCAL_RAG_BM25_B,
) -> list[float]:
    """Okapi BM25 lexical scores for each candidate doc over the candidate corpus."""
    total = len(docs_terms)
    if total == 0 or not query_terms:
        return [0.0] * total
    unique_query = set(query_terms)
    lengths = [len(terms) for terms in docs_terms]
    avgdl = (sum(lengths) / total) or 1.0
    doc_freq: dict[str, int] = {}
    for terms in docs_terms:
        for term in unique_query.intersection(terms):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    scores: list[float] = []
    for index, terms in enumerate(docs_terms):
        length = lengths[index] or 1
        term_freq: dict[str, int] = {}
        for term in terms:
            if term in unique_query:
                term_freq[term] = term_freq.get(term, 0) + 1
        score = 0.0
        for term, freq in term_freq.items():
            df = doc_freq.get(term, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            denom = freq + k1 * (1 - b + b * (length / avgdl))
            if denom:
                score += idf * (freq * (k1 + 1)) / denom
        scores.append(score)
    return scores


def sqlite_vec_available() -> bool:
    return importlib.util.find_spec("sqlite_vec") is not None


def connect_db() -> sqlite3.Connection:
    LOCAL_RAG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LOCAL_RAG_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    if LOCAL_RAG_BACKEND != "sqlite_vec":
        return False
    try:
        import sqlite_vec
        sqlite_vec.load(conn)
        return True
    except Exception as exc:
        set_last_error(f"sqlite-vec unavailable: {exc}")
        return False


def initialize_schema(conn: sqlite3.Connection, *, vec_loaded: bool) -> bool:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {META_TABLE} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ITEM_TABLE} (
            item_id TEXT PRIMARY KEY,
            vector_id INTEGER NOT NULL UNIQUE,
            collection TEXT NOT NULL,
            source_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            text TEXT NOT NULL,
            embedding TEXT NOT NULL,
            metadata TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ITEM_TABLE}_collection ON {ITEM_TABLE}(collection)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ITEM_TABLE}_source ON {ITEM_TABLE}(collection, source_id, project_id)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{ITEM_TABLE}_scope ON {ITEM_TABLE}(collection, scope)")

    configured_dim = str(embedding_pipeline().dimensions)
    existing_dim = conn.execute(f"SELECT value FROM {META_TABLE} WHERE key = 'embedding_dimensions'").fetchone()
    if existing_dim and str(existing_dim["value"]) != configured_dim:
        conn.execute(f"DELETE FROM {ITEM_TABLE}")
        conn.execute(f"DROP TABLE IF EXISTS {VECTOR_TABLE}")
    conn.execute(
        f"INSERT OR REPLACE INTO {META_TABLE}(key, value) VALUES ('embedding_dimensions', ?)",
        (configured_dim,),
    )
    conn.execute(
        f"INSERT OR REPLACE INTO {META_TABLE}(key, value) VALUES ('embedding_provider', ?)",
        (embedding_pipeline().active_provider,),
    )

    vector_table_ready = False
    if vec_loaded:
        try:
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {VECTOR_TABLE}
                USING vec0(
                    item_id TEXT,
                    collection TEXT,
                    source_id TEXT,
                    project_id TEXT,
                    scope TEXT,
                    embedding float[{embedding_pipeline().dimensions}]
                )
                """
            )
            vector_table_ready = True
        except sqlite3.Error as exc:
            set_last_error(f"sqlite-vec table unavailable: {exc}")
            vector_table_ready = False
    conn.commit()
    return vector_table_ready


def set_last_error(message: str) -> None:
    global _last_error
    _last_error = message
    if message:
        logger.warning("local_rag_warning", extra={"detail": message})


def db_ready() -> tuple[sqlite3.Connection, bool]:
    conn = connect_db()
    vec_loaded = load_sqlite_vec(conn)
    vector_table_ready = initialize_schema(conn, vec_loaded=vec_loaded)
    return conn, vector_table_ready


def upsert_items(items: list[dict[str, Any]]) -> int:
    if not LOCAL_RAG_ENABLED or not items:
        return 0
    with _db_lock:
        try:
            conn, vector_table_ready = db_ready()
            try:
                for item in items:
                    upsert_item(conn, item, vector_table_ready=vector_table_ready)
                conn.commit()
                return len(items)
            finally:
                conn.close()
        except Exception as exc:
            set_last_error(f"index write failed: {exc}")
            return 0


def upsert_item(conn: sqlite3.Connection, item: dict[str, Any], *, vector_table_ready: bool) -> None:
    item_id = str(item.get("item_id") or "")
    if not item_id:
        return
    text = str(item.get("text") or "")[:MAX_RAG_TEXT_CHARS]
    raw_embedding = item.get("embedding")
    embedding = normalize_vector(raw_embedding if isinstance(raw_embedding, list) else embed_text(text))
    vector_id = stable_vector_id(item_id)
    values = {
        "item_id": item_id,
        "vector_id": vector_id,
        "collection": str(item.get("collection") or ""),
        "source_id": str(item.get("source_id") or ""),
        "project_id": str(item.get("project_id") or ""),
        "chunk_index": int(item.get("chunk_index") or 0),
        "name": str(item.get("name") or ""),
        "kind": str(item.get("kind") or ""),
        "scope": str(item.get("scope") or ""),
        "text": text,
        "embedding": json.dumps(embedding, separators=(",", ":")),
        "metadata": json.dumps(item.get("metadata") if isinstance(item.get("metadata"), dict) else {}, ensure_ascii=False),
        "updated_at": int(item.get("updated_at") or int(time.time() * 1000)),
    }
    conn.execute(
        f"""
        INSERT INTO {ITEM_TABLE}
        (item_id, vector_id, collection, source_id, project_id, chunk_index, name, kind, scope, text, embedding, metadata, updated_at)
        VALUES
        (:item_id, :vector_id, :collection, :source_id, :project_id, :chunk_index, :name, :kind, :scope, :text, :embedding, :metadata, :updated_at)
        ON CONFLICT(item_id) DO UPDATE SET
            vector_id=excluded.vector_id,
            collection=excluded.collection,
            source_id=excluded.source_id,
            project_id=excluded.project_id,
            chunk_index=excluded.chunk_index,
            name=excluded.name,
            kind=excluded.kind,
            scope=excluded.scope,
            text=excluded.text,
            embedding=excluded.embedding,
            metadata=excluded.metadata,
            updated_at=excluded.updated_at
        """,
        values,
    )
    if vector_table_ready:
        try:
            conn.execute(f"DELETE FROM {VECTOR_TABLE} WHERE rowid = ?", (vector_id,))
            conn.execute(
                f"""
                INSERT INTO {VECTOR_TABLE}(rowid, item_id, collection, source_id, project_id, scope, embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vector_id,
                    values["item_id"],
                    values["collection"],
                    values["source_id"],
                    values["project_id"],
                    values["scope"],
                    vector_blob(embedding),
                ),
            )
        except sqlite3.Error as exc:
            set_last_error(f"sqlite-vec insert failed: {exc}")


def delete_items(*, collection: str, source_id: str = "", project_id: str = "", scope: str = "") -> int:
    if not LOCAL_RAG_ENABLED:
        return 0
    with _db_lock:
        try:
            conn, vector_table_ready = db_ready()
            try:
                clauses = ["collection = ?"]
                params: list[Any] = [collection]
                if source_id:
                    clauses.append("source_id = ?")
                    params.append(source_id)
                if project_id:
                    clauses.append("project_id = ?")
                    params.append(project_id)
                if scope:
                    clauses.append("scope = ?")
                    params.append(scope)
                rows = conn.execute(f"SELECT vector_id FROM {ITEM_TABLE} WHERE {' AND '.join(clauses)}", params).fetchall()
                conn.execute(f"DELETE FROM {ITEM_TABLE} WHERE {' AND '.join(clauses)}", params)
                if vector_table_ready:
                    for row in rows:
                        conn.execute(f"DELETE FROM {VECTOR_TABLE} WHERE rowid = ?", (int(row["vector_id"]),))
                conn.commit()
                return len(rows)
            finally:
                conn.close()
        except Exception as exc:
            set_last_error(f"delete failed: {exc}")
            return 0


def file_item_id(file_id: str, project_id: str, chunk_index: int) -> str:
    return f"file:{project_id or '_'}:{file_id}:{int(chunk_index)}"


def memory_item_id(memory_id: str) -> str:
    return f"memory:{memory_id}"


def media_item_id(media_id: str, segment_id: str, chunk_index: int) -> str:
    return f"media:{media_id}:{segment_id}:{int(chunk_index)}"


def existing_doc_chunks(collection: str, source_id: str, project_id: str) -> dict[int, dict[str, Any]]:
    """Return ``{chunk_index: {"hash", "embedding"}}`` already indexed for a document."""
    if not LOCAL_RAG_ENABLED or not source_id:
        return {}
    result: dict[int, dict[str, Any]] = {}
    with _db_lock:
        try:
            conn, _ = db_ready()
            try:
                rows = conn.execute(
                    f"SELECT chunk_index, embedding, metadata FROM {ITEM_TABLE} "
                    "WHERE collection = ? AND source_id = ? AND project_id = ?",
                    (collection, source_id, project_id),
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            set_last_error(f"existing chunk read failed: {exc}")
            return {}
    for row in rows:
        try:
            meta = json.loads(str(row["metadata"] or "{}"))
        except json.JSONDecodeError:
            meta = {}
        chunk_hash_value = str(meta.get("hash") or "") if isinstance(meta, dict) else ""
        result[int(row["chunk_index"] or 0)] = {"hash": chunk_hash_value, "embedding": parse_embedding(row["embedding"])}
    return result


def index_file_payload(cached: dict[str, Any], *, project_id: str = "") -> int:
    file_id = str(cached.get("id") or "").strip()
    if not file_id:
        return 0
    project = project_id or ""
    raw_chunks = cached.get("chunks")
    chunks = raw_chunks if isinstance(raw_chunks, list) else []

    prepared: list[dict[str, Any]] = []
    hash_by_index: dict[int, str] = {}
    for fallback_index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue
        raw_index = chunk.get("index")
        chunk_index = int(raw_index) if raw_index is not None else fallback_index
        content_hash = chunk_hash(text)
        hash_by_index[chunk_index] = content_hash
        prepared.append({"chunk_index": chunk_index, "text": text, "chunk": chunk, "hash": content_hash})

    if not prepared:
        delete_items(collection=COLLECTION_FILES, source_id=file_id, project_id=project)
        return 0

    new_version = doc_version(hash_by_index)
    existing = existing_doc_chunks(COLLECTION_FILES, file_id, project) if LOCAL_RAG_INCREMENTAL else {}
    if LOCAL_RAG_INCREMENTAL and existing:
        existing_version = doc_version({index: data["hash"] for index, data in existing.items() if data.get("hash")})
        if existing_version and existing_version == new_version:
            # 增量索引：文档内容哈希未变，跳过重嵌入与重写。
            return len(prepared)

    items: list[dict[str, Any]] = []
    for record in prepared:
        chunk = record["chunk"]
        chunk_index = record["chunk_index"]
        prior = existing.get(chunk_index)
        if prior and prior.get("hash") == record["hash"] and prior.get("embedding"):
            embedding = prior["embedding"]  # reuse stored vector for an unchanged chunk
        else:
            embedding = embed_text(record["text"])
        items.append(
            {
                "item_id": file_item_id(file_id, project, chunk_index),
                "collection": COLLECTION_FILES,
                "source_id": file_id,
                "project_id": project,
                "chunk_index": chunk_index,
                "name": str(cached.get("name") or file_id),
                "kind": str(cached.get("kind") or "text"),
                "scope": "",
                "text": record["text"],
                "embedding": embedding,
                "metadata": {
                    "lineStart": int(chunk.get("lineStart") or 0),
                    "lineEnd": int(chunk.get("lineEnd") or 0),
                    "start": int(chunk.get("start") or 0),
                    "end": int(chunk.get("end") or 0),
                    "page": int(chunk.get("page") or 0),
                    "hash": record["hash"],
                    "docVersion": new_version,
                },
            }
        )
    delete_items(collection=COLLECTION_FILES, source_id=file_id, project_id=project)
    return upsert_items(items)


def sync_memories(memories: list[dict[str, Any]]) -> int:
    delete_items(collection=COLLECTION_MEMORY)
    items: list[dict[str, Any]] = []
    for item in memories:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        memory_id = str(item.get("id") or "").strip()
        if not content or not memory_id:
            continue
        scope = str(item.get("scope") or "global")
        items.append(
            {
                "item_id": memory_item_id(memory_id),
                "collection": COLLECTION_MEMORY,
                "source_id": memory_id,
                "project_id": "",
                "chunk_index": 0,
                "name": str(item.get("category") or "memory"),
                "kind": "memory",
                "scope": scope,
                "text": content,
                "embedding": embed_text(content),
                "metadata": {
                    "id": memory_id,
                    "category": str(item.get("category") or "fact"),
                    "source": str(item.get("source") or ""),
                    "pinned": bool(item.get("pinned")),
                    "createdAt": str(item.get("createdAt") or ""),
                    "updatedAt": str(item.get("updatedAt") or ""),
                },
            }
        )
    return upsert_items(items)


def search(
    query: str,
    *,
    collection: str,
    limit: int = LOCAL_RAG_SEARCH_LIMIT,
    source_id: str = "",
    project_id: str | None = None,
    scopes: list[str] | None = None,
) -> list[RAGSearchResult]:
    if not LOCAL_RAG_ENABLED or not str(query or "").strip():
        return []
    with _db_lock:
        try:
            conn, vector_table_ready = db_ready()
            try:
                return _search_db(
                    conn,
                    query,
                    collection=collection,
                    limit=limit,
                    source_id=source_id,
                    project_id=project_id,
                    scopes=scopes,
                    vector_table_ready=vector_table_ready,
                )
            finally:
                conn.close()
        except Exception as exc:
            set_last_error(f"search failed: {exc}")
            return []


def _search_db(
    conn: sqlite3.Connection,
    query: str,
    *,
    collection: str,
    limit: int,
    source_id: str,
    project_id: str | None,
    scopes: list[str] | None,
    vector_table_ready: bool,
) -> list[RAGSearchResult]:
    query_vector = embed_text(query)
    vector_distances: dict[str, float] = {}
    if vector_table_ready:
        try:
            clauses = ["embedding MATCH ?", "k = ?", "collection = ?"]
            params: list[Any] = [vector_blob(query_vector), max(limit * 4, limit), collection]
            if source_id:
                clauses.append("source_id = ?")
                params.append(source_id)
            if project_id is not None:
                clauses.append("project_id = ?")
                params.append(project_id)
            if scopes:
                placeholders = ", ".join("?" for _ in scopes)
                clauses.append(f"scope IN ({placeholders})")
                params.extend(scopes)
            rows = conn.execute(
                f"SELECT item_id, distance FROM {VECTOR_TABLE} WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
            vector_distances = {str(row["item_id"]): float(row["distance"]) for row in rows}
        except sqlite3.Error as exc:
            set_last_error(f"sqlite-vec search failed: {exc}")

    rows = load_candidate_rows(conn, collection=collection, source_id=source_id, project_id=project_id, scopes=scopes, item_ids=vector_distances)
    tokens = query_tokens(query)
    # Hybrid retriever: blend dense vector similarity with sparse BM25 lexical scores
    # computed over the candidate corpus.
    docs_terms = [query_tokens(str(row["text"] or "")) for row in rows]
    lexical_scores = bm25_scores(tokens, docs_terms)
    results: list[RAGSearchResult] = []
    for index, row in enumerate(rows):
        embedding = parse_embedding(row["embedding"])
        cosine = cosine_similarity(query_vector, embedding)
        if row["item_id"] in vector_distances:
            distance = max(0.0, vector_distances[row["item_id"]])
            vector_score = max(cosine, 1.0 / (1.0 + distance))
        else:
            vector_score = cosine
        keyword_score = lexical_scores[index]
        score = int(round(vector_score * 100 + keyword_score * 10))
        if score <= 0:
            continue
        results.append(row_to_result(row, score=score, vector_score=vector_score, keyword_score=int(round(keyword_score))))
    results.sort(key=lambda item: (-item.score, item.name, item.chunk_index))
    return results[:limit]


def load_candidate_rows(
    conn: sqlite3.Connection,
    *,
    collection: str,
    source_id: str,
    project_id: str | None,
    scopes: list[str] | None,
    item_ids: dict[str, float],
) -> list[sqlite3.Row]:
    clauses = ["collection = ?"]
    params: list[Any] = [collection]
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if scopes:
        placeholders = ", ".join("?" for _ in scopes)
        clauses.append(f"scope IN ({placeholders})")
        params.extend(scopes)
    rows = conn.execute(f"SELECT * FROM {ITEM_TABLE} WHERE {' AND '.join(clauses)}", params).fetchall()
    if item_ids:
        rows_by_id = {str(row["item_id"]): row for row in rows}
        missing = [item_id for item_id in item_ids if item_id not in rows_by_id]
        if missing:
            placeholders = ", ".join("?" for _ in missing)
            rows.extend(conn.execute(f"SELECT * FROM {ITEM_TABLE} WHERE item_id IN ({placeholders})", missing).fetchall())
    return rows


def parse_embedding(value: Any) -> list[float]:
    try:
        data = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return normalize_vector(data if isinstance(data, list) else [])


def row_to_result(row: sqlite3.Row, *, score: int, vector_score: float, keyword_score: int) -> RAGSearchResult:
    try:
        metadata = json.loads(str(row["metadata"] or "{}"))
    except json.JSONDecodeError:
        metadata = {}
    return RAGSearchResult(
        item_id=str(row["item_id"]),
        collection=str(row["collection"]),
        source_id=str(row["source_id"]),
        project_id=str(row["project_id"]),
        chunk_index=int(row["chunk_index"] or 0),
        name=str(row["name"]),
        kind=str(row["kind"]),
        scope=str(row["scope"]),
        text=str(row["text"]),
        score=int(score),
        vector_score=float(vector_score),
        keyword_score=int(keyword_score),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def search_file_chunks(file_id: str, project_id: str, query: str, *, limit: int = 8) -> list[int]:
    results = search(query, collection=COLLECTION_FILES, limit=limit, source_id=file_id, project_id=project_id or "")
    return [max(0, result.chunk_index) for result in results]


def search_files_index(query: str, *, limit: int = 5) -> list[RAGSearchResult]:
    return search(query, collection=COLLECTION_FILES, limit=limit)


def search_media_index(query: str, *, project_id: str | None = None, media_id: str = "", limit: int = 8) -> list[RAGSearchResult]:
    return search(query, collection=COLLECTION_MEDIA, source_id=media_id, project_id=project_id, limit=limit)


def search_memories_index(query: str, *, scopes: list[str], limit: int = 12) -> list[RAGSearchResult]:
    return search(query, collection=COLLECTION_MEMORY, scopes=scopes, limit=limit)


def chunk_lineage(result: RAGSearchResult) -> dict[str, Any]:
    """Trace a retrieved chunk back to its document/page/offset/hash (lineage)."""
    meta = result.metadata if isinstance(result.metadata, dict) else {}
    return {
        "chunkId": result.item_id,
        "docId": result.source_id,
        "projectId": result.project_id,
        "page": int(meta.get("page") or 0),
        "startChar": int(meta.get("start") or 0),
        "endChar": int(meta.get("end") or 0),
        "hash": str(meta.get("hash") or ""),
        "docVersion": str(meta.get("docVersion") or ""),
    }


def get_chunk(item_id: str) -> sqlite3.Row | None:
    if not LOCAL_RAG_ENABLED or not item_id:
        return None
    with _db_lock:
        try:
            conn, _ = db_ready()
            try:
                return conn.execute(f"SELECT * FROM {ITEM_TABLE} WHERE item_id = ?", (item_id,)).fetchone()
            finally:
                conn.close()
        except Exception as exc:
            set_last_error(f"chunk read failed: {exc}")
            return None


def _normalize_grounding_text(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def verify_citation(item_id: str, snippet: str, *, min_coverage: float = 0.8) -> dict[str, Any]:
    """Verify a cited snippet is actually grounded in the indexed chunk (引用真实性校验).

    Exact (whitespace-normalized) substring match is fully grounded; otherwise we
    fall back to query-token coverage so lightly reworded quotes still validate.
    """
    row = get_chunk(item_id)
    if row is None:
        return {"grounded": False, "itemId": item_id, "coverage": 0.0, "reason": "chunk_not_found"}
    lineage = chunk_lineage(row_to_result(row, score=0, vector_score=0.0, keyword_score=0))
    snippet_norm = _normalize_grounding_text(snippet)
    if not snippet_norm:
        return {"grounded": False, "itemId": item_id, "coverage": 0.0, "reason": "empty_snippet", "lineage": lineage}
    chunk_norm = _normalize_grounding_text(str(row["text"] or ""))
    if snippet_norm in chunk_norm:
        return {"grounded": True, "itemId": item_id, "coverage": 1.0, "reason": "", "lineage": lineage}
    snippet_terms = query_tokens(snippet)
    chunk_terms = set(query_tokens(str(row["text"] or "")))
    coverage = (sum(1 for term in snippet_terms if term in chunk_terms) / len(snippet_terms)) if snippet_terms else 0.0
    grounded = coverage >= min_coverage
    return {
        "grounded": grounded,
        "itemId": item_id,
        "coverage": round(coverage, 3),
        "reason": "" if grounded else "snippet_not_grounded",
        "lineage": lineage,
    }


def evaluate_recall(cases: list[dict[str, Any]], *, k: int = 5, collection: str = COLLECTION_FILES) -> dict[str, Any]:
    """Compute Recall@K and MRR for labeled ``{query, relevant:[...]}`` cases.

    ``relevant`` ids match either a chunk ``item_id`` or a document ``source_id``.
    """
    safe_k = max(1, int(k))
    details: list[dict[str, Any]] = []
    hits = 0
    reciprocal = 0.0
    evaluated = 0
    for case in cases if isinstance(cases, list) else []:
        if not isinstance(case, dict):
            continue
        query = str(case.get("query") or "").strip()
        raw_relevant = case.get("relevant") or case.get("relevantSourceIds") or []
        relevant = {str(item) for item in raw_relevant if str(item)} if isinstance(raw_relevant, list) else set()
        if not query or not relevant:
            continue
        evaluated += 1
        results = search(query, collection=collection, limit=safe_k)
        rank_hit = 0
        for rank, result in enumerate(results[:safe_k], start=1):
            if result.source_id in relevant or result.item_id in relevant:
                rank_hit = rank
                break
        if rank_hit:
            hits += 1
            reciprocal += 1.0 / rank_hit
        details.append({"query": query, "hit": rank_hit > 0, "rank": rank_hit, "topSourceIds": [r.source_id for r in results[:safe_k]]})
    return {
        "k": safe_k,
        "cases": evaluated,
        "recallAtK": round(hits / evaluated, 4) if evaluated else 0.0,
        "mrr": round(reciprocal / evaluated, 4) if evaluated else 0.0,
        "details": details,
    }


def status() -> dict[str, Any]:
    pipeline = embedding_pipeline()
    payload: dict[str, Any] = {
        "enabled": LOCAL_RAG_ENABLED,
        "backend": LOCAL_RAG_BACKEND,
        "databasePath": str(LOCAL_RAG_DB),
        "sqliteVecAvailable": sqlite_vec_available(),
        "embeddingProvider": pipeline.active_provider,
        "embeddingProviderRequested": pipeline.requested_provider,
        "embeddingDimensions": pipeline.dimensions,
        "embeddingModelPathConfigured": bool(LOCAL_RAG_ONNX_MODEL_PATH),
        "tokenizerPathConfigured": bool(LOCAL_RAG_TOKENIZER_PATH),
        "hybridSearch": "bm25+vector",
        "bm25K1": LOCAL_RAG_BM25_K1,
        "bm25B": LOCAL_RAG_BM25_B,
        "incremental": LOCAL_RAG_INCREMENTAL,
        "lastError": _last_error or pipeline.error,
        "indexedItems": 0,
        "indexedFiles": 0,
        "indexedMedia": 0,
        "indexedMemories": 0,
        "vectorTableAvailable": False,
    }
    if not LOCAL_RAG_ENABLED or not LOCAL_RAG_DB.exists():
        return payload
    with _db_lock:
        try:
            conn, vector_table_ready = db_ready()
            try:
                payload["vectorTableAvailable"] = vector_table_ready
                payload["indexedItems"] = int(conn.execute(f"SELECT COUNT(*) FROM {ITEM_TABLE}").fetchone()[0])
                payload["indexedFiles"] = int(
                    conn.execute(f"SELECT COUNT(DISTINCT source_id) FROM {ITEM_TABLE} WHERE collection = ?", (COLLECTION_FILES,)).fetchone()[0]
                )
                payload["indexedMedia"] = int(
                    conn.execute(f"SELECT COUNT(DISTINCT source_id) FROM {ITEM_TABLE} WHERE collection = ?", (COLLECTION_MEDIA,)).fetchone()[0]
                )
                payload["indexedMemories"] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {ITEM_TABLE} WHERE collection = ?", (COLLECTION_MEMORY,)).fetchone()[0]
                )
            finally:
                conn.close()
        except Exception as exc:
            payload["lastError"] = f"status failed: {exc}"
    return payload


def rebuild_index() -> dict[str, Any]:
    delete_items(collection=COLLECTION_FILES)
    delete_items(collection=COLLECTION_MEDIA)
    delete_items(collection=COLLECTION_MEMORY)
    files_count = 0
    chunks_count = 0
    for path, project_id in iter_cached_file_paths():
        cached = read_json_dict(path)
        if not cached:
            continue
        files_count += 1
        chunks_count += index_file_payload(cached, project_id=project_id)
    memories = read_json_list(MEMORY_FILE)
    memories_count = sync_memories(memories)
    media_count, media_chunks = rebuild_media_index()
    return {
        "ok": True,
        "files": files_count,
        "chunks": chunks_count,
        "media": media_count,
        "mediaChunks": media_chunks,
        "memories": memories_count,
        "localRag": status(),
    }


def rebuild_media_index() -> tuple[int, int]:
    try:
        from deepseek_infra.infra.media import indexer as media_indexer
        from deepseek_infra.infra.media import library as media_library
    except Exception:
        return 0, 0
    media_count = 0
    chunk_count = 0
    for media in media_library.list_media():
        if media.get("status") != "ready":
            continue
        segments = media_library.list_segments(str(media.get("mediaId") or ""))
        if not segments:
            continue
        media_count += 1
        chunk_count += media_indexer.index_media_segments(media, segments)
    return media_count, chunk_count


def iter_cached_file_paths() -> list[tuple[Path, str]]:
    paths: list[tuple[Path, str]] = []
    if FILE_CACHE_DIR.exists():
        paths.extend((path, "") for path in FILE_CACHE_DIR.glob("*.json"))
    if PROJECTS_DIR.exists():
        for path in PROJECTS_DIR.glob("*/files/*.json"):
            paths.append((path, path.parent.parent.name))
    return paths


def read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
