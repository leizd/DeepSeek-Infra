"""Reminders parity probe, Python side.

Runs the real `reminders.py` against a scratch root and prints canonical JSON so
the Rust port can be diffed byte-for-byte.

The two non-deterministic inputs are pinned: `secrets.token_hex` becomes a
counter and `time.time` a fixed value, so ids and `createdAt` are comparable. The
oracle reads both as module globals, so patching the extracted namespace is enough.

`mutation_gate` is the **real** module (its own probe already verifies it), pointed
at the same scratch root. That makes the composite faithful — the write really goes
through the fence — and gives us `store::generation` as evidence that it did.

Usage::

    python tasks/native-runtime/reminders_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example reminders_parity_probe > ../rust.json
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
import mutation_gate_parity_probe as gate_probe  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "deepseek_infra" / "infra" / "data" / "reminders.py"
ERRORS = REPO / "deepseek_infra" / "core" / "errors.py"

FUNCTIONS = (
    "load_reminders",
    "create_reminder",
    "due_reminders",
    "delete_reminder",
    "parse_due_at",
    "parse_natural_reminder",
    "_read_reminders",
    "_write_reminders",
)

# The corpus is chosen to probe `datetime.fromisoformat`, which is the one piece
# here with real edge cases.
DUE_AT_CASES: list[tuple[str, object]] = [
    ("zulu", "2026-09-15T10:30:00Z"),
    ("offset", "2026-09-15T10:30:00+08:00"),
    ("naive", "2026-09-15T10:30:00"),
    ("no-seconds", "2026-09-15T10:30"),
    ("date-only", "2026-09-15"),
    ("microseconds", "2026-09-15T10:30:00.123456"),
    ("compact-offset", "2026-09-15T10:30:00+0000"),
    ("lowercase-z", "2026-09-15t10:30:00z"),
    ("already-utc", "2026-09-15T10:30:00+00:00"),
    ("negative-offset", "2026-09-15T10:30:00-05:00"),
    ("whitespace", "  2026-09-15T10:30:00Z  "),
    ("empty", ""),
    ("none", None),
    ("garbage", "not a date"),
    ("compact-digits", "20260915"),
    ("week-format", "2026-W37-1"),
    # Pinning the parser's edges before implementing it.
    ("lowercase-t", "2026-09-15t10:30:00"),
    ("uppercase-t-lowercase-z", "2026-09-15T10:30:00z"),
    ("space-separator", "2026-09-15 10:30:00"),
    ("hour-only", "2026-09-15T10"),
    ("fraction-1-digit", "2026-09-15T10:30:00.1"),
    ("compact-time", "20260915T103000"),
    ("invalid-hour", "2026-09-15T25:00:00"),
    ("invalid-date", "2026-02-30"),
    ("offset-no-colon", "2026-09-15T10:30:00+0800"),
    ("z-with-fraction", "2026-09-15T10:30:00.5Z"),
    ("offset-hours-only", "2026-09-15T10:30:00+08"),
    # ISO week years: 2026 has 53 weeks, 2025 has 52, and week 1 can start in
    # December of the previous year.
    ("week-2026-w01", "2026-W01-1"),
    ("week-2026-w53", "2026-W53-1"),
    ("week-2025-w53", "2025-W53-1"),
    ("week-2020-w53", "2020-W53-1"),
    ("week-w00", "2026-W00-1"),
    ("week-w54", "2026-W54-1"),
    ("leap-day", "2024-02-29"),
]

CREATE_CASES: list[tuple[str, dict]] = [
    ("full", {"title": "Stand up", "content": "stretch", "dueAt": "2026-09-15T10:30:00Z"}),
    ("blank-title", {"title": "   ", "content": "c", "dueAt": "2026-09-15T10:30:00Z"}),
    ("missing-title", {"content": "c", "dueAt": "2026-09-15T10:30:00Z"}),
    ("snake-case-due", {"title": "t", "content": "c", "due_at": "2026-09-15T10:30:00Z"}),
    ("long-title", {"title": "x" * 200, "content": "c", "dueAt": "2026-09-15T10:30:00Z"}),
    ("long-content", {"title": "t", "content": "y" * 3000, "dueAt": "2026-09-15T10:30:00Z"}),
    ("no-due", {"title": "t", "content": "c"}),
    ("non-string", {"title": 42, "content": None, "dueAt": "2026-09-15T10:30:00Z"}),
]

STATUS_CASES: list[tuple[str, object]] = [
    ("active", "active"),
    ("notified", "notified"),
    ("all", "all"),
    ("bogus", "bogus"),
    ("mixed-case", "  ACTIVE  "),
    ("empty", ""),
    ("none", None),
    ("non-string", 7),
]

READ_CASES: list[tuple[str, str]] = [
    ("not-json", "{not json"),
    ("scalar", "42"),
    ("object", "{}"),
    ("empty-list", "[]"),
    ("mixed-items", '[{"id": "a"}, "x", 7, null, {"id": "b"}]'),
]


def build_namespace(root: Path) -> dict:
    namespace: dict = {}
    exec(compile(ERRORS.read_text(encoding="utf-8"), str(ERRORS), "exec"), namespace)  # noqa: S102

    import datetime as datetime_module
    import re
    import secrets
    import threading
    import time
    from datetime import datetime, timedelta, timezone
    from pathlib import Path as _Path
    from typing import Any

    reminders_dir = root / ".reminders"
    reminders_file = reminders_dir / "reminders.json"

    # The real gate, pointed at the same root.
    gate = gate_probe.build_namespace(root)

    class _Config:
        REMINDERS_DIR = reminders_dir
        REMINDERS_FILE = reminders_file

    namespace.update(
        {
            "config": _Config,
            "REMINDERS_DIR": reminders_dir,
            "REMINDERS_FILE": reminders_file,
            "mutation_gate": type(
                "mutation_gate",
                (),
                {"mutation_scope": staticmethod(gate["mutation_scope"])},
            ),
            "json": json,
            "re": re,
            "secrets": secrets,
            "threading": threading,
            "time": time,
            "datetime": datetime,
            "timedelta": timedelta,
            "timezone": timezone,
            "datetime_module": datetime_module,
            "Path": _Path,
            "Any": Any,
            "_LOCK": threading.RLock(),
            "MAX_REMINDERS": 200,
        }
    )

    source = MODULE.read_text(encoding="utf-8")
    for name in FUNCTIONS:
        segment = next(
            (
                ast.get_source_segment(source, node)
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.FunctionDef) and node.name == name
            ),
            None,
        )
        if segment is None:
            raise SystemExit(f"could not extract {name}")
        exec(compile(segment, str(MODULE), "exec"), namespace)  # noqa: S102

    # --- pin the non-deterministic inputs -----------------------------------
    counter = {"n": 0}

    class _FixedSecrets:
        @staticmethod
        def token_hex(width: int) -> str:
            # `secrets.token_hex(8)` -> 16 lowercase hex characters.
            counter["n"] += 1
            return f"{counter['n']:016x}"

    class _FixedTime:
        FIXED = 1_760_000_000.0

        @staticmethod
        def time() -> float:
            return _FixedTime.FIXED

    namespace["secrets"] = _FixedSecrets
    namespace["time"] = _FixedTime
    namespace["_NEW_ID"] = _FixedSecrets.token_hex
    return namespace


def main() -> int:
    for path in (MODULE, ERRORS):
        if not path.exists():
            print(f"missing {path}", file=sys.stderr)
            return 2

    root = Path(tempfile.mkdtemp(prefix="reminders-parity-"))
    out: dict = {}
    try:
        ns = build_namespace(root)
        store = ns["REMINDERS_FILE"]
        parse_due_at = ns["parse_due_at"]
        create_reminder = ns["create_reminder"]
        load_reminders = ns["load_reminders"]
        AppError = ns["AppError"]

        # --- parse_due_at -----------------------------------------------------
        for label, value in DUE_AT_CASES:
            try:
                out[f"due::{label}"] = {"ok": True, "value": parse_due_at(value)}
            except AppError as exc:
                out[f"due::{label}"] = {
                    "ok": False,
                    "error": str(exc),
                    "code": exc.code.value,
                }

        # --- create ----------------------------------------------------------
        for label, payload in CREATE_CASES:
            try:
                reminder = create_reminder(dict(payload))
                out[f"create::{label}"] = {"ok": True, "reminder": reminder}
            except AppError as exc:
                out[f"create::{label}"] = {
                    "ok": False,
                    "error": str(exc),
                    "code": exc.code.value,
                }

        # After all the creates, the store on disk is the real contract: the
        # oracle's `indent=2` layout, the key order, and the sort/tail slice.
        out["store::file"] = store.read_text(encoding="utf-8")
        out["store::loaded"] = load_reminders()
        out["store::count"] = len(load_reminders())
        # Evidence that the write actually went through the fence.
        generation = root / ".workspace-generation"
        out["store::generation"] = (
            generation.read_text(encoding="ascii") if generation.exists() else None
        )
        out["store::lock-exists"] = (root / ".workspace-mutation.lock").exists()
        out["store::tmp-leftovers"] = sorted(
            entry.name for entry in (root / ".reminders").iterdir()
            if entry.name.endswith(".tmp")
        )

        # --- list_reminders_tool ---------------------------------------------
        # The wrapper lives in tools.py, so it is reproduced here against the same
        # store rather than extracted; the shape it returns is the contract.
        def list_reminders_tool(status: object = "active") -> dict:
            normalized = str(status or "active").strip().lower()
            if normalized not in {"active", "notified", "all"}:
                normalized = "active"
            reminders = load_reminders()
            if normalized == "active":
                reminders = [item for item in reminders if not bool(item.get("notified"))]
            elif normalized == "notified":
                reminders = [item for item in reminders if bool(item.get("notified"))]
            return {
                "status": normalized,
                "reminders": reminders[:50],
                "count": len(reminders),
            }

        for label, status in STATUS_CASES:
            result = list_reminders_tool(status)
            out[f"list::{label}"] = {
                "status": result["status"],
                "count": result["count"],
                "returned": len(result["reminders"]),
                "ids": [item.get("id") for item in result["reminders"]],
            }

        # With a notified entry present, the status filter matters.
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps(
                [
                    {"id": "n1", "title": "a", "content": "", "dueAt": "2026-01-01T00:00:00+00:00",
                     "createdAt": 1, "notified": True, "notifiedAt": 2},
                    {"id": "a1", "title": "b", "content": "", "dueAt": "2027-01-01T00:00:00+00:00",
                     "createdAt": 1, "notified": False},
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        for label, status in STATUS_CASES:
            result = list_reminders_tool(status)
            out[f"filtered::{label}"] = {
                "status": result["status"],
                "count": result["count"],
                "ids": [item.get("id") for item in result["reminders"]],
            }

        # --- tolerant reads ---------------------------------------------------
        for label, raw in READ_CASES:
            store.write_text(raw, encoding="utf-8")
            out[f"read::{label}"] = load_reminders()
        store.unlink()
        out["read::missing"] = load_reminders()

        # --- delete -----------------------------------------------------------
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(
            json.dumps([{"id": "keep"}, {"id": "drop"}], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        out["delete::hit"] = ns["delete_reminder"]("drop")
        out["delete::miss"] = ns["delete_reminder"]("nope")
        out["delete::blank"] = ns["delete_reminder"]("")
        out["delete::remaining"] = load_reminders()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
