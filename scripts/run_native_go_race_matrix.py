"""Run or combine the independent GitHub runner groups for one Go race variant."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any

from scripts.run_native_go_race import (
    GO_TIMEOUT,
    ROOT,
    STORE_PACKAGE,
    VARIANT_BUDGET_SECONDS,
    RaceGateError,
    catalog_tests,
    cloud_identity,
    cloud_packages,
    execute,
    inspect_events,
    partition_catalog,
    require_cloud_execution,
    run_pattern,
)

API_PACKAGE = STORE_PACKAGE.removesuffix("/store") + "/api"
SPLIT_GROUPS = {
    "store-a": (STORE_PACKAGE, range(4)),
    "store-b": (STORE_PACKAGE, range(4, 8)),
    "api-a": (API_PACKAGE, range(1)),
    "api-b": (API_PACKAGE, range(1, 2)),
}
RUNNER_GROUPS = (*SPLIT_GROUPS, "other")
GROUP_REPORT_KIND = "native-go-race-runner-group-v1"
MATRIX_REPORT_KIND = "native-complete-go-race-v2"
STORE_WEIGHTS = ROOT / "tests/native/go_race_store_weights_v1.json"


def shard_groups(package: str, names: list[str], timings: dict[str, Any]) -> list[list[str]]:
    if package == API_PACKAGE:
        return partition_catalog(names, 2)
    ordered = catalog_tests("\n".join(names))
    if len(ordered) < 8:
        raise RaceGateError("SHARD_PARTITION_INVALID")
    weights = timings["seconds"]
    fallback = timings["fallbackSeconds"]
    if type(fallback) is not int or fallback < 1 or any(type(value) is not int or value < 1 for value in weights.values()):
        raise RaceGateError("RACE_STORE_TIMING_WEIGHTS_INVALID")
    buckets: list[list[str]] = [[] for _ in range(8)]
    loads = [0] * len(buckets)
    for name in sorted(ordered, key=lambda item: (-weights.get(item, fallback), item)):
        bucket = min(range(len(buckets)), key=loads.__getitem__)
        buckets[bucket].append(name)
        loads[bucket] += weights.get(name, fallback)
    return [sorted(bucket) for bucket in buckets]


def other_packages(packages: list[str]) -> list[str]:
    return [package for package in packages if package not in (STORE_PACKAGE, API_PACKAGE)]


def group_selectors(
    group: str, report: dict[str, Any], baseline: dict[str, Any], timings: dict[str, Any]
) -> dict[str, dict[str, list[str]]]:
    if group == "other":
        required = {package: baseline["topLevelTests"].get(package, []) for package in other_packages(report["packages"])}
        return {"other.jsonl": required}
    package, indexes = SPLIT_GROUPS[group]
    catalog = report["catalog"]
    if catalog != catalog_tests("\n".join(catalog)) or not set(baseline["topLevelTests"][package]) <= set(catalog):
        raise RaceGateError("SPLIT_PACKAGE_CATALOG_INCOMPLETE")
    shards = shard_groups(package, catalog, timings)
    prefix = package.rsplit("/", 1)[-1]
    return {f"{prefix}-{index}.jsonl": {package: shards[index]} for index in indexes}


def inspect_group(
    group: str, report: dict[str, Any], source: Path, baseline: dict[str, Any], timings: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    selectors = group_selectors(group, report, baseline, timings)
    executions = report["executions"]
    if set(executions) != set(selectors):
        raise RaceGateError("RACE_GROUP_INVOCATIONS_DIFFER")
    verified = {}
    allowed_skips = set(baseline["allowedSkippedTests"])
    for filename, required in selectors.items():
        execution = executions[filename]
        stderr = Path(filename).with_suffix(".stderr.log").name
        if execution["exitCode"] != 0 or execution["timeout"] is not False:
            raise RaceGateError("RACE_COMMAND_FAILED")
        for key, expected in (("stdout", filename), ("stderr", stderr)):
            if execution[key] != expected or hashlib.sha256((source / expected).read_bytes()).hexdigest() != execution[key + "Sha256"]:
                raise RaceGateError("RACE_GROUP_LOG_DIGEST_DIFFERS")
        corpus = inspect_events(source / filename, set(required), required, allowed_skips, exact=group != "other")
        verified[filename] = {**execution, "corpus": corpus}
        if group != "other":
            verified[filename]["index"] = int(Path(filename).stem.rsplit("-", 1)[-1])
    return verified


def run_group(
    group: str, out: Path, report: dict[str, Any], identity: dict[str, Any],
    baseline: dict[str, Any], timings: dict[str, Any], deadline: float,
) -> None:
    packages = cloud_packages(identity, baseline)
    if API_PACKAGE not in packages:
        raise RaceGateError("GO_PACKAGE_CORPUS_INCOMPLETE")
    report.update(packages=packages, runnerGroup=group, executions={})
    if group == "other":
        report["executions"]["other.jsonl"] = execute(
            ["go", "test", "-race", "-json", "-count=1", "-timeout=" + GO_TIMEOUT, *other_packages(packages)],
            ROOT / "go", out / "other.jsonl", min(5460, deadline - time.monotonic()),
        )
    else:
        package, indexes = SPLIT_GROUPS[group]
        name = package.rsplit("/", 1)[-1]
        working_dir = ROOT / "go" / "internal" / name
        with tempfile.TemporaryDirectory(prefix="native-go-race-", dir=os.environ["RUNNER_TEMP"]) as temporary:
            binary = Path(temporary) / f"{name}-race.test"
            build = execute(["go", "test", "-race", "-c", "-o", str(binary), "./internal/" + name],
                            ROOT / "go", out / "build.log", min(600, deadline - time.monotonic()))
            report["build"] = build
            if build["exitCode"] != 0 or build["timeout"]:
                raise RaceGateError("RACE_BINARY_BUILD_FAILED")
            catalog = subprocess.check_output([str(binary), "-test.list=.*"], cwd=working_dir, timeout=60, text=True)
            report.update(catalog=catalog_tests(catalog), catalogRawSha256=hashlib.sha256(catalog.encode()).hexdigest(),
                          raceBinarySha256=hashlib.sha256(binary.read_bytes()).hexdigest())
            selectors = group_selectors(group, report, baseline, timings)

            def shard(filename: str) -> dict[str, Any]:
                command = ["go", "tool", "test2json", "-t", "-p", package, str(binary),
                           "-test.v=test2json", "-test.count=1", "-test.paniconexit0", "-test.timeout=" + GO_TIMEOUT,
                           "-test.run=" + run_pattern(selectors[filename][package])]
                return execute(command, working_dir, out / filename, min(5460, deadline - time.monotonic()))

            with ThreadPoolExecutor(max_workers=len(indexes)) as executor:
                report["executions"] = dict(zip(selectors, executor.map(shard, selectors), strict=True))
    report["executions"] = inspect_group(group, report, out, baseline, timings)


def verify_matrix(
    source: Path, out: Path, identity: dict[str, Any], baseline: dict[str, Any], timings: dict[str, Any]
) -> dict[str, Any]:
    directories = {group: source / f"go-race-shard-{identity['variant']}-{group}" for group in RUNNER_GROUPS}
    if set(source.iterdir()) != set(directories.values()):
        raise RaceGateError("RACE_RUNNER_GROUPS_INCOMPLETE")
    packages: list[str] = []
    catalogs: dict[str, list[str]] = {}
    reports: dict[str, Any] = {}
    executions: dict[str, Any] = {}
    bounds: list[tuple[int, int]] = []
    required_packages = set(baseline["packages"]) | {STORE_PACKAGE, API_PACKAGE}
    seen: set[tuple[str, str]] = set()
    skipped: set[str] = set()
    for group, directory in directories.items():
        group_report = json.loads((directory / "result.json").read_bytes())
        if (group_report.get("kind") != GROUP_REPORT_KIND or group_report.get("status") != "PASS"
                or group_report.get("runnerGroup") != group
                or any(group_report.get(key) != value for key, value in identity.items())):
            raise RaceGateError("RACE_RUNNER_GROUP_IDENTITY_DIFFERS")
        declared = group_report["packages"]
        if len(declared) != len(set(declared)) or not required_packages <= set(declared):
            raise RaceGateError("GO_PACKAGE_CORPUS_INCOMPLETE")
        if packages and packages != declared:
            raise RaceGateError("RACE_RUNNER_PACKAGE_LISTS_DIFFER")
        packages = declared
        started, finished = group_report["startedUnixMs"], group_report["finishedUnixMs"]
        if type(started) is not int or type(finished) is not int or not 0 < started <= finished:
            raise RaceGateError("RACE_RUNNER_TIMING_INVALID")
        bounds.append((started, finished))
        if group in SPLIT_GROUPS:
            package, _ = SPLIT_GROUPS[group]
            catalog = group_report["catalog"]
            if catalogs.setdefault(package, catalog) != catalog:
                raise RaceGateError("RACE_RUNNER_TEST_CATALOGS_DIFFER")
        verified = inspect_group(group, group_report, directory, baseline, timings)
        for filename, execution in verified.items():
            corpus = execution["corpus"]
            tests = {(pkg, name) for pkg, names in corpus["startedTests"].items() for name in names}
            if seen & tests or filename in executions:
                raise RaceGateError("RACE_RUNNER_TESTS_DUPLICATED")
            seen.update(tests)
            skipped.update(corpus["skippedTests"])
            for log in (filename, execution["stderr"]):
                shutil.copyfile(directory / log, out / log)
        executions.update(verified)
        reports[group] = {"artifact": directory.name, **{
            key: group_report[key] for key in ("build", "catalogRawSha256", "raceBinarySha256") if key in group_report
        }}

    store_catalog = catalogs[STORE_PACKAGE]
    api_catalog = catalogs[API_PACKAGE]
    expected = {(package, name) for package, catalog in catalogs.items() for name in catalog}
    if not expected <= seen:
        raise RaceGateError("AGGREGATE_CORPUS_COUNT_DIFFERS")
    elapsed_ms = max(finished for _, finished in bounds) - min(started for started, _ in bounds)
    if elapsed_ms > VARIANT_BUDGET_SECONDS * 1000:
        raise RaceGateError("VARIANT_WALL_CLOCK_BUDGET_EXHAUSTED")
    return {
        "completeRaceCorpusQualified": True, "testResultCacheUsed": False,
        "variantElapsedSeconds": elapsed_ms / 1000,
        "packages": packages, "topLevelTests": len(seen), "skippedTests": sorted(skipped),
        "storeCatalog": store_catalog, "storeGroups": shard_groups(STORE_PACKAGE, store_catalog, timings),
        "storeExecutions": [executions[f"store-{i}.jsonl"] for i in range(8)],
        "apiCatalog": api_catalog, "apiGroups": shard_groups(API_PACKAGE, api_catalog, timings),
        "apiExecutions": [executions[f"api-{i}.jsonl"] for i in range(2)],
        "otherExecution": executions["other.jsonl"], "otherCorpus": executions["other.jsonl"]["corpus"],
        "runnerGroups": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=["default", "android"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--group", choices=RUNNER_GROUPS)
    mode.add_argument("--merge-from", type=Path)
    args = parser.parse_args()
    deadline = time.monotonic() + VARIANT_BUDGET_SECONDS
    started = time.time_ns() // 1_000_000
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = dict(kind=GROUP_REPORT_KIND if args.group else MATRIX_REPORT_KIND,
                  status="FAIL", variant=args.variant, completeRaceCorpusQualified=False,
                  coverageQualified=False, fullMigrationQualified=False, readiness="NOT_READY")
    try:
        require_cloud_execution()
        identity, baseline = cloud_identity(args.variant)
        if not identity["runId"] or not identity["event"]:
            raise RaceGateError("GITHUB_RUN_IDENTITY_REQUIRED")
        encoded = STORE_WEIGHTS.read_bytes()
        identity["storeWeightsSha256"] = hashlib.sha256(encoded).hexdigest()
        timings = json.loads(encoded)
        report.update(identity)
        if args.group:
            report["startedUnixMs"] = started
            run_group(args.group, out, report, identity, baseline, timings, deadline)
            report["finishedUnixMs"] = time.time_ns() // 1_000_000
        else:
            report.update(verify_matrix(args.merge_from, out, identity, baseline, timings))
        report["status"] = "PASS"
    except Exception as error:
        report.update(status="FAIL", completeRaceCorpusQualified=False,
                      failureCode=str(error) if isinstance(error, RaceGateError) else type(error).__name__)
    (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "variant", "runnerGroup", "failureCode", "topLevelTests")}), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
