"""Request-shaping helpers parity probe, Python side.

Covers the pure leaves of `build_deepseek_request`'s closure: `TOOL_PARALLEL_SYSTEM_HINT`,
`normalize_reasoning_effort`, `_has_image_content`, `tools_for_payload`,
`forced_artifact_tool_name` (with `should_force_create_pptx`, `has_create_pptx_tool` and
`mindmap_intent_requested`), `count_payload_attachments` (`chat_payload.py`), and
`empty_memory_state` / `memory_scope_from_payload` (`data/memory.py`).

`tools_for_payload` is compared as the **sequence of function names**, not as whole
definitions: the definitions themselves come from `agent_tool_definitions`, which the tool
catalog probe already covers, and comparing them again here would bury this probe's own
subject under a very large blob.

Usage::

    python tasks/native-runtime/request_shaping_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example request_shaping_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.infra.data import memory as mem  # noqa: E402
from deepseek_infra.infra.gateway import chat_payload, deepseek_client as dc  # noqa: E402

EFFORT_CASES = ["low", "max", "minimal", "MEDIUM", " high ", "", None, 5, True, "medium"]

MESSAGE_LISTS: list[Any] = [
    [],
    [{"role": "user", "content": "text"}],
    [{"role": "user", "content": [{"type": "text", "text": "a"}]}],
    [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}],
    [{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}],
    [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {"url": "u"}}]}],
    [{"role": "user", "content": "no parts"}, {"role": "user", "content": [{"type": "image_url"}]}],
]

TOOL_PAYLOADS: list[Any] = [
    {},
    {"searchEnabled": True, "searchMode": "on"},
    {"searchEnabled": True, "searchMode": "off"},
    {"searchEnabled": True, "allowedTools": ["create_pptx", "web_search"]},
    {"allowedTools": ["create_pptx"]},
    {"allowedTools": ["create_pptx"], "searchEnabled": True},
    {"allowedTools": "not-a-list"},
    {"allowedTools": [1, None, "create_pptx"]},
]

PPT_QUERY = [{"role": "user", "content": "帮我做一份 PPT"}]
MINDMAP_QUERY = [{"role": "user", "content": "画一张思维导图"}]
KEYWORD_ONLY = [{"role": "user", "content": "什么是 mindmap？"}]

FORCE_CASES: list[Any] = [
    ({"messages": PPT_QUERY}, "full"),
    ({"messages": PPT_QUERY, "toolsEnabled": False}, "full"),
    ({"messages": PPT_QUERY, "allowedTools": ["web_search"]}, "full"),
    ({"messages": PPT_QUERY, "allowedTools": ["create_pptx"]}, "full"),
    ({"messages": MINDMAP_QUERY}, "full"),
    ({"messages": MINDMAP_QUERY, "allowedTools": ["create_pptx"]}, "full"),
    ({"messages": KEYWORD_ONLY}, "full"),
    ({"messages": PPT_QUERY}, "own"),
]

ATTACHMENT_CASES = [
    None,
    [],
    [{"attachments": [{"a": 1}, {"b": 2}]}],
    [{"attachments": "x"}, {"attachments": [1, "y", {"z": 3}]}],
    [{"attachments": []}, {"role": "user"}],
    ["not a dict", {"attachments": [{"a": 1}]}],
]

MEMORY_PAYLOADS: list[Any] = [
    {},
    {"memoryEnabled": False},
    {"memoryEnabled": 0},
    {"memoryEnabled": ""},
    {"memoryScope": "project:abc"},
    {"memoryScope": "bogus"},
    {"messages": [{"role": "user", "projectId": "p1"}]},
    {"messages": [{"role": "user", "seekId": "s1"}]},
    {"messages": [{"role": "user", "projectId": "p1"}, {"role": "user", "content": "x"}]},
    {"messages": [{"role": "user", "content": "x"}, {"role": "user", "projectId": "p2"}]},
    {"memoryScope": "global", "messages": [{"role": "user", "projectId": "p3"}]},
    {"messages": [{"role": "assistant", "projectId": "p4"}]},
    {"messages": [{"role": "user", "projectId": "bad id!"}]},
]


def names(tools: list) -> list[str]:
    return [str(tool.get("function", {}).get("name") or "") for tool in tools]


def main() -> int:
    out: dict[str, object] = {}

    out["parallel-hint"] = dc.TOOL_PARALLEL_SYSTEM_HINT

    for index, value in enumerate(EFFORT_CASES):
        out[f"effort::{index}"] = dc.normalize_reasoning_effort(value)

    for index, messages in enumerate(MESSAGE_LISTS):
        out[f"image::{index}"] = dc._has_image_content(messages)

    for index, payload in enumerate(TOOL_PAYLOADS):
        tools = dc.tools_for_payload(payload)
        out[f"tools::{index}"] = names(tools)
        out[f"tools-count::{index}"] = len(tools)

    for index, (payload, mode) in enumerate(FORCE_CASES):
        tools = dc.tools_for_payload(payload) if mode == "own" else dc.tools_for_payload({})
        out[f"force::{index}"] = dc.forced_artifact_tool_name(payload, tools)
        out[f"force-own::{index}"] = mode == "own"

    out["has-pptx::catalog"] = dc.has_create_pptx_tool(dc.tools_for_payload({}))
    out["has-pptx::empty"] = dc.has_create_pptx_tool([])
    out["force-alias::ppt"] = dc.should_force_create_pptx({"messages": PPT_QUERY}) == dc.presentation_intent_requested({"messages": PPT_QUERY})
    out["force-alias::none"] = dc.should_force_create_pptx({}) == dc.presentation_intent_requested({})

    for index, messages in enumerate(ATTACHMENT_CASES):
        out[f"attach::{index}"] = chat_payload.count_payload_attachments(messages)

    for index, payload in enumerate(MEMORY_PAYLOADS):
        out[f"memory-state::{index}"] = mem.empty_memory_state(payload)
        out[f"memory-scope::{index}"] = mem.memory_scope_from_payload(payload)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
