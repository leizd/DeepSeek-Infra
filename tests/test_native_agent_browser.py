from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import sqlite3
from typing import Any

import pytest

from scripts import verify_native_agent_browser as browser


def browser_fixture() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result: dict[str, Any] = {
        "status": "PASS", "sourceCommit": "a" * 40, "runId": "browser-run",
        "runStatus": "done", "nextIndex": 10, "actualPreset": "auto", "durablePhases": 3,
        "actualPlan": [{"id": "coder", "task": browser.BROWSER_TASK}, {"id": "critic", "task": "review", "depends_on": ["coder"]}],
        "runtimeRevision": 2, "metadataSchemaVersion": 2,
        "actualDaemonCLIQualified": True, "independentlyValidatedByGoStore": True,
        "resourcesReleased": True, "builtFrontendAssetsServed": True,
        "frontendIndexSha256": "b" * 64, "frontendAssetCount": 3,
        "rustEffectsIndependentlyRetained": True, "requestCredentialEncrypted": True,
        "independentNativeBackupPolicyVerified": True, "independentMirrorCiphertextVerified": True,
        "privateCustodyVariablesExportedToGo": False, "cloudOnlyNativeBuild": True,
        "inputBodySeededByOfflineOracle": False, "pythonMutatingPublicRequests": 0,
        "productionRuntimeProcesses": ["Go", "Rust"], "productionProviderQualified": False,
        "fullMigrationQualified": False, "readiness": "NOT_READY",
        "browser": {
            "createdRunId": "browser-run", "creationByBrowser": True,
            "createdConversationId": "conversation-1", "mirroredConversationId": "conversation-1",
            "conversationTitle": browser.BROWSER_TITLE, "titleRetainedAfterReload": True,
            "finalAnswerVisible": True, "reloadRetainedFinalAnswer": True,
            "planConfirmationByBrowser": True, "noTasksBeforePlanConfirmation": True,
            "planEditedByBrowser": True,
            "backupPolicyCreatedByBrowser": False, "backupPolicyConfiguredByGoFixture": True, "finalEnvelopeMirrored": True,
            "requestInterceptionUsed": False, "apiInvokedOutsideUI": False,
            "pageErrors": [], "consoleErrors": [],
            "screenshot": "browser-final.png", "screenshotSha256": "c" * 64,
            "requests": [
                {"method": "POST", "path": "/api/agent-runs", "status": 200},
                {"method": "GET", "path": "/api/agent-runs/browser-run/stream", "status": 200},
                {"method": "POST", "path": "/api/title", "status": 200},
                {"method": "POST", "path": "/api/agent-runs/browser-run/plan", "status": 200},
                {"method": "PUT", "path": "/api/workspace/backup-mirrors/mirror_123/frontend", "status": 200},
            ],
        },
    }
    exchanges = [dict(kind="agent", requestCredentialUsed=True, credentialInProviderBody=False,
                      actionId=f"phase-{i}", executionEpoch="1", operationId=f"node-{i}",
                      browserEditedTaskInPrompt=i in (1, 3), predecessorSummaryInPrompt=i in (2, 3),
                      privateReasoningInPrompt=False) for i in range(4)]
    exchanges.append(dict(kind="title", requestCredentialUsed=True, credentialInProviderBody=False))
    return result, exchanges


def final_mirror_payload() -> dict[str, Any]:
    conversation = {"id": "conversation-1", "title": "Native browser conversat", "autoTitleDone": True,
        "messages": [{"role": "user", "content": browser.BROWSER_QUERY},
                     {"role": "assistant", "content": "Controlled final answer", "agentRunId": "browser-run", "agentRunStatus": "done"}]}
    return {"sourceEpoch": "legacy", "envelope": {"schemaVersion": 1, "conversations": [
        {"conversationId": "conversation-1", "headRevision": "revision-1", "checkpoint": {
            "schemaVersion": 3, "conversationId": "conversation-1", "revision": "revision-1", "conversation": conversation}}]}}


