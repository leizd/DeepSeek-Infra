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


def complete_recovery_evidence():
    base, exchanges = complete_evidence()
    base.update(runId="controlled-recovery", sourceCommit="f" * 40, requestSha256="a" * 64)
    result = copy.deepcopy(base)
    result.update(baseExecution=base, nextIndex=11, publicEventCount=11, requestSha256="b" * 64)
    result["recoveryPreparation"] = {
        "kind":"completed-resume", "generation":1, "originalRuntimeRevision":1, "runtimeRevision":2,
        "priorEffectCount":4, "retainedOutputCount":2, "newProviderCalls":0, "newDurablePhases":0,
        "goOpenedRustBody":False, "fullResumeRerunQualified":False, "originalRequestSha256":"a" * 64,
        "contextSha256":"c" * 64, "contextArtifactSha256":"d" * 64, "contextArtifactLength":180,
        **{key:True for key in ["preparedByRust", "readOnlyPreview", "preparationRetryIdempotent", "commitRetryIdempotent",
            "forgedContextDenied", "unauthenticatedDenied", "stalePreparationDenied", "originalEffectsUnchanged",
            "originalPublicEventsUnchanged", "publicReadQualified"]}}
    return result, exchanges


def test_recovery_preparation_requires_original_success_and_new_exact_custody():
    from scripts.verify_native_agent_public_driver import validate_recovery_preparation_result

    result, exchanges = complete_recovery_evidence()
    original = copy.deepcopy(result)
    validate_recovery_preparation_result(result, exchanges)
    assert result == original


@pytest.mark.parametrize("section,key,value", [
    ("recoveryPreparation", "priorEffectCount", 3), ("recoveryPreparation", "retainedOutputCount", 0),
    ("recoveryPreparation", "newProviderCalls", 1), ("recoveryPreparation", "newDurablePhases", 1),
    ("recoveryPreparation", "goOpenedRustBody", True), ("recoveryPreparation", "fullResumeRerunQualified", True),
    ("recoveryPreparation", "generation", 2), ("recoveryPreparation", "runtimeRevision", 1),
    ("recoveryPreparation", "contextSha256", "unbound"), ("recoveryPreparation", "originalRequestSha256", "e" * 64),
    ("recoveryPreparation", "commitRetryIdempotent", False), ("recoveryPreparation", "stalePreparationDenied", False),
    ("recoveryPreparation", "forgedContextDenied", False), ("recoveryPreparation", "contextArtifactLength", 0),
    ("baseExecution", "durablePhases", 2), ("baseExecution", "sourceCommit", "e" * 40),
    ("", "nextIndex", 9), ("", "publicEventCount", 9), ("", "requestSha256", "a" * 64),
    ("", "productionProviderQualified", True), ("", "fullMigrationQualified", True),
])
def test_recovery_preparation_rejects_partial_or_rebound_evidence(section, key, value):
    from scripts.verify_native_agent_public_driver import validate_recovery_preparation_result

    result, exchanges = complete_recovery_evidence()
    (result[section] if section else result)[key] = value
    with pytest.raises(ValueError):
        validate_recovery_preparation_result(result, exchanges)


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
    with pytest.raises(ValueError, match="recovery|incomplete or overclaims"):
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


