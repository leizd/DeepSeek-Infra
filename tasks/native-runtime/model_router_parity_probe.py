"""Model-router parity probe, Python side.

Covers `gateway/model_router.py` and the four names it consumes from `edge_inference`
(three query-shape patterns plus `chat_messages_from_payload` / `has_image_attachment`).

The three pattern **texts** are compared as strings, not just through behaviour: they contain
CJK literals that would otherwise be transcribed blind into Rust, and a wrong character would
otherwise show up only as a mysterious routing difference.

Usage::

    python tasks/native-runtime/model_router_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example model_router_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.infra.gateway import edge_inference as edge  # noqa: E402
from deepseek_infra.infra.gateway import model_router as mr  # noqa: E402

QUERIES = [
    "",
    "   ",
    "帮我写代码",
    "```python\nprint(1)",
    "how to fix this traceback",
    "做一份 PPT",
    "看看 mind map",
    "你好",
    "please explain",
    "short question?",
    "a" * 1300,
    "解释" + "啊" * 500,
    "啊" * 200,
]

IMAGE_PAYLOADS = [
    {},
    {"messages": "not-a-list"},
    {"messages": [{"role": "user"}]},
    {"messages": [{"role": "user", "attachments": "x"}]},
    {"messages": [{"role": "user", "attachments": [{"imageData": "data:image/png;base64,AAAA"}]}]},
    {"messages": [{"role": "user", "attachments": [{"imageData": "http://x/y.png"}]}]},
    {"messages": [{"role": "user", "attachments": [{"imageData": ""}, {"imageData": "data:image/jpeg;base64,B"}]}]},
    {"messages": ["not-a-dict", {"role": "user", "attachments": [{"imageData": "data:image/"}]}]},
    {"messages": [{"role": "user", "attachments": [{"imageData": 5}]}]},
]

AUTO_PAYLOADS = [
    {},
    {"model": "auto"},
    {"model": "AUTO"},
    {"model": " auto "},
    {"model": "auto", "autoRoute": True},
    {"autoRoute": True},
    {"autoRoute": 1},
    {"autoRoute": "true"},
    {"model": "deepseek-v4-pro", "cascade": True},
    {"cascade": 1},
    {"cascade": True, "agentMode": True},
    {"cascade": True, "judge": True},
]

ROUTE_PAYLOADS = [
    {},
    {"model": "flash"},
    {"model": "v4pro"},
    {"model": "deepseek_v4_flash"},
    {"model": "unknown-model"},
    {"model": ""},
    {"model": 0},
    {"model": None},
    {"model": "auto", "messages": [{"role": "user", "content": "你好"}]},
    {"model": "auto", "messages": [{"role": "user", "content": "帮我写代码"}]},
    {"model": "auto", "messages": [{"role": "user", "content": "啊" * 200}]},
    {"model": "auto", "messages": [{"role": "user", "content": "帮我写代码"}], "attachments": []},
    {"model": "auto", "messages": [{"role": "user", "content": "x", "attachments": [{"imageData": "data:image/png;base64,AA"}]}]},
    {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "x", "attachments": [{"imageData": "data:image/png;base64,AA"}]}]},
    {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "x", "attachments": [{"imageData": "data:image/png;base64,AA"}]}]},
]

QUALITY_CASES = [
    ("", 80, False),
    ("   ", 80, False),
    ("太短了", 80, False),
    ("a" * 200, 80, False),
    ("很抱歉，我无法回答这个问题。" + "a" * 200, 80, False),
    ("我不确定这一点，而且无法确定答案。" + "a" * 200, 80, False),
    ("我不确定这一点。" + "a" * 200, 80, False),
    ("结论见 [^W1]，细节略。" + "a" * 200, 80, True),
    ("结论见文档，细节略。" + "a" * 200, 80, True),
    ("The answer is [^f2]." + "a" * 200, 80, True),
    ("I cannot help with that." + "a" * 200, 80, False),
    ("As an AI language model, I am unable to help." + "a" * 200, 80, False),
    ("a" * 200, 500, False),
]

# (enabled, cascade_enabled, judge_enabled, judge_model, judge_threshold, draft_model,
#  refine_model, cascade_min_chars, cost_budget_tokens)
SETTINGS_CASES = [
    (True, True, False, "deepseek-v4-flash", 0.6, "deepseek-v4-flash", "deepseek-v4-pro", 80, 0),
    (False, True, False, "deepseek-v4-flash", 0.6, "deepseek-v4-flash", "deepseek-v4-pro", 80, 0),
    (True, False, True, "deepseek-v4-pro", 0.9, "ollama/qwen", "deepseek-v4-pro", 10, 50),
]


def with_settings(case, call):
    names = (
        "MODEL_ROUTER_ENABLED",
        "MODEL_ROUTER_CASCADE_ENABLED",
        "MODEL_ROUTER_JUDGE_ENABLED",
        "MODEL_ROUTER_JUDGE_MODEL",
        "MODEL_ROUTER_JUDGE_THRESHOLD",
        "MODEL_ROUTER_DRAFT_MODEL",
        "MODEL_ROUTER_REFINE_MODEL",
        "MODEL_ROUTER_CASCADE_MIN_CHARS",
        "MODEL_ROUTER_COST_BUDGET_TOKENS",
    )
    saved = tuple(getattr(mr, name) for name in names)
    for name, value in zip(names, case):
        setattr(mr, name, value)
    try:
        return call()
    finally:
        for name, value in zip(names, saved):
            setattr(mr, name, value)


def main() -> int:
    out: dict[str, object] = {}

    out["pattern::complex"] = edge.COMPLEX_QUERY_RE.pattern
    out["pattern::artifact"] = edge.ARTIFACT_QUERY_RE.pattern
    out["pattern::simple"] = edge.SIMPLE_TASK_RE.pattern
    out["pattern::citation"] = mr._CITATION_RE.pattern
    out["markers::uncertainty"] = list(mr.UNCERTAINTY_MARKERS)
    out["markers::refusal"] = list(mr.REFUSAL_MARKERS)

    for index, query in enumerate(QUERIES):
        out[f"complexity::{index}"] = mr.query_complexity(query)

    for index, payload in enumerate(IMAGE_PAYLOADS):
        out[f"messages::{index}"] = edge.chat_messages_from_payload(payload)
        out[f"has-image::{index}"] = edge.has_image_attachment(payload)

    for settings_index, case in enumerate(SETTINGS_CASES):
        out[f"status-s{settings_index}"] = with_settings(case, mr.router_status)
        for payload_index, payload in enumerate(AUTO_PAYLOADS):
            out[f"auto-s{settings_index}::{payload_index}"] = with_settings(
                case, lambda: mr.is_auto_request(payload)
            )
            out[f"cascade-req-s{settings_index}::{payload_index}"] = with_settings(
                case, lambda: mr.cascade_requested(payload)
            )
            plan = with_settings(case, lambda: mr.cascade_plan(payload).to_dict())
            out[f"cascade-plan-s{settings_index}::{payload_index}"] = plan
        for payload_index, payload in enumerate(ROUTE_PAYLOADS):
            for budget_used in (0, 40):
                decision = with_settings(
                    case, lambda: mr.route_request(payload, budget_used=budget_used).to_dict()
                )
                out[f"route-s{settings_index}::{payload_index}::b{budget_used}"] = decision

    for index, (content, min_chars, require_citations) in enumerate(QUALITY_CASES):
        out[f"gate::{index}"] = mr.quality_gate(
            content, min_chars=min_chars, require_citations=require_citations
        ).to_dict()

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
