"""Budget-manager parity probe, Python side.

Covers the **pure half** of `gateway/budget_manager.py`: the usage extractor, the pricing table
and cost arithmetic, `BudgetPolicy` with its payload override, the in-memory `ToolBudget`, the
scope key, and the cost diagnostic. The SQLite ledger (`connect_db`, `record_spend`,
`daily_spend`, `over_daily_budget`, `should_downgrade`, `budget_status`) is deliberately out of
scope — it is I/O and it is its own slice.

`today()` reads the clock in the oracle, so the probe patches `budget_manager.datetime` with a
stub whose `now(tz)` returns a fixed instant; the Rust side takes the epoch as a parameter.

Usage::

    python tasks/native-runtime/budget_manager_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example budget_manager_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.infra.gateway import budget_manager as bm  # noqa: E402

USAGE_CASES = [
    {},
    {"prompt_tokens": 10, "completion_tokens": 20},
    {"prompt_tokens": "12", "completion_tokens": "3.9"},
    {"prompt_tokens": 3.7, "completion_tokens": 0.2},
    {"prompt_tokens": True, "completion_tokens": False},
    {"prompt_tokens": -5, "completion_tokens": -1},
    {"prompt_tokens": "abc", "promptTokens": 7},
    {"prompt_tokens": "", "promptTokens": 7},
    {"prompt_tokens": None, "promptTokens": 7},
    {"prompt_tokens": [], "promptTokens": 7},
    {"promptTokens": 9},
    {"prompt_tokens": " 5 "},
]

MODELS = [None, "", "deepseek-v4-pro", "deepseek-v4-flash", "unknown", " deepseek-v4-pro ", "DeepSeek-V4-Pro"]

POLICY_PAYLOADS = [
    {},
    {"budget": "not-a-dict"},
    {"budget": {}},
    {"budget": {"max_total_tokens": 100, "max_agent_tokens": "50"}},
    {"budget": {"max_total_tokens": -5, "max_estimated_cost_usd": "1.25"}},
    {"budget": {"max_total_tokens": "abc", "max_tool_calls": 3.9}},
    {"budget": {"max_search_calls": None, "max_tool_calls": []}},
    {"budget": {"policy": "downgrade_to_flash_when_exceeded"}},
    {"budgetPolicy": "downgrade_to_flash_when_exceeded"},
    {"budgetPolicy": "unknown-policy", "budget": {"policy": "none"}},
    {"budgetPolicy": "", "budget": {"policy": "downgrade_to_flash_when_exceeded"}},
]

SCOPE_PAYLOADS = [
    {},
    {"memoryScope": "  project:abc  "},
    {"memoryScope": "中" * 200},
    {"memoryScope": ""},
    {"memoryScope": None},
    {"projectId": "p1"},
    {"projectId": "p1", "activeProjectId": "p2"},
    {"projectId": ""},
    {"projectId": "", "activeProjectId": "p2"},
    {"activeProjectId": 7},
    {"projectId": 0, "activeProjectId": "p2"},
    {"memoryScope": 0, "projectId": "p1"},
]

DIAGNOSTICS = [
    {},
    {"a": 1},
    {"costUsd": 9.9, "other": None},
    {"nested": {"x": 1}},
]

# (max_total_tokens, max_agent_tokens, max_search_calls, max_tool_calls,
#  max_estimated_cost_usd, policy, pricing)
SETTINGS_CASES = [
    (0, 0, 0, 0, 0.0, "none", {"deepseek-v4-pro": (0.55, 2.19), "deepseek-v4-flash": (0.27, 1.10)}),
    (1_000, 500, 3, 4, 1.5, "downgrade_to_flash_when_exceeded", {"deepseek-v4-pro": (1.0, 2.0)}),
    (0, 0, 0, 0, 0.0, "none", {}),
]

TOOL_BUDGET_RUNS = [
    (0, ["", "a", "a", "b"]),
    (2, ["", "a", "b"]),
    (1, ["x"]),
    (-3, ["a"]),
]


def with_settings(case, call):
    names = (
        "BUDGET_MAX_TOTAL_TOKENS",
        "BUDGET_MAX_AGENT_TOKENS",
        "BUDGET_MAX_SEARCH_CALLS",
        "BUDGET_MAX_TOOL_CALLS",
        "BUDGET_MAX_ESTIMATED_COST_USD",
        "BUDGET_POLICY",
        "BUDGET_PRICING",
    )
    saved = tuple(getattr(bm, name) for name in names)
    for name, value in zip(names, case):
        setattr(bm, name, value)
    try:
        return call()
    finally:
        for name, value in zip(names, saved):
            setattr(bm, name, value)


class FixedDatetime:
    """`datetime.now(tz)` at a pinned instant — 2025-09-17T04:05:06Z."""

    instant = datetime(2025, 9, 17, 4, 5, 6, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant


def main() -> int:
    out: dict[str, object] = {}

    out["constants"] = [bm.SPEND_TABLE, bm.DOWNGRADE_POLICY, sorted(bm.VALID_POLICIES)]

    for index, usage in enumerate(USAGE_CASES):
        out[f"usage-int::{index}"] = bm._usage_int(usage, "prompt_tokens", "promptTokens")
        out[f"completion-int::{index}"] = bm._usage_int(usage, "completion_tokens", "completionTokens")

    for index, model in enumerate(MODELS):
        out[f"pricing::{index}"] = list(bm.model_pricing(model))
        out[f"cost::{index}"] = bm.estimate_cost(1_234_567, 765_432, model)

    for index, usage in enumerate(USAGE_CASES):
        out[f"cost-from-usage::{index}"] = bm.cost_from_usage(usage, "deepseek-v4-pro")
        out[f"cost-from-usage-bad::{index}"] = bm.cost_from_usage("not-a-dict", "deepseek-v4-pro")

    out["default-policy"] = bm.default_budget_policy().to_dict()
    out["default-policy::downgrade"] = bm.default_budget_policy().downgrade

    for settings_index, case in enumerate(SETTINGS_CASES):
        out[f"default-policy-s{settings_index}"] = with_settings(
            case, lambda: bm.default_budget_policy().to_dict()
        )
        for payload_index, payload in enumerate(POLICY_PAYLOADS):
            policy = with_settings(case, lambda: bm.budget_policy_from_payload(payload))
            out[f"policy-s{settings_index}::{payload_index}"] = policy.to_dict()
            out[f"policy-downgrade-s{settings_index}::{payload_index}"] = policy.downgrade
        for index, model in enumerate(MODELS):
            out[f"pricing-s{settings_index}::{index}"] = with_settings(
                case, lambda: list(bm.model_pricing(model))
            )
            out[f"cost-s{settings_index}::{index}"] = with_settings(
                case, lambda: bm.estimate_cost(1_000_000, 1_000_000, model)
            )

    for index, (limit, keys) in enumerate(TOOL_BUDGET_RUNS):
        budget = bm.ToolBudget(total_limit=limit)
        out[f"tool-budget::{index}"] = {
            "totalLimit": budget.total_limit,
            "attempts": [budget.try_consume(key) for key in keys],
            "used": budget.used,
            "usedByKey": budget.used_by_key,
        }

    saved_datetime = bm.datetime
    bm.datetime = FixedDatetime
    try:
        out["today"] = bm.today()
    finally:
        bm.datetime = saved_datetime

    for index, payload in enumerate(SCOPE_PAYLOADS):
        out[f"scope::{index}"] = bm.budget_scope(payload)

    for index, diagnostics in enumerate(DIAGNOSTICS):
        for usage_index, usage in enumerate(USAGE_CASES[:4]):
            out[f"diagnostics::{index}::{usage_index}"] = bm.diagnostics_with_cost(
                diagnostics, usage, "deepseek-v4-flash"
            )

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
