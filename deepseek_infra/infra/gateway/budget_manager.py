"""Cost & Token Budget Manager: pricing, policy, tool budget, daily spend.

Cost governance for the runtime: estimate USD cost from token usage with a
per-model price table, parse a unified :class:`BudgetPolicy` (max tokens / agent
tokens / search calls / tool calls / estimated cost), enforce a tool-call budget
(:class:`ToolBudget`, mirroring ``SearchBudget``), and persist per-scope **daily**
spend in a local SQLite ledger so a project can be held to a daily budget and
the model router can downgrade to the cheap model when a scope is over budget.

Pure helpers (pricing/policy/tool budget) have no I/O; the daily ledger is the
only persistent part. Recording is gated by ``BUDGET_TRACKING_ENABLED`` and never
runs on a semantic-cache hit (no real upstream cost).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from deepseek_infra.core.config import (
    BUDGET_DB,
    BUDGET_DIR,
    BUDGET_MAX_AGENT_TOKENS,
    BUDGET_MAX_ESTIMATED_COST_USD,
    BUDGET_MAX_SEARCH_CALLS,
    BUDGET_MAX_TOOL_CALLS,
    BUDGET_MAX_TOTAL_TOKENS,
    BUDGET_POLICY,
    BUDGET_PRICING,
    BUDGET_TRACKING_ENABLED,
)

logger = logging.getLogger("deepseek_infra.budget")

SPEND_TABLE = "budget_daily"
DOWNGRADE_POLICY = "downgrade_to_flash_when_exceeded"
VALID_POLICIES = {"none", DOWNGRADE_POLICY}

_db_lock = threading.RLock()
_last_error = ""


def set_last_error(message: str) -> None:
    global _last_error
    _last_error = message
    if message:
        logger.warning("budget_error", extra={"detail": message})


def _usage_int(usage: dict[str, Any], *names: str) -> int:
    for name in names:
        raw = usage.get(name)
        if raw is None or raw == "":
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 0


# --- Pricing / cost estimation --------------------------------------------------

def model_pricing(model: str | None) -> tuple[float, float]:
    """USD per 1M tokens ``(input, output)`` for a model; local/unknown → free."""
    value = BUDGET_PRICING.get(str(model or "").strip())
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return float(value[0]), float(value[1])
    return 0.0, 0.0


def estimate_cost(prompt_tokens: int, completion_tokens: int, model: str | None) -> float:
    input_price, output_price = model_pricing(model)
    cost = (max(0, int(prompt_tokens)) / 1_000_000) * input_price + (max(0, int(completion_tokens)) / 1_000_000) * output_price
    return round(cost, 6)


def cost_from_usage(usage: Any, model: str | None) -> float:
    data = usage if isinstance(usage, dict) else {}
    return estimate_cost(
        _usage_int(data, "prompt_tokens", "promptTokens"),
        _usage_int(data, "completion_tokens", "completionTokens"),
        model,
    )


# --- Budget policy --------------------------------------------------------------

@dataclass(frozen=True)
class BudgetPolicy:
    max_total_tokens: int
    max_agent_tokens: int
    max_search_calls: int
    max_tool_calls: int
    max_estimated_cost_usd: float
    policy: str

    @property
    def downgrade(self) -> bool:
        return self.policy == DOWNGRADE_POLICY

    def to_dict(self) -> dict[str, Any]:
        return {
            "maxTotalTokens": self.max_total_tokens,
            "maxAgentTokens": self.max_agent_tokens,
            "maxSearchCalls": self.max_search_calls,
            "maxToolCalls": self.max_tool_calls,
            "maxEstimatedCostUsd": self.max_estimated_cost_usd,
            "policy": self.policy,
        }


def default_budget_policy() -> BudgetPolicy:
    return BudgetPolicy(
        max_total_tokens=BUDGET_MAX_TOTAL_TOKENS,
        max_agent_tokens=BUDGET_MAX_AGENT_TOKENS,
        max_search_calls=BUDGET_MAX_SEARCH_CALLS,
        max_tool_calls=BUDGET_MAX_TOOL_CALLS,
        max_estimated_cost_usd=BUDGET_MAX_ESTIMATED_COST_USD,
        policy=BUDGET_POLICY,
    )


def budget_policy_from_payload(payload: dict[str, Any]) -> BudgetPolicy:
    base = default_budget_policy()
    raw = payload.get("budget") if isinstance(payload, dict) else None
    block = raw if isinstance(raw, dict) else {}

    def _int(key: str, fallback: int) -> int:
        try:
            return max(0, int(block[key]))
        except (KeyError, TypeError, ValueError):
            return fallback

    def _float(key: str, fallback: float) -> float:
        try:
            return max(0.0, float(block[key]))
        except (KeyError, TypeError, ValueError):
            return fallback

    policy = str(payload.get("budgetPolicy") or block.get("policy") or base.policy)
    return BudgetPolicy(
        max_total_tokens=_int("max_total_tokens", base.max_total_tokens),
        max_agent_tokens=_int("max_agent_tokens", base.max_agent_tokens),
        max_search_calls=_int("max_search_calls", base.max_search_calls),
        max_tool_calls=_int("max_tool_calls", base.max_tool_calls),
        max_estimated_cost_usd=_float("max_estimated_cost_usd", base.max_estimated_cost_usd),
        policy=policy if policy in VALID_POLICIES else base.policy,
    )


# --- Tool-call budget (mirrors SearchBudget) ------------------------------------

class ToolBudget:
    """Thread-safe per-run tool-call budget. ``total_limit <= 0`` means unlimited."""

    def __init__(self, *, total_limit: int) -> None:
        self.total_limit = max(0, int(total_limit))
        self.used = 0
        self.used_by_key: dict[str, int] = {}
        self._lock = threading.Lock()

    def try_consume(self, key: str = "default") -> bool:
        normalized = str(key or "default")
        with self._lock:
            if self.total_limit > 0 and self.used >= self.total_limit:
                return False
            self.used += 1
            self.used_by_key[normalized] = self.used_by_key.get(normalized, 0) + 1
            return True


# --- Per-scope daily spend ledger ----------------------------------------------

def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def budget_scope(payload: dict[str, Any]) -> str:
    raw = str(payload.get("memoryScope") or "").strip()
    if raw:
        return raw[:120]
    project_id = str(payload.get("projectId") or payload.get("activeProjectId") or "").strip()
    if project_id:
        return f"project:{project_id}"[:120]
    return "global"


def connect_db() -> sqlite3.Connection:
    BUDGET_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(BUDGET_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SPEND_TABLE} (
            scope TEXT NOT NULL,
            day TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            model_calls INTEGER NOT NULL DEFAULT 0,
            search_calls INTEGER NOT NULL DEFAULT 0,
            tool_calls INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope, day)
        )
        """
    )


