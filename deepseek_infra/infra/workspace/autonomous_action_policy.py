"""Global Recovery Intelligence - Autonomous Action Policy Gate (4.7.0 P0-4).

Governs which resilience actions are allowed to execute autonomously without
operator intervention (repair, rebalance, drill) and strictly gates risky,
destructive, or topology-mutating actions (primary promotion, policy changes,
copy deletions) behind explicit manual operator approval.
"""

from __future__ import annotations

import json
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

AUTOMATION_POLICY_VERSION = 1
POLICY_DIR = config.ROOT / ".resilience-policy"
POLICY_FILE = POLICY_DIR / "autonomous_policy.json"

DEFAULT_ALLOWED_ACTIONS = [
    "CREATE_REPAIR_JOB",
    "CREATE_REBALANCE_JOB",
    "START_DR_DRILL",
]

DEFAULT_APPROVAL_REQUIRED = [
    "PRIMARY_PROMOTION",
    "POLICY_CHANGE",
    "COPY_DELETION",
    "TOPOLOGY_MUTATION",
]

DEFAULT_POLICY: dict[str, Any] = {
    "automationPolicyVersion": AUTOMATION_POLICY_VERSION,
    "allowedActions": DEFAULT_ALLOWED_ACTIONS,
    "approvalRequired": DEFAULT_APPROVAL_REQUIRED,
    "rateLimits": {
        "maxConcurrentActions": 3,
        "maxActionsPerHour": 20,
    },
    "enabled": True,
}


def get_autonomous_action_policy() -> dict[str, Any]:
    """Retrieve the active autonomous action policy."""
    if not POLICY_FILE.is_file():
        return dict(DEFAULT_POLICY)
    try:
        raw = POLICY_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return dict(DEFAULT_POLICY)


def set_autonomous_action_policy(policy_data: dict[str, Any]) -> dict[str, Any]:
    """Update and persist the autonomous action policy."""
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULT_POLICY)
    merged.update(policy_data)
    merged["automationPolicyVersion"] = AUTOMATION_POLICY_VERSION

    # Prohibit un-gating critical forbidden actions
    allowed = set(merged.get("allowedActions", []))
    for forbidden in ("PRIMARY_PROMOTION", "COPY_DELETION"):
        if forbidden in allowed:
            raise AppError(
                f"Action '{forbidden}' cannot be added to autonomous allowed actions; operator approval strictly required",
                code=ErrorCode.INVALID_REQUEST,
                status=400,
            )

    POLICY_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return merged


def is_action_autonomous(action_type: str, policy: dict[str, Any] | None = None) -> bool:
    """Check if an action type is permitted for autonomous execution without approval."""
    pol = policy or get_autonomous_action_policy()
    if not pol.get("enabled", True):
        return False
    allowed = set(pol.get("allowedActions", DEFAULT_ALLOWED_ACTIONS))
    approval = set(pol.get("approvalRequired", DEFAULT_APPROVAL_REQUIRED))
    act = str(action_type).upper()
    if act in approval:
        return False
    return act in allowed


def validate_action_admission(
    action: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate whether an action can be scheduled and executed."""
    act_type = str(action.get("type", "")).upper()
    if not act_type:
        return False, "missing-action-type"

    req_appr = bool(action.get("requiresApproval"))
    if req_appr and is_action_autonomous(act_type, policy):
        # Action explicitly marked as requiring approval despite type defaults
        return False, f"action-{act_type}-requires-explicit-approval"

    if not is_action_autonomous(act_type, policy) and not action.get("approved"):
        return False, f"action-{act_type}-not-autonomous-approval-required"

    return True, "admitted"
