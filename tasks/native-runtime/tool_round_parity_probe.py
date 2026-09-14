"""Tool-round control parity probe, Python side.

Extracts the round-bookkeeping functions **verbatim** from the real
``deepseek_infra`` sources via ``ast`` and replays a fixed set of scripts,
printing results as canonical JSON so the Rust side can be diffed against them
byte-for-byte.

Scope is layer 1 only — round control, not tool execution or policy. The
functions exercised here are exactly the ones ``tool_rounds.rs`` mirrors:

    deepseek_client.py   normalize_tool_calls (lenient mode)
                         stable_tool_call_id, canonical_tool_arguments
                         force_final_answer_without_tools
                         append_tool_exchange   (message-assembly half only)
                         tool_names
    tool_runtime/tools.py  MAX_TOOL_ROUNDS, MAX_TOOL_CALLS_PER_RESPONSE

``append_tool_exchange`` calls ``execute_tool_calls`` for the tool *results*, so
that call is stubbed: layer 2 is out of scope, and the probe feeds the same
synthetic results to both sides. Stubbing it is stated explicitly rather than
hidden, because a probe that silently replaces the thing it measures proves
nothing.

Usage::

    python tasks/native-runtime/tool_round_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-gateway --example tool_round_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEEPSEEK_CLIENT = REPO / "deepseek_infra" / "infra" / "gateway" / "deepseek_client.py"
TOOLS = REPO / "deepseek_infra" / "infra" / "tool_runtime" / "tools.py"

# The delta scripts both sides replay. Each is one streamed `delta.tool_calls`
# array, in arrival order. They are chosen to pin the behaviors that are easy to
# get wrong: argument fragment concatenation, id overwrite, index sorting, an
# index-less delta, non-object entries, and a nameless call.
DELTA_SCRIPTS: list[tuple[str, list[object]]] = [
    (
        "single-call-fragmented-arguments",
        [
            [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "search_files", "arguments": '{"qu'}}],
            [{"index": 0, "function": {"arguments": 'ery":"x"}'}}],
        ],
    ),
    (
        "later-id-overwrites-earlier",
        [
            [{"index": 0, "id": "call_1", "function": {"name": "a", "arguments": "{}"}}],
            [{"index": 0, "id": "call_real"}],
        ],
    ),
    (
        "indexless-delta-lands-last",
        [
            [{"function": {"name": "first", "arguments": "{}"}}],
            [{"index": 5, "function": {"name": "five", "arguments": "{}"}}],
            [{"function": {"name": "third", "arguments": "{}"}}],
        ],
    ),
    (
        "out-of-order-indexes-sort",
        [
            [{"index": 1, "function": {"name": "second", "arguments": "{}"}}],
            [{"index": 0, "function": {"name": "first", "arguments": "{}"}}],
        ],
    ),
    (
        "placeholder-id-when-absent",
        [
            [{"index": 2, "function": {"name": "x", "arguments": "{}"}}],
        ],
    ),
    (
        "non-array-and-non-object-deltas",
        [
            "nope",
            [1, "two", None],
        ],
    ),
    (
        "empty-arguments-fragment-is-ignored",
        [
            [{"index": 0, "id": "c", "function": {"name": "n", "arguments": ""}}],
        ],
    ),
]

# Standalone lenient-normalization inputs, fed directly to `normalize_tool_calls`.
NORMALIZE_INPUTS: list[tuple[str, object]] = [
    ("not-a-list", {"nope": True}),
    ("nameless-dropped", [
        {"id": "a", "function": {"name": "  ", "arguments": "{}"}},
        {"id": "b", "function": {"name": "keep", "arguments": "{}"}},
        "not an object",
    ]),
    ("top-level-name-fallback", [{"name": "flat", "arguments": "{}"}]),
    ("object-arguments-json-encoded", [{"function": {"name": "x", "arguments": {"a": 1}}}]),
    ("missing-id-uses-positional", [{"function": {"name": "n", "arguments": "{}"}}]),
    ("missing-type-defaults-to-function", [{"id": "z", "function": {"name": "n", "arguments": "{}"}}]),
    ("numeric-id-is-stringified", [{"id": 123, "type": 7, "function": {"name": "n", "arguments": "{}"}}]),
    ("zero-id-falls-back", [{"id": 0, "function": {"name": "n", "arguments": "{}"}}]),
    ("empty-string-id-falls-back", [{"id": "", "function": {"name": "n", "arguments": "{}"}}]),
    # `object-arguments-json-encoded` above already pins the `": "` separator. The
    # next two pin the `", "` separator, nested containers, and non-ASCII keys,
    # which `ensure_ascii=False` must emit as raw UTF-8.
    ("nested-arguments-keep-python-separators", [{
        "function": {"name": "n", "arguments": {"a": 1, "b": [1, 2, {"c": "x"}]}},
    }]),
    # Non-ASCII values stay raw UTF-8 (`ensure_ascii=False`). The keys are given
    # in sorted order on purpose: this workspace compiles `serde_json` without
    # `preserve_order`, so Rust re-sorts object keys while Python keeps insertion
    # order. Sorting the *input* removes that variable so the case measures the
    # escaping rule, which is what layer 1 controls. The ordering limitation
    # itself is recorded in docs/GATEWAY_TOOL_ROUND_PARITY.md.
    ("non-ascii-arguments-stay-unescaped", [{
        "function": {"name": "n", "arguments": {"n": None, "t": True, "名": "值"}},
    }]),
]


def extract(path: Path, names: set[str]) -> dict[str, str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                found[node.name] = segment
    missing = names - set(found)
    if missing:
        raise SystemExit(f"could not extract {sorted(missing)} from {path}")
    return found


def extract_constants(path: Path, names: set[str]) -> dict[str, int]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    found[target.id] = ast.literal_eval(node.value)
    missing = names - set(found)
    if missing:
        raise SystemExit(f"could not extract constants {sorted(missing)} from {path}")
    return found


def build_namespace() -> dict:
    """Namespace holding the oracle's real round-control functions.

    `execute_tool_calls` is stubbed because it is layer 2 (tool execution). The
    stub returns the caller-supplied synthetic results unchanged, so the message
    assembly under test is the real one. `raise_if_cancelled` is stubbed because
    cancellation is a transport concern with no bearing on assembly.

    Everything else — including `append_tool_exchange` and
    `merge_stream_tool_call_deltas` — is the oracle's own code, extracted
    verbatim. Only the two boundary calls are replaced, and that replacement is
    stated here rather than hidden, because a probe that silently substitutes the
    thing it measures proves nothing.
    """
    functions = extract(
        DEEPSEEK_CLIENT,
        {
            "normalize_tool_calls",
            "stable_tool_call_id",
            "canonical_tool_arguments",
            "force_final_answer_without_tools",
            "tool_names",
            "append_tool_exchange",
            "merge_stream_tool_call_deltas",
            "finalized_stream_tool_calls",
        },
    )
    constants = extract_constants(TOOLS, {"MAX_TOOL_ROUNDS", "MAX_TOOL_CALLS_PER_RESPONSE"})
    assembled: list = []
    namespace: dict = {
        "json": json,
        "Any": object,
        "Callable": object,
        "MAX_TOOL_ROUNDS": constants["MAX_TOOL_ROUNDS"],
        "MAX_TOOL_CALLS_PER_RESPONSE": constants["MAX_TOOL_CALLS_PER_RESPONSE"],
        "TOOL_BUDGET_EXHAUSTED_PROMPT": _extract_budget_prompt(),
        # Layer 2 boundary: returns the synthetic results the driver supplied.
        "execute_tool_calls": lambda tool_calls, **kwargs: list(assembled),
        # Cancellation is transport, not assembly.
        "raise_if_cancelled": lambda event=None: None,
        "threading": __import__("threading"),
        "ToolPolicy": object,
    }
    for name, source in functions.items():
        exec(compile(source, str(DEEPSEEK_CLIENT), "exec"), namespace)  # noqa: S102
    namespace["_assembled"] = assembled
    return namespace


def _extract_budget_prompt() -> str:
    source = DEEPSEEK_CLIENT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "TOOL_BUDGET_EXHAUSTED_PROMPT":
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str):
                        return value
    raise SystemExit("could not extract TOOL_BUDGET_EXHAUSTED_PROMPT")


def _extract_round_budget_note() -> str:
    """Lift the budget `system_note` literal out of `stream_deepseek`.

    The oracle inlines this string in the loop rather than naming it, so it is
    located structurally: the single string literal inside the loop's budget
    branch. The Rust constant `TOOL_BUDGET_NOTE` is a convenience name, and this
    key is what proves the two spellings agree.
    """
    source = DEEPSEEK_CLIENT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Name) and function.id == "emit_checked"):
            continue
        if len(node.args) != 1 or not isinstance(node.args[0], ast.Dict):
            continue
        fields = {
            key.value: value
            for key, value in zip(node.args[0].keys, node.args[0].values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        kind = fields.get("type")
        text = fields.get("text")
        if not (isinstance(kind, ast.Constant) and kind.value == "system_note"):
            continue
        if isinstance(text, ast.Constant) and isinstance(text.value, str):
            if text.value.startswith("工具调用次数已达上限"):
                return text.value
    raise SystemExit("could not extract the round-budget system_note literal")


def _append(namespace: dict, body: dict, assistant: dict, tool_calls: list, tool_results: list) -> dict:
    """Drive the oracle's real `append_tool_exchange` with layer 2 stubbed.

    The only substitution is `execute_tool_calls`: the oracle's own function is
    called, and its single boundary call returns the synthetic results the
    driver supplied. The assembly code being measured is therefore the real one,
    not a transcription of it.
    """
    namespace["_assembled"][:] = [dict(result) for result in tool_results]
    return namespace["append_tool_exchange"](
        body,
        assistant,
        tool_calls,
        memory_suggestion_callback=None,
        default_memory_scope="global",
        web_search_callback=None,
        cancel_event=None,
        policy=None,
        trace_id="",
        parent_span_id="",
    )


def main() -> int:
    for path in (DEEPSEEK_CLIENT, TOOLS):
        if not path.exists():
            print(f"missing {path}", file=sys.stderr)
            return 2
    namespace = build_namespace()
    normalize = namespace["normalize_tool_calls"]
    tool_names = namespace["tool_names"]

    out: dict = {}

    # 1. The merge loop, run by the oracle's own merge routine.
    merge = namespace["merge_stream_tool_call_deltas"]
    for name, deltas in DELTA_SCRIPTS:
        accumulator: dict = {}
        for delta in deltas:
            merge(accumulator, delta)
        raw = [accumulator[index] for index in sorted(accumulator)]
        out[f"merge::{name}"] = {
            "finalized": normalize(raw),
            "names": tool_names(normalize(raw)),
        }

    # 2. Lenient normalization fed directly.
    for name, value in NORMALIZE_INPUTS:
        out[f"normalize::{name}"] = normalize(value)

    # 3. Round-budget decisions, replayed with the oracle's branch order.
    for calls, tool_round in ((0, 99), (1, 0), (1, 2), (1, 3), (3, 1)):
        out[f"decide::{calls}@{tool_round}"] = _decide(calls, tool_round, namespace["MAX_TOOL_ROUNDS"])

    # 4. Message assembly and the forced final answer.
    body = {
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "tool_choice": {"type": "function", "function": {"name": "pinned"}},
    }
    calls = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    results = [{"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    out["append::object-tool-choice-released"] = _append(
        namespace, body, {"content": "thinking out loud", "reasoning_content": "why"}, calls, results
    )
    out["append::string-tool-choice-kept"] = _append(
        namespace, {"messages": [], "tool_choice": "auto"}, {"content": "a"}, [], []
    )
    out["append::empty-reasoning-omitted"] = _append(
        namespace, {"messages": []}, {"content": "answer", "reasoning_content": ""}, [], []
    )
    out["append::reasoning-alias-field"] = _append(
        namespace, {"messages": []}, {"content": "a", "reasoning": "alias"}, [], []
    )
    out["force::keeps-tools"] = namespace["force_final_answer_without_tools"](dict(body))
    out["force::drops-choice-without-tools"] = namespace["force_final_answer_without_tools"](
        {"messages": [], "tool_choice": "auto"}
    )
    out["force::empty-tools-counts-as-none"] = namespace["force_final_answer_without_tools"](
        {"messages": [], "tools": [], "tool_choice": "auto"}
    )

    # 5. Notes and limits.
    out["note::names"] = _tool_call_note(tool_names(calls))
    out["note::empty"] = _tool_call_note([])
    out["note::round-budget"] = _extract_round_budget_note()
    out["note::budget"] = namespace["TOOL_BUDGET_EXHAUSTED_PROMPT"]  # type: ignore[index]
    out["limits::per-response"] = namespace["MAX_TOOL_CALLS_PER_RESPONSE"]
    out["limits::rounds"] = namespace["MAX_TOOL_ROUNDS"]

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _decide(tool_call_count: int, tool_round: int, max_tool_rounds: int) -> str:
    """The oracle's branch, in its own order: finish is tested first.

    A transcription, not an extraction: the oracle's decision is an inline
    `if`/`break`/`continue` inside `stream_deepseek`'s loop, so there is no
    function to lift. The ordering it encodes is asserted independently by the
    Rust unit tests for `decide_round`.
    """
    if not tool_call_count:
        return "finish"
    if tool_round >= max_tool_rounds:
        return "force_final_answer"
    return "continue"


def _tool_call_note(names: list[str]) -> str:
    return f"正在调用本地工具：{', '.join(names) or 'tool'}\n\n"


if __name__ == "__main__":
    raise SystemExit(main())
