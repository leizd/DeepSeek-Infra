from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import run_native_go_race_matrix as matrix
from scripts.run_native_go_race import STORE_PACKAGE, VARIANT_BUDGET_SECONDS, RaceGateError

WORKER_PACKAGE = matrix.API_PACKAGE.removesuffix("/api") + "/worker"
PACKAGES = sorted([STORE_PACKAGE, matrix.API_PACKAGE, WORKER_PACKAGE])
CATALOGS = {
    STORE_PACKAGE: [f"TestStore{i}" for i in range(8)],
    matrix.API_PACKAGE: [f"TestAPI{i}" for i in range(4)],
    WORKER_PACKAGE: ["TestWorker"],
}
BASELINE = {
    "packages": PACKAGES,
    "topLevelTests": CATALOGS,
    "allowedSkippedTests": [f"{STORE_PACKAGE}:TestStore7"],
}
TIMINGS = {"fallbackSeconds": 10, "seconds": {"TestStore0": 100}}


def event(package: str, action: str, name: str | None = None) -> dict:
    row = {"Time": "2026-11-27T07:00:00Z", "Package": package, "Action": action}
    return row if name is None else {**row, "Test": name}


def write_stream(directory: Path, filename: str, required: dict[str, list[str]]) -> dict:
    rows = []
    for package, names in required.items():
        rows.append(event(package, "start"))
        for name in names:
            rows.append(event(package, "run", name))
            outcome = "skip" if f"{package}:{name}" in BASELINE["allowedSkippedTests"] else "pass"
            rows.append(event(package, outcome, name))
        rows.append(event(package, "pass"))
    raw = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
    (directory / filename).write_bytes(raw)
    stderr = Path(filename).with_suffix(".stderr.log").name
    (directory / stderr).write_bytes(b"")
    return {"stdout": filename, "stderr": stderr, "stdoutSha256": hashlib.sha256(raw).hexdigest(),
            "stderrSha256": hashlib.sha256(b"").hexdigest(), "exitCode": 0, "timeout": False}


def make_reports(tmp_path: Path, variant: str) -> tuple[Path, Path, dict]:
    outside = tmp_path / "groups"
    outside.mkdir()
    out = tmp_path / "combined"
    out.mkdir()
    identity = {
        "sourceCommit": "source", "sourceTree": "tree", "goSourceTree": "go-tree",
        "runId": "run", "variant": variant, "goVersion": "1.27.1", "event": "pull_request",
        "goFlags": "-tags=deepseek_android,sqlite3_flock" if variant == "android" else "",
        "baselineSha256": "baseline", "storeWeightsSha256": "weights",
    }
    for group in matrix.RUNNER_GROUPS:
        directory = outside / f"go-race-shard-{variant}-{group}"
        directory.mkdir()
        record = dict(identity, kind=matrix.GROUP_REPORT_KIND, status="PASS", runnerGroup=group,
                      packages=PACKAGES, startedUnixMs=1000, finishedUnixMs=3000)
        if group in matrix.SPLIT_GROUPS:
            package, _ = matrix.SPLIT_GROUPS[group]
            record["catalog"] = CATALOGS[package]
        record["executions"] = {
            file: write_stream(directory, file, expected)
            for file, expected in matrix.group_selectors(group, record, BASELINE, TIMINGS).items()
        }
        (directory / "result.json").write_text(json.dumps(record), encoding="utf-8")
    return outside, out, identity


def rewrite_report(source: Path, group: str, **changes: object) -> None:
    path = source / f"go-race-shard-default-{group}" / "result.json"
    report = json.loads(path.read_bytes())
    report.update(changes)
    path.write_text(json.dumps(report), encoding="utf-8")


@pytest.mark.parametrize("variant", ["default", "android"])
def test_matrix_verifies_every_split_and_unsplit_package(tmp_path: Path, variant: str) -> None:
    source, out, identity = make_reports(tmp_path, variant)
    result = matrix.verify_matrix(source, out, identity, BASELINE, TIMINGS)
    assert result["completeRaceCorpusQualified"] is True
    assert result["testResultCacheUsed"] is False
    assert result["variantElapsedSeconds"] == 2
    assert result["packages"] == PACKAGES
    assert result["topLevelTests"] == 13
    assert result["skippedTests"] == BASELINE["allowedSkippedTests"]
    assert [execution["index"] for execution in result["storeExecutions"]] == list(range(8))
    assert [execution["index"] for execution in result["apiExecutions"]] == [0, 1]
    assert result["otherCorpus"]["startedTests"] == {WORKER_PACKAGE: ["TestWorker"]}
    assert (out / "api-0.jsonl").is_file()
    assert (out / "other.stderr.log").is_file()


