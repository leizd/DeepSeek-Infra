"""Store parity probe: the budget database and the file index.

Unlike the earlier probes, this one exercises **real I/O on both sides** — each side creates
its own SQLite database and its own cache files in a temporary directory, and the outputs are
compared byte for byte. That makes three things comparable that a stub could not:

- the DDL, because SQLite stores the statement text verbatim in `sqlite_master`;
- the accumulated row, because the upsert is what makes two calls add up;
- the four failure modes of `load_cached_file`, message and code included.

`updated_at` is deliberately left out of the raw-row view: the oracle reads the wall clock
there and its value is not part of the contract the two sides can share.

Usage::

    python tasks/native-runtime/store_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example store_parity_probe > ../rust.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.gateway import budget_manager as bm  # noqa: E402
from deepseek_infra.infra.rag import files  # noqa: E402

BAD_FILE_IDS = ["A" * 32, "a" * 31, "a" * 33, "g" * 32, "", "a" * 31 + "B"]

RAW_COLUMNS = [
    "scope",
    "day",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "model_calls",
    "search_calls",
    "tool_calls",
]


def error_view(call: Callable[[], Any]) -> Any:
    try:
        return {"ok": call()}
    except AppError as exc:
        return {"error": str(exc), "code": exc.code.value, "status": exc.status}


def main() -> int:
    out: dict[str, Any] = {}
    temp_dir = Path(tempfile.mkdtemp(prefix="store-probe-"))

    # --- the budget database -----------------------------------------------------------
    saved = (bm.BUDGET_DIR, bm.BUDGET_DB, bm.BUDGET_TRACKING_ENABLED, files.FILE_CACHE_DIR, files.PROJECTS_DIR)
    try:
        bm.BUDGET_DIR = temp_dir / "budget"
        bm.BUDGET_DB = bm.BUDGET_DIR / "budget.db"
        bm.BUDGET_TRACKING_ENABLED = True

        connection = bm.connect_db()
        bm.initialize_schema(connection)
        schema_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (bm.SPEND_TABLE,)
        ).fetchone()
        out["budget::schema"] = schema_row[0]
        connection.close()
        out["budget::dir-created"] = bm.BUDGET_DIR.is_dir()

        bm.record_spend("global", prompt_tokens=10, completion_tokens=20, cost_usd=4.9e-05, model_calls=1)
        bm.record_spend("global", prompt_tokens=5, cost_usd=1e-06, model_calls=1, tool_calls=2)
        out["budget::accumulated"] = bm.daily_spend("global")
        out["budget::other-scope"] = bm.daily_spend("project-x")
        out["budget::other-day"] = bm.daily_spend("global", "2026-01-01")

        connection = bm.connect_db()
        raw = connection.execute(
            "SELECT * FROM budget_daily WHERE scope = ? AND day = ?", ("global", bm.today())
        ).fetchone()
        out["budget::raw-row"] = {name: raw[name] for name in RAW_COLUMNS}
        connection.close()

        # A fresh database directory is created on demand, so a first write to a new day and
        # scope lands in a row of its own.
        bm.record_spend("project-x", prompt_tokens=1, model_calls=1)
        out["budget::new-scope"] = bm.daily_spend("project-x")

        # --- the file index ------------------------------------------------------------
        files.FILE_CACHE_DIR = temp_dir / "cache"
        files.PROJECTS_DIR = temp_dir / "projects"
        files.FILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        file_id = "a" * 32
        document = {"id": file_id, "name": "报告.pdf", "chunks": [{"text": "片段"}]}
        (files.FILE_CACHE_DIR / f"{file_id}.json").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        out["file::ok"] = files.load_cached_file(file_id)

        for index, bad in enumerate(BAD_FILE_IDS):
            out[f"file::bad-id-{index}"] = error_view(lambda bad=bad: files.load_cached_file(bad))

        out["file::missing"] = error_view(lambda: files.load_cached_file("b" * 32))

        (files.FILE_CACHE_DIR / f"{'c' * 32}.json").write_text("not json", encoding="utf-8")
        out["file::not-json"] = error_view(lambda: files.load_cached_file("c" * 32))

        (files.FILE_CACHE_DIR / f"{'d' * 32}.json").write_text("[1, 2]", encoding="utf-8")
        out["file::not-object"] = error_view(lambda: files.load_cached_file("d" * 32))

        # A project cache lives under its own directory, and the project id is validated
        # before the path is built.
        project_files = files.PROJECTS_DIR / "proj1" / "files"
        project_files.mkdir(parents=True, exist_ok=True)
        (project_files / f"{file_id}.json").write_text(
            json.dumps({**document, "projectId": "proj1"}, ensure_ascii=False), encoding="utf-8"
        )
        out["file::project-ok"] = files.load_cached_file(file_id, "proj1")
        out["file::project-short"] = error_view(lambda: files.load_cached_file(file_id, "ab"))
        out["file::project-empty-is-global"] = files.load_cached_file(file_id, "")

        # The document as it stands now, after a rewrite: a changed file changes the memo key.
        (files.FILE_CACHE_DIR / f"{file_id}.json").write_text(
            json.dumps({**document, "name": "报告-v2.pdf"}, ensure_ascii=False), encoding="utf-8"
        )
        out["file::after-rewrite"] = files.load_cached_file(file_id)
    finally:
        (
            bm.BUDGET_DIR,
            bm.BUDGET_DB,
            bm.BUDGET_TRACKING_ENABLED,
            files.FILE_CACHE_DIR,
            files.PROJECTS_DIR,
        ) = saved

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
