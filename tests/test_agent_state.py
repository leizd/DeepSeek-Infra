from __future__ import annotations

import deepseek_infra.infra.agent_runtime.agent_state as st


def _plan() -> list[dict[str, object]]:
    return [
        {"id": "researcher", "task": "find"},
        {"id": "coder", "task": "code", "depends_on": ["researcher"]},
        {"id": "critic", "task": "review", "depends_on": ["researcher", "coder"]},
    ]


def test_can_transition_follows_lifecycle() -> None:
    assert st.can_transition("created", "queued")
    assert st.can_transition("queued", "running")
    assert st.can_transition("running", "succeeded")
    assert st.can_transition("running", "failed")
    assert st.can_transition("failed", "retrying")
    assert st.can_transition("succeeded", "retrying")  # a rerun re-opens a node
    assert st.can_transition("running", "running")  # identity is always allowed
    assert not st.can_transition("succeeded", "running")
    assert not st.can_transition("cancelled", "running")


def test_reduce_seeds_created_and_queued_from_dependencies() -> None:
    nodes = st.reduce_node_states(_plan(), [{"type": "agent_plan", "plan": _plan()}])
    # researcher has no deps -> queued; coder/critic wait on unmet deps -> created.
    assert nodes["researcher"]["state"] == "queued"
    assert nodes["coder"]["state"] == "created"
    assert nodes["critic"]["state"] == "created"


def test_reduce_tracks_running_success_and_metrics() -> None:
    events = [
        {"type": "agent_plan", "plan": _plan()},
        {"type": "agent", "phase": "researcher", "status": "running"},
        {"type": "agent", "phase": "researcher", "status": "done", "durationMs": 1200},
        {
            "type": "agent_output",
            "phase": "researcher",
            "output": {"id": "researcher", "duration_ms": 1500, "usage": {"prompt_tokens": 800, "completion_tokens": 200}},
        },
    ]
    nodes = st.reduce_node_states(_plan(), events)
    researcher = nodes["researcher"]
    assert researcher["state"] == "succeeded"
    assert researcher["attempts"] == 1
    assert researcher["latencyMs"] == 1500  # agent_output latency wins over the agent event
    assert researcher["promptTokens"] == 800
    assert researcher["completionTokens"] == 200
    # coder's dep is now satisfied -> queued.
    assert nodes["coder"]["state"] == "queued"


def test_reduce_marks_failed_node_and_keeps_it_incomplete() -> None:
    events = [
        {"type": "agent_plan", "plan": _plan()},
        {"type": "agent_output", "phase": "researcher", "output": {"id": "researcher"}},
        {"type": "agent", "phase": "coder", "status": "running"},
        {"type": "agent", "phase": "coder", "status": "error", "durationMs": 500},
        {"type": "agent_output", "phase": "coder", "output": {"id": "coder", "failed": True}},
    ]
    nodes = st.reduce_node_states(_plan(), events)
    assert nodes["researcher"]["state"] == "succeeded"
    assert nodes["coder"]["state"] == "failed"
    assert nodes["coder"]["failed"] is True
    # critic still waits because coder did not succeed.
    assert nodes["critic"]["state"] == "created"
    assert [item["id"] for item in st.incomplete_plan_nodes(_plan(), nodes)] == ["coder", "critic"]
    assert st.completed_node_ids(_plan(), nodes) == ["researcher"]


def test_reduce_reopens_node_on_reset() -> None:
    events = [
        {"type": "agent_plan", "plan": _plan()},
        {"type": "agent_output", "phase": "coder", "output": {"id": "coder"}},
        {"type": "agent_reset", "phase": "coder", "reason": "rerun_agent"},
    ]
    nodes = st.reduce_node_states(_plan(), events)
    assert nodes["coder"]["state"] == "retrying"
    assert nodes["coder"]["failed"] is False


def test_reduce_cancels_non_terminal_nodes_on_run_cancel() -> None:
    events = [
        {"type": "agent_plan", "plan": _plan()},
        {"type": "agent_output", "phase": "researcher", "output": {"id": "researcher"}},
        {"type": "agent", "phase": "coder", "status": "running"},
        {"type": "run_status", "status": "cancelled"},
    ]
    nodes = st.reduce_node_states(_plan(), events)
    assert nodes["researcher"]["state"] == "succeeded"  # terminal survives cancel
    assert nodes["coder"]["state"] == "cancelled"
    assert nodes["critic"]["state"] == "cancelled"


def test_reduce_ignores_orchestration_phases() -> None:
    events = [
        {"type": "agent_plan", "plan": _plan()},
        {"type": "agent", "phase": "leader", "status": "running"},
        {"type": "agent", "phase": "leader", "status": "done"},
        {"type": "agent_note", "phase": "worker-7", "text": "noise"},
    ]
    nodes = st.reduce_node_states(_plan(), events)
    assert "leader" not in nodes
    assert "worker-7" not in nodes  # agent_note does not create nodes


def test_reduce_tracks_node_outside_plan_snapshot() -> None:
    # A run with no plan event still derives node state purely from agent events.
    events = [
        {"type": "agent", "phase": "coder", "status": "running"},
        {"type": "agent", "phase": "coder", "status": "done", "durationMs": 42},
    ]
    nodes = st.reduce_node_states([], events)
    assert nodes["coder"]["state"] == "succeeded"
    assert nodes["coder"]["latencyMs"] == 42
