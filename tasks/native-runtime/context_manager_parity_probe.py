"""Context-manager parity probe, Python side.

Covers `gateway/context_manager.py`: `stable_json_dumps`, `tool_name` / the tool ordering,
`sliding_window_messages`, `manage_request_body` and `merge_context_manager_diagnostics`.

Both modules read their knobs as globals, so the corpus patches the manager's own four and the
engine's four through one helper — a small window plus a zero reserve is what makes the
trimming paths reachable without pushing large payloads through both sides.

Usage::

    python tasks/native-runtime/context_manager_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example context_manager_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.infra.gateway import context_engine as ce  # noqa: E402
from deepseek_infra.infra.gateway import context_manager as cm  # noqa: E402

STABLE_VALUES = [
    {"b": 1, "a": [1, 2, {"z": None}]},
    {"text": "中文", "n": 1.5},
    [],
    "plain",
]

# Tool *objects* only. A bare non-dict entry is deliberately absent: the oracle's `tool_name`
# calls `.get` on it and raises AttributeError, so it cannot be part of a compared corpus.
# `tool_sort_key` does guard, and that difference is pinned separately below and by unit tests.
TOOLS = [
    {"type": "function", "function": {"name": "web_search"}},
    {"type": "function", "function": {"name": "create_pptx"}},
    {"type": "other", "function": {"name": "create_pptx"}},
    {"function": {"name": "a_tool"}},
    {"type": "function", "function": {}},
    {"type": "function", "function": "not-a-dict"},
    {"type": "function"},
]

MESSAGE_SETS = [
    [],
    [{"role": "system", "content": "prefix"}, {"role": "user", "content": "hi"}],
    [
        {"role": "system", "content": "prefix"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "system", "content": "dynamic"},
    ],
    [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2"}],
    [{"role": "system", "content": "prefix"}, "not-a-dict", {"role": "user", "content": "u"}, 5],
    [{"role": "user", "content": f"m{i}"} for i in range(40)],
]

# (manager_enabled, window, engine_enabled, token_aware_trim,
#  engine_reserve, engine_safety, engine_default_window, engine_min_keep)
SETTINGS_CASES = [
    (True, 36, True, True, 8_192, 0.05, 65_536, 2),
    (True, 8, True, True, 0, 0.0, 40, 2),
    (True, 8, True, False, 0, 0.0, 40, 2),
    (True, 8, False, True, 0, 0.0, 40, 2),
    (False, 36, True, True, 8_192, 0.05, 65_536, 2),
]

MERGE_CASES = [
    ({"a": 1}, {"enabled": True, "requestMessageCount": 3}),
    ({"a": 1}, {"enabled": True, "requestMessageCount": 0}),
    ({"a": 1}, {"enabled": True, "contextEngine": {"x": 1}, "requestMessageCount": 2}),
    ({"a": 1, "contextEngine": {"old": True}}, {"enabled": True}),
]


def with_settings(case, call):
    manager_names = (
        "GATEWAY_CONTEXT_MANAGER_ENABLED",
        "GATEWAY_CONTEXT_WINDOW_MESSAGES",
        "CONTEXT_ENGINE_ENABLED",
        "CONTEXT_ENGINE_TOKEN_AWARE_TRIM",
    )
    engine_names = (
        "CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS",
        "CONTEXT_ENGINE_SAFETY_MARGIN_RATIO",
        "CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW",
        "CONTEXT_ENGINE_MIN_KEEP_MESSAGES",
    )
    saved = (tuple(getattr(cm, n) for n in manager_names), tuple(getattr(ce, n) for n in engine_names))
    for name, value in zip(manager_names, case[:4]):
        setattr(cm, name, value)
    for name, value in zip(engine_names, case[4:]):
        setattr(ce, name, value)
    try:
        return call()
    finally:
        for name, value in zip(manager_names, saved[0]):
            setattr(cm, name, value)
        for name, value in zip(engine_names, saved[1]):
            setattr(ce, name, value)


def main() -> int:
    out: dict[str, object] = {}

    for index, value in enumerate(STABLE_VALUES):
        out[f"stable::{index}"] = cm.stable_json_dumps(value)

    for index, tool in enumerate(TOOLS):
        out[f"tool-name::{index}"] = cm.tool_name(tool)
        out[f"tool-key::{index}"] = list(cm.tool_sort_key(tool))
    # `tool_sort_key` tolerates a non-dict where `tool_name` raises; only the former is
    # comparable, and the difference is recorded here so it is not mistaken for a port bug.
    out["tool-key::non-dict"] = list(cm.tool_sort_key("not-a-dict"))

    for index, messages in enumerate(MESSAGE_SETS):
        trimmed, dropped = cm.sliding_window_messages(messages)
        out[f"window::dropped::{index}"] = dropped
        out[f"window::kept::{index}"] = len(trimmed)
        out[f"window::roles::{index}"] = [
            item.get("role") if isinstance(item, dict) else "?" for item in trimmed
        ]

    for settings_index, case in enumerate(SETTINGS_CASES):
        for messages_index, messages in enumerate(MESSAGE_SETS):
            for allow in (False, True):
                # A model *outside* the window table: otherwise the table's 131 072 wins
                # over the small patched default and the token-trim path never runs.
                body = {"model": "unknown-model", "messages": messages, "tools": TOOLS}
                managed, diagnostics = with_settings(
                    case, lambda: cm.manage_request_body(body, allow_sliding_window=allow)
                )
                key = f"manage::s{settings_index}::m{messages_index}::a{int(allow)}"
                # Ask the oracle for the names rather than reaching into the shapes: a tool
                # whose `function` is not a dict is legal input and renders as an empty name.
                out[f"{key}::tools"] = [cm.tool_name(t) for t in managed.get("tools", [])]
                out[f"{key}::messages"] = len(managed.get("messages", []))
                # A message list may legally contain entries that are not objects, so the
                # probe compares a defensive role rather than assuming a shape.
                kept = managed.get("messages") or []
                role_of = lambda item: item.get("role") if isinstance(item, dict) else None
                out[f"{key}::first"] = role_of(kept[0]) if kept else None
                out[f"{key}::last"] = role_of(kept[-1]) if kept else None
                out[f"{key}::diagnostics"] = diagnostics

    for index, (diagnostics, manager) in enumerate(MERGE_CASES):
        out[f"merge::{index}"] = cm.merge_context_manager_diagnostics(diagnostics, dict(manager))

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
