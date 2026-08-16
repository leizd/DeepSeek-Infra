"""Targeted test coverage boosters for agent_state and automation scheduler."""

from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.agent_runtime import agent_state
from deepseek_infra.infra.automation import registry, scheduler


def test_agent_state_transitions_and_reduction(tmp_settings: Path) -> None:
    # 1. State transitions
    assert agent_state.can_transition("created", "queued") is True
    assert agent_state.can_transition("created", "running") is True
    assert agent_state.can_transition("queued", "running") is True
    assert agent_state.can_transition("running", "succeeded") is True
    assert agent_state.can_transition("running", "failed") is True
    assert agent_state.can_transition("failed", "retrying") is True
    assert agent_state.can_transition("retrying", "running") is True
    assert agent_state.can_transition("succeeded", "retrying") is True
    assert agent_state.can_transition("cancelled", "running") is False
    assert agent_state.can_transition("running", "running") is True

    # 2. Plan and Event Reduction
    plan = [
        {"id": "node_1", "role": "researcher"},
        {"id": "node_2", "role": "writer", "depends_on": ["node_1"]},
    ]
    events = [
        {"type": "agent", "phase": "node_1", "status": "running"},
        {"type": "agent", "phase": "node_1", "status": "done", "durationMs": 150},
        {"type": "agent_output", "phase": "node_1", "output": {"duration_ms": 150, "usage": {"prompt_tokens": 100, "completion_tokens": 50}}},
        {"type": "agent", "phase": "node_2", "status": "running"},
        {"type": "agent", "phase": "node_2", "status": "error", "durationMs": 50},
        {"type": "agent_reset", "phase": "node_2"},
    ]
    nodes = agent_state.reduce_node_states(plan, events)
    assert nodes["node_1"]["state"] == "succeeded"
    assert nodes["node_1"]["promptTokens"] == 100
    assert nodes["node_1"]["completionTokens"] == 50
    assert nodes["node_2"]["state"] == "retrying"

    # Incomplete and completed node helpers
    completed = agent_state.completed_node_ids(plan, nodes)
    assert completed == ["node_1"]

    incomplete = agent_state.incomplete_plan_nodes(plan, nodes)
    assert len(incomplete) == 1
    assert incomplete[0]["id"] == "node_2"

    # Cancelled run event
    cancel_events = [
        {"type": "agent", "phase": "node_1", "status": "running"},
        {"type": "run_status", "status": "cancelled"},
    ]
    cancelled_nodes = agent_state.reduce_node_states(plan, cancel_events)
    assert cancelled_nodes["node_1"]["state"] == "cancelled"
    assert cancelled_nodes["node_2"]["state"] == "cancelled"


def test_automation_scheduler_simulate_trigger(tmp_settings: Path) -> None:
    # Register a test automation
    auto = registry.create_automation(
        {
            "name": "Simulate Test",
            "trigger": {"type": "event", "event": "artifact.created"},
            "condition": {"type": "always"},
            "action": {"type": "create_artifact", "title": "Summary", "artifactType": "markdown"},
        }
    )
    a_id = auto["automationId"]

    # Simulate trigger matching
    sim_res = scheduler.simulate_trigger(
        a_id,
        event={"event": "artifact.created"},
    )
    assert sim_res.get("ok") is True
    assert sim_res.get("triggerMatched") is True
    assert sim_res.get("wouldRun") is True
