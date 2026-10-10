"""Decision C measurement: does the memory vector bonus change what is retrieved?

The wiring plan (``tasks/native-runtime/assembly-wiring-plan.md`` §4) recorded an
open question before ``prepare_memory_state`` can be ported with ``vector_hits =
None``: the Rust port has no provider for ``local_rag.search_memories_index``, but
``LOCAL_RAG_ENABLED`` defaults to **true** and the default embedding provider is
``hash`` — fully offline and deterministic — so the bonus the oracle applies is
real in a default deployment, not an API-key-only path.

This probe answers it with a paired measurement over the **real** modules:

- run A: the index is live — ``save_memories`` populates it through
  ``sync_memories`` exactly as production does, and ``retrieve_memories`` is
  called with ``local_rag`` importable;
- run B: ``local_rag.search_memories_index`` is monkeypatched to raise, so
  ``retrieve_memories`` takes its own ``except Exception`` path (``vector_hits =
  {}``) — the state the Rust port defaults to.

Both runs read the **same** store file (``retrieve_memories`` never writes), so
the only variable is the bonus. Run A is executed twice to prove determinism:
any A/B difference is the index, not flakiness.

The corpus is engineered so the probe can fail:

- ``m-tie-a`` / ``m-tie-b`` carry one ``React`` occurrence each and the same
  ``updatedAt``, so their lexical scores and sort keys tie exactly — only the
  vector bonus (or load order) can separate them;
- ``m-proj2`` is out of scope for the global queries and in scope for the
  scoped ones, so the scope filter is exercised in both directions;
- ``m-pinned`` carries the +100 that dominates lexical ties.

Usage::

    python tasks/native-runtime/memory_vector_bonus_probe.py
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

QUERIES = [
    ("react", ["global"]),
    ("React 组件", ["global"]),
    ("你记得什么", ["global"]),
    ("我的生日", ["global"]),
    ("后端", ["global"]),
    ("react", ["global", "project:abc"]),
    ("pnpm", ["global", "project:abc"]),
    ("kubernetes", ["global"]),
]


def retrieve_all(memory, query: str, scopes: list[str]) -> list[str]:
    return [
        str(item.get("id") or "")
        for item in memory.retrieve_memories(query, scopes=scopes)
    ]


def index_hits(local_rag, query: str, scopes: list[str]) -> list[dict]:
    return [
        {
            "id": str(hit.metadata.get("id") or hit.source_id),
            "score": int(hit.score),
        }
        for hit in local_rag.search_memories_index(query, scopes=sorted(scopes), limit=24)
    ]


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="memory-vector-bonus-"))
    os.environ["DEEPSEEK_INFRA_ROOT"] = str(root)
    sys.path.insert(0, str(REPO))

    from deepseek_infra.infra.data import memory, memory as memory_module  # noqa: E402
    from deepseek_infra.infra.rag import local_rag  # noqa: E402

    out: dict = {}
    try:
        out["config"] = {
            "root": str(root),
            "local_rag_enabled": bool(local_rag.LOCAL_RAG_ENABLED),
            "backend": str(local_rag.LOCAL_RAG_BACKEND),
            "embedding_provider": str(local_rag.LOCAL_RAG_EMBEDDING_PROVIDER),
            "db": str(local_rag.LOCAL_RAG_DB),
        }

        # The production write path: save_memories -> sync_memories populates the index.
        memory.save_memories(CORPUS)
        out["store_ids"] = [str(item.get("id") or "") for item in memory.load_memories()]

        # Run A (live index), executed twice — determinism control.
        run_a: dict[str, list[str]] = {}
        run_a2: dict[str, list[str]] = {}
        hits_a: dict[str, list[dict]] = {}
        for query, scopes in QUERIES:
            key = f"{query}::{','.join(scopes)}"
            run_a[key] = retrieve_all(memory_module, query, scopes)
            hits_a[key] = index_hits(local_rag, query, scopes)
        for query, scopes in QUERIES:
            key = f"{query}::{','.join(scopes)}"
            run_a2[key] = retrieve_all(memory_module, query, scopes)

        out["deterministic"] = run_a == run_a2
        out["index_hits"] = hits_a

        # Run B: the index is forced to raise, taking the oracle's except path.
        def _raise(*_args, **_kwargs):
            raise RuntimeError("forced for the paired measurement")

        original = local_rag.search_memories_index
        local_rag.search_memories_index = _raise
        try:
            run_b: dict[str, list[str]] = {}
            for query, scopes in QUERIES:
                key = f"{query}::{','.join(scopes)}"
                run_b[key] = retrieve_all(memory_module, query, scopes)
        finally:
            local_rag.search_memories_index = original

        out["run_a"] = run_a
        out["run_b"] = run_b
        out["differs"] = {
            key: {"a": run_a[key], "b": run_b[key], "same_order": run_a[key] == run_b[key]}
            for key in run_a
        }
        out["summary"] = {
            "queries": len(run_a),
            "differing": sum(1 for key in run_a if run_a[key] != run_b[key]),
            "queries_with_live_hits": sum(1 for hits in hits_a.values() if hits),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
