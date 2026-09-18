"""Memory-store parity probe, Python side.

Runs the real `data/memory.py` against a scratch root and prints canonical JSON so
the Rust port can be diffed byte-for-byte.

Extracted verbatim: the normalization/fingerprint/category/sensitive/conflict
helpers, `build_memory_suggestion`, `retrieve_memories`, `delete_memories_by_query`,
the load/save path, and from `tools.py` the `memory_tool_scopes`,
`recall_memory_tool` and `forget_memory_tool` wrappers. The `suggest_memory` branch
is inline in `execute_tool_call`, so it is reproduced here in the same shape.

Pinned: `utc_now_iso` (frozen) so a store write is reproducible, and `local_rag`
(absent) so `retrieve_memories` takes the oracle's own `except Exception` path — the
vector bonus is not ported, which is recorded in `docs/MEMORY_STORE.md`.

Usage::

    python tasks/native-runtime/memory_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example memory_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_gate_parity_probe as gate_probe  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MEMORY = REPO / "deepseek_infra" / "infra" / "data" / "memory.py"
TOOLS = REPO / "deepseek_infra" / "infra" / "tool_runtime" / "tools.py"
UTILS = REPO / "deepseek_infra" / "core" / "utils.py"
ERRORS = REPO / "deepseek_infra" / "core" / "errors.py"

MEMORY_FUNCTIONS = (
    "normalize_memory_text",
    "normalize_memory_scope",
    "memory_fingerprint",
    "is_sensitive_memory",
    "infer_memory_category",
    "normalize_memory_category",
    "memory_conflict_key",
    "detect_memory_conflicts",
    "build_memory_suggestion",
    "load_memories",
    "_load_memories_unlocked",
    "save_memories",
    "_save_memories_unlocked",
    "memory_file_lock",
    "delete_memories_by_query",
    "is_memory_broad_query",
    "retrieve_memories",
    "empty_memory_state",
    "memory_scope_from_payload",
    "memory_scope_candidates",
    "memory_scope_label",
    "format_memory_context",
    "upsert_memory",
    "clear_memories",
    "delete_memory_by_id",
    "apply_explicit_memory_command",
    "prepare_memory_state",
)
TOOLS_FUNCTIONS = ("memory_tool_scopes", "recall_memory_tool", "forget_memory_tool")
UTILS_FUNCTIONS = ("query_tokens", "score_chunk", "utc_now_iso", "latest_user_query")

SCOPE_CASES = [
    "global", "  global  ", "project:abc", "seek:SEARCH-1", "skill:pack.v1",
    "automation:job_2", "project:", "project:a b", "unknown:x", "global:x",
    "PROJECT:abc", "", "   ",
]

TEXT_CASES = ["  a   b\tc  ", "", "   ", "x" * 2000]

CATEGORY_CASES = [
    ("我喜欢简洁", None), ("项目 代码", None), ("待办 计划", None),
    ("the sky is blue", None), ("我喜欢简洁", "FACT"), ("x", "preference"),
    ("x", "  project  "), ("x", "nope"),
]

CONFLICT_CASES = [
    ("我用 React", "preference"), ("请一步一步来", "preference"),
    ("用中文回答", "preference"), ("叫我 leizd", "preference"),
    ("我喜欢深色主题", "preference"), ("我喜欢猫", "preference"),
    ("项目：DeepSeek-Infra", "project"), ("事实", "fact"),
]

SENSITIVE_CASES = [
    "my api key is x", "APIKEY", "the token", "secret", "password", "密码",
    "密钥", "银行卡", "身份证", "验证码", "授权码", "PASSWORD",
    "I like concise answers",
]

TOOL_SCOPE_CASES = [
    ("", "global"), ("", "project:abc"), ("global", "project:abc"),
    ("seek:s1", "project:abc"), ("bogus", "project:abc"), ("", ""),
]

STORE_FIXTURE: list[dict[str, Any]] = [
    {"id": "m-pref", "content": "我用 React 做前端", "category": "preference", "scope": "global",
     "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-05T00:00:00+00:00"},
    {"id": "m-pinned", "content": "重要：我用 React", "category": "fact", "scope": "global",
     "pinned": True, "createdAt": "2026-01-01T00:00:00+00:00",
     "updatedAt": "2026-01-02T00:00:00+00:00"},
    {"id": "m-project", "content": "项目用 Rust 写", "category": "project", "scope": "project:abc",
     "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-03T00:00:00+00:00"},
    {"id": "m-unrelated", "content": "the sky is blue", "category": "fact", "scope": "global",
     "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-01T00:00:00+00:00"},
]

RECALL_CASES = [
    ("match", {"query": "React"}, "global"),
    ("blank-query", {"query": "   "}, "global"),
    ("broad", {"query": "你记得什么"}, "global"),
    ("no-match", {"query": "zzz-absent"}, "global"),
    ("project-scope", {"query": "Rust"}, "project:abc"),
    ("explicit-global", {"query": "Rust", "scope": "global"}, "project:abc"),
    ("explicit-scope", {"query": "Rust", "scope": "project:abc"}, "global"),
]

FORGET_CASES = [
    ("blank", {"query": "   "}),
    ("absent", {"query": "zzz-absent"}),
]

SUGGEST_CASES = [
    ("plain", {"content": "  我喜欢简洁的回答  "}, "global"),
    ("sensitive", {"content": "my password is hunter2"}, "global"),
    ("empty", {"content": "   "}, "global"),
    ("project", {"content": "项目用 Rust 写", "scope": "project:abc"}, "global"),
    ("bad-scope", {"content": "facts", "scope": "bogus"}, "global"),
    ("category", {"content": "whatever", "category": "PREFERENCE"}, "global"),
]

FROZEN_EPOCH = 1_760_000_000


def _extract(source: str, name: str) -> str:
    """Extract a function with its decorators (they are separate AST nodes)."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            if segment is None:
                raise SystemExit(f"could not extract {name}")
            if node.decorator_list:
                decorators = "\n".join(
                    "@" + ast.unparse(decorator) for decorator in node.decorator_list
                )
                return decorators + "\n" + segment
            return segment
    raise SystemExit(f"could not extract {name}")


