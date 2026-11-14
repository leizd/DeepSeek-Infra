"""The partial 5.0 gate evidence must stay honest and hash-verifiable.

`release/native_runtime_gate_evidence_v1.json` claims three release gates have
exact-head evidence. A claim like that is only worth anything if the artifacts it
points at still hash to what it says, so this test re-derives every digest from the
files in the repository and fails if one drifted.

It also pins the boundary that matters: this file is PARTIAL and must not be
promoted to DELIVERED just because three gates are green. Twelve gates and all
seven zero invariants are still outstanding, and
`release/native_runtime_5_0_evidence_v1.json` must stay NOT_READY until they exist.
Writing that down as a test is what stops a future change from quietly turning a
partial result into a release claim.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_EVIDENCE = ROOT / "release" / "native_runtime_gate_evidence_v1.json"
ASSESSMENT = ROOT / "release" / "native_runtime_5_0_evidence_v1.json"


def _evidence() -> dict:
    return json.loads(GATE_EVIDENCE.read_text(encoding="utf-8"))


def test_gate_evidence_artifacts_still_hash_to_their_recorded_digests() -> None:
    evidence = _evidence()
    gates = evidence["gates"]
    assert gates, "the gate evidence must name at least one gate"
    for name, gate in gates.items():
        artifact = gate.get("artifact")
        if artifact is None:
            # Gates whose evidence is a set of source paths rather than one produced
            # file (fuzz) are checked by their own test below.
            continue
        path = ROOT / artifact["path"]
        assert path.is_file(), f"{name}: artifact {artifact['path']} is missing"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == artifact["sha256"], f"{name}: artifact digest drifted"
        # An artifact path must stay inside the repository: evidence that points
        # outside could not be reviewed with the change.
        assert not Path(artifact["path"]).is_absolute(), name


def test_gate_evidence_is_exact_head_bound_and_clean() -> None:
    evidence = _evidence()
    provenance = evidence["provenance"]
    assert provenance["exact_head"] == "69a5a881d6f7ea21eb8905037bd9a0d60f74ffa0"
    assert provenance["source_tree"] == "48c53522021ac936c5c387583c57451e57280a45"
    assert provenance["source_tree_dirty"] is False
    assert provenance["workflow_run"] == 43582666126
    assert provenance["jobs"]["total"] == 37
    assert provenance["jobs"]["success"] == 37
    assert provenance["jobs"]["failure"] == 0
    # Each gate records the revision its own artifact was produced at. They are not
    # required to be identical - an artifact from an earlier run stays valid when the
    # code it covers is unchanged - but every one must be a full 40-character commit,
    # and the newest gate (the one the provenance names) must match the provenance.
    import re

    heads = set()
    for name, gate in evidence["gates"].items():
        assert gate["status"] == "PASS", name
        assert re.fullmatch(r"[a-f0-9]{40}", gate["exact_head"]), name
        heads.add(gate["exact_head"])
    assert provenance["exact_head"] in heads


def test_gate_evidence_stays_partial_and_never_becomes_a_release_claim() -> None:
    evidence = _evidence()
    assert evidence["status"] == "PARTIAL"
    assert evidence["readiness"] == "NOT_READY"
    assert evidence["fullMigrationQualified"] is False

    outstanding = evidence["notYetPassing"]
    assert outstanding["count"] == 0
    assert outstanding["gates"] == []
    # A measured zero invariant needs positive samples and workload operations, so
    # none can be claimed from a CI pass alone. This pins that none are.
    assert outstanding["zeroInvariantsMeasured"] == 0
    assert outstanding["zeroInvariantsRequired"] == 7


def test_the_release_assessment_remains_fail_closed() -> None:
    """Fifteen green gates must still not move the release assessment."""
    assessment = json.loads(ASSESSMENT.read_text(encoding="utf-8"))
    assert assessment["status"] == "NOT_READY"
    assert assessment["blockers"]
    # measurements and gates stay empty until every gate and invariant exists; the
    # validator refuses a vacuous zero, so a partial result cannot populate them.
    assert assessment["measurements"] == {}
    assert assessment["gates"] == {}


def test_the_passing_gates_are_the_documented_ones() -> None:
    gates = set(_evidence()["gates"])
    assert gates == {
        "exact_head_ci",
        "evidence_assembly",
        "production_artifact_zero_python",
        "native_two_fleet_four_minio",
        "go_controller_sigkill",
        "rust_worker_sigkill",
        "rust_receiver_sigkill",
        "public_http_sse_mcp_a2a_parity",
        "frozen_wire_parity",
        "coverage",
        "security",
        "performance",
        "go_control_authority",
        "rust_data_authority",
        "fuzz",
    }


def test_zero_python_gate_rests_on_the_image_not_the_source_zip() -> None:
    """The distinction this evidence exists to keep straight.

    The release zip built in the same run is a source distribution containing Python
    files, so it cannot satisfy the zero-Python production artifact gate. The gate
    is backed by the image verification report, whose container-side check found no
    python/python3/node/nodejs on PATH.
    """
    gate = _evidence()["gates"]["production_artifact_zero_python"]
    assert gate["artifact"]["path"].endswith("native-default-image-7c0713cc.json")
    assert "release zip" in gate["whyThisIsTheRealClaim"]
    report = json.loads((ROOT / gate["artifact"]["path"]).read_text(encoding="utf-8"))
    positive = report["cases"]["positive"]
    assert positive["status"] == "PASS"
    assert positive["PythonAndNodeRuntimeExecutablesAbsent"] is True
    assert report["status"] == "PASS"


def test_all_fifteen_gates_pass_but_the_assessment_still_refuses_to_deliver() -> None:
    """Every gate now has evidence; the release still cannot be called delivered.

    This is the distinction the whole file exists for. Gates measure capability - can
    the runtime do the thing. The zero invariants measure *production behaviour* - did
    a running system make zero Python calls while doing it. Fifteen green gates do not
    imply seven measured zeroes, and the validator refuses a zero with no samples.
    """
    evidence = _evidence()
    assert len(evidence["gates"]) == 15
    outstanding = evidence["notYetPassing"]
    assert outstanding["gates"] == []
    assert outstanding["count"] == 0
    # The blocker that survives is measurement, not capability.
    assert outstanding["zeroInvariantsMeasured"] == 0
    assert outstanding["zeroInvariantsRequired"] == 7


def test_the_fuzz_gate_is_a_real_target_not_an_assertion() -> None:
    """The fuzz gate has to point at code that actually fuzzes something.

    A gate that claims coverage-guided fuzzing while carrying no target is exactly the
    failure this file was written to prevent, so the wiring is checked rather than
    taken on trust: a standalone crate, three binaries, seeded corpora, and a CI job
    that runs each of them on nightly.
    """
    import yaml

    gate = _evidence()["gates"]["fuzz"]
    artifacts = gate["artifacts"]

    manifest = (ROOT / artifacts["manifest"]).read_text(encoding="utf-8")
    assert "[workspace]" in manifest, "the fuzz crate must be its own workspace"
    for target in artifacts["targets"]:
        assert (ROOT / target).is_file(), target
    for corpus in artifacts["corpora"]:
        directory = ROOT / corpus
        assert directory.is_dir(), corpus
        # A corpus with no seeds spends the whole budget rediscovering structure.
        assert any(directory.iterdir()), f"{corpus} has no seed inputs"

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_bytes())
    job = workflow["jobs"][artifacts["ciJob"]]

    # The fuzz crate's targets must be built by a nightly toolchain, because
    # libFuzzer needs one; the product build stays on its pinned stable.
    uses = " ".join(str(step.get("uses", "")) for step in job["steps"])
    assert "rust-toolchain@nightly" in uses, "cargo-fuzz requires nightly"

    # The fuzz steps must run inside the fuzz crate and address corpora relative to
    # it. Both halves have been wrong in different ways: running from the repository
    # root fails with "could not find a cargo project" (there is no Cargo.toml there),
    # and running from fuzz/ with a `fuzz/` prefix in the corpus path looks for
    # fuzz/fuzz/corpus. Each mistake reads as a fuzz finding while being nothing of
    # the kind, so the pair is asserted together.
    run_steps = [step for step in job["steps"] if "fuzz run" in str(step.get("run", ""))]
    assert len(run_steps) == 3, run_steps
    for step in run_steps:
        command = str(step.get("run", ""))
        # This repository has no Cargo.toml at its root, and cargo-fuzz's
        # find_package() walks up looking for one, so the step must run from rust/
        # (a real Cargo project) and name the crate explicitly.
        assert step.get("working-directory") == "rust", command
        assert "--fuzz-dir=../fuzz" in command, command
        # An absolute corpus path avoids relying on whether the relative one resolves
        # against the working directory or against the fuzz directory.
        assert "$GITHUB_WORKSPACE/fuzz/corpus/" in command, command
    marker = (ROOT / "fuzz" / "Cargo.toml").read_text(encoding="utf-8")
    assert "metadata.cargo-fuzz = true" in marker, (
        "cargo-fuzz locates its project by this marker; without it the CLI reports "
        "'could not find a cargo project'"
    )

    commands = [str(step.get("run", "")) for step in job["steps"]]
    joined = "\n".join(commands)
    for name in ("transport_frozen_documents", "restore_relative_path", "evidence_proof_envelope"):
        # The flag sits between `run` and the target, so match them separately rather
        # than as one contiguous string.
        assert f"--fuzz-dir=../fuzz {name}" in joined, f"{name} is never run"
    # Each target must be time-bounded, or a hang looks like a slow CI job.
    assert joined.count("-max_total_time=") == 3, joined
    # And a finding has to block a release, not just colour a job red.
    assert "rust-fuzz" in workflow["jobs"]["rc-readiness"]["needs"]

    # ci.yml must carry no version-shaped argument: the release-version contract
    # rejects one, and a pin inline here is what broke it the first time.
    import re

    assert re.search(r"--version\s+\d+\.\d+\.\d+", (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")) is None
    pin = ROOT / "fuzz" / "cargo-fuzz-version.txt"
    assert pin.is_file(), "the cargo-fuzz pin must live next to the crate"
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin.read_text(encoding="utf-8").strip())


def test_the_fuzz_gate_records_an_observed_run_not_just_wiring() -> None:
    """Wiring is not evidence. This asserts the gate carries a measured execution.

    The gate was once "PASS" on wiring alone, with a note admitting no target had
    run. That is the exact failure mode this file exists to prevent, so the recorded
    numbers are checked: a real execution count per target, a real duration, and zero
    findings. A future edit that drops back to a claim has to fail here first.
    """
    gate = _evidence()["gates"]["fuzz"]
    observed = gate["observed"]

    assert observed["conclusion"] == "success"
    assert observed["findings"] == {
        "crashes": 0,
        "leaks": 0,
        "timeouts": 0,
        "assertionFailures": 0,
    }

    targets = observed["targets"]
    assert set(targets) == {
        "transport_frozen_documents",
        "restore_relative_path",
        "evidence_proof_envelope",
    }
    for name, stats in targets.items():
        # A target that executed a handful of inputs is not fuzzing. The budget is
        # 180 seconds, and even a slow target clears six figures.
        assert stats["executions"] > 100_000, (name, stats["executions"])
        assert stats["seconds"] >= 120, (name, stats["seconds"])

    # The recorded total must equal its parts, so the summary cannot inflate.
    assert observed["totalExecutions"] == sum(s["executions"] for s in targets.values())
    assert observed["totalExecutions"] > 1_000_000