def browser_recovery_fixture() -> tuple[dict[str,Any],list[dict[str,Any]]]:
    result,exchanges=browser_fixture()
    result.update(nextIndex=17,runtimeRevision=3,metadataSchemaVersion=4,durablePhases=5,recoveryGeneration=1,
        independentBrowserRecoveryProgressVerified=True,originalBrowserActionsAndEffectsUnchanged=True,
        originalBrowserHistoryUnchanged=True,browserRetainedCoderAndLatestCriticVerified=True,browserProgressRustJournalVerified=True,
        browserRecoveryTaskActionId="rerun-task",browserRecoverySynthesisActionId="rerun-synthesis")
    result["browser"].update(rerunByBrowser=True,oldFinalVisibleDuringRerun=True,recoveredFinalVisible=True,
        generatedTitleRetainedDuringRecovery=True,
        rerunSelectionAndPayloadVerified=True,screenshot="browser-recovery.png")
    result["browser"]["requests"].append({"method":"POST","path":"/api/agent-runs/browser-run/rerun","status":200})
    exchanges[2]["actionId"]=exchanges[1]["actionId"]
    for name in ["task","synthesis"]:
        exchanges.append(dict(kind="agent",requestCredentialUsed=True,credentialInProviderBody=False,
            actionId="rerun-"+name,operationId="node-rerun-"+name,executionEpoch="1",stream=False,
            browserEditedTaskInPrompt=name=="synthesis",predecessorSummaryInPrompt=True,
            recoveredCriticSummaryInPrompt=name=="synthesis",privateReasoningInPrompt=False))
    for row in exchanges:
        if row.get("kind")=="agent":
            row["stream"]=False
    return result,exchanges


def test_actual_browser_recovery_retains_exact_ui_and_native_proof_boundaries() -> None:
    from scripts.verify_native_agent_browser_recovery import validate_browser_recovery_result

    validate_browser_recovery_result(*browser_recovery_fixture())


@pytest.mark.parametrize("title_proof", [False, None])
def test_actual_browser_recovery_requires_generated_title_survival(title_proof: bool | None) -> None:
    from scripts.verify_native_agent_browser_recovery import validate_browser_recovery_result

    result, exchanges = browser_recovery_fixture()
    if title_proof is None:
        result["browser"].pop("generatedTitleRetainedDuringRecovery")
    else:
        result["browser"]["generatedTitleRetainedDuringRecovery"] = title_proof
    with pytest.raises(ValueError):
        validate_browser_recovery_result(result, exchanges)


@pytest.mark.parametrize("change",["source","cursor","runtime","schema","phases","generation","signature","old-actions",
    "old-history","latest-critic","journal","rerun-ui","old-final","reload","selection","page-error","console-error",
    "interception","outside-ui","duplicate-rerun","missing-rerun","wrong-rerun-status","extra-provider","credential",
    "old-action","old-operation","new-summary","private-reasoning","overclaim"])
