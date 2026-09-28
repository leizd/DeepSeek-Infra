"""file-route helper parity probe, oracle side.

Compares `clean_filename`, `content_disposition_header`, `original_file_media_type`,
`file_reader_window`, the `/api/file-chunk` body and `file_page_text` with the Rust
port. The routes themselves are pinned by the gateway's own tests; what this probe
pins is the value-level detail, including a refusal's message, code and status.

Usage::

    python tasks/native-runtime/file_routes_parity_probe.py \\
        --rust-example rust/target/debug/examples/file_routes_parity_probe.exe

**Nothing here is re-implemented.** Every function is imported from `deepseek_infra`, so
a change in the oracle fails this probe rather than silently agreeing with the port.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from deepseek_infra.core.utils import clean_filename  # noqa: E402
from deepseek_infra.web.http_utils import content_disposition_header  # noqa: E402
from deepseek_infra.web.server import original_file_media_type  # noqa: E402

FILENAME_CASES: list[str] = [
    "",
    "   ",
    "report.pdf",
    "  report.pdf  ",
    '"report.pdf"',
    '""',
    "///",
    "\\\\",
    "a/b.txt",
    "a\\b.txt",
    r"C:\Users\me\report.pdf",
    "/tmp/a/b/report.pdf",
    'a"b.txt',
    "my report.pdf",
    "报告.pdf",
    "报告",
    "日本語のファイル.txt",
    "éclair.txt",
    "naïve — dash.txt",
    "emoji 🎉.png",
    "with%percent.txt",
    "with+plus.txt",
    "with#hash.txt",
    "with?query=1.txt",
    "with&and.txt",
    ".hidden",
]

OOXML = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

MEDIA_TYPE_CASES: list[tuple[str, dict[str, Any]]] = [
    ("empty", {}),
    ("pdf_kind", {"kind": "pdf"}),
    ("pdf_type", {"type": "application/pdf"}),
    ("pdf_kind_wins", {"kind": "pdf", "type": "text/plain"}),
    ("image_png", {"kind": "image", "type": "image/png"}),
    ("image_jpeg", {"kind": "image", "type": "image/jpeg"}),
    ("image_svg", {"kind": "image", "type": "image/svg+xml"}),
    ("svg_without_kind", {"type": "image/svg+xml"}),
    ("text_markdown", {"type": "text/markdown"}),
    ("text_plain", {"type": "text/plain"}),
    ("text_with_charset", {"type": "text/plain; charset=utf-16"}),
    ("text_html", {"type": "text/html"}),
    ("kind_csv", {"kind": "csv"}),
    ("kind_md", {"kind": "md"}),
    ("kind_TXT_uppercase", {"kind": "TXT"}),
    ("kind_unknown", {"kind": "zip"}),
    ("ooxml_passthrough", {"type": OOXML}),
    ("ooxml_with_kind", {"kind": "docx", "type": OOXML}),
    ("image_kind_bad_type", {"kind": "image", "type": "text/plain"}),
    ("null_kind", {"kind": None, "type": "text/csv"}),
    ("number_type", {"type": 5}),
]

LONG_LENGTHS = [179, 180, 181, 400]

READER_WINDOWS: list[tuple[int | None, int | None]] = [
    (None, None),
    (1, 2),
    (2, 2),
    (0, 3),
    (99, 2),
    (1, 99),
    (0, 0),
    (-5, -1),
]

READER_CHUNK_INDEXES: list[int | None] = [None, 0, 1, 2, 99, -3]

# `(label, page)` pairs handed to `file_page_text`. `None` is passed explicitly, so
# the function's default is not what runs — `_reader_positive_int(None)` is.
PAGE_CASES: list[tuple[str, Any]] = [
    ("none", None),
    ("one", 1),
    ("two", 2),
    ("three", 3),
    ("five", 5),
    ("ninety-nine", 99),
    ("zero", 0),
    ("negative", -4),
    ("empty", ""),
    ("bad", "x"),
    ("float-string", "1.5"),
    ("padded", " 4 "),
    ("underscore", "1_0"),
    ("bool-true", True),
    ("bool-false", False),
    ("float", 2.5),
]


def reader_indexes() -> list[tuple[str, dict[str, Any]]]:
    """The cached indexes the reader is compared over — the same corpus as Rust."""

    def chunk(index: int) -> dict[str, Any]:
        return {
            "index": index,
            "start": index * 10,
            "end": index * 10 + 9,
            "lineStart": index + 1,
            "lineEnd": index + 1,
            "text": f"chunk {index}",
        }

    return [
        ("empty", {"name": "empty.txt", "chunks": []}),
        (
            "five",
            {"name": "a.txt", "kind": "txt", "size": 42, "chunks": [chunk(i) for i in range(5)]},
        ),
        (
            "twenty",
            {
                "name": "b.txt",
                "kind": "txt",
                "charCount": 90,
                "chunkCount": 20,
                "chunks": [chunk(i) for i in range(20)],
            },
        ),
        (
            "malformed",
            {
                "name": "c.txt",
                "chunks": [
                    {"index": 0, "text": "first"},
                    "not an object",
                    {"text": "third", "index": "2"},
                ],
            },
        ),
        ("string_index", {"name": "d.txt", "chunks": [{"index": "7", "text": "seven"}]}),
        ("no_name", {"chunks": [{"index": 0, "text": "x"}]}),
        (
            "source_available",
            {
                "name": "e.txt",
                "kind": "pdf",
                "type": "application/pdf",
                "sourceAvailable": True,
                "pageCount": 3,
                "chunks": [{"index": 0, "text": "x"}],
            },
        ),
    ]


def _outcome(call: Any) -> dict[str, Any]:
    """A call as a value, so an error's message, code and status are compared too."""
    from deepseek_infra.core.errors import AppError

    try:
        return {"ok": call()}
    except AppError as exc:
        return {"error": {"message": str(exc), "code": exc.code.value, "status": exc.status}}