def build_namespace(root: Path) -> dict:
    namespace: dict = {}
    exec(compile(ERRORS.read_text(encoding="utf-8"), str(ERRORS), "exec"), namespace)  # noqa: S102

    import contextlib
    import datetime as datetime_module
    import hashlib
    import os
    import re
    import threading
    from datetime import datetime, timezone
    from pathlib import Path as _Path
    from typing import Any, Iterator

    memory_dir = root / ".memory"
    memory_file = memory_dir / "memories.json"
    gate = gate_probe.build_namespace(root)

    # `utc_now_iso` frozen, so a store write is reproducible.
    def utc_now_iso() -> str:
        return datetime_module.datetime.fromtimestamp(
            FROZEN_EPOCH, tz=datetime_module.timezone.utc
        ).isoformat(timespec="seconds")

    namespace.update(
        {
            "MEMORY_DIR": memory_dir,
            "MEMORY_FILE": memory_file,
            "MEMORY_MAX_ITEMS": 400,
            "MEMORY_RETRIEVE_LIMIT": 12,
            "MEMORY_CONTEXT_CHAR_BUDGET": 8000,
            "mutation_gate": type(
                "mutation_gate",
                (),
                {"mutation_scope": staticmethod(gate["mutation_scope"])},
            ),
            "json": json,
            "os": os,
            "re": re,
            "hashlib": hashlib,
            "threading": threading,
            "datetime_module": datetime_module,
            "datetime": datetime,
            "timezone": timezone,
            "contextmanager": contextlib.contextmanager,
            "Path": _Path,
            "Any": Any,
            "Iterator": Iterator,
            "_memory_lock": threading.RLock(),
            "utc_now_iso": utc_now_iso,
        }
    )

    memory_source = MEMORY.read_text(encoding="utf-8")
    for name in MEMORY_FUNCTIONS:
        exec(compile(_extract(memory_source, name), str(MEMORY), "exec"), namespace)  # noqa: S102

    # The scorer the memory module imports from `core.utils`.
    utils_source = UTILS.read_text(encoding="utf-8")
    for name in UTILS_FUNCTIONS:
        if name == "utc_now_iso":
            continue
        exec(compile(_extract(utils_source, name), str(UTILS), "exec"), namespace)  # noqa: S102

    tools_source = TOOLS.read_text(encoding="utf-8")
    for name in TOOLS_FUNCTIONS:
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
        }


