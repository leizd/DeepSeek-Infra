"""Budget-ledger parity probe, Python side.

Covers the behavioural half of `budget_manager.py` lines 183-371: the spend view a row becomes,
the row `record_spend` writes, the four threshold checks, `should_downgrade`,
`record_request_spend` and `budget_status`.

Two deliberate stubs, matching what the Rust side injects:

- **the connection.** Group B patches `connect_db` to a real in-memory database and points
  `BUDGET_DB` at a file that exists, so the oracle's own SQL runs here. On the Rust side the
  same upsert semantics are simulated by a map, because the SQL statements and the connection
  are **not part of this slice** — they belong to the store. What the two sides therefore
  compare is the shaping around the SQL (floors, rounding, the timestamp) and the read-back
  view, not the statements themselves.
- **`daily_spend`**, in group A, so the threshold and status logic can be driven from fixed
  spend views instead of from a database.

Usage::

    python tasks/native-runtime/budget_ledger_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example budget_ledger_parity_probe > ../rust.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from deepseek_infra.infra.gateway import budget_manager as bm  # noqa: E402

SPEND_CASES: list[dict[str, Any]] = [
    {"totalTokens": 0, "costUsd": 0.0, "searchCalls": 0, "toolCalls": 0},
    {"totalTokens": 100, "costUsd": 0.5, "searchCalls": 2, "toolCalls": 3},
    {"totalTokens": 1_000, "costUsd": 1.5, "searchCalls": 3, "toolCalls": 4},
    {"totalTokens": 999, "costUsd": 1.499_999, "searchCalls": 2, "toolCalls": 3},
]

POLICY_CASES: list[dict[str, Any]] = [
    {"max_total_tokens": 0, "max_agent_tokens": 0, "max_search_calls": 0, "max_tool_calls": 0, "max_estimated_cost_usd": 0.0, "policy": "none"},
    {"max_total_tokens": 1_000, "max_search_calls": 3, "max_tool_calls": 4, "max_estimated_cost_usd": 1.5, "policy": "none"},
    {"max_total_tokens": 100, "max_search_calls": 0, "max_tool_calls": 0, "max_estimated_cost_usd": 0.0, "policy": "none"},
    {"max_total_tokens": 1, "max_search_calls": 1, "max_tool_calls": 1, "max_estimated_cost_usd": 0.01, "policy": "downgrade_to_flash_when_exceeded"},
    {"max_total_tokens": -5, "max_search_calls": -1, "max_tool_calls": -1, "max_estimated_cost_usd": -1.0, "policy": "none"},
    {"max_total_tokens": 0, "max_search_calls": 0, "max_tool_calls": 0, "max_estimated_cost_usd": 0.0, "policy": "downgrade_to_flash_when_exceeded"},
]

RECORD_CASES: list[dict[str, Any]] = [
    {"scope": "global", "prompt_tokens": 10, "completion_tokens": 20, "cost_usd": 4.9e-05, "model_calls": 1},
    {"scope": "global", "prompt_tokens": 5, "completion_tokens": 0, "cost_usd": 0.000_075_5, "model_calls": 1, "tool_calls": 2},
    {"scope": "项目:1", "prompt_tokens": -5, "completion_tokens": 3.7, "cost_usd": -1.0, "model_calls": -2, "search_calls": "2"},
    {"scope": "", "prompt_tokens": " 7 ", "completion_tokens": "abc", "cost_usd": "0.5", "model_calls": 0},
]

REQUEST_SPEND_CASES: list[Any] = [
    ({}, None, {}, 0, 0),
    ({"memoryScope": "project:abc"}, "deepseek-v4-pro", {"prompt_tokens": 100, "completion_tokens": 50}, 2, 1),
    ({}, "deepseek-v4-flash", {"promptTokens": "30"}, -3, 0),
    ({"projectId": "p1"}, "unknown", "not-a-dict", 1, 1),
]


def policy(case: dict[str, Any]) -> Any:
    return bm.BudgetPolicy(
        max_total_tokens=case["max_total_tokens"],
        max_agent_tokens=case.get("max_agent_tokens", 0),
        max_search_calls=case["max_search_calls"],
        max_tool_calls=case["max_tool_calls"],
        max_estimated_cost_usd=case["max_estimated_cost_usd"],
        policy=case["policy"],
    )


def spend_view(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": "global",
        "day": "2026-09-18",
        "promptTokens": case["totalTokens"],
        "completionTokens": 0,
        "totalTokens": case["totalTokens"],
        "costUsd": case["costUsd"],
        "modelCalls": 1,
        "searchCalls": case["searchCalls"],
        "toolCalls": case["toolCalls"],
    }


def main() -> int:
    out: dict[str, Any] = {}

    # --- A: thresholds, the downgrade decision and the status envelope -----------------
    daily_calls = {"count": 0}
    failures = {"connect": False}

    def stubbed_daily_spend(scope: str = "global", day: str | None = None) -> dict[str, Any]:
        daily_calls["count"] += 1
        return CURRENT_SPEND[0]

    CURRENT_SPEND: list[dict[str, Any]] = [spend_view(SPEND_CASES[0])]
    real_daily_spend = bm.daily_spend
    real_budget_db = bm.BUDGET_DB
    # `budget_status` renders the path, so both sides use the same relative one, and a file
    # has to exist for the read path to be taken (the connection itself is stubbed).
    bm.BUDGET_DB = Path("probe-budget.db")
    probe_db = bm.BUDGET_DB
    probe_db.write_text("", encoding="utf-8")
    bm.daily_spend = stubbed_daily_spend
    try:
        for spend_index, spend_case in enumerate(SPEND_CASES):
            CURRENT_SPEND[0] = spend_view(spend_case)
            for policy_index, policy_case in enumerate(POLICY_CASES):
                over = bm.over_daily_budget("global", policy(policy_case))
                out[f"over::{spend_index}::{policy_index}"] = over
                out[f"downgrade::{spend_index}::{policy_index}"] = bm.should_downgrade(
                    "global", policy(policy_case)
                )

        for scope_index, scope in enumerate(["global", "project:abc", ""]):
            daily_calls["count"] = 0
            status = bm.budget_status(scope)
            out[f"status::{scope_index}"] = status
            # `today` and `overBudget` each read the ledger: two reads, not one.
            out[f"status-reads::{scope_index}"] = daily_calls["count"]
    finally:
        bm.daily_spend = real_daily_spend

    # --- B: the row, the upsert and the read-back -------------------------------------
    saved = (bm.connect_db, bm.BUDGET_DIR, bm.BUDGET_TRACKING_ENABLED, bm.today)
    temp_dir = Path(tempfile.mkdtemp(prefix="budget-probe-"))
    try:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        bm.connect_db = lambda: connection
        bm.BUDGET_DIR = temp_dir
        bm.BUDGET_TRACKING_ENABLED = True
        bm.today = lambda: "2026-09-18"

        out["read::missing-row"] = bm.daily_spend("global")
        out["read::missing-scope"] = bm.daily_spend("project:never")

        for index, case in enumerate(RECORD_CASES):
            bm.record_spend(
                case["scope"],
                prompt_tokens=case["prompt_tokens"],
                completion_tokens=case["completion_tokens"],
                cost_usd=case["cost_usd"],
                model_calls=case["model_calls"],
                search_calls=case.get("search_calls", 0),
                tool_calls=case.get("tool_calls", 0),
            )
            out[f"record::{index}"] = bm.daily_spend(case["scope"])

        # The same row again: an upsert accumulates rather than replacing.
        bm.record_spend("global", prompt_tokens=1, completion_tokens=1, cost_usd=1e-06, model_calls=1)
        out["record::accumulated"] = bm.daily_spend("global")
        out["record::other-day"] = bm.daily_spend("global", "2026-01-01")

        # Recording is gated; reading still works while it is off.
        bm.BUDGET_TRACKING_ENABLED = False
        bm.record_spend("gated", prompt_tokens=9, completion_tokens=9, cost_usd=1.0, model_calls=1)
        out["gated::write"] = bm.daily_spend("gated")
        bm.BUDGET_TRACKING_ENABLED = True

        # A database file that is not there is the empty view, not an error.
        bm.BUDGET_DB = temp_dir / "absent.db"
        out["absent::read"] = bm.daily_spend("global")

        # A read that blows up is stored, not raised: the empty view plus `lastError`.
        bm.BUDGET_DB = probe_db
        bm.connect_db = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
        failed = bm.daily_spend("global")
        out["failed::read"] = failed
        out["failed::status"] = bm.budget_status("global")
        out["failed::error"] = bm._last_error
        bm.connect_db = lambda: connection

        for index, (payload, model, usage, tool_calls, search_calls) in enumerate(REQUEST_SPEND_CASES):
            view = bm.record_request_spend(
                payload, model, usage, tool_calls=tool_calls, search_calls=search_calls
            )
            out[f"request-spend::{index}"] = view
        out["request-spend::scope-total"] = bm.daily_spend("global")
        out["request-spend::project-total"] = bm.daily_spend("project:abc")
        out["request-spend::p1-total"] = bm.daily_spend("project:p1")
        out["request-spend::free-total"] = bm.daily_spend("global", "2026-01-01")
    finally:
        bm.connect_db, bm.BUDGET_DIR, bm.BUDGET_TRACKING_ENABLED, bm.today = saved
        bm.BUDGET_DB = real_budget_db
        probe_db.unlink(missing_ok=True)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
