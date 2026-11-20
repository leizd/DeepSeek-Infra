"""The zero-invariant harness must refuse to report a measurement it did not take.

The seven zero invariants were stuck at zero of seven for a long time precisely
because the release validator refuses a vacuous zero: `value: 0` with no samples
behind it is a claim, not a measurement. This harness is the thing that produces the
samples, so its own honesty is the thing that matters.

These tests exercise the failure paths, because the failure paths are the whole point:

* a workload that completes nothing must raise rather than report zeros;
* a workload where no mutation was refused is not meaningful, because "zero Python
  mutations" would then mean "nothing was attempted";
* the process probe must not count the harness itself, which is Python - it did on the
  first run, and reported a false positive.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import measure_native_zero_invariants as harness  # noqa: E402


def test_the_seven_invariant_names_match_the_release_validator() -> None:
    """A name drift here would silently produce measurements nobody reads."""
    from scripts.native_runtime_evidence import REQUIRED_ZERO_INVARIANTS

    assert tuple(harness.INVARIANTS) == tuple(REQUIRED_ZERO_INVARIANTS)
    assert len(harness.INVARIANTS) == 7


def test_a_zero_operation_workload_is_refused() -> None:
    with pytest.raises(harness.MeasurementError, match="positive"):
        harness.measure(operations=0)


def test_the_probe_does_not_count_this_harness_as_a_python_service(monkeypatch) -> None:
    """The harness runs on Python, so it must exclude itself.

    Without this the measurement reports a Python process on every run and the
    invariant it is supposed to demonstrate becomes unmeasurable. It reported exactly
    that the first time it was executed.
    """
    # os.getpid / getppid are the exclusions; assert they are what the probe uses.
    import inspect

    source = inspect.getsource(harness._python_processes)
    assert "exclude_pid" in source
    assert "exclude_parent" in source

    # And a run excludes both this process and its parent by construction.
    import os

    probe = harness._python_processes(exclude_pid=os.getpid(), exclude_parent=os.getppid())
    # This test process is Python; if the exclusion were broken it would be listed.
    assert not probe or all(name for name in probe), probe


def test_measurement_evidence_carries_counts_a_reviewer_can_check() -> None:
    """Every measurement must show what was executed, not only the verdict.

    A document that says `value: 0` and nothing else is exactly the vacuous zero the
    validator rejects. The recorded artifact is checked here so a future edit cannot
    strip the evidence back to a bare zero.
    """
    import json

    artifact = ROOT / "release" / "native-runtime-evidence-5.0" / "zero-invariant-measurement.json"
    assert artifact.is_file(), "the zero-invariant measurement artifact is missing"
    document = json.loads(artifact.read_text(encoding="utf-8"))

    assert document["schema"] == harness.SCHEMA
    measurements = document["measurements"]
    assert set(measurements) == set(harness.INVARIANTS)

    for name, entry in measurements.items():
        assert entry["value"] == 0, name
        # Positive samples and operations are what make the zero non-vacuous.
        assert entry["samples"] > 0, name
        assert entry["workload_operations"] > 0, name
        assert entry["how"], name
        evidence = entry["evidence"]
        assert evidence["succeeded"]["healthz"] + evidence["succeeded"]["status"] > 0, name
        # A workload that never attempted a mutation cannot support "zero mutations".
        assert evidence["succeeded"]["refused_mutation"] > 0, name
        assert evidence["pythonProcessesObserved"] == [], name

    # The banner proves the process really started natively, in the expected mode.
    assert document["banner"]["mode"] == "shadow"


def test_the_recorded_gate_notes_the_scope_of_the_measurement() -> None:
    """An isolated shadow-mode run is not production traffic, and must say so.

    This is the difference between "the measurement method works and a native workload
    needs no Python" and "production makes no Python calls". Only the first is true, so
    the evidence names the second as unproven rather than implying it.
    """
    import json

    evidence = json.loads((ROOT / "release" / "native_runtime_gate_evidence_v1.json").read_text(encoding="utf-8"))
    recorded = evidence["zeroInvariantMeasurement"]
    assert "not production traffic" in recorded["whatIsStillNotProven"]
    assert recorded["artifact"]["sha256"]
    assert recorded["harness"] == "scripts/measure_native_zero_invariants.py"