def test_credential_transport_is_explicit_and_ambient_configuration_cannot_override_it(tmp_path):
    from scripts.verify_native_agent_public_driver import public_edge_environment

    ready = {"origin": "http://127.0.0.1:18090", "internalBearer": "isolated-control-" + "x" * 32}
    source = {"DEEPSEEK_AGENT_CREDENTIAL_WORKER_ORIGIN": "https://untrusted.example:443",
              "DEEPSEEK_AGENT_CREDENTIAL_WORKER_BEARER": "ambient-fixture",
              "DEEPSEEK_WORKER_TLS_KEY_FILE": "private-key", "DEEPSEEK_API_KEY": "private-provider"}
    transport = {"ORIGIN": "https://127.0.0.1:18091", "CA_FILE": (tmp_path / "ca.pem").as_posix(),
                 "SERVER_NAME": "localhost", "BEARER": "isolated-worker-" + "y" * 32}
    result = public_edge_environment(source, "127.0.0.1:18100", tmp_path / "static/ui", tmp_path / "artifacts", ready, transport)
    assert result["DEEPSEEK_AGENT_CREDENTIAL_WORKER_ORIGIN"] == transport["ORIGIN"]
    assert result["DEEPSEEK_AGENT_CREDENTIAL_WORKER_BEARER"] == transport["BEARER"]
    assert "DEEPSEEK_WORKER_TLS_KEY_FILE" not in result and "DEEPSEEK_API_KEY" not in result
    stripped = public_edge_environment(source, "127.0.0.1:18100", tmp_path / "static/ui", tmp_path / "artifacts", ready)
    assert not any(key.startswith("DEEPSEEK_AGENT_CREDENTIAL_") for key in stripped)


@pytest.mark.parametrize("target", ["127.0.0.1:1", "127.0.0.1:50052", "127.0.0.1:65535"])
def test_worker_listen_target_becomes_a_tls_origin_for_public_credential_custody(tmp_path, target):
    from scripts.verify_native_agent_public_driver import credential_worker_transport, public_edge_environment

    ca = tmp_path / "ca.pem"
    bearer = "isolated-worker-" + "y" * 32
    transport = credential_worker_transport(target, ca, bearer)
    ready = {"origin": "http://127.0.0.1:18090", "internalBearer": "isolated-control-" + "x" * 32}
    environment = public_edge_environment({}, "127.0.0.1:18100", tmp_path / "static/ui", tmp_path / "artifacts", ready, transport)

    assert transport == {"ORIGIN": "https://" + target, "CA_FILE": ca.as_posix(), "SERVER_NAME": "localhost", "BEARER": bearer}
    assert environment["DEEPSEEK_AGENT_CREDENTIAL_WORKER_ORIGIN"] == "https://" + target
    assert environment["DEEPSEEK_AGENT_CREDENTIAL_WORKER_CA_FILE"] == ca.as_posix()
    assert environment["DEEPSEEK_AGENT_CREDENTIAL_WORKER_SERVER_NAME"] == "localhost"
    assert not ca.exists()  # Assembling transport configuration creates no private material.


@pytest.mark.parametrize("mutation", ["remote", "relative-ca", "missing-field", "short-bearer"])
def test_invalid_credential_transport_is_rejected_before_starting_a_process(tmp_path, mutation):
    from scripts.verify_native_agent_public_driver import public_edge_environment

    ready = {"origin": "http://127.0.0.1:18090", "internalBearer": "isolated-control-" + "x" * 32}
    transport = {"ORIGIN": "https://127.0.0.1:18091", "CA_FILE": (tmp_path / "ca.pem").as_posix(),
                 "SERVER_NAME": "localhost", "BEARER": "isolated-worker-" + "y" * 32}
    if mutation == "remote":
        transport["ORIGIN"] = "https://untrusted.example:443"
    elif mutation == "relative-ca":
        transport["CA_FILE"] = "relative.pem"
    elif mutation == "missing-field":
        transport.pop("SERVER_NAME")
    else:
        transport["BEARER"] = "short-fixture"
    with pytest.raises(ValueError, match="credential custody transport"):
        public_edge_environment({}, "127.0.0.1:18100", tmp_path / "static/ui", tmp_path / "artifacts", ready, transport)


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


def cancellation_evidence():
    from scripts.verify_native_agent_public_driver import cancellation_cases
    case = cancellation_cases()[0]
    result = {"status":"PASS","runStatus":"cancelled","beforeCancelIndex":4,"nextIndex":5,"publicEventCount":5,"publicStreamEventCount":5,
              "durablePhases":1,"actualPlan":case["plan"],"actualPreset":case["preset"],"actualPlanningSource":"upstream-provider",
              "resourcesReleased":True,"noTasksAfterCancellation":True,"repeatCancellationIdempotent":True,
              "confirmationAfterCancellationDenied":True,"planConfirmationRequired":True,"requestBodyStoredByRust":True,
              "publicCancellationWhileAwaitingPlanQualified":True,"inputBodySeededByOfflineOracle":False,
              "productionProviderQualified":False,"fullMigrationQualified":False,"activeTaskCancellationQualified":False,"productionRuntimeProcesses":["Go","Rust"]}
    return result,[{"actionId":"planner-action","operationId":"leader","executionEpoch":"1"}],case


