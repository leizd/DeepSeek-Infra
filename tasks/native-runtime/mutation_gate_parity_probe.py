"""Mutation-gate parity probe, Python side.

Runs the real `mutation_gate` through a scripted sequence against a scratch
workspace root and prints canonical JSON, so the Rust port can be diffed against
it byte-for-byte.

The oracle module is extracted verbatim; only `deepseek_infra.core.config` is
stubbed (it exposes `ROOT` and a dozen directory settings, and pulling the real
`core.config` would drag in the whole settings tree for one attribute we set
ourselves). `workspace_root_for_path` is therefore not exercised here — it is not
on the data-layer write path, which is what this probe is about.

Everything path-dependent is reported as a basename so the two sides can use
different scratch roots.

Usage::

    python tasks/native-runtime/mutation_gate_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example mutation_gate_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "deepseek_infra" / "infra" / "workspace" / "mutation_gate.py"
ERRORS = REPO / "deepseek_infra" / "core" / "errors.py"


def _extract(source: str, name: str) -> str:
    """Extract a function with its decorators.

    `ast.get_source_segment` returns only the `def` block, so `@contextmanager`
    would be dropped and `exclusive_gate`/`mutation_scope` would come back as bare
    generators instead of context managers.
    """
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


FUNCTIONS = (
    "_root",
    "lock_path",
    "fence_path",
    "generation_path",
    "_fsync_directory",
    "_lock_file",
    "_unlock_file",
    "exclusive_gate",
    "read_fence",
    "assert_mutation_allowed",
    "write_fence",
    "clear_fence",
    "read_generation",
    "bump_generation",
    "mutation_scope",
)


def build_namespace(root: Path) -> dict:
    namespace: dict = {}
    exec(compile(ERRORS.read_text(encoding="utf-8"), str(ERRORS), "exec"), namespace)  # noqa: S102

    import contextlib
    import threading
    from pathlib import Path as _Path
    from typing import Any, Iterator

    class _Config:
        ROOT = root

    namespace.update(
        {
            "config": _Config,
            "json": json,
            "os": os,
            "threading": threading,
            "contextmanager": contextlib.contextmanager,
            "Path": _Path,
            "Any": Any,
            "Iterator": Iterator,
        }
    )
    # The module-level primitives.
    namespace["_PROCESS_LOCK"] = threading.RLock()
    namespace["_GATE_STATE"] = threading.local()

    source = MODULE.read_text(encoding="utf-8")
    for name in FUNCTIONS:
        exec(compile(_extract(source, name), str(MODULE), "exec"), namespace)  # noqa: S102
    return namespace


def main() -> int:
    if not MODULE.exists():
        print(f"missing {MODULE}", file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="gate-parity-"))
    out: dict = {}
    try:
        ns = build_namespace(root)
        gen = ns["generation_path"]()
        fence = ns["fence_path"]()
        lock = ns["lock_path"]()

        out["paths"] = {
            "lock": lock.name,
            "fence": fence.name,
            "generation": gen.name,
            "all_under_root": all(path.parent == root for path in (lock, fence, gen)),
        }

        # --- generation ------------------------------------------------------
        out["generation::initial"] = ns["read_generation"]()
        out["bump::first"] = ns["bump_generation"]()
        out["generation::file"] = gen.read_text(encoding="ascii")
        out["bump::second"] = ns["bump_generation"]()
        out["generation::file2"] = gen.read_text(encoding="ascii")

        gen.write_text("not a number", encoding="ascii")
        out["generation::unparseable"] = ns["read_generation"]()
        gen.write_text("-5", encoding="ascii")
        out["generation::negative"] = ns["read_generation"]()
        gen.write_text("7", encoding="ascii")
        out["generation::valid"] = ns["read_generation"]()
        gen.unlink()

        def leftovers() -> list[str]:
            return sorted(
                entry.name for entry in root.iterdir() if entry.name.endswith(".tmp")
            )

        out["temps::after-bumps"] = leftovers()

        # --- the fence -------------------------------------------------------
        out["fence::initial"] = ns["read_fence"]()
        out["assert::initial"] = "ok" if ns["assert_mutation_allowed"](None) is None else "raised"

        value = {"restoreId": "r1", "startedAt": "2026-09-15T00:00:00Z", "n": 1}
        ns["write_fence"](value)
        out["fence::file"] = fence.read_text(encoding="utf-8")
        out["fence::read"] = ns["read_fence"]()
        out["temps::after-fence"] = leftovers()

        def outcome(call) -> dict:
            try:
                result = call()
            except Exception as exc:  # noqa: BLE001 - the probe reports the shape
                return {
                    "raised": type(exc).__name__,
                    "message": str(exc),
                    "code": getattr(getattr(exc, "code", None), "value", None),
                    "status": getattr(exc, "status", None),
                }
            return {"raised": None, "result": result}

        out["assert::foreign"] = outcome(lambda: ns["assert_mutation_allowed"](None))
        out["assert::other-owner"] = outcome(lambda: ns["assert_mutation_allowed"]("other"))
        out["assert::owner"] = outcome(lambda: ns["assert_mutation_allowed"]("r1"))

        def enter_scope(*args) -> None:
            """`@contextmanager` is lazy: the body only runs on `__enter__`.

            Calling `mutation_scope(...)` and discarding the result would assert
            nothing and bump nothing, so the probe must actually enter it.
            """
            with ns["mutation_scope"](*args):
                pass

        # A refused scope must not bump anything.
        before = ns["read_generation"]()
        out["scope::foreign"] = outcome(enter_scope)
        out["scope::foreign-bumped"] = ns["read_generation"]() - before

        out["clear::foreign"] = outcome(lambda: ns["clear_fence"]("other"))
        out["clear::owner"] = outcome(lambda: ns["clear_fence"]("r1"))
        out["fence::after-clear"] = ns["read_fence"]()
        out["clear::absent"] = outcome(lambda: ns["clear_fence"]("r1"))

        # --- the scope -------------------------------------------------------
        before = ns["read_generation"]()
        with ns["mutation_scope"]():
            out["scope::inside-bump"] = ns["read_generation"]() - before
        out["scope::after-bump"] = ns["read_generation"]() - before

        # Nesting the same root is allowed.
        before = ns["read_generation"]()
        with ns["exclusive_gate"]():
            with ns["exclusive_gate"]():
                pass
        out["gate::nested-same-root"] = "ok"
        out["gate::nested-generation"] = ns["read_generation"]() - before

        # A different root inside an open gate is a programming error. `_GATE_STATE`
        # is a **module-level** global, so the check only fires when the same module
        # instance sees a foreign root — building a second namespace would give the
        # inner gate its own thread-local state and no error.
        other = Path(tempfile.mkdtemp(prefix="gate-parity-other-"))

        def enter_other_gate() -> None:
            with ns["exclusive_gate"](other):
                pass

        try:
            with ns["exclusive_gate"]():
                out["gate::nested-other-root"] = outcome(enter_other_gate)
        finally:
            shutil.rmtree(other, ignore_errors=True)

        # --- malformed fences ------------------------------------------------
        fence.write_text("{not json", encoding="utf-8")
        out["fence::unreadable"] = outcome(lambda: ns["read_fence"]())
        fence.write_text("42", encoding="utf-8")
        out["fence::scalar"] = ns["read_fence"]()
        fence.unlink()

        out["lock::file"] = lock.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
