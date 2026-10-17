#!/usr/bin/env python3
"""Actual native public API/DAG process probe, built only on the cloud runner."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import verify_agent_signing_process as signing  # noqa: E402


def credential_worker_transport(target: str, ca_file: Path, bearer: str) -> dict[str, str]:
    """Adapt the worker's socket target to the gateway's HTTPS origin contract."""
    return {"ORIGIN": "https://" + target, "CA_FILE": ca_file.as_posix(), "SERVER_NAME": "localhost", "BEARER": bearer}


def public_edge_environment(source: dict[str, str], address: str, static: Path, artifacts: Path,
                            ready: dict[str, Any], credential_worker: dict[str, str] | None = None) -> dict[str, str]:
    """Explicit gateway custody/control configuration after ambient identities are removed."""
    origin, bearer = ready.get("origin"), ready.get("internalBearer")
    if not isinstance(origin, str) or not origin.startswith("http://127.0.0.1:") or \
            not isinstance(bearer, str) or len(bearer.strip()) < 32:
        raise ValueError("Actual Go control readiness omitted the private loopback binding")
    environment = {key: value for key, value in signing.child_environment(source).items()
                   if not key.startswith("DEEPSEEK_AGENT_CREDENTIAL_")}
    environment.update(GATEWAY_BIND_ADDR=address, DEEPSEEK_RUNTIME_MODE="python_disabled",
        DEEPSEEK_INFRA_STATIC_DIR=static.parent.as_posix(), AUTH_DISABLED="1",
        GO_CONTROL_ADDR=origin, DEEPSEEK_INTERNAL_BEARER=bearer,
        DEEPSEEK_AGENT_ARTIFACT_DIR=artifacts.as_posix())
    if credential_worker is not None:
        expected = {"ORIGIN", "CA_FILE", "SERVER_NAME", "BEARER"}
        if set(credential_worker) != expected or not credential_worker["ORIGIN"].startswith("https://127.0.0.1:") or \
                not Path(credential_worker["CA_FILE"]).is_absolute() or len(credential_worker["BEARER"]) < 32:
            raise ValueError("Explicit private Rust credential custody transport is invalid")
        environment.update({"DEEPSEEK_AGENT_CREDENTIAL_WORKER_" + key: value for key, value in credential_worker.items()})
    return environment


def validate_driver_result(result: dict[str, Any], exchanges: list[dict[str, Any]]) -> None:
    if result.get("status") != "PASS" or result.get("runStatus") != "done" or result.get("nextIndex") != 9 or \
            result.get("durablePhases") != 3 or result.get("publicEventCount") != 9 or \
            result.get("resourcesReleased") is not True or result.get("finalAnswerVisible") is not True or \
            result.get("inputBodySeededByOfflineOracle") is not False or \
            result.get("productionRuntimeProcesses") != ["Go", "Rust"] or \
            result.get("publicCreationAndDAGDriverQualified") is not True or \
            result.get("publicPlanConfirmationQualified") is not True or result.get("noTasksBeforePublicConfirmation") is not True or \
            result.get("productionProviderQualified") is not False or result.get("fullMigrationQualified") is not False:
        raise ValueError("Native public driver evidence is incomplete or overclaims qualification")
    if len(exchanges) != 4:
        raise ValueError("Native public driver must make exactly four controlled provider calls")
    actions = [row.get("actionId") for row in exchanges]
    operations = [row.get("operationId") for row in exchanges]
    if any(not isinstance(value, str) or not value for value in actions + operations) or len(set(operations)) != 4 or \
            len(set(actions)) != 3 or actions[1] != actions[2] or \
            any(row.get("executionEpoch") != "1" or row.get("stream") is not False for row in exchanges):
        raise ValueError("Provider calls lack the actual distinct-node/shared-task-phase fences")
    if exchanges[1].get("selectedTaskInPrompt") is not True or exchanges[2].get("dependentTaskInPrompt") is not True or \
            exchanges[2].get("predecessorSummaryInPrompt") is not True or exchanges[2].get("privateReasoningInPrompt") is not False or \
            exchanges[3].get("synthesisCoderInPrompt") is not True or exchanges[3].get("synthesisCriticInPrompt") is not True:
        raise ValueError("Actual native DAG dependency/final request assembly is unqualified")


def validate_recovery_preparation_result(result: dict[str, Any], exchanges: list[dict[str, Any]]) -> None:
    base = result.get("baseExecution")
    recovery = result.get("recoveryPreparation")
    if not isinstance(base, dict) or not isinstance(recovery, dict):
        raise ValueError("Actual recovery preparation evidence is missing")
    validate_driver_result(base, exchanges)
    if result.get("status") != "PASS" or result.get("runStatus") != "done" or result.get("nextIndex") != 11 or \
            result.get("publicEventCount") != 11 or result.get("durablePhases") != 3 or \
            result.get("productionRuntimeProcesses") != ["Go", "Rust"] or result.get("finalAnswerVisible") is not True or \
            result.get("productionProviderQualified") is not False or result.get("fullMigrationQualified") is not False or \
            result.get("sourceCommit") != base.get("sourceCommit") or result.get("runId") != base.get("runId"):
        raise ValueError("Recovery preparation omitted actual public settlement or overclaims qualification")
    if recovery.get("kind") != "completed-resume" or recovery.get("generation") != 1 or \
            recovery.get("originalRuntimeRevision") != 1 or recovery.get("runtimeRevision") != 2 or \
            recovery.get("priorEffectCount") != 4 or recovery.get("retainedOutputCount") != 2 or \
            recovery.get("newProviderCalls") != 0 or recovery.get("newDurablePhases") != 0 or \
            recovery.get("goOpenedRustBody") is not False or recovery.get("fullResumeRerunQualified") is not False:
        raise ValueError("Recovery preparation lost original custody or admitted extra effects")
    for key in ["preparedByRust", "readOnlyPreview", "preparationRetryIdempotent", "commitRetryIdempotent",
                "forgedContextDenied", "unauthenticatedDenied", "stalePreparationDenied", "originalEffectsUnchanged",
                "originalPublicEventsUnchanged", "publicReadQualified"]:
        if recovery.get(key) is not True:
            raise ValueError("Recovery preparation lacks actual custody/CAS denial evidence: " + key)
    for value in [result.get("requestSha256"), recovery.get("originalRequestSha256"),
                  recovery.get("contextSha256"), recovery.get("contextArtifactSha256")]:
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Recovery preparation omitted a precise immutable artifact binding")
    if recovery["originalRequestSha256"] != base.get("requestSha256") or \
            result["requestSha256"] == base.get("requestSha256") or \
            type(recovery.get("contextArtifactLength")) is not int or recovery["contextArtifactLength"] <= 0:
        raise ValueError("Recovery preparation reused or omitted the new context/request custody")


def validate_recovery_execution_result(result: dict[str, Any], exchanges: list[dict[str, Any]]) -> None:
    base, recovery = result.get("baseExecution"), result.get("recoveryPreparation")
    if not isinstance(base, dict) or not isinstance(recovery, dict) or len(exchanges) != 5:
        raise ValueError("Direct synthesis recovery omitted original execution or made extra provider calls")
    validate_driver_result(base, exchanges[:4])
    if result.get("status") != "PASS" or result.get("runStatus") != "done" or result.get("nextIndex") != 13 or \
            result.get("publicEventCount") != 13 or result.get("durablePhases") != 4 or \
            result.get("sourceCommit") != base.get("sourceCommit") or result.get("runId") != base.get("runId") or \
            result.get("directSynthesisRecoveryQualified") is not True or result.get("productionProviderQualified") is not False or \
            result.get("fullMigrationQualified") is not False or result.get("productionRuntimeProcesses") != ["Go", "Rust"]:
        raise ValueError("Direct synthesis recovery omitted durable/public settlement or overclaims qualification")
    if recovery.get("kind") != "direct-synthesis" or recovery.get("generation") != 1 or \
            recovery.get("originalRuntimeRevision") != 1 or recovery.get("runtimeRevision") != 2 or \
            recovery.get("priorEffectCount") != 4 or recovery.get("retainedOutputCount") != 2 or \
            recovery.get("newProviderCalls") != 1 or recovery.get("newDurablePhases") != 1 or recovery.get("newTaskPhases") != 0 or \
            recovery.get("goOpenedRustBody") is not False or recovery.get("fullResumeRerunQualified") is not False:
        raise ValueError("Direct synthesis recovery reused false custody or invented task settlement")
    for flag in ["preparedByRust", "preparationRetryIdempotent", "commitRetryIdempotent", "stalePreparationDenied",
                 "originalEffectsUnchanged", "originalPublicEventsUnchanged", "publicReadQualified"]:
        if recovery.get(flag) is not True:
            raise ValueError("Direct synthesis recovery lacks actual proof: " + flag)
    for value in [result.get("requestSha256"), recovery.get("contextSha256"), recovery.get("contextArtifactSha256")]:
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Direct synthesis recovery omitted exact immutable bindings")
    if recovery.get("originalRequestSha256") != base.get("requestSha256") or result["requestSha256"] == base.get("requestSha256") or \
            type(recovery.get("contextArtifactLength")) is not int or recovery["contextArtifactLength"] <= 0:
        raise ValueError("Direct synthesis recovery rebound the original request")
    last = exchanges[4]
    if not last.get("actionId") or last.get("actionId") != recovery.get("newSynthesisActionId") or \
            last["actionId"] in {row["actionId"] for row in exchanges[:4]} or not last.get("operationId") or \
            last["operationId"] in {row["operationId"] for row in exchanges[:4]} or last.get("executionEpoch") != "1" or last.get("stream") is not False or \
            last.get("synthesisCoderInPrompt") is not True or last.get("synthesisCriticInPrompt") is not True or \
            last.get("privateReasoningInPrompt") is not False:
        raise ValueError("Direct synthesis lacks a fresh fenced effect and both original public summaries")


def public_completion_cases() -> list[dict[str, Any]]:
    return [{"name":alias or "completed-resume","alias":alias,"synthesize":bool(alias),"path":"rerun" if alias else "resume"}
            for alias in ["","synth","leader","synthesizer"]]


def validate_public_completion_result(result: dict[str, Any], exchanges: list[dict[str, Any]], case: dict[str, Any]) -> None:
    alias=case.get("alias")
    if not isinstance(alias,str) or alias not in {"","synth","leader","synthesizer"}:
        raise ValueError("Unrecognized public completion selection")
    synthesize=bool(alias)
    path="rerun" if alias else "resume"
    if case.get("synthesize") is not synthesize or case.get("path")!=path:
        raise ValueError("Public completion scenario changed its independently selected request")
    base,recovery=result.get("baseExecution"),result.get("recoveryPreparation")
    if not isinstance(base,dict) or not isinstance(recovery,dict):
        raise ValueError("Public completion omitted the original durable execution")
    validate_driver_result(base,exchanges[:4])
    count=int(synthesize)
    if result.get("status")!="PASS" or result.get("runStatus")!="done" or len(exchanges)!=4+count or \
            result.get("nextIndex")!=11+2*count or result.get("publicEventCount")!=11+2*count or result.get("durablePhases")!=3+count or \
            result.get("sourceCommit")!=base.get("sourceCommit") or result.get("runId")!=base.get("runId") or \
            result.get("publicCompletionRecoveryQualified") is not True or result.get("productionProviderQualified") is not False or \
            result.get("fullMigrationQualified") is not False or result.get("productionRuntimeProcesses")!=["Go","Rust"]:
        raise ValueError("Public completion omitted exact durable/provider boundaries or overclaimed qualification")
    if recovery.get("kind")!="public-completion" or recovery.get("path")!=path or recovery.get("agentId")!=alias or \
            recovery.get("synthesize") is not synthesize or recovery.get("literalFalseSentForAlias") is not synthesize or \
            recovery.get("generation")!=1 or recovery.get("originalRuntimeRevision")!=1 or recovery.get("runtimeRevision")!=2 or \
            recovery.get("priorEffectCount")!=4 or recovery.get("retainedOutputCount")!=2 or \
            recovery.get("newProviderCalls")!=count or recovery.get("newDurablePhases")!=count or recovery.get("newTaskPhases")!=0 or \
            recovery.get("goOpenedRustBody") is not False or recovery.get("privatePreparationRetryQualified") is not False or \
            recovery.get("fullResumeRerunQualified") is not False:
        raise ValueError("Public completion rebound selection, source custody or retained phase semantics")
    for flag in ["preparedByRust","admittedThroughRustHTTP","commitRetryIdempotent","originalEffectsUnchanged",
                 "originalPublicEventsUnchanged","originalTaskOutputsUnchanged","publicReadQualified"]:
        if recovery.get(flag) is not True:
            raise ValueError("Public completion lacks actual proof: "+flag)
    for value in [result.get("requestSha256"),recovery.get("contextSha256"),recovery.get("contextArtifactSha256")]:
        if not isinstance(value,str) or len(value)!=64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Public completion omitted exact immutable body bindings")
    if recovery.get("originalRequestSha256")!=base.get("requestSha256") or result["requestSha256"]==base.get("requestSha256") or \
            type(recovery.get("contextArtifactLength")) is not int or recovery["contextArtifactLength"]<=0 or \
            any(row.get("requestCredentialUsed") is not True for row in exchanges):
        raise ValueError("Public completion omitted actual inherited Rust credentials or a fresh runtime request")
    refusals=recovery.get("publicRefusals")
    if synthesize:
        expected={"invalid-json":400,"missing-role":400,"unknown-role":400,"private-input":400,
                  "duplicate-rerun-while-active":409,"resume-while-active":409,"missing-run":404}
        if recovery.get("actualRecoveryPostRequests")!=8 or not isinstance(refusals,list) or len(refusals)!=7 or \
                {r.get("case"):r.get("status") for r in refusals if isinstance(r,dict)}!=expected or \
                any(not isinstance(r,dict) or r.get("runUnchanged") is not True or type(r.get("status")) is not int or
                    r.get("path")!=("resume" if r.get("case")=="resume-while-active" else "rerun") for r in refusals):
            raise ValueError("Public synthesis alias omitted actual POST refusal and no-mutation evidence")
        last=exchanges[4]
        if not last.get("actionId") or last.get("actionId")!=recovery.get("newSynthesisActionId") or \
                last["actionId"] in {r["actionId"] for r in exchanges[:4]} or not last.get("operationId") or \
                last["operationId"] in {r["operationId"] for r in exchanges[:4]} or last.get("executionEpoch")!="1" or last.get("stream") is not False or \
                last.get("synthesisCoderInPrompt") is not True or last.get("synthesisCriticInPrompt") is not True or \
                last.get("privateReasoningInPrompt") is not False:
            raise ValueError("Public alias synthesis lacks a distinct effect and the original public task summaries")
    elif recovery.get("actualRecoveryPostRequests")!=1 or refusals not in (None,[]) or recovery.get("newSynthesisActionId")!="":
        raise ValueError("Completed public resume admitted a new provider/task effect")


def task_rerun_cases() -> list[dict[str, Any]]:
    return [{"name":role+("-synthesize" if synthesize else "-keep-final"),"role":role,"synthesize":synthesize}
            for role in ["critic","researcher"] for synthesize in [True,False]]


def validate_task_recovery_result(result: dict[str, Any], exchanges: list[dict[str, Any]], case: dict[str, Any]) -> None:
    base,recovery=result.get("baseExecution"),result.get("recoveryPreparation")
    role,synthesize=case.get("role"),case.get("synthesize")
    if role not in {"critic","researcher"} or type(synthesize) is not bool or not isinstance(base,dict) or not isinstance(recovery,dict):
        raise ValueError("Task rerun omitted its independently selected scenario")
    validate_driver_result(base,exchanges[:4])
    calls,events=1+int(synthesize),14+2*int(synthesize)
    if len(exchanges)!=4+calls or result.get("status")!="PASS" or result.get("runStatus")!="done" or \
            result.get("nextIndex")!=events or result.get("publicEventCount")!=events or result.get("durablePhases")!=3+calls or \
            result.get("sourceCommit")!=base.get("sourceCommit") or result.get("runId")!=base.get("runId") or \
            result.get("taskRerunProgressQualified") is not True or result.get("productionProviderQualified") is not False or \
            result.get("fullMigrationQualified") is not False or result.get("productionRuntimeProcesses")!=["Go","Rust"]:
        raise ValueError("Task rerun omitted public/terminal settlement or admitted extra effects")
    if recovery.get("kind")!="task-rerun" or recovery.get("role")!=role or recovery.get("synthesize") is not synthesize or \
            recovery.get("generation")!=1 or recovery.get("originalRuntimeRevision")!=1 or recovery.get("runtimeRevision")!=2 or \
            recovery.get("priorEffectCount")!=4 or recovery.get("retainedOutputCount")!=(1 if role=="critic" else 2) or \
            recovery.get("newProviderCalls")!=calls or recovery.get("newDurablePhases")!=calls or \
            recovery.get("goOpenedRustBody") is not False or recovery.get("fullResumeRerunQualified") is not False or \
            recovery.get("progressMetadataSchemaVersion")!=4 or recovery.get("originalRequestSha256")!=base.get("requestSha256") or \
            result.get("requestSha256")==base.get("requestSha256"):
        raise ValueError("Task rerun changed its source, selection, progress schema or original request")
    for flag in ["preparedByRust","commitRetryIdempotent","originalEffectsUnchanged",
                 "originalPublicEventsUnchanged","publicReadQualified","oldFinalRetainedDuringTask","unfinishedProgressDenied","staleProgressDenied"]:
        if recovery.get(flag) is not True:
            raise ValueError("Task rerun lacks actual proof: "+flag)
    if case.get("publicAdmission") is True:
        expected_refusals={"invalid-json":400,"missing-role":400,"unknown-role":400,"private-input":400,
                           "duplicate-rerun-while-active":409,"resume-while-active":409,"missing-run":404}
        refusals=recovery.get("publicRefusals")
        if recovery.get("publicAdmissionQualified") is not True or recovery.get("preparationRetryIdempotent") is not False or \
                recovery.get("actualPublicRecoveryHTTPRequests")!=8 or \
                not isinstance(refusals,list) or len(refusals)!=len(expected_refusals):
            raise ValueError("Task rerun lacks actual public HTTP admission and refusals")
        if {r.get("case"):r.get("status") for r in refusals if isinstance(r,dict)}!=expected_refusals or \
                any(not isinstance(r,dict) or r.get("runUnchanged") is not True or type(r.get("status")) is not int or
                    r.get("path")!=("resume" if r.get("case")=="resume-while-active" else "rerun") for r in refusals):
            raise ValueError("Public recovery refusal changed status or mutated the paused run")
    elif recovery.get("preparationRetryIdempotent") is not True:
        raise ValueError("Private recovery omitted its pre-commit immutable preparation retry")
    for value in [result.get("requestSha256"),recovery.get("contextSha256"),recovery.get("contextArtifactSha256"),
                  recovery.get("noteBodySha256"),recovery.get("nextBodySha256"),recovery.get("progressReceiptSha256")]:
        if not isinstance(value,str) or len(value)!=64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Task rerun omitted exact immutable artifact bindings")
    encoded=recovery.get("progressReceiptBase64")
    if not isinstance(encoded,str) or len(encoded)>2*1024**2:
        raise ValueError("Task rerun omitted its bounded actual Rust receipt")
    try:
        receipt=base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4))
    except ValueError as error:
        raise ValueError("Malformed progress receipt encoding") from error
    if not receipt or base64.urlsafe_b64encode(receipt).decode().rstrip("=")!=encoded or hashlib.sha256(receipt).hexdigest()!=recovery.get("progressReceiptSha256"):
        raise ValueError("Task rerun receipt bytes differ from its actual stored signature")
    old_actions={item.get("actionId") for item in exchanges[:4]}
    old_operations={item.get("operationId") for item in exchanges[:4]}
    task=exchanges[4]
    if not task.get("actionId") or task["actionId"]!=recovery.get("newTaskActionId") or task["actionId"] in old_actions or \
            not task.get("operationId") or task["operationId"] in old_operations or task.get("executionEpoch")!="1" or task.get("stream") is not False or \
            task.get("predecessorSummaryInPrompt") is not True or task.get("rerunSelectedRoleInPrompt") is not True or \
            task.get("requestCredentialUsed") is not True or task.get("privateReasoningInPrompt") is not False:
        raise ValueError("Rerun task lacks original summaries, inherited Rust credentials or a fresh fenced effect")
    if synthesize:
        final=exchanges[5]
        if not final.get("actionId") or final["actionId"]!=recovery.get("newSynthesisActionId") or final["actionId"] in old_actions|{task["actionId"]} or \
                not final.get("operationId") or final["operationId"] in old_operations|{task["operationId"]} or final.get("executionEpoch")!="1" or \
                final.get("stream") is not False or final.get("synthesisCoderInPrompt") is not True or final.get("synthesisCriticInPrompt") is not True or \
                final.get("recoveredTaskSummaryInPrompt") is not True or final.get("requestCredentialUsed") is not True or final.get("privateReasoningInPrompt") is not False:
            raise ValueError("Rerun synthesis lacks the latest selected output or its own admitted effect")
    elif recovery.get("newSynthesisActionId")!="":
        raise ValueError("Rerun without synthesis invented a synthesis action")


def wait_edge(origin: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Native public edge exited before listening")
        try:
            with urlopen(origin + "/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise TimeoutError("Native public edge did not become reachable")


def preset_cases() -> list[dict[str, Any]]:
    fixture = json.loads((ROOT / "rust/crates/deepseek-policy/tests/fixtures/agent_preset_plan.json").read_text(encoding="utf-8"))
    rows = fixture["cases"]
    choices = [("full", "inspect"), ("code", "inspect"), ("research", "inspect"), ("reason", "inspect"), ("critic", "inspect"),
               ("auto", "BUG in class C"), ("auto", "最新资料"), ("auto", "复杂权衡"), ("auto", "字" * 501),
               ("auto", "代码和今天新闻"), ("leader", "inspect")]
    result = []
    for index, (preset, query) in enumerate(choices):
        row = next(value for value in rows if value["preset"] == preset and value["payload"]["messages"][0]["content"] == query)
        upstream = row["expected"] is None
        plan = [{"id":"coder","task":"write"},{"id":"critic","task":"review","depends_on":["coder"]}] if upstream else row["expected"]["agents"]
        result.append({"name":f"{index:02d}-{preset}", "preset":preset, "query":query, "plan":plan,
                       "upstreamPlanner":upstream, "confirmation":preset == "auto", "explicitConfirmation":False})
    for suffix, query, explicit in [("long", "字" * 1801, False), ("explicit", "inspect", True)]:
        result.append({"name":"full-" + suffix,"preset":"full","query":query,"plan":result[0]["plan"],
                       "upstreamPlanner":False,"confirmation":True,"explicitConfirmation":explicit})
    return result


def validate_preset_result(result: dict[str, Any], exchanges: list[dict[str, Any]], case: dict[str, Any]) -> None:
    count, confirm, planner = len(case["plan"]), case["confirmation"], case["upstreamPlanner"]
    event_count, requests = 6 + count + int(confirm), count + 1 + int(planner)
    expected_source = "upstream-provider" if planner else "retained-preset-policy"
    actual_plan = [{"id":node.get("id"),"task":node.get("task"),"depends_on":node.get("depends_on", [])} for node in result.get("actualPlan", [])]
    expected_plan = [{"id":node["id"],"task":node["task"],"depends_on":node.get("depends_on", [])} for node in case["plan"]]
    if result.get("status") != "PASS" or result.get("runStatus") != "done" or result.get("durablePhases") != 3 or \
            result.get("nextIndex") != event_count or result.get("publicEventCount") != event_count or actual_plan != expected_plan or \
            result.get("actualPreset") != case["preset"] or result.get("actualPlanningSource") != expected_source or \
            result.get("planConfirmationRequired") is not confirm or result.get("publicPlanConfirmationQualified") is not confirm or \
            result.get("noTasksBeforePublicConfirmation") is not confirm or result.get("resourcesReleased") is not True or \
            result.get("finalAnswerVisible") is not True or result.get("inputBodySeededByOfflineOracle") is not False or \
            result.get("requestBodyStoredByRust") is not True or result.get("productionRuntimeProcesses") != ["Go", "Rust"] or \
            result.get("productionProviderQualified") is not False or result.get("fullMigrationQualified") is not False or len(exchanges) != requests:
        raise ValueError("Preset process evidence differs from retained behavior or overclaims qualification")
    actions, operations = [row.get("actionId") for row in exchanges], [row.get("operationId") for row in exchanges]
    task_actions = actions[int(planner):-1]
    if any(not isinstance(value, str) or not value for value in actions + operations) or len(set(operations)) != requests or \
            len(set(task_actions)) != 1 or len(set(actions)) != 2 + int(planner) or \
            any(row.get("executionEpoch") != "1" or row.get("stream") is not False or row.get("privateReasoningInPrompt") for row in exchanges):
        raise ValueError("Preset provider calls lack distinct nodes, shared task phase or privacy fences")
    if any(not exchanges[index + int(planner)].get("predecessorSummaryInPrompt") for index, node in enumerate(case["plan"]) if node.get("depends_on")):
        raise ValueError("Preset dependency summaries are absent from actual provider prompts")


def cancellation_cases() -> list[dict[str, Any]]:
    static = dict(preset_cases()[-1], name="full-awaiting-plan")
    dynamic = {"name":"auto-awaiting-plan", "preset":"auto", "query":"Prepare an isolated verification plan",
               "plan":[{"id":"coder","task":"write"},{"id":"critic","task":"review","depends_on":["coder"]}],
               "upstreamPlanner":True,"confirmation":True,"explicitConfirmation":False}
    return [dynamic, static]


def validate_cancellation_result(result: dict[str, Any], exchanges: list[dict[str, Any]], case: dict[str, Any]) -> None:
    before, after = result.get("beforeCancelIndex"), result.get("nextIndex")
    actual = [{"id":n.get("id"),"task":n.get("task"),"depends_on":n.get("depends_on",[])} for n in result.get("actualPlan",[])]
    expected = [{"id":n["id"],"task":n["task"],"depends_on":n.get("depends_on",[])} for n in case["plan"]]
    if result.get("status") != "PASS" or result.get("runStatus") != "cancelled" or type(before) is not int or before < 1 or \
            type(after) is not int or after != before + 1 or result.get("publicEventCount") != after or result.get("publicStreamEventCount") != after or \
            result.get("durablePhases") != 1 or actual != expected or result.get("actualPreset") != case["preset"] or \
            result.get("actualPlanningSource") != ("upstream-provider" if case["upstreamPlanner"] else "retained-preset-policy") or \
            any(result.get(key) is not True for key in ["resourcesReleased","noTasksAfterCancellation","repeatCancellationIdempotent", \
                "confirmationAfterCancellationDenied","planConfirmationRequired","requestBodyStoredByRust","publicCancellationWhileAwaitingPlanQualified"]) or \
            any(result.get(key) is not False for key in ["inputBodySeededByOfflineOracle","productionProviderQualified","fullMigrationQualified","activeTaskCancellationQualified"]) or \
            result.get("productionRuntimeProcesses") != ["Go","Rust"] or len(exchanges) != int(case["upstreamPlanner"]):
        raise ValueError("Public cancellation evidence is incomplete or overclaims qualification")
    if exchanges and any(not exchanges[0].get(key) for key in ["actionId","operationId","executionEpoch"]):
        raise ValueError("Actual planner HTTP exchange omitted its durable fence")


def active_cancellation_case() -> dict[str, Any]:
    return {"name":"leader-in-flight-task","preset":"leader","query":"Prepare an isolated verification plan",
            "plan":[{"id":"coder","task":"write"},{"id":"critic","task":"review","depends_on":["coder"]}],
            "upstreamPlanner":True,"confirmation":False,"explicitConfirmation":False}


def active_planning_cancellation_case() -> dict[str, Any]:
    row = next(case for case in preset_cases() if case["preset"] == "auto" and case["upstreamPlanner"])
    return dict(row, name="auto-in-flight-plan", cancelPhase="plan")


def validate_active_cancellation_result(result: dict[str, Any], exchanges: list[dict[str, Any]], case: dict[str, Any]) -> None:
    before,after=result.get("beforeCancelIndex"),result.get("nextIndex")
    planning = case.get("cancelPhase") == "plan"
    phases, requests = (1, 1) if planning else (2, 2)
    plan_matches = result.get("actualPlan") in (None, []) if planning else result.get("actualPlan") == case["plan"]
    custody_field = "actualLateEffectCustodyConfirmed" if planning else "actualLateTaskCustodyConfirmed"
    if result.get("status")!="PASS" or result.get("runStatus")!="cancelled" or type(before) is not int or type(after) is not int or \
            before<1 or after!=before+1 or result.get("durablePhases")!=phases or result.get("publicStreamEventCount")!=after or result.get("actualPreset")!=case["preset"] or \
            not plan_matches or result.get("productionRuntimeProcesses")!=["Go","Rust"] or len(exchanges)!=requests or \
            any(result.get(k) is not True for k in ["resourcesRetainedBeforeRecovery","resourcesReleased","controllerRestartRecoveryQualified","cancelledPhaseRecoveryQualified","cancelledDuringActualProviderRequest",custody_field, \
                "noDependentOrSynthesisAdmission","repeatCancellationIdempotent","activeCancellationSafetyQualified","requestBodyStoredByRust"]) or \
            any(result.get(k) is not False for k in ["resourcesRetained","inputBodySeededByOfflineOracle", \
                "productionProviderQualified","fullMigrationQualified"]):
        raise ValueError("Active cancellation evidence lacks authoritative recovery")
    if planning and (result.get("noTaskAdmission") is not True or result.get("cancelledPhase") != "plan"):
        raise ValueError("Planning cancellation admitted work or omitted its original phase")
    original,recovered=result.get("originalExecutionEpoch"),result.get("recoveryExecutionEpoch")
    if type(original) is not int or type(recovered) is not int or original<1 or recovered!=original+1:
        raise ValueError("Cancelled recovery did not preserve the original fence and take over once")
    if (not planning and exchanges[0].get("actionId")==exchanges[1].get("actionId")) or \
            any(not item.get(k) for item in exchanges for k in ["actionId","operationId","executionEpoch"]):
        raise ValueError("Active planner/task HTTP effects lack distinct durable admission")


def run(args: argparse.Namespace) -> dict[str, Any]:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Native public compilation and containers are cloud-only on this host")
    preset_case = getattr(args, "preset_case", None)
    cancel_after_plan = bool(getattr(args, "cancel_after_plan", False))
    cancel_active = bool(getattr(args, "cancel_active", False))
    recovery_preparation = bool(getattr(args, "recovery_preparation", False))
    recovery_driving = bool(getattr(args, "recovery_driving", False))
    task_recovery_case = getattr(args, "task_recovery_case", None)
    public_completion_case = getattr(args, "public_completion_case", None)
    public_recovery = bool(getattr(args, "public_recovery_rerun_matrix", False) or getattr(args, "public_recovery_completion_matrix", False))
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if source_commit != os.environ.get("GITHUB_SHA"):
        raise ValueError("Cloud checkout is not the workflow's exact source revision")
    with tempfile.TemporaryDirectory(prefix="native-public-agent-") as temporary:
        scratch = Path(temporary)
        environment = signing.cache_environment(ROOT, env=signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        go_temporary = scratch / "go-tmp"
        go_temporary.mkdir()
        environment.update(GOCACHE=args.go_cache.resolve().as_posix(), GOTMPDIR=go_temporary.as_posix(),
            GOTOOLCHAIN="local", GOMAXPROCS="2", CGO_ENABLED="0", GOPROXY="off", GOSUMDB="off", GOWORK="off")
        version = subprocess.check_output(["go", "version"], env=environment, text=True).strip()
        if not version.startswith(f"go version {signing.GO_VERSION} "):
            raise ValueError("Native Go toolchain pin differs")
        fixtures = [ROOT / "tests/native/agent_signing_process.go.txt", ROOT / "tests/native/agent_public_driver_process.go.txt",
                    ROOT / "tests/native/agent_recovery_preparation_process.go.txt",
                    ROOT / "tests/native/agent_task_recovery_process.go.txt",
                    ROOT / "tests/native/agent_public_recovery_process.go.txt",
                    ROOT / "tests/native/agent_public_completion_process.go.txt"]
        overlay = scratch / "overlay.json"
        overlay.write_text(json.dumps({"Replace": {
            (ROOT / "go/internal/api/zz_native_agent_signing_process_test.go").as_posix(): fixtures[0].as_posix(),
            (ROOT / "go/internal/api/zz_native_agent_public_driver_process_test.go").as_posix(): fixtures[1].as_posix(),
            (ROOT / "go/internal/api/zz_native_agent_recovery_preparation_process_test.go").as_posix(): fixtures[2].as_posix(),
            (ROOT / "go/internal/api/zz_native_agent_task_recovery_process_test.go").as_posix(): fixtures[3].as_posix(),
            (ROOT / "go/internal/api/zz_native_agent_public_recovery_process_test.go").as_posix(): fixtures[4].as_posix(),
            (ROOT / "go/internal/api/zz_native_agent_public_completion_process_test.go").as_posix(): fixtures[5].as_posix()}}), encoding="utf-8")
        binary = scratch / "native-public-agent-api-tests"
        signing.record_command(["go", "test", "-c", "-p=2", "-overlay", overlay.as_posix(), "-o", binary.as_posix(), "./internal/api"],
            ROOT / "go", environment, output, "go-public-driver-build", timeout=600)
        custody = scratch / "custody"
        custody.mkdir(mode=0o700)
        password, bundle = custody / "passphrase", custody / "bundle.json"
        password.write_bytes(secrets.token_urlsafe(48).encode())
        password.chmod(0o600)
        private_environment = signing.child_environment(dict(os.environ))
        private_environment.update(DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE=bundle.as_posix(),
            DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE=password.as_posix(),
            DEEPSEEK_WORKER_AUTHORITY_FLEET_ID="fleet-a", DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT="production")
        provisioned = subprocess.run([args.provisioner.resolve().as_posix()], cwd=ROOT, env=private_environment,
            capture_output=True, timeout=60, check=False)
        if provisioned.returncode:
            raise RuntimeError("Native public custody provisioning failed")
        binding = signing.validate_public_binding(json.loads(provisioned.stdout))
        ca, certificate, key = signing.create_tls(custody)
        case = scratch / "case"
        case.mkdir()
        artifacts = case / "rust-artifacts"
        artifacts.mkdir(mode=0o700)
        state = case / "rust-state"
        state.mkdir()
        exchanges: list[dict[str, Any]] = []
        plan = {"summary": "Controlled provider plan", "tasks": [{"role": "coder", "task": "write", "dependsOn": []},
            {"role": "critic", "task": "review", "dependsOn": ["coder"]}]}

        class ProviderFixture(BaseHTTPRequestHandler):
            def log_message(self, *values: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024**2:
                    self.send_error(400)
                    return
                body = json.loads(self.rfile.read(length))
                prompt = json.dumps(body, ensure_ascii=False)
                exchanges.append({"actionId": self.headers.get("X-DeepSeek-Action-Id"),
                    "executionEpoch": self.headers.get("X-DeepSeek-Execution-Epoch"),
                    "operationId": self.headers.get("X-DeepSeek-Node-Operation-Id"), "model": body.get("model"),
                    "stream": body.get("stream"), "selectedTaskInPrompt": "Agent 子任务：write" in prompt,
                    "dependentTaskInPrompt": "Agent 子任务：review" in prompt,
                    "predecessorSummaryInPrompt": "Controlled public task summary" in prompt,
                    "privateReasoningInPrompt": "private-task-reasoning" in prompt,
                    "synthesisCoderInPrompt": "任务：write" in prompt, "synthesisCriticInPrompt": "任务：review" in prompt,
                    "recoveredTaskSummaryInPrompt":"Controlled rerun task summary" in prompt,
                    "requestCredentialUsed":self.headers.get("Authorization")=="Bearer request-only-credential",
                    "rerunSelectedRoleInPrompt":task_recovery_case is not None and
                        ("你负责挑错、找漏洞" if task_recovery_case["role"]=="critic" else "你负责事实、资料") in prompt})
                position = len(exchanges)
                if public_completion_case is not None and public_completion_case["synthesize"] and position==5:
                    signing.write_ready(case/"public-synthesis-arrived.json",{"position":position})
                    deadline=time.monotonic()+60
                    while not (case/"public-synthesis-release.json").exists():
                        if time.monotonic()>deadline:
                            raise RuntimeError("Public synthesis did not release the controlled provider")
                        time.sleep(0.05)
                if task_recovery_case is not None and position==5:
                    signing.write_ready(case/"rerun-task-arrived.json",{"position":position})
                    deadline=time.monotonic()+(60 if public_recovery else 30)
                    while not (case/"rerun-task-release.json").exists():
                        if time.monotonic()>deadline:
                            raise RuntimeError("Task rerun did not release the controlled provider")
                        time.sleep(0.05)
                planning_cancel = cancel_active and preset_case is not None and preset_case.get("cancelPhase") == "plan"
                if cancel_active and position == (1 if planning_cancel else 2):
                    signing.write_ready(case / "provider-task-arrived.json", {"nodeId":"leader" if planning_cancel else "coder"})
                    deadline = time.monotonic() + 30
                    while not (case / "provider-release.json").exists():
                        if time.monotonic() > deadline:
                            raise RuntimeError("Actual public cancellation did not release the controlled provider")
                        time.sleep(0.05)
                upstream = preset_case is None or preset_case["upstreamPlanner"]
                final_position = 4 if preset_case is None else len(preset_case["plan"]) + 1 + int(upstream)
                content = json.dumps({"agents":plan["tasks"]}) if upstream and position == 1 else ("Controlled final answer" if position == final_position else "## 摘要\nControlled public task summary")
                if recovery_driving and position == 5:
                    content = "Controlled recovered final answer"
                if task_recovery_case is not None and position>=5:
                    content="## 摘要\nControlled rerun task summary" if position==5 else "Controlled recovered final answer"
                reply = json.dumps({"id": f"public-agent-{position}", "model": "deepseek-v4-pro",
                    "choices": [{"message": {"role": "assistant", "content": content, "reasoning_content": "private-task-reasoning"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderFixture)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        producer = worker = edge = None
        try:
            bearer = secrets.token_urlsafe(48)
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            environment.update(DEEPSEEK_AGENT_PUBLIC_DRIVER_PROBE="1", DEEPSEEK_AGENT_SIGNING_PROBE_DIR=case.as_posix(),
                DEEPSEEK_AGENT_SIGNING_WORKER_BEARER=bearer, DEEPSEEK_TEST_HEAD_SHA=source_commit,
                DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=binding["signerPublicKey"])
            if recovery_preparation:
                environment["DEEPSEEK_TEST_AGENT_RECOVERY_PREPARATION"] = "1"
            if recovery_driving:
                environment["DEEPSEEK_TEST_AGENT_RECOVERY_DRIVING"] = "1"
            if task_recovery_case is not None:
                environment["DEEPSEEK_TEST_AGENT_RECOVERY_ROLE"]=task_recovery_case["role"]
                environment["DEEPSEEK_TEST_AGENT_RECOVERY_SYNTHESIZE"]=str(task_recovery_case["synthesize"]).lower()
            if public_recovery:
                environment["DEEPSEEK_TEST_AGENT_PUBLIC_RECOVERY"]="1"
            if public_completion_case is not None:
                if public_completion_case["synthesize"]:
                    environment["DEEPSEEK_TEST_AGENT_PUBLIC_SYNTHESIS_ALIAS"]=public_completion_case["alias"]
                else:
                    environment["DEEPSEEK_TEST_AGENT_PUBLIC_COMPLETED_RESUME"]="1"
            if cancel_after_plan:
                environment["DEEPSEEK_TEST_AGENT_CANCEL_AFTER_PLAN"] = "1"
            if cancel_active:
                environment["DEEPSEEK_TEST_AGENT_CANCEL_ACTIVE"] = "1"
                if preset_case is not None and preset_case.get("cancelPhase") == "plan":
                    environment["DEEPSEEK_TEST_AGENT_CANCEL_ACTIVE_PLAN"] = "1"
            if signing.PRIVATE_ENV & set(environment) or "DEEPSEEK_API_KEY" in environment:
                raise ValueError("Private custody configuration entered Go")
            if preset_case is not None:
                environment.update(DEEPSEEK_TEST_AGENT_PRESET=preset_case["preset"], DEEPSEEK_TEST_AGENT_QUERY=preset_case["query"],
                    DEEPSEEK_TEST_AGENT_PLAN=json.dumps(preset_case["plan"], ensure_ascii=False),
                    DEEPSEEK_TEST_AGENT_CONFIRMATION=str(preset_case["confirmation"]).lower(),
                    DEEPSEEK_TEST_AGENT_EXPLICIT_CONFIRMATION=str(preset_case["explicitConfirmation"]).lower())
            with (output / "public-driver-go.log").open("wb") as log:
                producer = subprocess.Popen([binary.as_posix(), "-test.run=^TestNativeAgentPublicDriverProcess$", "-test.v", "-test.timeout=4m"],
                    cwd=ROOT / "go/internal/api", env=environment, stdout=log, stderr=subprocess.STDOUT)
            ready = signing.wait_json(case / "go-ready.json", producer)
            worker_environment = dict(private_environment, DEEPSEEK_WORKER_TLS_CERT_FILE=certificate.as_posix(),
                DEEPSEEK_WORKER_TLS_KEY_FILE=key.as_posix(), DEEPSEEK_WORKER_SERVICE_BEARER=bearer,
                DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT=expiry, DEEPSEEK_WORKER_SERVICE_NAME="go-control-plane",
                DEEPSEEK_WORKER_SERVICE_ROLE="controller", DEEPSEEK_WORKER_STATE_ROOT=state.as_posix(),
                DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=binding["signerPublicKey"],
                DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN=str(ready["fencingToken"]), GO_CONTROL_ADDR=ready["origin"],
                DEEPSEEK_INTERNAL_BEARER=ready["internalBearer"], DEEPSEEK_AGENT_ARTIFACT_DIR=artifacts.as_posix(),
                DEEPSEEK_INFRA_ROOT=case.as_posix(), DEEPSEEK_AGENT_DATA_MODE="python_disabled",
                DEEPSEEK_API_URL=f"http://127.0.0.1:{provider.server_port}/chat", DEEPSEEK_API_KEY="isolated-fixture-credential")
            worker, target = signing.start_worker(args.worker.resolve(), worker_environment, ca, output / "public-driver-rust.log")
            signing.write_ready(case / "worker-ready.json", {"Target": target, "TrustRootFile": ca.as_posix(),
                "PublicKey": binding["signerPublicKey"], "ExpiresAt": expiry})
            static = case / "static/ui"
            static.mkdir(parents=True)
            (static / "index.html").write_text("<!doctype html><title>Isolated native API fixture</title>", encoding="utf-8")
            with socket.socket() as port:
                port.bind(("127.0.0.1", 0))
                address = f"127.0.0.1:{port.getsockname()[1]}"
            edge_environment = public_edge_environment(worker_environment, address, static, artifacts, ready,
                credential_worker_transport(target, ca, bearer))
            with (output / "public-driver-edge.log").open("wb") as log:
                edge = subprocess.Popen([args.edge.resolve().as_posix()], cwd=ROOT, env=edge_environment, stdout=log, stderr=subprocess.STDOUT)
            wait_edge("http://" + address, edge)
            if (artifacts / "objects-v1.sqlite").exists():
                with sqlite3.connect((artifacts / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as db:
                    if db.execute("SELECT count(*) FROM artifacts").fetchone()[0] != 0:
                        raise ValueError("Input custody was seeded before actual public creation")
            signing.write_ready(case / "public-ready.json", {"origin": "http://" + address})
            result = signing.wait_json(case / "result.json", producer, timeout=360 if cancel_active else 180)
            if producer.wait(timeout=15) != 0:
                raise RuntimeError("Actual native public driver process failed")
            if public_completion_case is not None:
                validate_public_completion_result(result,exchanges,public_completion_case)
            elif task_recovery_case is not None:
                validate_task_recovery_result(result,exchanges,task_recovery_case)
            elif recovery_driving:
                validate_recovery_execution_result(result, exchanges)
            elif recovery_preparation:
                validate_recovery_preparation_result(result, exchanges)
            elif preset_case is None:
                validate_driver_result(result, exchanges)
            if result.get("sourceCommit") != source_commit:
                raise ValueError("Go process source revision differs")
            with sqlite3.connect((artifacts / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as db:
                row = db.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (result["requestSha256"],)).fetchone()
                if not row or row[0] != result["requestLength"] or hashlib.sha256(row[1]).hexdigest() != result["requestSha256"]:
                    raise ValueError("Actual Rust request custody digest differs")
                request = json.loads(row[1])
                if request["payload"].get("apiKey") is not None or "request-only-credential" in row[1].decode():
                    raise ValueError("Actual public request persisted a credential")
                if recovery_preparation or recovery_driving:
                    recovery = result["recoveryPreparation"]
                    context_row = db.execute("SELECT length,bytes FROM artifacts WHERE sha256=?",
                                             (recovery["contextArtifactSha256"],)).fetchone()
                    envelope = request.get("_nativeRecoveryContext")
                    if not context_row or context_row[0] != recovery["contextArtifactLength"] or \
                            hashlib.sha256(context_row[1]).hexdigest() != recovery["contextArtifactSha256"] or \
                            json.loads(context_row[1]) != envelope or not isinstance(envelope, dict) or \
                            set(envelope) != {"schema", "metadataProto"} or envelope["schema"] != "native-agent-recovery-context-v1":
                        raise ValueError("Prepared recovery context lacks independently retained Rust custody")
                    encoded = envelope["metadataProto"]
                    context_proto = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                    if base64.urlsafe_b64encode(context_proto).decode().rstrip("=") != encoded or \
                            hashlib.sha256(context_proto).hexdigest() != recovery["contextSha256"]:
                        raise ValueError("Prepared recovery protobuf digest differs from live Go preview")
                    result["recoveryContextArtifactVerified"] = True
            with sqlite3.connect((state / "rust-worker/authority.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
                rows = db.execute("SELECT operation_id,state,output,output_sha256 FROM agent_node_effects").fetchall()
                expected_nodes = (1 if preset_case is not None and preset_case.get("cancelPhase") == "plan" else 2) if cancel_active else (1 if cancel_after_plan else (4 if preset_case is None else len(preset_case["plan"]) + 2))
                if recovery_driving:
                    expected_nodes = 5
                if task_recovery_case is not None:
                    expected_nodes=5+int(task_recovery_case["synthesize"])
                if len(rows) != expected_nodes or any(row[1] != "CONFIRMED" or hashlib.sha256(row[2]).hexdigest() != row[3] for row in rows):
                    raise ValueError("Rust actual four-node custody is missing or corrupt")
                leader = next((json.loads(row[2]) for row in rows if json.loads(row[2]).get("publicEvent", {}).get("type") == "agent_plan"), None)
                if leader is None:
                    raise ValueError("Rust retained planning outcome is missing")
                result["actualPlanningSource"] = "retained-preset-policy" if leader.get("planningSource") == "retained-preset-policy" else "upstream-provider"
            if task_recovery_case is not None:
                recovery=result["recoveryPreparation"]
                encoded=recovery["progressReceiptBase64"]
                receipt=base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4))
                with sqlite3.connect((state/"rust-worker/control-signatures.sqlite3").as_uri()+"?mode=ro",uri=True) as signatures:
                    if signatures.execute("PRAGMA integrity_check").fetchone()[0]!="ok" or signatures.execute("SELECT count(*) FROM signatures WHERE document=?",(receipt,)).fetchone()[0]!=1:
                        raise ValueError("Go progress does not match a unique actual Rust signer journal receipt")
                with sqlite3.connect((artifacts/"objects-v1.sqlite").as_uri()+"?mode=ro",uri=True) as objects:
                    bodies=[]
                    for name in ["noteBodySha256","nextBodySha256"]:
                        row=objects.execute("SELECT length,bytes FROM artifacts WHERE sha256=?",(recovery[name],)).fetchone()
                        if not row or row[0]!=len(row[1]) or hashlib.sha256(row[1]).hexdigest()!=recovery[name]:
                            raise ValueError("Actual Rust progress body custody is missing or corrupt")
                        bodies.append(json.loads(row[1]))
                expected_note={"type":"agent_note","phase":task_recovery_case["role"],
                    "name":"反驳审查 Agent" if task_recovery_case["role"]=="critic" else "资料检索 Agent",
                    "text":"其他 Agent 未自动重跑；最终回答将基于最新该 Agent 与现有其他 Agent 输出重新综合。"}
                expected_next={"type":"final_reset","scope":"final_answer","reason":"rerun_agent"} if task_recovery_case["synthesize"] else {"type":"run_status","status":"done"}
                if bodies!=[expected_note,expected_next]:
                    raise ValueError("Actual progress bodies changed retained public behavior")
                result["progressArtifactAndSignerCustodyVerified"]=True
            result.update(actualHTTPRequests=len(exchanges), providerExchanges=exchanges,
                requestBodyStoredByRust=True, bootstrapUIIsOfflineFixture=True, cloudOnlyNativeBuild=True,
                privateCustodyVariablesExportedToGo=False, readiness="NOT_READY")
            if cancel_active:
                assert preset_case is not None
                validate_active_cancellation_result(result, exchanges, preset_case)
            elif cancel_after_plan:
                assert preset_case is not None
                validate_cancellation_result(result, exchanges, preset_case)
            elif preset_case is not None:
                validate_preset_result(result, exchanges, preset_case)
            (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            for process in [producer, edge, worker]:
                if process is not None:
                    signing.stop_child(process)
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["worker", "edge", "provisioner", "go-cache", "out"]:
        parser.add_argument("--" + name, type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preset-matrix", action="store_true")
    mode.add_argument("--cancellation-matrix", action="store_true")
    mode.add_argument("--active-cancellation-matrix", action="store_true")
    mode.add_argument("--recovery-preparation", action="store_true")
    mode.add_argument("--recovery-driving", action="store_true")
    mode.add_argument("--recovery-rerun-matrix", action="store_true")
    mode.add_argument("--public-recovery-rerun-matrix", action="store_true")
    mode.add_argument("--public-recovery-completion-matrix", action="store_true")
    args = parser.parse_args()
    if args.public_recovery_completion_matrix:
        output=args.out.resolve()
        results=[]
        for case in public_completion_cases():
            child=argparse.Namespace(**vars(args))
            child.out=output/case["name"]
            child.public_completion_case=case
            child.recovery_driving=case["synthesize"]
            child.recovery_preparation=not case["synthesize"]
            report=run(child)
            if report.get("recoveryContextArtifactVerified") is not True:
                raise ValueError("Public completion omitted actual Rust context custody")
            results.append({"name":case["name"],"alias":case["alias"],"synthesize":case["synthesize"],"status":report["status"],
                "sourceCommit":report["sourceCommit"],"actualProviderHTTPRequests":report["actualHTTPRequests"],
                "actualRecoveryPostRequests":report["recoveryPreparation"]["actualRecoveryPostRequests"],"nextIndex":report["nextIndex"]})
            print(json.dumps(results[-1]),flush=True)
        result={"status":"PASS","sourceCommit":results[0]["sourceCommit"],"caseCount":len(results),"cases":results,
            "allInputsCreatedThroughRustHTTP":True,"publicCompletionRecoveryQualified":True,
            "actualRecoveryPostRequests":sum(r["actualRecoveryPostRequests"] for r in results),
            "actualProviderHTTPRequests":sum(r["actualProviderHTTPRequests"] for r in results),
            "runtimePython":False,"realProviderQualified":False,"fullMigrationQualified":False,"readiness":"NOT_READY"}
        output.mkdir(parents=True,exist_ok=True)
        (output/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    elif args.recovery_rerun_matrix or args.public_recovery_rerun_matrix:
        output=args.out.resolve()
        results=[]
        for case in task_rerun_cases():
            if args.public_recovery_rerun_matrix:
                case=dict(case,publicAdmission=True)
            child=argparse.Namespace(**vars(args))
            child.out=output/case["name"]
            child.task_recovery_case=case
            child.recovery_driving=True
            report=run(child)
            if report.get("progressArtifactAndSignerCustodyVerified") is not True:
                raise ValueError("Task recovery lacks actual Rust custody")
            results.append({"name":case["name"],"role":case["role"],"synthesize":case["synthesize"],"status":report["status"],"sourceCommit":report["sourceCommit"],
                "actualHTTPRequests":report["actualHTTPRequests"],"nextIndex":report["nextIndex"],"progressArtifactAndSignerCustodyVerified":True,
                "publicAdmissionQualified":report["recoveryPreparation"].get("publicAdmissionQualified",False),
                "actualPublicRecoveryHTTPRequests":report["recoveryPreparation"].get("actualPublicRecoveryHTTPRequests",0)})
            print(json.dumps(results[-1]),flush=True)
        result={"status":"PASS","sourceCommit":results[0]["sourceCommit"],"caseCount":len(results),"cases":results,
            "allInputsCreatedThroughRustHTTP":True,"taskRerunProgressQualified":True,
            "publicRecoveryAdmissionQualified":args.public_recovery_rerun_matrix,
            "actualPublicRecoveryHTTPRequests":sum(r["actualPublicRecoveryHTTPRequests"] for r in results),
            "actualProviderHTTPRequests":sum(r["actualHTTPRequests"] for r in results),
            "runtimePython":False,"realProviderQualified":False,"fullMigrationQualified":False,"readiness":"NOT_READY"}
        output.mkdir(parents=True,exist_ok=True)
        (output/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    elif args.preset_matrix or args.cancellation_matrix or args.active_cancellation_matrix:
        output = args.out.resolve()
        cases = [active_cancellation_case(), active_planning_cancellation_case()] if args.active_cancellation_matrix else (cancellation_cases() if args.cancellation_matrix else preset_cases())
        results = []
        for case in cases:
            child = argparse.Namespace(**vars(args))
            child.out, child.preset_case = output / case["name"], case
            child.cancel_after_plan = args.cancellation_matrix
            child.cancel_active = args.active_cancellation_matrix
            report = run(child)
            results.append({"name":case["name"],"preset":case["preset"],"status":report["status"],"sourceCommit":report["sourceCommit"],
                            "actualHTTPRequests":report["actualHTTPRequests"],"nextIndex":report["nextIndex"],"actualPlanningSource":report["actualPlanningSource"]})
            print(json.dumps(results[-1]), flush=True)
        result = {"status":"PASS","sourceCommit":results[0]["sourceCommit"],"caseCount":len(results),"cases":results,
                  "allInputsCreatedThroughRustHTTP":True,"cancellationWhileAwaitingPlanQualified":args.cancellation_matrix,"activeTaskCancellationQualified":False,"activePlanningCancellationQualified":args.active_cancellation_matrix,"activeCancellationSafetyQualified":args.active_cancellation_matrix,"cancelledPhaseRecoveryQualified":args.active_cancellation_matrix,"runtimePython":False,"realProviderQualified":False,"fullMigrationQualified":False,"readiness":"NOT_READY"}
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        result = run(args)
        print(json.dumps({key: result[key] for key in ["status", "sourceCommit", "actualHTTPRequests", "runStatus", "nextIndex", "readiness"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
