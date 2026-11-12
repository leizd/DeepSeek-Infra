"""The absolute latency gate must measure, or say plainly that it could not.

The Rust sidecar benchmark used to hard-code `informationalOnly: true` and
`absoluteLatencyMergeGate: false`, which meant no performance gate could ever exist
no matter how the numbers moved. This tests the replacement: a budget table plus an
evaluator that either asserts the budgets or reports itself unmeasured.

The distinction the tests protect is the one that matters for a merge gate: too few
samples is **not** a pass. A percentile built from two samples is noise, so a
two-sample run has to come out `measured: False` rather than a green gate nobody can
trust.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.bench_rust_sidecar_release import (  # noqa: E402
    LATENCY_BUDGET_US,
    MINIMUM_GATE_SAMPLES,
    evaluate_absolute_latency_gates,
)


def _delegate(name: str, scenario: str, core_p95: float | None, http_p95: float | None) -> dict:
    return {
        "delegate": name,
        "scenarios": [
            {
                "name": scenario,
                "layers": {
                    "pureRustCore": {"p95Us": core_p95},
                    "releaseSidecarHttp": {"p95Us": http_p95},
                },
            }
        ],
    }


def test_too_few_samples_is_unmeasured_not_a_pass() -> None:
    """Two samples cannot support a percentile, so the gate refuses to claim one."""
    result = evaluate_absolute_latency_gates(
        [_delegate("tool_policy", "safe_path", 2.0, 90.0)], iterations=2
    )
    assert result["measured"] is False
    assert result["reason"] == "insufficient-samples"
    assert result["iterations"] == 2
    assert result["minimumSamples"] == MINIMUM_GATE_SAMPLES
    # Crucially, there is no `passed: True` to be mistaken for a green gate.
    assert "passed" not in result
    assert result["scenarios"] == {}


def test_budgets_pass_when_every_scenario_is_inside_them() -> None:
    core_budget, http_budget = LATENCY_BUDGET_US["tool_policy"]["safe_path"]
    result = evaluate_absolute_latency_gates(
        [_delegate("tool_policy", "safe_path", core_budget / 2, http_budget / 2)],
        iterations=MINIMUM_GATE_SAMPLES,
    )
    assert result["measured"] is True
    assert result["passed"] is True
    assert result["violations"] == []
    entry = result["scenarios"]["tool_policy/safe_path"]
    assert entry["httpWithinBudget"] is True
    assert entry["coreWithinBudget"] is True


def test_a_slow_http_path_fails_the_gate_and_names_the_scenario() -> None:
    _, http_budget = LATENCY_BUDGET_US["tool_policy"]["safe_path"]
    result = evaluate_absolute_latency_gates(
        [_delegate("tool_policy", "safe_path", 1.0, http_budget * 3)],
        iterations=MINIMUM_GATE_SAMPLES,
    )
    assert result["measured"] is True
    assert result["passed"] is False
    assert any("tool_policy/safe_path" in violation for violation in result["violations"])
    assert result["scenarios"]["tool_policy/safe_path"]["httpWithinBudget"] is False


def test_a_slow_pure_core_fails_even_when_the_http_path_is_fine() -> None:
    """The core has its own tighter budget; a regression there must not hide behind
    a healthy HTTP number."""
    core_budget, http_budget = LATENCY_BUDGET_US["tool_policy"]["safe_path"]
    result = evaluate_absolute_latency_gates(
        [_delegate("tool_policy", "safe_path", core_budget * 3, http_budget / 4)],
        iterations=MINIMUM_GATE_SAMPLES,
    )
    assert result["passed"] is False
    assert result["scenarios"]["tool_policy/safe_path"]["coreWithinBudget"] is False


def test_a_missing_measurement_fails_rather_than_passing_vacuously() -> None:
    result = evaluate_absolute_latency_gates(
        [_delegate("tool_policy", "safe_path", None, None)],
        iterations=MINIMUM_GATE_SAMPLES,
    )
    assert result["passed"] is False
    assert any("missing" in violation for violation in result["violations"])


def test_ungated_scenarios_are_skipped_not_failed() -> None:
    """A scenario with no budget is measured but not gated, so it must not produce a
    violation - otherwise every new scenario would break the gate on arrival."""
    result = evaluate_absolute_latency_gates(
        [_delegate("gateway_request_preparation", "large_multipart_content", 1.0, 1.0)],
        iterations=MINIMUM_GATE_SAMPLES,
    )
    assert result["measured"] is True
    assert result["scenarios"] == {}
    assert result["violations"] == []
    assert result["passed"] is True


@pytest.mark.parametrize("samples", [MINIMUM_GATE_SAMPLES - 1, 0])
def test_the_sample_threshold_is_where_it_says_it_is(samples: int) -> None:
    result = evaluate_absolute_latency_gates(
        [_delegate("tool_policy", "safe_path", 1.0, 1.0)], iterations=samples
    )
    assert result["measured"] is False


def test_every_budget_is_positive_and_reaches_a_real_scenario() -> None:
    """A typo in the budget table would silently disable a gate, so the table's
    shape is asserted directly."""
    assert LATENCY_BUDGET_US, "the budget table is empty"
    assert MINIMUM_GATE_SAMPLES >= 5
    for delegate, scenarios in LATENCY_BUDGET_US.items():
        assert scenarios, delegate
        for scenario, budget in scenarios.items():
            assert isinstance(budget, tuple) and len(budget) == 2, f"{delegate}/{scenario}"
            core_budget, http_budget = budget
            # Every budget is a positive float, and HTTP is the slower path so its
            # budget must be the larger of the two.
            for value in (core_budget, http_budget):
                assert isinstance(value, float) and value > 0, f"{delegate}/{scenario}"
            assert http_budget > core_budget, f"{delegate}/{scenario}"


def test_the_committed_report_is_unmeasured_by_its_own_admission() -> None:
    """The recorded report used 2 iterations, so it must not claim a gate.

    This is the regression that made the old flags dishonest: the report said PASS
    while `absoluteLatencyMergeGate` was hard-coded false. Now the same two samples
    produce an explicit "unmeasured", and the evaluator run over the report's own
    delegate data reproduces that verdict.
    """
    import json

    # The recorded artifact's filenames carry the release version, so it is derived
    # from VERSION rather than written out: a hard-coded token would fail the
    # repository's release-version contract check.
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    report_path = ROOT / "release" / "native-runtime-evidence-5.0" / f"rust-sidecar-performance-v{version}.json"
    assert report_path.is_file(), "the recorded performance evidence is missing"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    # The stored report predates this gate and carries the old hard-coded flags.
    # What matters is that re-evaluating its own measurements at 2 iterations does
    # not produce a pass.
    result = evaluate_absolute_latency_gates(report["delegates"], iterations=2)
    assert result["measured"] is False
    assert "passed" not in result

    # And at an adequate sample count the same numbers are inside their budgets,
    # which shows the budgets were calibrated from evidence rather than guessed low.
    adequate = evaluate_absolute_latency_gates(report["delegates"], iterations=MINIMUM_GATE_SAMPLES)
    assert adequate["measured"] is True
    assert adequate["passed"] is True, adequate["violations"][:5]


def _report_with(latency: dict) -> dict:
    """A report shaped closely enough for main() to write it.

    ``_stable_evidence`` reads ``machine``, so the fixture supplies it rather than
    making main() defensive about a report the real benchmark always produces.
    """
    return {"absoluteLatency": latency, "machine": {}}


def test_gate_mode_fails_when_the_run_could_not_be_measured(monkeypatch, tmp_path, capsys) -> None:
    """A two-sample run must exit non-zero under --require-latency-gate.

    This is the difference between reporting and gating. Without it the budgets were
    measured and then ignored, which is exactly how the old hard-coded false flag
    survived.
    """
    from benchmarks import bench_rust_sidecar_release as bench

    monkeypatch.setattr(
        bench,
        "run_benchmark",
        lambda **_kwargs: _report_with({"measured": False, "reason": "insufficient-samples", "detail": "only 2 samples"}),
    )
    exit_code = bench.main(
        ["--require-latency-gate", "--artifact-out", str(tmp_path / "a.json"), "--evidence-out", str(tmp_path / "e.json")]
    )
    assert exit_code == 1
    assert "could not be measured" in capsys.readouterr().err


def test_gate_mode_fails_and_names_each_budget_violation(monkeypatch, tmp_path, capsys) -> None:
    from benchmarks import bench_rust_sidecar_release as bench

    monkeypatch.setattr(
        bench,
        "run_benchmark",
        lambda **_kwargs: _report_with(
            {"measured": True, "passed": False, "violations": ["tool_policy/safe_path: http p95 900us over budget 650us"]}
        ),
    )
    exit_code = bench.main(
        ["--require-latency-gate", "--artifact-out", str(tmp_path / "a.json"), "--evidence-out", str(tmp_path / "e.json")]
    )
    assert exit_code == 1
    assert "tool_policy/safe_path" in capsys.readouterr().err


def test_without_gate_mode_the_benchmark_still_records_and_exits_zero(monkeypatch, tmp_path, capsys) -> None:
    """The benchmark is also used to collect numbers, so it must not always fail."""
    from benchmarks import bench_rust_sidecar_release as bench

    monkeypatch.setattr(
        bench,
        "run_benchmark",
        lambda **_kwargs: _report_with({"measured": True, "passed": False, "violations": ["x"]}),
    )
    exit_code = bench.main(["--artifact-out", str(tmp_path / "a.json"), "--evidence-out", str(tmp_path / "e.json")])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["absoluteLatencyGate"] is False


def test_gate_mode_passes_when_budgets_hold(monkeypatch, tmp_path) -> None:
    from benchmarks import bench_rust_sidecar_release as bench

    monkeypatch.setattr(
        bench,
        "run_benchmark",
        lambda **_kwargs: _report_with({"measured": True, "passed": True, "violations": []}),
    )
    exit_code = bench.main(
        ["--require-latency-gate", "--artifact-out", str(tmp_path / "a.json"), "--evidence-out", str(tmp_path / "e.json")]
    )
    assert exit_code == 0
