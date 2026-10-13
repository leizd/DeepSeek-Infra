"""Offline evidence boundaries; these tests never start native compilation."""
import copy

import pytest

from scripts.verify_native_agent_public_driver import validate_driver_result


def complete_evidence():
    result = {"status": "PASS", "runStatus": "done", "nextIndex": 9, "durablePhases": 3, "publicEventCount": 9,
        "resourcesReleased": True, "finalAnswerVisible": True, "inputBodySeededByOfflineOracle": False,
        "productionRuntimeProcesses": ["Go", "Rust"], "publicCreationAndDAGDriverQualified": True,
        "publicPlanConfirmationQualified": True, "noTasksBeforePublicConfirmation": True,
        "productionProviderQualified": False, "fullMigrationQualified": False}
    exchanges = [{"actionId": action, "operationId": f"node-{index}", "executionEpoch": "1", "stream": False}
        for index, action in enumerate(["plan-action", "task-action", "task-action", "synthesis-action"])]
    exchanges[1]["selectedTaskInPrompt"] = True
    exchanges[2].update(dependentTaskInPrompt=True, predecessorSummaryInPrompt=True, privateReasoningInPrompt=False)
    exchanges[3].update(synthesisCoderInPrompt=True, synthesisCriticInPrompt=True)
    return result, exchanges


def test_public_driver_complete_controlled_evidence_retains_qualification_limits():
    result, exchanges = complete_evidence()
    original = copy.deepcopy((result, exchanges))
    validate_driver_result(result, exchanges)
    assert (result, exchanges) == original


@pytest.mark.parametrize("key,value", [
    ("status", "FAIL"), ("runStatus", "running"), ("nextIndex", 7), ("durablePhases", 2), ("publicEventCount", 7),
    ("resourcesReleased", 1), ("finalAnswerVisible", False), ("inputBodySeededByOfflineOracle", True),
    ("productionRuntimeProcesses", ["Go", "Rust", "Python"]), ("publicCreationAndDAGDriverQualified", False),
    ("productionProviderQualified", True), ("fullMigrationQualified", True),
    ("publicPlanConfirmationQualified", False), ("noTasksBeforePublicConfirmation", False),
])
def test_public_driver_incomplete_or_overclaimed_evidence_is_refused(key, value):
    result, exchanges = complete_evidence()
    result[key] = value
    with pytest.raises(ValueError, match="incomplete or overclaims"):
        validate_driver_result(result, exchanges)


@pytest.mark.parametrize("change", ["missing-call", "duplicate-call", "missing-action", "duplicate-node", "different-task-phase",
    "stale-epoch", "stream", "task", "dependency", "summary", "reasoning", "synthesis-coder", "synthesis-critic"])
def test_public_driver_actual_dispatch_and_dependency_boundaries(change):
    result, exchanges = complete_evidence()
    if change == "missing-call":
        exchanges.pop()
    elif change == "duplicate-call":
        exchanges.append(copy.deepcopy(exchanges[-1]))
    elif change == "missing-action":
        exchanges[0]["actionId"] = ""
    elif change == "duplicate-node":
        exchanges[1]["operationId"] = exchanges[2]["operationId"]
    elif change == "different-task-phase":
        exchanges[2]["actionId"] = "different-phase"
    elif change == "stale-epoch":
        exchanges[2]["executionEpoch"] = "0"
    elif change == "stream":
        exchanges[1]["stream"] = True
    else:
        index, field, value = {"task": (1, "selectedTaskInPrompt", False), "dependency": (2, "dependentTaskInPrompt", False),
            "summary": (2, "predecessorSummaryInPrompt", False), "reasoning": (2, "privateReasoningInPrompt", True),
            "synthesis-coder": (3, "synthesisCoderInPrompt", False), "synthesis-critic": (3, "synthesisCriticInPrompt", False)}[change]
        exchanges[index][field] = value
    with pytest.raises(ValueError):
        validate_driver_result(result, exchanges)
