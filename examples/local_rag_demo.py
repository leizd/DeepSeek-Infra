#!/usr/bin/env python3
"""本地 RAG demo：完全离线、无需 API Key、不碰你真实的 .local-rag 索引。

把仓库自身的 ``docs/*.md`` 索引进一个**临时**本地 RAG 索引（hash embedding +
BM25 hybrid），跑一次检索，并展示两件 RAG Infra 该有的事：

1. **chunk lineage**：每条检索结果都能回溯到 文档 / chunk / 行号 / 内容哈希；
2. **引用真实性校验**：``verify_citation`` 验证「引用的片段真的存在于该 chunk」。

运行::

    python examples/local_rag_demo.py
    python examples/local_rag_demo.py --query "trace 瀑布图在哪里看？" --k 3
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.rag import local_rag  # noqa: E402

# 零依赖路径下 sqlite-vec 缺失会按预期回退并告警；demo 输出保持干净。
logging.getLogger("deepseek_infra").setLevel(logging.ERROR)

CHUNK_CHAR_BUDGET = 800


def chunk_document(text: str) -> list[dict[str, Any]]:
    """与 evals/runners/run_rag_eval.py 相同的朴素分块：~800 字符、记录行号。"""
    lines = text.splitlines()
    chunks: list[dict[str, Any]] = []
    buffer: list[str] = []
    line_start = 1
    size = 0
    index = 0
    for line_number, line in enumerate(lines, start=1):
        buffer.append(line)
        size += len(line) + 1
        if size >= CHUNK_CHAR_BUDGET:
            chunks.append({"text": "\n".join(buffer).strip(), "index": index, "lineStart": line_start, "lineEnd": line_number})
            index += 1
            buffer = []
            size = 0
            line_start = line_number + 1
    if buffer and "\n".join(buffer).strip():
        chunks.append({"text": "\n".join(buffer).strip(), "index": index, "lineStart": line_start, "lineEnd": len(lines)})
    return [chunk for chunk in chunks if chunk["text"]]


# 与 evals/golden 的语料口径一致：只索引聚焦的参考文档。总览类页面（状态矩阵 /
# Demo / 部署 / 威胁模型）什么都提一嘴，会在检索里盖过真正含答案的具体文档。
META_OVERVIEW_DOCS = {"IMPLEMENTATION_STATUS.md", "DEMO.md", "DEPLOYMENT.md", "THREAT_MODEL.md"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline local RAG demo (lineage + citation check)")
    parser.add_argument("--query", default="fetch_url 的 SSRF 防护会拦截哪些内网或元数据地址？")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--docs", default=str(REPO_ROOT / "docs"), help="要索引的目录（默认仓库 docs/）")
    args = parser.parse_args()

    docs_dir = Path(args.docs)
    documents = sorted(path for path in docs_dir.glob("*.md") if path.name not in META_OVERVIEW_DOCS)
    if not documents:
        print(f"目录里没有 .md 文档：{docs_dir}", file=sys.stderr)
        return 1

    index_dir = Path(tempfile.mkdtemp(prefix="rag-demo-"))
    # 与 eval runner 相同的隔离手法：把模块级索引路径指到临时目录。
    local_rag.LOCAL_RAG_ENABLED = True
    local_rag.LOCAL_RAG_DIR = index_dir
    local_rag.LOCAL_RAG_DB = index_dir / "rag.sqlite3"
    try:
        total_chunks = 0
        started = time.perf_counter()
        for path in documents:
            payload = {
                "id": f"docs/{path.name}",
                "name": path.name,
                "kind": "text",
                "chunks": chunk_document(path.read_text(encoding="utf-8")),
            }
            total_chunks += local_rag.index_file_payload(payload)
        index_ms = (time.perf_counter() - started) * 1000.0
        print(f"indexed {len(documents)} docs / {total_chunks} chunks in {index_ms:.0f} ms（临时索引：{index_dir.name}）")

        started = time.perf_counter()
        results = local_rag.search_files_index(args.query, limit=max(1, args.k))
        search_ms = (time.perf_counter() - started) * 1000.0
        print(f'\nquery: "{args.query}"  ({search_ms:.1f} ms, hybrid = vector*100 + bm25*10)')
        if not results:
            print("没有检索结果", file=sys.stderr)
            return 1

        for position, result in enumerate(results, start=1):
            flat = " ".join(result.text.split())
            snippet = flat[:110] + ("…" if len(flat) > 110 else "")
            print(f"\n#{position} {result.source_id} · score={result.score} (vector={result.vector_score:.4f}, bm25={result.keyword_score})")
            print(f"   {snippet}")

        top = results[0]
        lineage = local_rag.chunk_lineage(top)
        print("\n[chunk lineage] 检索结果可回溯：")
        for key in ("chunkId", "docId", "page", "startChar", "endChar", "hash", "docVersion"):
            if lineage.get(key) not in (None, ""):
                print(f"   {key}: {lineage[key]}")

        quoted = " ".join(top.text.split())[:60]
        verdict = local_rag.verify_citation(top.item_id, quoted)
        print(f'\n[verify_citation] 片段 "{quoted[:40]}…" → grounded={verdict.get("grounded")} coverage={verdict.get("coverage")}')
        fabricated = local_rag.verify_citation(top.item_id, "这句话并不存在于任何被索引的文档之中。")
        print(f"[verify_citation] 编造片段 → grounded={fabricated.get('grounded')}（引用造假会被拒绝）")
        return 0
    finally:
        shutil.rmtree(index_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
