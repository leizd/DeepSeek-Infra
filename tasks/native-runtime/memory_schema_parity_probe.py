"""Memory v3 schema/policy parity probe, Python side.

Covers `deepseek_infra/infra/memory/{schema,policy,store,search}.py` — the v3.0
Memory projection layer over the legacy store.

**What is compared.** The *pure* surface: `public_scope`, `storage_scope`,
`public_type`, `legacy_category`, `normalize_source_ref`, `public_source`,
`public_confidence`, `public_memory`, `assert_memory_safe`, `readable_scopes`,
`skill_can_read_memory`, and the store/search operations driven against a real
temporary workspace root.

**Why the clock is pinned.** `public_memory` calls `utc_now_iso()` itself whenever a
stored row has no `createdAt`/`updatedAt`, so two reads of the same malformed row can
disagree. The probe rebinds the module's `utc_now_iso` to a fixed instant, and the
Rust side takes the clock as a parameter; the compared values are then the same
*rendering* for the same instant. That signature difference is deliberate and is
recorded in `docs/MEMORY_SCHEMA.md`.

**Non-determinism that is masked.** `legacy_memory.upsert_memory` mints the id from
`secrets.token_hex`, so created ids differ run to run. The probe reports the id's
*shape* (length and hex-ness) rather than its value, and the id is replaced by a
placeholder everywhere it would otherwise appear.

Usage::

    $env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"
    python tasks/native-runtime/memory_schema_parity_probe.py > python.json
    cd rust; cargo run -p deepseek-policy --example memory_schema_parity_probe > ../rust.json

Both streams must be captured as **UTF-8**. On Windows a `PYTHONIOENCODING` that
resolves to the legacy code page mangles the CJK in `format_memory_context`'s output
while the Rust side emits correct UTF-8, which appears as a spurious diff on
`store::context_read` alone. Measured with the variable set: byte-identical at md5
`d0bbb07505465d8759a9d1943486ec1f`, 164 keys, 18 935 chars.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# A fixed instant, matching the Rust example's `FixedClock`.
FIXED_ISO = "2026-09-19T11:02:17+00:00"

# --- corpora ---------------------------------------------------------------------

SCOPE_CASES: list[str] = [
    "global",
    "project:p1",
    "skill:s1",
    "automation:a1",
    "seek:x",
    "project",
    "PROJECT:p1",
    "project:",
    "project:bad id",
    "project:" + "a" * 81,
    "project:" + "a" * 80,
    "unknown:x",
    "",
    "   ",
    "global:extra",
]

STORAGE_SCOPE_CASES: list[tuple[str, str, str, str, str]] = [
    ("global-none", "global", "", "", ""),
    ("project-with-id", "project", "p1", "", ""),
    ("project-no-id", "project", "", "", ""),
    ("skill-with-id", "skill", "", "s1", ""),
    ("automation-with-id", "automation", "", "", "a1"),
    ("prefixed", "project:p1", "", "", ""),
    ("unknown-family", "nonsense", "p1", "", ""),
    ("blank", "", "p1", "", ""),
    ("bad-id", "project", "bad id", "", ""),
]

TYPE_CASES: list[str] = [
    "preference",
    "fact",
    "project",
    "todo",
    "instruction",
    "summary",
    "artifact_ref",
    "PREFERENCE",
    "  fact  ",
    "",
    "nonsense",
]

CONFIDENCE_CASES: list[object] = [
    None,
    0.9,
    1.0,
    0.0,
    2.0,
    -1.0,
    0.25,
    "0.5",
    "1e-3",
    "nonsense",
    "",
    True,
    False,
    "nan",
    "inf",
    "-inf",
    10**9,
]

SOURCE_REF_CASES: list[dict] = [
    {},
    {"kind": "chat"},
    {"bad key!": "x", "ok": 1},
    {"nested": {"ok": 1, "drop me!": 2}},
    {"emptyNested": {"!!!": 1}},
    {"list": [1, None, "two"]},
    {"emptyList": [None]},
    {"nothing": None, "flag": True, "zero": 0},
    {"!!!": 1},
    {"a" * 100: "x"},
    {"deep": {"deeper": {"deepest": [1, 2, 3]}}},
    {"many": list(range(30))},
]

PUBLIC_SOURCE_CASES: list[object] = [
    None,
    {"kind": "chat"},
    {"kind": "nonsense"},
    {"kind": ""},
    {"type": "chat", "id": "m1"},
    {"refId": "r1"},
    {"savedItemId": "s1"},
    {"messageId": "msg1"},
    {"refId": "", "id": "fallback-used"},
    "chat",
    5,
    True,
    {},
]

PUBLIC_MEMORY_CASES: list[dict] = [
    {"id": "m1", "content": "hello"},
    {"id": "m1", "content": "hello", "createdAt": "2020-01-01T00:00:00+00:00"},
    {"id": "m1", "content": "hello", "updatedAt": "2021-01-01T00:00:00+00:00"},
    {"memoryId": "mid1", "id": "m1", "content": "both"},
    {"id": "m1", "content": "c", "scope": "project:p1", "type": "instruction"},
    {"id": "m1", "content": "c", "scope": "seek:x", "category": "todo"},
    {"id": "m1", "content": "c", "pinned": True, "confidence": 0.4},
    {"id": "m1", "content": "c", "pinned": "yes"},
    {"id": "m1", "content": "c", "expiresAt": "2030-01-01T00:00:00+00:00"},
    {"id": "m1", "content": "c", "expiresAt": ""},
    {"id": "m1", "content": "   collapsed   text  "},
    {"id": "m1", "content": "c", "source": {"kind": "chat", "refId": "r"}},
    {"id": "m1", "content": "c", "source": "manual"},
    {"id": "m1", "content": "c", "source": 5},
    {"id": "m1", "content": ""},
    {"id": "", "content": "no id"},
    {"content": "no id at all"},
]

SAFE_CASES: list[str] = [
    "just a normal fact",
    "my api key is sk-abcdefghijklmnopqrstuvwxyz",
    "password: hunter2hunter2",
    "",
]

READABLE_SCOPE_CASES: list[tuple[str, str, str, str]] = [
    ("none", "", "", ""),
    ("project", "p1", "", ""),
    ("skill", "", "s1", ""),
    ("automation", "", "", "a1"),
    ("all", "p1", "s1", "a1"),
]

SKILL_POLICY_CASES: list[dict] = [
    {},
    {"memoryPolicy": {}},
    {"memoryPolicy": {"read": False}},
    {"memoryPolicy": {"read": True}},
    {"memoryPolicy": {"read": 0}},
    {"memoryPolicy": {"read": "no"}},
    {"memoryPolicy": {"read": True, "scope": "project"}},
    {"memoryPolicy": {"read": True, "scope": "global"}},
    {"memoryPolicy": "not-a-dict"},
]

HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")


def error_code(exc: BaseException) -> str | None:
    """The oracle's `ErrorCode` value, or `None` when the exception carries none.

    `error_code(exc)` raises `AttributeError` on an exception with no
    `code` at all, which would turn a probe into a false failure — so the attribute and
    its `.value` are both checked.
    """
    code = getattr(exc, "code", None)
    return getattr(code, "value", None) if code is not None else None


def id_shape(value: object) -> str:
    text = str(value or "")
    if HEX16.match(text):
        return "<hex16>"
    return text


def mask_ids(value: object, known: list[str]) -> object:
    """Replace every minted id with a placeholder so runs are comparable."""
    if isinstance(value, str):
        for item in known:
            if item and item in value:
                value = value.replace(item, "<id>")
        return value
    if isinstance(value, list):
        return [mask_ids(item, known) for item in value]
    if isinstance(value, dict):
        return {key: mask_ids(item, known) for key, item in value.items()}
    return value


def main() -> int:
    workspace = tempfile.mkdtemp(prefix="memory-schema-probe-")
    os.environ["DEEPSEEK_INFRA_ROOT"] = workspace

    # Import after the root is set so every store path resolves under it.
    from deepseek_infra.infra.memory import policy, schema, search, store
    from deepseek_infra.infra.memory import schema as schema_module
    from deepseek_infra.infra.data import memory as legacy

    # Pin the clock the projection reads. Three modules render it: `schema` (for a
    # missing timestamp on read), `data.memory` (through `schema`'s rebound alias) and
    # `memory.store` (for `edit_memory`'s `updatedAt`, which imports the name
    # directly). Rebinding only the first two leaves `edit_memory` on the real clock,
    # which is exactly the divergence this pin exists to remove.
    schema_module.utc_now_iso = lambda: FIXED_ISO
    schema_module.legacy_memory.utc_now_iso = lambda: FIXED_ISO
    legacy.utc_now_iso = lambda: FIXED_ISO
    store.utc_now_iso = lambda: FIXED_ISO

    out: dict = {}

    for label in SCOPE_CASES:
        out[f"public_scope::{label!r}"] = schema.public_scope(label)

    for label, scope, project_id, skill_id, automation_id in STORAGE_SCOPE_CASES:
        out[f"storage_scope::{label}"] = schema.storage_scope(
            scope, project_id=project_id, skill_id=skill_id, automation_id=automation_id
        )

    for label in TYPE_CASES:
        out[f"public_type::{label!r}"] = schema.public_type(label)
        out[f"legacy_category::{label!r}"] = schema.legacy_category(label)

    for index, value in enumerate(CONFIDENCE_CASES):
        result = schema.public_confidence(value)
        # NaN and inf have no JSON representation; report them as tags so the two
        # sides compare the same thing.
        if isinstance(result, float) and result != result:
            out[f"confidence::{index}"] = "nan"
        elif isinstance(result, float) and result in (float("inf"), float("-inf")):
            out[f"confidence::{index}"] = "inf" if result > 0 else "-inf"
        else:
            out[f"confidence::{index}"] = result

    for index, value in enumerate(SOURCE_REF_CASES):
        out[f"source_ref::{index}"] = schema.normalize_source_ref(value)

    for index, value in enumerate(PUBLIC_SOURCE_CASES):
        out[f"public_source::{index}"] = schema.public_source(value)
        out[f"public_source_fallback::{index}"] = schema.public_source(value, fallback_ref="fb")

    for index, value in enumerate(PUBLIC_MEMORY_CASES):
        out[f"public_memory::{index}"] = schema.public_memory(dict(value))

    for label in SAFE_CASES:
        try:
            policy.assert_memory_safe(label)
        except Exception as exc:  # noqa: BLE001 - the probe records the refusal
            out[f"safe::{label!r}"] = {"refused": True, "code": error_code(exc)}
        else:
            out[f"safe::{label!r}"] = {"refused": False}

    for label, project_id, skill_id, automation_id in READABLE_SCOPE_CASES:
        out[f"readable::{label}"] = policy.readable_scopes(
            project_id=project_id, skill_id=skill_id, automation_id=automation_id
        )

    for index, skill in enumerate(SKILL_POLICY_CASES):
        out[f"can_read::{index}"] = policy.skill_can_read_memory(skill, project_id="p1")
        out[f"can_read_noproject::{index}"] = policy.skill_can_read_memory(skill, project_id="")

    # --- store operations, against the real temp root ---------------------------
    created = store.add_memory(
        "likes dark mode",
        memory_type="preference",
        source={"kind": "chat", "refId": "c1"},
        confidence=0.7,
        pinned=True,
    )
    minted = [str(created.get("memoryId") or "")]
    out["store::add::shape"] = id_shape(created.get("memoryId"))
    out["store::add::item"] = mask_ids(created, minted)

    stored = legacy.load_memories()
    out["store::stored_count"] = len(stored)
    out["store::stored_row"] = mask_ids(stored[0] if stored else {}, minted)

    store.add_memory("a global fact", memory_type="fact")
    store.add_memory("a project fact", scope="project", project_id="p1", memory_type="fact")
    out["store::list_all"] = mask_ids(store.list_memories(), minted)
    out["store::list_project"] = mask_ids(store.list_memories(scope="project", project_id="p1"), minted)
    out["store::list_global"] = mask_ids(store.list_memories(scope="global"), minted)

    edited_id = str(created.get("memoryId") or "")
    edited = store.edit_memory(edited_id, {"content": "updated", "type": "instruction", "pinned": True})
    out["store::edit"] = mask_ids(edited, minted)
    try:
        store.edit_memory("missing-id", {"content": "x"})
    except Exception as exc:  # noqa: BLE001
        out["store::edit_missing"] = {"code": error_code(exc), "status": getattr(exc, "status", None)}
    try:
        store.edit_memory("  ", {"content": "x"})
    except Exception as exc:  # noqa: BLE001
        out["store::edit_blank"] = {"code": error_code(exc)}

    out["store::search"] = mask_ids(search.search_memories("updated", limit=10), minted)
    out["store::search_zero"] = mask_ids(search.search_memories("updated", limit=0), minted)
    out["store::search_no_limit"] = len(search.search_memories("updated"))
    out["store::context_none"] = search.memory_context_for_skill({}, "updated")
    out["store::context_read"] = mask_ids(
        search.memory_context_for_skill({"memoryPolicy": {"read": True}}, "updated"), minted
    )

    out["store::delete_by_id"] = store.delete_memory(edited_id)
    out["store::delete_again"] = store.delete_memory(edited_id)
    out["store::delete_blank"] = store.delete_memory("")

    # Sensitive content is refused before anything is written.
    before = len(legacy.load_memories())
    try:
        store.add_memory("my api key is sk-abcdefghijklmnopqrstuvwxyz")
    except Exception as exc:  # noqa: BLE001
        out["store::add_sensitive"] = {
            "code": error_code(exc),
            "unchanged": len(legacy.load_memories()) == before,
        }

    out["store::clear"] = store.clear_memory_count() if hasattr(store, "clear_memory_count") else None
    out.pop("store::clear", None)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


