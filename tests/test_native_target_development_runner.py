"""Offline coordinator checks; these tests never execute Go."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_target_development_runner", ROOT / "scripts/run_native_target_development.py")
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def test_groups_retain_each_original_test_and_isolate_the_new_family() -> None:
    original = {"TestMediaRetained", "TestTargetDeleteContractRetained", "TestMigrationRetained"}
    new = {"TestTargetActivateContractOriginalResult", "TestTargetActivateContractRollback"}
    plan = dict(GATE.plan_groups(original | new))
    assert set(plan["retained"]) == original
    assert set(plan["activation"]) == new
    assert not set(plan["retained"]) & set(plan["activation"])
    for invalid in [set(), original, new]:
        with pytest.raises(ValueError, match="nonempty"):
            GATE.plan_groups(invalid)


def test_profile_merge_retains_blocks_and_rejects_incompatible_instrumentation(tmp_path: Path) -> None:
    left, right, merged = [tmp_path / name for name in ["left.out", "right.out", "merged.out"]]
    left.write_text("mode: atomic\npkg/a.go:1.1,2.1 2 0\npkg/b.go:1.1,2.1 1 3\n", encoding="utf-8")
    right.write_text("mode: atomic\npkg/a.go:1.1,2.1 2 4\npkg/c.go:1.1,2.1 5 0\n", encoding="utf-8")
    GATE.merge_profiles([left, right], merged)
    assert merged.read_text(encoding="utf-8") == (
        "mode: atomic\npkg/a.go:1.1,2.1 2 4\npkg/b.go:1.1,2.1 1 3\npkg/c.go:1.1,2.1 5 0\n")
    for invalid in ["mode: set\n", "mode: atomic\npkg/a.go:1.1,2.1 3 1\n", "mode: atomic\npkg/x.go:1.1,2.1 1 -1\n"]:
        right.write_text(invalid, encoding="utf-8")
        with pytest.raises(ValueError):
            GATE.merge_profiles([left, right], merged)


@pytest.mark.parametrize("failed_group", [None, "retained", "activation"])
def test_every_group_runs_with_original_limits_and_failures_cannot_be_overwritten(tmp_path: Path, failed_group: str | None) -> None:
    source = tmp_path / "go/internal/store"
    source.mkdir(parents=True)
    (source / "case_test.go").write_text(
        "func TestMediaRetained(t *testing.T) {}\nfunc TestTargetActivateContractNew(t *testing.T) {}\n", encoding="utf-8")
    calls: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert command[:6] == ["go", "test", "-race", "-json", "-count=1", "-timeout=8m"]
        assert "-covermode=atomic" in command and kwargs["cwd"] == tmp_path / "go"
        assert kwargs["stderr"] == subprocess.STDOUT and kwargs["check"] is False
        expression = command[-1]
        name = "retained" if re.fullmatch(expression, "TestMediaRetained") else "activation"
        calls.append(name)
        output = json.dumps({"group": name}).encode() + b"\n"
        kwargs["stdout"].write(output)
        profile = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("-coverprofile=")))
        profile.write_text("mode: atomic\npkg/a.go:1.1,2.1 1 1\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 1 if name == failed_group else 0)

    out = tmp_path / "artifacts/reports"
    result = GATE.execute_groups(tmp_path, out, "Test.*", fake_run)
    assert calls == ["retained", "activation"]
    assert result["exitCode"] == (0 if failed_group is None else 1)
    assert [group["exitCode"] for group in result["groups"]] == [int(failed_group == "retained"), int(failed_group == "activation")]
    assert (out / "go-tests.jsonl").read_text(encoding="utf-8") == '{"group": "retained"}\n{"group": "activation"}\n'
    assert result["fullGoSuiteQualified"] is False
    with pytest.raises(ValueError, match="artifacts"):
        GATE.execute_groups(tmp_path, tmp_path / "outside", "Test.*", fake_run)