def test_matrix_refuses_an_incomplete_runner_set(tmp_path: Path) -> None:
    source, out, identity = make_reports(tmp_path, "default")
    (source / "go-race-shard-default-api-b").rename(tmp_path / "omitted")
    with pytest.raises(RaceGateError, match="RACE_RUNNER_GROUPS_INCOMPLETE"):
        matrix.verify_matrix(source, out, identity, BASELINE, TIMINGS)


@pytest.mark.parametrize(("field", "value"), [
    ("status", "FAIL"), ("sourceCommit", "previous"), ("runId", "other"),
    ("runnerGroup", "api-a"), ("goFlags", "-tags=deepseek_android,sqlite3_flock"),
    ("storeWeightsSha256", "stale"), ("baselineSha256", "incomplete"),
])
def test_matrix_checks_group_identity_and_success(tmp_path: Path, field: str, value: str) -> None:
    source, out, identity = make_reports(tmp_path, "default")
    rewrite_report(source, "store-b", **{field: value})
    with pytest.raises(RaceGateError, match="RACE_RUNNER_GROUP_IDENTITY_DIFFERS"):
        matrix.verify_matrix(source, out, identity, BASELINE, TIMINGS)


def test_matrix_refuses_conflicting_split_catalogs(tmp_path: Path) -> None:
    source, out, identity = make_reports(tmp_path, "default")
    rewrite_report(source, "api-b", catalog=CATALOGS[matrix.API_PACKAGE] + ["TestAPIAdded"])
    with pytest.raises(RaceGateError, match="RACE_RUNNER_TEST_CATALOGS_DIFFER"):
        matrix.verify_matrix(source, out, identity, BASELINE, TIMINGS)


def test_matrix_refuses_a_valid_log_for_the_wrong_selector(tmp_path: Path) -> None:
    source, out, identity = make_reports(tmp_path, "default")
    directory = source / "go-race-shard-default-api-b"
    wrong = write_stream(directory, "api-1.jsonl", {matrix.API_PACKAGE: ["TestAPI0", "TestAPI2"]})
    rewrite_report(source, "api-b", executions={"api-1.jsonl": wrong})
    with pytest.raises(RaceGateError, match="TEST_CORPUS_INCOMPLETE_OR_FILTER_LEAK"):
        matrix.verify_matrix(source, out, identity, BASELINE, TIMINGS)


def test_matrix_checks_log_digests(tmp_path: Path) -> None:
    source, out, identity = make_reports(tmp_path, "default")
    (source / "go-race-shard-default-other" / "other.stderr.log").write_bytes(b"changed")
    with pytest.raises(RaceGateError, match="RACE_GROUP_LOG_DIGEST_DIFFERS"):
        matrix.verify_matrix(source, out, identity, BASELINE, TIMINGS)


def test_matrix_enforces_the_variant_wide_wall_budget(tmp_path: Path) -> None:
    source, out, identity = make_reports(tmp_path, "default")
    rewrite_report(source, "api-b", finishedUnixMs=1001 + VARIANT_BUDGET_SECONDS * 1000)
    with pytest.raises(RaceGateError, match="VARIANT_WALL_CLOCK_BUDGET_EXHAUSTED"):
        matrix.verify_matrix(source, out, identity, BASELINE, TIMINGS)


def test_weighted_store_partitions_exhaust_every_test_including_new_cases() -> None:
    names = [f"TestStore{i:02d}" for i in range(16)] + ["TestAddedAfterTimingSample"]
    timings = {"fallbackSeconds": 10, "seconds": {"TestStore00": 70, "TestStore08": 60}}
    groups = matrix.shard_groups(STORE_PACKAGE, names, timings)
    assert sorted(name for group in groups for name in group) == sorted(names)
    assert groups == matrix.shard_groups(STORE_PACKAGE, list(reversed(names)), timings)
    assert not any({"TestStore00", "TestStore08"} <= set(group) for group in groups)
