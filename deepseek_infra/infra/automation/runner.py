"""Automation Runtime execution orchestration."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.automation import actions, history, policy, registry, schema, triggers
from deepseek_infra.infra.observability.observability import finish_trace, start_span, start_trace
from deepseek_infra.infra.workspace.schema import now_ms, timestamp_ms_to_iso


def run_once(
    automation_id: str,
    *,
    trigger: dict[str, Any] | None = None,
    event: dict[str, Any] | None = None,
    now: datetime | None = None,
    confirmed: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    automation = registry.get_automation(automation_id)
    trigger_data = trigger if isinstance(trigger, dict) else {"type": "manual"}
    if not automation.get("enabled") and not force:
        return _record_terminal_run(
            automation,
            trigger=trigger_data,
            status="skipped",
            skipped_reason="automation_disabled",
            logs=["automation disabled"],
        )
    if not force:
        matched, reason = triggers.trigger_matches(automation, trigger=trigger_data, event=event, now=now)
        if not matched:
            return _record_terminal_run(automation, trigger=trigger_data, status="skipped", skipped_reason=reason, logs=[reason])
        condition_ok, condition_reason = triggers.condition_matches(automation, event=event)
        if not condition_ok:
            return _record_terminal_run(automation, trigger=trigger_data, status="skipped", skipped_reason=condition_reason, logs=[condition_reason])
    decision = policy.evaluate(automation, trigger=trigger_data, confirmed=confirmed)
    if decision.needs_confirmation:
        return _record_terminal_run(
            automation,
            trigger=trigger_data,
            status="requires_confirmation",
            skipped_reason="policy_requires_confirmation",
            logs=decision.to_dict()["reasons"],
            evidence={"policy": decision.to_dict()},
        )
    if not decision.allowed:
        return _record_terminal_run(
            automation,
            trigger=trigger_data,
            status="skipped",
            skipped_reason="policy_denied:" + ",".join(decision.reasons),
            logs=decision.to_dict()["reasons"],
            evidence={"policy": decision.to_dict()},
        )
    return _execute_allowed(automation, trigger=trigger_data, event=event, decision=decision)


def rerun(run_id: str, *, confirmed: bool = False) -> dict[str, Any]:
    run = history.get_run(run_id)
    return run_once(str(run.get("automationId") or ""), trigger={"type": "manual", "rerunOf": run_id}, confirmed=confirmed, force=True)


def _execute_allowed(
    automation: dict[str, Any],
    *,
    trigger: dict[str, Any],
    event: dict[str, Any] | None,
    decision: policy.AutomationPolicyDecision,
) -> dict[str, Any]:
    run_id = schema.new_run_id()
    started_ms = now_ms()
    trace_id = start_trace(
        kind="automation",
        title=str(automation.get("name") or automation.get("automationId") or "Automation"),
        metadata={
            "automationId": automation.get("automationId"),
            "projectId": automation.get("projectId"),
            "trigger": trigger,
        },
    )
    span = start_span(
        trace_id,
        name=f"automation.run:{automation.get('automationId')}",
        kind="automation_run",
        input_data={"automation": automation, "trigger": trigger, "event": event, "policy": decision.to_dict()},
    )
    raw_policy = automation.get("policy")
    policy_data: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
    raw_retry = policy_data.get("retry")
    retry: dict[str, Any] = raw_retry if isinstance(raw_retry, dict) else {}
    max_attempts = max(1, int(retry.get("maxAttempts") or 1))
    timeout_seconds = max(1, int(policy_data.get("timeoutSeconds") or 1_800))
    attempts = 0
    logs: list[str] = []
    last_error = ""
    outputs: dict[str, list[str]] = {"artifactIds": [], "savedItemIds": [], "mediaIds": [], "exportIds": []}
    raw_result: dict[str, Any] = {}
    skipped_reason = ""
    started_monotonic = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            action_result = actions.run_action(automation, run_id=run_id, trigger=trigger, event=event)
            raw_outputs = action_result.get("outputs")
            outputs = _merge_outputs(outputs, raw_outputs if isinstance(raw_outputs, dict) else {})
            logs.extend(str(item) for item in action_result.get("logs", []) if str(item or ""))
            raw_action_result = action_result.get("raw")
            raw_result = raw_action_result if isinstance(raw_action_result, dict) else {}
            skipped_reason = str(action_result.get("skippedReason") or "")
            if time.monotonic() - started_monotonic > timeout_seconds:
                raise AppError("Automation run exceeded timeout")
            status = "skipped" if skipped_reason else "success"
            finished_ms = now_ms()
            if span.trace_id:
                span.finish(status="ok" if status == "success" else "skipped", output_data={"outputs": outputs, "skippedReason": skipped_reason})
            finish_trace(trace_id, status="completed" if status == "success" else "skipped", metadata={"automationId": automation.get("automationId"), "runId": run_id})
            return history.record_run(
                {
                    "runId": run_id,
                    "automationId": automation.get("automationId"),
                    "projectId": automation.get("projectId"),
                    "status": status,
                    "startedAtMs": started_ms,
                    "finishedAtMs": finished_ms,
                    "durationMs": finished_ms - started_ms,
                    "trigger": trigger,
                    "outputs": outputs,
                    "traceId": trace_id,
                    "attempts": attempts,
                    "skippedReason": skipped_reason,
                    "logs": logs,
                    "evidence": {"policy": decision.to_dict(), "action": raw_result},
                }
            )
        except Exception as exc:
            last_error = str(exc)
            logs.append(f"attempt {attempt} failed: {last_error}")
            if attempt >= max_attempts:
                break
    finished_ms = now_ms()
    if span.trace_id:
        span.finish(status="error", output_data={"outputs": outputs}, error=last_error)
    finish_trace(trace_id, status="error", metadata={"automationId": automation.get("automationId"), "runId": run_id}, error=last_error)
    return history.record_run(
        {
            "runId": run_id,
            "automationId": automation.get("automationId"),
            "projectId": automation.get("projectId"),
            "status": "failed",
            "startedAtMs": started_ms,
            "finishedAtMs": finished_ms,
            "durationMs": finished_ms - started_ms,
            "trigger": trigger,
            "outputs": outputs,
            "traceId": trace_id,
            "attempts": attempts,
            "error": last_error,
            "logs": logs,
            "evidence": {"policy": decision.to_dict()},
        }
    )


def _record_terminal_run(
    automation: dict[str, Any],
    *,
    trigger: dict[str, Any],
    status: str,
    skipped_reason: str = "",
    logs: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_ms = now_ms()
    trace_id = start_trace(
        kind="automation",
        title=str(automation.get("name") or automation.get("automationId") or "Automation"),
        metadata={"automationId": automation.get("automationId"), "projectId": automation.get("projectId"), "trigger": trigger},
    )
    finish_trace(trace_id, status=status, metadata={"automationId": automation.get("automationId"), "terminal": True})
    return history.record_run(
        {
            "runId": schema.new_run_id(),
            "automationId": automation.get("automationId"),
            "projectId": automation.get("projectId"),
            "status": status,
            "startedAtMs": current_ms,
            "finishedAtMs": current_ms,
            "durationMs": 0,
            "startedAt": timestamp_ms_to_iso(current_ms),
            "finishedAt": timestamp_ms_to_iso(current_ms),
            "trigger": trigger,
            "outputs": {"artifactIds": [], "savedItemIds": [], "mediaIds": [], "exportIds": []},
            "traceId": trace_id,
            "attempts": 1,
            "skippedReason": skipped_reason,
            "logs": logs or [],
            "evidence": evidence or {},
        }
    )


def _merge_outputs(left: dict[str, list[str]], right: dict[str, Any]) -> dict[str, list[str]]:
    merged = {key: list(values) for key, values in left.items()}
    for key in ("artifactIds", "savedItemIds", "mediaIds", "exportIds"):
        for value in right.get(key, []) if isinstance(right.get(key), list) else []:
            text = str(value or "").strip()
            if text and text not in merged[key]:
                merged[key].append(text)
    return merged
