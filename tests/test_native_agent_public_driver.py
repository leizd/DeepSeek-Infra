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


def test_actual_public_edge_configuration_retains_only_explicit_control_and_body_custody(tmp_path):
    from scripts.verify_native_agent_public_driver import public_edge_environment

    source = {"PATH": "offline-test-path", "DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE": "private-bundle",
        "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE": "private-passphrase",
        "DEEPSEEK_WORKER_TLS_KEY_FILE": "private-tls-key", "DEEPSEEK_API_KEY": "private-provider-key",
        "DEEPSEEK_WORKER_AUTHORITY_NOW": "fake-clock", "GO_CONTROL_ADDR": "http://untrusted:80",
        "DEEPSEEK_INTERNAL_BEARER": "untrusted", "DEEPSEEK_AGENT_ARTIFACT_DIR": "untrusted-custody"}
    ready = {"origin": "http://127.0.0.1:18090", "internalBearer": "isolated-control-" + "x" * 32}
    static, artifacts = tmp_path / "static/ui", tmp_path / "rust-agent-artifacts"
    result = public_edge_environment(source, "127.0.0.1:18100", static, artifacts, ready)
    assert result == {"PATH": "offline-test-path", "GATEWAY_BIND_ADDR": "127.0.0.1:18100",
        "DEEPSEEK_RUNTIME_MODE": "python_disabled", "DEEPSEEK_INFRA_STATIC_DIR": static.parent.as_posix(),
        "AUTH_DISABLED": "1", "GO_CONTROL_ADDR": ready["origin"], "DEEPSEEK_INTERNAL_BEARER": ready["internalBearer"],
        "DEEPSEEK_AGENT_ARTIFACT_DIR": artifacts.as_posix()}
    assert not artifacts.exists()  # Configuration itself cannot seed input custody.


@pytest.mark.parametrize("ready", [{}, {"origin": 12, "internalBearer": "x" * 40},
    {"origin": "http://remote:80", "internalBearer": "x" * 40},
    {"origin": "http://127.0.0.1:18090", "internalBearer": "short"},
    {"origin": "http://127.0.0.1:18090", "internalBearer": 12}])
def test_public_edge_configuration_refuses_missing_actual_control_binding(tmp_path, ready):
    from scripts.verify_native_agent_public_driver import public_edge_environment

    with pytest.raises(ValueError, match="private loopback binding"):
        public_edge_environment({}, "127.0.0.1:18100", tmp_path / "static/ui", tmp_path / "rust-agent-artifacts", ready)


def test_preset_matrix_retains_static_paths_dynamic_fallbacks_and_confirmation_boundaries():
    from scripts.verify_native_agent_public_driver import preset_cases

    cases = preset_cases()
    assert len(cases) == 13
    assert {case["preset"] for case in cases} == {"full", "code", "research", "reason", "critic", "auto", "leader"}
    assert sum(case["upstreamPlanner"] for case in cases) == 2
    assert all(case["confirmation"] for case in cases if case["preset"] == "auto")
    assert all(not case["upstreamPlanner"] for case in cases if case["preset"] in {"full", "code", "research", "reason", "critic"})
    assert cases[-2]["query"] == "字" * 1801 and cases[-2]["confirmation"]
    assert cases[-1]["explicitConfirmation"] and cases[-1]["confirmation"]


@pytest.mark.parametrize("mutation", ["dependencies", "confirmation", "source", "extra-call", "private-reasoning", "upstream-overclaim"])
def test_preset_evidence_rejects_semantic_and_provider_dispatch_changes(mutation):
    from scripts.verify_native_agent_public_driver import preset_cases, validate_preset_result
    import copy

    case = next(value for value in preset_cases() if value["preset"] == "research")
    report = {"status":"PASS","runStatus":"done","durablePhases":3,"nextIndex":8,"publicEventCount":8,
        "actualPlan":copy.deepcopy(case["plan"]),"actualPreset":"research","actualPlanningSource":"retained-preset-policy",
        "planConfirmationRequired":False,"publicPlanConfirmationQualified":False,"noTasksBeforePublicConfirmation":False,
        "resourcesReleased":True,"finalAnswerVisible":True,"inputBodySeededByOfflineOracle":False,"requestBodyStoredByRust":True,
        "productionRuntimeProcesses":["Go","Rust"],"productionProviderQualified":False,"fullMigrationQualified":False}
    exchanges = [{"actionId":"tasks" if i < 2 else "final","operationId":str(i),"executionEpoch":"1","stream":False,
        "privateReasoningInPrompt":False,"predecessorSummaryInPrompt":i == 1} for i in range(3)]
    validate_preset_result(report, exchanges, case)
    if mutation == "dependencies":
        report["actualPlan"][1]["depends_on"] = []
    elif mutation == "confirmation":
        report["planConfirmationRequired"] = True
    elif mutation == "source":
        report["actualPlanningSource"] = "upstream-provider"
    elif mutation == "extra-call":
        exchanges.append(dict(exchanges[0]))
    elif mutation == "private-reasoning":
        exchanges[0]["privateReasoningInPrompt"] = True
    else:
        report["productionProviderQualified"] = True
    with pytest.raises(ValueError):
        validate_preset_result(report, exchanges, case)
