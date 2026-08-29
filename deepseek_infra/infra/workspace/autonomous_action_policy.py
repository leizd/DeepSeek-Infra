"""Global Recovery Intelligence - Autonomous Action Policy Gate (4.7.1 Gate F & K).

Governs which resilience actions are allowed to execute autonomously without
operator intervention (repair, rebalance, drill) and strictly gates risky,
destructive, or topology-mutating actions (primary promotion, policy changes,
copy deletions, topology mutations) behind an immutable code-level safety floor.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

AUTOMATION_POLICY_VERSION = 1
POLICY_DIR = config.ROOT / ".resilience-policy"
POLICY_FILE = POLICY_DIR / "autonomous_policy.json"

NEVER_AUTONOMOUS = frozenset(
    {
        "PRIMARY_PROMOTION",
        "POLICY_CHANGE",
        "COPY_DELETION",
        "TOPOLOGY_MUTATION",
    }
)

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

DEFAULT_RATE_LIMITS = {
    "maxConcurrentActions": 3,
    "maxActionsPerHour": 20,
    "maxConcurrentPerTarget": 2,
    "maxConcurrentPerPolicy": 2,
    "maxSimultaneousFailureDomainsTouched": 1,
    "maxRebalancesPerTargetPerHour": 5,
    "maxDrillsPerPolicyPerDay": 2,
}

DEFAULT_POLICY: dict[str, Any] = {
    "automationPolicyVersion": AUTOMATION_POLICY_VERSION,
    "allowedActions": DEFAULT_ALLOWED_ACTIONS,
    "approvalRequired": DEFAULT_APPROVAL_REQUIRED,
    "rateLimits": dict(DEFAULT_RATE_LIMITS),
    "enabled": True,
}


def get_autonomous_action_policy() -> dict[str, Any]:
    """Retrieve the active autonomous action policy."""
    if not POLICY_FILE.is_file():
        return dict(DEFAULT_POLICY)
    for _ in range(5):
        try:
            raw = POLICY_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                # Ensure safety floor invariants are present in memory view
                res = dict(DEFAULT_POLICY)
                res.update(data)
                return res
        except (OSError, json.JSONDecodeError):
            time.sleep(0.01)
    return dict(DEFAULT_POLICY)


def set_autonomous_action_policy(policy_data: dict[str, Any]) -> dict[str, Any]:
    """Update and persist the autonomous action policy with immutable safety floor enforcement."""
    POLICY_DIR.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULT_POLICY)
    merged.update(policy_data)
    merged["automationPolicyVersion"] = AUTOMATION_POLICY_VERSION

    # Prohibit un-gating critical forbidden actions via code safety floor (Gate F)
    allowed = set(merged.get("allowedActions", []))
    for forbidden in sorted(NEVER_AUTONOMOUS):
        if forbidden in allowed:
            raise AppError(
                f"Action '{forbidden}' cannot be added to autonomous allowed actions; operator approval strictly required by code safety floor",
                code=ErrorCode.INVALID_REQUEST,
                status=400,
            )

    temp_file = POLICY_DIR / f"autonomous_policy.{uuid.uuid4().hex}.tmp"
    temp_file.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_file.replace(POLICY_FILE)
    return merged


def get_action_rate_limits(policy: dict[str, Any] | None = None) -> dict[str, int]:
    """Get active rate limit settings."""
    pol = policy or get_autonomous_action_policy()
    limits = dict(DEFAULT_RATE_LIMITS)
    configured = pol.get("rateLimits")
    if isinstance(configured, dict):
        for k, v in configured.items():
            if isinstance(v, int) and v > 0:
                limits[k] = v
    return limits


def set_action_rate_limits(rate_limits: dict[str, int]) -> dict[str, int]:
    """Configure action rate limits."""
    pol = get_autonomous_action_policy()
    cur_limits = dict(DEFAULT_RATE_LIMITS)
    cur_limits.update(rate_limits)
    pol["rateLimits"] = cur_limits
    set_autonomous_action_policy(pol)
    return cur_limits


def is_action_autonomous(action_type: str, policy: dict[str, Any] | None = None) -> bool:
    """Check if an action type is permitted for autonomous execution without approval."""
    act = str(action_type).upper()
    # Immutable code-level safety floor (Gate F)
    if act in NEVER_AUTONOMOUS:
        return False

    pol = policy or get_autonomous_action_policy()
    if not pol.get("enabled", True):
        return False

    allowed = set(pol.get("allowedActions", DEFAULT_ALLOWED_ACTIONS))
    approval = set(pol.get("approvalRequired", DEFAULT_APPROVAL_REQUIRED))
    if act in approval:
        return False
    return act in allowed


def validate_action_admission(
    action: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Validate whether an action can be scheduled and executed autonomously."""
    act_type = str(action.get("type", "")).upper()
    if not act_type:
        return False, "missing-action-type"

    if act_type in NEVER_AUTONOMOUS:
        if not action.get("approved"):
            return False, f"action-{act_type}-forbidden-by-safety-floor"

    req_appr = bool(action.get("requiresApproval"))
    if req_appr and is_action_autonomous(act_type, policy):
        # Action explicitly marked as requiring approval despite type defaults
        return False, f"action-{act_type}-requires-explicit-approval"

    if not is_action_autonomous(act_type, policy) and not action.get("approved"):
        return False, f"action-{act_type}-not-autonomous-approval-required"

    return True, "admitted"
