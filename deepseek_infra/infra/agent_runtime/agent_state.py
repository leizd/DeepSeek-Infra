"""Event-sourced node state machine for the Durable Agent Runtime.

The Agent Run is already event-sourced: ``events`` is the source of truth and
the run snapshot is a derived cache (see ``agent_runs.apply_event_snapshot``).
This module adds the formal *node-level* state machine that was missing, and a
pure reducer that reconstructs every node's lifecycle purely from the plan +
event log — so a run can be replayed, inspected, and resumed deterministically.

Node lifecycle::

    created → queued → running → succeeded
                          ↘ failed → retrying → running → ...
                          ↘ cancelled

``created`` = planned but its ``depends_on`` are not all satisfied yet.
``queued``  = planned, dependencies satisfied, not started.
``running`` = an ``agent`` running event was observed.
``succeeded``/``failed`` = an ``agent`` done/error or ``agent_output`` was observed.
``retrying`` = an ``agent_reset`` (rerun / critic revision / resume) reset the node.
``cancelled`` = the run was cancelled before the node reached a terminal state.

Everything here is pure (no I/O, no network): it maps the existing event types
to node states, so no new live SSE events are introduced and the streaming
protocol the frontend and tests depend on is unchanged.
"""

from __future__ import annotations

from typing import Any

NODE_STATES = {"created", "queued", "running", "succeeded", "failed", "retrying", "cancelled"}
TERMINAL_NODE_STATES = {"succeeded", "cancelled"}
# Phases that are orchestration roles, not DAG worker nodes.
NON_NODE_PHASES = {"", "leader", "synthesizer"}

# Intended lifecycle transitions. The reducer maps events to target states
# directly; ``can_transition`` documents and validates the legal moves (used by
# tests and any future strict-mode enforcement).
NODE_TRANSITIONS: dict[str, set[str]] = {
    "created": {"queued", "running", "cancelled"},
    "queued": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancelled"},
    "failed": {"retrying", "running", "cancelled"},
    "retrying": {"running", "cancelled"},
    "succeeded": {"retrying"},  # a rerun re-opens a finished node
    "cancelled": set(),
}


def can_transition(from_state: str, to_state: str) -> bool:
    """True if ``from_state → to_state`` is a legal node-lifecycle move."""
    if from_state == to_state:
        return True
    return to_state in NODE_TRANSITIONS.get(from_state, set())


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


def _new_node(node_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "state": "created",
        "attempts": 0,
        "latencyMs": None,
        "promptTokens": 0,
        "completionTokens": 0,
        "failed": False,
    }


def _ensure_node(nodes: dict[str, dict[str, Any]], node_id: str) -> dict[str, Any]:
    node = nodes.get(node_id)
    if node is None:
        node = _new_node(node_id)
        nodes[node_id] = node
    return node


def _plan_dependencies(plan: list[Any]) -> dict[str, set[str]]:
    deps: dict[str, set[str]] = {}
    for item in plan:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "")
        if not node_id:
            continue
        raw = item.get("depends_on")
        deps[node_id] = {str(dep) for dep in raw if str(dep)} if isinstance(raw, list) else set()
    return deps


def reduce_node_states(plan: list[Any], events: list[Any]) -> dict[str, dict[str, Any]]:
    """Reconstruct every node's state purely from the plan + event log.

    Returns a mapping ``{node_id: {id, state, attempts, latencyMs,
    promptTokens, completionTokens, failed}}``. Deterministic: replaying the
    same events always yields the same node states.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for item in plan if isinstance(plan, list) else []:
        if isinstance(item, dict) and item.get("id"):
            _ensure_node(nodes, str(item["id"]))

    run_cancelled = False
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "run_status" and event.get("status") == "cancelled":
            run_cancelled = True
            continue

        phase = str(event.get("phase") or "")
        if event_type == "agent":
            if phase in NON_NODE_PHASES:
                continue
            node = _ensure_node(nodes, phase)
            status = str(event.get("status") or "")
            if status == "running":
                node["state"] = "running"
                node["attempts"] = int(node.get("attempts") or 0) + 1
            elif status == "done":
                node["state"] = "succeeded"
                _record_latency(node, event.get("durationMs"))
            elif status == "error":
                node["state"] = "failed"
                _record_latency(node, event.get("durationMs"))
        elif event_type == "agent_output":
            raw_output = event.get("output")
            output: dict[str, Any] = raw_output if isinstance(raw_output, dict) else {}
            node_id = phase or str(output.get("id") or "")
            if node_id in NON_NODE_PHASES:
                continue
            node = _ensure_node(nodes, node_id)
            failed = bool(output.get("failed"))
            node["failed"] = failed
            node["state"] = "failed" if failed else "succeeded"
            _record_latency(node, output.get("duration_ms"))
            raw_usage = output.get("usage")
            usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
            prompt = _usage_int(usage, "prompt_tokens", "promptTokens")
            completion = _usage_int(usage, "completion_tokens", "completionTokens")
            if prompt:
                node["promptTokens"] = prompt
            if completion:
                node["completionTokens"] = completion
        elif event_type == "agent_reset":
            if phase in NON_NODE_PHASES:
                continue
            node = _ensure_node(nodes, phase)
            node["state"] = "retrying"
            node["failed"] = False

    deps = _plan_dependencies(plan if isinstance(plan, list) else [])
    for node_id, node in nodes.items():
        if run_cancelled and node["state"] not in TERMINAL_NODE_STATES:
            node["state"] = "cancelled"
            continue
        if node["state"] == "created":
            node_deps = deps.get(node_id, set())
            if all(nodes.get(dep, {}).get("state") == "succeeded" for dep in node_deps):
                node["state"] = "queued"
    return nodes


def _record_latency(node: dict[str, Any], value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        return
    if value >= 0:
        node["latencyMs"] = value


def incomplete_plan_nodes(plan: list[Any], nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Plan items whose node has not durably succeeded (need (re)running on resume)."""
    result: list[dict[str, Any]] = []
    for item in plan if isinstance(plan, list) else []:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "")
        if not node_id:
            continue
        if nodes.get(node_id, {}).get("state") != "succeeded":
            result.append(item)
    return result


def completed_node_ids(plan: list[Any], nodes: dict[str, dict[str, Any]]) -> list[str]:
    """Plan node ids that durably succeeded (skip on resume), in plan order."""
    ids: list[str] = []
    for item in plan if isinstance(plan, list) else []:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or "")
        if node_id and nodes.get(node_id, {}).get("state") == "succeeded":
            ids.append(node_id)
    return ids