def oracle_reader_report() -> dict[str, Any]:
    """The oracle's own reader over the corpus, written into its own cache directory.

    `file_reader_window` and the `/api/file-chunk` body both read through
    `load_cached_file`, which resolves `.file-cache/{id}.json` under the configured
    root. `files.py` imports `FILE_CACHE_DIR` **by value** at module load
    (`from ...config import FILE_CACHE_DIR`), so the corpus is written to a temporary
    root and `rag_files.FILE_CACHE_DIR` — the name the functions actually read — is
    repointed at it. The oracle's own functions run unmodified.
    """
    import shutil
    import tempfile

    from deepseek_infra.infra.rag import files as rag_files

    saved = rag_files.FILE_CACHE_DIR
    root = Path(tempfile.mkdtemp(prefix="file-routes-probe-"))
    cache_dir = root / ".file-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    rag_files.FILE_CACHE_DIR = cache_dir
    try:
        windows: dict[str, Any] = {}
        chunks: dict[str, Any] = {}
        for label, index in reader_indexes():
            file_id = f"{len(label):032x}"
            (cache_dir / f"{file_id}.json").write_text(json.dumps(index), encoding="utf-8")
            for start, count in READER_WINDOWS:
                start_label = start if start is not None else "none"
                count_label = count if count is not None else "none"
                key = f"{label}|{start_label}|{count_label}"
                windows[key] = _outcome(
                    lambda start=start, count=count, file_id=file_id: rag_files.file_reader_window(
                        file_id, None, chunk_start=start, chunk_count=count
                    )
                )
            for index_value in READER_CHUNK_INDEXES:
                key = f"{label}|{index_value if index_value is not None else 'none'}"
                chunks[key] = _outcome(
                    lambda index_value=index_value, file_id=file_id: _oracle_file_chunk(
                        rag_files, file_id, index_value
                    )
                )
        return {"reader_windows": windows, "reader_chunks": chunks}
    finally:
        rag_files.FILE_CACHE_DIR = saved
        shutil.rmtree(root, ignore_errors=True)


def _oracle_file_chunk(
    rag_files: Any, file_id: str, chunk_index: int | None
) -> dict[str, Any]:
    """The `/api/file-chunk` body from `web/server.py`, without the HTTP layer.

    Mirrors the handler's lines exactly, including `max(0, int(...) - 1)` and the two
    `AppError`s, and calls the oracle's own `load_cached_file`.
    """
    from deepseek_infra.core.errors import AppError, ErrorCode

    try:
        chunk_index_value = max(0, int(chunk_index or 0) - 1)
    except (TypeError, ValueError) as exc:
        raise AppError("Invalid chunk index", code=ErrorCode.INVALID_PAYLOAD, status=400) from exc
    cached = rag_files.load_cached_file(file_id, project_id=None)
    raw_chunks = cached.get("chunks")
    chunks: list[Any] = raw_chunks if isinstance(raw_chunks, list) else []
    if chunk_index_value >= len(chunks):
        raise AppError("Chunk not found", code=ErrorCode.NOT_FOUND, status=404)
    chunk = chunks[chunk_index_value]
    if not isinstance(chunk, dict):
        raise AppError("Chunk not found", code=ErrorCode.NOT_FOUND, status=404)
    return {
        "file": {
            "name": cached.get("name"),
            "kind": cached.get("kind"),
            "fileId": file_id,
            "projectId": "",
        },
        "chunk": chunk,
    }


