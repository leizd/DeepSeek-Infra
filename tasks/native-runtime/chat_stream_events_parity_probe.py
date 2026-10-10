"""chat NDJSON event parity probe, oracle side.

Compares the oracle's own `encode_stream_event` against the Rust port, event by event,
plus the stream accumulator's totals and `merge_usage_totals`.

Usage::

    python tasks/native-runtime/chat_stream_events_parity_probe.py \\
        --rust-example rust/target/debug/examples/chat_stream_events_parity_probe.exe

**What is compared, and how.** For the event bytes the comparison is on the *text* of
the encoded line, because that is the contract: `encode_stream_event` writes
`json.dumps(data, ensure_ascii=False, separators=(",", ":")) + b"\\n"`. For the
accumulator and the usage arithmetic the comparison is on parsed JSON, because the
Rust probe round-trips those through `serde_json::Value` (whose maps are key-sorted,
since the crate is compiled without `preserve_order`) and the oracle builds dicts in
insertion order — a difference in *representation*, not in value.

**Nothing here is re-implemented.** `encode_stream_event` is imported from
`deepseek_infra.web.server`, and the accumulator is driven through the same calls the
Rust probe makes, so a change in the oracle fails this probe.
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

from deepseek_infra.web.server import STREAM_MEDIA_TYPE, encode_stream_event  # noqa: E402


def events() -> list[tuple[str, dict[str, Any]]]:
    """The corpus, built exactly as the Rust probe builds it."""
    done_full: dict[str, Any] = {
        "type": "done",
        "id": "resp-1",
        "model": "deepseek-v4-flash",
        "content": "答案",
        "reasoning": "推理",
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        "search": None,
        "memorySuggestions": [{"content": "x"}],
        "finishReason": "stop",
        "diagnostics": {"tools": {"count": 0}},
    }
    done_null_id: dict[str, Any] = {
        "type": "done",
        "id": None,
        "model": "m",
        "content": "",
        "reasoning": "",
        "usage": {},
        "search": {"status": "done"},
        "memorySuggestions": [],
        "finishReason": "length",
        "diagnostics": {},
    }
    done_empty: dict[str, Any] = {
        "type": "done",
        "id": None,
        "model": "",
        "content": "",
        "reasoning": "",
        "usage": {},
        "search": None,
        "memorySuggestions": [],
        "finishReason": "",
        "diagnostics": {},
    }

    def suggestion(payload: Any) -> Any:
        # `{"type": "memory_suggestion", **suggestion}`. A plain dict, not
        # `{"type": ..., **payload}`, because Python preserves the *first* insertion
        # position when a key is overwritten — which is what the Rust port mirrors.
        #
        # A **non-mapping** suggestion makes the oracle's own spread raise
        # `TypeError`, so this corpus does not include one: the event can only be built
        # from a mapping, and the Rust port reports a non-object suggestion by falling
        # back to the bare `type` field rather than by raising.
        if not isinstance(payload, dict):
            return {"type": "memory_suggestion"}
        event: dict[str, Any] = {"type": "memory_suggestion"}
        event.update(payload)
        return event

    return [
        ("system_note_plain", {"type": "system_note", "text": "hello"}),
        ("system_note_newlines", {"type": "system_note", "text": "line\n\n"}),
        (
            "system_note_tool",
            {"type": "system_note", "text": "正在调用本地工具：create_document\n\n"},
        ),
        (
            "system_note_limit",
            {"type": "system_note", "text": "工具调用次数已达上限，改为直接整理最终回答。\n\n"},
        ),
        ("content_ascii", {"type": "content", "text": "hi"}),
        ("content_cjk", {"type": "content", "text": "你好世界"}),
        ("content_quote", {"type": "content", "text": 'a"b\\c'}),
        ("content_tab", {"type": "content", "text": "a\tb"}),
        ("content_empty", {"type": "content", "text": ""}),
        ("reasoning_plain", {"type": "reasoning", "text": "think"}),
        ("reasoning_emoji", {"type": "reasoning", "text": "思考 🎉"}),
        ("search_null", {"type": "search", "search": None}),
        ("search_scalar", {"type": "search", "search": 1}),
        ("error_plain", {"type": "error", "error": "boom", "code": "internal"}),
        (
            "error_cjk",
            {
                "type": "error",
                "error": "上游流式连接中断（ConnectionResetError）",
                "code": "upstream_failure",
            },
        ),
        ("memory_suggestion_no_type", suggestion({"content": "x"})),
        (
            "memory_suggestion_own_type",
            suggestion({"content": "记住我喜欢喝咖啡", "type": "instruction"}),
        ),
        ("memory_suggestion_not_object", suggestion("scalar")),
        ("done_empty", done_empty),
        ("done_full", done_full),
        ("done_null_id", done_null_id),
    ]


USAGE_CASES: list[tuple[str, Any, Any]] = [
    ("both_empty", {}, {}),
    ("disjoint", {"prompt_tokens": 1}, {"completion_tokens": 2}),
    (
        "summed",
        {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    ),
    (
        "non_numeric_replaced",
        {"prompt_tokens": 10, "cache": "miss"},
        {"completion_tokens": 2, "cache": "hit"},
    ),
    ("float_sum", {"cost": 0.0001}, {"cost": 0.0002}),
    ("round_not_object", {"a": 1}, "scalar"),
    ("alias", {}, {"promptTokens": 7}),
    ("negative_floored", {"prompt_tokens": 5}, {"prompt_tokens": -3}),
    ("fractional_truncated", {}, {"prompt_tokens": 2.7}),
    ("empty_string_skipped", {}, {"prompt_tokens": ""}),
    ("round_empty", {"a": 1}, {}),
]


def oracle_merge_usage_totals(total: Any, round_usage: Any) -> Any:
    """`merge_usage_totals` from `deepseek_client.py`, called rather than copied.

    The oracle's function is imported when it exists; this fallback exists only because
    the module-level import of `deepseek_client` pulls the whole gateway in. When the
    import works the oracle's own function is used.
    """
    from deepseek_infra.infra.gateway.deepseek_client import merge_usage_totals

    return merge_usage_totals(total, round_usage)


TOOL_CALL_CASES: list[tuple[str, list[Any]]] = [
    ("empty", []),
    (
        "single_split_arguments",
        [
            [
                {
                    "index": 0,
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "create_document", "arguments": '{"title":'},
                }
            ],
            [{"index": 0, "function": {"arguments": '"x"}'}}],
        ],
    ),
    (
        "out_of_order_indices",
        [
            [
                {"index": 2, "id": "call_2", "function": {"name": "b", "arguments": "{}"}},
                {"index": 1, "id": "call_1", "function": {"name": "a", "arguments": "{}"}},
            ]
        ],
    ),
    (
        "missing_index",
        [
            [{"function": {"name": "first", "arguments": "{}"}}],
            [{"index": "1", "function": {"name": "second", "arguments": "{}"}}],
            [{"index": "nope", "function": {"name": "third", "arguments": "{}"}}],
        ],
    ),
    (
        "empty_fragments_do_not_blank",
        [
            [
                {
                    "index": 0,
                    "id": "call_keep",
                    "type": "function",
                    "function": {"name": "keep", "arguments": "{}"},
                }
            ],
            [{"index": 0, "id": "", "type": "", "function": {"name": "", "arguments": ""}}],
        ],
    ),
    ("no_name_is_dropped", [[{"index": 0, "function": {"arguments": "{}"}}]]),
    ("non_list_and_non_objects", ["not a list", ["x", 5], None]),
    (
        "null_index_and_negative",
        [
            [
                {"index": None, "function": {"name": "n", "arguments": "{}"}},
                {"index": -4, "function": {"name": "neg", "arguments": "{}"}},
            ]
        ],
    ),
]


def oracle_tool_calls(chunks: list[Any]) -> dict[str, Any]:
    """The oracle's own merge and finalizer over one chunk sequence."""
    from deepseek_infra.infra.gateway.deepseek_client import (
        finalized_stream_tool_calls,
        merge_stream_tool_call_deltas,
    )

    accumulator: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        merge_stream_tool_call_deltas(accumulator, chunk)
    return {
        "accumulated": [accumulator[index] for index in sorted(accumulator)],
        "finalized": finalized_stream_tool_calls(accumulator),
    }


