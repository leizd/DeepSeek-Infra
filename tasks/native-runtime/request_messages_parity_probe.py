"""Message-layer parity probe, Python side.

Covers `normalize_chat_messages`, its two validators (`validate_deepseek_payload`,
`_validate_request_messages`), `preflight_deepseek_payload`, and the tool-call helpers they
lean on (`normalize_tool_calls`, `stable_tool_call_id`, `canonical_tool_arguments`,
`_image_content_parts`).

**The content expander is stubbed.** `expanded_message_content` expands attachments through
`build_attachment_context`, which reads the file index — that is I/O and belongs to the file
store's slice. The stub here mirrors the oracle's *non-attachment* path exactly
(`str(message.get("content") or "").strip()`), and the Rust port takes the expander as a
parameter, so this probe compares the layer rather than the store. Attachment *parts* are
still exercised, through `_image_content_parts`, which is pure.

Usage::

    python tasks/native-runtime/request_messages_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example request_messages_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.infra.gateway import deepseek_client as dc  # noqa: E402

LONG_IMAGE = "data:image/png;base64," + "A" * 40
SHORT_IMAGE = "data:image/png;base64,AA"

MESSAGE_SETS: list[Any] = [
    [],
    [{"role": "user", "content": "hi"}],
    [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "ok"}],
    [{"role": "user", "content": "  padded  "}],
    [{"role": "user", "content": ""}],
    [{"role": "user", "content": "   "}],
    [{"role": "user", "content": None}],
    [{"role": "user", "content": 5}],
    [{"role": "tool", "content": "result", "tool_call_id": "c1"}],
    [{"role": "tool", "content": "result"}],
    [{"role": "tool", "content": "  ", "tool_call_id": "c1"}],
    [{"role": "unknown", "content": "x"}],
    [{"role": 5, "content": "x"}],
    [5],
    ["not-a-dict"],
    [{"role": "user", "content": "look", "attachments": [{"imageData": LONG_IMAGE}]}],
    [{"role": "user", "content": "", "attachments": [{"imageData": LONG_IMAGE}]}],
    [{"role": "user", "content": "look", "attachments": [{"imageData": SHORT_IMAGE}]}],
    [{"role": "user", "content": "look", "attachments": [{"imageData": "http://x"}]}],
    [{"role": "user", "content": "x", "attachments": [{"imageData": LONG_IMAGE}, "not-a-dict"]}],
    [{"role": "assistant", "content": "calling", "tool_calls": [{"id": "c1", "function": {"name": "web_search", "arguments": "{\"q\": \"中\"}"}}]}],
    [{"role": "assistant", "content": "calling", "tool_calls": []}],
    [{"role": "assistant", "content": "calling", "tool_calls": [{"function": {"name": ""}}]}],
]

TOOL_CALL_VALUES: list[Any] = [
    None,
    "not-a-list",
    [],
    [{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{\"q\": 1}"}}],
    # Keys already sorted: an object `arguments` is re-serialized in insertion order by the
    # oracle and in sorted order by the port, which cannot recover the order (see the module
    # docs in `request_messages.rs`). An unsorted object would compare that known divergence.
    [{"function": {"name": "search", "arguments": {"a": 2, "b": 1}}}],
    [{"function": {"name": "search"}}],
    [{"name": "top_level", "arguments": "raw"}],
    [{"function": {"name": "   ", "arguments": "x"}}],
    [{"function": "not-a-dict", "name": "fallback", "arguments": "{}"}],
    [{"id": "", "function": {"name": "blank-id"}}],
    [{"id": 7, "type": "", "function": {"name": "numeric-id"}}],
    ["not-a-dict", {"function": {"name": "kept"}}],
    [{"function": {"name": "canon", "arguments": "{\"b\":2,\"a\":1}"}}],
    [{"function": {"name": "bad-json", "arguments": "not json"}}],
    [{"function": {"name": "long_name_" + "x" * 60, "arguments": "{}"}}],
]

STABLE_ID_CASES = [(0, "web_search"), (1, ""), (2, "  "), (3, "  Mixed-Case.Name  "), (4, "中" * 60)]

CANONICAL_CASES: list[Any] = ["{\"b\":2,\"a\":1}", "not json", "  padded  ", 5, {"z": 1, "a": [2, 3]}, [1, "中"]]

VALIDATE_PAYLOADS: list[Any] = [
    {},
    {"apiKey": "k", "messages": [{"role": "user", "content": "x"}]},
    {"apiKey": "  k  ", "messages": [{"role": "user", "content": "x"}]},
    {"apiKey": "", "messages": [{"role": "user", "content": "x"}]},
    {"apiKey": "k", "model": "flash", "messages": [{"role": "user", "content": "x"}]},
    {"apiKey": "k", "model": "unknown", "messages": [{"role": "user", "content": "x"}]},
    {"apiKey": "k", "model": "auto", "messages": [{"role": "user", "content": "帮我写代码"}]},
    {"apiKey": 0, "messages": [{"role": "user", "content": "x"}]},
    {"apiKey": "k"},
    {"apiKey": "k", "messages": []},
    {"apiKey": "k", "messages": "not-a-list"},
]

VALIDATE_MESSAGE_CASES = [
    ([{"role": "user", "content": "x"}], None),
    ([{"role": "system", "content": "x"}], None),
    ([{"role": "user", "content": "x"}], "summary"),
    ([{"role": "user", "content": f"m{i}"} for i in range(41)], None),
    ([{"role": "user", "content": f"m{i}"} for i in range(41)], "summary"),
    ([{"role": "user", "content": f"m{i}"} for i in range(40)], None),
    ([{"role": "user", "content": "  "}], None),
]


def expander(message):
    """The oracle's non-attachment path, verbatim: `str(content or "").strip()`."""
    return str(message.get("content") or "").strip()


