#!/usr/bin/env python3
"""RAG regression eval: real (offline) retrieval against the repo's own docs.

Indexes each ``expected_source`` doc into an *isolated* local RAG index (hash
embeddings + BM25, no network, no API key), runs every golden question through
``local_rag.search_files_index``, and scores RAG Recall@K, Citation Accuracy,
Keyword Coverage and retrieval latency with the pure ``evaluation.harness`` library.

Usage::

    python evals/runners/run_rag_eval.py
    python evals/runners/run_rag_eval.py --golden evals/golden/rag_questions.jsonl --k 5 --json

The index is built in a throwaway temp dir, so your real ``.local-rag`` is never
touched. Reports are written to ``evals/reports/`` unless ``--no-report`` is given.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.evaluation import harness  # noqa: E402
from deepseek_infra.infra.rag import local_rag  # noqa: E402

CHUNK_CHAR_BUDGET = 800


def chunk_document(text: str) -> list[dict[str, Any]]:
    """Split a document into ~CHUNK_CHAR_BUDGET-char chunks with line tracking."""
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


def index_documents(sources: list[str], docs_root: Path) -> dict[str, int]:
    """Index each source file under its repo-relative path as the source id."""
    indexed: dict[str, int] = {}
    for source in sources:
        path = docs_root / source
        if not path.exists():
            print(f"warning: golden source not found: {source}", file=sys.stderr)
            continue
        payload = {
            "id": source,
            "name": Path(source).name,
            "kind": "text",
            "chunks": chunk_document(path.read_text(encoding="utf-8")),
        }
        indexed[source] = local_rag.index_file_payload(payload)
    return indexed


def evaluate(golden: list[dict[str, Any]], *, k: int, docs_root: Path) -> harness.EvalReport:
    sources = sorted({str(row.get("expected_source") or "") for row in golden if row.get("expected_source")})
    index_documents(sources, docs_root)

    rows: list[dict[str, Any]] = []
    for case in golden:
        question = str(case.get("question") or "").strip()
        expected_source = str(case.get("expected_source") or "")
        keywords = [str(kw) for kw in (case.get("expected_keywords") or [])]
        if not question:
            continue
        started = time.perf_counter()
        results = local_rag.search_files_index(question, limit=k)
        latency_ms = (time.perf_counter() - started) * 1000.0
        ranked = [result.source_id for result in results]
        hit, rank = harness.recall_hit(ranked, {expected_source}, k)
        top_source = ranked[0] if ranked else ""
        # Grounding is checked across the retrieved *context* (the top-K chunks the
        # model would actually see), not just the single #1 chunk, since the expected
        # facts are legitimately spread across chunks of the cited document.
        context_text = "\n\n".join(result.text for result in results[:k])
        citation = harness.citation_case(top_source, expected_source, context_text, keywords)
        rows.append(
            {
                "id": str(case.get("id") or ""),
                "question": question,
                "hit": hit,
                "rank": rank,
                "topSource": top_source,
                "topSources": ranked[:k],
                "latencyMs": round(latency_ms, 2),
                **citation,
            }
        )
    return harness.build_rag_report(rows, k=k, suite="rag")


def main(argv: list[str] | None = None) -> int:
    # Reproducible regression: hash-seed-driven set iteration over query terms can flip
    # the BM25 float sum at an int-rounding boundary for near-tied docs, so the same
    # eval would wobble by a case run-to-run. Pin PYTHONHASHSEED=0 by re-executing once
    # (only in the CLI/__main__ path; importing this module for tests never re-execs).
    if os.environ.get("PYTHONHASHSEED") != "0":
        child_env = {**os.environ, "PYTHONHASHSEED": "0"}
        return subprocess.run([sys.executable, *sys.argv], env=child_env, check=False).returncode

    parser = argparse.ArgumentParser(description="RAG retrieval regression eval")
    parser.add_argument("--golden", default=str(REPO_ROOT / "evals" / "golden" / "rag_questions.jsonl"))
    parser.add_argument("--docs-root", default=str(REPO_ROOT), help="Root the expected_source paths are relative to.")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--report-dir", default=str(REPO_ROOT / "evals" / "reports"))
    parser.add_argument("--no-report", action="store_true", help="Skip writing the JSON report.")
    parser.add_argument("--json", action="store_true", help="Print the machine-readable report dict instead of text.")
    parser.add_argument(
        "--fail-under-recall",
        type=float,
        default=0.0,
        help="CI 门禁：Recall@K 低于该值时退出码 1（默认 0 = 只报告不拦截）。",
    )
    args = parser.parse_args(argv)

    golden = harness.load_jsonl(args.golden)
    index_dir = Path(tempfile.mkdtemp(prefix="rag-eval-"))
    # Redirect the local RAG index at a throwaway dir so the real .local-rag is untouched.
    local_rag.LOCAL_RAG_ENABLED = True
    local_rag.LOCAL_RAG_DIR = index_dir
    local_rag.LOCAL_RAG_DB = index_dir / "rag.sqlite3"
    try:
        report = evaluate(golden, k=max(1, int(args.k)), docs_root=Path(args.docs_root))
    finally:
        shutil.rmtree(index_dir, ignore_errors=True)

    if args.json:
        import json

        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.to_text())
    if not args.no_report:
        path = report.write(args.report_dir)
        print(f"\nReport written to {path}", file=sys.stderr)
    recall = float(report.metrics.get("ragRecallAtK") or 0.0)
    if args.fail_under_recall > 0 and recall < args.fail_under_recall:
        print(f"FAIL: RAG Recall@{report.k} {recall:.3f} < gate {args.fail_under_recall:.3f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