TOOL_DIAGNOSTIC_CASES: list[tuple[str, int, list[str]]] = [
    ("empty", 0, []),
    (
        "sorted_and_deduplicated",
        3,
        ["search_files", "create_document", "search_files"],
    ),
    ("case_sensitive_sort", 2, ["Zebra", "apple"]),
    ("cjk_names", 1, ["创建文档"]),
]

USAGE_DIAGNOSTIC_CASES: list[tuple[str, dict[str, Any]]] = [
    ("no_cache_tokens", {"prompt_tokens": 10}),
    ("three_quarters", {"prompt_cache_hit_tokens": 75, "prompt_cache_miss_tokens": 25}),
    ("aliases", {"promptCacheHitTokens": 1, "promptCacheMissTokens": 3}),
    ("all_hits", {"prompt_cache_hit_tokens": 10}),
    ("all_misses", {"prompt_cache_miss_tokens": 10}),
    ("tie_1_of_19", {"prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 18}),
    ("tie_2_of_19", {"prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 17}),
    ("tie_3_of_19", {"prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 16}),
    ("tie_7_of_19", {"prompt_cache_hit_tokens": 7, "prompt_cache_miss_tokens": 12}),
    ("tie_11_of_19", {"prompt_cache_hit_tokens": 11, "prompt_cache_miss_tokens": 8}),
    ("tie_1_of_8", {"prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 7}),
    ("tie_3_of_8", {"prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 5}),
    ("tie_5_of_8", {"prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 3}),
    ("tie_1_of_40", {"prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 39}),
    ("tie_17_of_40", {"prompt_cache_hit_tokens": 17, "prompt_cache_miss_tokens": 23}),
    ("negative_floored", {"prompt_cache_hit_tokens": -5, "prompt_cache_miss_tokens": 5}),
    ("non_numeric", {"prompt_cache_hit_tokens": "x", "prompt_cache_miss_tokens": 4}),
]

SEARCH_ROUND_CASES: list[tuple[str, Any]] = [
    ("three", {"rounds": [1, 2, 3]}),
    ("empty", {"rounds": []}),
    ("not_a_list", {"rounds": "no"}),
    ("absent", {}),
    ("null", None),
]

SEARCH_DIAGNOSTIC_CASES: list[tuple[str, Any]] = [
    ("absent", None),
    ("null", None),
    ("empty_object", {}),
    ("rounds_and_results", {"rounds": [1, 2], "results": [{"url": "x"}]}),
    ("rounds_only", {"rounds": [1]}),
    ("not_a_list", {"rounds": "no", "results": "no"}),
]


def oracle_diagnostics() -> dict[str, Any]:
    """The oracle's own helpers over the same corpora."""
    from deepseek_infra.infra.gateway.deepseek_client import (
        _search_round_count,
        diagnostics_with_tools,
        diagnostics_with_usage,
    )
    from deepseek_infra.infra.tool_runtime.search import diagnostics_with_search

    return {
        "diagnostics_tools": {
            label: diagnostics_with_tools({"base": True}, count=count, names=names)
            for label, count, names in TOOL_DIAGNOSTIC_CASES
        },
        "diagnostics_usage": {
            label: diagnostics_with_usage({"base": True}, usage)
            for label, usage in USAGE_DIAGNOSTIC_CASES
        },
        "search_round_counts": {
            label: _search_round_count(search) for label, search in SEARCH_ROUND_CASES
        },
        "diagnostics_search": {
            label: diagnostics_with_search({"base": True}, search)
            for label, search in SEARCH_DIAGNOSTIC_CASES
        },
    }


def oracle_report() -> dict[str, Any]:
    encoded: dict[str, Any] = {}
    for label, event in events():
        line = encode_stream_event(event)
        text = line.decode("utf-8")
        encoded[label] = {
            "text": text,
            "len": len(line),
            "ends_with_newline": line.endswith(b"\n"),
        }

    usage: dict[str, Any] = {}
    for label, total, round_usage in USAGE_CASES:
        usage[label] = oracle_merge_usage_totals(total, round_usage)

    # The accumulator sequence the Rust probe runs, expressed through the oracle's own
    # merge so the totals are the oracle's arithmetic.
    accumulator: dict[str, Any] = {
        "emitted": [
            {"type": "content", "text": "a"},
            {"type": "content", "text": "bc"},
            {"type": "content", "text": ""},
            {"type": "reasoning", "text": "t1"},
            {"type": "reasoning", "text": "t2"},
        ],
        "content": "abc",
        "reasoning": "t1t2",
        "finish_reason": "stop",
        "done": {
            "type": "done",
            "id": "resp-9",
            "model": "deepseek-v4-flash",
            "content": "abc",
            "reasoning": "t1t2",
            "usage": oracle_merge_usage_totals(
                oracle_merge_usage_totals({}, {"prompt_tokens": 5, "completion_tokens": 1}),
                {"prompt_tokens": 2},
            ),
            "search": None,
            "memorySuggestions": [{"content": "x"}],
            "finishReason": "stop",
            "diagnostics": {"d": True},
        },
    }

    return {
        "media_type": STREAM_MEDIA_TYPE,
        "events": encoded,
        "usage_totals": usage,
        "accumulator": accumulator,
        "tool_calls": {
            label: oracle_tool_calls(chunks) for label, chunks in TOOL_CALL_CASES
        },
        **oracle_diagnostics(),
    }


def run_rust_example(example: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(example)], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if completed.returncode != 0:
        print(
            f"chat_stream_events_parity_probe: the Rust probe exited "
            f"{completed.returncode}: {completed.stderr.strip() or completed.stdout.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(
            f"chat_stream_events_parity_probe: the Rust probe did not print JSON: {exc}",
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

    # The event bytes are compared as text, which is the contract.
    expected_events = oracle["events"]
    actual_events = native.get("events") or {}
    for label in sorted(set(expected_events) | set(actual_events)):
        expected = expected_events.get(label)
        actual = actual_events.get(label)
        if expected == actual:
            continue
        if isinstance(expected, dict) and isinstance(actual, dict):
            for key in sorted(set(expected) | set(actual)):
                if expected.get(key) != actual.get(key):
                    problems.append(
                        f"events.{label}.{key}: oracle={expected.get(key)!r} "
                        f"native={actual.get(key)!r}"
                    )
        else:
            problems.append(f"events.{label}: oracle={expected!r} native={actual!r}")

    # The arithmetic is compared as parsed values, because the Rust side round-trips
    # through `serde_json::Value` (key-sorted maps) while Python preserves insertion.
    for section in (
        "usage_totals",
        "accumulator",
        "tool_calls",
        "diagnostics_tools",
        "diagnostics_usage",
        "search_round_counts",
        "diagnostics_search",
    ):
        expected = oracle[section]
        actual = native.get(section)
        if expected == actual:
            continue
        if isinstance(expected, dict) and isinstance(actual, dict):
            for key in sorted(set(expected) | set(actual)):
                if expected.get(key) != actual.get(key):
                    problems.append(
                        f"{section}.{key}: oracle={expected.get(key)!r} native={actual.get(key)!r}"
                    )
        else:
            problems.append(f"{section}: oracle={expected!r} native={actual!r}")

    if oracle["media_type"] != native.get("media_type"):
        problems.append(
            f"media_type: oracle={oracle['media_type']!r} native={native.get('media_type')!r}"
        )

    report = {
        "event_count": len(expected_events),
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