def record_spend(
    scope: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    model_calls: int = 0,
    search_calls: int = 0,
    tool_calls: int = 0,
) -> None:
    """Accumulate today's spend for a scope (daily rows reset implicitly by date)."""
    if not BUDGET_TRACKING_ENABLED:
        return
    day = today()
    try:
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            conn.execute(
                f"""
                INSERT INTO {SPEND_TABLE}
                    (scope, day, prompt_tokens, completion_tokens, cost_usd, model_calls, search_calls, tool_calls, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, day) DO UPDATE SET
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    cost_usd = cost_usd + excluded.cost_usd,
                    model_calls = model_calls + excluded.model_calls,
                    search_calls = search_calls + excluded.search_calls,
                    tool_calls = tool_calls + excluded.tool_calls,
                    updated_at = excluded.updated_at
                """,
                (
                    str(scope or "global"),
                    day,
                    max(0, int(prompt_tokens)),
                    max(0, int(completion_tokens)),
                    round(max(0.0, float(cost_usd)), 6),
                    max(0, int(model_calls)),
                    max(0, int(search_calls)),
                    max(0, int(tool_calls)),
                    datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                ),
            )
    except Exception as exc:
        set_last_error(f"budget record failed: {exc}")


def daily_spend(scope: str = "global", day: str | None = None) -> dict[str, Any]:
    resolved_day = day or today()
    empty = {
        "scope": str(scope or "global"),
        "day": resolved_day,
        "promptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
        "costUsd": 0.0,
        "modelCalls": 0,
        "searchCalls": 0,
        "toolCalls": 0,
    }
    if not BUDGET_DB.exists():
        return empty
    try:
        with _db_lock, connect_db() as conn:
            initialize_schema(conn)
            row = conn.execute(
                f"SELECT * FROM {SPEND_TABLE} WHERE scope = ? AND day = ?",
                (str(scope or "global"), resolved_day),
            ).fetchone()
    except Exception as exc:
        set_last_error(f"budget read failed: {exc}")
        return empty
    if row is None:
        return empty
    prompt = int(row["prompt_tokens"] or 0)
    completion = int(row["completion_tokens"] or 0)
    return {
        "scope": str(row["scope"]),
        "day": str(row["day"]),
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": prompt + completion,
        "costUsd": round(float(row["cost_usd"] or 0.0), 6),
        "modelCalls": int(row["model_calls"] or 0),
        "searchCalls": int(row["search_calls"] or 0),
        "toolCalls": int(row["tool_calls"] or 0),
    }


