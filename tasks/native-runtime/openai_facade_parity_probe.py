"""OpenAI facade payload-parity probe, Python side.

`POST /v1/chat/completions` is a facade: `openai_to_internal_payload` translates the
OpenAI body into the internal chat payload and `call_deepseek` ->
`build_deepseek_request` builds the upstream body from *that*. So the translation is
part of the public contract, and the native route has to reproduce it exactly --
including the fields it **drops**.

This probe pins the whole accept/refuse set of `openai_to_internal_payload`
(`deepseek_infra/infra/gateway/openai_api.py:29`) against the Rust port in
`rust/crates/deepseek-gateway/src/openai_facade.rs`.

The corpus is written to reach every branch, and each case is labelled with what it is
for, so a diff says which behaviour moved rather than only that bytes differ:

- the six fields the facade forwards, and the seven it drops;
- `body.get("model") or settings.default_model` -- the falsy set, then alias
  normalization (case, underscores, spaces, unknown names, a truthy bool);
- `"stream": bool(body.get("stream"))` -- Python truthiness, where the string
  `"false"` is **true**;
- `isinstance(temperature, (int, float)) and not isinstance(temperature, bool)`;
- the two refusals, as `{message, code, status}`.

Usage::

    python tasks/native-runtime/openai_facade_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-gateway --example openai_facade_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.core.config import DEFAULT_MODEL, MODEL_ALIASES  # noqa: E402
from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.gateway import openai_api  # noqa: E402

BASE_URL = "http://127.0.0.1:8000"

TURNS: list[dict[str, Any]] = [{"role": "user", "content": " hi "}]

CASES: list[tuple[str, Any]] = [
    # --- what the facade forwards --------------------------------------------------
    ("minimal", {"model": "deepseek-v4-pro", "messages": TURNS}),
    ("default-model-when-absent", {"messages": TURNS}),
    ("falsy-model-empty-string", {"model": "", "messages": TURNS}),
    ("falsy-model-null", {"model": None, "messages": TURNS}),
    ("falsy-model-zero", {"model": 0, "messages": TURNS}),
    ("falsy-model-false", {"model": False, "messages": TURNS}),
    ("falsy-model-empty-list", {"model": [], "messages": TURNS}),
    ("falsy-model-empty-dict", {"model": {}, "messages": TURNS}),
    ("truthy-model-bool", {"model": True, "messages": TURNS}),
    ("truthy-model-number", {"model": 7, "messages": TURNS}),
    ("alias-exact", {"model": "deepseek-v4-pro", "messages": TURNS}),
    ("alias-uppercase", {"model": "DEEPSEEK-V4-PRO", "messages": TURNS}),
    ("alias-short", {"model": "fast", "messages": TURNS}),
    ("alias-short-expert", {"model": "expert", "messages": TURNS}),
    ("alias-underscores", {"model": "v4_pro", "messages": TURNS}),
    ("alias-spaces-stripped", {"model": "deep seek v4 flash", "messages": TURNS}),
    ("alias-surrounding-space", {"model": "  flash  ", "messages": TURNS}),
    ("alias-unknown-passthrough", {"model": "my-model", "messages": TURNS}),
    ("alias-unknown-keeps-case", {"model": "My Model", "messages": TURNS}),
    ("stream-absent", {"messages": TURNS}),
    ("stream-true", {"messages": TURNS, "stream": True}),
    ("stream-false", {"messages": TURNS, "stream": False}),
    ("stream-string-false-is-true", {"messages": TURNS, "stream": "false"}),
    ("stream-empty-string-is-false", {"messages": TURNS, "stream": ""}),
    ("stream-zero", {"messages": TURNS, "stream": 0}),
    ("stream-one", {"messages": TURNS, "stream": 1}),
    ("stream-empty-list", {"messages": TURNS, "stream": []}),
    ("stream-null", {"messages": TURNS, "stream": None}),
    ("temperature-float", {"messages": TURNS, "temperature": 0.5}),
    ("temperature-zero", {"messages": TURNS, "temperature": 0}),
    ("temperature-negative", {"messages": TURNS, "temperature": -1}),
    ("temperature-large-int", {"messages": TURNS, "temperature": 3}),
    ("temperature-bool-true-ignored", {"messages": TURNS, "temperature": True}),
    ("temperature-bool-false-ignored", {"messages": TURNS, "temperature": False}),
    ("temperature-string-ignored", {"messages": TURNS, "temperature": "0.5"}),
    ("temperature-list-ignored", {"messages": TURNS, "temperature": [0.5]}),
    ("temperature-null-ignored", {"messages": TURNS, "temperature": None}),
    ("messages-forwarded-verbatim", {
        "messages": [
            {"role": "user", "content": "  "},
            {"role": "tool", "content": "x"},
            "not-an-object",
            {"role": "assistant", "content": None},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ]
    }),
    ("messages-extra-turn-fields-kept", {
        "messages": [{"role": "user", "content": "hi", "name": "n", "tool_call_id": "t"}]
    }),
    # --- what the facade drops ------------------------------------------------------
    ("drops-tools", {
        "model": "fast",
        "messages": TURNS,
        "tools": [{"type": "function", "function": {"name": "t", "parameters": {}}}],
        "tool_choice": "auto",
    }),
    ("drops-sampling-and-reasoning", {
        "messages": TURNS,
        "max_tokens": 16,
        "top_p": 0.5,
        "reasoning_effort": "high",
        "thinking": {"type": "enabled"},
    }),
    ("drops-everything-at-once", {
        "model": "expert",
        "messages": TURNS,
        "tools": [],
        "tool_choice": "none",
        "max_tokens": 1,
        "top_p": 1,
        "reasoning_effort": "minimal",
        "thinking": {"type": "enabled"},
        "temperature": 0.25,
    }),
    # --- the refusals ---------------------------------------------------------------
    ("refuse-empty-object", {}),
    ("refuse-no-messages", {"model": "deepseek-v4-pro"}),
    ("refuse-empty-messages", {"messages": []}),
    ("refuse-messages-string", {"messages": "hi"}),
    ("refuse-messages-object", {"messages": {"role": "user"}}),
    ("refuse-messages-null", {"messages": None}),
    ("refuse-messages-number", {"messages": 3}),
    ("refuse-body-list", []),
    ("refuse-body-string", "x"),
    ("refuse-body-number", 1),
    ("refuse-body-null", None),
]


def view(case: Any) -> dict[str, Any]:
    try:
        payload = openai_api.openai_to_internal_payload(case, local_base_url=BASE_URL)
    except AppError as exc:
        return {"error": str(exc), "code": exc.code.value, "status": exc.status}
    return {"ok": json.dumps(payload, ensure_ascii=False, sort_keys=True)}


def main() -> int:
    # The corpus carries non-ASCII; pin the stream so the documented redirect is UTF-8
    # on Windows (AGENTS.md), where `sys.stdout` defaults to a legacy code page.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")

    out = {
        "probe::base-url": BASE_URL,
        "probe::default-model": str(DEFAULT_MODEL),
        "probe::aliases": dict(MODEL_ALIASES),
    }
    for label, case in CASES:
        out[f"case::{label}"] = view(case)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
