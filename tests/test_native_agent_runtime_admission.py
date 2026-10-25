"""Offline admission for retained Agent runtime updates.

These tests call the shipped Python oracle, which loads the same JSON contract
the Rust edge compiles in. They also require the Rust route, the worker vault
scan, and the Go driver to name those rules. They do not start Cargo, Go, or
Docker, and they do not treat a metadata decision as a provider re-run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from deepseek_infra.infra.native_runtime.agent_runtime_admission import (
    admit,
    agent_node_send_deadline,
    continue_retained_tools,
    contract,
    admitted_provider_send,
    dependency_provider_body,
    execution_send_budget,
    execution_send_lease,
    follow_up_exchange,
    provider_exchange_unknown,
    current_runtime_revision,
    dispatch_allowed,
    journal_observation_bytes,
    next_model_turn,
    project_journal_observation,
    published_dependency_output,
    published_model_answer,
    resume_stored_tools,
    settle_published_answer,
    retained_driver_tool_results,
    retained_effect,
    successor_binding,
    successor_execution,
    successor_generation,
    successor_operation_id,
    vault_revision_allowed,
    workspace_tool_lease,
)
from deepseek_infra.infra.tool_runtime.tools import MAX_TOOL_CALLS_PER_RESPONSE, execute_tool_call

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "rust/crates/deepseek-policy/src/agent_runtime_admission_v1.json"
RUST_ADMISSION = ROOT / "rust/crates/deepseek-policy/src/agent_runtime_admission.rs"
ROUTES = ROOT / "rust/crates/deepseek-gateway/src/agent_run_routes.rs"
WORKER = ROOT / "rust/crates/deepseek-worker/src/agent_provider_credentials.rs"
GO_DISPATCH = ROOT / "go/internal/agent/runtime_admission.go"
GO_DRIVER = ROOT / "go/internal/api/agent_run_driver.go"
GO_SCHEMA = ROOT / "go/internal/store/schema.go"
GO_EVENTS = ROOT / "go/internal/store/agent_run_events.go"


def fields(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "operation": "resume",
        "status": "failed",
        "identityValid": True,
        "lease": "live",
        "epoch": "match",
        "effect": "none",
        "runtimeRevision": 1,
        "requestedRevision": 2,
        "agentId": "researcher",
        "resynthesize": True,
        "plan": ["researcher", "coder"],
        "nodes": [
            {"id": "researcher", "state": "succeeded", "failed": False},
            {"id": "coder", "state": "failed", "failed": True},
        ],
        "hasFinalAnswer": False,
        "sameEffect": False,
    }
    body.update(overrides)
    return body


def test_contract_is_the_rust_json_the_oracle_loads() -> None:
    loaded = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert contract() == loaded
    assert loaded["schema"] == "agent-runtime-admission-v1"
    assert loaded["schemaVersion"] == 1
    assert loaded["operations"] == ["resume", "rerun", "runtime_update", "tool_continuation"]
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    assert 'include_str!("agent_runtime_admission_v1.json")' in rust


def test_completed_resume_with_a_final_answer_is_a_visible_success_without_a_provider_call() -> None:
    decision = admit(fields(
        nodes=[
            {"id": "researcher", "state": "succeeded", "failed": False},
            {"id": "coder", "state": "succeeded", "failed": False},
        ],
        hasFinalAnswer=True,
    ))
    assert decision["admitted"] is True
    assert decision["httpStatus"] == 200
    assert decision["started"] is False
    assert decision["dispatch"] == "none"
    assert decision["bumpsRevision"] is True
    assert decision["requestedRevision"] == 2
    assert decision["events"] == [
        {"type": "run_status", "status": "running", "reason": "resume"},
        {"type": "run_status", "status": "done", "reason": "resume"},
    ]


def test_incomplete_resume_records_a_reset_and_does_not_claim_a_provider_rerun() -> None:
    decision = admit(fields())
    assert decision["admitted"] is True
    assert decision["started"] is True
    assert decision["dispatch"] == "reset-incomplete"
    assert decision["events"] == [
        {"type": "run_status", "status": "running", "reason": "resume"},
        {"type": "agent_reset", "phase": "coder", "reason": "resume"},
        {"type": "final_reset", "scope": "final_answer", "reason": "resume"},
    ]
    assert all(event["type"] != "agent_output" for event in decision["events"])


def test_resume_without_a_final_answer_asks_for_synthesis_only() -> None:
    decision = admit(fields(
        nodes=[
            {"id": "researcher", "state": "succeeded", "failed": False},
            {"id": "coder", "state": "succeeded", "failed": False},
        ],
        hasFinalAnswer=False,
    ))
    assert decision["dispatch"] == "synthesis-only"
    assert decision["started"] is True
    assert decision["events"] == [
        {"type": "run_status", "status": "running", "reason": "resume"},
        {"type": "final_reset", "scope": "final_answer", "reason": "resume"},
    ]


def test_runtime_update_bumps_the_revision_and_does_not_change_status() -> None:
    decision = admit(fields(operation="runtime_update"))
    assert decision["admitted"] is True
    assert decision["dispatch"] == "none"
    assert decision["started"] is False
    assert decision["bumpsRevision"] is True
    assert decision["requestedRevision"] == 2
    assert decision["events"] == []


def test_rerun_of_a_known_agent_records_the_reset_and_a_final_reset() -> None:
    decision = admit(fields(operation="rerun", agentId="coder"))
    assert decision["dispatch"] == "rerun-node"
    assert decision["started"] is True
    assert decision["events"] == [
        {"type": "run_status", "status": "running", "reason": "rerun"},
        {"type": "agent_reset", "phase": "coder", "reason": "rerun_agent"},
        {"type": "final_reset", "scope": "final_answer", "reason": "rerun_agent"},
    ]


@pytest.mark.parametrize("agent_id", ["synth", "leader", "synthesizer"])
def test_rerun_synthesizer_alias_does_not_reset_a_worker_node(agent_id: str) -> None:
    decision = admit(fields(operation="rerun", agentId=agent_id))
    assert decision["dispatch"] == "synthesis-only"
    assert decision["events"] == [
        {"type": "run_status", "status": "running", "reason": "rerun"},
        {"type": "final_reset", "scope": "final_answer", "reason": "rerun_synthesizer"},
    ]


def test_rerun_can_omit_the_final_reset_when_resynthesis_is_declined() -> None:
    decision = admit(fields(operation="rerun", agentId="critic", resynthesize=False))
    assert [event["type"] for event in decision["events"]] == ["run_status", "agent_reset"]
    assert decision["events"][1]["phase"] == "critic"


def test_admitted_tool_continuation_stays_on_the_same_effect() -> None:
    decision = admit(fields(
        operation="tool_continuation",
        status="running",
        effect="awaiting_tools",
        sameEffect=True,
        runtimeRevision=4,
        requestedRevision=4,
        agentId="researcher",
        plan=["researcher", "coder"],
    ))
    assert decision["admitted"] is True
    assert decision["dispatch"] == "same-effect"
    assert decision["started"] is False
    assert decision["bumpsRevision"] is False
    assert decision["requestedRevision"] == 4
    assert decision["events"] == []


@pytest.mark.parametrize("operation", ["resume", "rerun", "runtime_update", "tool_continuation"])
def test_unknown_effect_is_refused_for_every_operation_and_is_not_retried(operation: str) -> None:
    decision = admit(fields(
        operation=operation,
        effect="effect_unknown",
        status="running" if operation == "tool_continuation" else "failed",
        sameEffect=True,
        requestedRevision=1 if operation == "tool_continuation" else 2,
    ))
    assert decision == {
        "admitted": False,
        "httpStatus": 409,
        "code": "EFFECT_UNKNOWN",
        "message": "Agent runtime effect is unknown and was not retried",
        "dispatch": "none",
        "started": False,
        "bumpsRevision": False,
        "requestedRevision": 0,
        "events": [],
    }


@pytest.mark.parametrize(
    ("overrides", "code", "message"),
    [
        ({"operation": "delete"}, "AGENT_RUN_REQUEST_INVALID", "任务请求无效"),
        ({"identityValid": False}, "INVALID_IDENTITY", "Agent runtime identity is invalid"),
        ({"lease": "expired"}, "LEASE_NOT_HELD", "Agent runtime lease is not live"),
        ({"lease": ""}, "LEASE_NOT_HELD", "Agent runtime lease is not live"),
        ({"epoch": "stale"}, "STALE_EXECUTION_EPOCH", "Agent runtime epoch is stale"),
        ({"epoch": "mismatch"}, "FENCE_MISMATCH", "Agent runtime fence does not match"),
        ({"epoch": "promoted"}, "FENCE_MISMATCH", "Agent runtime fence does not match"),
        ({"requestedRevision": 1}, "AGENT_RUNTIME_REVISION_DENIED", "Agent runtime revision is not the next admitted revision"),
        ({"requestedRevision": 3}, "AGENT_RUNTIME_REVISION_DENIED", "Agent runtime revision is not the next admitted revision"),
        ({"runtimeRevision": 0, "requestedRevision": 1}, "AGENT_RUNTIME_REVISION_DENIED", "Agent runtime revision is not the next admitted revision"),
        ({"runtimeRevision": True, "requestedRevision": 2}, "AGENT_RUNTIME_REVISION_DENIED", "Agent runtime revision is not the next admitted revision"),
        ({"status": "created"}, "NATIVE_AGENT_ACTION_CONFLICT", "Agent run is still running"),
        ({"status": "planning"}, "NATIVE_AGENT_ACTION_CONFLICT", "Agent run is still running"),
        ({"status": "running"}, "NATIVE_AGENT_ACTION_CONFLICT", "Agent run is still running"),
        ({"status": "awaiting_plan"}, "NATIVE_AGENT_ACTION_CONFLICT", "Confirm the plan before resuming"),
        ({"status": "paused"}, "NATIVE_AGENT_ACTION_CONFLICT", "Agent run is not ready for this action"),
        ({"operation": "rerun", "agentId": ""}, "AGENT_RUN_REQUEST_INVALID", "agentId is required"),
        ({"operation": "rerun", "agentId": "ghost"}, "AGENT_RUN_REQUEST_INVALID", "Unknown Agent"),
        ({"effect": "awaiting_tools"}, "AGENT_TOOL_CONTINUATION_REQUIRED", "Confirm tool continuation before changing this run"),
        ({"plan": "researcher,coder"}, "AGENT_RUN_REQUEST_INVALID", "任务请求无效"),
    ],
)
def test_illegal_runtime_action_is_refused(overrides: dict[str, object], code: str, message: str) -> None:
    decision = admit(fields(**overrides))
    assert decision["admitted"] is False
    assert decision["dispatch"] == "none"
    assert decision["started"] is False
    assert decision["bumpsRevision"] is False
    assert decision["events"] == []
    assert decision["code"] == code
    assert decision["message"] == message


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"requestedRevision": 5}, "AGENT_RUNTIME_REVISION_DENIED"),
        ({"sameEffect": False}, "AGENT_TOOL_CONTINUATION_REQUIRED"),
        ({"effect": "none"}, "AGENT_TOOL_CONTINUATION_REQUIRED"),
        ({"status": "failed"}, "AGENT_TOOL_CONTINUATION_REQUIRED"),
        ({"plan": "researcher"}, "AGENT_TOOL_CONTINUATION_REQUIRED"),
        ({"agentId": "ghost"}, "AGENT_RUN_REQUEST_INVALID"),
        ({"agentId": "critic", "plan": ["researcher"]}, "AGENT_RUN_REQUEST_INVALID"),
    ],
)
def test_tool_continuation_refuses_a_new_effect_or_an_unknown_agent(
    overrides: dict[str, object], code: str,
) -> None:
    body = fields(
        operation="tool_continuation",
        status="running",
        effect="awaiting_tools",
        sameEffect=True,
        requestedRevision=1,
        agentId="researcher",
        plan=["researcher"],
    )
    body.update(overrides)
    decision = admit(body)
    assert decision["admitted"] is False
    assert decision["dispatch"] == "none"
    assert decision["code"] == code


def test_tool_continuation_distinguishes_a_bad_wait_from_an_unknown_agent() -> None:
    waiting = admit(fields(
        operation="tool_continuation",
        status="running",
        effect="none",
        sameEffect=True,
        requestedRevision=1,
        agentId="researcher",
        plan=["researcher"],
    ))
    unknown = admit(fields(
        operation="tool_continuation",
        status="running",
        effect="awaiting_tools",
        sameEffect=True,
        requestedRevision=1,
        agentId="ghost",
        plan=["researcher"],
    ))
    assert waiting["code"] == "AGENT_TOOL_CONTINUATION_REQUIRED"
    assert unknown["code"] == "AGENT_RUN_REQUEST_INVALID"
    assert unknown["message"] == "Unknown Agent"


@pytest.mark.parametrize(
    ("sealed", "requested", "allowed"),
    [
        (None, 1, True),
        (None, 2, True),
        (None, 5, True),
        (None, 0, False),
        (None, True, False),
        (True, 1, False),
        (1, 1, True),
        (1, 2, True),
        (1, 3, False),
        (2, 4, False),
    ],
)
def test_vault_revision_allows_the_first_seal_then_only_replay_or_the_next_one(
    sealed: Any, requested: Any, allowed: bool,
) -> None:
    assert vault_revision_allowed(sealed, requested) is allowed


def test_runtime_revision_history_starts_at_creation_and_a_gap_is_corrupt() -> None:
    assert current_runtime_revision([]) == 1
    assert current_runtime_revision([
        {"type": "run_status", "status": "running"},
        {"type": "runtime_payload", "runtimeRevision": 2},
        {"type": "agent_reset", "phase": "coder"},
        {"type": "runtime_payload", "runtimeRevision": 3},
    ]) == 3
    with pytest.raises(ValueError, match="corrupt"):
        current_runtime_revision([{"type": "runtime_payload", "runtimeRevision": 3}])
    with pytest.raises(ValueError, match="corrupt"):
        current_runtime_revision([{"type": "runtime_payload", "runtimeRevision": True}])


def test_dispatch_refuses_a_dead_lease_a_bad_epoch_and_an_unknown_effect() -> None:
    assert dispatch_allowed("expired", "match", "CONFIRMED") == ("LEASE_NOT_HELD", False)
    assert dispatch_allowed("live", "stale", "CONFIRMED") == ("STALE_EXECUTION_EPOCH", False)
    assert dispatch_allowed("live", "mismatch", "NONE") == ("FENCE_MISMATCH", False)
    assert dispatch_allowed("live", "match", "EFFECT_UNKNOWN") == ("EFFECT_UNKNOWN", False)
    assert dispatch_allowed("live", "match", "") == ("EFFECT_UNKNOWN", False)
    assert dispatch_allowed("live", "match", "AWAITING_TOOLS") == ("AGENT_DRIVER_TOOLS_PENDING", False)
    assert dispatch_allowed("live", "match", "CONFIRMED") == ("", True)
    assert dispatch_allowed("live", "match", "SETTLED_FAILURE") == ("", True)
    assert dispatch_allowed("live", "match", "NONE") == ("", True)
    assert dispatch_allowed("live", "match", "DISPATCHING") == ("EFFECT_UNKNOWN", False)


def test_rust_and_go_sources_name_the_same_refusal_and_the_real_entry() -> None:
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    routes = ROUTES.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    go_dispatch = GO_DISPATCH.read_text(encoding="utf-8")
    driver = GO_DRIVER.read_text(encoding="utf-8")
    schema = GO_SCHEMA.read_text(encoding="utf-8")
    events = GO_EVENTS.read_text(encoding="utf-8")
    for code, message in (
        ("EFFECT_UNKNOWN", "Agent runtime effect is unknown and was not retried"),
        ("LEASE_NOT_HELD", "Agent runtime lease is not live"),
        ("STALE_EXECUTION_EPOCH", "Agent runtime epoch is stale"),
        ("FENCE_MISMATCH", "Agent runtime fence does not match"),
        ("INVALID_IDENTITY", "Agent runtime identity is invalid"),
        ("AGENT_RUNTIME_REVISION_DENIED", "Agent runtime revision is not the next admitted revision"),
        ("AGENT_TOOL_CONTINUATION_REQUIRED", "Tool continuation requires the original awaiting effect"),
        ("AGENT_TOOL_CONTINUATION_REQUIRED", "Confirm tool continuation before changing this run"),
        ("NATIVE_AGENT_ACTION_CONFLICT", "Agent run is still running"),
        ("NATIVE_AGENT_ACTION_CONFLICT", "Confirm the plan before resuming"),
    ):
        assert code in rust
        assert message in rust
    assert 'dispatch: "same-effect"' in rust
    assert "pub fn admit(" in rust
    assert "pub fn vault_revision_allowed(" in rust
    assert "deepseek_policy::agent_runtime_admission::resume_stored_tools(" in routes
    assert "deepseek_policy::agent_runtime_admission::admit(" not in routes
    assert '.route("/api/agent-runs/:run_id/resume", post(resume_run))' in routes
    assert '.route("/api/agent-runs/:run_id/rerun", post(rerun_run))' in routes
    assert "current.checked_add(1)" in routes
    assert "decision.requested_revision" in routes
    decision = routes.split("async fn runtime_decision", 1)[1].split("async fn append_decision_event", 1)[0]
    assert "retained_effect(" in decision
    assert decision.index("retained_effect(") < decision.index("resume_stored_tools(")
    assert "effect: retained.effect" in decision
    assert "same_effect: retained.same_effect" in decision
    assert "&bodies" in decision
    assert "admit(" not in decision
    assert 'effect: "none"' not in decision
    bound = worker[worker.index("fn bound_reference"):worker.index("impl WorkerRpcService")]
    assert "runtime_revision != 1" not in bound
    assert "runtime_revision == 0" in bound
    assert "vault_revision_allowed(" in worker
    assert "max_runtime_revision(" in worker
    for token in (
        "LEASE_NOT_HELD",
        "STALE_EXECUTION_EPOCH",
        "FENCE_MISMATCH",
        "EFFECT_UNKNOWN",
        "AWAITING_TOOLS",
        "AGENT_DRIVER_TOOLS_PENDING",
        "CONFIRMED",
        "SETTLED_FAILURE",
    ):
        assert token in go_dispatch
        assert token in rust
    assert "AGENT_RUNTIME_RESET_REQUIRES_GENERATION" in driver
    assert 'nodeState == "retrying"' in driver
    assert "func DispatchAllowed(" in go_dispatch
    rules = contract()
    for status in rules["resumableStatuses"] + rules["activeStatuses"] + ["awaiting_plan"]:
        assert f'"{status}"' in schema
    assert "runtime_payload" in events
    assert "AdmittedRevision()+1" not in events.split("runtime_payload", 1)[1].split("if !found", 1)[0]
    assert "^uint64(0)" in events


def generation(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "identityValid": True,
        "lease": "live",
        "epoch": "match",
        "effect": "CONFIRMED",
        "resetAdmitted": True,
        "resetFenceMatches": True,
        "operationId": "agent-node-" + ("ab" * 32),
        "actionId": "action-1",
        "executionEpoch": 7,
        "nodeId": "coder",
        "resetIndex": 4,
        "existingSuccessor": "",
    }
    body.update(overrides)
    return body


def test_settled_reset_on_the_same_fence_mints_a_new_id_and_keeps_the_old_one() -> None:
    first = successor_generation(generation())
    second = successor_generation(generation(effect="SETTLED_FAILURE"))
    old = "agent-node-" + ("ab" * 32)
    expected = successor_operation_id(old, "action-1", 7, "coder", 4)
    assert first["admitted"] is True
    assert first["dispatch"] == "successor"
    assert first["retainedOperationId"] == old
    assert first["successorOperationId"] == expected
    assert first["successorOperationId"] != old
    assert str(first["successorOperationId"]).startswith("agent-successor-")
    assert first["replayed"] is False
    assert second["successorOperationId"] == expected
    replay = successor_generation(generation(existingSuccessor=expected))
    assert replay["admitted"] is True
    assert replay["replayed"] is True
    assert replay["successorOperationId"] == expected
    assert replay["retainedOperationId"] == old


def test_a_different_reset_or_epoch_mints_a_different_successor() -> None:
    original = successor_generation(generation())
    later_reset = successor_generation(generation(resetIndex=5))
    other_epoch = successor_generation(generation(executionEpoch=8))
    assert later_reset["successorOperationId"] != original["successorOperationId"]
    assert other_epoch["successorOperationId"] != original["successorOperationId"]
    assert later_reset["retainedOperationId"] == original["retainedOperationId"]
    assert other_epoch["retainedOperationId"] == original["retainedOperationId"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"effect": "EFFECT_UNKNOWN"}, "EFFECT_UNKNOWN"),
        ({"effect": ""}, "EFFECT_UNKNOWN"),
        ({"effect": "NONE"}, "EFFECT_UNKNOWN"),
        ({"effect": "DISPATCHING"}, "EFFECT_UNKNOWN"),
        ({"effect": "AWAITING_TOOLS"}, "AGENT_DRIVER_TOOLS_PENDING"),
        ({"resetAdmitted": False}, "AGENT_RUNTIME_RESET_REQUIRES_GENERATION"),
        ({"resetFenceMatches": False}, "AGENT_RUNTIME_RESET_REQUIRES_GENERATION"),
        ({"lease": "expired"}, "LEASE_NOT_HELD"),
        ({"epoch": "stale"}, "STALE_EXECUTION_EPOCH"),
        ({"epoch": "mismatch"}, "FENCE_MISMATCH"),
        ({"identityValid": False}, "INVALID_IDENTITY"),
        ({"operationId": "old\nid"}, "AGENT_RUNTIME_RESET_REQUIRES_GENERATION"),
        ({"executionEpoch": 0}, "AGENT_RUNTIME_RESET_REQUIRES_GENERATION"),
        ({"existingSuccessor": "agent-successor-other"}, "AGENT_RUNTIME_GENERATION_CONFLICT"),
    ],
)
def test_successor_generation_refuses_without_minting_a_new_id(overrides: dict[str, object], code: str) -> None:
    decision = successor_generation(generation(**overrides))
    old = "agent-node-" + ("ab" * 32)
    if overrides.get("operationId") == "old\nid":
        old = "old\nid"
    assert decision["admitted"] is False
    assert decision["dispatch"] == "none"
    assert decision["successorOperationId"] == ""
    assert decision["retainedOperationId"] == old
    assert decision["code"] == code


def test_rust_and_go_use_the_same_successor_material_and_do_not_execute_it() -> None:
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    go_decision = GO_DISPATCH.read_text(encoding="utf-8")
    driver = GO_DRIVER.read_text(encoding="utf-8")
    oracle = (ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py").read_text(encoding="utf-8")
    material = "agent-node-successor-v1\\n"
    assert material in rust
    assert material in go_decision
    assert material in oracle
    assert "agent-successor-" in rust
    assert "agent-successor-" in go_decision
    assert "agent-successor-" in oracle
    assert "pub fn successor_generation(" in rust
    assert "func SuccessorGeneration(" in go_decision
    assert "agent.SuccessorGeneration(" in driver
    assert "AGENT_RUNTIME_SUCCESSOR_HELD" in driver
    node = driver.split("func (driver *agentRunDriver) node(", 1)[1].split("func resetSharingFence", 1)[0]
    query_at = node.index("QueryAgentNodeEffect")
    successor_at = node.index("driver.successorHold(")
    execute_at = node.index("ExecuteAgentTask")
    assert query_at < successor_at < execute_at
    hold = driver.split("func (driver *agentRunDriver) successorHold", 1)[1].split(
        "func (driver *agentRunDriver) successorExecute", 1
    )[0]
    assert "Execute" not in hold


def _old_operation() -> str:
    return "agent-node-" + ("ab" * 32)


def _stored_successor() -> str:
    return successor_operation_id(_old_operation(), "action-1", 7, "coder", 4)


def execution(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "identityValid": True,
        "lease": "live",
        "epoch": "match",
        "effect": "CONFIRMED",
        "resetAdmitted": True,
        "resetFenceMatches": True,
        "operationId": _old_operation(),
        "actionId": "action-1",
        "executionEpoch": 7,
        "nodeId": "coder",
        "resetIndex": 4,
        "storedSuccessor": _stored_successor(),
        "storedOperationId": _old_operation(),
        "authorityIssued": True,
        "workerPromoted": False,
        "authorityEpoch": 8,
        "presentedEpoch": 8,
    }
    body.update(overrides)
    return body


def test_binding_keeps_the_original_operation_and_stores_the_successor() -> None:
    bound = successor_binding(generation())
    assert bound["persisted"] is True
    assert bound["replacedOperationId"] is False
    assert bound["operationId"] == _old_operation()
    assert bound["successorOperationId"] == _stored_successor()
    assert bound["successorOperationId"] != bound["operationId"]
    assert bound["resetIndex"] == 4
    assert bound["replayed"] is False
    replay = successor_binding(generation(existingSuccessor=_stored_successor()))
    assert replay["persisted"] is True
    assert replay["replayed"] is True
    assert replay["operationId"] == _old_operation()
    assert replay["successorOperationId"] == _stored_successor()


def test_same_fence_does_not_execute_and_a_later_authority_epoch_does() -> None:
    held = successor_execution(execution(authorityEpoch=7, presentedEpoch=7))
    assert held["execute"] is False
    assert held["admitted"] is False
    assert held["code"] == "AGENT_RUNTIME_SUCCESSOR_HELD"
    assert held["operationId"] == ""
    assert held["executionEpoch"] == 0
    assert held["retainedOperationId"] == _old_operation()
    assert held["retainedEpoch"] == 7
    assert held["promoted"] is False
    ran = successor_execution(execution())
    assert ran["execute"] is True
    assert ran["admitted"] is True
    assert ran["operationId"] == _stored_successor()
    assert ran["executionEpoch"] == 8
    assert ran["retainedOperationId"] == _old_operation()
    assert ran["retainedEpoch"] == 7
    assert ran["promoted"] is False
    later = successor_execution(execution(authorityEpoch=10, presentedEpoch=10))
    assert later["execute"] is True
    assert later["operationId"] == _stored_successor()
    assert later["executionEpoch"] == 10
    assert later["retainedEpoch"] == 7


def test_unknown_effect_does_not_persist_or_execute() -> None:
    bound = successor_binding(generation(effect="EFFECT_UNKNOWN"))
    assert bound["persisted"] is False
    assert bound["replacedOperationId"] is False
    assert bound["operationId"] == _old_operation()
    assert bound["successorOperationId"] == ""
    assert bound["code"] == "EFFECT_UNKNOWN"
    refused = successor_execution(execution(effect="EFFECT_UNKNOWN"))
    assert refused["execute"] is False
    assert refused["code"] == "EFFECT_UNKNOWN"
    assert refused["operationId"] == ""
    assert refused["executionEpoch"] == 0
    assert refused["promoted"] is False


def test_stale_fence_and_worker_promotion_do_not_execute() -> None:
    stale = successor_execution(execution(presentedEpoch=7))
    assert stale["execute"] is False
    assert stale["code"] == "STALE_EXECUTION_EPOCH"
    assert stale["executionEpoch"] == 0
    assert stale["operationId"] == ""
    named = successor_execution(execution(epoch="stale"))
    assert named["code"] == "STALE_EXECUTION_EPOCH"
    assert named["execute"] is False
    promoted = successor_execution(execution(workerPromoted=True, authorityEpoch=9, presentedEpoch=9))
    assert promoted["execute"] is False
    assert promoted["promoted"] is False
    assert promoted["code"] == "FENCE_MISMATCH"
    assert promoted["executionEpoch"] == 0
    assert promoted["operationId"] == ""


def test_unissued_authority_and_a_replaced_operation_do_not_execute() -> None:
    unissued = successor_execution(execution(authorityIssued=False))
    assert unissued["execute"] is False
    assert unissued["code"] == "FENCE_MISMATCH"
    assert unissued["executionEpoch"] == 0
    replaced = successor_execution(execution(storedOperationId="agent-node-" + ("cd" * 32)))
    assert replaced["execute"] is False
    assert replaced["code"] == "AGENT_RUNTIME_GENERATION_CONFLICT"
    assert replaced["retainedOperationId"] == _old_operation()
    other = successor_execution(execution(storedSuccessor="agent-successor-" + ("00" * 32)))
    assert other["execute"] is False
    assert other["code"] == "AGENT_RUNTIME_SUCCESSOR_HELD"
    assert other["operationId"] == ""


def test_driver_stores_the_successor_and_executes_it_only_after_authority() -> None:
    driver = GO_DRIVER.read_text(encoding="utf-8")
    store = (ROOT / "go/internal/store/agent_node_dispatch.go").read_text(encoding="utf-8")
    control = (ROOT / "go/internal/store/control.go").read_text(encoding="utf-8")
    journal = (ROOT / "rust/crates/deepseek-worker/src/authority_store/agent_nodes.rs").read_text(encoding="utf-8")
    opened = (ROOT / "rust/crates/deepseek-worker/src/authority_store.rs").read_text(encoding="utf-8")
    grant = (ROOT / "rust/crates/deepseek-worker/src/agent_execution_grant.rs").read_text(encoding="utf-8")
    effect = (ROOT / "rust/crates/deepseek-worker/src/agent_node_effect.rs").read_text(encoding="utf-8")
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    assert "pub fn successor_binding(" in rust
    assert "pub fn successor_execution(" in rust
    assert "func SuccessorBinding(" in GO_DISPATCH.read_text(encoding="utf-8")
    assert "func SuccessorExecution(" in GO_DISPATCH.read_text(encoding="utf-8")
    hold = driver.split("func (driver *agentRunDriver) successorHold", 1)[1].split(
        "func (driver *agentRunDriver) successorExecute", 1
    )[0]
    assert "Execute" not in hold
    assert hold.index("SuccessorGeneration") < hold.index("BindAgentNodeSuccessor")
    refused = hold.split("EFFECT_UNKNOWN", 1)[1]
    assert refused.index("return errAgentDriverUnknown") < refused.index("BindAgentNodeSuccessor")
    body = driver.split("func (driver *agentRunDriver) successorExecute", 1)[1].split(
        "func (driver *agentRunDriver) tasks", 1
    )[0]
    assert body.index("SuccessorExecution") < body.index("EFFECT_UNKNOWN")
    assert body.index("EFFECT_UNKNOWN") < body.index("STALE_EXECUTION_EPOCH")
    assert body.index("STALE_EXECUTION_EPOCH") < body.index("FENCE_MISMATCH")
    assert body.index("FENCE_MISMATCH") < body.index("AdmitSuccessorDispatch")
    assert body.index("AdmitSuccessorDispatch") < body.index("ExecuteAgentTask")
    assert "WorkerPromoted: false" in body
    assert "install_authoritative_epoch" not in body
    assert "AuthorityIssued: record.ExecutionEpoch > intent.ExecutionEpoch" in body
    assert "BindAgentNodeSuccessor(" in store
    assert "AdmitSuccessorDispatch(" in store
    assert "agentSuccessorBind" in control
    assert "agentSuccessorEpoch" in control
    assert "nativeAgentSuccessorBindEdge" in control
    assert "nativeAgentSuccessorEpochEdge" in control
    assert "UNIQUE(run_id,phase,metadata_index,node_id,epoch)" in journal
    assert "fn schema_v7(" in journal
    assert 'user_version", 7' in opened
    assert "1..=7" in opened
    assert 'strip_prefix("agent-successor-")' in grant
    assert ".admit(" in effect


def _succeeded_nodes() -> list[dict[str, object]]:
    return [
        {"id": "researcher", "state": "succeeded", "failed": False},
        {"id": "coder", "state": "succeeded", "failed": False},
    ]


def _admit_retained(events: Sequence[Any], operation: str) -> dict[str, Any]:
    retained = retained_effect(events)
    requested = 1 if operation == "tool_continuation" else 2
    return resume_stored_tools(fields(
        operation=operation,
        effect=retained["effect"],
        sameEffect=retained["sameEffect"],
        status="running" if operation == "tool_continuation" else "done",
        runtimeRevision=1,
        requestedRevision=requested,
        hasFinalAnswer=True,
        nodes=_succeeded_nodes(),
        plan=["researcher", "coder"],
        agentId="coder",
    ), events)


def test_a_settled_or_absent_effect_still_completes_resume_rerun_and_payload_update() -> None:
    streams: list[list[object]] = [
        [],
        [
            {"type": "run_status", "status": "done"},
            {"type": "agent_output", "phase": "coder", "output": {"id": "coder", "content": "done"}},
            {"type": "content", "text": "final"},
        ],
        [{"type": "node_effect", "phase": "coder", "state": "CONFIRMED"}],
        [{"type": "node_effect", "phase": "coder", "state": "SETTLED_FAILURE"}],
        [{"type": "node_effect", "phase": "coder", "state": "NONE"}],
        [
            {"type": "agent", "phase": "coder", "status": "running"},
            {"type": "agent", "phase": "coder", "status": "error"},
        ],
        [
            {"type": "node_effect", "phase": "coder", "state": "EFFECT_UNKNOWN"},
            {"type": "agent_output", "phase": "coder", "output": {"id": "coder", "content": "done"}},
        ],
    ]
    for events in streams:
        assert retained_effect(events) == {"effect": "none", "sameEffect": False}
        resume = _admit_retained(events, "resume")
        assert resume["admitted"] is True
        assert resume["httpStatus"] == 200
        assert resume["dispatch"] == "none"
        assert resume["started"] is False
        assert resume["events"] == [
            {"type": "run_status", "status": "running", "reason": "resume"},
            {"type": "run_status", "status": "done", "reason": "resume"},
        ]
        payload = _admit_retained(events, "runtime_update")
        assert payload["admitted"] is True
        assert payload["dispatch"] == "none"
        assert payload["events"] == []
        assert payload["requestedRevision"] == 2
        rerun = _admit_retained(events, "rerun")
        assert rerun["admitted"] is True
        assert rerun["dispatch"] == "rerun-node"
        assert rerun["events"][0] == {"type": "run_status", "status": "running", "reason": "rerun"}


@pytest.mark.parametrize(
    "event",
    [
        {"type": "node_effect", "phase": "coder", "state": "EFFECT_UNKNOWN"},
        {"type": "node_effect", "phase": "coder", "state": "DISPATCHING"},
        {"type": "node_effect", "phase": "coder", "state": "NOT_APPLIED"},
        {"type": "node_effect", "phase": "coder", "state": ""},
        {"type": "node_effect", "phase": "coder", "observation": "timeout"},
        {"type": "node_effect", "phase": "coder", "observation": "missing"},
        {"type": "node_effect", "phase": "coder", "observation": "absent_log"},
        {"type": "node_effect", "phase": "coder", "state": "CONFIRMED", "observation": "unavailable"},
        {"type": "agent", "phase": "coder", "status": "running"},
        [
            {"type": "node_effect", "phase": "coder", "state": "EFFECT_UNKNOWN"},
            {"type": "agent_tool_request", "phase": "coder", "toolCount": 1},
            {"type": "node_effect", "phase": "coder", "state": "NONE"},
            {"type": "node_effect", "phase": "coder", "state": "NOT_APPLIED"},
        ],
        "unreadable",
    ],
)
def test_unknown_or_unreadable_effect_refuses_resume_rerun_and_payload_update(event: object) -> None:
    events = event if isinstance(event, list) else [event]
    retained = retained_effect(events)
    assert retained == {"effect": "effect_unknown", "sameEffect": False}
    for operation in ("resume", "rerun", "runtime_update"):
        decision = _admit_retained(events, operation)
        assert decision["admitted"] is False
        assert decision["httpStatus"] == 409
        assert decision["code"] == "EFFECT_UNKNOWN"
        assert decision["dispatch"] == "none"
        assert decision["events"] == []
        assert decision["started"] is False


def test_an_open_tool_request_stays_on_the_same_effect_and_runs_no_tool_body() -> None:
    events = [{
        "type": "agent_tool_request",
        "phase": "coder",
        "toolCount": 1,
        "toolContinuation": {"toolCalls": [{"id": "call-1"}]},
    }]
    retained = retained_effect(events)
    assert retained == {"effect": "awaiting_tools", "sameEffect": True}
    public_only = [{"type": "agent_tool_request", "phase": "coder", "toolCount": 1}]
    assert retained_effect(public_only) == {"effect": "awaiting_tools", "sameEffect": True}
    assert retained_effect([{"type": "agent_tool_request", "phase": "coder"}])["effect"] == "effect_unknown"
    for operation in ("resume", "rerun", "runtime_update"):
        decision = _admit_retained(events, operation)
        assert decision["admitted"] is False
        assert decision["code"] == "AGENT_TOOL_CONTINUATION_REQUIRED"
        assert decision["events"] == []
    continued = _admit_retained(events, "tool_continuation")
    assert continued["admitted"] is True
    assert continued["dispatch"] == "same-effect"
    assert continued["started"] is False
    assert continued["events"] == []
    assert all(item.get("type") != "agent_output" for item in continued["events"])
    settled = events + [{"type": "agent_output", "phase": "coder", "output": {"id": "coder", "content": "done"}}]
    assert retained_effect(settled) == {"effect": "none", "sameEffect": False}


def test_public_routes_pass_the_retained_effect_into_admit() -> None:
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    routes = ROUTES.read_text(encoding="utf-8")
    classifier = rust.split("pub struct RetainedEffect", 1)[1].split("pub fn admit", 1)[0]
    assert "pub fn retained_effect(" in rust
    for token in (
        "effect_unknown",
        "awaiting_tools",
        "NOT_APPLIED",
        "agent_tool_request",
        "CONFIRMED",
        "observation",
        "DISPATCHING",
    ):
        assert token in classifier
    decision = routes.split("async fn runtime_decision", 1)[1].split("async fn append_decision_event", 1)[0]
    assert decision.index("materialize_node_effects(") < decision.index("session.bodies(")
    assert decision.index("session.bodies(") < decision.index("retained_effect(")
    assert decision.index("retained_effect(") < decision.index("resume_stored_tools(")
    assert "effect: retained.effect" in decision
    assert "same_effect: retained.same_effect" in decision
    assert "&bodies" in decision
    assert "admit(" not in decision
    assert 'effect: "none"' not in decision
    assert "runtime_decision(&mut session, \"resume\"" in routes
    assert "runtime_decision(&mut session, \"rerun\"" in routes


_STATE_TEMPLATE = '{"type":"node_effect","phase":"%s","status":"%s","state":"%s"}'
_OBSERVATION_TEMPLATE = '{"type":"node_effect","phase":"%s","scope":"%s","observation":"%s"}'


def _template_present(text: str, template: str) -> bool:
    return template in text or template.replace('"', '\\"') in text


def _assert_projection_refuses(projected: dict[str, Any]) -> None:
    assert projected["append"] is True
    assert "NOT_APPLIED" not in projected["bytes"].decode("utf-8")
    assert projected["sha256"] == hashlib.sha256(projected["bytes"]).hexdigest()
    assert projected["length"] == len(projected["bytes"])
    assert retained_effect([projected["event"]]) == {"effect": "effect_unknown", "sameEffect": False}
    for operation in ("resume", "rerun", "runtime_update"):
        decision = _admit_retained([projected["event"]], operation)
        assert decision["admitted"] is False
        assert decision["httpStatus"] == 409
        assert decision["code"] == "EFFECT_UNKNOWN"
        assert decision["dispatch"] == "none"
        assert decision["events"] == []
        assert decision["started"] is False


def test_a_projected_unknown_row_refuses_and_a_settled_projection_completes() -> None:
    unknown = project_journal_observation("coder", True, "EFFECT_UNKNOWN", None)
    literal = b'{"type":"node_effect","phase":"coder","status":"EFFECT_UNKNOWN","state":"EFFECT_UNKNOWN"}'
    assert unknown["bytes"] == literal
    assert unknown["bytes"] == journal_observation_bytes("coder", "EFFECT_UNKNOWN", "")
    assert unknown["event"]["state"] == "EFFECT_UNKNOWN"
    _assert_projection_refuses(unknown)
    for state in ("DISPATCHING", ""):
        projected = project_journal_observation("coder", True, state, None)
        assert projected["append"] is True
        assert projected["event"]["state"] == ("DISPATCHING" if state == "DISPATCHING" else "")
        _assert_projection_refuses(projected)
    waiting = project_journal_observation("coder", True, "AWAITING_TOOLS", None)
    assert waiting["append"] is True
    assert waiting["event"]["state"] == "AWAITING_TOOLS"
    assert retained_effect([waiting["event"]]) == {"effect": "awaiting_tools", "sameEffect": True}
    for operation in ("resume", "rerun", "runtime_update"):
        decision = _admit_retained([waiting["event"]], operation)
        assert decision["admitted"] is False
        assert decision["code"] == "AGENT_TOOL_CONTINUATION_REQUIRED"
        assert decision["events"] == []
    settled = project_journal_observation("coder", True, "CONFIRMED", None)
    assert settled["append"] is False
    assert settled["event"]["state"] == "CONFIRMED"
    assert retained_effect([settled["event"]]) == {"effect": "none", "sameEffect": False}
    resume = _admit_retained([settled["event"]], "resume")
    assert resume["admitted"] is True
    assert resume["httpStatus"] == 200
    assert resume["dispatch"] == "none"
    assert resume["events"] == [
        {"type": "run_status", "status": "running", "reason": "resume"},
        {"type": "run_status", "status": "done", "reason": "resume"},
    ]
    rerun = _admit_retained([settled["event"]], "rerun")
    assert rerun["admitted"] is True
    assert rerun["events"][0] == {"type": "run_status", "status": "running", "reason": "rerun"}
    payload = _admit_retained([settled["event"]], "runtime_update")
    assert payload["admitted"] is True
    assert payload["dispatch"] == "none"
    assert payload["events"] == []


def test_a_missing_timeout_or_absent_log_is_not_state_not_applied() -> None:
    missing = project_journal_observation("coder", False, "NOT_APPLIED", None)
    missing_literal = b'{"type":"node_effect","phase":"coder","scope":"missing","observation":"missing"}'
    assert missing["bytes"] == missing_literal
    assert missing["event"] == {"type": "node_effect", "phase": "coder", "scope": "missing", "observation": "missing"}
    assert "state" not in missing["event"]
    _assert_projection_refuses(missing)
    timeout = project_journal_observation("coder", True, "CONFIRMED", "context deadline exceeded")
    assert timeout["event"]["observation"] == "timeout"
    assert "state" not in timeout["event"]
    _assert_projection_refuses(timeout)
    cancelled = project_journal_observation("coder", True, "CONFIRMED", "context canceled")
    assert cancelled["event"]["observation"] == "timeout"
    absent = project_journal_observation("coder", False, None, "worker ABSENT_LOG")
    assert absent["event"]["observation"] == "absent_log"
    assert "state" not in absent["event"]
    _assert_projection_refuses(absent)
    logged = project_journal_observation("coder", True, "EFFECT_UNKNOWN", "worker log is missing")
    assert logged["event"]["observation"] == "unavailable"
    assert logged["event"]["observation"] != "absent_log"
    _assert_projection_refuses(logged)
    unapplied = project_journal_observation("coder", True, "NOT_APPLIED", None)
    assert unapplied["event"]["observation"] == "not_applied"
    assert "state" not in unapplied["event"]
    assert "NOT_APPLIED" not in unapplied["bytes"].decode("utf-8")
    _assert_projection_refuses(unapplied)
    invalid = project_journal_observation("bad node", True, "EFFECT_UNKNOWN", None)
    assert invalid["append"] is True
    assert invalid["event"]["phase"] == ""
    assert invalid["event"]["observation"] == "unavailable"
    assert b"bad node" not in invalid["bytes"]
    _assert_projection_refuses(invalid)


def test_driver_projects_a_journal_read_before_execute_and_the_gateway_materializes_it() -> None:
    oracle = ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py"
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    routes = ROUTES.read_text(encoding="utf-8")
    driver = GO_DRIVER.read_text(encoding="utf-8")
    go = GO_DISPATCH.read_text(encoding="utf-8")
    for text in (oracle.read_text(encoding="utf-8"), rust, go):
        assert _template_present(text, _STATE_TEMPLATE)
        assert _template_present(text, _OBSERVATION_TEMPLATE)
    node = driver.split("func (driver *agentRunDriver) node(", 1)[1].split("func resetSharingFence", 1)[0]
    assert node.index("projectJournalObservation(") < node.index("ExecuteAgentTask")
    assert "NOT_APPLIED" not in node
    helper = driver.split("func (driver *agentRunDriver) projectJournalObservation(", 1)[1].split(
        "func (driver *agentRunDriver) appendJournalObservation(", 1
    )[0]
    assert "ProjectJournalObservation(" in helper
    assert helper.index("ProjectJournalObservation(") < helper.index("decision.Append")
    assert "AppendAgentRunEvent" not in helper
    assert "NOT_APPLIED" not in helper
    appended = driver.split("func (driver *agentRunDriver) appendJournalObservation(", 1)[1].split(
        "func journalObservationAlreadyPublic(", 1
    )[0]
    assert "AppendAgentRunEvent(" in appended
    assert "ActionId: runID" in appended
    assert "ExecutionEpoch: record.ExecutionEpoch" in appended
    assert "claim.Fence" not in appended
    assert "NOT_APPLIED" not in appended
    cancel = driver.split("func (driver *agentRunDriver) recoverCancellation(", 1)[1].split("\nfunc ", 1)[0]
    queried = cancel.split("QueryAgentNodeEffect(", 1)[1]
    assert queried.index("projectJournalObservation(") < queried.index("errAgentDriverUnknown")
    missing = go.split("func ProjectJournalObservation(", 1)[1].split("if !found {", 1)[1].split("switch state", 1)[0]
    assert '"missing"' in missing
    assert "NOT_APPLIED" not in missing
    rust_missing = rust.split("pub fn project_journal_observation(", 1)[1].split("if !found {", 1)[1].split(
        "let Some(state)", 1
    )[0]
    assert '"missing"' in rust_missing
    assert "NOT_APPLIED" not in rust_missing
    materialize = routes.split("async fn materialize_node_effects(", 1)[1].split("async fn runtime_decision(", 1)[0]
    assert "node_effect_value(" in materialize
    assert "open_native(" in materialize
    assert ".put(" in materialize
    assert "QueryEffect" not in materialize
    assert "NOT_APPLIED" not in materialize
    assert "python_disabled" in materialize


def _chart_call(call_id: str = "call-chart") -> dict[str, Any]:
    return {
        "id": call_id,
        "function": {
            "name": "generate_chart",
            "arguments": {
                "type": "bar",
                "title": "Load",
                "data": [{"label": "a", "value": 1}, {"label": "b", "value": 2}],
            },
        },
    }


def _tool_request(calls: list[dict[str, Any]], *, phase: str = "coder") -> dict[str, Any]:
    return {
        "type": "agent_tool_request",
        "phase": phase,
        "toolCount": len(calls),
        "toolContinuation": {"toolCalls": calls},
    }


def _continuation_fields(**overrides: object) -> dict[str, object]:
    body = fields(
        operation="tool_continuation",
        status="running",
        effect="awaiting_tools",
        sameEffect=True,
        runtimeRevision=1,
        requestedRevision=1,
        agentId="coder",
        plan=["coder"],
    )
    body.update(overrides)
    return body


def test_a_legal_tool_continuation_returns_the_dispatcher_result() -> None:
    call = _chart_call()
    direct = execute_tool_call(call)
    assert direct["ok"] is True
    assert direct["tool"] == "generate_chart"
    assert "markdownTable" in direct["result"]
    events = [_tool_request([call])]
    decision = continue_retained_tools(_continuation_fields(), events)
    assert decision["admitted"] is True
    assert decision["dispatch"] == "same-effect"
    assert decision["started"] is False
    assert decision["bumpsRevision"] is False
    assert decision["events"] == [{
        "type": "agent_tool_result",
        "phase": "coder",
        "toolCallId": "call-chart",
        "output": direct,
    }]
    assert decision["events"][0]["output"] == execute_tool_call(call)
    assert all(event.get("type") != "node_effect" for event in decision["events"])
    assert all(event.get("state") != "CONFIRMED" for event in decision["events"])
    nameless = {"id": "call-1"}
    unnamed = continue_retained_tools(_continuation_fields(), [_tool_request([nameless])])
    assert unnamed["events"][0]["output"] == execute_tool_call(nameless)
    assert unnamed["events"][0]["output"]["ok"] is False
    assert "markdownTable" not in json.dumps(unnamed)
    public_only = [{"type": "agent_tool_request", "phase": "coder", "toolCount": 1}]
    invented = continue_retained_tools(_continuation_fields(), public_only)
    assert invented["admitted"] is True
    assert invented["dispatch"] == "same-effect"
    assert invented["events"] == []
    extra = [_chart_call(f"call-{index}") for index in range(MAX_TOOL_CALLS_PER_RESPONSE + 1)]
    capped = continue_retained_tools(_continuation_fields(), [_tool_request(extra)])
    assert len(capped["events"]) == MAX_TOOL_CALLS_PER_RESPONSE
    assert capped["events"][0]["output"] == execute_tool_call(extra[0])


def test_tool_continuation_refuses_unknown_and_keeps_a_later_settlement_visible() -> None:
    call = _chart_call()
    request = _tool_request([call])
    legal = _continuation_fields()
    for overrides in (
        {"lease": "expired"},
        {"epoch": "stale"},
        {"epoch": "promoted"},
        {"identityValid": False},
        {"agentId": "ghost"},
        {"effect": "effect_unknown"},
        {"sameEffect": False},
    ):
        refused = continue_retained_tools(_continuation_fields(**overrides), [request])
        assert refused["admitted"] is False
        assert refused["events"] == []
        assert refused["dispatch"] == "none"
        assert "markdownTable" not in json.dumps(refused)
    missing = project_journal_observation("coder", False, "NOT_APPLIED", None)
    assert missing["event"]["observation"] == "missing"
    assert "NOT_APPLIED" not in missing["bytes"].decode("utf-8")
    blocked = continue_retained_tools(legal, [missing["event"], request])
    assert blocked["admitted"] is False
    assert blocked["code"] == "EFFECT_UNKNOWN"
    assert blocked["events"] == []
    assert "NOT_APPLIED" not in json.dumps(blocked["events"])
    for observation in ("timeout", "absent_log"):
        projected = project_journal_observation("coder", False, None, observation)
        assert projected["event"]["observation"] == observation
        assert "NOT_APPLIED" not in projected["bytes"].decode("utf-8")
        refused = continue_retained_tools(legal, [projected["event"], request])
        assert refused["code"] == "EFFECT_UNKNOWN"
        assert refused["events"] == []
    produced = continue_retained_tools(legal, [request])
    assert produced["events"][0]["output"]["ok"] is True
    settled = [request, *produced["events"], {
        "type": "agent_output",
        "phase": "coder",
        "output": {"id": "coder", "content": "done"},
    }]
    assert retained_effect(settled) == {"effect": "none", "sameEffect": False}
    resume = _admit_retained(settled, "resume")
    assert resume["admitted"] is True
    assert resume["events"] == [
        {"type": "run_status", "status": "running", "reason": "resume"},
        {"type": "run_status", "status": "done", "reason": "resume"},
    ]
    confirmed = project_journal_observation("coder", True, "CONFIRMED", None)
    assert confirmed["append"] is False
    visible = [request, *produced["events"], confirmed["event"]]
    assert retained_effect(visible) == {"effect": "none", "sameEffect": False}
    assert _admit_retained(visible, "resume")["events"][-1]["status"] == "done"


def test_worker_persists_the_tool_request_before_the_workspace_tool() -> None:
    oracle = (ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py").read_text(encoding="utf-8")
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    worker = (ROOT / "rust/crates/deepseek-worker/src/agent_task_executor.rs").read_text(encoding="utf-8")
    publisher = (ROOT / "rust/crates/deepseek-worker/src/agent_plan_executor.rs").read_text(encoding="utf-8")
    chat = (ROOT / "rust/crates/deepseek-gateway/src/chat_tool_loop.rs").read_text(encoding="utf-8")
    driver = GO_DRIVER.read_text(encoding="utf-8")
    continued = rust.split("pub fn continue_retained_tools(", 1)[1].split("fn hex_encode(", 1)[0]
    assert "retained_effect(" in continued
    assert continued.index("admit(") < continued.index("crate::tool_dispatch::dispatch(")
    assert "deny_store_write(" in continued
    assert continued.index("deny_store_write(") < continued.index("crate::tool_dispatch::dispatch(")
    assert "MAX_TOOL_CALLS_PER_RESPONSE" in continued
    assert "NOT_APPLIED" not in continued.split("does not", 1)[-1]
    assert '"CONFIRMED"' not in continued
    assert "ExecutorContext::default()" not in continued
    oracle_fn = oracle.split("def continue_retained_tools(", 1)[1].split("def _canonical_token(", 1)[0]
    assert "execute_tool_call(" in oracle_fn
    assert oracle_fn.index("admit(") < oracle_fn.index("execute_tool_call(")
    assert "MAX_TOOL_CALLS_PER_RESPONSE" in oracle_fn
    assert "_execute_retained_tool(" in oracle_fn
    helper = oracle.split("def _execute_retained_tool(", 1)[1].split("def _canonical_token(", 1)[0]
    assert "REMINDERS_FILE" in helper
    assert helper.index("try:") < helper.index("execute_tool_call(")
    assert "finally:" in helper
    retain = worker.split("fn retain_workspace_tool_results(", 1)[1].split("fn task_envelope(", 1)[0]
    assert retain.index("read_retained_tool_results(") < retain.index("continue_retained_tools(")
    assert retain.index("continue_retained_tools(") < retain.index("store_retained_tool_results(")
    assert "with_chat_execution_context(" in retain
    assert "for_workspace(" in retain
    assert "executor.workspace_root.clone()" in retain
    assert "retained_store_denial(" in retain
    assert 'lease: if lease_live { "live" } else { "expired" }' in retain
    assert "NOT_APPLIED" not in retain
    assert "CONFIRMED" not in retain
    live = worker.split("let envelope = if synthesis {", 1)[1]
    assert live.index(".confirm_agent_node_effect(") < live.index("retain_workspace_tool_results(")
    between = live.split(".confirm_agent_node_effect(", 1)[1].split("retain_workspace_tool_results(", 1)[0]
    assert between.index("admitted_provider_send(") < between.index("execution_send_lease(")
    assert between.index("execution_send_lease(") < between.index(".allowed")
    assert "lease_live" in between
    assert "deadline > current" not in between
    assert "deadline > current" not in worker
    replay = worker.split("AgentNodeEffectStart::Confirmed(outcome)", 1)[1].split(
        "AgentNodeEffectStart::EffectUnknown", 1
    )[0]
    assert "continue_retained_tools(" not in replay
    assert "retain_workspace_tool_results(" not in replay
    sync = chat.split("pub fn execute_call_sync(", 1)[1].split("pub async fn run_round(", 1)[0]
    assert "with_chat_execution_context(" in sync
    builder = chat.split("fn bind_chat_execution_context<T>(", 1)[1].split("fn policy_from_env(", 1)[0]
    assert "WorkspaceBundle::new" not in builder
    assert "bundle.view()" in builder
    assert "FetchContext" in builder
    assert "SearchProvider::callback" in builder
    assert "fn for_workspace(" in chat
    assert "WorkspaceBundle::new(root)" in chat
    store = publisher.split("fn store_retained_tool_results(", 1)[1].split("async fn prepare_preset(", 1)[0]
    assert "create_new(true)" in store
    assert "CONFIRMED" not in store
    assert "agent_node_effects" not in store
    publish = publisher.split("async fn publish_known(", 1)[1].split("#[tonic::async_trait]", 1)[0]
    assert "read_retained_tool_results(" in publish
    assert publish.index("toolResults") < publish.index('"AWAITING_TOOLS"')
    assert "toolContinuation" not in publish.split("if pending_tools {", 1)[1].split("let root", 1)[0]
    node = driver.split("func (driver *agentRunDriver) node(", 1)[1].split("func resetSharingFence(", 1)[0]
    pending = node.split('effect.State == "AWAITING_TOOLS"', 1)[1].split(
        "return nil, errAgentDriverUnknown", 1
    )[0]
    assert "retainedDriverToolResults(" in pending
    assert "errAgentDriverTools" in pending
    assert "CONFIRMED" not in pending


def test_retained_list_reminders_reads_the_supplied_workspace() -> None:
    import tempfile

    import deepseek_infra.infra.data.reminders as reminders

    real_file = reminders.REMINDERS_FILE
    real_dir = reminders.REMINDERS_DIR
    reminder = {
        "id": "r-temp",
        "title": "stand",
        "content": "up",
        "dueAt": "2099-01-01T00:00:00Z",
        "notified": False,
    }
    encoded = json.dumps([reminder])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = root / ".reminders"
        store.mkdir()
        (store / "reminders.json").write_text(encoded, encoding="utf-8")
        call = {
            "id": "call-rem",
            "function": {"name": "list_reminders", "arguments": {"status": "active"}},
        }
        request = _tool_request([call])
        decision = continue_retained_tools(_continuation_fields(), [request], workspace_root=root)
        assert decision["admitted"] is True
        assert decision["dispatch"] == "same-effect"
        assert decision["started"] is False
        assert decision["bumpsRevision"] is False
        output = decision["events"][0]["output"]
        assert output["ok"] is True
        assert output["tool"] == "list_reminders"
        assert output["result"]["count"] == 1
        assert output["result"]["reminders"][0]["id"] == "r-temp"
        assert output["result"]["reminders"][0]["title"] == "stand"
        assert (store / "reminders.json").read_text(encoding="utf-8") == encoded
        assert decision["events"][0]["type"] == "agent_tool_result"
        assert all(event.get("state") != "CONFIRMED" for event in decision["events"])
        refused = continue_retained_tools(
            _continuation_fields(lease="expired"),
            [request],
            workspace_root=root,
        )
        assert refused["admitted"] is False
        assert refused["events"] == []
        assert "r-temp" not in json.dumps(refused)
        missing = project_journal_observation("coder", False, "NOT_APPLIED", None)
        blocked = continue_retained_tools(
            _continuation_fields(),
            [missing["event"], request],
            workspace_root=root,
        )
        assert blocked["code"] == "EFFECT_UNKNOWN"
        assert blocked["events"] == []
        assert "NOT_APPLIED" not in json.dumps(blocked["events"])
        assert "r-temp" not in json.dumps(blocked)
        assert (store / "reminders.json").read_text(encoding="utf-8") == encoded
    assert reminders.REMINDERS_FILE == real_file
    assert reminders.REMINDERS_DIR == real_dir
    chart = _chart_call()
    plain = continue_retained_tools(_continuation_fields(), [_tool_request([chart])])
    assert plain["events"][0]["output"] == execute_tool_call(chart)


def _stored_result(call_id: str, marker: str) -> dict[str, Any]:
    return {
        "type": "agent_tool_result",
        "phase": "coder",
        "toolCallId": call_id,
        "output": {"ok": True, "tool": "list_reminders", "result": {"id": marker}},
    }


def _request_with_results(*results: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "agent_tool_request",
        "phase": "coder",
        "toolCount": len(results) or 1,
        "toolContinuation": {
            "toolCalls": [{"id": item["toolCallId"], "function": {"name": "list_reminders", "arguments": {}}}]
            for item in results
        },
        "toolResults": list(results),
    }


def test_resume_and_rerun_return_stored_tool_results_without_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(call: object) -> dict[str, object]:
        raise AssertionError(call)

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.tools.execute_tool_call", forbidden)
    stored = _stored_result("call-stored", "stored-rem")
    request = _request_with_results(stored)
    for operation in ("resume", "rerun"):
        decision = resume_stored_tools(fields(operation=operation, effect="none", sameEffect=False), [request])
        assert decision["admitted"] is True
        assert decision["httpStatus"] == 200
        assert decision["code"] == ""
        assert decision["dispatch"] == "none"
        assert decision["started"] is False
        assert decision["bumpsRevision"] is False
        assert decision["requestedRevision"] == 2
        assert decision["events"] == [stored]
        assert decision["events"][0] is not stored
        assert decision["events"][0]["type"] == "agent_tool_result"
        assert all(event.get("type") != "node_effect" for event in decision["events"])
        assert all(event.get("state") != "CONFIRMED" for event in decision["events"])
        assert "NOT_APPLIED" not in json.dumps(decision)
        assert "CONFIRMED" not in json.dumps(decision)
        assert decision["events"] != [
            {"type": "run_status", "status": "running", "reason": operation},
            {"type": "run_status", "status": "done", "reason": operation},
        ]
    second = _stored_result("call-2", "stored-chart")
    partial = resume_stored_tools(fields(operation="rerun"), [_request_with_results(stored, second), stored])
    assert partial["admitted"] is True
    assert partial["dispatch"] == "none"
    assert partial["bumpsRevision"] is False
    assert partial["events"] == [second]
    again = resume_stored_tools(fields(operation="resume"), [request, stored])
    assert again["admitted"] is True
    assert again["events"] == []
    assert again["dispatch"] == "none"
    assert again["started"] is False
    settled = [request, stored, {"type": "agent_output", "phase": "coder", "output": {"id": "coder", "content": "done"}}]
    assert retained_effect(settled) == {"effect": "none", "sameEffect": False}
    done = _admit_retained(settled, "resume")
    assert done["admitted"] is True
    assert done["events"] == [
        {"type": "run_status", "status": "running", "reason": "resume"},
        {"type": "run_status", "status": "done", "reason": "resume"},
    ]


def test_a_missing_tool_result_file_stays_awaiting_tools() -> None:
    calls = {"toolCalls": [{"id": "call-1", "function": {"name": "list_reminders", "arguments": {}}}]}
    absent = {"type": "agent_tool_request", "phase": "coder", "toolCount": 1, "toolContinuation": calls}
    empty = {**absent, "toolResults": []}
    wrong = {**absent, "toolResults": [{"type": "note", "text": "missing file"}]}
    not_a_list = {**absent, "toolResults": {"type": "agent_tool_result", "toolCallId": "call-1"}}
    for events in ([absent], [empty], [wrong], [not_a_list]):
        assert retained_effect(events) == {"effect": "awaiting_tools", "sameEffect": True}
        for operation in ("resume", "rerun"):
            decision = resume_stored_tools(fields(operation=operation, effect="none"), events)
            assert decision["admitted"] is False
            assert decision["httpStatus"] == 409
            assert decision["code"] == "AGENT_TOOL_CONTINUATION_REQUIRED"
            assert decision["message"] == "Confirm tool continuation before changing this run"
            assert decision["events"] == []
            assert decision["dispatch"] == "none"
            assert "NOT_APPLIED" not in json.dumps(decision)
    stored = _stored_result("call-stored", "stored-rem")
    request = _request_with_results(stored)
    update = resume_stored_tools(
        fields(operation="runtime_update", effect="none", sameEffect=False, status="done"),
        [request],
    )
    assert update["admitted"] is False
    assert update["code"] == "AGENT_TOOL_CONTINUATION_REQUIRED"
    assert update["events"] == []
    assert "stored-rem" not in json.dumps(update)
    for overrides, code in (
        ({"lease": "expired"}, "LEASE_NOT_HELD"),
        ({"epoch": "stale"}, "STALE_EXECUTION_EPOCH"),
        ({"epoch": "promoted"}, "FENCE_MISMATCH"),
        ({"identityValid": False}, "INVALID_IDENTITY"),
        ({"requestedRevision": 1}, "AGENT_RUNTIME_REVISION_DENIED"),
        ({"requestedRevision": 3}, "AGENT_RUNTIME_REVISION_DENIED"),
    ):
        refused = resume_stored_tools(fields(operation="resume", **overrides), [request])
        assert refused["admitted"] is False
        assert refused["code"] == code
        assert refused["events"] == []
        assert refused["dispatch"] == "none"
        assert "stored-rem" not in json.dumps(refused)
    unknown = {"type": "node_effect", "phase": "researcher", "observation": "missing"}
    blocked = resume_stored_tools(fields(operation="rerun", effect="none"), [request, unknown])
    assert blocked["admitted"] is False
    assert blocked["code"] == "EFFECT_UNKNOWN"
    assert blocked["events"] == []
    assert "NOT_APPLIED" not in json.dumps(blocked)
    assert "stored-rem" not in json.dumps(blocked)


def test_resume_source_copies_stored_results_and_does_not_call_the_tool() -> None:
    oracle = (ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py").read_text(encoding="utf-8")
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    routes = ROUTES.read_text(encoding="utf-8")
    driver = GO_DRIVER.read_text(encoding="utf-8")
    function = rust.split("pub fn resume_stored_tools(", 1)[1].split("fn retained_tool_calls(", 1)[0]
    assert function.index("retained_effect(") < function.index("admit(")
    assert function.index("admit(") < function.index("events: fresh")
    assert "stored_tool_results(" in function
    assert "tool_dispatch::dispatch" not in function
    assert "execute_tool_call" not in function
    assert "NOT_APPLIED" not in function
    assert '"CONFIRMED"' not in function
    assert 'dispatch: "none"' in function
    assert "bumps_revision: false" in function
    body = oracle.split("def resume_stored_tools(", 1)[1].split("def _retained_tool_calls(", 1)[0]
    assert body.index("retained_effect(") < body.index("admit(")
    assert "execute_tool_call(" not in body
    assert "NOT_APPLIED" not in body
    assert "CONFIRMED" not in body
    assert '"dispatch": "none"' in body
    assert '"bumpsRevision": False' in body
    apply = routes.split("async fn apply_runtime_action(", 1)[1].split("async fn resume_run(", 1)[0]
    assert "for event in &decision.events" in apply
    assert "append_decision_event(session, event)" in apply
    assert "CONFIRMED" not in apply.split("for event in &decision.events", 1)[1]
    node = driver.split("func (driver *agentRunDriver) node(", 1)[1].split("func resetSharingFence(", 1)[0]
    pending = node.split('effect.State == "AWAITING_TOOLS"', 1)[1].split("return nil, errAgentDriverUnknown", 1)[0]
    assert "retainedDriverToolResults(" in pending
    assert "errAgentDriverTools" in pending
    assert "CONFIRMED" not in pending


def _public_ref(seed: str) -> dict[str, Any]:
    return {"sha256": seed * 32, "length": 12}


def _result_header(scope: str, seed: str) -> dict[str, Any]:
    return {
        "type": "agent_tool_result",
        "phase": "coder",
        "scope": scope,
        "bodyRef": {"sha256": seed * 32, "length": 4},
    }


def _result_marker(public: dict[str, Any], phase: str = "coder") -> dict[str, Any]:
    return {
        "type": "agent_tool_request",
        "phase": phase,
        "scope": "retained_tool_results",
        "bodyRef": dict(public),
    }


def test_driver_returns_stored_tool_results_without_confirming(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(call: object) -> dict[str, object]:
        raise AssertionError(call)

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.tools.execute_tool_call", forbidden)
    stored = _stored_result("call-stored", "stored-rem")
    request = _request_with_results(stored)
    decision = retained_driver_tool_results("AWAITING_TOOLS", [request])
    assert decision["returned"] is True
    assert decision["code"] == ""
    assert decision["events"] == [stored]
    assert decision["events"][0] is not stored
    assert decision["events"][0]["type"] == "agent_tool_result"
    assert "NOT_APPLIED" not in json.dumps(decision)
    assert "CONFIRMED" not in json.dumps(decision)
    again = retained_driver_tool_results("awaiting_tools", [request, stored])
    assert again["returned"] is True
    assert again["events"] == [stored]
    assert again["events"][0] is not stored
    public = _public_ref("ab")
    header = _result_header("call-1", "cd")
    projected = retained_driver_tool_results("AWAITING_TOOLS", [_result_marker(public), header], public)
    assert projected["returned"] is True
    assert projected["code"] == ""
    assert projected["events"] == [header]
    assert projected["events"][0] is not header
    assert "NOT_APPLIED" not in json.dumps(projected)
    duplicate = retained_driver_tool_results(
        "AWAITING_TOOLS",
        [_result_marker(public), header, dict(header)],
        public,
    )
    assert duplicate["events"] == [header]
    other = _result_header("call-2", "ef")
    both = retained_driver_tool_results("AWAITING_TOOLS", [_result_marker(public), header, other], public)
    assert both["returned"] is True
    assert both["events"] == [header, other]
    assert dispatch_allowed("live", "match", "AWAITING_TOOLS") == ("AGENT_DRIVER_TOOLS_PENDING", False)


def test_driver_missing_tool_result_stays_pending() -> None:
    calls = {"toolCalls": [{"id": "call-1", "function": {"name": "list_reminders", "arguments": {}}}]}
    absent = {"type": "agent_tool_request", "phase": "coder", "toolCount": 1, "toolContinuation": calls}
    empty = {**absent, "toolResults": []}
    wrong = {**absent, "toolResults": [{"type": "note", "text": "missing file"}]}
    not_a_list = {**absent, "toolResults": {"type": "agent_tool_result", "toolCallId": "call-1"}}
    public = _public_ref("ab")
    for events in ([absent], [empty], [wrong], [not_a_list], []):
        decision = retained_driver_tool_results("AWAITING_TOOLS", events, public)
        assert decision == {"returned": False, "code": "AGENT_DRIVER_TOOLS_PENDING", "events": []}
        assert "NOT_APPLIED" not in json.dumps(decision)
    header = _result_header("call-1", "cd")
    other_public = _public_ref("ef")
    cases = (
        [_result_marker(other_public), header],
        [_result_marker(public)],
        [header],
        [_result_marker(public, phase=""), header],
        [{**_result_marker(public), "scope": "other"}, header],
        [_result_marker(public), {**header, "bodyRef": {"sha256": "AB" * 32, "length": 4}}],
        [_result_marker(public), {**header, "bodyRef": {"sha256": "ab", "length": 4}}],
        [_result_marker({"sha256": "ab", "length": 4}), header],
    )
    for events in cases:
        decision = retained_driver_tool_results("AWAITING_TOOLS", events, public)
        assert decision["returned"] is False
        assert decision["code"] == "AGENT_DRIVER_TOOLS_PENDING"
        assert decision["events"] == []
        assert "NOT_APPLIED" not in json.dumps(decision)
    idle = retained_driver_tool_results("NONE", [_request_with_results(_stored_result("call-stored", "stored-rem"))], public)
    assert idle == {"returned": False, "code": "", "events": []}
    assert "stored-rem" not in json.dumps(idle)


def test_driver_source_returns_stored_results_at_both_sites() -> None:
    driver = GO_DRIVER.read_text(encoding="utf-8")
    oracle = (ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py").read_text(encoding="utf-8")
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    publisher = (ROOT / "rust/crates/deepseek-worker/src/agent_plan_executor.rs").read_text(encoding="utf-8")
    grant = (ROOT / "rust/crates/deepseek-worker/src/agent_execution_grant.rs").read_text(encoding="utf-8")
    go_admit = GO_DISPATCH.read_text(encoding="utf-8")

    def awaiting_branch(text: str) -> str:
        return text.split('effect.State == "AWAITING_TOOLS"', 1)[1].split("return nil, errAgentDriverUnknown", 1)[0]

    node = driver.split("func (driver *agentRunDriver) node(", 1)[1].split("func resetSharingFence(", 1)[0]
    successor = driver.split("func (driver *agentRunDriver) successorExecute(", 1)[1].split(
        "func (driver *agentRunDriver) tasks(", 1
    )[0]
    for branch in (awaiting_branch(node), awaiting_branch(successor)):
        assert "retainedDriverToolResults(" in branch
        assert "decision.Returned" in branch
        assert "errAgentDriverTools" in branch
        assert "return effect, nil" in branch
        assert "NOT_APPLIED" not in branch
        assert "CONFIRMED" not in branch
        assert "ExecuteAgentTask(" not in branch
        assert "RecordOutcome(" not in branch
    hold = driver.split("func (driver *agentRunDriver) successorHold(", 1)[1].split(
        "func (driver *agentRunDriver) successorExecute(", 1
    )[0]
    assert "errAgentDriverTools" in hold
    assert "retainedDriverToolResults(" not in hold
    drive = driver.split("func (driver *agentRunDriver) drive(", 1)[1].split("func driverQuery(", 1)[0]
    assert drive.index('effect.State == "AWAITING_TOOLS"') < drive.index("ProviderPlan")
    synthesis = drive.split("AGENT_RUN_EXECUTION_PHASE_SYNTHESIS", 1)[1]
    assert synthesis.index('effect.State == "AWAITING_TOOLS"') < synthesis.index("ProjectAgentSynthesis(")
    tasks = driver.split("func (driver *agentRunDriver) tasks(", 1)[1].split(
        "func (driver *agentRunDriver) recoverCancellation(", 1
    )[0]
    assert tasks.index('effect.State == "AWAITING_TOOLS"') < tasks.index("effect.OutcomeReceipt")
    helper = driver.split("func (driver *agentRunDriver) retainedDriverToolResults(", 1)[1].split(
        "func (driver *agentRunDriver) projectJournalObservation(", 1
    )[0]
    assert "RetainedDriverToolResults(" in helper
    assert "AgentRunEventsAfter(" in helper
    assert "errAgentDriverUnknown" in helper
    assert "NOT_APPLIED" not in helper
    assert "CONFIRMED" not in helper
    assert "ExecuteAgentTask(" not in helper
    assert "RecordOutcome(" not in helper
    go_fn = go_admit.split("func RetainedDriverToolResults(", 1)[1].split("const journalStateDocument", 1)[0]
    assert "AGENT_DRIVER_TOOLS_PENDING" in go_fn
    assert "retainedToolResultScope" in go_fn
    assert "NOT_APPLIED" not in go_fn
    assert "CONFIRMED" not in go_fn
    assert 'case "AWAITING_TOOLS":\n\t\treturn "AGENT_DRIVER_TOOLS_PENDING", false' in go_admit
    body = oracle.split("def retained_driver_tool_results(", 1)[1].split("def _canonical_token(", 1)[0]
    assert "execute_tool_call(" not in body
    assert "NOT_APPLIED" not in body
    assert "CONFIRMED" not in body
    assert "AGENT_DRIVER_TOOLS_PENDING" in body
    rust_fn = rust.split("fn driver_pending()", 1)[1].split("pub fn next_model_turn(", 1)[0]
    assert "AGENT_DRIVER_TOOLS_PENDING" in rust_fn
    assert "execute_tool_call" not in rust_fn
    assert "tool_dispatch::dispatch" not in rust_fn
    assert "admit(" not in rust_fn
    assert "NOT_APPLIED" not in rust_fn
    assert "CONFIRMED" not in rust_fn
    block = publisher.split('metadata.state == "AWAITING_TOOLS"', 1)[1].split("return Ok(metadata)", 1)[0]
    assert "project_retained_driver_tools(" in block
    assert "CONFIRMED" not in block
    assert "NOT_APPLIED" not in block
    project = publisher.split("async fn project_retained_driver_tools(", 1)[1].split(
        "pub fn with_agent_plan_executor(", 1
    )[0]
    assert project.index("retained_driver_tool_results(") < project.index("project_driver_tool_results(")
    assert "ArtifactStore::open_readonly(" in project
    assert "ArtifactStore::open_native(" in project
    assert '"python_disabled"' in project
    assert "continue_retained_tools(" not in project
    assert "tool_dispatch::dispatch" not in project
    assert "CONFIRMED" not in project
    assert "NOT_APPLIED" not in project
    grant_fn = grant.split("async fn project_driver_tool_results(", 1)[1].split(
        "pub(crate) fn validate_node_dispatch(", 1
    )[0]
    assert "retain-driver-marker:" in grant_fn
    assert "retain-driver-result:" in grant_fn
    assert "agent_tool_request" in grant_fn
    assert "agent_tool_result" in grant_fn
    assert "RETAINED_TOOL_RESULT_SCOPE" in grant_fn
    assert "operator:agent-worker" in grant_fn
    assert "CONFIRMED" not in grant_fn
    assert "NOT_APPLIED" not in grant_fn


def _model_request(stored: dict[str, Any]) -> dict[str, Any]:
    request = _request_with_results(stored)
    request["toolContinuation"]["assistant"] = {"content": "checking", "reasoning_content": "think"}
    return request


def _model_body() -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "hi"}],
        "tool_choice": {"type": "function", "function": {"name": "list_reminders"}},
    }


def test_next_model_turn_reads_stored_tool_results_without_calling_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(call: object) -> dict[str, object]:
        raise AssertionError(call)

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.tools.execute_tool_call", forbidden)
    stored = _stored_result("call-stored", "stored-rem")
    request = _model_request(stored)
    body = _model_body()
    decision = next_model_turn(body, [request])
    assert decision["started"] is True
    assert decision["code"] == ""
    assert decision["events"] == []
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert isinstance(body["tool_choice"], dict)
    messages = decision["body"]["messages"]
    assert messages[0] == {"role": "user", "content": "hi"}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "checking"
    assert messages[1]["reasoning_content"] == "think"
    assert messages[1]["tool_calls"][0]["id"] == "call-stored"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call-stored"
    assert messages[2]["name"] == "list_reminders"
    assert "stored-rem" in messages[2]["content"]
    assert decision["body"]["tool_choice"] == "auto"
    assert "NOT_APPLIED" not in json.dumps(decision)
    assert "CONFIRMED" not in json.dumps(decision)
    plain = dict(body)
    plain["tool_choice"] = "auto"
    kept = next_model_turn(plain, [request])
    assert kept["started"] is True
    assert kept["body"]["tool_choice"] == "auto"


def test_missing_tool_result_does_not_start_the_model_turn() -> None:
    stored = _stored_result("call-stored", "stored-rem")
    request = _model_request(stored)
    body = _model_body()
    calls = request["toolContinuation"]["toolCalls"]
    absent = {
        "type": "agent_tool_request",
        "phase": "coder",
        "toolCount": 1,
        "toolContinuation": {"assistant": {"content": ""}, "toolCalls": calls},
    }
    empty = {**absent, "toolResults": []}
    wrong = {**absent, "toolResults": [{"type": "note", "text": "missing file"}]}
    mismatched = _model_request(stored)
    mismatched["toolResults"] = [_stored_result("other-call", "stored-rem")]
    no_output = _model_request(stored)
    no_output["toolResults"] = [{**stored, "output": None}]
    non_array = {**absent, "toolResults": "missing-file"}
    count_only = {"type": "agent_tool_request", "phase": "coder", "toolCount": 1}
    waiting = ([absent], [empty], [wrong], [mismatched], [no_output], [non_array], [count_only])
    for events in waiting:
        decision = next_model_turn(body, events)
        assert decision["started"] is False
        assert decision["code"] == "AGENT_DRIVER_TOOLS_PENDING"
        assert decision["body"] is None
        assert decision["events"] == []
        assert "NOT_APPLIED" not in json.dumps(decision)
        assert "CONFIRMED" not in json.dumps(decision)
        assert "stored-rem" not in json.dumps(decision)
    idle_events = next_model_turn(body, [])
    assert idle_events == {"started": False, "code": "", "body": None, "events": []}
    assert "NOT_APPLIED" not in json.dumps(idle_events)
    assert "stored-rem" not in json.dumps(idle_events)
    unknown = {"type": "node_effect", "phase": "coder", "observation": "missing"}
    blocked = next_model_turn(body, [request, unknown])
    assert blocked["started"] is False
    assert blocked["code"] == "EFFECT_UNKNOWN"
    assert blocked["body"] is None
    assert blocked["events"] == []
    assert "NOT_APPLIED" not in json.dumps(blocked)
    assert "stored-rem" not in json.dumps(blocked)
    settled = [request, {"type": "agent_output", "phase": "coder", "output": {"id": "coder", "content": "done"}}]
    idle = next_model_turn(body, settled)
    assert idle == {"started": False, "code": "", "body": None, "events": []}
    assert "stored-rem" not in json.dumps(idle)


def test_worker_source_starts_the_model_turn_only_after_saved_results() -> None:
    oracle = (ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py").read_text(encoding="utf-8")
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    worker = (ROOT / "rust/crates/deepseek-worker/src/agent_task_executor.rs").read_text(encoding="utf-8")
    publisher = (ROOT / "rust/crates/deepseek-worker/src/agent_plan_executor.rs").read_text(encoding="utf-8")
    turn = oracle.split("def next_model_turn(", 1)[1]
    assert "tool_result_message(" in turn
    assert "execute_tool_call(" not in turn
    assert "NOT_APPLIED" not in turn
    assert "CONFIRMED" not in turn
    assert "AGENT_DRIVER_TOOLS_PENDING" in turn
    rust_turn = rust.split("pub fn next_model_turn(", 1)[1]
    assert "tool_result_message(" in rust_turn
    assert "execute_tool_call" not in rust_turn
    assert "tool_dispatch::dispatch" not in rust_turn
    assert "NOT_APPLIED" not in rust_turn
    assert "CONFIRMED" not in rust_turn
    assert "AGENT_DRIVER_TOOLS_PENDING" in rust_turn
    saved = worker.split("fn saved_model_turn(", 1)[1].split("fn task_envelope(", 1)[0]
    assert saved.index("read_retained_tool_results(") < saved.index("next_model_turn(")
    assert "exchange_fenced_turn(" not in saved
    assert "continue_retained_tools(" not in saved
    assert "execute_tool_call" not in saved
    assert "NOT_APPLIED" not in saved
    assert "CONFIRMED" not in saved
    sent = worker.split("let sent = async {", 1)[1].split("match sent {", 1)[0]
    assert sent.index("confirm_agent_node_effect") < sent.index("retain_workspace_tool_results(")
    assert sent.index("retain_workspace_tool_results(") < sent.index("saved_model_turn(")
    tail = sent.split("saved_model_turn(", 1)[1]
    assert tail.index("deadline") < tail.index("exchange_fenced_turn(")
    gate = tail.split("exchange_fenced_turn(", 1)[0]
    assert gate.index("execution_send_lease(") < gate.index("admitted_provider_send(")
    assert "if follow_admit.allowed" in gate
    follow = tail.split("if let Some(next_body) = follow {", 1)[1]
    assert "exchange_fenced_turn(" in follow
    assert "store_model_turn(" in follow
    assert follow.index("follow_up_exchange(") < follow.index("store_model_turn(")
    lost = follow.split("Err(_) => {", 1)[1].split("}", 1)[0]
    assert "follow_up_exchange(" in lost
    assert "false" in lost
    assert "store_model_turn(" not in lost
    assert "mark_agent_node_effect_unknown(" not in lost
    assert "confirm_agent_node_effect(" not in follow
    assert "continue_retained_tools(" not in follow
    assert "NOT_APPLIED" not in follow
    assert "CONFIRMED" not in follow
    replay = worker.split("AgentNodeEffectStart::Confirmed(outcome)", 1)[1].split(
        "AgentNodeEffectStart::EffectUnknown", 1
    )[0]
    assert "saved_model_turn(" not in replay
    assert "exchange_fenced_turn(" not in replay
    model_store = publisher.split("fn store_model_turn(", 1)[1].split("async fn prepare_preset(", 1)[0]
    assert "create_new(true)" in model_store
    assert "CONFIRMED" not in model_store
    assert "NOT_APPLIED" not in model_store
    assert "continue_retained_tools(" not in model_store


def _finished_answer(content: str, phase: str = "coder") -> dict[str, Any]:
    return {
        "runId": "run-1",
        "publicEvent": {
            "type": "agent_output",
            "phase": phase,
            "output": {"id": phase, "content": content, "task": "work"},
        },
        "providerResponseSha256": "ab",
    }


def test_published_answer_leaves_resume_without_calling_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(call: object) -> dict[str, object]:
        raise AssertionError(call)

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.tools.execute_tool_call", forbidden)
    stored = _stored_result("call-stored", "stored-rem")
    request = _model_request(stored)
    saved = _finished_answer("done-answer")
    decision = published_model_answer([request], saved)
    assert decision["published"] is True
    assert decision["code"] == ""
    assert decision["event"] == saved["publicEvent"]
    encoded = json.dumps(decision)
    assert "NOT_APPLIED" not in encoded
    assert "CONFIRMED" not in encoded
    bare = _tool_request([{"id": "call-1", "function": {"name": "list_reminders", "arguments": {}}}])
    refused = _admit_retained([bare], "resume")
    assert refused["admitted"] is False
    assert refused["code"] == "AGENT_TOOL_CONTINUATION_REQUIRED"
    assert refused["events"] == []
    held = _admit_retained([request], "resume")
    assert held["admitted"] is True
    assert held["events"][0]["type"] == "agent_tool_result"
    assert held["events"] != [
        {"type": "run_status", "status": "running", "reason": "resume"},
        {"type": "run_status", "status": "done", "reason": "resume"},
    ]
    published = dict(request)
    published["publishedAnswer"] = decision["event"]
    assert retained_effect([published]) == {"effect": "none", "sameEffect": False}
    done = _admit_retained([published], "resume")
    assert done["admitted"] is True
    assert done["dispatch"] == "none"
    assert done["started"] is False
    assert done["events"] == [
        {"type": "run_status", "status": "running", "reason": "resume"},
        {"type": "run_status", "status": "done", "reason": "resume"},
    ]
    copied = retained_driver_tool_results("AWAITING_TOOLS", [published])
    assert copied["returned"] is True
    assert copied["events"][0]["toolCallId"] == "call-stored"


def test_missing_answer_file_does_not_publish_or_confirm() -> None:
    stored = _stored_result("call-stored", "stored-rem")
    request = _model_request(stored)
    secret = "secret-answer"
    saved = _finished_answer(secret)
    running = [
        {"type": "run_status", "status": "running", "reason": "resume"},
        {"type": "run_status", "status": "done", "reason": "resume"},
    ]
    calls = {"toolContinuation": {"toolCalls": [{"id": "again", "function": {"name": "list_reminders"}}]}}
    mismatched = _finished_answer(secret, "researcher")
    empty = _finished_answer("   ")
    unfinished = {
        "type": "agent_output",
        "phase": "coder",
        "output": {"id": "coder", "content": secret},
        "toolCalls": [{"id": "again"}],
    }
    refused_cases: tuple[Any, ...] = (
        None,
        "missing-file",
        [],
        {},
        {**saved, **calls},
        mismatched,
        empty,
        unfinished,
        {"publicEvent": {"type": "note", "text": secret}},
    )
    for saved_value in refused_cases:
        decision = published_model_answer([request], saved_value)
        assert decision == {"published": False, "code": "", "event": None}
        assert secret not in json.dumps(decision)
        assert "NOT_APPLIED" not in json.dumps(decision)
        assert "CONFIRMED" not in json.dumps(decision)
    idle_events = published_model_answer([], saved)
    assert idle_events == {"published": False, "code": "", "event": None}
    assert secret not in json.dumps(idle_events)
    settled = [request, {"type": "agent_output", "phase": "coder", "output": {"id": "coder", "content": "done"}}]
    idle = published_model_answer(settled, saved)
    assert idle == {"published": False, "code": "", "event": None}
    assert secret not in json.dumps(idle)
    assert _admit_retained(settled, "resume")["events"] == running
    unknown = {"type": "node_effect", "phase": "coder", "observation": "missing"}
    blocked = published_model_answer([request, unknown], saved)
    assert blocked == {"published": False, "code": "EFFECT_UNKNOWN", "event": None}
    assert secret not in json.dumps(blocked)
    assert "stored-rem" not in json.dumps(blocked)
    assert "NOT_APPLIED" not in json.dumps(blocked)


def test_worker_source_publishes_the_saved_answer_without_a_second_confirm() -> None:
    publisher = (ROOT / "rust/crates/deepseek-worker/src/agent_plan_executor.rs").read_text(encoding="utf-8")
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    driver = GO_DRIVER.read_text(encoding="utf-8")
    oracle = (ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py").read_text(encoding="utf-8")
    publish = publisher.split("async fn publish_known(", 1)[1].split("#[tonic::async_trait]", 1)[0]
    assert publish.index("read_model_turn(") < publish.index("published_model_answer(")
    assert publish.index("published_model_answer(") < publish.index("publishedAnswer")
    assert publish.index("publishedAnswer") < publish.index('"ANSWER_PUBLISHED"')
    assert publish.index("toolResults") < publish.index('"AWAITING_TOOLS"')
    pending = publish.split("if pending_tools {", 1)[1].split("let root", 1)[0]
    assert "read_model_turn(" in pending
    assert "toolContinuation" not in pending
    assert "exchange_fenced_turn(" not in publish
    assert "confirm_agent_node_effect(" not in publish
    block = publisher.split('metadata.state == "AWAITING_TOOLS"', 1)[1].split("return Ok(metadata)", 1)[0]
    assert "ANSWER_PUBLISHED" in block
    assert "project_retained_driver_tools(" in block
    assert "CONFIRMED" not in block
    assert "NOT_APPLIED" not in block
    arm = rust.split('"agent_tool_request" =>', 1)[1].split('"agent_output" =>', 1)[0]
    assert "request_published_answer(" in arm
    assert "tool_waiting(" in arm
    answer = oracle.split("def published_model_answer(", 1)[1]
    assert "execute_tool_call(" not in answer
    assert "NOT_APPLIED" not in answer
    assert "CONFIRMED" not in answer
    rust_answer = rust.split("pub fn published_model_answer(", 1)[1]
    assert "execute_tool_call" not in rust_answer
    assert "tool_dispatch::dispatch" not in rust_answer
    assert "NOT_APPLIED" not in rust_answer
    assert "CONFIRMED" not in rust_answer

    def awaiting_branch(text: str) -> str:
        return text.split('effect.State == "AWAITING_TOOLS"', 1)[1].split("return nil, errAgentDriverUnknown", 1)[0]

    node = driver.split("func (driver *agentRunDriver) node(", 1)[1].split("func resetSharingFence(", 1)[0]
    successor = driver.split("func (driver *agentRunDriver) successorExecute(", 1)[1].split(
        "func (driver *agentRunDriver) tasks(", 1
    )[0]
    for branch in (awaiting_branch(node), awaiting_branch(successor)):
        assert 'effect.State == "ANSWER_PUBLISHED"' in branch
        assert "return effect, nil" in branch
        assert "CONFIRMED" not in branch
        assert "NOT_APPLIED" not in branch
        assert "ExecuteAgentTask(" not in branch
        assert "RecordOutcome(" not in branch
    tasks = driver.split("func (driver *agentRunDriver) tasks(", 1)[1].split(
        "func (driver *agentRunDriver) recoverCancellation(", 1
    )[0]
    assert 'effect.State == "ANSWER_PUBLISHED"' in tasks
    assert tasks.index('effect.State == "AWAITING_TOOLS"') < tasks.index("effect.OutcomeReceipt")
    drive = driver.split("func (driver *agentRunDriver) drive(", 1)[1].split("func driverQuery(", 1)[0]
    assert drive.index('effect.State == "AWAITING_TOOLS"') < drive.index("ProviderPlan")
    assert 'effect.State == "ANSWER_PUBLISHED"' in drive.split("ProviderPlan", 1)[0]
    synthesis = drive.split("AGENT_RUN_EXECUTION_PHASE_SYNTHESIS", 1)[1]
    assert 'effect.State == "ANSWER_PUBLISHED"' in synthesis.split("ProjectAgentSynthesis(", 1)[0]
    assert synthesis.index('effect.State == "AWAITING_TOOLS"') < synthesis.index("ProjectAgentSynthesis(")
    path = publisher.split("fn model_turn_path(", 1)[1].split("pub fn load_agent_plan_executor_from_env(", 1)[0]
    assert "retained-model-turns" in path


def _settled_answer(content: str, phase: str = "coder", task: str = "work") -> dict[str, Any]:
    saved = _finished_answer(content, phase)
    output = saved["publicEvent"]["output"]
    output["task"] = task
    output["latencyMs"] = 12
    output["usage"] = {"prompt_tokens": 3, "completion_tokens": 4}
    return saved


def test_published_answer_settles_on_the_installed_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(call: object) -> dict[str, object]:
        raise AssertionError(call)

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.tools.execute_tool_call", forbidden)
    stored = _stored_result("call-stored", "stored-rem")
    saved = _settled_answer("done-answer")
    request = _model_request(stored)
    request["publishedAnswer"] = saved["publicEvent"]
    decision = settle_published_answer([request], saved, 4, 4, "work")
    assert decision["settled"] is True
    assert decision["code"] == ""
    assert decision["event"] == saved["publicEvent"]
    assert decision["output"] == {
        "promptTokens": 3,
        "completionTokens": 4,
        "latencyMs": 12,
        "failed": False,
        "task": "work",
    }
    assert set(decision) == {"settled", "code", "event", "output"}
    encoded = json.dumps(decision)
    assert "NOT_APPLIED" not in encoded
    assert "CONFIRMED" not in encoded
    assert "signature" not in decision
    assert "receipt" not in decision
    secret = "secret-answer"
    hidden = _settled_answer(secret)
    hidden_request = _model_request(stored)
    hidden_request["publishedAnswer"] = hidden["publicEvent"]
    idle = {"settled": False, "code": "", "event": None, "output": None}
    assert settle_published_answer([hidden_request], None, 4, 4, "work") == idle
    assert settle_published_answer([hidden_request], "missing-file", 4, 4, "work") == idle
    bare = _model_request(stored)
    assert settle_published_answer([bare], hidden, 4, 4, "work") == idle
    wrong = _model_request(stored)
    wrong["publishedAnswer"] = _settled_answer("other")["publicEvent"]
    assert settle_published_answer([wrong], hidden, 4, 4, "work") == idle
    plain = _finished_answer(secret)
    plain_request = _model_request(stored)
    plain_request["publishedAnswer"] = plain["publicEvent"]
    assert settle_published_answer([plain_request], plain, 4, 4, "work") == idle
    assert settle_published_answer([hidden_request], hidden, 4, 4, "other-task") == idle
    assert settle_published_answer([hidden_request], hidden, 4, 4, "") == idle
    promoted = settle_published_answer([hidden_request], hidden, 5, 4, "work")
    assert promoted == {"settled": False, "code": "FENCE_MISMATCH", "event": None, "output": None}
    assert secret not in json.dumps(promoted)
    assert "epoch" not in promoted
    assert settle_published_answer([hidden_request], hidden, True, 4, "work")["code"] == "FENCE_MISMATCH"
    assert settle_published_answer([hidden_request], hidden, 0, 4, "work")["code"] == "FENCE_MISMATCH"
    stale = settle_published_answer([hidden_request], hidden, 3, 4, "work")
    assert stale == {"settled": False, "code": "STALE_EXECUTION_EPOCH", "event": None, "output": None}
    assert secret not in json.dumps(stale)
    unknown = {"type": "node_effect", "phase": "coder", "observation": "missing"}
    blocked = settle_published_answer([hidden_request, unknown], hidden, 4, 4, "work")
    assert blocked["settled"] is False
    assert blocked["code"] == "EFFECT_UNKNOWN"
    assert blocked["event"] is None
    assert secret not in json.dumps(blocked)
    assert "NOT_APPLIED" not in json.dumps(blocked)
    assert "stored-rem" not in json.dumps(blocked)


def test_worker_source_settles_the_published_receipt_without_promoting_epoch() -> None:
    publisher = (ROOT / "rust/crates/deepseek-worker/src/agent_plan_executor.rs").read_text(encoding="utf-8")
    nodes = (ROOT / "rust/crates/deepseek-worker/src/authority_store/agent_nodes.rs").read_text(encoding="utf-8")
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    oracle = (ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py").read_text(encoding="utf-8")
    driver = GO_DRIVER.read_text(encoding="utf-8")
    client = (ROOT / "go/internal/worker/agent_nodes.go").read_text(encoding="utf-8")
    block = publisher.split('metadata.state == "AWAITING_TOOLS"', 1)[1].split("return Ok(metadata)", 1)[0]
    assert "attach_published_answer_receipt(" in block
    assert "project_retained_driver_tools(" in block
    assert "CONFIRMED" not in block
    assert "NOT_APPLIED" not in block
    attach = publisher.split("async fn attach_published_answer_receipt(", 1)[1].split(
        "pub fn with_agent_plan_executor(", 1
    )[0]
    assert attach.index("published_answer_outcome_evidence(") < attach.index("sign_node_outcome(")
    assert "confirm_agent_node_effect(" not in attach
    assert "install_authoritative_epoch(" not in attach
    assert "install_signed_epoch(" not in attach
    assert "exchange_fenced_turn(" not in attach
    assert "NOT_APPLIED" not in attach
    assert "UPDATE " not in attach
    proof = nodes.split("fn published_answer_outcome_evidence(", 1)[1].split("pub(crate) fn agent_node_outcome(", 1)[0]
    assert 'row.state != "CONFIRMED"' in proof
    assert "agent_tool_request" in proof
    assert "settle_published_answer(" in proof
    assert "execution_epoch" in proof
    assert "install_authoritative_epoch(" not in proof
    assert "install_signed_epoch(" not in proof
    assert "confirm_agent_node_effect(" not in proof
    assert "UPDATE " not in proof
    old = nodes.split("fn agent_node_outcome_evidence(", 1)[1].split("fn published_answer_outcome_evidence(", 1)[0]
    assert '["type"] == "agent_output"' in old
    settlement = rust.split("pub fn settle_published_answer(", 1)[1]
    assert "published_model_answer(" in settlement
    assert "FENCE_MISMATCH" in settlement
    assert "STALE_EXECUTION_EPOCH" in settlement
    assert "execute_tool_call" not in settlement
    assert "tool_dispatch::dispatch" not in settlement
    assert "NOT_APPLIED" not in settlement
    assert "CONFIRMED" not in settlement
    py_settlement = oracle.split("def settle_published_answer(", 1)[1]
    assert "published_model_answer(" in py_settlement
    assert "FENCE_MISMATCH" in py_settlement
    assert "STALE_EXECUTION_EPOCH" in py_settlement
    assert "execute_tool_call(" not in py_settlement
    assert "NOT_APPLIED" not in py_settlement
    assert "CONFIRMED" not in py_settlement
    tasks = driver.split("func (driver *agentRunDriver) tasks(", 1)[1].split(
        "func (driver *agentRunDriver) recoverCancellation(", 1
    )[0]
    assert tasks.index('effect.State == "AWAITING_TOOLS"') < tasks.index("effect.OutcomeReceipt")
    published = tasks.split('effect.State == "ANSWER_PUBLISHED"', 1)[1]
    assert published.index("return nil") < published.index("RecordOutcome(")
    assert published.index("RecordOutcome(") < published.index("SettleTask(")
    gate = published.split("SettleTask(", 1)[0]
    assert "ExecutionEpoch +" not in gate
    assert "NOT_APPLIED" not in gate
    accepted = client.split('case "ANSWER_PUBLISHED":', 1)[1].split("default:", 1)[0]
    assert "OutcomeReceipt != nil" in accepted
    assert "TaskOutput != nil" in accepted
    execute = client.split("func (client *Client) ExecuteAgentTask(", 1)[1].split("func agentTaskRole(", 1)[0]
    assert 'result.State == "ANSWER_PUBLISHED"' in execute


def _direct_dependency(content: str = "public summary") -> dict[str, Any]:
    return {
        "runId": "run-1",
        "publicEvent": {
            "type": "agent_output",
            "phase": "coder",
            "output": {"id": "coder", "task": "work", "content": content},
        },
    }


def _request_dependency() -> dict[str, Any]:
    return {
        "runId": "run-1",
        "publicEvent": {"type": "agent_tool_request", "phase": "coder", "toolCount": 1},
    }


def test_published_answer_feeds_a_dependency_without_calling_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(call: object) -> dict[str, object]:
        raise AssertionError(call)

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.tools.execute_tool_call", forbidden)
    secret = "secret-prior"
    direct = published_dependency_output(_direct_dependency(), {"hidden": secret}, "coder", "work", "run-1")
    assert direct["ready"] is True
    assert direct["code"] == ""
    assert direct["output"] == _direct_dependency()["publicEvent"]["output"]
    assert set(direct) == {"ready", "code", "output"}
    assert secret not in json.dumps(direct)
    saved = _finished_answer("done-answer")
    request = _request_dependency()
    ready = published_dependency_output(request, saved, "coder", "work", "run-1")
    assert ready["ready"] is True
    assert ready["code"] == ""
    assert ready["output"] == saved["publicEvent"]["output"]
    assert "done-answer" in ready["output"]["content"]
    idle = {"ready": False, "code": "", "output": None}
    assert published_dependency_output(request, None, "coder", "work", "run-1") == idle
    assert published_dependency_output(request, "missing-file", "coder", "work", "run-1") == idle
    hidden = _finished_answer(secret)
    hidden["toolContinuation"] = {"toolCalls": [{"id": "again"}]}
    refused = published_dependency_output(request, hidden, "coder", "work", "run-1")
    assert refused == idle
    assert secret not in json.dumps(refused)
    assert published_dependency_output(request, saved, "critic", "work", "run-1") == idle
    assert published_dependency_output(request, saved, "coder", "other-task", "run-1") == idle
    assert published_dependency_output(request, saved, "coder", "work", "other-run") == idle
    sticky = {"runId": "run-1", "publicEvent": {"type": "agent_tool_request", "phase": "coder"}}
    unknown = published_dependency_output(sticky, hidden, "coder", "work", "run-1")
    assert unknown["ready"] is False
    assert unknown["code"] == "EFFECT_UNKNOWN"
    assert unknown["output"] is None
    assert secret not in json.dumps(unknown)
    assert "NOT_APPLIED" not in json.dumps(unknown)
    assert "NOT_APPLIED" not in json.dumps(ready)
    assert "CONFIRMED" not in json.dumps(ready)


def test_worker_source_reads_the_saved_answer_before_the_dependent_provider() -> None:
    nodes = (ROOT / "rust/crates/deepseek-worker/src/authority_store/agent_nodes.rs").read_text(encoding="utf-8")
    task = (ROOT / "rust/crates/deepseek-worker/src/agent_task_executor.rs").read_text(encoding="utf-8")
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    oracle = (ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py").read_text(encoding="utf-8")
    legacy = (
        ROOT / "rust/crates/deepseek-worker/src/authority_store/agent_nodes/tests/task_executor.rs"
    ).read_text(encoding="utf-8")
    wrapper = nodes.split("fn agent_task_prior_outputs(", 1)[1].split("fn agent_task_prior_outputs_for(", 1)[0]
    assert "agent_task_prior_outputs_for(" in wrapper
    assert "|_| None" in wrapper
    body = nodes.split("fn agent_task_prior_outputs_for(", 1)[1].split("pub(crate) fn agent_node_snapshot(", 1)[0]
    assert "published_dependency_output(" in body
    assert "saved_for(" in body
    assert "UPDATE " not in body
    assert "install_authoritative_epoch(" not in body
    assert "install_signed_epoch(" not in body
    window = task.split("agent_task_prior_outputs_for(", 1)[1].split("exchange_fenced_turn(", 1)[0]
    assert "read_model_turn(" in window
    assert "AGENT_DEPENDENCY_UNPUBLISHED" in window
    assert "synthesis_payload(" in window
    assert "worker_payload(" in window
    assert "provider_body_from_outputs(" in window
    assert "&outputs" in window
    assert window.index("AGENT_DEPENDENCY_UNPUBLISHED") < window.index("prepare_payload(")
    assert window.index("prepare_payload(") < window.index("provider_body_from_outputs(")
    assert window.index("provider_body_from_outputs(") < window.index("worker_payload(")
    assert window.index("worker_payload(") < window.index("execution_send_lease(")
    assert window.index("execution_send_lease(") < window.index("admitted_provider_send(")
    assert "if !admit.allowed" in window
    send = window.split("admitted_provider_send(", 1)[1]
    assert "confirm_agent_node_effect(" not in send
    assert "store_model_turn(" not in send
    assert "NOT_APPLIED" not in send
    first_exchange = task.split("exchange_fenced_turn(", 1)[1].split(".map_err", 1)[0]
    assert "&body," in first_exchange
    assert "&body" not in window.split("admitted_provider_send(", 1)[1]
    assert task.index("execution_send_lease(") < task.index("admitted_provider_send(")
    assert task.index("admitted_provider_send(") < task.index("exchange_fenced_turn(")
    assert task.index("exchange_fenced_turn(") < task.index("provider_exchange_unknown(")
    assert task.index("provider_exchange_unknown(") < task.index("confirm_agent_node_effect(")
    mapped = task.split("exchange_fenced_turn(", 1)[1].split(".map_err", 1)[1].split("?;", 1)[0]
    assert "provider_exchange_unknown(" in mapped
    assert "false" in mapped
    assert "NOT_APPLIED" not in mapped
    lease_fn = rust.split("pub fn execution_send_lease(", 1)[1]
    admit_fn = lease_fn.split("pub fn admitted_provider_send(", 1)[1].split("pub struct ", 1)[0]
    assert "lease.mark_unknown" in admit_fn
    assert "lease.provider_effect" in admit_fn
    assert 'code: "AGENT_EXECUTION_LEASE_DENIED"' in admit_fn
    unknown_fn = lease_fn.split("pub fn provider_exchange_unknown(", 1)[1]
    assert 'code: "AGENT_PROVIDER_RESULT_UNKNOWN"' in unknown_fn
    assert "mark_unknown: true" in unknown_fn
    assert "provider_effect: false" in unknown_fn
    assert "receipt_present" in unknown_fn
    assert 'code: "AGENT_EXECUTION_LEASE_DENIED"' in lease_fn
    assert "mark_unknown: true" in lease_fn
    assert "provider_effect: false" in lease_fn
    assert "execute_tool_call" not in lease_fn
    assert "NOT_APPLIED" not in lease_fn
    assert "CONFIRMED" not in lease_fn
    planner = (ROOT / "rust/crates/deepseek-worker/src/agent_plan_executor.rs").read_text(encoding="utf-8")
    plan_send = planner.split("execution_send_lease(", 1)[1].split("exchange_fenced_turn(", 1)[0]
    assert "admitted_provider_send(" in plan_send
    assert "confirm_agent_node_effect(" not in plan_send
    plan_failure = planner.split("exchange_fenced_turn(", 1)[1].split("confirm_agent_node_effect(", 1)[0]
    assert "provider_exchange_unknown(" in plan_failure
    assert "false" in plan_failure
    assert "NOT_APPLIED" not in plan_failure
    assert task.index("agent_task_prior_outputs_for(") < task.index("exchange_fenced_turn(")
    helper = rust.split("pub fn provider_body_from_outputs(", 1)[1].split("pub fn dependency_provider_body(", 1)[0]
    assert "worker_payload(" in helper
    assert "synthesis_payload(" in helper
    assert helper.index("outputs.is_empty()") < helper.index("worker_payload(")
    assert helper.index("outputs.is_empty()") < helper.index("synthesis_payload(")
    shaped = rust.split("pub fn dependency_provider_body(", 1)[1]
    assert shaped.index("published_dependency_output(") < shaped.index("provider_body_from_outputs(")
    assert shaped.index("AGENT_DEPENDENCY_UNPUBLISHED") < shaped.index("provider_body_from_outputs(")
    assert "install_authoritative_epoch(" not in shaped
    assert "UPDATE " not in shaped
    py_body = oracle.split("def dependency_provider_body(", 1)[1]
    assert py_body.index("published_dependency_output(") < py_body.index("build_prior_context(")
    assert py_body.index("AGENT_DEPENDENCY_UNPUBLISHED") < py_body.index("build_prior_context(")
    assert py_body.index("build_prior_context(") < py_body.index("agent_messages(")
    assert py_body.index("agent_messages(") < py_body.index("synthesis_messages(")
    arm = task.split("Err(error) => {", 1)[1].split("Err(error)", 1)[0]
    assert arm.index("AGENT_DEPENDENCY_UNPUBLISHED") < arm.index("mark_agent_node_effect_unknown(")
    assert "NOT_APPLIED" not in arm
    decision = rust.split("pub fn published_dependency_output(", 1)[1]
    assert "published_model_answer(" in decision
    assert "execute_tool_call" not in decision
    assert "tool_dispatch::dispatch" not in decision
    assert "NOT_APPLIED" not in decision
    assert "CONFIRMED" not in decision
    py_decision = oracle.split("def published_dependency_output(", 1)[1]
    assert "published_model_answer(" in py_decision
    assert "execute_tool_call(" not in py_decision
    assert "NOT_APPLIED" not in py_decision
    assert "CONFIRMED" not in py_decision
    assert "fn retained_tool_request_cannot_attest_completion_or_satisfy_dependencies(" in legacy
    assert "agent_task_prior_outputs(&critic).is_err()" in legacy


def test_dependency_provider_body_contains_the_saved_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("tool")

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.tools.execute_tool_call", forbidden)
    content = "saved-answer-body"
    payload = {"messages": [{"role": "user", "content": "original question"}]}

    def call(envelope: Any, saved: Any) -> dict[str, Any]:
        return dependency_provider_body(
            envelope,
            saved,
            "coder",
            "work",
            "run-1",
            payload,
            "critic",
            "review the saved answer",
            "deepseek-v4-pro",
            "original question",
        )

    built = call(_request_dependency(), _finished_answer(content))
    assert built["built"] is True
    assert built["code"] == ""
    assert set(built) == {"built", "code", "worker", "synthesis"}
    worker_text = built["worker"][-1]["content"]
    synthesis_text = built["synthesis"][-1]["content"]
    assert content in worker_text
    assert content in synthesis_text
    assert "original question" in built["worker"][0]["content"]
    assert "original question" in built["synthesis"][0]["content"]
    assert "### 输出\n" + content in synthesis_text
    direct = call(_direct_dependency(content), {"hidden": "secret-prior"})
    assert direct["built"] is True
    assert content in direct["worker"][-1]["content"]
    assert content in direct["synthesis"][-1]["content"]
    assert "secret-prior" not in json.dumps(direct, ensure_ascii=False)

    def builders_must_not_run(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("builder")

    monkeypatch.setattr(
        "deepseek_infra.infra.agent_runtime.multi_agent.build_prior_context",
        builders_must_not_run,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.agent_runtime.multi_agent.agent_messages",
        builders_must_not_run,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.agent_runtime.multi_agent.synthesis_messages",
        builders_must_not_run,
    )
    idle = {"built": False, "code": "AGENT_DEPENDENCY_UNPUBLISHED", "worker": None, "synthesis": None}
    assert call(_request_dependency(), None) == idle
    assert call(_request_dependency(), "missing-file") == idle
    hidden = _finished_answer("secret-prior")
    hidden["toolContinuation"] = {"toolCalls": [{"id": "again"}]}
    refused = call(_request_dependency(), hidden)
    assert refused == idle
    assert "secret-prior" not in json.dumps(refused)
    sticky = {"runId": "run-1", "publicEvent": {"type": "agent_tool_request", "phase": "coder"}}
    unknown = call(sticky, hidden)
    assert unknown["built"] is False
    assert unknown["code"] == "EFFECT_UNKNOWN"
    assert unknown["worker"] is None
    assert unknown["synthesis"] is None
    assert "secret-prior" not in json.dumps(unknown)
    assert "NOT_APPLIED" not in json.dumps(unknown)
    assert "NOT_APPLIED" not in json.dumps(built)


def test_dead_execution_lease_denies_the_provider_send() -> None:
    denied = {
        "allowed": False,
        "code": "AGENT_EXECUTION_LEASE_DENIED",
        "markUnknown": True,
        "providerEffect": False,
    }
    assert execution_send_lease(10, 10) == denied
    assert execution_send_lease(9, 10) == denied
    assert execution_send_lease(0, 10) == denied
    assert execution_send_lease(True, 10) == denied
    assert execution_send_lease(10, False) == denied
    assert execution_send_lease(2**63, 10) == denied
    live = execution_send_lease(11, 10)
    assert live == {"allowed": True, "code": "", "markUnknown": False, "providerEffect": False}
    assert set(live) == {"allowed", "code", "markUnknown", "providerEffect"}
    assert "NOT_APPLIED" not in json.dumps(denied)
    assert "epoch" not in denied
    assert denied["providerEffect"] is False


def test_live_lease_is_the_only_send_and_a_lost_receipt_stays_unknown() -> None:
    live = execution_send_lease(11, 10)
    admitted = admitted_provider_send(live)
    assert admitted == {
        "allowed": True,
        "code": "",
        "markUnknown": False,
        "providerEffect": False,
    }
    assert admitted["providerEffect"] is False
    denied = {
        "allowed": False,
        "code": "AGENT_EXECUTION_LEASE_DENIED",
        "markUnknown": True,
        "providerEffect": False,
    }
    assert admitted_provider_send(execution_send_lease(10, 10)) == denied
    assert admitted_provider_send(execution_send_lease(9, 10)) == denied
    dirty = {
        "allowed": True,
        "code": "",
        "markUnknown": False,
        "providerEffect": False,
        "epoch": 2,
    }
    assert admitted_provider_send(dirty) == denied
    marked = dict(live)
    marked["markUnknown"] = True
    assert admitted_provider_send(marked) == denied
    effected = dict(live)
    effected["providerEffect"] = True
    assert admitted_provider_send(effected) == denied
    assert admitted_provider_send(True) == denied
    assert "NOT_APPLIED" not in json.dumps(denied)
    assert "epoch" not in denied
    unknown = {
        "code": "AGENT_PROVIDER_RESULT_UNKNOWN",
        "markUnknown": True,
        "providerEffect": False,
    }
    for observation in (False, None, "", "timeout", "missing-object", "absent-log", 0):
        decision = provider_exchange_unknown(observation)
        assert decision == unknown
        assert "NOT_APPLIED" not in json.dumps(decision)
        assert decision["providerEffect"] is False
    held = provider_exchange_unknown(True)
    assert held == {"code": "", "markUnknown": False, "providerEffect": False}
    assert set(held) == {"code", "markUnknown", "providerEffect"}
    assert "NOT_APPLIED" not in json.dumps(held)
    assert held["providerEffect"] is False


def test_follow_up_send_keeps_a_dead_lease_and_a_lost_receipt_unstored() -> None:
    dead = follow_up_exchange(admitted_provider_send(execution_send_lease(10, 10))["allowed"], True)
    assert dead == {
        "store": False,
        "code": "AGENT_EXECUTION_LEASE_DENIED",
        "markUnknown": False,
        "providerEffect": False,
    }
    negative = follow_up_exchange(
        admitted_provider_send(execution_send_lease(11, -1))["allowed"],
        True,
    )
    assert negative == dead
    lost = {
        "store": False,
        "code": "AGENT_PROVIDER_RESULT_UNKNOWN",
        "markUnknown": False,
        "providerEffect": False,
    }
    for observation in (False, None, "", "timeout", "missing-object", "absent-log", 0):
        decision = follow_up_exchange(True, observation)
        assert decision == lost
        assert decision["markUnknown"] is False
        assert decision["providerEffect"] is False
        assert "NOT_APPLIED" not in json.dumps(decision)
    kept = follow_up_exchange(True, True)
    assert kept == {"store": True, "code": "", "markUnknown": False, "providerEffect": False}
    assert kept["markUnknown"] is False
    assert "epoch" not in kept
    assert "NOT_APPLIED" not in json.dumps(dead)


def test_workspace_tool_lease_passes_only_a_live_execution_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    live = workspace_tool_lease(11, 10)
    assert live == {"leaseLive": True, "lease": "live", "code": "", "providerEffect": False}
    assert set(live) == {"leaseLive", "lease", "code", "providerEffect"}
    chart = _chart_call("call-lease")
    request = _tool_request([chart])
    decision = continue_retained_tools(_continuation_fields(lease=live["lease"]), [request])
    assert decision["admitted"] is True
    assert decision["dispatch"] == "same-effect"
    assert decision["events"][0]["output"] == execute_tool_call(chart)
    assert "NOT_APPLIED" not in json.dumps(decision["events"])
    denied = {
        "leaseLive": False,
        "lease": "expired",
        "code": "AGENT_EXECUTION_LEASE_DENIED",
        "providerEffect": False,
    }
    clocks = (
        (10, 10),
        (9, 10),
        (0, 10),
        (11, 0),
        (11, -1),
        (-1, 10),
        (True, 10),
        (11, False),
        (2**63, 10),
    )
    for deadline, now in clocks:
        assert workspace_tool_lease(deadline, now) == denied
    assert "NOT_APPLIED" not in json.dumps(denied)
    assert "epoch" not in denied
    assert denied["providerEffect"] is False

    def forbidden(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("tool")

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.tools.execute_tool_call", forbidden)
    refused = continue_retained_tools(_continuation_fields(lease=denied["lease"]), [request])
    assert refused["admitted"] is False
    assert refused["code"] == "LEASE_NOT_HELD"
    assert refused["events"] == []
    assert "NOT_APPLIED" not in json.dumps(refused)


def test_send_deadline_is_the_three_lease_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    chosen = agent_node_send_deadline(30, 20, 25)
    assert chosen == {"deadline": 20, "code": "", "providerEffect": False}
    assert set(chosen) == {"deadline", "code", "providerEffect"}
    assert agent_node_send_deadline(11, 30, 40)["deadline"] == 11
    assert agent_node_send_deadline(30, 11, 40)["deadline"] == 11
    assert agent_node_send_deadline(30, 40, 11)["deadline"] == 11
    sent = execution_send_lease(chosen["deadline"], 10)
    assert sent["allowed"] is True
    assert admitted_provider_send(sent)["allowed"] is True
    flag = workspace_tool_lease(chosen["deadline"], 10)
    assert flag["leaseLive"] is True
    assert flag["lease"] == "live"
    chart = _chart_call("call-deadline")
    request = _tool_request([chart])
    ran = continue_retained_tools(_continuation_fields(lease=flag["lease"]), [request])
    assert ran["admitted"] is True
    assert ran["events"][0]["output"] == execute_tool_call(chart)
    assert "NOT_APPLIED" not in json.dumps(ran["events"])
    assert "epoch" not in chosen

    denied_lease = {
        "allowed": False,
        "code": "AGENT_EXECUTION_LEASE_DENIED",
        "markUnknown": True,
        "providerEffect": False,
    }
    expired_flag = {
        "leaseLive": False,
        "lease": "expired",
        "code": "AGENT_EXECUTION_LEASE_DENIED",
        "providerEffect": False,
    }
    dead_clocks = (
        ((40, 30, 15), 15),
        ((40, 0, 30), 10),
        ((-3, 40, 30), 10),
        ((40, 30, -8), 10),
        ((-9, -4, -1), 10),
        ((12, 12, 12), 12),
        ((40, 30, 25), 26),
    )
    for args, now in dead_clocks:
        decision = agent_node_send_deadline(*args)
        assert decision["code"] == ""
        assert decision["providerEffect"] is False
        assert decision["deadline"] == min(args)
        refused = execution_send_lease(decision["deadline"], now)
        assert refused == denied_lease
        assert admitted_provider_send(refused)["allowed"] is False
        assert "NOT_APPLIED" not in json.dumps(refused)
        assert workspace_tool_lease(decision["deadline"], now) == expired_flag
    missing = {"deadline": None, "code": "AGENT_EXECUTION_LEASE_DENIED", "providerEffect": False}
    for bad in (
        (True, 20, 30),
        (20, False, 30),
        (20, 30, True),
        ("20", 30, 40),
        (20, None, 40),
        (20, 30, 1.5),
        (2**63, 20, 30),
        (20, -(2**63) - 1, 30),
    ):
        decision = agent_node_send_deadline(*bad)
        assert decision == missing
        assert "epoch" not in decision
        assert "NOT_APPLIED" not in json.dumps(decision)
        assert execution_send_lease(decision["deadline"], 10) == denied_lease
        assert workspace_tool_lease(decision["deadline"], 10) == expired_flag

    def forbidden(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("tool")

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.tools.execute_tool_call", forbidden)
    held = continue_retained_tools(_continuation_fields(lease=expired_flag["lease"]), [request])
    assert held["admitted"] is False
    assert held["code"] == "LEASE_NOT_HELD"
    assert held["events"] == []
    assert "NOT_APPLIED" not in json.dumps(held)

    nodes = (ROOT / "rust/crates/deepseek-worker/src/authority_store/agent_nodes.rs").read_text(encoding="utf-8")
    body = nodes.split("fn agent_node_send_deadline(", 1)[1].split("fn agent_task_prior_outputs(", 1)[0]
    assert 'row.state != "DISPATCHING"' in body
    assert body.index("validate_scope(") < body.index("agent_runtime_admission::agent_node_send_deadline(")
    call = body.split("agent_runtime_admission::agent_node_send_deadline(", 1)[1].split(")", 1)[0]
    assert call.index("expires") < call.index("lease_until")
    assert call.index("lease_until") < call.index("writer_lease_until")
    assert "expires.min(" not in body
    assert "UPDATE " not in body
    assert "install_authoritative_epoch(" not in body
    assert "install_signed_epoch(" not in body
    task = (ROOT / "rust/crates/deepseek-worker/src/agent_task_executor.rs").read_text(encoding="utf-8")
    before_send = task.split("let deadline = service.node_send_lease(&permit).await?;", 1)[1].split(
        "exchange_fenced_turn(", 1
    )[0]
    assert before_send.index("execution_send_lease(deadline, now)") < before_send.index("admitted_provider_send(")
    assert before_send.index("if !admit.allowed") < before_send.index("execution_send_budget(")
    between = task.split(".confirm_agent_node_effect(", 1)[1].split("retain_workspace_tool_results(", 1)[0]
    assert "execution_send_lease(deadline, current)" in between
    assert between.index("admitted_provider_send(") < between.index("execution_send_lease(")
    assert between.index("execution_send_lease(") < between.index(".allowed")
    planner = (ROOT / "rust/crates/deepseek-worker/src/agent_plan_executor.rs").read_text(encoding="utf-8")
    plan_gate = planner.split("let deadline = service.node_send_lease(&permit).await?;", 1)[1].split(
        "exchange_fenced_turn(", 1
    )[0]
    assert plan_gate.index("execution_send_lease(") < plan_gate.index("admitted_provider_send(")
    assert "if !admit.allowed" in plan_gate
    assert "confirm_agent_node_effect(" not in plan_gate
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    rust_fn = rust.split("pub fn agent_node_send_deadline(", 1)[1]
    assert ".min(claim_lease).min(writer_lease)" in rust_fn
    assert "execute_tool_call" not in rust_fn
    assert "NOT_APPLIED" not in rust_fn
    assert "CONFIRMED" not in rust_fn
    assert "install_authoritative_epoch(" not in rust_fn
    oracle = (ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py").read_text(encoding="utf-8")
    py_fn = oracle.split("def agent_node_send_deadline(", 1)[1]
    assert "min(grant, claim, writer)" in py_fn
    assert "execute_tool_call(" not in py_fn
    assert "NOT_APPLIED" not in py_fn
    assert "CONFIRMED" not in py_fn


def test_send_budget_uses_only_the_live_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    chosen = agent_node_send_deadline(50, 30, 40)
    assert chosen["deadline"] == 30
    assert execution_send_lease(chosen["deadline"], 12)["allowed"] is True
    budget = execution_send_budget(chosen["deadline"], 12, 180)
    assert budget == {"armed": True, "seconds": 18, "code": "", "providerEffect": False}
    assert set(budget) == {"armed", "seconds", "code", "providerEffect"}
    assert execution_send_budget(chosen["deadline"], 12, 4)["seconds"] == 4
    assert execution_send_budget(chosen["deadline"], 12, 18)["seconds"] == 18
    short = agent_node_send_deadline(11, 40, 50)
    assert execution_send_budget(short["deadline"], 10, 180)["seconds"] == 1
    flag = workspace_tool_lease(chosen["deadline"], 12)
    assert flag["leaseLive"] is True
    chart = _chart_call("call-budget")
    request = _tool_request([chart])
    ran = continue_retained_tools(_continuation_fields(lease=flag["lease"]), [request])
    assert ran["admitted"] is True
    assert ran["events"][0]["output"] == execute_tool_call(chart)
    assert "NOT_APPLIED" not in json.dumps(ran["events"])
    assert "epoch" not in budget

    idle = {
        "armed": False,
        "seconds": None,
        "code": "AGENT_EXECUTION_LEASE_DENIED",
        "providerEffect": False,
    }
    dead = (
        ((40, 30, 15), 15),
        ((40, 0, 30), 10),
        ((-3, 40, 30), 10),
        ((12, 12, 12), 12),
        ((40, 30, 25), 26),
    )
    for args, now in dead:
        decision = agent_node_send_deadline(*args)
        assert execution_send_lease(decision["deadline"], now)["allowed"] is False
        refused = execution_send_budget(decision["deadline"], now, 180)
        assert refused == idle
        assert "NOT_APPLIED" not in json.dumps(refused)
        assert workspace_tool_lease(decision["deadline"], now)["leaseLive"] is False
    for cap in (0, -1, True, False, None, 1.5, 2**63):
        assert execution_send_budget(chosen["deadline"], 12, cap) == idle

    def forbidden(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("tool")

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.tools.execute_tool_call", forbidden)
    held = continue_retained_tools(_continuation_fields(lease="expired"), [request])
    assert held["code"] == "LEASE_NOT_HELD"
    assert held["events"] == []
    assert "NOT_APPLIED" not in json.dumps(held)

    task = (ROOT / "rust/crates/deepseek-worker/src/agent_task_executor.rs").read_text(encoding="utf-8")
    before_send = task.split("let deadline = service.node_send_lease(&permit).await?;", 1)[1].split(
        "exchange_fenced_turn(", 1
    )[0]
    assert before_send.index("if !admit.allowed") < before_send.index("execution_send_budget(")
    assert before_send.index("execution_send_budget(") < before_send.index("if !budget.armed")
    assert "Duration::from_secs(budget.seconds)" in before_send
    assert "deadline - now" not in before_send
    follow = task.split("if follow_admit.allowed {", 1)[1].split("exchange_fenced_turn(", 1)[0]
    assert follow.index("execution_send_budget(") < follow.index("if follow_budget.armed")
    assert follow.index("if follow_budget.armed") < follow.index("if let Some(next_body) = follow {")
    planner = (ROOT / "rust/crates/deepseek-worker/src/agent_plan_executor.rs").read_text(encoding="utf-8")
    plan_gate = planner.split("execution_send_lease(", 1)[1].split("exchange_fenced_turn(", 1)[0]
    assert "execution_send_budget(" in plan_gate
    assert plan_gate.index("if !admit.allowed") < plan_gate.index("execution_send_budget(")
    assert "if !budget.armed" in plan_gate
    assert "deadline - now" not in plan_gate
    rust = RUST_ADMISSION.read_text(encoding="utf-8")
    budget_fn = rust.split("pub fn execution_send_budget(", 1)[1]
    assert budget_fn.index("execution_send_lease(") < budget_fn.index("deadline - now")
    assert "admitted_provider_send(" in budget_fn
    assert "execute_tool_call" not in budget_fn
    assert "NOT_APPLIED" not in budget_fn
    assert "CONFIRMED" not in budget_fn
    oracle = (ROOT / "deepseek_infra/infra/native_runtime/agent_runtime_admission.py").read_text(encoding="utf-8")
    py_budget = oracle.split("def execution_send_budget(", 1)[1]
    assert py_budget.index("execution_send_lease(") < py_budget.index("left - right")
    assert "execute_tool_call(" not in py_budget
    assert "NOT_APPLIED" not in py_budget
    assert "CONFIRMED" not in py_budget