def test_actual_browser_recovery_rejects_unproven_ui_or_repeated_provider_effects(change: str) -> None:
    from scripts.verify_native_agent_browser_recovery import validate_browser_recovery_result

    result,exchanges=browser_recovery_fixture()
    observed=result["browser"]
    if change=="source":
        result["sourceCommit"]="bad"
    elif change in {"cursor","runtime","schema","phases","generation"}:
        key={"cursor":"nextIndex","runtime":"runtimeRevision","schema":"metadataSchemaVersion",
             "phases":"durablePhases","generation":"recoveryGeneration"}[change]
        result[key]+=1
    elif change in {"signature","old-actions","old-history","latest-critic","journal"}:
        key={"signature":"independentBrowserRecoveryProgressVerified","old-actions":"originalBrowserActionsAndEffectsUnchanged",
            "old-history":"originalBrowserHistoryUnchanged","latest-critic":"browserRetainedCoderAndLatestCriticVerified",
            "journal":"browserProgressRustJournalVerified"}[change]
        result[key]=False
    elif change in {"rerun-ui","old-final","reload","selection","interception","outside-ui"}:
        key={"rerun-ui":"rerunByBrowser","old-final":"oldFinalVisibleDuringRerun","reload":"reloadRetainedFinalAnswer",
            "selection":"rerunSelectionAndPayloadVerified","interception":"requestInterceptionUsed","outside-ui":"apiInvokedOutsideUI"}[change]
        observed[key]=change in {"interception","outside-ui"}
    elif change in {"page-error","console-error"}:
        observed["pageErrors" if change=="page-error" else "consoleErrors"].append("unexpected error")
    elif change=="duplicate-rerun":
        observed["requests"].append(deepcopy(observed["requests"][-1]))
    elif change=="missing-rerun":
        observed["requests"].pop()
    elif change=="wrong-rerun-status":
        observed["requests"][-1]["status"]=409
    elif change=="extra-provider":
        exchanges.append(deepcopy(exchanges[-1]))
    elif change=="credential":
        exchanges[-1]["requestCredentialUsed"]=False
    elif change=="old-action":
        exchanges[-1]["actionId"]=exchanges[0]["actionId"]
    elif change=="old-operation":
        exchanges[-1]["operationId"]=exchanges[0]["operationId"]
    elif change=="new-summary":
        exchanges[-1]["recoveredCriticSummaryInPrompt"]=False
    elif change=="private-reasoning":
        exchanges[-1]["privateReasoningInPrompt"]=True
    else:
        result["fullMigrationQualified"]=True
    with pytest.raises(ValueError):
        validate_browser_recovery_result(result,exchanges)


def test_final_mirror_keeps_the_same_conversation_and_new_recovery_answer() -> None:
    from scripts.verify_native_agent_browser_recovery import RECOVERED_FINAL

    payload=final_mirror_payload()
    message=payload["envelope"]["conversations"][0]["checkpoint"]["conversation"]["messages"][1]
    message["content"]=RECOVERED_FINAL
    assert not browser.is_final_browser_mirror(payload,"browser-run","conversation-1")
    assert browser.is_final_browser_mirror(payload,"browser-run","conversation-1",RECOVERED_FINAL)


