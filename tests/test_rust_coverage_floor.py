"""The Rust coverage floor lives in three places; keep them one value.

Raising the floor from 80% to 95% is done in stages. Each stage is a committed
number that CI enforces rather than an aspiration, so a stage advance must move
every surface at once: the script's current stage, the release manifest's
recorded floor, and the RC requirement. These tests fail on any drift, which is
what makes "advance the ladder" a one-line change instead of a hunt.

They are deliberately not coverage tests: they run without cargo and guard the
gate's own arithmetic so a red `rust-coverage` job always means lost coverage,
never a stale constant.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ladder() -> tuple[list[float], int]:
    """Return the declared stage floors and the index of the stage CI enforces."""
    source = (ROOT / "scripts" / "run_rust_coverage.py").read_text(encoding="utf-8")
    match = re.search(
        r"RUST_COVERAGE_STAGE_FLOORS: tuple\[float, \.\.\.\] = \(([^)]*)\)",
        source,
    )
    assert match, "RUST_COVERAGE_STAGE_FLOORS declaration not found"
    stages = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", match.group(1))]
    assert stages, "the ladder declares no stages"
    current = re.search(
        r"^RUST_COVERAGE_CURRENT_FLOOR = RUST_COVERAGE_STAGE_FLOORS\[(\d+)\]",
        source,
        re.M,
    )
    assert current, "RUST_COVERAGE_CURRENT_FLOOR must index the ladder"
    return stages, int(current.group(1))


def _enforced_floor() -> float:
    stages, index = _ladder()
    assert 0 <= index < len(stages), f"current stage index {index} is outside the ladder"
    return stages[index]


def test_the_ladder_rises_and_ends_at_the_target() -> None:
    """A ladder that does not rise never gates anything; one that ends low is not the target."""
    stages, _ = _ladder()
    assert stages == sorted(stages), f"stages are not monotonic: {stages}"
    assert len(set(stages)) == len(stages), f"duplicate stage values: {stages}"
    assert stages[-1] == 95.0, f"final stage must be 95.0, got {stages[-1]}"
    assert stages[0] > 80.0, "stage 1 must be above the previous 80.0 floor"


def test_the_enforced_floor_is_one_of_the_declared_stages() -> None:
    stages, index = _ladder()
    assert 0 <= index < len(stages), f"current stage index {index} is outside the ladder"
    current = stages[index]
    source = (ROOT / "scripts" / "run_rust_coverage.py").read_text(encoding="utf-8")
    # The argparse default is what CI actually enforces, since the workflow no
    # longer passes --threshold itself.
    assert "default=RUST_COVERAGE_CURRENT_FLOOR" in source, (
        "--threshold must default to the current stage so CI cannot pass a stale number"
    )
    assert current == 83.0, (
        f"stage index {index} gives {current}, but this test pins 83.0; "
        "advancing the ladder means updating this test in the same commit"
    )


def test_the_release_manifest_records_the_enforced_floor() -> None:
    from deepseek_infra.infra.diagnostics.release_manifest import RUST_COVERAGE_FLOOR

    current = _enforced_floor()
    assert RUST_COVERAGE_FLOOR == current, (
        f"release manifest records {RUST_COVERAGE_FLOOR} but CI enforces {current}"
    )


def test_the_rc_requirement_stays_at_its_frozen_evidence_floor() -> None:
    """The 4.0 RC requirement is pinned to a frozen evidence file, not to today's CI.

    `release/4_0_rc_requirements.json` is checked against
    `docs/evidence/rust-coverage-v4.0.0-rc.2.json`, a historical measurement of
    80.43%. Raising its `required` value would make that pair disagree and turn
    the RC report red on every branch, so it deliberately lags the CI ladder and
    this test records that separation.
    """
    document = json.loads((ROOT / "release" / "4_0_rc_requirements.json").read_text(encoding="utf-8"))
    items = document if isinstance(document, list) else document.get("requirements", [])
    requirement = next(item for item in items if item.get("id") == "rust_measured_line_coverage")
    evidence_path = ROOT / requirement["evidence"][0]
    assert evidence_path.is_file(), f"RC evidence missing: {requirement['evidence'][0]}"
    measured = json.loads(evidence_path.read_text(encoding="utf-8"))
    percent = measured["coverage"]["lines"]["percent"]
    assert requirement["required"] <= percent, (
        f"RC requirement {requirement['required']} exceeds its own evidence {percent:.2f}; "
        "the requirement is checked against this frozen file, not against current CI"
    )
    assert requirement["blocking"] is True


def test_the_ci_workflow_does_not_pass_its_own_threshold() -> None:
    """The workflow must inherit the script's stage instead of duplicating the number."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = workflow.split("Enforce Rust workspace line coverage", 1)
    assert len(step) == 2, "the coverage step was renamed; this guard needs updating"
    body = step[1].split("- run:", 1)[0]
    assert "--threshold" not in body, (
        "ci.yml passes --threshold again; the stage constant in run_rust_coverage.py "
        "must stay the single source of truth"
    )


def test_the_gate_evidence_keeps_the_floor_that_was_actually_measured() -> None:
    """Evidence may only claim a floor that was reached, never a future stage.

    The gate evidence file records what has been demonstrated. It is advanced in
    the same commit that first measures the new floor, and this test refuses a
    value that is ahead of the enforced stage.
    """
    path = ROOT / "release" / "native_runtime_gate_evidence_v1.json"
    if not path.is_file():
        return
    document = json.loads(path.read_text(encoding="utf-8"))
    gate = (document.get("gates") or {}).get("coverage")
    if gate is None:
        return
    current = _enforced_floor()
    recorded = gate.get("minimumPercent")
    if recorded is None:
        return
    assert float(recorded) <= current, (
        f"gate evidence claims a floor of {recorded}% but CI only enforces {current}%; "
        "evidence must not run ahead of what was measured"
    )
