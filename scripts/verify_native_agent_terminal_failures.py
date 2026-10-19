#!/usr/bin/env python3
"""Cloud qualification of conclusive planner/synthesis rejection and public resume."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def _receipt_types() -> tuple[Any, Any]:
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    descriptor = Path(__file__).resolve().parents[1] / "proto/generated/descriptor.pb"
    files = descriptor_pb2.FileDescriptorSet.FromString(descriptor.read_bytes())
    pool = descriptor_pool.DescriptorPool()
    pending = {f.name: f for f in files.file}
    loaded: set[str] = set()
    while pending:
        ready = [name for name, f in pending.items() if set(f.dependency) <= loaded]
        if not ready:
            raise ValueError("Native descriptor dependency graph is incomplete")
        for name in ready:
            pool.Add(pending.pop(name))
            loaded.add(name)
    return (message_factory.GetMessageClass(pool.FindMessageTypeByName("deepseek.agent.v1.AgentNodeOutcomeReceipt")),
            message_factory.GetMessageClass(pool.FindMessageTypeByName("deepseek.agent.v1.AgentFailureProjectionReceipt")))


def _hex(value: Any, length: int = 64) -> bool:
    return isinstance(value, str) and len(value) == length and all(c in "0123456789abcdef" for c in value)


def _document(document: dict[str, Any]) -> bytes:
    encoded, digest = document.get("receiptBase64"), document.get("receiptSha256")
    if not isinstance(encoded, str) or not _hex(digest):
        raise ValueError("Terminal failure lacks a canonical original document")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid original document encoding") from exc
    if not raw or len(raw) > 1 << 20 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Terminal failure document bytes do not match their binding")
    return raw


def validate_terminal_result(result: dict[str, Any], exchanges: list[dict[str, Any]], mode: str) -> None:
    if mode not in {"plan", "synthesis"}:
        raise ValueError("Unknown terminal failure mode")
    planning = mode == "plan"
    expected = {
        "initialEventCount": 2 if planning else 8, "nextIndex": 14 if planning else 12,
        "publicEventCount": 14 if planning else 12, "initialActionCount": 1 if planning else 3,
        "durablePhases": 3 if planning else 4, "runtimeRevision": 2, "generation": 1,
        "metadataSchemaVersion": 3, "initialPublicPlanCount": 0 if planning else 2,
        "publicPlanCount": 0 if planning else 2, "executeCount": 4 if planning else 0,
    }
    if result.get("status") != "PASS" or result.get("mode") != mode or result.get("runStatus") != "done" or not _hex(result.get("sourceCommit"), 40) or not isinstance(result.get("runId"), str) or not result["runId"]:
        raise ValueError("Terminal recovery did not qualify its actual source/run")
    if any(type(result.get(k)) is not int or result[k] != v for k, v in expected.items()):
        raise ValueError("Terminal recovery changed exact cursor, phase or runtime boundaries")
    for key in ["originalActionsUnchanged", "originalEventsUnchanged", "resourcesReleased", "coherentPublicSettlementVerified",
                "finalAnswerVisible", "initialFailurePublic", "publicResumeQualified", "providerFixtureIsOfflineTestTool"]:
        if result.get(key) is not True:
            raise ValueError("Terminal recovery omitted actual proof: " + key)
    if result.get("defaultDAGKeptOutOfPublicPlan") is not planning or result.get("productionRuntimeProcesses") != ["Go", "Rust"] or any(
        result.get(k) is not False for k in ["inputBodySeededByOfflineOracle", "productionProviderQualified", "fullMigrationQualified"]
    ):
        raise ValueError("Terminal recovery fabricated authority or whole-product qualification")
    roles = ["researcher", "coder", "reasoner", "critic"] if planning else ["coder", "critic"]
    if result.get("effectiveRecoveryRoles") != roles:
        raise ValueError("Terminal recovery changed the retained default/task DAG")
    for key in ["requestSha256", "originalRequestSha256", "contextArtifactSha256", "contextSha256"]:
        if not _hex(result.get(key)):
            raise ValueError("Terminal recovery omitted an immutable binding")
    if result["requestSha256"] == result["originalRequestSha256"] or any(type(result.get(k)) is not int or result[k] <= 0 for k in ["requestLength", "contextArtifactLength"]):
        raise ValueError("Terminal recovery reused an old runtime request")
    count, actions, rejected = (6, 3, 0) if planning else (5, 4, 3)
    documents, projections = result.get("receiptDocuments"), result.get("failureProjectionDocuments")
    if not isinstance(documents, list) or len(documents) != count or not isinstance(projections, list) or len(projections) != 1 or len(exchanges) != count:
        raise ValueError("Terminal recovery omitted or repeated a real effect/projection")
    if any(not isinstance(d, dict) or d.get("pinnedSignatureVerified") is not True for d in documents + projections):
        raise ValueError("Terminal recovery lacks independent Go pinned signature verification")
    for document in documents + projections:
        _document(document)
    failed = [d for d in documents if d.get("terminalFailure") is True]
    if len(failed) != 1 or failed[0].get("nodeId") != ("leader" if planning else "synthesizer") or failed[0].get("httpStatus") != 401:
        raise ValueError("Terminal recovery lost its original typed phase failure")
    if any(d.get("failed") is not False or d.get("terminalFailure") is not (d is failed[0]) for d in documents):
        raise ValueError("Terminal failure was relabelled as a failed ordinary task")
    by_operation = {d.get("operationId"): d for d in documents}
    operations = {x.get("operationId") for x in exchanges}
    if len(by_operation) != count or len(operations) != count or None in operations or set(by_operation) != operations or len({x.get("actionId") for x in exchanges}) != actions:
        raise ValueError("Terminal recovery rebound or repeated an original provider operation")
    if projections[0].get("actionId") != failed[0]["actionId"] or exchanges[rejected].get("operationId") != failed[0]["operationId"]:
        raise ValueError("Terminal projection does not bind the actual rejected request")
    for index, exchange in enumerate(exchanges):
        if (
            exchange.get("httpStatus") != (401 if index == rejected else 200) or not _hex(exchange.get("responseSha256"))
            or exchange.get("requestCredentialUsed") is not True or exchange.get("privateReasoningInPrompt") is not False
            or exchange.get("stream") is not False or by_operation[exchange["operationId"]]["actionId"] != exchange.get("actionId")
            or not str(exchange.get("executionEpoch", "")).isdecimal() or int(exchange["executionEpoch"]) < 1
        ):
            raise ValueError("Actual provider exchanges lost scope, private-data isolation or rejection semantics")
    if planning and exchanges[-1].get("terminalDefaultDescriptionsInPrompt") is not True:
        raise ValueError("Default recovery omitted actual four-role synthesis inputs")
    if not planning and any(exchanges[-1].get(k) is not True for k in ["synthesisCoderInPrompt", "synthesisCriticInPrompt"]):
        raise ValueError("Synthesis recovery discarded retained task inputs")


def verify_terminal_custody(result: dict[str, Any], exchanges: list[dict[str, Any]], state: Path, artifacts: Path) -> None:
    node_type, projection_type = _receipt_types()
    signed, event_bodies = {}, []
    for document in result["receiptDocuments"]:
        receipt = node_type.FromString(_document(document))
        e = receipt.evidence
        if receipt.schema_version != 1 or len(receipt.signature) != 64 or e.schema_version != 1 or e.run_id != result["runId"] or (
            e.operation_id != document["operationId"] or e.node_id != document["nodeId"] or e.fence.action_id != document["actionId"]
            or e.HasField("failure") is not document["terminalFailure"] or (e.failure.http_status if e.HasField("failure") else 0) != document["httpStatus"]
        ):
            raise ValueError("Go report rebound fields of the actual signed protobuf")
        signed[e.operation_id] = receipt
    failed_document = next(d for d in result["receiptDocuments"] if d["terminalFailure"])
    failed = signed[failed_document["operationId"]].evidence
    for document in result["failureProjectionDocuments"]:
        projection = projection_type.FromString(_document(document))
        p = projection.evidence
        if projection.schema_version != 1 or len(projection.signature) != 64 or p.schema_version != 1 or (
            p.source_receipt_sha256 != failed_document["receiptSha256"] or p.settlement_fence.action_id != failed.fence.action_id
            or p.settlement_fence.execution_epoch < failed.fence.execution_epoch or p.run_id != result["runId"] or p.phase != failed.phase
            or p.metadata_epoch != failed.metadata_epoch or p.expected_index != result["initialEventCount"] - 1
            or p.error_body != failed.public_event or p.fleet_id != failed.fleet_id or p.environment != failed.environment
            or p.settlement_writer_token < 1
        ):
            raise ValueError("Actual failure projection rebound original signed custody")
    with sqlite3.connect((state / "rust-worker/control-signatures.sqlite3").as_uri() + "?mode=ro", uri=True) as signatures:
        if signatures.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Actual Rust signature journal failed integrity")
        for document in result["receiptDocuments"] + result["failureProjectionDocuments"]:
            raw = _document(document)
            if signatures.execute("SELECT count(*) FROM signatures WHERE document=?", (raw,)).fetchone()[0] != 1:
                raise ValueError("Terminal proof lacks unique original Rust signer custody")
    with sqlite3.connect((state / "rust-worker/authority.sqlite3").as_uri() + "?mode=ro", uri=True) as effects:
        if effects.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Actual Rust effect journal failed integrity")
        rows = effects.execute("SELECT operation_id,state,output,output_sha256 FROM agent_node_effects").fetchall()
        if len(rows) != len(exchanges) or any(row[1] != "CONFIRMED" or hashlib.sha256(row[2]).hexdigest() != row[3] for row in rows):
            raise ValueError("Terminal recovery repeated, lost or fabricated native effects")
        envelopes = {operation: json.loads(raw) for operation, _, raw, _ in rows}
        expected_types = Counter({"error": 1, "agent_output": 4, "content": 1}) if result["mode"] == "plan" else Counter({"agent_plan": 1, "agent_output": 2, "error": 1, "content": 1})
        if Counter(x["publicEvent"]["type"] for x in envelopes.values()) != expected_types:
            raise ValueError("Native terminal recovery changed successful/failing phase roles")
        for exchange in exchanges:
            envelope = envelopes[exchange["operationId"]]
            evidence = signed[exchange["operationId"]].evidence
            raw = next(row[2] for row in rows if row[0] == exchange["operationId"])
            if (hashlib.sha256(raw).hexdigest() != evidence.outcome_sha256 or len(raw) != evidence.outcome_length
                or envelope.get("providerResponseSha256") != exchange["responseSha256"] or evidence.fence.action_id != exchange["actionId"]
                or str(evidence.fence.execution_epoch) != str(exchange["executionEpoch"])):
                raise ValueError("Native terminal outcome differs from actual provider bytes")
            event_bodies.append((evidence.public_event, envelope["publicEvent"]))
            if exchange["httpStatus"] != 200:
                role = "规划" if result["mode"] == "plan" else "综合"
                if evidence.failure.response_sha256 != exchange["responseSha256"] or envelope.get("providerFailure") != {"schemaVersion": 1, "httpStatus": 401} or envelope["publicEvent"] != {
                    "type": "error", "phase": "leader" if result["mode"] == "plan" else "synthesizer", "code": "AGENT_PROVIDER_REJECTED", "error": f"上游已拒绝{role}请求（HTTP 401）"
                }:
                    raise ValueError("Original Rust outcome did not retain the exact sanitized failure")
    with sqlite3.connect((artifacts / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as objects:
        for reference, event in event_bodies:
            value = objects.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (reference.sha256,)).fetchone()
            if not value or value[0] != reference.length or hashlib.sha256(value[1]).hexdigest() != reference.sha256 or json.loads(value[1]) != event:
                raise ValueError("Public event bytes disagree with the independently signed original outcome")
        row = objects.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (result["contextArtifactSha256"],)).fetchone()
        request = objects.execute("SELECT bytes FROM artifacts WHERE sha256=?", (result["requestSha256"],)).fetchone()
        if not row or row[0] != result["contextArtifactLength"] or hashlib.sha256(row[1]).hexdigest() != result["contextArtifactSha256"] or not request:
            raise ValueError("Terminal recovery lacks immutable Rust request/context custody")
        context = json.loads(row[1])
        if set(context) != {"schema", "metadataProto"} or context["schema"] != "native-agent-recovery-context-v1" or json.loads(request[0]).get("_nativeRecoveryContext") != context:
            raise ValueError("Terminal recovery request rebound its prepared protobuf context")
        encoded = context["metadataProto"]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded or hashlib.sha256(raw).hexdigest() != result["contextSha256"]:
            raise ValueError("Terminal context differs from the Go-authoritative recovery digest")
        if any(marker in body for (body,) in objects.execute("SELECT bytes FROM artifacts") for marker in [b"request-only-credential", b"private-provider-rejection", b"private-task-reasoning"]):
            raise ValueError("Terminal public artifact custody exposed provider-private material")
    result.update(actualRustSignerCustodyVerified=True, actualRustEffectCustodyVerified=True, actualRustRecoveryContextVerified=True)


def validate_unknown_result(result: dict[str, Any], exchanges: list[dict[str, Any]], mode: str) -> None:
    if mode not in {"plan-5xx", "plan-eof", "synthesis-5xx", "synthesis-eof"}:
        raise ValueError("Unknown ambiguous terminal case")
    planning, eof = mode.startswith("plan-"), mode.endswith("-eof")
    count = 1 if planning else 4
    expected = {"initialEventCount": 1 if planning else 7, "nextIndex": 2 if planning else 8,
                "durablePhases": 1 if planning else 3, "resumeStatus": 409, "rerunStatus": 409}
    if result.get("status") != "PASS" or result.get("mode") != mode or result.get("runStatus") != "cancelled" or not _hex(result.get("sourceCommit"), 40):
        raise ValueError("Unknown effect was not preserved through actual public cancellation")
    if any(type(result.get(k)) is not int or result[k] != value for k, value in expected.items()):
        raise ValueError("Unknown effect admitted a new phase, cursor or recovery")
    for key in ["unknownNotClosed", "originalFenceRetained", "reservationRetained", "noRecoveryGeneration", "originalRequestRetained", "providerFixtureIsOfflineTestTool"]:
        if result.get(key) is not True:
            raise ValueError("Unknown effect lost required custody: " + key)
    if any(result.get(k) is not False for k in ["inputBodySeededByOfflineOracle", "productionProviderQualified", "fullMigrationQualified"]) or result.get("productionRuntimeProcesses") != ["Go", "Rust"]:
        raise ValueError("Ambiguous effect falsely qualified production or a replacement runtime")
    if not _hex(result.get("requestSha256")) or type(result.get("requestLength")) is not int or result["requestLength"] <= 0 or type(result.get("originalExecutionEpoch")) is not int or result["originalExecutionEpoch"] < 1:
        raise ValueError("Unknown effect omitted its original request or fence")
    documents = result.get("receiptDocuments")
    if not isinstance(documents, list) or len(documents) != count - 1 or len(exchanges) != count or len({x.get("operationId") for x in exchanges}) != count:
        raise ValueError("Unknown effect repeated or lost a provider dispatch")
    for document in documents:
        if document.get("pinnedSignatureVerified") is not True:
            raise ValueError("Known predecessor lacks Go pinned verification")
        _document(document)
    if exchanges[-1].get("operationId") != result.get("unknownOperationId") or exchanges[-1].get("actionId") != result.get("unknownActionId") or str(exchanges[-1].get("executionEpoch")) != str(result["originalExecutionEpoch"]):
        raise ValueError("Unknown result rebound the actual provider operation")
    for index, exchange in enumerate(exchanges):
        unknown = index == count - 1
        if (exchange.get("httpStatus") != ((401 if eof else 503) if unknown else 200)
            or exchange.get("responseComplete") is not (not (unknown and eof)) or not _hex(exchange.get("responseSha256"))
            or exchange.get("requestCredentialUsed") is not True or exchange.get("privateReasoningInPrompt") is not False
            or exchange.get("stream") is not False):
            raise ValueError("Ambiguous response was replaced with a conclusive or private result")


def verify_unknown_custody(result: dict[str, Any], exchanges: list[dict[str, Any]], state: Path, artifacts: Path) -> None:
    from google.protobuf.message import DecodeError

    node_type, _ = _receipt_types()
    known = {}
    with sqlite3.connect((state / "rust-worker/control-signatures.sqlite3").as_uri() + "?mode=ro", uri=True) as signatures:
        if signatures.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Unknown effect signer journal failed integrity")
        for document in result["receiptDocuments"]:
            raw = _document(document)
            if signatures.execute("SELECT count(*) FROM signatures WHERE document=?", (raw,)).fetchone()[0] != 1:
                raise ValueError("Known predecessor lost original signature custody")
            receipt = node_type.FromString(raw)
            e = receipt.evidence
            if e.operation_id != document["operationId"] or e.HasField("failure"):
                raise ValueError("Unknown case altered a successful predecessor")
            known[e.operation_id] = e
        for (raw,) in signatures.execute("SELECT document FROM signatures"):
            try:
                receipt = node_type.FromString(raw)
            except DecodeError:
                continue
            if receipt.evidence.operation_id == result["unknownOperationId"]:
                raise ValueError("Unknown effect acquired a signed terminal outcome")
    with sqlite3.connect((state / "rust-worker/authority.sqlite3").as_uri() + "?mode=ro", uri=True) as effects:
        if effects.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Unknown effect journal failed integrity")
        rows = effects.execute("SELECT operation_id,action_id,epoch,run_id,state,output,output_sha256 FROM agent_node_effects").fetchall()
        if len(rows) != len(exchanges) or {r[0] for r in rows} != {x["operationId"] for x in exchanges}:
            raise ValueError("Unknown effect disappeared or was redispatched under a new identity")
        for operation, action, epoch, run, status, output, digest in rows:
            exchange = next(x for x in exchanges if x["operationId"] == operation)
            if action != exchange["actionId"] or str(epoch) != str(exchange["executionEpoch"]) or run != result["runId"]:
                raise ValueError("Original unknown/provider fence changed")
            if operation == result["unknownOperationId"]:
                if status != "EFFECT_UNKNOWN" or output is not None or digest is not None:
                    raise ValueError("Ambiguous provider result was closed or erased")
            else:
                if status != "CONFIRMED" or output is None or digest != hashlib.sha256(output).hexdigest() or operation not in known or digest != known[operation].outcome_sha256:
                    raise ValueError("Unknown case lost successful original custody")
                if json.loads(output).get("providerResponseSha256") != exchange["responseSha256"]:
                    raise ValueError("Known predecessor differs from actual provider bytes")
    with sqlite3.connect((artifacts / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as objects:
        row = objects.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (result["requestSha256"],)).fetchone()
        if not row or row[0] != result["requestLength"] or hashlib.sha256(row[1]).hexdigest() != result["requestSha256"] or json.loads(row[1]).get("_nativeRecoveryContext") is not None:
            raise ValueError("Refused recovery changed the original runtime request")
    result.update(actualRustSignerCustodyVerified=True, actualRustEffectCustodyVerified=True, actualUnknownQuarantineVerified=True)


def main() -> int:
    import verify_native_agent_public_driver as public

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["worker", "edge", "provisioner", "go-cache", "out"]:
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    cases, failures = [], []
    for mode in ["plan", "synthesis", "plan-5xx", "plan-eof", "synthesis-5xx", "synthesis-eof"]:
        child = argparse.Namespace(**vars(args))
        child.out, child.terminal_failure_case = args.out / mode, mode
        try:
            result = public.run(child)
            if "-" in mode:
                validate_unknown_result(result, result["providerExchanges"], mode)
            else:
                validate_terminal_result(result, result["providerExchanges"], mode)
            third_proof = "actualUnknownQuarantineVerified" if "-" in mode else "actualRustRecoveryContextVerified"
            if any(result.get(k) is not True for k in ["actualRustSignerCustodyVerified", "actualRustEffectCustodyVerified", third_proof]):
                raise ValueError("Actual terminal recovery custody was not verified")
            cases.append(result)
        except Exception as exc:
            failures.append({"mode": mode, "errorType": type(exc).__name__, "message": str(exc)[:1000]})
    report = {"status": "FAIL" if failures else "PASS", "caseCount": len(cases), "cases": cases, "failures": failures,
              "fullMigrationQualified": False, "readiness": "NOT_READY"}
    if not failures:
        if len({x["sourceCommit"] for x in cases}) != 1 or len({x["runId"] for x in cases}) != 6 or sum(x["actualHTTPRequests"] for x in cases) != 21:
            raise ValueError("Terminal matrix mixed revisions, identities or actual request counts")
        report.update(sourceCommit=cases[0]["sourceCommit"], actualProviderHTTPRequests=21)
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
