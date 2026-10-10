"""Automation Runtime safety gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from deepseek_infra.core import config
from deepseek_infra.infra.automation import history
from deepseek_infra.infra.browser import safety as browser_safety

ALLOW = "allow"
DENY = "deny"
NEEDS_CONFIRMATION = "needs_confirmation"
BROWSER_ACTIONS = {"browser_snapshot", "browser_check"}
LOCAL_ACTIONS = {"run_skill", "project_summary", "media_process", "create_artifact", "save_item", "export_conversation", "export_project"}


@dataclass(frozen=True, slots=True)
class AutomationPolicyDecision:
    verdict: str
    reasons: tuple[str, ...] = ()
    risk: str = "low"
    policy: dict[str, Any] | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict == ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.verdict == NEEDS_CONFIRMATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "risk": self.risk,
            "reasons": list(self.reasons),
            "policy": self.policy or {},
        }


def evaluate(
    automation: dict[str, Any],
    *,
    trigger: dict[str, Any] | None = None,
    now: datetime | None = None,
    confirmed: bool = False,
) -> AutomationPolicyDecision:
    reasons: list[str] = []
    risk = "low"
    policy = _policy(automation)
    raw_action = automation.get("action")
    action: dict[str, Any] = raw_action if isinstance(raw_action, dict) else {}
    action_type = str(action.get("type") or "").strip().lower()
    if not config.AUTOMATION_ENABLED:
        return AutomationPolicyDecision(DENY, ("automation_disabled",), "high", policy)
    if policy.get("requiresConfirmation") and not confirmed:
        return AutomationPolicyDecision(NEEDS_CONFIRMATION, ("automation_requires_confirmation",), "medium", policy)
    max_runs = max(1, int(policy.get("maxRunsPerDay") or config.AUTOMATION_MAX_RUNS_PER_DAY))
    today = history.runs_today(str(automation.get("automationId") or ""), now=now)
    if today >= max_runs:
        return AutomationPolicyDecision(DENY, ("max_runs_per_day_exceeded",), "medium", policy)
    if action_type in BROWSER_ACTIONS:
        risk = "medium"
        if not policy.get("allowBrowser"):
            return AutomationPolicyDecision(DENY, ("browser_not_allowed",), risk, policy)
        if str(policy.get("browserMode") or "read_only") == "disabled":
            return AutomationPolicyDecision(DENY, ("browser_mode_disabled",), risk, policy)
        url = str(action.get("url") or action.get("downloadUrl") or "").strip()
        if _network_url(url) and not policy.get("allowNetwork"):
            return AutomationPolicyDecision(DENY, ("network_not_allowed",), risk, policy)
        if url and not policy.get("allowPrivateHosts"):
            safe, reason = browser_safety.evaluate_url_safety(url)
            if not safe:
                return AutomationPolicyDecision(DENY, (f"unsafe_url:{reason}",), "critical", policy)
    elif action_type not in LOCAL_ACTIONS:
        return AutomationPolicyDecision(DENY, ("unsupported_action",), "high", policy)
    browser_write_action = str(action.get("browserAction") or "").strip().lower()
    if browser_write_action and browser_write_action in browser_safety.WRITE_ACTIONS:
        risk = "high"
        if config.AUTOMATION_REQUIRE_CONFIRM_FOR_BROWSER_WRITE and not confirmed:
            reasons.append("browser_write_requires_confirmation")
    raw_trigger = trigger if isinstance(trigger, dict) else automation.get("trigger")
    trigger_data: dict[str, Any] = raw_trigger if isinstance(raw_trigger, dict) else {}
    trigger_type = str(trigger_data.get("type") or "").lower()
    if trigger_type != "manual" and action_type == "run_skill" and bool(action.get("allowNetwork")):
        risk = "medium"
        reasons.append("scheduled_skill_network_requires_confirmation")
    if reasons:
        return AutomationPolicyDecision(NEEDS_CONFIRMATION, tuple(sorted(set(reasons))), risk, policy)
    return AutomationPolicyDecision(ALLOW, (), risk, policy)


def _policy(automation: dict[str, Any]) -> dict[str, Any]:
    data = automation.get("policy")
    return data if isinstance(data, dict) else {}


def _network_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"}