@pytest.mark.parametrize("fault",[None,"old-action","old-effect","history","coder","critic","missing-journal","duplicate-journal","receipt-hash"])
def test_browser_recovery_inspection_reads_immutable_rows_and_actual_receipt_journal(tmp_path: Path, fault: str | None) -> None:
    import base64
    from scripts import verify_native_agent_browser_recovery as recovery

    control,state=tmp_path/"control",tmp_path/"state"
    control.mkdir()
    (state/"rust-worker").mkdir(parents=True)
    with sqlite3.connect(control/"control.sqlite3") as database:
        database.execute("CREATE TABLE action_journal(id TEXT PRIMARY KEY,state TEXT,payload_json TEXT)")
        for i in range(3):
            database.execute("INSERT INTO action_journal VALUES(?,?,?)",(f"old-{i}","SUCCEEDED",'{}'))
    with sqlite3.connect(state/"rust-worker/authority.sqlite3") as database:
        database.execute("CREATE TABLE agent_node_effects(operation_id TEXT PRIMARY KEY,state TEXT,output BLOB,output_sha256 TEXT)")
        for i in range(4):
            body=f'{{"public":{i}}}'.encode()
            database.execute("INSERT INTO agent_node_effects VALUES(?,?,?,?)",(f"old-{i}","CONFIRMED",body,hashlib.sha256(body).hexdigest()))
    public: dict[str,Any]={"status":"done","nextIndex":10,"finalAnswer":"Controlled final answer","events":[{"index":i} for i in range(10)],
            "agentOutputs":{"coder":"retained coder","critic":"original critic"}}
    before=recovery.capture_before_recovery(control,state,public)
    with sqlite3.connect(control/"control.sqlite3") as database:
        for key in ["new-task","new-synthesis"]:
            database.execute("INSERT INTO action_journal VALUES(?,?,?)",(key,"SUCCEEDED",'{}'))
        if fault=="old-action":
            database.execute("UPDATE action_journal SET payload_json='{} changed' WHERE id='old-0'")
    with sqlite3.connect(state/"rust-worker/authority.sqlite3") as database:
        for key in ["new-task","new-synthesis"]:
            body=b'{"public":"fresh"}'
            database.execute("INSERT INTO agent_node_effects VALUES(?,?,?,?)",(key,"CONFIRMED",body,hashlib.sha256(body).hexdigest()))
        if fault=="old-effect":
            body=b'{"public":"changed"}'
            database.execute("UPDATE agent_node_effects SET output=?,output_sha256=? WHERE operation_id='old-0'",(body,hashlib.sha256(body).hexdigest()))
    document=b"offline-shape-only; actual signature is independently required from Go"
    with sqlite3.connect(state/"rust-worker/control-signatures.sqlite3") as database:
        database.execute("CREATE TABLE signatures(document BLOB)")
        if fault!="missing-journal":
            database.execute("INSERT INTO signatures VALUES(?)",(document,))
        if fault=="duplicate-journal":
            database.execute("INSERT INTO signatures VALUES(?)",(document,))
    final=deepcopy(public)
    final.update(nextIndex=17,finalAnswer=recovery.RECOVERED_FINAL,events=public["events"]+[{"index":i} for i in range(10,17)])
    final["agentOutputs"]["critic"]=recovery.RERUN_SUMMARY
    if fault=="history":
        final["events"][0]["index"]=-1
    elif fault=="coder":
        final["agentOutputs"]["coder"]="changed"
    elif fault=="critic":
        final["agentOutputs"]["critic"]="original critic"
    inspected={"browserRecoveryReceiptBase64":base64.urlsafe_b64encode(document).decode().rstrip("="),
               "browserRecoveryReceiptSha256":hashlib.sha256(document).hexdigest() if fault!="receipt-hash" else "e"*64}
    if fault is None:
        assert all(recovery.inspect_after_recovery(control,state,final,before,inspected).values())
    else:
        with pytest.raises(ValueError):
            recovery.inspect_after_recovery(control,state,final,before,inspected)


def test_native_browser_title_matches_the_existing_title_normalization_contract() -> None:
    from deepseek_infra.infra.gateway.title_generator import _sanitize_title

    assert _sanitize_title("Native browser conversation") == browser.BROWSER_TITLE == "Native browser conversat"


def test_final_mirror_matches_the_actual_normalized_title_and_same_native_run() -> None:
    assert browser.is_final_browser_mirror(final_mirror_payload(), "browser-run", "conversation-1")


@pytest.mark.parametrize("change", ["raw_title", "unrelated_title", "other_run", "other_conversation", "unfinished",
    "waiting_hint", "missing_answer", "missing_query", "wrong_schema", "wrong_checkpoint", "wrong_revision", "oversized"])
def test_final_mirror_rejects_coincidental_text_and_incomplete_or_foreign_conversations(change: str) -> None:
    payload = final_mirror_payload()
    row = payload["envelope"]["conversations"][0]
    conversation = row["checkpoint"]["conversation"]
    if change == "raw_title":
        conversation["title"] = "Native browser conversation"
    elif change == "unrelated_title":
        conversation["title"] = "Another conversation"
        payload["unrelated"] = "Native browser conversat"
    elif change == "other_run":
        conversation["messages"][1]["agentRunId"] = "another-run"
    elif change == "other_conversation":
        conversation["id"] = "another-conversation"
    elif change == "unfinished":
        conversation["messages"][1]["agentRunStatus"] = "running"
    elif change == "waiting_hint":
        conversation["messages"][1]["content"] = "Waiting for confirmation. Controlled final answer"
    elif change == "missing_answer":
        conversation["messages"].pop()
        payload["unrelated"] = "Controlled final answer"
    elif change == "missing_query":
        conversation["messages"].pop(0)
    elif change == "wrong_schema":
        payload["envelope"]["schemaVersion"] = 2
    elif change == "wrong_checkpoint":
        row["checkpoint"]["conversationId"] = "another-conversation"
    elif change == "wrong_revision":
        row["checkpoint"]["revision"] = "another-revision"
    else:
        payload["unrelated"] = "x" * (2 * 1024**2)
    assert not browser.is_final_browser_mirror(payload, "browser-run", "conversation-1")


