"""Require all original9 and all24 actual, same-source PROCESS recovery cases."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

KINDS = ("webpage", "image", "screenshot", "pdf", "audio", "video")
BACKENDS = ("sqlite", "sqlite_vec")
PHASES = ("processing", "final")
ORIGINALS = ("process", "file-sync-failure", "directory-sync-failure", "create-crash", "create-source-sync-failures",
             "delete-sqlite", "delete-vector", "delete-crash-sqlite", "delete-crash-vector")
BOUND = 4 * 1024**2


def require(value: Any, code: str) -> None:
    if not value:
        raise ValueError("MEDIA_PROCESS_MATRIX_" + code)


def hex_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return left.keys() == right.keys() and all(type(left[key]) is type(right[key]) and left[key] == right[key] for key in left)


def immutable(before: dict[str, Any], after: dict[str, Any]) -> None:
    require(before.keys() == after.keys(), "ORIGINAL_FIELDS_CHANGED")
    require(equal({k: v for k, v in before.items() if k not in {"state", "outcomeSha256"}},
                  {k: v for k, v in after.items() if k not in {"state", "outcomeSha256"}}), "ORIGINAL_REBOUND")
    require(before["outcomeSha256"] is None and after["state"] == "APPLIED" and hex_hash(after["outcomeSha256"]), "UNKNOWN_OR_UNSIGNED_EFFECT")


def publication(value: dict[str, Any]) -> None:
    require(hex_hash(value["sha256"]) and type(value["inode"]) is int and value["inode"] > 0
            and type(value["mtimeNs"]) is int and value["mtimeNs"] >= 0, "PUBLICATION_IDENTITY_INVALID")


def validate_case(case: dict[str, Any], source: str, run: str, backend: str, kind: str) -> tuple[str, str]:
    require(case["status"] == "PASS" and (case["sourceCommit"], str(case["runId"]), case["backend"], case["mediaKind"])
            == (source, run, backend, kind) and case["phase"] in PHASES, "CASE_IDENTITY_OR_STATUS_INVALID")
    for name in ["workerSIGKILLDelivered", "allOriginalProcessesExited", "realRenameObserved", "originalSignedOutcomeRetained",
                 "originalSourceIdentityRetained", "unrelatedMediaAndIndexRetained", "originalNamespaceMetadataRetained",
                 "independentPublicReadsAndOriginalOracleVerified", "retainedAcrossAnotherCompleteRestart"]:
        require(case[name] is True, "ACTUAL_CASE_EVIDENCE_MISSING")
    require(type(case["writeRequestsSent"]) is int and case["writeRequestsSent"] == 1
            and type(case["writeRequestsAfterRestart"]) is int and case["writeRequestsAfterRestart"] == 0
            and case["originalOperationRepeated"] is False, "ORIGINAL_REQUEST_REPEATED")
    go, rust, settled_go, settled_rust = (case[name] for name in ["beforeCrashGo", "beforeCrashRust", "afterRestartGo", "afterRestartRust"])
    require({"operationId", "actionId", "epoch", "claimWriterFence", "intentSha256", "requestSha256", "claimTokenSha256",
             "ownerSha256", "rowCount", "state", "outcomeSha256"} <= go.keys()
            and {"operationId", "claimSha256", "preparationSha256", "plannedSha256", "beforeSha256", "afterSha256",
                 "processPreparationSha256", "rowCount", "state", "outcomeSha256"} <= rust.keys(), "ORIGINAL_BINDINGS_REQUIRED")
    immutable(go, settled_go)
    immutable(rust, settled_rust)
    require(go["state"] == "DISPATCHED" and rust["state"] == "APPLYING"
            and settled_go["outcomeSha256"] == settled_rust["outcomeSha256"], "ORIGINAL_EFFECT_DIFFERS")
    operation = go["operationId"]
    require(isinstance(operation, str) and re.fullmatch("media-process-[0-9a-f]{32}", operation)
            and operation == go["actionId"] == rust["operationId"] == case["originalOperationId"], "ORIGINAL_OPERATION_INVALID")
    require(type(go["epoch"]) is int and go["epoch"] == 1
            and all(type(v) is int for v in [go["claimWriterFence"], case["writerBefore"], case["writerAfter"]])
            and case["writerAfter"] > case["writerBefore"] == go["claimWriterFence"] > 0
            and type(go["rowCount"]) is type(rust["rowCount"]) is int and go["rowCount"] == rust["rowCount"] == 1,
            "ORIGINAL_FENCE_INVALID")
    for row in [go, rust]:
        require(all(hex_hash(value) for key, value in row.items() if key.endswith("Sha256") and key != "outcomeSha256"), "ORIGINAL_DIGEST_INVALID")
    require(go["intentSha256"] == go["requestSha256"] and rust["preparationSha256"] == rust["plannedSha256"], "ORIGINAL_PLAN_DIFFERS")
    initial, processing, held, after = (case[name] for name in ["initialPublication", "processingPhaseObserved", "publicationAtCrash", "publicationAfterRecovery"])
    require(equal(processing["go"], go) and equal(processing["rust"], rust), "PHASE_ORIGINAL_REBOUND")
    middle = processing["publication"]
    for witness in [initial, middle, held, after, case["publicationPredecessor"]]:
        publication(witness)
    require(initial["sha256"] == rust["beforeSha256"] and middle["sha256"] not in {rust["beforeSha256"], rust["afterSha256"]}
            and middle["inode"] != initial["inode"] and after["sha256"] == rust["afterSha256"], "PROCESSING_PUBLICATION_INVALID")
    if case["phase"] == "processing":
        require(equal(held, middle) and equal(case["publicationPredecessor"], initial), "PROCESSING_PREDECESSOR_DIFFERS")
    else:
        require(equal(case["publicationPredecessor"], middle) and held["inode"] != middle["inode"]
                and held["sha256"] == rust["afterSha256"] and equal(held, after), "FINAL_PUBLICATION_REBOUND_OR_REWRITTEN")
    identities = []
    groups = [case[name] for name in ["empty-launchProcesses", "mutation-launchProcesses", "recovery-launchProcesses", "retained-launchProcesses"]]
    for group in groups:
        require(len(group) == 7 and {r["executable"] for r in group} == {"deepseek-launch", "deepseekd", "deepseek-worker", "deepseek-gateway"}, "NATIVE_PROCESS_GROUP_INVALID")
        for row in group:
            require(type(row["pid"]) is int and row["pid"] > 1 and isinstance(row["startTicks"], str)
                    and re.fullmatch("[1-9][0-9]*", row["startTicks"]), "NATIVE_PROCESS_IDENTITY_INVALID")
            identities.append((row["pid"], row["startTicks"]))
    require(len(set(identities)) == 28 and len(case["killSignals"]) == 7
            and all(row["signalDelivered"] is True for row in case["killSignals"])
            and {(row["pid"], row["startTicks"]) for row in case["killSignals"]} == {
                (row["pid"], row["startTicks"]) for row in groups[1]}, "ORIGINAL_PROCESSES_NOT_KILLED")
    if backend == "sqlite_vec":
        require(case["actualVectorBytesAndBindingsVerified"] is True, "REAL_VECTOR_EVIDENCE_REQUIRED")
    return operation, settled_go["outcomeSha256"]


def qualify_matrix(cells: list[dict[str, Any]], originals: dict[str, dict[str, Any]], *, source: str, tree: str,
                   run: str, event: str = "workflow_dispatch", attempt: str = "1") -> dict[str, Any]:
    try:
        require(re.fullmatch("[0-9a-f]{40}", source) and re.fullmatch("[0-9a-f]{40}", tree) and re.fullmatch("[1-9][0-9]*", run), "SOURCE_REQUIRED")
        require(set(originals) == set(ORIGINALS) and all(row["status"] == "PASS" and row["sourceCommit"] == source for row in originals.values()), "ORIGINAL_NINE_REQUIRED")
        require(len(cells) == len(BACKENDS) * len(KINDS), "ALL_TWELVE_CELLS_REQUIRED")
        seen_cells, seen_cases, operations, outcomes = set(), set(), set(), set()
        summary = []
        for cell in cells:
            require(cell["kind"] == "native-media-process-crash-v1" and cell["status"] == "PASS"
                    and (cell["sourceCommit"], cell["sourceTree"], str(cell["runId"]), cell["event"], str(cell["attempt"]))
                    == (source, tree, run, event, attempt), "CELL_SOURCE_OR_STATUS_INVALID")
            require(len(cell["selectedMediaKinds"]) == 1, "EXPLICIT_SINGLE_KIND_REQUIRED")
            backend, kind = cell["backend"], cell["selectedMediaKinds"][0]
            require(backend in BACKENDS and kind in KINDS and (backend, kind) not in seen_cells, "CELL_DUPLICATE_OR_UNKNOWN")
            seen_cells.add((backend, kind))
            require(type(cell["requiredCases"]) is type(cell["completedCases"]) is int and cell["requiredCases"] == cell["completedCases"] == len(cell["cases"]) == 2
                    and cell["selectedProcessingAndFinalPublicationSIGKILLQualified"] is True, "BOTH_PHASES_REQUIRED")
            for name in ["actualProcessingAndFinalPublicationSIGKILLQualified", "originalOperationAutonomousRecoveryQualified",
                         "fullMigrationQualified", "fullProcessingPipelineQualified", "physicalPowerLossQualified", "allProcessingCrashPhasesQualified"]:
                require(cell[name] is False, "CELL_CANNOT_CLAIM_WHOLE_QUALIFICATION")
            require(cell["readiness"] == "NOT_READY", "CELL_SCOPE_INVALID")
            for case in cell["cases"]:
                operation, outcome = validate_case(case, source, run, backend, kind)
                key = (backend, kind, case["phase"])
                require(key not in seen_cases and operation not in operations and outcome not in outcomes, "CASE_OR_ORIGINAL_DUPLICATED")
                seen_cases.add(key)
                operations.add(operation)
                outcomes.add(outcome)
                summary.append(dict(backend=backend, mediaKind=kind, phase=case["phase"], operationId=operation, signedOutcomeSha256=outcome))
        require(seen_cases == {(b, k, p) for b in BACKENDS for k in KINDS for p in PHASES}, "FULL_CORPUS_REQUIRED")
        return dict(status="PASS", kind="native-media-process-matrix-v1", sourceCommit=source, sourceTree=tree, runId=run,
                    event=event, attempt=attempt, originalCasesPassed=9, completedCases=24, cases=summary,
                    actualProcessingAndFinalPublicationSIGKILLQualified=True, originalOperationAutonomousRecoveryQualified=True,
                    nativeSQLiteVecStandaloneForceRecoveryHTTPQualified=True, allProcessingCrashPhasesQualified=False,
                    fullProcessingPipelineQualified=False, physicalPowerLossQualified=False, fullSourceCIQualified=False,
                    fullMigrationQualified=False, readiness="NOT_READY")
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError("MEDIA_PROCESS_MATRIX_MALFORMED_REPORT") from error


def read_report(path: Path, root: Path) -> dict[str, Any]:
    require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()) and path.stat().st_size <= BOUND, "BOUNDED_OWNED_REPORT_REQUIRED")
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict), "REPORT_OBJECT_REQUIRED")
    return value


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    value: dict[str, Any] = dict(status="FAIL", fullMigrationQualified=False, readiness="NOT_READY")
    try:
        require(sys.platform == "linux" and os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "CLOUD_AGGREGATION_REQUIRED")
        source, tree, run = (os.environ[key] for key in ["GITHUB_SHA", "DEEPSEEK_SOURCE_TREE", "GITHUB_RUN_ID"])
        original_dir = args.inputs / "original"
        originals = {name: read_report(original_dir / name / "result.json", args.inputs) for name in ORIGINALS}
        binary_context = read_report(original_dir / "native-binaries.json", args.inputs)
        require((binary_context["sourceCommit"], binary_context["sourceTree"], str(binary_context["runId"])) == (source, tree, run), "BINARY_SOURCE_DIFFERS")
        require({r["name"] for r in binary_context["binaries"]} == {"deepseek-worker", "deepseek-gateway"}
                and all(hex_hash(r["sha256"]) and type(r["bytes"]) is int and r["bytes"] > 0 for r in binary_context["binaries"]), "NATIVE_BINARIES_REQUIRED")
        require(binary_context["requiredElfDependenciesResolved"] is True
                and any(r["path"] == "ui/index.html" and type(r["bytes"]) is int and r["bytes"] > 0 and hex_hash(r["sha256"])
                        for r in binary_context["files"]), "NATIVE_UI_AND_LINKAGE_REQUIRED")
        cells = []
        expected = {f"native-media-process-{b}-{k}-{source}" for b in BACKENDS for k in KINDS}
        require({p.name for p in args.inputs.iterdir()} == expected | {"original"}, "ARTIFACT_CORPUS_DIFFERS")
        for folder in sorted(expected):
            root = args.inputs / folder
            require(read_report(root / "binary-context.json", args.inputs) == binary_context, "BINARY_CONTEXT_REBOUND")
            cell = read_report(root / "result.json", args.inputs)
            for case in cell["cases"]:
                actual = read_report(root / (case["mediaKind"] + "-" + case["phase"]) / "result.json", args.inputs)
                require(json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(case, sort_keys=True, allow_nan=False), "CASE_REPORT_REBOUND")
            cells.append(cell)
        value = qualify_matrix(cells, originals, source=source, tree=tree, run=run,
                               event=os.environ["GITHUB_EVENT_NAME"], attempt=os.environ["GITHUB_RUN_ATTEMPT"])
        members = [p for p in args.inputs.rglob("*") if p.is_file()]
        require(len(members) <= 2000 and sum(p.stat().st_size for p in members) <= 64 * 1024**2
                and all(not p.is_symlink() and p.resolve().is_relative_to(args.inputs.resolve()) and p.stat().st_size <= 32 * 1024**2 for p in members), "REPORT_MEMBER_BOUND")
        hashes = []
        for path in sorted(members):
            hashes.append(dict(path=path.relative_to(args.inputs).as_posix(), sha256=file_hash(path)))
        value["reportMembers"] = hashes
    except Exception as error:
        value.update(failureCode=str(error)[:300], exceptionType=type(error).__name__)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: value[k] for k in ["status", "sourceCommit", "completedCases", "failureCode"] if k in value}), flush=True)
    return 0 if value["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