def emit(outcome):
    if isinstance(outcome, Exception):
        return {"error": getattr(outcome, "code", None), "status": getattr(outcome, "status", None),
                "message": str(outcome)}
    return outcome


def main() -> int:
    out: dict[str, object] = {}

    saved = dc.expanded_message_content
    dc.expanded_message_content = expander
    try:
        for index, messages in enumerate(MESSAGE_SETS):
            try:
                normalized = dc.normalize_chat_messages(messages)
                out[f"normalize::{index}"] = normalized
            except Exception as exc:  # noqa: BLE001 - the probe compares the rejection
                out[f"normalize::{index}"] = emit(exc)

        # The check layer gets its own corpus: the message-count rule only fires above 40
        # messages, so reusing MESSAGE_SETS here would leave the 409 path untested — a corpus
        # that cannot fail reads as a pass.
        for index, (messages, _unused) in enumerate(VALIDATE_MESSAGE_CASES):
            for summary_index, summary in enumerate([None, "s"]):
                payload = {} if summary is None else {"contextSummary": summary}
                try:
                    dc._validate_request_messages(payload, messages)
                    out[f"check::{index}::{summary_index}"] = "ok"
                except Exception as exc:  # noqa: BLE001
                    out[f"check::{index}::{summary_index}"] = emit(exc)
    finally:
        dc.expanded_message_content = saved

    for index, value in enumerate(TOOL_CALL_VALUES):
        out[f"tool-calls::{index}"] = dc.normalize_tool_calls(value)
        out[f"tool-calls::stable::{index}"] = dc.normalize_tool_calls(value, stable_ids=True)
        out[f"tool-calls::canonical::{index}"] = dc.normalize_tool_calls(value, canonical_arguments=True)

    with_exported = [(index, name) for index, name in STABLE_ID_CASES]
    for index, (position, name) in enumerate(with_exported):
        out[f"stable-id::{index}"] = dc.stable_tool_call_id(position, name)

    for index, value in enumerate(CANONICAL_CASES):
        out[f"canonical::{index}"] = dc.canonical_tool_arguments(value)

    image_cases: list[Any] = [
        {},
        {"attachments": "x"},
        {"attachments": [{"imageData": LONG_IMAGE}]},
        {"attachments": [{"imageData": SHORT_IMAGE}]},
        {"attachments": [{"imageData": " data:image/png;base64," + "B" * 30 + " "}]},
        {"attachments": [{"imageData": 5}, "x"]},
    ]
    for index, message in enumerate(image_cases):
        out[f"image-parts::{index}"] = dc._image_content_parts(message)

    for index, payload in enumerate(VALIDATE_PAYLOADS):
        try:
            api_key, model, messages = dc.validate_deepseek_payload(payload)
            out[f"validate::{index}"] = {"apiKey": api_key, "model": model, "messages": messages}
        except Exception as exc:  # noqa: BLE001
            out[f"validate::{index}"] = emit(exc)
        try:
            api_key, model, messages = dc.preflight_deepseek_payload(payload)
            out[f"preflight::{index}"] = {"apiKey": api_key, "model": model, "count": len(messages)}
        except Exception as exc:  # noqa: BLE001
            out[f"preflight::{index}"] = emit(exc)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
