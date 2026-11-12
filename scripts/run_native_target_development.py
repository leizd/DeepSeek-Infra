"""Run the retained and new target regression groups without extending their limits."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

PACKAGES = ("media", "store", "api", "worker")
ACTIVATION_PREFIX = "TestTargetActivateContract"
INVOCATION_TIMEOUT = "8m"


def selected_tests(root: Path, pattern: str) -> set[str]:
    result: set[str] = set()
    for package in PACKAGES:
        for path in (root / "go/internal" / package).glob("*_test.go"):
            result.update(name for name in re.findall(r"^func (Test\w*)\(", path.read_text(encoding="utf-8"), re.M)
                          if re.fullmatch(pattern, name))
    return result


def plan_groups(expected: set[str]) -> list[tuple[str, list[str]]]:
    activation = {name for name in expected if name.startswith(ACTIVATION_PREFIX)}
    retained = expected - activation
    if not retained or not activation:
        raise ValueError("both retained and activation groups must be nonempty")
    return [("retained", sorted(retained)), ("activation", sorted(activation))]


def merge_profiles(paths: list[Path], destination: Path) -> None:
    blocks: dict[str, tuple[int, int]] = {}
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0] != "mode: atomic":
            raise ValueError("unexpected coverage instrumentation mode")
        for line in lines[1:]:
            location, statements, count = line.rsplit(" ", 2)
            size, hits = int(statements), int(count)
            if size < 0 or hits < 0:
                raise ValueError("negative coverage block")
            if location in blocks:
                previous, old_hits = blocks[location]
                if previous != size:
                    raise ValueError("coverage block changed between groups")
                hits += old_hits
            blocks[location] = (size, hits)
    destination.write_text("mode: atomic\n" + "".join(
        f"{location} {size} {hits}\n" for location, (size, hits) in sorted(blocks.items())), encoding="utf-8")


def execute_groups(root: Path, out: Path, pattern: str,
                   runner: Callable[..., subprocess.CompletedProcess[bytes]]) -> dict[str, Any]:
    root, out = root.resolve(), out.resolve()
    if not out.is_relative_to(root / "artifacts"):
        raise ValueError("reports must remain in the task artifacts directory")
    expected = selected_tests(root, pattern)
    plan = plan_groups(expected)
    out.mkdir(parents=True, exist_ok=True)
    groups: list[dict[str, Any]] = []
    profiles: list[Path] = []
    aggregate = 0
    combined = out / "go-tests.jsonl"
    with combined.open("wb") as joined:
        for name, names in plan:
            raw = out / (name + "-tests.jsonl")
            profile = out / (name + "-coverage.out")
            expression = "^(" + "|".join(re.escape(n) for n in names) + ")$"
            command = ["go", "test", "-race", "-json", "-count=1", "-timeout=" + INVOCATION_TIMEOUT,
                       "-covermode=atomic", "-coverprofile=" + profile.as_posix(),
                       *["./internal/" + p for p in PACKAGES], "-run", expression]
            with raw.open("wb") as stream:
                result = runner(command, cwd=root / "go", stdout=stream, stderr=subprocess.STDOUT, check=False)
            if result.returncode != 0:
                aggregate = 1
            data = raw.read_bytes()
            joined.write(data)
            if data and not data.endswith(b"\n"):
                joined.write(b"\n")
            if profile.is_file():
                profiles.append(profile)
            elif result.returncode == 0:
                aggregate = 2
            group = dict(name=name, expectedTests=names, exitCode=result.returncode,
                         rawEventsFile=raw.name, rawEventsSha256=hashlib.sha256(data).hexdigest(),
                         coverageFile=profile.name, raceEnabled=True, perInvocationTimeout=INVOCATION_TIMEOUT)
            groups.append(group)
            print(json.dumps(dict(group=name, expectedTests=len(names), exitCode=result.returncode)), flush=True)
    merge_profiles(profiles, out / "scoped-coverage.out")
    report = dict(schema="native-target-development-groups-v1", expectedTests=sorted(expected), groups=groups,
                  exitCode=aggregate, raceEnabled=True, perInvocationTimeout=INVOCATION_TIMEOUT,
                  fullGoSuiteQualified=False, original95PercentCoverageQualified=False, fullMigrationQualified=False)
    (out / "grouped-run.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    # This host's native compilation is authorized only in GitHub Actions.
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("native development execution is cloud-only")
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if revision != os.environ.get("GITHUB_SHA"):
        parser.error("the requested source is not the checked-out revision")
    report = execute_groups(root, args.out, args.pattern, subprocess.run)
    return int(report["exitCode"])


if __name__ == "__main__":
    raise SystemExit(main())
