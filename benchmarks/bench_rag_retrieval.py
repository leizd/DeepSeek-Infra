#!/usr/bin/env python3
"""RAG 检索基准（离线、无需 API Key、不碰真实 .local-rag）。

把 evals golden 集引用的参考文档索引进临时本地索引（hash embedding + BM25
hybrid），对每个 golden 问题重复检索 ``--repeat`` 次，输出：

- 索引吞吐（docs / chunks / 耗时）
- 检索延迟 avg / P50 / P95 / max
- 质量：Recall@K 与 MRR（与 ``evals/runners/run_rag_eval.py`` 同口径）

运行::

    python benchmarks/bench_rag_retrieval.py
    python benchmarks/bench_rag_retrieval.py --repeat 10 --k 5 --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.evaluation import harness  # noqa: E402
from deepseek_infra.infra.rag import local_rag  # noqa: E402

logging.getLogger("deepseek_infra").setLevel(logging.ERROR)

CHUNK_CHAR_BUDGET = 800


def chunk_document(text: str) -> list[dict[str, Any]]:
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


def run_benchmark(golden: list[dict[str, Any]], *, k: int, repeat: int) -> dict[str, Any]:
    sources = sorted({str(row.get("expected_source") or "") for row in golden if row.get("expected_source")})
    indexed_docs = 0
    indexed_chunks = 0
    index_started = time.perf_counter()
    for source in sources:
        path = REPO_ROOT / source
        if not path.exists():
            print(f"warning: golden source not found: {source}", file=sys.stderr)
            continue
        payload = {"id": source, "name": Path(source).name, "kind": "text", "chunks": chunk_document(path.read_text(encoding="utf-8"))}
        indexed_chunks += local_rag.index_file_payload(payload)
        indexed_docs += 1
    index_ms = (time.perf_counter() - index_started) * 1000.0

    latencies: list[float] = []
    rankings: list[list[str]] = []
    relevant_sets: list[set[str]] = []
    for case in golden:
        question = str(case.get("question") or "").strip()
        expected_source = str(case.get("expected_source") or "")
        if not question:
            continue
        ranked: list[str] = []
        for _ in range(max(1, repeat)):
            started = time.perf_counter()
            results = local_rag.search_files_index(question, limit=k)
            latencies.append((time.perf_counter() - started) * 1000.0)
            ranked = [result.source_id for result in results]
        rankings.append(ranked)
        relevant_sets.append({expected_source} if expected_source else set())

    recall = harness.recall_at_k(rankings, relevant_sets, k)
    latency = harness.latency_benchmark(latencies)
    return {
        "suite": "bench_rag_retrieval",
        "corpus": {"docs": indexed_docs, "chunks": indexed_chunks, "indexMs": round(index_ms, 1)},
        "queries": len(rankings),
        "searchesTimed": len(latencies),
        "k": k,
        "recall": recall,
        "latencyMs": latency,
        "embedding": local_rag.embedding_pipeline().active_provider,
    }


def main(argv: list[str] | None = None) -> int:
    # 与 run_rag_eval 相同：钉 PYTHONHASHSEED 让近似打平文档的排序逐次可复现。
    if os.environ.get("PYTHONHASHSEED") != "0":
        child_env = {**os.environ, "PYTHONHASHSEED": "0"}
        return subprocess.run([sys.executable, *sys.argv], env=child_env, check=False).returncode

    parser = argparse.ArgumentParser(description="Offline RAG retrieval benchmark")
    parser.add_argument("--golden", default=str(REPO_ROOT / "evals" / "golden" / "rag_questions.jsonl"))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--repeat", type=int, default=5, help="每个问题重复检索次数（取延迟分布）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    golden = harness.load_jsonl(args.golden)
    index_dir = Path(tempfile.mkdtemp(prefix="rag-bench-"))
    local_rag.LOCAL_RAG_ENABLED = True
    local_rag.LOCAL_RAG_DIR = index_dir
    local_rag.LOCAL_RAG_DB = index_dir / "rag.sqlite3"
    try:
        report = run_benchmark(golden, k=max(1, args.k), repeat=args.repeat)
    finally:
        shutil.rmtree(index_dir, ignore_errors=True)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    corpus = report["corpus"]
    latency = report["latencyMs"]
    recall = report["recall"]
    print("=== Benchmark · RAG retrieval (offline) ===")
    print(f"Corpus: {corpus['docs']} docs / {corpus['chunks']} chunks, indexed in {corpus['indexMs']:.0f} ms")
    print(f"Embedding provider: {report['embedding']} (hybrid: vector + BM25)")
    print(f"Queries: {report['queries']} × repeat → {report['searchesTimed']} timed searches")
    print(f"Latency: avg {latency['avgMs']:.1f} ms · P50 {latency['p50Ms']:.1f} ms · P95 {latency['p95Ms']:.1f} ms · max {latency['maxMs']:.1f} ms")
    print(f"RAG Recall@{report['k']}: {recall['recallAtK']:.3f} · MRR: {recall['mrr']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