def page_indexes() -> list[tuple[str, str, dict[str, Any]]]:
    """Cached indexes for `file_page_text`. Ids are shared with the Rust probe."""

    long_text = "a" * 40_001
    return [
        (
            "raised",
            "a" * 32,
            {
                "name": "a.pdf",
                "kind": "pdf",
                "type": "application/pdf",
                "size": 120,
                "charCount": 18,
                "pageCount": 2,
                "sourceAvailable": True,
                "pageTexts": [
                    {"page": 1, "text": "page one"},
                    {"page": 5, "text": "page five"},
                ],
                "chunks": [{"index": 0, "text": "chunk text"}],
            },
        ),
        (
            "chunks",
            "b" * 32,
            {
                "name": "split.txt",
                "pageCount": 4,
                "chunks": [{"text": "aaaa"}, {"text": "bbbb"}, {"text": "cccc"}],
            },
        ),
        ("empty", "c" * 32, {"name": "empty.txt", "chunks": []}),
        (
            "malformed",
            "d" * 32,
            {
                "name": "m.pdf",
                "kind": "pdf",
                "pageCount": 1,
                "pageTexts": [
                    {"page": 1, "text": "one"},
                    {"page": 0, "text": "zero"},
                    {"page": 2.5, "text": "two-and-a-half"},
                    {"page": True, "text": "from-bool"},
                    {"page": "4", "text": "four"},
                    {"page": "x", "text": "bad"},
                    {"page": "1.5", "text": "float-string"},
                    {"page": 3, "text": "   \r\n  "},
                    "not an object",
                    {"page": 6, "text": 5},
                    {"text": "no page"},
                ],
                "chunks": [{"text": "fallback"}],
            },
        ),
        (
            "crlf",
            "e" * 32,
            {
                "name": "lines.txt",
                "pageCount": 1,
                "pageTexts": [{"page": 1, "text": "  a  \r\nb\x00c  \n"}],
                "chunks": [],
            },
        ),
        (
            "cjk",
            "1" * 32,
            {
                "name": "页.pdf",
                "kind": "pdf",
                "pageCount": 1,
                "pageTexts": [{"page": 1, "text": "第一页"}],
                "chunks": [],
            },
        ),
        (
            "capped",
            "2" * 32,
            {
                "name": "long.txt",
                "pageCount": 1,
                "pageTexts": [{"page": 1, "text": long_text}],
                "chunks": [],
            },
        ),
        ("noname", "3" * 32, {"pageCount": 1, "chunks": [{"text": "only"}]}),
    ]


def oracle_page_report() -> dict[str, Any]:
    """The oracle's own `file_page_text` over the page corpus.

    Same cache-directory repoint as the reader: `files.py` captured `FILE_CACHE_DIR`
    by value, so the corpus is written under a temporary root and `rag_files.FILE_CACHE_DIR`
    is what the function actually reads.
    """
    import shutil
    import tempfile

    from deepseek_infra.infra.rag import files as rag_files

    saved = rag_files.FILE_CACHE_DIR
    root = Path(tempfile.mkdtemp(prefix="file-page-probe-"))
    cache_dir = root / ".file-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    rag_files.FILE_CACHE_DIR = cache_dir
    try:
        pages: dict[str, Any] = {}
        for label, file_id, index in page_indexes():
            (cache_dir / f"{file_id}.json").write_text(
                json.dumps(index, ensure_ascii=False), encoding="utf-8"
            )
            for page_label, page in PAGE_CASES:
                key = f"{label}|{page_label}"
                pages[key] = _outcome(
                    lambda file_id=file_id, page=page: rag_files.file_page_text(
                        file_id, None, page=page
                    )
                )
        pages["missing|one"] = _outcome(
            lambda: rag_files.file_page_text("9" * 32, None, page=1)
        )
        pages["bad-id|one"] = _outcome(
            lambda: rag_files.file_page_text("../escape", None, page=1)
        )
        return {"page_texts": pages}
    finally:
        rag_files.FILE_CACHE_DIR = saved
        shutil.rmtree(root, ignore_errors=True)