@pytest.mark.parametrize("payload", [None, [], {}, {"envelope": []}, {"envelope": {"schemaVersion": 1, "conversations": [None]}}])
def test_final_mirror_rejects_malformed_envelopes(payload: Any) -> None:
    assert not browser.is_final_browser_mirror(payload, "browser-run", "conversation-1")


def test_accepts_completed_native_browser_run_with_independent_go_inspection() -> None:
    result, exchanges = browser_fixture()
    browser.validate_browser_result(result, exchanges)


def test_auto_browser_flow_retains_explicit_plan_confirmation() -> None:
    result, exchanges = browser_fixture()
    browser.validate_browser_result(result, exchanges)


@pytest.mark.parametrize("change", ["missing_plan", "unedited_task", "lost_dependency", "extra_task"])
def test_browser_confirmation_must_retain_the_operator_edit_and_dependency(change: str) -> None:
    result, exchanges = browser_fixture()
    if change == "missing_plan":
        result.pop("actualPlan")
    elif change == "unedited_task":
        result["actualPlan"][0]["task"] = "write"
    elif change == "lost_dependency":
        result["actualPlan"][1].pop("depends_on")
    else:
        result["actualPlan"].append({"id": "extra", "task": "unexpected"})
    with pytest.raises(ValueError, match="plan edit or dependent task"):
        browser.validate_browser_result(result, exchanges)


@pytest.mark.parametrize("change", ["no_confirmation", "failed_confirmation", "no_policy_configuration", "claimed_browser_policy", "no_mirror", "failed_mirror", "no_browser_confirmation", "early_tasks", "no_native_policy", "no_ciphertext_proof", "wrong_event_count", "unexpected_policy_request"])
def test_rejects_missing_confirmation_or_native_backup_evidence(change: str) -> None:
    result, exchanges = browser_fixture()
    requests = result["browser"]["requests"]
    index = {"no_confirmation": 3, "failed_confirmation": 3, "no_mirror": 4, "failed_mirror": 4}.get(change)
    if index is not None:
        if change.startswith("no_"):
            requests.pop(index)
        else:
            requests[index]["status"] = 503
    elif change == "no_policy_configuration":
        result["browser"]["backupPolicyConfiguredByGoFixture"] = False
    elif change == "claimed_browser_policy":
        result["browser"]["backupPolicyCreatedByBrowser"] = True
    elif change == "unexpected_policy_request":
        requests.append({"method": "POST", "path": "/api/workspace/backup-policies", "status": 200})
    elif change == "no_browser_confirmation":
        result["browser"]["planConfirmationByBrowser"] = False
    elif change == "early_tasks":
        result["browser"]["noTasksBeforePlanConfirmation"] = False
    elif change == "no_native_policy":
        result["independentNativeBackupPolicyVerified"] = False
    elif change == "no_ciphertext_proof":
        result["independentMirrorCiphertextVerified"] = False
    else:
        result["nextIndex"] = 9
    with pytest.raises(ValueError):
        browser.validate_browser_result(result, exchanges)


@pytest.mark.parametrize("key", [
    "actualDaemonCLIQualified", "independentlyValidatedByGoStore", "resourcesReleased",
    "builtFrontendAssetsServed", "rustEffectsIndependentlyRetained", "requestCredentialEncrypted",
    "cloudOnlyNativeBuild",
    "independentNativeBackupPolicyVerified", "independentMirrorCiphertextVerified",
])
def test_rejects_missing_native_browser_chain_evidence(key: str) -> None:
    result, exchanges = browser_fixture()
    result[key] = False
    with pytest.raises(ValueError):
        browser.validate_browser_result(result, exchanges)


