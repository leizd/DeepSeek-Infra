from __future__ import annotations

import copy
from typing import Any

import pytest

from scripts.verify_native_agent_failed_tasks import validate_failed_task_result


def sample(mode: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rerun = mode == "rerun"
    result: dict[str, Any] = {
        "status": "PASS",
        "mode": mode,
        "sourceCommit": "a" * 40,
        "runStatus": "done",
        "initialEventCount": 9,
        "nextIndex": 16 if rerun else 15,
        "publicEventCount": 16 if rerun else 15,
        "durablePhases": 5,
        "runtimeRevision": 2,
        "generation": 1,
        "metadataSchemaVersion": 4 if rerun else 3,
        "initialTaskFailed": True,
        "recoveredTaskFailed": rerun,
        "initialReceiptCount": 4,
        "initialFailureRetained": True,
        "originalEventsUnchanged": True,
        "originalActionsUnchanged": True,
        "otherRoleNotRerun": True,
        "resourcesReleased": True,
        "admittedThroughRustHTTP": True,
        "finalAnswerVisible": True,
        "coherentPublicSettlementVerified": True,
        "observationRegressionVerified": True,
        "inputBodySeededByOfflineOracle": False,
        "productionProviderQualified": False,
        "fullMigrationQualified": False,
        "productionRuntimeProcesses": ["Go", "Rust"],
        "providerFixtureIsOfflineTestTool": True,
        "requestSha256": "a" * 64,
        "originalRequestSha256": "b" * 64,
        "contextArtifactSha256": "c" * 64,
        "contextSha256": "d" * 64,
        "requestLength": 100,
        "contextArtifactLength": 100,
        "newTaskActionId": "new-task",
        "newSynthesisActionId": "new-synth",
        "receiptDocuments": [
            {
                "operationId": f"operation-{i}",
                "actionId": f"action-{0 if i == 0 else 1 if i in (1, 2) else i - 1}",
                "nodeId": ["leader", "coder", "critic", "synthesizer", "coder", "synthesizer"][i],
                "failed": i == 1 or (rerun and i == 4),
                "pinnedSignatureVerified": True,
                "receiptBase64": "YQ",
                "receiptSha256": "e" * 64,
            }
            for i in range(6)
        ],
    }
    if rerun:
        result.update(progressSignatureVerified=True, progressReceiptBase64="Yg", progressReceiptSha256="f" * 64)
    exchanges = [
        {
            "actionId": x["actionId"],
            "operationId": x["operationId"],
            "executionEpoch": "1",
            "stream": False,
            "model": "deepseek-v4-pro",
            "privateReasoningInPrompt": False,
            "requestCredentialUsed": True,
            "httpStatus": 401 if i == 1 else 429 if rerun and i == 4 else 200,
            "failedPredecessorInPrompt": i == 2,
            "synthesisFailureNoticeInPrompt": i == 3 or rerun and i == 5,
            "recoveredTaskSummaryInPrompt": not rerun and i == 5,
        }
        for i, x in enumerate(result["receiptDocuments"])
    ]
    return result, exchanges


@pytest.mark.parametrize("mode", ["resume", "rerun"])
def test_failed_task_report_accepts_only_explicit_proved_closed_attempts(mode: str) -> None:
    result, exchanges = sample(mode)
    validate_failed_task_result(result, exchanges, mode)


@pytest.mark.parametrize(
    "field",
    [
        "initialFailureRetained",
        "originalEventsUnchanged",
        "originalActionsUnchanged",
        "otherRoleNotRerun",
        "resourcesReleased",
        "admittedThroughRustHTTP",
        "finalAnswerVisible",
        "coherentPublicSettlementVerified",
        "observationRegressionVerified",
    ],
)
def test_failed_task_report_rejects_missing_proofs(field: str) -> None:
    result, exchanges = sample("resume")
    result[field] = False
    with pytest.raises(ValueError):
        validate_failed_task_result(result, exchanges, "resume")


@pytest.mark.parametrize(
    "field,value",
    [
        ("durablePhases", 6),
        ("initialEventCount", 8),
        ("runtimeRevision", 1),
        ("generation", 2),
        ("nextIndex", 14),
        ("recoveredTaskFailed", True),
        ("fullMigrationQualified", True),
        ("inputBodySeededByOfflineOracle", True),
        ("productionProviderQualified", True),
        ("requestSha256", "b" * 64),
    ],
)
def test_failed_task_report_rejects_changed_boundaries(field: str, value: Any) -> None:
    result, exchanges = sample("resume")
    result[field] = value
    with pytest.raises(ValueError):
        validate_failed_task_result(result, exchanges, "resume")


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate-call",
        "second-provider-call",
        "missing-rejection",
        "server-error",
        "missing-warning",
        "missing-credential",
        "private-reasoning",
        "missing-custody",
        "false-success",
    ],
)
def test_failed_task_report_rejects_ambiguous_effects_or_fabricated_success(mutation: str) -> None:
    result, exchanges = sample("rerun")
    exchanges = copy.deepcopy(exchanges)
    if mutation == "duplicate-call":
        exchanges[4]["operationId"] = exchanges[1]["operationId"]
    elif mutation == "second-provider-call":
        exchanges.append(exchanges[-1])
    elif mutation == "missing-rejection":
        exchanges[1]["httpStatus"] = 200
    elif mutation == "server-error":
        exchanges[4]["httpStatus"] = 503
    elif mutation == "missing-warning":
        exchanges[3]["synthesisFailureNoticeInPrompt"] = False
    elif mutation == "missing-credential":
        exchanges[4]["requestCredentialUsed"] = False
    elif mutation == "private-reasoning":
        exchanges[2]["privateReasoningInPrompt"] = True
    elif mutation == "missing-custody":
        result["receiptDocuments"][1]["pinnedSignatureVerified"] = False
    elif mutation == "false-success":
        result["receiptDocuments"][4]["failed"] = False
    with pytest.raises(ValueError):
        validate_failed_task_result(result, exchanges, "rerun")
