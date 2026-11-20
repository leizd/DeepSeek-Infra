"""Offline report-boundary tests; these fixtures never qualify native custody."""
from __future__ import annotations

import base64
import copy
import hashlib
from typing import Any

import pytest

from scripts.verify_native_agent_terminal_failures import _receipt_types, validate_terminal_result, validate_unknown_result, verify_terminal_custody


def sample(mode: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    planning = mode == "plan"
    nodes = ["leader", "researcher", "coder", "reasoner", "critic", "synthesizer"] if planning else ["leader", "coder", "critic", "synthesizer", "synthesizer"]
    actions = ["original-plan", "new-tasks", "new-tasks", "new-tasks", "new-tasks", "new-synth"] if planning else ["plan", "tasks", "tasks", "failed-synth", "new-synth"]
    rejected = 0 if planning else 3
    documents = []
    for i, node in enumerate(nodes):
        raw = f"offline-placeholder-outcome-{i}".encode()
        documents.append({"actionId": actions[i], "nodeId": node, "operationId": f"operation-{i}", "failed": False,
            "terminalFailure": i == rejected, "httpStatus": 401 if i == rejected else 0,
            "receiptBase64": base64.urlsafe_b64encode(raw).decode().rstrip("="), "receiptSha256": hashlib.sha256(raw).hexdigest(),
            "pinnedSignatureVerified": True})
    raw = b"offline-placeholder-projection"
    result: dict[str, Any] = {
        "status": "PASS", "mode": mode, "sourceCommit": "a" * 40, "runId": "offline-terminal-" + mode, "runStatus": "done",
        "initialEventCount": 2 if planning else 8, "nextIndex": 14 if planning else 12, "publicEventCount": 14 if planning else 12,
        "initialActionCount": 1 if planning else 3, "durablePhases": 3 if planning else 4, "runtimeRevision": 2, "generation": 1,
        "metadataSchemaVersion": 3, "initialPublicPlanCount": 0 if planning else 2, "publicPlanCount": 0 if planning else 2,
        "executeCount": 4 if planning else 0, "effectiveRecoveryRoles": ["researcher", "coder", "reasoner", "critic"] if planning else ["coder", "critic"],
        "receiptDocuments": documents,
        "failureProjectionDocuments": [{"actionId": actions[rejected], "receiptBase64": base64.urlsafe_b64encode(raw).decode().rstrip("="),
            "receiptSha256": hashlib.sha256(raw).hexdigest(), "pinnedSignatureVerified": True}],
        "requestSha256": "b" * 64, "originalRequestSha256": "c" * 64, "contextArtifactSha256": "d" * 64, "contextSha256": "e" * 64,
        "requestLength": 100, "contextArtifactLength": 100, "originalActionsUnchanged": True, "originalEventsUnchanged": True,
        "resourcesReleased": True, "coherentPublicSettlementVerified": True, "finalAnswerVisible": True, "initialFailurePublic": True,
        "publicResumeQualified": True, "defaultDAGKeptOutOfPublicPlan": planning, "inputBodySeededByOfflineOracle": False,
        "productionProviderQualified": False, "fullMigrationQualified": False, "providerFixtureIsOfflineTestTool": True,
        "productionRuntimeProcesses": ["Go", "Rust"],
    }
    exchanges = [{"actionId": actions[i], "operationId": f"operation-{i}", "executionEpoch": "1", "stream": False,
        "httpStatus": 401 if i == rejected else 200, "responseSha256": hashlib.sha256(f"response-{i}".encode()).hexdigest(),
        "requestCredentialUsed": True, "privateReasoningInPrompt": False, "terminalDefaultDescriptionsInPrompt": True,
        "synthesisCoderInPrompt": True, "synthesisCriticInPrompt": True} for i in range(len(nodes))]
    return result, exchanges


@pytest.mark.parametrize("mode", ["plan", "synthesis"])
def test_terminal_report_requires_explicit_recovery_and_original_bindings(mode: str) -> None:
    result, exchanges = sample(mode)
    validate_terminal_result(result, exchanges, mode)


@pytest.mark.parametrize("field", ["originalActionsUnchanged", "originalEventsUnchanged", "resourcesReleased", "coherentPublicSettlementVerified",
                                  "finalAnswerVisible", "initialFailurePublic", "publicResumeQualified", "providerFixtureIsOfflineTestTool"])
@pytest.mark.parametrize("mode", ["plan", "synthesis"])
def test_terminal_report_rejects_missing_actual_proofs(field: str, mode: str) -> None:
    result, exchanges = sample(mode)
    result[field] = False
    with pytest.raises(ValueError):
        validate_terminal_result(result, exchanges, mode)


@pytest.mark.parametrize("field,value", [("initialActionCount", True), ("nextIndex", 16), ("initialEventCount", 3), ("nextIndex", 15), ("runtimeRevision", 1), ("generation", 2),
    ("publicPlanCount", 4), ("executeCount", 0), ("defaultDAGKeptOutOfPublicPlan", False), ("fullMigrationQualified", True),
    ("productionProviderQualified", True), ("inputBodySeededByOfflineOracle", True), ("requestSha256", "c" * 64),
    ("contextSha256", "not-a-digest"), ("requestLength", True), ("effectiveRecoveryRoles", ["coder", "critic"])])
def test_terminal_report_rejects_wrong_dag_scope_or_false_completion(field: str, value: Any) -> None:
    result, exchanges = sample("plan")
    result[field] = value
    with pytest.raises(ValueError):
        validate_terminal_result(result, exchanges, "plan")


@pytest.mark.parametrize("mode", ["plan", "synthesis"])
@pytest.mark.parametrize("mutation", ["duplicate-call", "extra-call", "unknown-response", "lost-response-hash", "credential", "private-reasoning",
    "missing-outcome", "missing-projection", "projection-rebound", "bad-document", "unpinned-signature", "failure-as-task", "wrong-phase", "lost-synthesis-input"])
def test_terminal_report_rejects_ambiguous_effects_and_fabricated_custody(mode: str, mutation: str) -> None:
    result, exchanges = copy.deepcopy(sample(mode))
    rejected = 0 if mode == "plan" else 3
    if mutation == "duplicate-call":
        exchanges[-1]["operationId"] = exchanges[0]["operationId"]
    elif mutation == "extra-call":
        exchanges.append(copy.deepcopy(exchanges[-1]))
    elif mutation == "unknown-response":
        exchanges[rejected]["httpStatus"] = 503
    elif mutation == "lost-response-hash":
        exchanges[-1].pop("responseSha256")
    elif mutation == "credential":
        exchanges[-1]["requestCredentialUsed"] = False
    elif mutation == "private-reasoning":
        exchanges[-1]["privateReasoningInPrompt"] = True
    elif mutation == "missing-outcome":
        result["receiptDocuments"].pop()
    elif mutation == "missing-projection":
        result["failureProjectionDocuments"] = []
    elif mutation == "projection-rebound":
        result["failureProjectionDocuments"][0]["actionId"] = "different-action"
    elif mutation == "bad-document":
        result["receiptDocuments"][0]["receiptSha256"] = "f" * 64
    elif mutation == "unpinned-signature":
        result["receiptDocuments"][0]["pinnedSignatureVerified"] = False
    elif mutation == "failure-as-task":
        result["receiptDocuments"][rejected]["failed"] = True
    elif mutation == "wrong-phase":
        result["receiptDocuments"][rejected]["nodeId"] = "coder"
    elif mutation == "lost-synthesis-input":
        exchanges[-1]["terminalDefaultDescriptionsInPrompt"] = False
        exchanges[-1]["synthesisCriticInPrompt"] = False
    with pytest.raises(ValueError):
        validate_terminal_result(result, exchanges, mode)


def protobuf_documents(result: dict[str, Any]) -> tuple[list[Any], Any]:
    """Unsigned test bytes are used only for rejection before journal access."""
    node_type, projection_type = _receipt_types()
    nodes = []
    for d in result["receiptDocuments"]:
        receipt = node_type(schema_version=1, signature=b"x" * 64)
        e = receipt.evidence
        e.schema_version, e.run_id, e.operation_id, e.node_id = 1, result["runId"], d["operationId"], d["nodeId"]
        e.fence.action_id, e.fence.execution_epoch = d["actionId"], 1
        e.phase, e.metadata_epoch = (1 if e.node_id == "leader" else 3 if e.node_id == "synthesizer" else 2), 2
        e.public_event.sha256, e.public_event.length = "f" * 64, 20
        e.fleet_id, e.environment = "offline-fleet", "offline-env"
        if d["terminalFailure"]:
            e.failure.schema_version, e.failure.http_status, e.failure.response_sha256 = 1, 401, "e" * 64
        nodes.append(receipt)
    projection = projection_type(schema_version=1, signature=b"y" * 64)
    p = projection.evidence
    p.schema_version, p.settlement_writer_token, p.run_id, p.phase = 1, 1, result["runId"], 1
    p.settlement_fence.action_id, p.settlement_fence.execution_epoch = nodes[0].evidence.fence.action_id, 1
    p.metadata_epoch, p.expected_index = 2, result["initialEventCount"] - 1
    p.error_body.CopyFrom(nodes[0].evidence.public_event)
    p.fleet_id, p.environment = "offline-fleet", "offline-env"
    return nodes, projection


def encode_document(document: dict[str, Any], message: Any) -> None:
    raw = message.SerializeToString(deterministic=True)
    document.update(receiptBase64=base64.urlsafe_b64encode(raw).decode().rstrip("="), receiptSha256=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("field", ["run", "operation", "action", "node", "failure", "status", "schema"])
def test_terminal_custody_rejects_rebound_protobuf_before_opening_any_journal(tmp_path: Any, field: str) -> None:
    result, exchanges = sample("plan")
    nodes, _ = protobuf_documents(result)
    e = nodes[0].evidence
    if field == "run":
        e.run_id = "other-run"
    elif field == "operation":
        e.operation_id = "other-operation"
    elif field == "action":
        e.fence.action_id = "other-action"
    elif field == "node":
        e.node_id = "coder"
    elif field == "failure":
        e.ClearField("failure")
    elif field == "status":
        e.failure.http_status = 429
    else:
        e.schema_version = 2
    encode_document(result["receiptDocuments"][0], nodes[0])
    with pytest.raises(ValueError, match="signed protobuf"):
        verify_terminal_custody(result, exchanges, tmp_path / "absent-state", tmp_path / "absent-artifacts")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field", ["source", "action", "epoch", "run", "phase", "metadata", "cursor", "body", "fleet", "writer"])
def test_terminal_projection_rebinding_cannot_reach_or_create_a_custody_journal(tmp_path: Any, field: str) -> None:
    result, exchanges = sample("plan")
    nodes, projection = protobuf_documents(result)
    for document, message in zip(result["receiptDocuments"], nodes, strict=True):
        encode_document(document, message)
    p = projection.evidence
    p.source_receipt_sha256 = result["receiptDocuments"][0]["receiptSha256"]
    if field == "source":
        p.source_receipt_sha256 = "0" * 64
    elif field == "action":
        p.settlement_fence.action_id = "other-action"
    elif field == "epoch":
        p.settlement_fence.execution_epoch = 0
    elif field == "run":
        p.run_id = "other-run"
    elif field == "phase":
        p.phase = 3
    elif field == "metadata":
        p.metadata_epoch = 3
    elif field == "cursor":
        p.expected_index += 1
    elif field == "body":
        p.error_body.sha256 = "0" * 64
    elif field == "fleet":
        p.fleet_id = "other-fleet"
    else:
        p.settlement_writer_token = 0
    encode_document(result["failureProjectionDocuments"][0], projection)
    with pytest.raises(ValueError, match="original signed custody"):
        verify_terminal_custody(result, exchanges, tmp_path / "absent-state", tmp_path / "absent-artifacts")
    assert not list(tmp_path.iterdir())


def unknown_sample(mode: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    planning, eof = mode.startswith("plan-"), mode.endswith("-eof")
    prior, exchanges = sample("synthesis")
    exchanges = [] if planning else exchanges[:3]
    for exchange in exchanges:
        exchange["responseComplete"] = True
    exchanges.append({"actionId": "unknown-action", "operationId": "unknown-operation", "executionEpoch": "1", "stream": False,
        "httpStatus": 401 if eof else 503, "responseComplete": not eof, "responseSha256": "f" * 64,
        "requestCredentialUsed": True, "privateReasoningInPrompt": False})
    result = {"status": "PASS", "mode": mode, "sourceCommit": "a" * 40, "runId": "offline-unknown-" + mode, "runStatus": "cancelled",
        "initialEventCount": 1 if planning else 7, "nextIndex": 2 if planning else 8, "durablePhases": 1 if planning else 3,
        "resumeStatus": 409, "rerunStatus": 409, "unknownOperationId": "unknown-operation", "unknownActionId": "unknown-action", "originalExecutionEpoch": 1,
        "requestSha256": "b" * 64, "requestLength": 100, "receiptDocuments": [] if planning else prior["receiptDocuments"][:3],
        "unknownNotClosed": True, "originalFenceRetained": True, "reservationRetained": True, "noRecoveryGeneration": True,
        "originalRequestRetained": True, "inputBodySeededByOfflineOracle": False, "productionProviderQualified": False,
        "fullMigrationQualified": False, "providerFixtureIsOfflineTestTool": True, "productionRuntimeProcesses": ["Go", "Rust"]}
    return result, exchanges


@pytest.mark.parametrize("mode", ["plan-5xx", "plan-eof"])
def test_unknown_planner_requires_its_actual_admitted_created_cursor(mode: str) -> None:
    result, exchanges = unknown_sample(mode)
    result["initialEventCount"], result["nextIndex"] = 2, 3
    with pytest.raises(ValueError):
        validate_unknown_result(result, exchanges, mode)


@pytest.mark.parametrize("mode", ["plan-5xx", "plan-eof", "synthesis-5xx", "synthesis-eof"])
def test_unknown_effect_requires_cancelled_public_state_without_a_new_generation(mode: str) -> None:
    result, exchanges = unknown_sample(mode)
    validate_unknown_result(result, exchanges, mode)


@pytest.mark.parametrize("mode", ["plan-5xx", "plan-eof", "synthesis-5xx", "synthesis-eof"])
@pytest.mark.parametrize("change", ["closed", "new-generation", "released", "rebound-operation", "rebound-epoch", "extra-call",
    "conclusive", "false-completeness", "private-reasoning", "request", "false-production", "cursor"])
def test_unknown_effect_cannot_be_erased_rebound_or_qualified_as_success(mode: str, change: str) -> None:
    result, exchanges = unknown_sample(mode)
    if change == "closed":
        result["unknownNotClosed"] = False
    elif change == "new-generation":
        result["noRecoveryGeneration"] = False
    elif change == "released":
        result["reservationRetained"] = False
    elif change == "rebound-operation":
        result["unknownOperationId"] = "different-operation"
    elif change == "rebound-epoch":
        result["originalExecutionEpoch"] = 2
    elif change == "extra-call":
        exchanges.append(copy.deepcopy(exchanges[-1]))
    elif change == "conclusive":
        exchanges[-1]["httpStatus"] = 200
    elif change == "false-completeness":
        exchanges[-1]["responseComplete"] = not exchanges[-1]["responseComplete"]
    elif change == "private-reasoning":
        exchanges[-1]["privateReasoningInPrompt"] = True
    elif change == "request":
        result["requestSha256"] = "not-a-digest"
    elif change == "false-production":
        result["fullMigrationQualified"] = True
    else:
        result["nextIndex"] = 0
    with pytest.raises(ValueError):
        validate_unknown_result(result, exchanges, mode)