@pytest.mark.parametrize("key,value", [
    ("sourceCommit", "unknown"), ("runStatus", "running"), ("nextIndex", 7),
    ("actualPreset", "full"), ("durablePhases", 2), ("pythonMutatingPublicRequests", 1),
    ("runtimeRevision", 1), ("metadataSchemaVersion", 1),
    ("productionRuntimeProcesses", ["Python", "Go", "Rust"]), ("inputBodySeededByOfflineOracle", True),
    ("privateCustodyVariablesExportedToGo", True), ("productionProviderQualified", True),
    ("fullMigrationQualified", True), ("readiness", "READY"),
])
def test_rejects_incomplete_or_overclaimed_browser_qualification(key: str, value: Any) -> None:
    result, exchanges = browser_fixture()
    result[key] = value
    with pytest.raises(ValueError):
        browser.validate_browser_result(result, exchanges)


@pytest.mark.parametrize("key,value", [
    ("createdRunId", "another-run"), ("creationByBrowser", False), ("finalAnswerVisible", False),
    ("createdConversationId", ""), ("mirroredConversationId", "another-conversation"),
    ("conversationTitle", "Native browser conversation"), ("titleRetainedAfterReload", False),
    ("planEditedByBrowser", False),
    ("reloadRetainedFinalAnswer", False), ("requestInterceptionUsed", True),
    ("apiInvokedOutsideUI", True), ("pageErrors", ["unhandled exception"]),
    ("consoleErrors", ["failed request"]), ("screenshotSha256", "missing"),
])
def test_rejects_browser_substitution_errors_and_missing_render(key: str, value: Any) -> None:
    result, exchanges = browser_fixture()
    result["browser"][key] = value
    with pytest.raises(ValueError):
        browser.validate_browser_result(result, exchanges)


@pytest.mark.parametrize("change", ["creation_missing", "creation_failed", "stream_missing", "stream_failed", "duplicate_creation", "title_missing", "title_failed"])
def test_requires_one_successful_ui_creation_and_the_actual_run_stream(change: str) -> None:
    result, exchanges = browser_fixture()
    requests = result["browser"]["requests"]
    if change == "creation_missing":
        requests.pop(0)
    elif change == "creation_failed":
        requests[0]["status"] = 403
    elif change == "stream_missing":
        requests.pop(1)
    elif change == "stream_failed":
        requests[1]["status"] = 500
    elif change == "duplicate_creation":
        requests.append(deepcopy(requests[0]))
    elif change == "title_missing":
        requests.pop(2)
    elif change == "title_failed":
        requests[2]["status"] = 500
    with pytest.raises(ValueError):
        browser.validate_browser_result(result, exchanges)


@pytest.mark.parametrize("change", ["missing", "wrong_credential", "credential_body", "unfenced", "duplicate_operation", "edit_not_executed", "edit_not_synthesized"])
def test_retains_fenced_provider_and_private_credential_proof(change: str) -> None:
    result, exchanges = browser_fixture()
    if change == "missing":
        exchanges.pop()
    elif change == "wrong_credential":
        exchanges[1]["requestCredentialUsed"] = False
    elif change == "credential_body":
        exchanges[1]["credentialInProviderBody"] = True
    elif change == "unfenced":
        exchanges[1]["executionEpoch"] = "0"
    elif change == "edit_not_executed":
        exchanges[1]["browserEditedTaskInPrompt"] = False
    elif change == "edit_not_synthesized":
        exchanges[3]["browserEditedTaskInPrompt"] = False
    else:
        exchanges[1]["operationId"] = exchanges[0]["operationId"]
    with pytest.raises(ValueError):
        browser.validate_browser_result(result, exchanges)


@pytest.mark.parametrize("origin", ["https://127.0.0.1:8888", "http://example.com:8888", "http://127.0.0.1", "http://127.0.0.1:8888/path"])
def test_refuses_nonisolated_browser_origin_before_importing_or_starting_browser(origin: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="isolated loopback"):
        browser.run_browser_agent(origin, tmp_path)


def test_native_browser_acceptance_cannot_fall_back_to_local_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="cloud-only"):
        browser.run_browser_agent("http://127.0.0.1:8888", tmp_path)


