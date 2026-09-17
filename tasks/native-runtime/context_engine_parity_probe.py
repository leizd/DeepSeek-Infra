"""Context-engine parity probe, Python side.

Covers the token engine: `is_cjk`'s ranges through `estimate_tokens`, the message and tool
estimators, the body breakdown, the per-model window lookup, `available_input_tokens`,
`plan_token_budget` and `token_trim` — everything in `context_engine.py` except the
SHA-1-based identity half (`base_context_id`, `build_context_diff`,
`build_engine_diagnostics`), which is deliberately not ported yet.

Settings are patched through a `with_settings` helper because the oracle reads them as
module globals. The small-window cases exist so the trimming path can be exercised without
pushing hundreds of kilobytes through both sides: with `reserve_output_tokens=0`,
`safety_margin_ratio=0.0` and a 40-token window, a handful of messages is already over
budget.

Usage::

    python tasks/native-runtime/context_engine_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example context_engine_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.infra.gateway import context_engine as ce  # noqa: E402

TEXTS = [
    "",
    "a",
    "hello world",
    "中文",
    "中文 with latin",
    "你好，世界！",  # fullwidth punctuation
    "ひらがなカタカナ",
    "한국어",
    "漢字ＡＢＣ",  # fullwidth latin
    "🙂🙂",
    "a" * 400,
    "中" * 7,
    "mixed 中 and a",
    "ｆｕｌｌ\u3000width",
]

MESSAGES = [
    {},
    {"role": "user", "content": "text"},
    {"role": "user", "content": "中文内容"},
    {"role": "user", "content": [{"type": "text", "text": "中文"}]},
    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]},
    {"role": "user", "content": [{"type": "text"}, {"type": "image_url"}]},
    {"role": "assistant", "content": "ok", "tool_calls": [{"function": {"name": "web_search", "arguments": "{\"q\": \"中文\"}"}}]},
    {"role": "assistant", "tool_calls": ["bad", {"function": "not-a-dict"}]},
    {"role": "user", "content": 5},
]

TOOL_ARRAYS = [
    None,
    [],
    "not-a-list",
    [{"type": "function", "function": {"name": "web_search", "description": "搜索"}}],
    [{"type": "function", "function": {"name": f"tool{i}", "description": "描述"}} for i in range(4)],
]

BODIES = [
    {},
    {"messages": []},
    {"messages": [{"role": "system", "content": "role prompt"}]},
    {"messages": [{"role": "system", "content": "role prompt"}, {"role": "system", "content": "dynamic"}]},
    {"messages": [{"role": "system", "content": "a"}, {"role": "user", "content": "b"}, {"role": "system", "content": "c"}]},
    {"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "中文"}], "tools": [{"function": {"name": "t"}}]},
]

MODELS = [None, "", "deepseek-v4-pro", "deepseek-v4-flash", "unknown-model", " deepseek-v4-pro "]

# (enabled, token_aware_trim, reserve, safety, threshold, default_window, min_keep, windows)
SETTINGS_CASES = [
    # (enabled, reserve, safety, threshold, default_window, min_keep, windows)
    # `token_aware_trim` is absent on purpose: it belongs to `context_manager`, not the
    # engine, and the engine does not import it.
    (True, 8_192, 0.05, 75.0, 65_536, 2, {"deepseek-v4-pro": 131_072, "deepseek-v4-flash": 131_072}),
    (True, 0, 0.0, 75.0, 40, 2, {"deepseek-v4-pro": 100}),
    (True, 0, 0.0, 75.0, 40, 4, {"deepseek-v4-pro": 100}),
    (True, 8_192, 0.05, 1.0, 65_536, 2, {"deepseek-v4-pro": 131_072}),
    (True, 100_000, 0.05, 75.0, 65_536, 2, {"deepseek-v4-pro": 131_072}),
    (False, 8_192, 0.05, 75.0, 65_536, 2, {"deepseek-v4-pro": 131_072}),
]

TRIM_MESSAGES = [
    {"role": "system", "content": "stable prefix"},
    {"role": "user", "content": "中" * 20},
    {"role": "assistant", "content": "a" * 40},
    {"role": "user", "content": "b" * 40},
    {"role": "system", "content": "dynamic tail"},
]


def with_settings(case, call):
    names = (
        "CONTEXT_ENGINE_ENABLED",
        "CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS",
        "CONTEXT_ENGINE_SAFETY_MARGIN_RATIO",
        "CONTEXT_ENGINE_COMPRESS_THRESHOLD_PCT",
        "CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW",
        "CONTEXT_ENGINE_MIN_KEEP_MESSAGES",
        "CONTEXT_ENGINE_MODEL_CONTEXT_WINDOWS",
    )
    saved = tuple(getattr(ce, name) for name in names)
    for name, value in zip(names, case):
        setattr(ce, name, value)
    try:
        return call()
    finally:
        for name, value in zip(names, saved):
            setattr(ce, name, value)


def main() -> int:
    out: dict[str, object] = {}

    for index, text in enumerate(TEXTS):
        out[f"tokens::{index}"] = ce.estimate_tokens(text)

    for index, message in enumerate(MESSAGES):
        out[f"message::{index}"] = ce.estimate_message_tokens(message)

    for index, tools in enumerate(TOOL_ARRAYS):
        out[f"tools::{index}"] = ce.estimate_tools_tokens(tools)

    for index, body in enumerate(BODIES):
        out[f"breakdown::{index}"] = ce.estimate_body_breakdown(body)

    for index, model in enumerate(MODELS):
        out[f"window::{index}"] = ce.context_window_for_model(model)
        out[f"available::{index}"] = ce.available_input_tokens(model)

    for index, body in enumerate(BODIES):
        out[f"plan::{index}"] = ce.plan_token_budget(body).to_dict()

    for settings_index, case in enumerate(SETTINGS_CASES):
        for model_index, model in enumerate(MODELS):
            out[f"window-s{settings_index}::{model_index}"] = with_settings(
                case, lambda: ce.context_window_for_model(model)
            )
            out[f"available-s{settings_index}::{model_index}"] = with_settings(
                case, lambda: ce.available_input_tokens(model)
            )
        out[f"plan-s{settings_index}"] = with_settings(
            case, lambda: ce.plan_token_budget(BODIES[5]).to_dict()
        )
        trimmed, dropped = with_settings(case, lambda: ce.token_trim(TRIM_MESSAGES, model=None))
        out[f"trim-s{settings_index}::dropped"] = dropped
        out[f"trim-s{settings_index}::kept"] = len(trimmed)
        out[f"trim-s{settings_index}::roles"] = [item.get("role") for item in trimmed]
        out[f"trim-s{settings_index}::first"] = trimmed[0].get("content") if trimmed else None
        out[f"trim-s{settings_index}::last"] = trimmed[-1].get("content") if trimmed else None
        out[f"trim-s{settings_index}::overhead"] = with_settings(
            case, lambda: ce.token_trim(TRIM_MESSAGES, model=None, fixed_overhead_tokens=6)[1]
        )

    out["trim::empty"] = ce.token_trim([], model=None)[0]
    out["trim::empty-dropped"] = ce.token_trim([], model=None)[1]

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
