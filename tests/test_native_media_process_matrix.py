"""Offline corpus/identity oracles; no native process or application is started."""

import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

SOURCE, TREE, RUN = "a" * 40, "b" * 40, "12345"
KINDS = ("webpage", "image", "screenshot", "pdf", "audio", "video")
ORIGINALS = (
    "process",
    "file-sync-failure",
    "directory-sync-failure",
    "create-crash",
    "create-source-sync-failures",
    "delete-sqlite",
    "delete-vector",
    "delete-crash-sqlite",
    "delete-crash-vector",
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def case(number: int, backend: str, kind: str, phase: str) -> dict[str, Any]:
    op = "media-process-" + f"{number:032x}"
    go = dict(
        operationId=op,
        actionId=op,
        epoch=1,
        claimWriterFence=2,
        intentSha256=sha(op + "request"),
        requestSha256=sha(op + "request"),
        claimTokenSha256=sha(op + "token"),
        ownerSha256=sha(op + "owner"),
        rowCount=1,
        state="DISPATCHED",
        outcomeSha256=None,
    )
    rust = dict(
        operationId=op,
        claimSha256=sha(op + "claim"),
        preparationSha256=sha(op + "plan"),
        plannedSha256=sha(op + "plan"),
        beforeSha256=sha(op + "before"),
        afterSha256=sha(op + "after"),
        processPreparationSha256=sha(op + "private"),
        rowCount=1,
        state="APPLYING",
        outcomeSha256=None,
    )
    initial = dict(sha256=rust["beforeSha256"], inode=number * 10, mtimeNs=1)
    middle = dict(sha256=sha(op + "processing"), inode=number * 10 + 1, mtimeNs=2)
    final = dict(sha256=rust["afterSha256"], inode=initial["inode"], mtimeNs=3)
    result: dict[str, Any] = dict(
        status="PASS",
        sourceCommit=SOURCE,
        runId=RUN,
        backend=backend,
        mediaKind=kind,
        phase=phase,
        beforeCrashGo=go,
        beforeCrashRust=rust,
        afterRestartGo=dict(go, state="APPLIED", outcomeSha256=sha(op + "signed")),
        afterRestartRust=dict(rust, state="APPLIED", outcomeSha256=sha(op + "signed")),
        initialPublication=dict(initial),
        processingPhaseObserved=dict(publication=dict(middle), go=dict(go), rust=dict(rust)),
        publicationPredecessor=dict(initial if phase == "processing" else middle),
        publicationAtCrash=dict(middle if phase == "processing" else final),
        publicationAfterRecovery=dict(final),
        originalOperationId=op,
        originalOperationRepeated=False,
        writeRequestsSent=1,
        writeRequestsAfterRestart=0,
        writerBefore=2,
        writerAfter=3,
    )
    for key in [
        "workerSIGKILLDelivered",
        "allOriginalProcessesExited",
        "realRenameObserved",
        "originalSignedOutcomeRetained",
        "originalSourceIdentityRetained",
        "unrelatedMediaAndIndexRetained",
        "originalNamespaceMetadataRetained",
        "independentPublicReadsAndOriginalOracleVerified",
        "retainedAcrossAnotherCompleteRestart",
    ]:
        result[key] = True
    result["actualVectorBytesAndBindingsVerified"] = backend == "sqlite_vec"
    roles = ["deepseek-launch", "deepseek-launch", "deepseekd", "deepseek-launch", "deepseek-gateway", "deepseek-launch", "deepseek-worker"]
    for group, label in enumerate(
        ["empty-launchProcesses", "mutation-launchProcesses", "recovery-launchProcesses", "retained-launchProcesses"]
    ):
        result[label] = [
            dict(pid=1000 + number * 100 + group * 10 + index, startTicks=str(10000 + number * 100 + group * 10 + index), executable=role)
            for index, role in enumerate(roles)
        ]
    result["killSignals"] = [
        dict(pid=row["pid"], startTicks=row["startTicks"], signalDelivered=True) for row in result["mutation-launchProcesses"]
    ]
    return result


def corpus() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    cells = []
    number = 0
    for backend in ["sqlite", "sqlite_vec"]:
        for kind in KINDS:
            cases = []
            for phase in ["processing", "final"]:
                number += 1
                cases.append(case(number, backend, kind, phase))
            cells.append(
                dict(
                    kind="native-media-process-crash-v1",
                    status="PASS",
                    sourceCommit=SOURCE,
                    sourceTree=TREE,
                    runId=RUN,
                    event="workflow_dispatch",
                    attempt="1",
                    backend=backend,
                    selectedMediaKinds=[kind],
                    requiredCases=2,
                    completedCases=2,
                    selectedProcessingAndFinalPublicationSIGKILLQualified=True,
                    actualProcessingAndFinalPublicationSIGKILLQualified=False,
                    originalOperationAutonomousRecoveryQualified=False,
                    fullMigrationQualified=False,
                    fullProcessingPipelineQualified=False,
                    physicalPowerLossQualified=False,
                    allProcessingCrashPhasesQualified=False,
                    readiness="NOT_READY",
                    cases=cases,
                )
            )
    originals = {name: dict(status="PASS", sourceCommit=SOURCE) for name in ORIGINALS}
    return cells, originals


def qualify(cells: list[dict[str, Any]], originals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    module = importlib.import_module("scripts.verify_native_media_process_matrix")
    return module.qualify_matrix(cells, originals, source=SOURCE, tree=TREE, run=RUN)


def test_only_the_complete_original_nine_and_exact_twenty_four_actual_cases_qualify() -> None:
    cells, originals = corpus()
    result = qualify(cells, originals)
    assert result["completedCases"] == 24 and result["originalCasesPassed"] == 9
    assert result["actualProcessingAndFinalPublicationSIGKILLQualified"] is True
    assert result["fullMigrationQualified"] is False and result["readiness"] == "NOT_READY"


@pytest.mark.parametrize(
    "damage",
    [
        "missing-cell",
        "duplicate-cell",
        "missing-kind",
        "wrong-source",
        "wrong-tree",
        "wrong-run",
        "wrong-event",
        "failed-original",
        "missing-original",
        "missing-phase",
        "duplicate-phase",
        "failed-case",
        "skipped-case",
        "whole-backend-claim",
        "wrong-count",
        "bool-count",
        "unknown-kind",
        "false-selection",
    ],
)
def test_partial_or_mixed_candidates_never_replace_the_full_corpus(damage: str) -> None:
    cells, originals = corpus()
    if damage == "missing-cell":
        cells.pop()
    elif damage == "duplicate-cell":
        cells[-1] = copy.deepcopy(cells[0])
    elif damage == "missing-kind":
        cells[0]["selectedMediaKinds"] = []
    elif damage == "wrong-source":
        cells[0]["sourceCommit"] = "c" * 40
    elif damage == "wrong-tree":
        cells[0]["sourceTree"] = "c" * 40
    elif damage == "wrong-run":
        cells[0]["runId"] = "67890"
    elif damage == "wrong-event":
        cells[0]["event"] = "pull_request"
    elif damage == "failed-original":
        originals["process"]["status"] = "FAIL"
    elif damage == "missing-original":
        originals.pop("process")
    elif damage == "missing-phase":
        cells[0]["cases"].pop()
    elif damage == "duplicate-phase":
        cells[0]["cases"][1]["phase"] = "processing"
    elif damage in {"failed-case", "skipped-case"}:
        cells[0]["cases"][0]["status"] = "FAIL" if damage == "failed-case" else "SKIP"
    elif damage == "whole-backend-claim":
        cells[0]["actualProcessingAndFinalPublicationSIGKILLQualified"] = True
    elif damage == "wrong-count":
        cells[0]["completedCases"] = 1
    elif damage == "bool-count":
        cells[0]["completedCases"] = True
    elif damage == "unknown-kind":
        cells[0]["selectedMediaKinds"] = ["other"]
    elif damage == "false-selection":
        cells[0]["selectedProcessingAndFinalPublicationSIGKILLQualified"] = False
    with pytest.raises(ValueError):
        qualify(cells, originals)


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate-operation",
        "epoch",
        "bool-epoch",
        "claim",
        "inner-plan",
        "signature",
        "unknown-effect",
        "repeated-write",
        "no-kill",
        "foreign-data",
        "missing-restart",
        "missed-predecessor",
        "in-place-processing",
        "in-place-final",
        "rewritten-final",
        "wrong-file-hash",
        "wrong-phase-claim",
        "missing-process",
        "unowned-kill",
    ],
)
def test_each_real_case_retains_its_original_fences_effect_and_live_publication_predecessor(damage: str) -> None:
    cells, originals = corpus()
    current = cells[0]["cases"][1]
    if damage == "duplicate-operation":
        duplicate = copy.deepcopy(current)
        duplicate["mediaKind"] = cells[1]["selectedMediaKinds"][0]
        cells[1]["cases"][1] = duplicate
    elif damage == "epoch":
        current["afterRestartGo"]["epoch"] = 2
    elif damage == "bool-epoch":
        current["beforeCrashGo"]["epoch"] = True
    elif damage == "claim":
        current["afterRestartRust"]["claimSha256"] = sha("rebound")
    elif damage == "inner-plan":
        current["afterRestartRust"]["processPreparationSha256"] = sha("rebound")
    elif damage == "signature":
        current["afterRestartGo"]["outcomeSha256"] = sha("rebound")
    elif damage == "unknown-effect":
        current["afterRestartGo"]["state"] = "EFFECT_UNKNOWN"
    elif damage == "repeated-write":
        current["writeRequestsAfterRestart"] = 1
    elif damage == "no-kill":
        current["workerSIGKILLDelivered"] = False
    elif damage == "foreign-data":
        current["unrelatedMediaAndIndexRetained"] = False
    elif damage == "missing-restart":
        current["retainedAcrossAnotherCompleteRestart"] = False
    elif damage == "missed-predecessor":
        current["processingPhaseObserved"] = None
    elif damage == "in-place-processing":
        current["processingPhaseObserved"]["publication"]["inode"] = current["initialPublication"]["inode"]
    elif damage == "in-place-final":
        current["publicationAtCrash"]["inode"] = current["processingPhaseObserved"]["publication"]["inode"]
    elif damage == "rewritten-final":
        current["publicationAfterRecovery"] = dict(current["publicationAfterRecovery"], inode=999)
    elif damage == "wrong-file-hash":
        current["publicationAtCrash"]["sha256"] = sha("wrong")
    elif damage == "wrong-phase-claim":
        current["processingPhaseObserved"]["go"]["ownerSha256"] = sha("wrong")
    elif damage == "missing-process":
        current["mutation-launchProcesses"].pop()
    elif damage == "unowned-kill":
        current["killSignals"][0]["pid"] = 1
    with pytest.raises(ValueError):
        qualify(cells, originals)