def main() -> int:
    for path in (MEMORY, TOOLS, UTILS, ERRORS):
        if not path.exists():
            print(f"missing {path}", file=sys.stderr)
            return 2

    root = Path(tempfile.mkdtemp(prefix="memory-parity-"))
    out: dict = {}
    try:
        ns = build_namespace(root)
        store = ns["MEMORY_FILE"]

        def write_raw(raw: str) -> None:
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(raw, encoding="utf-8")

        def write_fixture(entries: list) -> None:
            write_raw(json.dumps(entries, ensure_ascii=False, indent=2))

        # --- pure helpers -----------------------------------------------------
        for index, value in enumerate(TEXT_CASES):
            out[f"text::{index}"] = ns["normalize_memory_text"](value)
        for value in SCOPE_CASES:
            out[f"scope::normalize::{json.dumps(value, ensure_ascii=False)}"] = ns["normalize_memory_scope"](value)
        for label, content in [
            ("plain", "Hello"), ("case", "hello"), ("spaces", "  a   b  "),
        ]:
            out[f"fingerprint::{label}"] = ns["memory_fingerprint"](content, "global")
        out["fingerprint::scoped"] = ns["memory_fingerprint"]("Hello", "project:abc")
        out["fingerprint::distinct"] = ns["memory_fingerprint"]("x", "project:a")
        for content in SENSITIVE_CASES:
            out[f"sensitive::{json.dumps(content, ensure_ascii=False)}"] = ns["is_sensitive_memory"](content)
        for index, (content, category) in enumerate(CATEGORY_CASES):
            out[f"category::{index}"] = ns["normalize_memory_category"](category, content)
        for content, category in CONFLICT_CASES:
            out[f"conflict::{json.dumps(content, ensure_ascii=False)}"] = ns["memory_conflict_key"](content, category)

        # --- tool scopes ------------------------------------------------------
        for index, (scope, default) in enumerate(TOOL_SCOPE_CASES):
            out[f"tool-scopes::{index}"] = ns["memory_tool_scopes"](scope, default)

        # --- suggest ----------------------------------------------------------
        def suggest_branch(payload: dict, default_scope: str) -> dict:
            scope_argument = payload.get("scope")
            scope = (
                scope_argument
                if isinstance(scope_argument, str) and scope_argument
                else default_scope
            )
            scope = ns["normalize_memory_scope"](scope)
            return ns["build_memory_suggestion"](
                str(payload.get("content") or ""),
                category=str(payload.get("category") or ""),
                scope=scope,
            )

        for label, payload, default_scope in SUGGEST_CASES:
            out[f"suggest::{label}"] = outcome(
                lambda payload=payload, default_scope=default_scope: suggest_branch(
                    payload, default_scope
                )
            )

        # --- the store --------------------------------------------------------
        write_fixture(STORE_FIXTURE)
        out["store::loaded"] = ns["load_memories"]()
        out["store::file"] = store.read_text(encoding="utf-8")

        # A save migrates in place: non-objects, empty content, out-of-range
        # confidence, invalid scopes.
        saved = [
            "not an object",
            {"content": "   "},
            {"content": "survivor", "confidence": 5, "category": "PREFERENCE", "scope": "bogus"},
            {"content": "bad confidence", "confidence": "nope"},
            {"content": 0},
            {"content": True},
            {"content": False},
        ]
        ns["save_memories"](saved)
        out["store::migrated"] = store.read_text(encoding="utf-8")

        for label, raw in [
            ("not-json", "{not json"), ("scalar", "42"), ("object", "{}"),
            ("empty-list", "[]"),
            ("mixed", '[{"id": "a"}, "x", 7, null, {"id": "b"}]'),
        ]:
            write_raw(raw)
            out[f"read::{label}"] = ns["load_memories"]()

        # --- recall -----------------------------------------------------------
        write_fixture(STORE_FIXTURE)
        for label, payload, default_scope in RECALL_CASES:
            args = dict(payload)
            # `recall_memory_tool` cannot raise — `retrieve_memories` swallows RAG
            # failures — so it is recorded unwrapped, matching the Rust side.
            out[f"recall::{label}"] = ns["recall_memory_tool"](
                str(args.get("query") or ""),
                scope=str(args.get("scope") or ""),
                default_scope=default_scope,
            )

        # --- forget -----------------------------------------------------------
        for label, payload in FORGET_CASES:
            write_fixture(STORE_FIXTURE)
            out[f"forget::{label}"] = outcome(
                lambda payload=payload: ns["forget_memory_tool"](
                    str(payload.get("query") or ""), default_scope="global"
                )
            )
        write_fixture(STORE_FIXTURE)
        out["forget::react-global"] = outcome(
            lambda: ns["forget_memory_tool"]("react", default_scope="global")
        )
        out["forget::after"] = ns["load_memories"]()
        write_fixture(STORE_FIXTURE)
        out["forget::project-default"] = outcome(
            lambda: ns["forget_memory_tool"]("React", default_scope="project:abc")
        )
        out["forget::project-after"] = [
            item.get("id") for item in ns["load_memories"]()
        ]

        # --- delete semantics -------------------------------------------------
        write_fixture(STORE_FIXTURE)
        out["delete::no-match"] = outcome(
            lambda: ns["delete_memories_by_query"]("zzz-absent", scopes=["global"])
        )
        out["delete::no-write-generation"] = (
            (root / ".workspace-generation").read_text(encoding="ascii")
            if (root / ".workspace-generation").exists() else None
        )
        out["delete::match"] = outcome(
            lambda: ns["delete_memories_by_query"]("react", scopes=["global"])
        )
        out["delete::generation"] = (
            (root / ".workspace-generation").read_text(encoding="ascii")
            if (root / ".workspace-generation").exists() else None
        )

        # --- conflicts --------------------------------------------------------
        write_fixture(STORE_FIXTURE)
        out["conflicts::same-domain"] = ns["detect_memory_conflicts"](
            "我用 Vue", category="preference", scope="global"
        )
        out["conflicts::identical"] = ns["detect_memory_conflicts"](
            "我用 React 做前端", category="preference", scope="global"
        )
        out["conflicts::no-key"] = ns["detect_memory_conflicts"](
            "the sky is blue", category="fact", scope="global"
        )
        out["lock::exists"] = ns["MEMORY_DIR"].joinpath("memories.lock").exists()

        # --- scope candidates / labels (the turn state's read half) ------------
        CANDIDATE_CASES: list[dict[str, Any]] = [
            {},
            {"messages": []},
            {"memoryScope": "project:abc"},
            {"memoryScope": "bogus"},
            {"memoryScope": 0},
            {"messages": [{"role": "user", "projectId": "p1"}]},
            {"messages": [{"role": "user", "seekId": "s1"}]},
            {"messages": [
                {"role": "assistant", "projectId": "p9"},
                {"role": "user", "content": "hi"},
            ]},
            {"memoryScope": "seek:s2", "messages": [{"role": "user", "projectId": "p1"}]},
        ]
        for index, payload in enumerate(CANDIDATE_CASES):
            out[f"candidates::{index}"] = ns["memory_scope_candidates"](payload)
            out[f"scope-of::{index}"] = ns["memory_scope_from_payload"](payload)
        for value in ["global", "project:abc", "seek:SEARCH-1", "skill:pack.v1", "bogus", "project:a:b:c", ""]:
            out[f"label::{json.dumps(value, ensure_ascii=False)}"] = ns["memory_scope_label"](value)

        # --- format context ----------------------------------------------------
        # The budget corpus must be able to fail: `normalize_memory_text` caps a
        # row at 1200 chars, so reaching the 8000 budget takes six full rows
        # (used = 7254) plus a 737-char row that lands exactly on 8000 and is
        # kept, with the row after it crossing into the 省略 marker.
        CONTEXT_CASES = [
            ("empty", []),
            ("fixture", STORE_FIXTURE),
            ("budget", [
                {"id": "c-1", "content": "戌" * 1200, "category": "fact", "scope": "global"},
                {"id": "c-2", "content": "戌" * 1200, "category": "fact", "scope": "global"},
                {"id": "c-3", "content": "戌" * 1200, "category": "fact", "scope": "global"},
                {"id": "c-4", "content": "戌" * 1200, "category": "fact", "scope": "global"},
                {"id": "c-5", "content": "戌" * 1200, "category": "fact", "scope": "global"},
                {"id": "c-6", "content": "戌" * 1200, "category": "fact", "scope": "global"},
                {"id": "c-7", "content": "辰" * 737, "category": "fact", "scope": "global"},
                {"id": "c-8", "content": "丁" * 100, "category": "fact", "scope": "global"},
            ]),
            ("boundary", [
                {"id": "c-first", "content": "戌" * 1200, "category": "fact", "scope": "global"},
                {"id": "c-second", "content": "y", "category": "fact", "scope": "global"},
            ]),
            ("falsy-rows", [
                {"id": "c-blank", "content": "   ", "category": "fact", "scope": "global"},
                {"id": "c-zero", "content": 0, "category": "fact", "scope": "global"},
                {"id": "c-false", "content": False, "category": "fact", "scope": "global"},
                {"id": "c-true", "content": True, "category": "fact", "scope": "global"},
                {"id": "c-real", "content": "真实记忆", "category": "fact", "scope": "global"},
            ]),
            ("scoped", [
                {"id": "c-scope", "content": "项目记忆", "category": "project", "scope": "project:abc"},
                {"id": "c-global", "content": "全局记忆", "category": "fact", "scope": "global"},
            ]),
            ("no-category", [{"id": "c-nocat", "content": "no category", "scope": "global"}]),
            ("category-number", [{"id": "c-numcat", "content": "x", "category": 7, "scope": "global"}]),
        ]
        for label, rows in CONTEXT_CASES:
            out[f"context::{label}"] = ns["format_memory_context"](rows)

        # --- upsert --------------------------------------------------------------
        # A row whose id is the fingerprint of its content, so the update path runs.
        update_id = ns["memory_fingerprint"]("我用 React 做前端", "global")
        update_fixture: list[dict[str, Any]] = [dict(row) for row in STORE_FIXTURE]
        for row in update_fixture:
            if row["id"] == "m-pref":
                row["id"] = update_id
        pinned_id = ns["memory_fingerprint"]("固定内容", "global")
        pinned_fixture = [
            {"id": pinned_id, "content": "固定内容", "category": "fact", "scope": "global",
             "pinned": True, "createdAt": "2026-01-01T00:00:00+00:00",
             "updatedAt": "2026-01-01T00:00:00+00:00"},
        ]
        UPSELL_CASES = [
            ("new", "新的一条：项目用 pnpm", None, "global", "manual", False, None, STORE_FIXTURE),
            ("update", "我用 React 做前端", None, "global", "manual", False, None, update_fixture),
            ("sensitive", "my password is hunter2", None, "global", "manual", False, None, STORE_FIXTURE),
            ("empty", "   ", None, "global", "manual", False, None, STORE_FIXTURE),
            ("replace", "全新的内容", None, "global", "manual", False, ["m-pref"], STORE_FIXTURE),
            ("scoped", "项目事实", None, "project:abc", "manual", False, None, STORE_FIXTURE),
            ("category-source", "whatever", "todo", "global", "agent", False, None, STORE_FIXTURE),
            ("pinned-merge", "固定内容", None, "global", "manual", False, None, pinned_fixture),
        ]
        for label, content, category, scope, source, pinned, replace, fixture in UPSELL_CASES:
            write_fixture(fixture)
            out[f"upsert::{label}"] = outcome(
                lambda content=content, category=category, scope=scope, source=source,
                pinned=pinned, replace=replace: ns["upsert_memory"](
                    content, category=category, scope=scope, source=source,
                    pinned=pinned, replace_ids=replace,
                )
            )
            out[f"upsert::{label}::file"] = store.read_text(encoding="utf-8")

        # --- clear / delete-by-id ------------------------------------------------
        write_fixture(STORE_FIXTURE)
        out["clear::count"] = outcome(lambda: ns["clear_memories"]())
        out["clear::file"] = store.read_text(encoding="utf-8")
        write_fixture(STORE_FIXTURE)
        out["by-id::hit"] = outcome(lambda: ns["delete_memory_by_id"]("m-pref"))
        out["by-id::hit-file"] = store.read_text(encoding="utf-8")
        out["by-id::miss"] = outcome(lambda: ns["delete_memory_by_id"]("absent-id"))
        out["by-id::miss-file"] = store.read_text(encoding="utf-8")

        # --- explicit commands ----------------------------------------------------
        def command(query, scope="global", scopes=None):
            return ns["apply_explicit_memory_command"](query, scope=scope, scopes=scopes)

        COMMAND_CASES = [
            ("remember", "请帮我记住: 我的生日是 3 月 5 日", "global", None),
            ("remember-en", "Don't forget: the alignment review", "global", None),
            ("negated-forget", "不要忘记: 牙医预约", "global", None),
            ("forget", "忘记: React", "global", ["global"]),
            ("delete-memory", "删除记忆: 生日", "global", ["global"]),
            ("forget-upper", "FORGET: react", "global", ["global"]),
            ("opt-out", "不要记住: 这是临时的", "global", None),
            ("opt-out-en", "don't remember: this", "global", None),
            ("bare-remember-gap", "记住: 我的生日", "global", None),
            ("sensitive", "请帮我记住: my api key", "global", None),
            ("multiline", "请帮我记住: 第一行\n第二行", "global", None),
            ("kept-scoped", "不要忘记: 项目笔记", "project:abc", None),
            ("forget-scoped", "忘记: Rust", "global", ["project:abc"]),
            ("plain", "今天天气怎么样", "global", None),
        ]
        for label, query, scope, scopes in COMMAND_CASES:
            write_fixture(STORE_FIXTURE)
            out[f"command::{label}"] = outcome(
                lambda query=query, scope=scope, scopes=scopes: command(query, scope=scope, scopes=scopes)
            )
            out[f"command::{label}::file"] = store.read_text(encoding="utf-8")

        # --- the turn state -------------------------------------------------------
        STATE_CASES: list[tuple[str, dict[str, Any]]] = [
            ("disabled", {"memoryEnabled": False, "messages": [
                {"role": "user", "content": "请帮我记住: 这条不该保存"},
            ]}),
            ("falsy-enabled", {"memoryEnabled": 0, "messages": [
                {"role": "user", "content": "React 怎么用"},
            ]}),
            ("plain", {"messages": [{"role": "user", "content": "React 怎么用"}]}),
            ("remember", {"messages": [
                {"role": "user", "content": "请帮我记住: 我喜欢深色主题"},
            ]}),
            ("sensitive", {"messages": [
                {"role": "user", "content": "请帮我记住: my password is 123"},
            ]}),
            ("scoped", {"messages": [
                {"role": "user", "projectId": "abc", "content": "Rust"},
            ]}),
            ("no-messages", {}),
            ("explicit-scope", {"memoryScope": "seek:s1"}),
            ("broad", {"messages": [{"role": "user", "content": "你记得什么"}]}),
        ]
        for label, payload in STATE_CASES:
            write_fixture(STORE_FIXTURE)
            out[f"state::{label}"] = ns["prepare_memory_state"](payload)
            out[f"state::{label}::file"] = store.read_text(encoding="utf-8")
        out["state::generation"] = (
            (root / ".workspace-generation").read_text(encoding="ascii")
            if (root / ".workspace-generation").exists() else None
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