@pytest.mark.parametrize("failure", [None, "unverified", "profile_path", "generation_path", "file_path", "bad_hash", "plaintext", "wrong_format"])
def test_browser_mirror_receipt_is_checked_against_readonly_ciphertext(tmp_path: Path, failure: str | None) -> None:
    profile, generation, filename = "mirror_0123456789abcdef", "generation_1", "fixture.age"
    # Offline validator input only; actual Rust encryption is proved by the cloud case.
    body = b"age-encryption.org/v1\noffline-validator-input"
    if failure == "plaintext":
        body += browser.BROWSER_CREDENTIAL.encode()
    elif failure == "wrong_format":
        body = b"unsealed public text"
    path = tmp_path / ".backup-mirror" / profile / "generations" / generation / filename
    path.parent.mkdir(parents=True)
    path.write_bytes(body)
    digest = "0" * 64 if failure == "bad_hash" else hashlib.sha256(body).hexdigest()
    observed = {"mirrorProfileId": "../outside" if failure == "profile_path" else profile,
                "mirrorReceipt": {"creationVerified": failure != "unverified",
                    "generationId": "../outside" if failure == "generation_path" else generation,
                    "recipientVariants": [{"filename": "../outside.age" if failure == "file_path" else filename,
                                           "ciphertextSha256": digest}]}}
    before = path.read_bytes()
    if failure:
        with pytest.raises(ValueError):
            browser.inspect_browser_mirror(tmp_path, observed)
    else:
        assert browser.inspect_browser_mirror(tmp_path, observed) == {"independentMirrorCiphertextVerified": True}
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", [None, "missing_effect", "unknown_effect", "bad_hash", "plaintext_cipher", "plaintext_artifact", "missing_cipher", "extra_cipher"])
def test_independent_readonly_browser_custody_inspection(tmp_path: Path, failure: str | None) -> None:
    artifacts, state = tmp_path / "artifacts", tmp_path / "state"
    effects = state / "rust-worker"
    effects.mkdir(parents=True)
    sealed = artifacts / "agent-provider-credentials-v1"
    sealed.mkdir(parents=True)
    # These are offline validator inputs, never a claim of real encryption.
    for index in range(1 if failure == "missing_cipher" else (3 if failure == "extra_cipher" else 2)):
        (sealed / f"fixture-{index}.json").write_bytes(
            browser.BROWSER_CREDENTIAL.encode() if failure == "plaintext_cipher" and index == 1 else b'{"offlineCipherFixture":true}')
    with sqlite3.connect(effects / "authority.sqlite3") as database:
        database.execute("CREATE TABLE agent_node_effects(state TEXT,output BLOB,output_sha256 TEXT)")
        body = b"offline node output"
        digest = "0" * 64 if failure == "bad_hash" else hashlib.sha256(body).hexdigest()
        database.executemany("INSERT INTO agent_node_effects VALUES(?,?,?)", [
            ("EFFECT_UNKNOWN" if failure == "unknown_effect" else "CONFIRMED", body, digest)
            for _ in range(3 if failure == "missing_effect" else 4)])
    with sqlite3.connect(artifacts / "objects-v1.sqlite") as database:
        database.execute("CREATE TABLE artifacts(bytes BLOB)")
        database.execute("INSERT INTO artifacts VALUES(?)", (
            browser.BROWSER_CREDENTIAL.encode() if failure == "plaintext_artifact" else b"offline public body",))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    if failure:
        with pytest.raises(ValueError):
            browser.inspect_browser_custody(artifacts, state)
    else:
        assert browser.inspect_browser_custody(artifacts, state) == {
            "rustEffectsIndependentlyRetained": True, "requestCredentialEncrypted": True}
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_history_reload_keeps_the_receipt_before_chromium_discards_the_old_document_response() -> None:
    # Offline response-lifetime model only: no browser, native process or network.
    events: list[str] = []
    receipt = {"generationId": "retained-generation", "creationVerified": True}

    class Response:
        def json(self) -> dict[str, Any]:
            if "navigation" in events:
                raise RuntimeError("Chromium discarded the previous document response")
            events.append("receipt")
            return receipt

    class Page:
        def reload(self, **options: Any) -> None:
            assert options == {"wait_until": "domcontentloaded"}
            events.append("navigation")

        def get_by_text(self, text: str, **options: Any) -> Any:
            assert options == {"exact": True}
            return SimpleNamespace(last=SimpleNamespace(wait_for=lambda: events.append(text)))

    assert browser.reload_browser_history(Page(), Response()) is receipt
    assert events == ["receipt", "navigation", "Controlled final answer", browser.BROWSER_TITLE]


