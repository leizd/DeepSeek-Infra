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
