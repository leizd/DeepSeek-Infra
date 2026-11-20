"""Offline safeguards against missing, duplicated, cached or newly skipped cases."""
from __future__ import annotations

from collections import Counter
import importlib.util
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_go_race", ROOT / "scripts/run_native_go_race.py")
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
PACKAGE = "fixture/store"


def write_events(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def event(action: str, name: str | None = None, **fields) -> dict:
    return {"Time": "2026-10-20T08:00:00Z", "Package": PACKAGE, "Action": action,
            **({"Test": name} if name else {}), **fields}


def complete_events() -> list[dict]:
    return [event("start"), event("run", "TestParent"), event("run", "TestParent/child"),
            event("pass", "TestParent/child"), event("pass", "TestParent"),
            event("run", "FuzzSeed"), event("pass", "FuzzSeed"),
            event("run", "ExampleValue"), event("pass", "ExampleValue"), event("pass")]


REQUIRED = {PACKAGE: ["TestParent", "FuzzSeed", "ExampleValue"]}


def test_store_partitions_retain_every_original_default_and_android_case() -> None:
    baseline = json.loads((ROOT / "tests/native/go_race_baseline_v1.json").read_text())
    assert baseline["sourceCommit"] == "95adf8c250f31cdb786be642e729bcc1bb4dc39a"
    assert baseline["runId"] == 39354290366
    assert baseline["artifactId"] == 12239309163
    for variant, count in [("default", 716), ("android", 717)]:
        names = baseline["variants"][variant]["topLevelTests"][gate.STORE_PACKAGE]
        assert len(names) == count
        groups = gate.partition_catalog(names, 4)
        assert Counter(name for group in groups for name in group) == Counter(names)
        assert max(map(len, groups)) - min(map(len, groups)) <= 1
        for group in groups:
            selector = re.compile(gate.run_pattern(group))
            assert {name for name in names if selector.fullmatch(name)} == set(group)
            assert all(selector.fullmatch(name + "UnexpectedSibling") is None for name in group)


def test_catalog_includes_fuzz_seed_and_example_and_excludes_benchmarks() -> None:
    names = gate.catalog_tests("TestUnicode_中文\nFuzzSeed\nExampleValue\nBenchmarkLarge\n")
    assert names == ["ExampleValue", "FuzzSeed", "TestUnicode_中文"]
    assert gate.partition_catalog(list(reversed(names)), 2) == gate.partition_catalog(names, 2)


@pytest.mark.parametrize("output", ["", "TestOne\nTestOne", "TestOne/child", "warning: race", "TestOne\n\nExampleValue", "TestOne.*", "BenchmarkOnly"])
def test_invalid_or_incomplete_catalog_is_refused(output: str) -> None:
    with pytest.raises(gate.RaceGateError):
        gate.catalog_tests(output)


@pytest.mark.parametrize("count", [0, -1, 3, 17])
def test_empty_or_invalid_partitions_are_refused(count: int) -> None:
    with pytest.raises(gate.RaceGateError):
        gate.partition_catalog(["TestA", "FuzzB"], count)


def test_fresh_complete_events_are_accepted(tmp_path: Path) -> None:
    result = gate.inspect_events(write_events(tmp_path, complete_events()), {PACKAGE}, REQUIRED, set(), exact=True)
    assert result["topLevelTests"] == result["passedTopLevelTests"] == 3
    assert result["subtestsStarted"] == 1
    assert result["skippedTests"] == []


@pytest.mark.parametrize(("change", "code"), [
    ("missing-required", "TEST_CORPUS_INCOMPLETE_OR_FILTER_LEAK"),
    ("missing-terminal", "TEST_TERMINAL_MISSING_OR_DUPLICATED"),
    ("missing-subtest-terminal", "SUBTEST_TERMINAL_MISSING_OR_DUPLICATED"),
    ("duplicate-test", "TEST_TERMINAL_MISSING_OR_DUPLICATED"),
    ("duplicate-package", "PACKAGE_TERMINAL_MISSING_OR_DUPLICATED"),
    ("missing-package", "PACKAGE_TERMINAL_MISSING_OR_DUPLICATED"),
    ("unexpected-package", "UNEXPECTED_TEST_PACKAGE"),
    ("filter-leak", "TEST_CORPUS_INCOMPLETE_OR_FILTER_LEAK"),
    ("new-skip", "UNREVIEWED_TEST_SKIP"),
    ("subtest-skip", "UNREVIEWED_TEST_SKIP"),
    ("cached", "CACHED_TEST_RESULT_FORBIDDEN"),
    ("missing-time", "FRESH_TEST_TIMESTAMP_MISSING"),
    ("failed", "RACE_TEST_FAILED"),
])
def test_partial_or_unreviewed_results_cannot_qualify(tmp_path: Path, change: str, code: str) -> None:
    rows = complete_events()
    if change == "missing-required":
        rows = [row for row in rows if row.get("Test") != "FuzzSeed"]
    elif change == "missing-terminal":
        rows = [row for row in rows if not (row.get("Test") == "FuzzSeed" and row["Action"] == "pass")]
    elif change == "missing-subtest-terminal":
        rows = [row for row in rows if not (row.get("Test") == "TestParent/child" and row["Action"] == "pass")]
    elif change == "duplicate-test":
        rows += [event("run", "FuzzSeed"), event("pass", "FuzzSeed")]
    elif change == "duplicate-package":
        rows.append(event("pass"))
    elif change == "missing-package":
        rows.pop()
    elif change == "unexpected-package":
        rows[0]["Package"] = "unplanned/package"
    elif change == "filter-leak":
        rows += [event("run", "TestParentExtra"), event("pass", "TestParentExtra")]
    elif change in {"new-skip", "subtest-skip"}:
        name = "FuzzSeed" if change == "new-skip" else "TestParent/child"
        next(row for row in rows if row.get("Test") == name and row["Action"] == "pass")["Action"] = "skip"
    elif change == "cached":
        rows.insert(0, event("output", Output="ok fixture/store (cached)\n"))
    elif change == "missing-time":
        del rows[-1]["Time"]
    elif change == "failed":
        rows[-1]["Action"] = "fail"
    with pytest.raises(gate.RaceGateError, match="^" + code + "$"):
        gate.inspect_events(write_events(tmp_path, rows), {PACKAGE}, REQUIRED, set(), exact=True)


def test_original_reviewed_skip_is_visible_and_new_skips_still_fail(tmp_path: Path) -> None:
    rows = complete_events()
    next(row for row in rows if row.get("Test") == "FuzzSeed" and row["Action"] == "pass")["Action"] = "skip"
    result = gate.inspect_events(write_events(tmp_path, rows), {PACKAGE}, REQUIRED,
                                 {PACKAGE + ":FuzzSeed"}, exact=True)
    assert result["topLevelTests"] == 3 and result["passedTopLevelTests"] == 2
    assert result["skippedTests"] == [PACKAGE + ":FuzzSeed"]


def test_new_unfiltered_package_cases_are_retained_while_original_cases_are_required(tmp_path: Path) -> None:
    rows = complete_events()
    rows += [event("run", "TestAdded"), event("pass", "TestAdded")]
    result = gate.inspect_events(write_events(tmp_path, rows), {PACKAGE}, REQUIRED, set(), exact=False)
    assert result["topLevelTests"] == 4
    assert "TestAdded" in result["startedTests"][PACKAGE]


def test_packages_with_no_test_files_remain_explicit(tmp_path: Path) -> None:
    result = gate.inspect_events(write_events(tmp_path, [event("start"), event("skip")]),
                                 {PACKAGE}, {PACKAGE: []}, set(), exact=False)
    assert result["packages"] == [PACKAGE] and result["topLevelTests"] == 0


def test_executed_package_cannot_be_reported_as_no_tests(tmp_path: Path) -> None:
    rows = complete_events()
    rows[-1]["Action"] = "skip"
    with pytest.raises(gate.RaceGateError, match="EXECUTED_PACKAGE_REPORTED_SKIPPED"):
        gate.inspect_events(write_events(tmp_path, rows), {PACKAGE}, REQUIRED, set(), exact=True)


@pytest.mark.parametrize("raw", ["not json\n", "[]\n", '{"Action":'])
def test_truncated_or_invalid_event_streams_fail_closed(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(gate.RaceGateError):
        gate.inspect_events(path, {PACKAGE}, REQUIRED, set(), exact=True)


def test_native_race_execution_refuses_this_local_host_before_any_go_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Local Go command was reached")

    monkeypatch.setattr(gate.subprocess, "check_output", forbidden)
    with pytest.raises(gate.RaceGateError, match="NATIVE_GO_RACE_EXECUTION_IS_CLOUD_ONLY"):
        gate.run(None, {})