SEARCH_QUERIES: list[tuple[str, str]] = [
    ("page", "page"),
    ("PAGE", "PAGE"),
    ("padded", "  page  "),
    ("blank", "   "),
    ("missing-word", "nope"),
    ("eszett", "strasse"),
    ("long", "q" * 201),
]


def search_indexes() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (
            "pages",
            "4" * 32,
            {
                "name": "a.pdf",
                "kind": "pdf",
                "pageCount": 1,
                "pageTexts": [
                    {"page": 1, "text": "See Straße and Page"},
                    {"page": 5, "text": "page five"},
                ],
                "chunks": [],
            },
        ),
        (
            "chunks",
            "5" * 32,
            {
                "name": "c.txt",
                "pageCount": 2,
                "chunks": [{"text": "alpha BETA"}, {"text": "gamma"}],
            },
        ),
    ]


def oracle_search_report() -> dict[str, Any]:
    """The oracle's own `file_page_search`, including `str.casefold`."""
    import shutil
    import tempfile

    from deepseek_infra.infra.rag import files as rag_files

    saved = rag_files.FILE_CACHE_DIR
    root = Path(tempfile.mkdtemp(prefix="file-search-probe-"))
    cache_dir = root / ".file-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    rag_files.FILE_CACHE_DIR = cache_dir
    try:
        found: dict[str, Any] = {}
        for label, file_id, index in search_indexes():
            (cache_dir / f"{file_id}.json").write_text(
                json.dumps(index, ensure_ascii=False), encoding="utf-8"
            )
            for query_label, query in SEARCH_QUERIES:
                key = f"{label}|{query_label}"
                found[key] = _outcome(
                    lambda file_id=file_id, query=query: rag_files.file_page_search(
                        file_id, None, query=query
                    )
                )
        return {"page_search": found}
    finally:
        rag_files.FILE_CACHE_DIR = saved
        shutil.rmtree(root, ignore_errors=True)


def oracle_report() -> dict[str, Any]:
    return {
        "clean_filename": {case: clean_filename(case) for case in FILENAME_CASES},
        "disposition_inline": {
            case: content_disposition_header("inline", case) for case in FILENAME_CASES
        },
        "disposition_attachment": {
            case: content_disposition_header("attachment", case) for case in FILENAME_CASES
        },
        "media_types": {
            label: original_file_media_type(cached) for label, cached in MEDIA_TYPE_CASES
        },
        "long_names": {
            str(length): {
                "cleaned": clean_filename("a" * length),
                "cleaned_len": len(clean_filename("a" * length)),
                "disposition": content_disposition_header("inline", "a" * length),
            }
            for length in LONG_LENGTHS
        },
        **oracle_reader_report(),
        **oracle_page_report(),
        **oracle_search_report(),
    }


def run_rust_example(example: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(example)], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if completed.returncode != 0:
        print(
            f"file_routes_parity_probe: the Rust probe exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(
            f"file_routes_parity_probe: the Rust probe did not print JSON: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rust-json", type=Path, help="a report captured from the Rust probe")
    source.add_argument("--rust-example", type=Path, help="the Rust probe binary to run")
    parser.add_argument("--report", type=Path, help="where to write the comparison report")
    args = parser.parse_args()

    oracle = oracle_report()
    native = (
        run_rust_example(args.rust_example)
        if args.rust_example is not None
        else json.loads(Path(args.rust_json).read_text(encoding="utf-8"))
    )

    problems: list[str] = []
    compared = 0
    for section in (
        "clean_filename",
        "disposition_inline",
        "disposition_attachment",
        "media_types",
        "long_names",
        "reader_windows",
        "reader_chunks",
        "page_texts",
        "page_search",
    ):
        expected = oracle.get(section)
        actual = native.get(section)
        if isinstance(expected, dict) and isinstance(actual, dict):
            for key in sorted(set(expected) | set(actual)):
                compared += 1
                if expected.get(key) != actual.get(key):
                    problems.append(
                        f"{section}.{key}: oracle={expected.get(key)!r} native={actual.get(key)!r}"
                    )
        else:
            problems.append(f"{section}: oracle={expected!r} native={actual!r}")

    report = {
        "cases_compared": compared,
        "problems": problems,
        "result": "PASS" if not problems else "FAIL",
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
