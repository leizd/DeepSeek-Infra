"""Read-only, exact-scope evidence for a real React Agent recovery click."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

RECOVERED_FINAL = "Controlled recovered browser final answer"
RERUN_SUMMARY = "Controlled browser rerun task summary"


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _actions(control: Path) -> dict[str, str]:
    with sqlite3.connect((control / "control.sqlite3").as_uri()+"?mode=ro", uri=True) as database:
        database.row_factory=sqlite3.Row
        rows=database.execute("SELECT * FROM action_journal ORDER BY id").fetchall()
    return {row["id"]:_digest(dict(row)) for row in rows}


def _effects(state: Path) -> dict[str, str]:
    with sqlite3.connect((state / "rust-worker/authority.sqlite3").as_uri()+"?mode=ro", uri=True) as database:
        rows=database.execute("SELECT operation_id,state,output,output_sha256 FROM agent_node_effects").fetchall()
    result={}
    for operation,status,body,digest in rows:
        if status!="CONFIRMED" or hashlib.sha256(body).hexdigest()!=digest or operation in result:
            raise ValueError("Actual browser effect custody is unresolved, duplicated or corrupt")
        result[operation]=_digest({"state":status,"sha256":digest})
    return result


def capture_before_recovery(control: Path, state: Path, public: dict[str, Any]) -> dict[str, Any]:
    """Observe already-completed application state; never create a fixture row."""
    actions,effects=_actions(control),_effects(state)
    events=public.get("events")
    if public.get("status")!="done" or public.get("nextIndex")!=10 or public.get("finalAnswer")!="Controlled final answer" or \
            not isinstance(events,list) or len(events)!=10 or not isinstance(public.get("agentOutputs"),dict) or len(actions)!=3 or len(effects)!=4:
        observed={"status":public.get("status"),"nextIndex":public.get("nextIndex"),
            "eventCount":len(events) if isinstance(events,list) else None,
            "agentOutputsType":type(public.get("agentOutputs")).__name__,"actions":len(actions),"effects":len(effects)}
        raise ValueError("Browser recovery baseline differs: "+json.dumps(observed,sort_keys=True))
    return {"actions":actions,"effects":effects,"events":deepcopy(events),"agentOutputs":deepcopy(public.get("agentOutputs")),
            "publicEventPrefixSha256":_digest(events)}


def inspect_inflight_public(during: dict[str, Any], before: dict[str, Any]) -> dict[str, bool]:
    """Use the same actual public output contract at every recovery boundary."""
    current=during.get("agentOutputs")
    original=before.get("agentOutputs")
    coder_retained=isinstance(current,dict) and isinstance(original,dict) and "coder" in original and current.get("coder")==original["coder"]
    final_retained=during.get("finalAnswer")=="Controlled final answer"
    if during.get("status")!="running" or during.get("nextIndex")!=12 or not final_retained or not coder_retained:
        observed={"status":during.get("status"),"nextIndex":during.get("nextIndex"),
            "agentOutputsType":type(current).__name__,"oldFinalRetained":final_retained,"coderRetained":coder_retained}
        raise ValueError("Actual browser in-flight recovery differs: "+json.dumps(observed,sort_keys=True))
    return {"oldFinalRetained":True,"coderRetained":True}


def inspect_after_recovery(control: Path, state: Path, public: dict[str, Any], before: dict[str, Any],
                           inspected: dict[str, Any]) -> dict[str, bool]:
    actions,effects=_actions(control),_effects(state)
    if len(actions)!=5 or len(effects)!=6 or any(actions.get(key)!=value for key,value in before["actions"].items()) or \
            any(effects.get(key)!=value for key,value in before["effects"].items()):
        raise ValueError("Actual React recovery repeated or changed an original Go action or Rust effect")
    events=public.get("events")
    if public.get("status")!="done" or public.get("nextIndex")!=17 or public.get("finalAnswer")!=RECOVERED_FINAL or \
            not isinstance(events,list) or len(events)!=17 or events[:10]!=before["events"] or \
            _digest(events[:10])!=before["publicEventPrefixSha256"]:
        raise ValueError("Actual React recovery changed public history or failed to persist its new final")
    outputs=public.get("agentOutputs")
    if not isinstance(outputs,dict) or not isinstance(before.get("agentOutputs"),dict) or \
            outputs.get("coder")!=before["agentOutputs"].get("coder") or RERUN_SUMMARY not in str(outputs.get("critic")):
        raise ValueError("Actual React recovery lost the retained coder or newest critic output")
    raw=inspected.get("browserRecoveryReceiptBase64")
    if not isinstance(raw,str) or len(raw)>2*1024**2:
        raise ValueError("Actual browser recovery omitted its bounded signed progress")
    try:
        document=base64.urlsafe_b64decode(raw+"="*(-len(raw)%4))
    except ValueError as error:
        raise ValueError("Actual browser recovery receipt is malformed") from error
    if not document or base64.urlsafe_b64encode(document).decode().rstrip("=")!=raw or \
            hashlib.sha256(document).hexdigest()!=inspected.get("browserRecoveryReceiptSha256"):
        raise ValueError("Actual browser recovery receipt digest differs")
    with sqlite3.connect((state / "rust-worker/control-signatures.sqlite3").as_uri()+"?mode=ro", uri=True) as database:
        if database.execute("PRAGMA integrity_check").fetchone()[0]!="ok" or \
                database.execute("SELECT count(*) FROM signatures WHERE document=?",(document,)).fetchone()[0]!=1:
            raise ValueError("Actual browser progress does not match a unique Rust custody signature")
    return {"originalBrowserActionsAndEffectsUnchanged":True,"originalBrowserHistoryUnchanged":True,
            "browserRetainedCoderAndLatestCriticVerified":True,"browserProgressRustJournalVerified":True}


def validate_browser_recovery_result(result: dict[str, Any], exchanges: list[dict[str, Any]]) -> None:
    required=["actualDaemonCLIQualified","independentlyValidatedByGoStore","resourcesReleased","builtFrontendAssetsServed",
              "rustEffectsIndependentlyRetained","requestCredentialEncrypted","cloudOnlyNativeBuild",
              "independentNativeBackupPolicyVerified","independentMirrorCiphertextVerified",
              "independentBrowserRecoveryProgressVerified","originalBrowserActionsAndEffectsUnchanged",
              "originalBrowserHistoryUnchanged","browserRetainedCoderAndLatestCriticVerified","browserProgressRustJournalVerified"]
    denied=["inputBodySeededByOfflineOracle","privateCustodyVariablesExportedToGo","productionProviderQualified","fullMigrationQualified"]
    if any(result.get(k) is not True for k in required) or any(result.get(k) is not False for k in denied) or \
            result.get("status")!="PASS" or result.get("runStatus")!="done" or result.get("nextIndex")!=17 or \
            result.get("runtimeRevision")!=3 or result.get("metadataSchemaVersion")!=4 or result.get("recoveryGeneration")!=1 or \
            result.get("durablePhases")!=5 or result.get("actualPreset")!="auto" or \
            result.get("pythonMutatingPublicRequests")!=0 or result.get("productionRuntimeProcesses")!=["Go","Rust"] or \
            result.get("readiness")!="NOT_READY" or not re.fullmatch(r"[a-f0-9]{40}",str(result.get("sourceCommit",""))) or \
            not re.fullmatch(r"[a-f0-9]{64}",str(result.get("frontendIndexSha256",""))) or \
            type(result.get("frontendAssetCount")) is not int or result["frontendAssetCount"]<1:
        raise ValueError("Actual browser recovery omitted native authority, original custody or exact runtime/phase scope")
    if result.get("actualPlan")!=[{"id":"coder","task":"write through browser"},
                                  {"id":"critic","task":"review","depends_on":["coder"]}]:
        raise ValueError("Actual browser recovery changed the edited dependency plan")
    observed=result.get("browser")
    if not isinstance(observed,dict) or observed.get("createdRunId")!=result.get("runId") or \
            not observed.get("createdConversationId") or observed.get("mirroredConversationId")!=observed["createdConversationId"] or \
            observed.get("conversationTitle")!="Native browser conversat" or \
            any(observed.get(k) is not True for k in ["creationByBrowser","finalAnswerVisible","reloadRetainedFinalAnswer",
                "titleRetainedAfterReload","planConfirmationByBrowser","planEditedByBrowser","noTasksBeforePlanConfirmation",
                "backupPolicyConfiguredByGoFixture","finalEnvelopeMirrored","rerunByBrowser","oldFinalVisibleDuringRerun",
                "recoveredFinalVisible","rerunSelectionAndPayloadVerified"]) or \
            any(observed.get(k) is not False for k in ["backupPolicyCreatedByBrowser","requestInterceptionUsed","apiInvokedOutsideUI"]) or \
            observed.get("pageErrors")!=[] or observed.get("consoleErrors")!=[] or observed.get("screenshot")!="browser-recovery.png" or \
            not re.fullmatch(r"[a-f0-9]{64}",str(observed.get("screenshotSha256",""))):
        raise ValueError("Actual React recovery click, live output or reload was substituted or incomplete")
    traffic=observed.get("requests")
    run_id=result.get("runId")
    if not isinstance(traffic,list) or any(not isinstance(row,dict) for row in traffic):
        raise ValueError("Actual React recovery omitted its observed API traffic")
    for path in ["/api/agent-runs",f"/api/agent-runs/{run_id}/plan",f"/api/agent-runs/{run_id}/rerun","/api/title"]:
        matching=[r for r in traffic if r.get("path")==path and r.get("method")=="POST"]
        if len(matching)!=1 or matching[0].get("status") not in {200,201}:
            raise ValueError("Actual React recovery duplicated or omitted an exact mutating UI command")
    agents=[row for row in exchanges if row.get("kind","agent")=="agent"]
    titles=[row for row in exchanges if row.get("kind")=="title"]
    if len(agents)!=6 or len(titles)!=1 or len(exchanges)!=7 or len({r.get("operationId") for r in agents})!=6 or \
            len({r.get("actionId") for r in agents})!=5 or agents[1].get("actionId")!=agents[2].get("actionId") or \
            any(r.get("requestCredentialUsed") is not True or r.get("credentialInProviderBody") is not False for r in exchanges) or \
            any(not r.get("actionId") or not r.get("operationId") or not str(r.get("executionEpoch","")).isdigit() or
                int(r["executionEpoch"])<=0 or r.get("stream") is not False for r in agents):
        raise ValueError("Actual browser recovery lacks exactly six independently fenced Agent provider effects")
    for index,key in [(1,"browserEditedTaskInPrompt"),(2,"predecessorSummaryInPrompt"),(3,"browserEditedTaskInPrompt"),
                      (3,"predecessorSummaryInPrompt"),(4,"predecessorSummaryInPrompt"),(5,"browserEditedTaskInPrompt"),
                      (5,"recoveredCriticSummaryInPrompt")]:
        if agents[index].get(key) is not True:
            raise ValueError("Actual browser recovery lost retained or latest visible summaries")
    if any(r.get("privateReasoningInPrompt") is not False for r in agents) or \
            agents[4].get("actionId")!=result.get("browserRecoveryTaskActionId") or \
            agents[5].get("actionId")!=result.get("browserRecoverySynthesisActionId") or \
            agents[4]["actionId"] in {r["actionId"] for r in agents[:4]} or agents[5]["actionId"] in {r["actionId"] for r in agents[:5]}:
        raise ValueError("Actual browser recovery replayed a previous effect or exposed private reasoning")
