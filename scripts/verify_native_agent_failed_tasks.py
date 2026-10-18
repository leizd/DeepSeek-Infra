#!/usr/bin/env python3
"""Cloud process qualification for signed failed tasks and explicit public recovery."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def _digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_failed_task_result(result: dict[str, Any], exchanges: list[dict[str, Any]], mode: str) -> None:
    if mode not in {"resume", "rerun"}:
        raise ValueError("Unknown public failed-task recovery case")
    rerun = mode == "rerun"
    expected = {
        "status": "PASS",
        "mode": mode,
        "runStatus": "done",
        "initialEventCount": 9,
        "nextIndex": 16 if rerun else 15,
        "publicEventCount": 16 if rerun else 15,
        "durablePhases": 5,
        "runtimeRevision": 2,
        "generation": 1,
        "metadataSchemaVersion": 4 if rerun else 3,
        "initialReceiptCount": 4,
    }
    if any(result.get(k) != v for k, v in expected.items()):
        raise ValueError("Failed-task recovery changed durable phase, cursor or generation boundaries")
    for field in (
        "initialTaskFailed",
        "initialFailureRetained",
        "originalEventsUnchanged",
        "originalActionsUnchanged",
        "otherRoleNotRerun",
        "resourcesReleased",
        "admittedThroughRustHTTP",
        "finalAnswerVisible",
        "providerFixtureIsOfflineTestTool",
    ):
        if result.get(field) is not True:
            raise ValueError("Failed-task recovery omitted actual proof: " + field)
    if (
        result.get("recoveredTaskFailed") is not rerun
        or any(
            result.get(k) is not False for k in ("inputBodySeededByOfflineOracle", "productionProviderQualified", "fullMigrationQualified")
        )
        or result.get("productionRuntimeProcesses") != ["Go", "Rust"]
    ):
        raise ValueError("Failed-task recovery fabricated success or whole-product qualification")
    for name in ("requestSha256", "originalRequestSha256", "contextArtifactSha256", "contextSha256"):
        if not _digest(result.get(name)):
            raise ValueError("Failed-task recovery omitted an exact immutable body binding")
    if result["requestSha256"] == result["originalRequestSha256"] or any(
        type(result.get(k)) is not int or result[k] <= 0 for k in ("requestLength", "contextArtifactLength")
    ):
        raise ValueError("Failed-task recovery reused the original runtime request")
    documents = result.get("receiptDocuments")
    if not isinstance(documents, list) or len(documents) != 6 or len(exchanges) != 6:
        raise ValueError("Failed-task recovery omitted custody or repeated a provider request")
    by_operation = {d.get("operationId"): d for d in documents if isinstance(d, dict)}
    operations = {x.get("operationId") for x in exchanges}
    if (
        len(by_operation) != 6
        or len(operations) != 6
        or set(by_operation) != operations
        or None in operations
        or len({x.get("actionId") for x in exchanges}) != 5
    ):
        raise ValueError("Failed-task recovery lost exact original operation identities")
    statuses = [200, 401, 200, 200, 429 if rerun else 200, 200]
    for exchange, status in zip(exchanges, statuses):
        document = by_operation[exchange["operationId"]]
        if (
            exchange.get("httpStatus") != status
            or exchange.get("stream") is not False
            or exchange.get("model") != "deepseek-v4-pro"
            or exchange.get("privateReasoningInPrompt") is not False
            or exchange.get("requestCredentialUsed") is not True
            or document.get("pinnedSignatureVerified") is not True
            or document.get("actionId") != exchange.get("actionId")
            or document.get("failed") is not (status != 200)
        ):
            raise ValueError("Failed-task result is not the exact conclusive signed provider response")
        if not _digest(document.get("receiptSha256")) or not isinstance(document.get("receiptBase64"), str):
            raise ValueError("Failed-task result omitted its actual receipt document")
    if (
        exchanges[2].get("failedPredecessorInPrompt") is not True
        or exchanges[3].get("synthesisFailureNoticeInPrompt") is not True
        or (rerun and exchanges[5].get("synthesisFailureNoticeInPrompt") is not True)
        or (not rerun and exchanges[5].get("recoveredTaskSummaryInPrompt") is not True)
    ):
        raise ValueError("Dependent or synthesis prompt concealed the failed role or latest recovered output")
    if rerun and (
        result.get("progressSignatureVerified") is not True
        or not _digest(result.get("progressReceiptSha256"))
        or not isinstance(result.get("progressReceiptBase64"), str)
    ):
        raise ValueError("Rejected rerun omitted actual typed Rust progress")


def verify_failed_task_custody(result: dict[str, Any], exchanges: list[dict[str, Any]], state: Path, artifacts: Path) -> None:
    """Read actual Rust-owned journals; offline flags alone never qualify custody."""
    with sqlite3.connect((state / "rust-worker/control-signatures.sqlite3").as_uri() + "?mode=ro", uri=True) as signatures:
        if signatures.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Actual Rust signer journal integrity failed")
        documents = [(d["receiptBase64"], d["receiptSha256"]) for d in result["receiptDocuments"]]
        if result["mode"] == "rerun":
            documents.append((result["progressReceiptBase64"], result["progressReceiptSha256"]))
        for encoded, digest in documents:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if (
                base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded
                or hashlib.sha256(raw).hexdigest() != digest
                or signatures.execute("SELECT count(*) FROM signatures WHERE document=?", (raw,)).fetchone()[0] != 1
            ):
                raise ValueError("Go receipt lacks matching unique actual Rust signer custody")
    with sqlite3.connect((state / "rust-worker/authority.sqlite3").as_uri() + "?mode=ro", uri=True) as effects:
        rows = effects.execute("SELECT operation_id,state,output,output_sha256 FROM agent_node_effects").fetchall()
        if len(rows) != 6 or any(row[1] != "CONFIRMED" or hashlib.sha256(row[2]).hexdigest() != row[3] for row in rows):
            raise ValueError("Failed-task recovery fabricated or repeated native effect custody")
        envelopes = {operation: json.loads(raw) for operation, _, raw, _ in rows}
        for exchange in exchanges:
            envelope = envelopes[exchange["operationId"]]
            if envelope["providerResponseSha256"] != exchange["responseSha256"]:
                raise ValueError("Signed task custody does not match actual provider bytes")
            if exchange["httpStatus"] != 200 and (
                envelope.get("providerStatus") != exchange["httpStatus"]
                or envelope["publicEvent"].get("output", {}).get("failed") is not True
            ):
                raise ValueError("Closed rejection was not retained as a failed task")
    with sqlite3.connect((artifacts / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as objects:
        row = objects.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (result["contextArtifactSha256"],)).fetchone()
        request = objects.execute("SELECT bytes FROM artifacts WHERE sha256=?", (result["requestSha256"],)).fetchone()
        if (
            not row
            or row[0] != result["contextArtifactLength"]
            or hashlib.sha256(row[1]).hexdigest() != result["contextArtifactSha256"]
            or not request
        ):
            raise ValueError("Failed-task recovery context lacks real immutable Rust bodies")
        context = json.loads(row[1])
        stored = json.loads(request[0])
        if (
            stored.get("_nativeRecoveryContext") != context
            or set(context) != {"schema", "metadataProto"}
            or context["schema"] != "native-agent-recovery-context-v1"
        ):
            raise ValueError("Failed-task runtime request does not bind the actual new context")
        encoded = context["metadataProto"]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if hashlib.sha256(raw).hexdigest() != result["contextSha256"]:
            raise ValueError("Failed-task recovery rebound the Go-authoritative context")
        if any(
            b"request-only-credential" in body or b"private-provider-rejection" in body
            for (body,) in objects.execute("SELECT bytes FROM artifacts")
        ):
            raise ValueError("Failed-task body custody exposed a private error or provider credential")
    result.update(actualRustSignerCustodyVerified=True, actualRustEffectCustodyVerified=True, actualRustRecoveryContextVerified=True)


def main() -> int:
    import verify_native_agent_public_driver as public

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("worker", "edge", "provisioner", "go-cache", "out"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    cases = []
    for mode in ("resume", "rerun"):
        child = argparse.Namespace(**vars(args))
        child.out = args.out / mode
        child.failed_task_case = mode
        result = public.run(child)
        validate_failed_task_result(result, result["providerExchanges"], mode)
        if not all(
            result.get(k) is True
            for k in ("actualRustSignerCustodyVerified", "actualRustEffectCustodyVerified", "actualRustRecoveryContextVerified")
        ):
            raise ValueError("Actual native failed-task custody not verified")
        cases.append(result)
    report = {
        "status": "PASS",
        "sourceCommit": cases[0]["sourceCommit"],
        "caseCount": 2,
        "actualProviderHTTPRequests": 12,
        "actualPublicRecoveryRequests": 2,
        "cases": cases,
        "readiness": "NOT_READY",
        "productionProviderQualified": False,
        "fullMigrationQualified": False,
        "cloudOnlyNativeBuild": True,
    }
    (args.out / "matrix-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
