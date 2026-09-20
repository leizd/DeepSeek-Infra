"""search_files parity probe, Python side.

Pins `compact_snippet`, the empty-query refusal, json_hybrid scoring over the
file cache and project indexes, and the merge/sort envelope of `search_files`.

`index_file_payload` is a RAG sqlite *write*. Python still owns that database, so
this probe stubs `search_files_index` to `[]` and `index_file_payload` to a
no-op — the json_hybrid path the native port always runs, without introducing a
second writer. The sqlite read path is covered by `memory_index` (same
cosine+BM25 over `rag_items`) plus unit tests.

Usage::

    python tasks/native-runtime/search_files_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example search_files_parity_probe > ../rust.json
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
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.tool_runtime import tools  # noqa: E402

SNIPPET_CASES = [
    ("short", "  hello   world  ", "hello", 700),
    ("exact-limit", "a" * 700, "a", 700),
    ("long-a", "a" * 2000, "a", 700),
    ("window-needle", ("prefix " * 40) + "NEEDLE" + (" tail" * 40), "needle", 40),
    ("cjk", "甲" * 80 + "关键词" + "乙" * 80, "关键词", 20),
    ("empty-text", "", "q", 700),
]


def error_view(exc: AppError) -> dict[str, Any]:
    return {"error": str(exc), "code": exc.code.value, "status": exc.status}


def write_cache(root: Path, relative: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def main() -> int:
    out: dict[str, Any] = {}
    for label, text, query, limit in SNIPPET_CASES:
        out[f"snippet::{label}"] = tools.compact_snippet(text, query, limit=limit)

    try:
        tools.search_files("")
        out["empty"] = "ok"
    except AppError as exc:
        out["empty"] = error_view(exc)
    try:
        tools.search_files("   ")
        out["blank"] = "ok"
    except AppError as exc:
        out["blank"] = error_view(exc)

    root = Path(tempfile.mkdtemp(prefix="search-files-parity-"))
    os.environ["DEEPSEEK_INFRA_ROOT"] = str(root)
    # Re-bind the module globals the branch reads.
    tools.FILE_CACHE_DIR = root / ".file-cache"
    tools.PROJECTS_DIR = root / ".projects"

    tools.local_rag.index_file_payload = lambda *_args, **_kwargs: 0
    tools.local_rag.search_files_index = lambda *_args, **_kwargs: []

    write_cache(
        root,
        ".file-cache/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
        json.dumps(
            {
                "id": "a" * 32,
                "name": "react-notes.txt",
                "kind": "text",
                "chunks": [{"index": 0, "text": "useMemo 可以缓存计算结果", "lineStart": 1, "lineEnd": 1}],
            },
            ensure_ascii=False,
        ),
    )
    write_cache(
        root,
        ".projects/proj_1/files/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json",
        json.dumps(
            {
                "id": "b" * 32,
                "name": "project-guide.txt",
                "kind": "text",
                "chunks": [{"index": 0, "text": "项目空间里的 useMemo 笔记", "lineStart": 2, "lineEnd": 3}],
            },
            ensure_ascii=False,
        ),
    )
    write_cache(root, ".file-cache/not-json.json", "not json")
    write_cache(root, ".file-cache/array.json", "[]")
    write_cache(
        root,
        ".projects/proj/files/cccccccccccccccccccccccccccccccc.json",
        json.dumps(
            {
                "id": "file",
                "name": "notes",
                "kind": "text",
                "chunks": [None, {"index": 0, "text": "needle text", "lineStart": 1, "lineEnd": 2}],
            },
            ensure_ascii=False,
        ),
    )

    out["two-indexes"] = tools.search_files("useMemo", limit=10)
    out["needle"] = tools.search_files("needle", limit=2)
    out["no-hit"] = tools.search_files("zzzz-not-present", limit=5)

    shutil.rmtree(root, ignore_errors=True)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
