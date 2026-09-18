"""Attachment-expansion parity probe, Python side.

Covers the **pure half** of `rag/files.py` (lines 69-283) with
`gateway/chat_payload.expanded_message_content` on top: the chunk selector and its scoring
and embedding helpers, the two formatters, and the attachment orchestration.

Three things are I/O or environment and are stubbed here, injected on the Rust side:

- `load_cached_file(file_id, project_id)` -- the file index;
- `local_rag.search_file_chunks(file_id, project_id, query, limit)` -- the vector index;
- `context_taint_file_guard_line()` -- a module-level alias of
  `gateway/context_taint.file_context_guard_line`. The default settings produce a **non-empty**
  line, so the empty case has to be produced explicitly to keep both branches reachable.

Every corpus row names the branch it is there to reach, because a selector whose paths are
unreachable reports parity whether or not it works.

Usage::

    python tasks/native-runtime/attachment_context_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example attachment_context_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.core.errors import AppError, ErrorCode  # noqa: E402
from deepseek_infra.infra.gateway import chat_payload  # noqa: E402
from deepseek_infra.infra.rag import files, local_rag  # noqa: E402

EMBED_TEXTS = [
    "",
    "a",
    "abc",
    "中文内容",
    "Mixed Case Text",
    "a" * 300,
    "关键词 关键词 关键词",
    "Hello, World! 123",
]

NORMALIZE_CASES: list[Any] = [
    [],
    [1.0, 2.0, 2.0],
    [0.0, 0.0],
    ["x", None, 3],
    list(range(70)),
    [1e-9, -1e-9, 5],
]

COSINE_CASES: list[Any] = [
    ([], []),
    ([1.0], []),
    ([1.0, 0.0], [1.0, 0.0]),
    ([1.0, 2.0], [2.0, 1.0]),
    ([1.0, 1.0, 1.0], [1.0, 1.0]),
    (["x", 2.0], [1.0, 1.0]),
    ([100.0], [100.0]),
    ([-1.0], [1.0]),
    # A non-finite product: Python's `min`/`max` keep the bound when a comparison is
    # false, so this must come out as 1.0 rather than NaN.
    (["nan"], [1.0]),
    ([None, 1.0], [2.0, 1.0]),
]

BROAD_QUERIES = [
    "",
    "全文总结",
    "python 报错怎么修",
    "SUMMARIZE this",
    "附件",
    "随便聊聊",
    "Outline",
    "这份文档的知识点",
]

# Small chunks: `total_chars <= min(FILE_FULL_CONTEXT_LIMIT, char_budget)` -> every index.
SMALL_CHUNKS: list[Any] = [
    {"text": "第一段", "index": 0, "start": 0, "end": 3},
    {"text": "second chunk", "index": 1, "start": 3, "end": 15},
    {"text": "第三段内容", "index": 2, "start": 15, "end": 20},
]

# Large chunks: crosses the full-context limit so the scored/broad/indexed paths run.
BIG_CHUNKS: list[Any] = []
for _i in range(12):
    _text = f"chunk {_i} " + ("关键词" if _i % 3 == 0 else "填充") + ("x" * 5_600)
    BIG_CHUNKS.append(
        {
            "text": _text,
            "index": _i,
            "start": _i * 6_000,
            "end": (_i + 1) * 6_000,
            "lineStart": _i * 10 + 1,
            "lineEnd": (_i + 1) * 10,
        }
    )

BIG_CHUNKS_VECTORED: list[Any] = [
    {**chunk, "vector": [0.5] * 64} if index % 4 == 0 else dict(chunk)
    for index, chunk in enumerate(BIG_CHUNKS)
]

FILE_ID = "f" * 32

# `file_id -> indices` for the injected vector search; anything else finds nothing.
INDEXED: dict[str, list[int]] = {FILE_ID: [5]}

# `(label, chunks, query, char_budget, file_id)` -- one row per selector branch.
SELECT_CASES: list[Any] = [
    ("empty", [], "python 报错", 115_000, ""),
    ("small", SMALL_CHUNKS, "python 报错", 115_000, ""),
    ("big-scored", BIG_CHUNKS_VECTORED, "python 报错 chunk 7", 115_000, ""),
    ("big-broad", BIG_CHUNKS_VECTORED, "全文总结", 115_000, ""),
    ("big-unmatched", BIG_CHUNKS_VECTORED, "zzz 不存在的词", 115_000, ""),
    ("big-tight-budget", BIG_CHUNKS_VECTORED, "python 报错 chunk 7", 9_000, ""),
    ("indexed", BIG_CHUNKS_VECTORED, "python 报错", 115_000, FILE_ID),
    # Nothing scores, so the only way to a non-empty answer is the indexed branch.
    ("indexed-only", BIG_CHUNKS_VECTORED, "zzz 不存在的词", 115_000, FILE_ID),
    ("indexed-tight", BIG_CHUNKS_VECTORED, "python 报错", 9_000, FILE_ID),
]

CHUNK_TEXTS = ["a" * 200, "b" * 200, "c" * 200]

LOCATOR_CASES: list[Any] = [
    ({"lineStart": 3, "lineEnd": 9}, 1, 4, 0, 100),
    ({"lineStart": 0, "lineEnd": 0}, 2, 4, 10, 20),
    ({"lineStart": 5, "lineEnd": 4}, 3, 4, 20, 30),
    ({}, 1, 1, 0, 5),
]

CORPUS_TEXT = "x" * 5_600
HUGE_TEXT = "x" * 120_000

CACHED: dict[str, Any] = {
    FILE_ID: {
        "id": FILE_ID,
        "name": "报告.pdf",
        "kind": "pdf",
        "charCount": 5_600 * 3,
        "projectId": "",
        "chunks": [
            {"text": CORPUS_TEXT, "index": 0, "start": 0, "end": 5_600, "lineStart": 1, "lineEnd": 100},
            {"text": "第二块 " + CORPUS_TEXT, "index": 1, "start": 5_600, "end": 11_200},
            {"text": "", "index": 2, "start": 11_200, "end": 11_200},
        ],
    },
    "g" * 32: {
        "id": "g" * 32,
        "name": "huge.txt",
        "kind": "text",
        "charCount": len(HUGE_TEXT),
        "chunks": [{"text": HUGE_TEXT, "index": 0, "start": 0, "end": len(HUGE_TEXT)}],
    },
}

GUARD_LINE = "[防注入隔离] 探针锚点"


def stub_load_cached_file(file_id: str, project_id: str | None = None) -> dict[str, Any]:
    if file_id == "0" * 32:
        raise AppError(
            "Uploaded file index has expired or is missing",
            code=ErrorCode.FILE_INDEX_EXPIRED,
            status=410,
        )
    cached = CACHED.get(file_id)
    if cached is None:
        raise AppError("Uploaded file index is invalid", code=ErrorCode.INTERNAL, status=500)
    return cached


def stub_search_file_chunks(file_id: str, project_id: str, query: str, *, limit: int = 8) -> list[int]:
    return list(INDEXED.get(file_id, []))[:limit]


def summary(text: str) -> str:
    """Length plus a window, so a 115k-character section stays comparable.

    The window is JSON-escaped rather than `repr`, because Rust's `Debug` for `&str` uses
    double quotes and Python's `repr` single ones — the escaping both sides can agree on is
    the one `json.dumps` produces, which is what the Rust side renders through serde_json.
    """

    return (
        f"len={len(text)}"
        f"|head={json.dumps(text[:60], ensure_ascii=False)}"
        f"|tail={json.dumps(text[-60:], ensure_ascii=False)}"
    )


def main() -> int:
    out: dict[str, Any] = {}

    real_load = files.load_cached_file
    real_search = local_rag.search_file_chunks
    real_guard = files.context_taint_file_guard_line
    files.load_cached_file = stub_load_cached_file
    local_rag.search_file_chunks = stub_search_file_chunks
    files.context_taint_file_guard_line = lambda: GUARD_LINE
    try:
        out["guard-line::default"] = real_guard()
        out["guard-line::patched"] = files.context_taint_file_guard_line()

        for index, text in enumerate(EMBED_TEXTS):
            out[f"embed::{index}"] = local_rag.hash_text_embedding(text)
        for index, vector in enumerate(NORMALIZE_CASES):
            out[f"normalize::{index}"] = local_rag.normalize_vector(vector, 64)
        for index, (left, right) in enumerate(COSINE_CASES):
            out[f"cosine::{index}"] = local_rag.cosine_similarity(left, right)
        for index, query in enumerate(BROAD_QUERIES):
            out[f"broad::{index}"] = files.is_broad_file_query(query)

        tokens = local_rag.query_tokens("python 报错")
        for index, chunk in enumerate(BIG_CHUNKS_VECTORED[:4]):
            text = str(chunk.get("text") or "")
            out[f"score::{index}"] = files.hybrid_chunk_score(chunk, text, tokens, "python 报错")

        for label, chunks, query, budget, file_id in SELECT_CASES:
            out[f"select::{label}"] = files.select_file_chunk_indices(
                chunks, query, char_budget=budget, file_id=file_id
            )

        for index, (chunk, chunk_index, total, start, end) in enumerate(LOCATOR_CASES):
            out[f"locator::{index}"] = files.format_chunk_locator(chunk, chunk_index, total, start, end)

        out["format::empty"] = files.format_cached_file_context(1, CACHED[FILE_ID], "报告讲了什么")
        out["format::no-chunks"] = files.format_cached_file_context(2, {"name": "n"}, "q")
        out["format::tight"] = files.format_cached_file_context(
            1, CACHED[FILE_ID], "报告讲了什么", char_budget=2_000
        )

        attachments_cases: list[Any] = [
            ("none", [], "q"),
            ("non-dict", ["x", 5], "q"),
            ("index", [{"fileId": FILE_ID, "name": "报告.pdf"}], "报告讲了什么"),
            ("index-fails", [{"fileId": "0" * 32, "name": "gone"}], "q"),
            ("index-unnamed", [{"fileId": "0" * 32}], "q"),
            ("legacy", [{"text": "旧版内容", "name": "n", "kind": "text"}], "q"),
            ("legacy-long", [{"text": HUGE_TEXT}], "q"),
            # The per-file share shrinks multiplicatively, so the budget only runs out
            # once the `max(8_000, ...)` floor is what binds -- which needs ~15 rows.
            ("budget-exhausted", [{"text": "x" * 20_000} for _ in range(20)], "q"),
            ("multi", [{"text": "一"}, {"text": "二"}, {"fileId": FILE_ID}], "q"),
        ]
        for label, attachments, query in attachments_cases:
            out[f"attachment::{label}"] = summary(files.build_attachment_context(attachments, query))

        # Without the guard line the header loses a row; patched to "" for that row only.
        files.context_taint_file_guard_line = lambda: ""
        try:
            out["attachment::no-guard"] = summary(
                files.build_attachment_context([{"text": "一"}], "q")
            )
        finally:
            files.context_taint_file_guard_line = lambda: GUARD_LINE

        expanded_cases: list[Any] = [
            ("empty", {}),
            ("content-only", {"content": "  你好  "}),
            ("legacy-attachment", {"content": "", "attachments": [{"text": "旧版内容"}]}),
            ("non-dict-attachments", {"content": "问题", "attachments": ["x"]}),
            ("index", {"content": "报告讲了什么", "attachments": [{"fileId": FILE_ID}]}),
        ]
        for label, message in expanded_cases:
            out[f"expanded::{label}"] = summary(chat_payload.expanded_message_content(message))
    finally:
        files.load_cached_file = real_load
        local_rag.search_file_chunks = real_search
        files.context_taint_file_guard_line = real_guard

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
