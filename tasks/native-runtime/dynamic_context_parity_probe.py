"""Per-turn dynamic context parity probe, Python side.

Covers the assembly layer of `gateway/deepseek_client.py`: `format_current_time_context`,
`format_context_summary_context`, `format_memory_notice`, `format_slides_skill_context`,
`presentation_intent_requested`, `build_dynamic_turn_context` (the reader of
`payload["searchContext"]`) and `append_context_to_latest_user`, plus the prompt constants
they splice in — the slides reference and runtime guidance are transcribed into Rust, so
comparing the assembled output is what proves the transcription.

`deepseek_client` imports cleanly here, so the modules are imported rather than re-executed
through `ast`: the functions under test are the oracle's own objects.

**The clock has to be stubbed.** `format_current_time_context` takes `now=`, but
`build_dynamic_turn_context` calls it with no argument and therefore reads the machine
clock, which is not comparable across runs or machines. The probe replaces
`dc.format_current_time_context` with the anchored output for the assembly cases, which is
the mirror image of the Rust side, where the clock is injected rather than read. The real
function is still exercised directly by the `time::` cases, over eight fixed instants.

The naive-datetime arm (`now.tzinfo is None` → assume UTC, then convert to the *machine's*
local zone) is deliberately absent from the corpus: its output depends on the host. Rust has
no counterpart to it either, since the offset is injected.

Time case tuple: (epoch_seconds, offset_seconds, tzname). The same triples, in the same
order, are constructed on the Rust side from `LocalNow`.

Usage::

    python tasks/native-runtime/dynamic_context_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example dynamic_context_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)

Nested structures are emitted as JSON values rather than strings so that `sort_keys=True`
reaches inside them. `append_context_to_latest_user` therefore compares as *content*; the
oracle's insertion order (`role` then `content`) is a byte-level concern for the
request-assembly slice, and that landmine is recorded in `dynamic_context.rs`.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.infra.gateway import deepseek_client as dc  # noqa: E402
from deepseek_infra.infra.rag import context_compressor as cc  # noqa: E402
from deepseek_infra.infra.tool_runtime import slides_skill as ss  # noqa: E402

TIME_CASES = [
    (1758096268, 0, "UTC"),
    (1758096268, 28800, "China Standard Time"),
    (1758096268, -18000, "Eastern Standard Time"),
    (1758096268, 19800, "India Standard Time"),
    (1758096268, -34200, "Marquesas Time"),
    (0, 0, "UTC"),
    (1758067200, 28800, "China Standard Time"),
    (1758153600, 28800, "China Standard Time"),
]

# The anchor whose formatted output stubs the clock for the assembly cases, and the
# `LocalNow` the Rust side builds for the same purpose.
ANCHOR_INDEX = 1

# (payload, memory_state, tools_enabled)
CORPUS = [
    ({}, {}, True),
    ({"searchContext": "hits"}, {}, True),
    ({"searchContext": "  spaced  ", "continuationContext": "cont"}, {}, True),
    ({"contextSummary": "较早的摘要"}, {"context": "记忆内容", "notice": "已保存"}, True),
    ({"searchEnabled": True, "searchMode": "on"}, {}, True),
    ({"searchEnabled": True, "searchMode": "off"}, {}, True),
    ({"messages": [{"role": "user", "content": "帮我做一份 PPT"}]}, {}, True),
    ({"messages": [{"role": "user", "content": "什么是 presentation？"}]}, {}, True),
    ({"searchContext": 0}, {}, True),
    ({"searchContext": [1, 2]}, {}, True),
    ({"contextSummary": "x" * 12001}, {}, True),
    (
        {
            "searchEnabled": True,
            "searchMode": "force",
            "contextSummary": "s",
            "continuationContext": "c",
            "searchContext": "ctx",
            "messages": [{"role": "user", "content": "生成一份幻灯片"}],
        },
        {"context": "m", "notice": "n"},
        True,
    ),
    ({"searchEnabled": True, "messages": [{"role": "user", "content": "做 PPT"}]}, {}, False),
    ({"searchEnabled": True, "searchMode": "on"}, {}, False),
]

MESSAGE_CASES = [
    ([], ""),
    ([{"role": "user", "content": "hi"}], ""),
    ([{"role": "user", "content": "hi"}], "[Per-turn context]\n\nx"),
    ([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], "ctx"),
]


def aware(epoch_seconds: int, offset_seconds: int, name: str) -> datetime:
    zone = timezone(timedelta(seconds=offset_seconds), name)
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).astimezone(zone)


def main() -> int:
    out: dict[str, object] = {}

    out["header"] = dc.CURRENT_TIME_CONTEXT_HEADER
    out["web-search-hint"] = dc.WEB_SEARCH_SYSTEM_HINT
    out["slides-name"] = ss.SLIDES_SKILL_NAME
    out["slides-reference"] = ss.SLIDES_SKILL_REFERENCE
    out["slides-guidance"] = ss.SLIDES_RUNTIME_GUIDANCE
    out["summary-max-chars"] = cc.CONTEXT_SUMMARY_MAX_CHARS
    out["slides-context"] = dc.format_slides_skill_context()

    for index, (epoch, offset, name) in enumerate(TIME_CASES):
        out[f"time::{index}"] = dc.format_current_time_context(now=aware(epoch, offset, name))

    out["summary::empty"] = dc.format_context_summary_context("")
    out["summary::short"] = dc.format_context_summary_context("abc")
    out["summary::capped"] = dc.format_context_summary_context("x" * 12001)
    out["notice::plain"] = dc.format_memory_notice("已保存")
    out["notice::empty"] = dc.format_memory_notice("")

    real_clock = dc.format_current_time_context
    anchor = real_clock(now=aware(*TIME_CASES[ANCHOR_INDEX]))
    try:
        dc.format_current_time_context = lambda: anchor
        for index, (payload, memory_state, tools_enabled) in enumerate(CORPUS):
            out[f"build::{index}"] = dc.build_dynamic_turn_context(
                payload, memory_state, tools_enabled=tools_enabled
            )
    finally:
        dc.format_current_time_context = real_clock

    for index, (messages, context) in enumerate(MESSAGE_CASES):
        out[f"append::{index}"] = dc.append_context_to_latest_user(messages, context)

    out["anchor"] = anchor

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
