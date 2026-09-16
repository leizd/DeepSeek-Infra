"""File-cache and projects-branch parity probe, Python side (slice E2).

Covers `load_cached_file` and its helpers from `infra/rag/files.py`, plus the
`list_project_files_tool` / `read_file_chunk_tool` / `project_document_for_tool`
bodies from `tool_runtime/tools.py`, on top of the projects store already verified in
`projects_parity_probe.py` (imported here, not re-extracted).

Usage::

    python tasks/native-runtime/file_cache_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example file_cache_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import projects_parity_probe as projects_probe  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FILES = REPO / "deepseek_infra" / "infra" / "rag" / "files.py"
TOOLS = REPO / "deepseek_infra" / "infra" / "tool_runtime" / "tools.py"
ERRORS = REPO / "deepseek_infra" / "core" / "errors.py"

FILE_FUNCTIONS = (
    "load_cached_file",
    "_load_cached_file_cached",
    "_load_cached_file_impl",
    "_load_cached_file_impl_from_path",
    "project_file_cache_dir",
)
TOOL_FUNCTIONS = (
    "list_project_files_tool",
    "project_document_for_tool",
    "read_file_chunk_tool",
)

GOOD_ID = "a" * 32

INDEX_BODY = {
    "id": "file-under-test",
    "name": "Report.pdf",
    "kind": "pdf",
    "projectId": "abcd",
    "chunks": [
        {"lineStart": 1, "lineEnd": 10, "text": "first chunk"},
        {"lineStart": 11, "lineEnd": 20, "text": "x" * 7000},
        {"lineStart": 21, "lineEnd": 30, "text": 42},
        "not-a-chunk",
    ],
}

LOAD_CASES = [
    ("bad-short-id", "abc", None),
    ("bad-uppercase-id", "A" * 32, None),
    ("bad-nonhex-id", "g" * 32, None),
    ("empty-id", "", None),
]

CHUNK_CASES = [
    ("first-default", {}, None),
    ("index-one", {"chunkIndex": 1}, None),
    ("index-two", {"chunkIndex": 2}, None),
    ("index-last", {"chunkIndex": 4}, None),
    ("out-of-range", {"chunkIndex": 5}, None),
    ("large-index", {"chunkIndex": 99}, None),
    ("zero-index", {"chunkIndex": 0}, None),
    ("negative-index", {"chunkIndex": -1}, None),
    ("non-dict-chunk", {"chunkIndex": 4}, None),
    ("missing-chunks", {}, "no-chunks"),
    ("chunks-not-list", {}, "chunks-scalar"),
    ("project-scoped", {"chunkIndex": 1, "projectId": "abcd"}, "project"),
    ("bad-project-id", {"chunkIndex": 1, "projectId": "ab"}, None),
]


def _extract(source: str, name: str) -> str:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            if node.decorator_list:
                decorators = "\n".join(
                    "@" + ast.unparse(decorator) for decorator in node.decorator_list
                )
                return decorators + "\n" + segment
            return segment
    raise SystemExit(f"could not extract {name}")


def build_namespace(root: Path) -> dict:
    # The projects store, from its own probe.
    namespace = projects_probe.build_namespace(root)

    import functools
    import re
    from pathlib import Path as _Path
    from typing import Any

    file_cache_dir = root / ".file-cache"
    namespace.update(
        {
            "FILE_CACHE_DIR": file_cache_dir,
            "PROJECTS_DIR": root / ".projects",
            "FILE_READER_MAX_CHUNKS": 12,
            "FILE_SOURCE_SUFFIX": ".source",
            "functools": functools,
            "lru_cache": functools.lru_cache,
            "_Path": _Path,
            "Any": Any,
            "re": re,
        }
    )

    files_source = FILES.read_text(encoding="utf-8")
    for name in FILE_FUNCTIONS:
        exec(compile(_extract(files_source, name), str(FILES), "exec"), namespace)  # noqa: S102

    tools_source = TOOLS.read_text(encoding="utf-8")
    for name in TOOL_FUNCTIONS:
        exec(compile(_extract(tools_source, name), str(TOOLS), "exec"), namespace)  # noqa: S102
    return namespace


def outcome(call) -> dict:
    try:
        return {"ok": True, "result": call()}
    except Exception as exc:  # noqa: BLE001 - the probe reports the shape
        return {
            "ok": False,
            "error": str(exc),
            "code": getattr(getattr(exc, "code", None), "value", None),
            "status": getattr(exc, "status", None),
        }


def main() -> int:
    for path in (FILES, TOOLS, ERRORS):
        if not path.exists():
            print(f"missing {path}", file=sys.stderr)
            return 2

    root = Path(tempfile.mkdtemp(prefix="file-cache-parity-"))
    out: dict = {}
    try:
        ns = build_namespace(root)
        cache_dir = ns["FILE_CACHE_DIR"]
        project_files = ns["PROJECTS_DIR"] / "abcd" / "files"

        def write_index(directory: Path, body) -> None:
            directory.mkdir(parents=True, exist_ok=True)
            text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=2)
            (directory / f"{GOOD_ID}.json").write_text(text, encoding="utf-8")

        # --- load_cached_file error shapes ------------------------------------
        for label, file_id, _ in LOAD_CASES:
            out[f"load::{label}"] = outcome(lambda file_id=file_id: ns["load_cached_file"](file_id))

        out["load::missing"] = outcome(lambda: ns["load_cached_file"](GOOD_ID))

        write_index(cache_dir, "{not json")
        out["load::malformed"] = outcome(lambda: ns["load_cached_file"](GOOD_ID))
        write_index(cache_dir, "42")
        out["load::scalar"] = outcome(lambda: ns["load_cached_file"](GOOD_ID))

        # --- the project-scoped path ------------------------------------------
        write_index(project_files, INDEX_BODY)
        out["load::global-cannot-see-project"] = outcome(lambda: ns["load_cached_file"](GOOD_ID))
        out["load::scoped"] = outcome(lambda: ns["load_cached_file"](GOOD_ID, project_id="abcd"))
        out["load::bad-project-id"] = outcome(
            lambda: ns["load_cached_file"](GOOD_ID, project_id="ab")
        )
        out["load::blank-project-id-is-global"] = outcome(
            lambda: ns["load_cached_file"](GOOD_ID, project_id="   ")
        )
        out["cache-dir::project"] = outcome(
            lambda: str(ns["project_file_cache_dir"]("abcd").relative_to(root))
        )

        # --- read_file_chunk_tool ---------------------------------------------
        def serve(body=INDEX_BODY, scoped=False) -> None:
            target = project_files if scoped else cache_dir
            write_index(target, body)

        for label, arguments, variant in CHUNK_CASES:
            body = INDEX_BODY
            scoped = False
            if variant == "no-chunks":
                body = {"id": "f", "name": "n", "kind": "text"}
            elif variant == "chunks-scalar":
                body = {"id": "f", "name": "n", "kind": "text", "chunks": "no"}
            elif variant == "project":
                scoped = True
            serve(body, scoped=scoped)
            if scoped:
                # The global copy must not exist, so only the project path can serve.
                (cache_dir / f"{GOOD_ID}.json").unlink(missing_ok=True)
            else:
                write_index(cache_dir, body)
                (project_files / f"{GOOD_ID}.json").unlink(missing_ok=True)
            out[f"chunk::{label}"] = outcome(
                lambda arguments=arguments: ns["read_file_chunk_tool"](
                    str(arguments.get("fileId") or GOOD_ID),
                    chunk_index=int(arguments.get("chunkIndex") or 0),
                    project_id=str(arguments.get("projectId") or ""),
                )
            )

        # --- list_project_files_tool ------------------------------------------
        def write_project(project_id: str, body: dict) -> None:
            target = ns["PROJECTS_DIR"] / project_id
            target.mkdir(parents=True, exist_ok=True)
            (target / "project.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        documents = [
            {"fileId": "b" * 32, "projectId": "abcd", "name": "A", "kind": "pdf",
             "pageCount": 2, "charCount": 30, "chunkCount": 4, "preview": "p" * 700},
            {"fileId": "c" * 32, "projectId": "abcd", "name": "B"},
            {"fileId": "bad", "projectId": "abcd"},
            "not-a-dict",
        ] + [
            {"fileId": f"{index:032x}", "projectId": "abcd", "name": f"D{index}"}
            for index in range(200)
        ]
        write_project("abcd", {"name": "Alpha", "documents": documents, "updatedAt": 5})
        write_project("efgh", {"name": "Beta", "documents": [], "updatedAt": 9})

        out["list::named"] = outcome(lambda: ns["list_project_files_tool"]("abcd"))
        out["list::missing"] = outcome(lambda: ns["list_project_files_tool"]("zzzz"))
        out["list::invalid-id"] = outcome(lambda: ns["list_project_files_tool"]("ab"))
        listed = outcome(lambda: ns["list_project_files_tool"](""))
        # The full payload is large; compare the shape and the counts.
        if listed.get("ok"):
            payload = listed["result"]
            out["list::all"] = {
                "ok": True,
                "ids": [project["id"] for project in payload["projects"]],
                "count": payload["count"],
                "per-project": [len(project["files"]) for project in payload["projects"]],
                "first-file": next(
                    (project["files"][0] for project in payload["projects"] if project["files"]),
                    None,
                ),
            }
        else:
            out["list::all"] = listed
    finally:
        shutil.rmtree(root, ignore_errors=True)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
