"""Memory-index read-path parity probe, Python side.

`memory_vector_bonus_probe.py` measured that the vector bonus `retrieve_memories`
takes from `local_rag.search_memories_index` is **not bounded** — 7 of 8 queries
change order *and set* when the index is live. This probe is the acceptance test
for the Rust provider that replaces it
(`rust/crates/deepseek-policy/src/memory_index.rs`).

It runs the **real** modules against a scratch root and prints canonical JSON, so
the Rust side can be diffed byte for byte. Three layers are pinned separately, so a
failure says which one moved:

- **pure**: `hash_text_embedding`, `normalize_search_query`, `bm25_scores`,
  `parse_embedding` — the scoring primitives;
- **store**: `search_memories_index` over the `rag.sqlite3` that `save_memories`
  wrote through `sync_memories` — the ordered `(id, score, vector_score,
  keyword_score)` list, plus the `id -> max(score)` map `retrieve_memories` builds;
- **turn**: `retrieve_memories` with the index live and with it forced to raise —
  the oracle's own degradation path, which is what `vector_hits = None` reproduces.

The Rust side reads the **same** database file, so the fixture is shared rather than
duplicated: this side must run first.

Usage::

    python tasks/native-runtime/memory_index_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example memory_index_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

DEFAULT_ROOT = Path(tempfile.gettempdir()) / "deepseek-memory-index-parity"

CORPUS: list[dict[str, Any]] = [
    {"id": "m-style", "content": "我喜欢简洁直接的回答风格", "category": "preference",
     "scope": "global", "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-02T00:00:00+00:00"},
    {"id": "m-stack", "content": "前端用 React，后端用 Rust 编写服务", "category": "project",
     "scope": "global", "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-03T00:00:00+00:00"},
    {"id": "m-birthday", "content": "我的生日是 3 月 5 日", "category": "fact",
     "scope": "global", "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-01T00:00:00+00:00"},
    {"id": "m-pinned", "content": "重要会议每周一上午十点", "category": "todo", "pinned": True,
     "scope": "global", "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-01T00:00:00+00:00"},
    {"id": "m-tie-a", "content": "React 状态管理库", "category": "fact",
     "scope": "global", "createdAt": "2026-02-01T00:00:00+00:00", "updatedAt": "2026-02-01T00:00:00+00:00"},
    {"id": "m-tie-b", "content": "React 服务端渲染方案", "category": "fact",
     "scope": "global", "createdAt": "2026-02-01T00:00:00+00:00", "updatedAt": "2026-02-01T00:00:00+00:00"},
    {"id": "m-long", "content": "用户长期从事基础设施开发，熟悉 kubernetes、terraform 与 CI 流水线", "category": "fact",
     "scope": "global", "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-04T00:00:00+00:00"},
    {"id": "m-english", "content": "The user prefers concise answers in English meetings", "category": "preference",
     "scope": "global", "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-01T00:00:00+00:00"},
    {"id": "m-proj", "content": "项目使用 pnpm 工作区管理依赖", "category": "project",
     "scope": "project:abc", "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-05T00:00:00+00:00"},
    {"id": "m-proj2", "content": "React 组件库文档", "category": "project",
     "scope": "project:abc", "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-06T00:00:00+00:00"},
]

QUERIES: list[tuple[str, list[str]]] = [
    ("react", ["global"]),
    ("React 组件", ["global"]),
    ("你记得什么", ["global"]),
    ("我的生日", ["global"]),
    ("后端", ["global"]),
    ("react", ["global", "project:abc"]),
    ("pnpm", ["global", "project:abc"]),
    ("kubernetes", ["global"]),
]

EMBEDDING_CASES = [
    "react",
    "React 组件",
    "你记得什么",
    "The user prefers concise answers in English meetings",
    "",
    "   ",
    "kubernetes、terraform",
]

NORMALIZE_CASES = [
    "  HeLLo   World  ",
    "项目\t用\nRust",
    "",
    "   ",
    "React  组件",
    "ÄÖÜ  ß",
]

# A raw token corpus for `bm25_scores`, independent of the store: the three shapes
# that separate the branches are a term in one document, a term in every document,
# and a document with no query term at all.
BM25_QUERY = "rust react 组件"
BM25_DOCS = [
    "rust 组件",
    "react",
    "rust react 组件 组件",
    "python",
    "",
    "rust rust react 组件 组件 组件",
]

PARSE_CASES = [
    "[3, 4]",
    "[0.5, -0.5]",
    "[]",
    "{not json",
    "42",
    '["x"]',
    "[1, null, 3]",
    "[1, 2, 3, 4]",
    "[0, 0]",
    "",
]

PURE_BM25_K1 = 1.5
PURE_BM25_B = 0.75


def main() -> int:
    # The corpus and the probe labels are non-ASCII, and this repository runs on
    # Windows (AGENTS.md) where `sys.stdout` defaults to a legacy code page. Pin the
    # stream so the documented `> python.json` redirect is UTF-8 on every host.
    # `getattr` rather than an attribute access: `TextIO` does not declare
    # `reconfigure`, and only a real `TextIOWrapper` has one.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    root = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROOT)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    os.environ["DEEPSEEK_INFRA_ROOT"] = str(root)
    sys.path.insert(0, str(REPO))

    from deepseek_infra.core import utils  # noqa: E402
    from deepseek_infra.infra.data import memory  # noqa: E402
    from deepseek_infra.infra.rag import local_rag  # noqa: E402

    out: dict[str, Any] = {}
    print(f"probe root: {root}", file=sys.stderr)

    # --- pure -----------------------------------------------------------------------
    for index, text in enumerate(EMBEDDING_CASES):
        out[f"pure::embedding-{index}"] = local_rag.hash_text_embedding(text)
    for index, query in enumerate(NORMALIZE_CASES):
        out[f"pure::normalize-{index}"] = local_rag.normalize_search_query(query)
    tokens = utils.query_tokens(local_rag.normalize_search_query(BM25_QUERY))
    docs_terms = [utils.query_tokens(text) for text in BM25_DOCS]
    out["pure::bm25-query-tokens"] = tokens
    out["pure::bm25-doc-tokens"] = docs_terms
    out["pure::bm25"] = local_rag.bm25_scores(tokens, docs_terms, k1=PURE_BM25_K1, b=PURE_BM25_B)
    for index, raw in enumerate(PARSE_CASES):
        out[f"pure::parse-{index}"] = local_rag.parse_embedding(raw)

    # The index is populated by the production write path, not by hand.
    memory.save_memories(CORPUS)
    # Only what both sides derive from the same file and the same configuration is
    # compared; `sqlite_vec_available()` and the plugin's `backend` are Python-process
    # facts with no Rust counterpart, so they would be a difference of shape rather
    # than of behaviour.
    connection, vector_table_ready = local_rag.db_ready()
    try:
        memory_rows = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {local_rag.ITEM_TABLE} WHERE collection = ?",
                (local_rag.COLLECTION_MEMORY,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    out["store::dimensions"] = int(local_rag.embedding_pipeline().dimensions)
    out["store::vector-table-present"] = bool(vector_table_ready)
    out["store::memory-rows"] = memory_rows

    limit = int(memory.MEMORY_RETRIEVE_LIMIT) * 2
    out["store::limit"] = limit
    for query, scopes in QUERIES:
        key = f"{query}::{','.join(scopes)}"
        results = local_rag.search_memories_index(query, scopes=sorted(scopes), limit=limit)
        out[f"index::{key}"] = [
            {
                "id": str(hit.metadata.get("id") or hit.source_id),
                "score": int(hit.score),
                "vector_score": float(hit.vector_score),
                "keyword_score": int(hit.keyword_score),
                "name": str(hit.name),
                "chunk_index": int(hit.chunk_index),
            }
            for hit in results
        ]
        hits: dict[str, int] = {}
        for hit in results:
            memory_id = str(hit.metadata.get("id") or hit.source_id)
            if memory_id:
                hits[memory_id] = max(hits.get(memory_id, 0), int(hit.score))
        out[f"hits::{key}"] = hits

    # --- turn: the live index, then the oracle's own degradation path ---------------
    def retrieve(query: str, scopes: list[str]) -> list[str]:
        return [str(item.get("id") or "") for item in memory.retrieve_memories(query, scopes=scopes)]

    live: dict[str, list[str]] = {}
    without: dict[str, list[str]] = {}
    for query, scopes in QUERIES:
        key = f"{query}::{','.join(scopes)}"
        live[key] = retrieve(query, scopes)

    original = local_rag.search_memories_index

    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("forced for the paired measurement")

    local_rag.search_memories_index = _raise
    try:
        for query, scopes in QUERIES:
            key = f"{query}::{','.join(scopes)}"
            without[key] = retrieve(query, scopes)
    finally:
        local_rag.search_memories_index = original

    for key, value in live.items():
        out[f"retrieve::{key}"] = value
    for key, value in without.items():
        out[f"retrieve-none::{key}"] = value
    out["turn::differing"] = sum(1 for key in live if live[key] != without[key])
    out["turn::queries"] = len(live)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