def test_all_observations_cannot_omit_the_original_private_preparation_binding() -> None:
    cells, originals = corpus()
    current = cells[0]["cases"][0]
    for row in [current["beforeCrashRust"], current["afterRestartRust"], current["processingPhaseObserved"]["rust"]]:
        row.pop("processPreparationSha256")
    with pytest.raises(ValueError):
        qualify(cells, originals)


@pytest.mark.parametrize("damage", ["", "missing-context", "different-binary-context", "different-case-file", "extra-artifact"])
def test_cloud_aggregation_binds_the_exact_files_and_binary_context_before_qualifying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    module = importlib.import_module("scripts.verify_native_media_process_matrix")
    cells, originals = corpus()
    root, output = tmp_path / "inputs", tmp_path / "evidence/result.json"

    def write(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    context: dict[str, Any] = dict(
        sourceCommit=SOURCE,
        sourceTree=TREE,
        runId=RUN,
        requiredElfDependenciesResolved=True,
        binaries=[dict(name=name, sha256=sha(name), bytes=10) for name in ["deepseek-worker", "deepseek-gateway"]],
        files=[dict(path="ui/index.html", sha256=sha("ui"), bytes=10)],
    )
    write(root / "original/native-binaries.json", context)
    for name, value in originals.items():
        write(root / "original" / name / "result.json", value)
    for index, cell in enumerate(cells):
        folder = root / f"native-media-process-{cell['backend']}-{cell['selectedMediaKinds'][0]}-{SOURCE}"
        if not (damage == "missing-context" and index == 0):
            actual_context = copy.deepcopy(context)
            if damage == "different-binary-context" and index == 0:
                actual_context["binaries"][0]["sha256"] = sha("different")
            write(folder / "binary-context.json", actual_context)
        write(folder / "result.json", cell)
        for entry in cell["cases"]:
            actual_case = copy.deepcopy(entry)
            if damage == "different-case-file" and index == 0:
                actual_case["beforeCrashGo"]["epoch"] = True
            write(folder / (entry["mediaKind"] + "-" + entry["phase"]) / "result.json", actual_case)
    if damage == "extra-artifact":
        (root / "unrelated-binary-artifact").mkdir()
    for key, environment_value in {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_SHA": SOURCE,
        "DEEPSEEK_SOURCE_TREE": TREE,
        "GITHUB_RUN_ID": RUN,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
    }.items():
        monkeypatch.setenv(key, environment_value)
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", ["matrix", "--inputs", str(root), "--out", str(output)])
    assert module.main() == (1 if damage else 0)
    result = json.loads(output.read_bytes())
    assert result["fullMigrationQualified"] is False and result["readiness"] == "NOT_READY"
    if not damage:
        assert result["completedCases"] == 24
        assert all(len(member["sha256"]) == 64 for member in result["reportMembers"])


def test_process_matrix_workflow_retains_all_original_steps_and_requires_all_twelve_cells() -> None:
    import yaml

    root = Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / ".github/workflows/native-media-recovery.yml").read_bytes())
    main = data["jobs"]["media-recovery"]
    names = {
        "Bind the two source-built Rust binaries for all required PROCESS cells",
        "Share only native executables between cloud jobs",
        "Verify the full matrix evidence and macOS workflow source contracts",
    }
    preserved = [step for step in main["steps"] if step.get("name") not in names]
    assert len(preserved) == 29 and main["timeout-minutes"] == 60
    # Public SHA256 of the original workflow structure, not a credential.
    assert (
        hashlib.sha256(json.dumps(preserved, sort_keys=True).encode()).hexdigest()
        == "644bcd5206033eac13833b01c16d9b69c89752a332e99d89008c8f8784bf9b6a"  # pragma: allowlist secret
    )
    cell = data["jobs"]["media-process-recovery"]
    assert cell["needs"] == "media-recovery" and cell["timeout-minutes"] == 20
    assert cell["strategy"]["matrix"] == {"backend": ["sqlite", "sqlite_vec"], "media_kind": list(KINDS)}
    assert cell["strategy"]["fail-fast"] is False and cell["strategy"]["max-parallel"] == 4
    collect = data["jobs"]["media-process-evidence"]
    assert collect["if"] == "always()" and set(collect["needs"]) == {"media-recovery", "media-process-recovery"}


def test_each_matrix_cell_verifies_the_source_binaries_and_frontend_before_launching() -> None:
    import ast
    import re
    import yaml

    root = Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / ".github/workflows/native-media-recovery.yml").read_bytes())
    steps = data["jobs"]["media-process-recovery"]["steps"]
    source = "\n".join(step.get("run", "") for step in steps)
    for required in [
        "native-binaries.json",
        "sourceTree",
        "hashlib.file_digest",
        "receipt['files']",
        "ui/index.html",
        "requiredElfDependenciesResolved",
        "--media-kind",
        "--backend",
        "set -euo pipefail",
    ]:
        assert required in source
    for job in data["jobs"].values():
        assert not any("${{ runner." in str(value) for value in job.get("env", {}).values())
        for step in job["steps"]:
            for program in re.findall(r"python - <<'PY'\n(.*?)\nPY", step.get("run", ""), re.DOTALL):
                compile(program, "embedded-media-matrix.py", "exec")
                assert not any(isinstance(node, ast.Constant) and node.value == "\\n" for node in ast.walk(ast.parse(program)))
