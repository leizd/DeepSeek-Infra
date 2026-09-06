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
    assert "continue-on-error" not in job
    assert all(not step.get("continue-on-error", False) for step in steps)
    assert "native-s3-transport" in workflow["jobs"]["evidence-assembly"]["needs"]


def test_provider_suite_requires_explicit_opt_in_but_never_skips_missing_minio() -> None:
    manifest = (ROOT / "rust/crates/deepseek-storage/Cargo.toml").read_text(encoding="utf-8")
    assert 'name = "s3_provider"\nrequired-features = ["s3-e2e"]' in manifest
    tests = (ROOT / "rust/crates/deepseek-storage/tests/s3_provider.rs").read_text(encoding="utf-8")
    assert "#[ignore" not in tests
    assert '.expect("run scripts/run_native_s3_e2e.py with real MinIO")' in tests