def over_daily_budget(scope: str, policy: BudgetPolicy, day: str | None = None) -> dict[str, Any]:
    spend = daily_spend(scope, day)
    reasons: list[str] = []
    if policy.max_total_tokens > 0 and spend["totalTokens"] >= policy.max_total_tokens:
        reasons.append("max_total_tokens")
    if policy.max_estimated_cost_usd > 0 and spend["costUsd"] >= policy.max_estimated_cost_usd:
        reasons.append("max_estimated_cost_usd")
    if policy.max_search_calls > 0 and spend["searchCalls"] >= policy.max_search_calls:
        reasons.append("max_search_calls")
    if policy.max_tool_calls > 0 and spend["toolCalls"] >= policy.max_tool_calls:
        reasons.append("max_tool_calls")
    return {"exceeded": bool(reasons), "reasons": reasons, "spend": spend}


def should_downgrade(scope: str, policy: BudgetPolicy, day: str | None = None) -> bool:
    if not policy.downgrade:
        return False
    return over_daily_budget(scope, policy, day)["exceeded"]


def record_request_spend(
    payload: dict[str, Any],
    model: str | None,
    usage: Any,
    *,
    tool_calls: int = 0,
    search_calls: int = 0,
) -> dict[str, Any]:
    """Record one upstream model call's spend and return its cost view."""
    data = usage if isinstance(usage, dict) else {}
    cost = cost_from_usage(data, model)
    scope = budget_scope(payload)
    record_spend(
        scope,
        prompt_tokens=_usage_int(data, "prompt_tokens", "promptTokens"),
        completion_tokens=_usage_int(data, "completion_tokens", "completionTokens"),
        cost_usd=cost,
        model_calls=1,
        tool_calls=max(0, int(tool_calls)),
        search_calls=max(0, int(search_calls)),
    )
    return {"costUsd": cost, "scope": scope, "model": str(model or "")}


def diagnostics_with_cost(diagnostics: dict[str, Any], usage: Any, model: str | None) -> dict[str, Any]:
    result = dict(diagnostics)
    result["costUsd"] = cost_from_usage(usage, model)
    return result


def budget_status(scope: str = "global", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = budget_policy_from_payload(payload or {})
    return {
        "enabled": BUDGET_TRACKING_ENABLED,
        "databasePath": str(BUDGET_DB),
        "pricing": {model: {"inputPerMTok": price[0], "outputPerMTok": price[1]} for model, price in BUDGET_PRICING.items()},
        "policy": policy.to_dict(),
        "scope": str(scope or "global"),
        "today": daily_spend(scope),
        "overBudget": over_daily_budget(scope, policy),
        "lastError": _last_error,
    }