def test_cancellation_evidence_retains_actual_scope_and_is_not_mutated():
    from scripts.verify_native_agent_public_driver import validate_cancellation_result
    result,exchanges,case=cancellation_evidence()
    before=copy.deepcopy((result,exchanges,case))
    validate_cancellation_result(result,exchanges,case)
    assert (result,exchanges,case)==before


def test_static_cancellation_requires_no_provider_exchange():
    from scripts.verify_native_agent_public_driver import cancellation_cases, validate_cancellation_result
    result,_,_=cancellation_evidence()
    case=cancellation_cases()[1]
    result.update(actualPlan=case["plan"],actualPreset=case["preset"],actualPlanningSource="retained-preset-policy")
    validate_cancellation_result(result,[],case)


@pytest.mark.parametrize("key,value",[
    ("runStatus","done"),("nextIndex",6),("publicStreamEventCount",4),("durablePhases",2),("beforeCancelIndex",True),
    ("noTasksAfterCancellation",False),("repeatCancellationIdempotent",False),("confirmationAfterCancellationDenied",False),
    ("activeTaskCancellationQualified",True),("fullMigrationQualified",True),("productionProviderQualified",True),
    ("inputBodySeededByOfflineOracle",True),("requestBodyStoredByRust",False),("actualPlan",[]),
])
def test_cancellation_rejects_missing_or_overclaimed_proof(key,value):
    from scripts.verify_native_agent_public_driver import validate_cancellation_result
    result,exchanges,case=cancellation_evidence()
    result[key]=value
    with pytest.raises(ValueError,match="recovery|incomplete or overclaims"):
        validate_cancellation_result(result,exchanges,case)


def test_cancellation_rejects_post_cancel_provider_requests_and_missing_fences():
    from scripts.verify_native_agent_public_driver import validate_cancellation_result
    result,exchanges,case=cancellation_evidence()
    with pytest.raises(ValueError,match="recovery|incomplete or overclaims"):
        validate_cancellation_result(result,exchanges*2,case)
    exchanges[0].pop("actionId")
    with pytest.raises(ValueError,match="durable fence"):
        validate_cancellation_result(result,exchanges,case)


def active_cancellation_evidence():
    from scripts.verify_native_agent_public_driver import active_cancellation_case
    case=active_cancellation_case()
    result={"status":"PASS","runStatus":"cancelled","beforeCancelIndex":4,"nextIndex":5,"durablePhases":2,"publicStreamEventCount":5,
            "actualPreset":case["preset"],"actualPlan":case["plan"],"productionRuntimeProcesses":["Go","Rust"],"resourcesRetained":False,"resourcesRetainedBeforeRecovery":True,
            "cancelledDuringActualProviderRequest":True,"actualLateTaskCustodyConfirmed":True,"noDependentOrSynthesisAdmission":True,
            "repeatCancellationIdempotent":True,"activeCancellationSafetyQualified":True,"requestBodyStoredByRust":True,
            "resourcesReleased":True,"cancelledPhaseRecoveryQualified":True,"controllerRestartRecoveryQualified":True,"originalExecutionEpoch":1,"recoveryExecutionEpoch":2,"inputBodySeededByOfflineOracle":False,"productionProviderQualified":False,"fullMigrationQualified":False}
    exchanges=[{"actionId":"plan","operationId":"leader","executionEpoch":"1"},{"actionId":"task","operationId":"coder","executionEpoch":"1"}]
    return result,exchanges,case


