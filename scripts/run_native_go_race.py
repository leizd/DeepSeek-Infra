"""Run the complete Go race corpus with bounded, exhaustive store shards.

Only the large store package is split. All store shards use the SAME race-built
binary, -test.count=1, anchored top-level selectors, and the original 90m Go
timeout per invocation. Every listed Test/Fuzz seed/Example must finish once.
Other packages run once through ordinary go test -race -json without a filter.
See https://pkg.go.dev/cmd/test2json for the single-binary JSON framing.
Native execution is cloud-only. The parser and partition functions are offline.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STORE_PACKAGE = "github.com/leizd/DeepSeek-Infra/go/internal/store"
GO_TIMEOUT = "90m"


class RaceGateError(RuntimeError):
    pass


def catalog_tests(output: str) -> list[str]:
    names: list[str] = []
    for line in output.splitlines():
        if line.startswith("Benchmark") and line.isidentifier():
            continue  # go test without -bench does not execute benchmarks.
        if not line.startswith(("Test", "Fuzz", "Example")) or not line.isidentifier():
            raise RaceGateError("TEST_CATALOG_INVALID")
        names.append(line)
    if not names or len(names) != len(set(names)):
        raise RaceGateError("TEST_CATALOG_EMPTY_OR_DUPLICATED")
    return sorted(names)


def partition_catalog(names: list[str], count: int) -> list[list[str]]:
    if not 1 <= count <= 16 or len(names) < count or len(names) != len(set(names)):
        raise RaceGateError("SHARD_PARTITION_INVALID")
    ordered = catalog_tests("\n".join(names))
    groups = [ordered[index::count] for index in range(count)]
    if Counter(name for group in groups for name in group) != Counter(names):
        raise RaceGateError("SHARD_PARTITION_NOT_EXHAUSTIVE")
    return groups


def run_pattern(names: list[str]) -> str:
    # Go identifiers contain no RE2 metacharacters. The outer anchors prevent a
    # selected parent from also running a similarly prefixed sibling test.
    if catalog_tests("\n".join(names)) != sorted(names):
        raise RaceGateError("SHARD_SELECTOR_INVALID")
    return "^(" + "|".join(names) + ")$"


def inspect_events(path: Path, packages: set[str], required: dict[str, list[str]],
                   allowed_skips: set[str], *, exact: bool) -> dict[str, Any]:
    started: Counter[tuple[str, str]] = Counter()
    finished: Counter[tuple[str, str]] = Counter()
    child_started: Counter[tuple[str, str]] = Counter()
    child_finished: Counter[tuple[str, str]] = Counter()
    terminal: dict[tuple[str, str], str] = {}
    package_terminal: dict[str, list[str]] = {}
    skipped: set[str] = set()
    subtests = 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise RaceGateError("RACE_JSON_STREAM_INVALID") from error
            if not isinstance(row, dict):
                raise RaceGateError("RACE_JSON_EVENT_INVALID")
            package, name, action = row.get("Package"), row.get("Test"), row.get("Action")
            if not package:
                continue  # Go build events are not package test outcomes.
            if package not in packages:
                raise RaceGateError("UNEXPECTED_TEST_PACKAGE")
            if action == "fail":
                raise RaceGateError("RACE_TEST_FAILED")
            if "(cached)" in row.get("Output", ""):
                raise RaceGateError("CACHED_TEST_RESULT_FORBIDDEN")
            if action in {"run", "pass", "skip"} and (not isinstance(row.get("Time"), str) or not row["Time"]):
                raise RaceGateError("FRESH_TEST_TIMESTAMP_MISSING")
            if not name and action in {"pass", "skip"}:
                package_terminal.setdefault(package, []).append(action)
            if not name:
                continue
            if not isinstance(name, str):
                raise RaceGateError("TEST_NAME_INVALID")
            if action == "skip":
                identity = package + ":" + name
                if identity not in allowed_skips:
                    raise RaceGateError("UNREVIEWED_TEST_SKIP")
                skipped.add(identity)
            if "/" in name:
                subtests += int(action == "run")
                if action == "run":
                    child_started[(package, name)] += 1
                if action in {"pass", "skip"}:
                    child_finished[(package, name)] += 1
                continue
            identity = (package, name)
            if action == "run":
                started[identity] += 1
            if action in {"pass", "skip"}:
                finished[identity] += 1
                terminal[identity] = action
    expected = {(package, name) for package, names in required.items() for name in names}
    if not expected <= set(started) or (exact and set(started) != expected):
        raise RaceGateError("TEST_CORPUS_INCOMPLETE_OR_FILTER_LEAK")
    if started != finished or any(count != 1 for count in started.values()):
        raise RaceGateError("TEST_TERMINAL_MISSING_OR_DUPLICATED")
    if child_started != child_finished or any(count != 1 for count in child_started.values()):
        raise RaceGateError("SUBTEST_TERMINAL_MISSING_OR_DUPLICATED")
    if set(package_terminal) != packages or any(len(states) != 1 for states in package_terminal.values()):
        raise RaceGateError("PACKAGE_TERMINAL_MISSING_OR_DUPLICATED")
    if any(states == ["skip"] and any(pkg == package for pkg, _ in started)
           for package, states in package_terminal.items()):
        raise RaceGateError("EXECUTED_PACKAGE_REPORTED_SKIPPED")
    return {"status": "PASS", "packages": sorted(packages), "topLevelTests": len(started),
            "passedTopLevelTests": sum(value == "pass" for value in terminal.values()),
            "skippedTests": sorted(skipped), "subtestsStarted": subtests,
            "startedTests": {package: sorted(name for pkg, name in started if pkg == package) for package in sorted(packages)}}


def execute(command: list[str], cwd: Path, output: Path, timeout: float) -> dict[str, Any]:
    if sys.platform == "win32":
        raise RaceGateError("NATIVE_GO_RACE_EXECUTION_IS_CLOUD_ONLY")
    if timeout <= 0:
        raise RaceGateError("VARIANT_WALL_CLOCK_BUDGET_EXHAUSTED")
    error_path = output.with_suffix(".stderr.log")
    start = time.monotonic()
    with output.open("xb") as stdout, error_path.open("xb") as stderr:
        child = subprocess.Popen(command, cwd=cwd, stdout=stdout, stderr=stderr, start_new_session=True)
        timed_out = False
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = child.wait(timeout=15)
    result = {"command": command, "exitCode": code, "timeout": timed_out,
              "elapsedSeconds": round(time.monotonic() - start, 3), "stdout": output.name,
              "stdoutSha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "stderr": error_path.name, "stderrSha256": hashlib.sha256(error_path.read_bytes()).hexdigest()}
    print(json.dumps({key: result[key] for key in ("stdout", "exitCode", "timeout", "elapsedSeconds")}), flush=True)
    return result


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or not sys.platform.startswith("linux"):
        raise RaceGateError("NATIVE_GO_RACE_EXECUTION_IS_CLOUD_ONLY")
    deadline = time.monotonic() + 9000
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=30).strip()
    if revision != os.environ.get("GITHUB_SHA"):
        raise RaceGateError("EXACT_CLOUD_REVISION_REQUIRED")
    toolchain = json.loads((ROOT / "release/native_runtime_toolchain_v1.json").read_text())["go"]["version"]
    if subprocess.check_output(["go", "env", "GOVERSION"], text=True, timeout=30).strip() != "go" + toolchain:
        raise RaceGateError("GO_TOOLCHAIN_PIN_DIFFERS")
    flags = os.environ.get("GOFLAGS", "")
    expected_flags = "" if args.variant == "default" else "-tags=deepseek_android,sqlite3_flock"
    if flags != expected_flags or subprocess.check_output(["go", "env", "CGO_ENABLED"], text=True, timeout=30).strip() != "1":
        raise RaceGateError("GO_RACE_VARIANT_CONFIGURATION_INVALID")
    baseline_path = ROOT / "tests/native/go_race_baseline_v1.json"
    baseline = json.loads(baseline_path.read_text())["variants"][args.variant]
    packages = subprocess.check_output(["go", "list", "./..."], cwd=ROOT / "go", text=True, timeout=120).splitlines()
    if len(packages) != len(set(packages)) or not set(baseline["packages"]) <= set(packages) or STORE_PACKAGE not in packages:
        raise RaceGateError("GO_PACKAGE_CORPUS_INCOMPLETE")
    report.update(sourceCommit=revision, goVersion=toolchain, goFlags=flags,
                  sourceTree=subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True, timeout=30).strip(),
                  goSourceTree=subprocess.check_output(["git", "rev-parse", "HEAD:go"], cwd=ROOT, text=True, timeout=30).strip(),
                  baselineSha256=hashlib.sha256(baseline_path.read_bytes()).hexdigest(), packages=packages,
                  originalGoTimeoutPerInvocation=GO_TIMEOUT, variantWallClockBudgetSeconds=9000,
                  originalBaselineStoreTests=len(baseline["topLevelTests"][STORE_PACKAGE]))
    with tempfile.TemporaryDirectory(prefix="native-go-race-", dir=os.environ["RUNNER_TEMP"]) as temporary:
        binary = Path(temporary) / "store-race.test"
        build = execute(["go", "test", "-race", "-c", "-o", str(binary), "./internal/store"],
                        ROOT / "go", args.out / "build.log", min(600, deadline - time.monotonic()))
        report["build"] = build
        if build["exitCode"] != 0:
            raise RaceGateError("RACE_BINARY_BUILD_FAILED")
        catalog_raw = subprocess.check_output([str(binary), "-test.list=.*"], cwd=ROOT / "go/internal/store", timeout=60, text=True)
        names = catalog_tests(catalog_raw)
        if not set(baseline["topLevelTests"][STORE_PACKAGE]) <= set(names):
            raise RaceGateError("ORIGINAL_STORE_TESTS_MISSING")
        groups = partition_catalog(names, args.store_shards)
        report.update(raceBinarySha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                      storeCatalogRawSha256=hashlib.sha256(catalog_raw.encode()).hexdigest(),
                      storeCatalog=names, storeGroups=groups, testResultCacheUsed=False,
                      maxParallelStoreProcesses=args.max_parallel)
        others = [package for package in packages if package != STORE_PACKAGE]
        other = execute(["go", "test", "-race", "-json", "-count=1", "-timeout=" + GO_TIMEOUT, *others],
                        ROOT / "go", args.out / "other.jsonl", min(5460, deadline - time.monotonic()))
        report["otherExecution"] = other

        def shard(index: int) -> dict[str, Any]:
            path = args.out / f"store-{index}.jsonl"
            command = ["go", "tool", "test2json", "-t", "-p", STORE_PACKAGE, str(binary),
                       "-test.v=test2json", "-test.count=1", "-test.paniconexit0", "-test.timeout=" + GO_TIMEOUT,
                       "-test.run=" + run_pattern(groups[index])]
            result = execute(command, ROOT / "go/internal/store", path, min(5460, deadline - time.monotonic()))
            result["index"] = index
            if result["exitCode"] == 0:
                try:
                    result["corpus"] = inspect_events(path, {STORE_PACKAGE}, {STORE_PACKAGE: groups[index]},
                                                      set(baseline["allowedSkippedTests"]), exact=True)
                except RaceGateError as error:
                    result["verificationFailure"] = str(error)
            return result

        with ThreadPoolExecutor(max_workers=args.max_parallel) as executor:
            # map preserves group identity; the processes themselves run with a
            # bounded worker pool. No timed-out or failed group is retried.
            report["storeExecutions"] = list(executor.map(shard, range(len(groups))))
        if other["exitCode"] != 0 or any(result["exitCode"] != 0 or "corpus" not in result for result in report["storeExecutions"]):
            raise RaceGateError("RACE_COMMAND_FAILED")
        report["otherCorpus"] = inspect_events(args.out / "other.jsonl", set(others),
            {package: baseline["topLevelTests"].get(package, []) for package in others},
            set(baseline["allowedSkippedTests"]), exact=False)
        expected_count = len(names) + report["otherCorpus"]["topLevelTests"]
        observed_count = sum(result["corpus"]["topLevelTests"] for result in report["storeExecutions"]) + report["otherCorpus"]["topLevelTests"]
        if observed_count != expected_count:
            raise RaceGateError("AGGREGATE_CORPUS_COUNT_DIFFERS")
        report.update(status="PASS", completeRaceCorpusQualified=True, topLevelTests=observed_count,
                      skippedTests=sorted(set(report["otherCorpus"]["skippedTests"]).union(
                          *(set(result["corpus"]["skippedTests"]) for result in report["storeExecutions"]))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=["default", "android"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--store-shards", type=int, default=4, choices=range(1, 17))
    parser.add_argument("--max-parallel", type=int, default=2, choices=range(1, 3))
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"kind": "native-complete-go-race-v1", "status": "FAIL", "variant": args.variant,
        "sourceCommit": os.environ.get("GITHUB_SHA"), "runId": os.environ.get("GITHUB_RUN_ID"),
        "event": os.environ.get("GITHUB_EVENT_NAME"), "completeRaceCorpusQualified": False,
        "coverageQualified": False, "fullMigrationQualified": False, "readiness": "NOT_READY"}
    try:
        run(args, report)
        # Keep the previous aggregate entry points, alongside per-shard raw logs.
        for suffix in ("jsonl", "stderr.log"):
            target = args.out.parent / (args.variant + "." + suffix)
            with target.open("xb") as combined:
                for path in sorted(args.out.glob("*." + suffix)):
                    if not path.stem.startswith("build"):
                        combined.write(path.read_bytes())
            report["aggregate" + suffix] = {"path": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    except Exception as error:
        report.update(status="FAIL", completeRaceCorpusQualified=False,
                      failureCode=str(error) if isinstance(error, RaceGateError) else type(error).__name__)
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "variant", "failureCode", "topLevelTests")}), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