@pytest.mark.parametrize("payload", [None, [], "invalid receipt"])
def test_history_reload_rejects_a_missing_receipt_before_navigating(payload: Any) -> None:
    page = SimpleNamespace(reload=lambda **_: pytest.fail("Invalid receipt must not navigate"))
    with pytest.raises(ValueError, match="mirror receipt"):
        browser.reload_browser_history(page, SimpleNamespace(json=lambda: payload))


@pytest.mark.parametrize("change", ["critic_lost_predecessor", "synthesis_lost_outputs", "private_context"])
def test_browser_provider_context_requires_settled_public_predecessor_summaries(change: str) -> None:
    result, exchanges = browser_fixture()
    if change == "critic_lost_predecessor":
        exchanges[2]["predecessorSummaryInPrompt"] = False
    elif change == "synthesis_lost_outputs":
        exchanges[3]["predecessorSummaryInPrompt"] = False
    else:
        exchanges[2]["privateReasoningInPrompt"] = True
    with pytest.raises(ValueError, match="public predecessor"):
        browser.validate_browser_result(result, exchanges)


@pytest.mark.parametrize("legacy", [False, True])
def test_browser_recovery_baseline_uses_actual_native_public_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool) -> None:
    import json
    from scripts import verify_native_agent_browser_recovery as recovery

    public=json.loads((Path(__file__).parent/"native/agent_public_browser_v1.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(recovery,"_actions",lambda _: {str(i):"original-action" for i in range(3)})
    monkeypatch.setattr(recovery,"_effects",lambda _: {str(i):"original-effect" for i in range(4)})
    if legacy:
        public["outputs"]=public.pop("agentOutputs")
        with pytest.raises(ValueError):
            recovery.capture_before_recovery(tmp_path,tmp_path,public)
    else:
        before=recovery.capture_before_recovery(tmp_path,tmp_path,public)
        assert before["agentOutputs"]==public["agentOutputs"]
        assert before["events"]==public["events"]


@pytest.mark.parametrize("fault",[None,"legacy-field","wrong-final","wrong-coder","missing-coder","wrong-status","wrong-index","missing-baseline"])
def test_browser_inflight_guard_uses_the_actual_native_output_contract(fault: str | None) -> None:
    import json
    from scripts import verify_native_agent_browser_recovery as recovery

    initial = json.loads((Path(__file__).parent / "native/agent_public_browser_v1.json").read_text(encoding="utf-8"))
    before = {"agentOutputs": deepcopy(initial["agentOutputs"])}
    during = deepcopy(initial)
    during.update(status="running", nextIndex=12)
    during["agentOutputs"].pop("critic")
    if fault == "legacy-field":
        during["outputs"] = during.pop("agentOutputs")
    elif fault == "wrong-final":
        during["finalAnswer"] = "erased"
    elif fault == "wrong-coder":
        during["agentOutputs"]["coder"] = {"content": "changed"}
    elif fault == "missing-coder":
        during["agentOutputs"].pop("coder")
    elif fault == "wrong-status":
        during["status"] = "done"
    elif fault == "wrong-index":
        during["nextIndex"] = 13
    elif fault == "missing-baseline":
        before = {}
    if fault is None:
        assert recovery.inspect_inflight_public(during, before) == {"oldFinalRetained": True, "coderRetained": True}
    else:
        with pytest.raises(ValueError):
            recovery.inspect_inflight_public(during, before)