def test_active_cancellation_evidence_requires_authoritative_recovery():
    from scripts.verify_native_agent_public_driver import validate_active_cancellation_result
    result,exchanges,case=active_cancellation_evidence()
    before=copy.deepcopy((result,exchanges,case))
    validate_active_cancellation_result(result,exchanges,case)
    assert (result,exchanges,case)==before


@pytest.mark.parametrize("key,value",[
    ("beforeCancelIndex",-1),("nextIndex",6),("durablePhases",3),("publicStreamEventCount",4),("resourcesRetained",True),
    ("actualLateTaskCustodyConfirmed",False),("noDependentOrSynthesisAdmission",False),("cancelledDuringActualProviderRequest",False),
    ("resourcesReleased",False),("cancelledPhaseRecoveryQualified",False),("controllerRestartRecoveryQualified",False),("resourcesRetainedBeforeRecovery",False),("recoveryExecutionEpoch",1),("originalExecutionEpoch",0),("productionProviderQualified",True),("fullMigrationQualified",True),
])
def test_active_cancellation_refuses_missing_or_false_recovery_proof(key,value):
    from scripts.verify_native_agent_public_driver import validate_active_cancellation_result
    result,exchanges,case=active_cancellation_evidence()
    result[key]=value
    with pytest.raises(ValueError,match="recovery|incomplete or overclaims"):
        validate_active_cancellation_result(result,exchanges,case)


def test_active_cancellation_refuses_extra_calls_and_reused_phase_fence():
    from scripts.verify_native_agent_public_driver import validate_active_cancellation_result
    result,exchanges,case=active_cancellation_evidence()
    with pytest.raises(ValueError,match="recovery|incomplete or overclaims"):
        validate_active_cancellation_result(result,exchanges*2,case)
    exchanges[1]["actionId"]=exchanges[0]["actionId"]
    with pytest.raises(ValueError,match="distinct durable"):
        validate_active_cancellation_result(result,exchanges,case)


def planning_cancellation_evidence():
    from scripts.verify_native_agent_public_driver import active_planning_cancellation_case
    result, exchanges, _ = active_cancellation_evidence()
    case = active_planning_cancellation_case()
    result.update(actualPreset=case["preset"], actualPlan=None, beforeCancelIndex=1, nextIndex=2,
                  publicStreamEventCount=2, durablePhases=1, actualLateTaskCustodyConfirmed=False,
                  actualLateEffectCustodyConfirmed=True, noTaskAdmission=True, cancelledPhase="plan")
    return result, exchanges[:1], case


def test_planning_cancellation_evidence_requires_original_custody_and_no_task():
    from scripts.verify_native_agent_public_driver import validate_active_cancellation_result
    result, exchanges, case = planning_cancellation_evidence()
    before = copy.deepcopy((result, exchanges, case))
    validate_active_cancellation_result(result, exchanges, case)
    assert (result, exchanges, case) == before
    assert case["upstreamPlanner"] is True and case["cancelPhase"] == "plan"


@pytest.mark.parametrize("key,value", [
    ("actualPlan", [{"id":"coder","task":"fabricated"}]), ("durablePhases", 2),
    ("noTaskAdmission", False), ("actualLateEffectCustodyConfirmed", False),
    ("cancelledPhase", "tasks"), ("controllerRestartRecoveryQualified", False),
    ("resourcesReleased", False), ("resourcesRetained", True),
    ("recoveryExecutionEpoch", 1), ("productionProviderQualified", True),
])
def test_planning_cancellation_refuses_fabricated_projection_or_unproved_recovery(key, value):
    from scripts.verify_native_agent_public_driver import validate_active_cancellation_result
    result, exchanges, case = planning_cancellation_evidence()
    result[key] = value
    with pytest.raises(ValueError, match="cancellation|recovery"):
        validate_active_cancellation_result(result, exchanges, case)


def test_planning_cancellation_refuses_an_extra_provider_effect():
    from scripts.verify_native_agent_public_driver import validate_active_cancellation_result
    result, exchanges, case = planning_cancellation_evidence()
    with pytest.raises(ValueError, match="recovery"):
        validate_active_cancellation_result(result, exchanges * 2, case)
