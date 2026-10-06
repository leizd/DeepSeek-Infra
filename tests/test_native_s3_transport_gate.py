"""Keep real native S3 verification reachable from CI, with no skip fallback."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_ci_builds_native_s3_on_msrv_and_runs_real_provider_suite() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["native-s3-transport"]
    steps = job["steps"]
    assert any(step.get("uses") == "dtolnay/rust-toolchain@1.85.0" for step in steps)
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "--features s3 --lib --test s3_transport" in commands
    assert "python scripts/run_native_s3_e2e.py" in commands
    assert "go1.27.1.linux-amd64.tar.gz" in commands
    assert "sha256sum --check --strict" in commands
    assert job["env"]["CGO_ENABLED"] == "0"
    assert job["env"]["GOTOOLCHAIN"] == "local"
    download_index = next(index for index, step in enumerate(steps) if "go mod download\n" in step.get("run", ""))
    provider_index = next(index for index, step in enumerate(steps) if "python scripts/run_native_s3_e2e.py" in step.get("run", ""))
    assert steps[download_index]["working-directory"] == "go"
    assert "go mod verify" in steps[download_index]["run"]
    assert download_index < provider_index
    assert steps[provider_index]["env"]["GOPROXY"] == "off"
    assert "continue-on-error" not in job
    assert all(not step.get("continue-on-error", False) for step in steps)
    assert "native-s3-transport" in workflow["jobs"]["evidence-assembly"]["needs"]


def test_provider_suite_requires_explicit_opt_in_but_never_skips_missing_minio() -> None:
    manifest = (ROOT / "rust/crates/deepseek-storage/Cargo.toml").read_text(encoding="utf-8")
    assert 'name = "s3_provider"\nrequired-features = ["s3-e2e"]' in manifest
    tests = (ROOT / "rust/crates/deepseek-storage/tests/s3_provider.rs").read_text(encoding="utf-8")
    assert "#[ignore" not in tests
    assert '.expect("run scripts/run_native_s3_e2e.py with real MinIO")' in tests

    worker_manifest = (ROOT / "rust/crates/deepseek-worker/Cargo.toml").read_text(encoding="utf-8")
    assert 'name = "authorized_storage_provider"\nrequired-features = ["s3-e2e"]' in worker_manifest
    worker_tests = (ROOT / "rust/crates/deepseek-worker/tests/authorized_storage_provider.rs").read_text(encoding="utf-8")
    assert "#[ignore" not in worker_tests
    assert '.expect("run scripts/run_native_s3_e2e.py with real MinIO")' in worker_tests
    runner = (ROOT / "scripts/run_native_s3_e2e.py").read_text(encoding="utf-8")
    assert "TestRustWorkerPromotedControlWritesAndRecoversRealProviders" in runner
    assert 'subprocess.run(command3, cwd=ROOT / "go"' in runner


def test_go_recovery_suite_runs_under_the_race_detector_with_cgo_enabled() -> None:
    """The recovery qualification claims a race run, so the runner must actually pass -race.

    `-race` needs cgo, and the job keeps `CGO_ENABLED: "0"` for every statically linked
    step, so the runner has to re-enable it for this one command. Without both, the claim
    in the commit message and the runbook is false: `-race` is silently dropped or the
    command refuses to build.
    """
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["native-s3-transport"]
    # The production image and the other Go steps stay statically linked.
    assert job["env"]["CGO_ENABLED"] == "0"
    # A 20-minute job cannot hold the race build plus the plain suite's 63s multiplied
    # by the detector across three providers.
    assert job["timeout-minutes"] >= 40

    runner = (ROOT / "scripts/run_native_s3_e2e.py").read_text(encoding="utf-8")
    command3 = runner.split("command3 = [", 1)[1].split("]", 1)[0]
    assert '"-race"' in command3
    # `-timeout` bounds the suite run itself; the detector costs several times the
    # plain 63s across three providers, so it needs far more than the original 3m.
    assert '-timeout=15m' in command3
    # cgo is re-enabled for the Go race command only. The subprocess bound sits above
    # `-timeout` so the instrumented build is not charged to the suite's own timeout.
    assert 'environment["CGO_ENABLED"] = "1"' in runner
    assert "timeout=1500" in runner
