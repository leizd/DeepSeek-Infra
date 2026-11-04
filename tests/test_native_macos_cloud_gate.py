"""Source pipeline contracts; these do not build or qualify a local macOS host."""
import ast
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow() -> dict[Any, Any]:
    return yaml.safe_load((ROOT / ".github/workflows/native-windows-cloud.yml").read_bytes())


def test_optional_macos_probe_preserves_the_complete_original_windows_job() -> None:
    value = workflow()
    windows_job = value["jobs"]["windows-app"]
    added = [step for step in windows_job["steps"]
             if step.get("name") == "Verify streamed media images records compact results and digest corpus on Windows"]
    assert len(added) == 1
    # Bind the approved additional gate separately; preserve the original job's frozen hash.
    extra = json.dumps(added[0], sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(extra).hexdigest() == "e40985f389cb457c60e083545b5456f3da208e7794759319e638ec2d195fdf91"  # pragma: allowlist secret
    prebuild = [step for step in windows_job["steps"]
                if step.get("name") == "Build Windows media test binaries for all retained caller graphs"]
    assert len(prebuild) == 1
    prepared = json.dumps(prebuild[0], sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(prepared).hexdigest() == "0add0a41658794f014e157e6e9811c579dc1245f839e80a8f40944b7c7cdc08c"  # pragma: allowlist secret
    original_job = dict(windows_job, steps=[step for step in windows_job["steps"]
                                          if step is not added[0] and step is not prebuild[0]])
    original = json.dumps(original_job, sort_keys=True, separators=(",", ":")).encode()
    # Public SHA256 of the original workflow structure, not a credential.
    assert hashlib.sha256(original).hexdigest() == "8108d2be47dea2b3ee06cd760950a5d338fecea0f2e68a881106f7870f4ac537"  # pragma: allowlist secret
    trigger = value.get("on", value.get(True))
    assert isinstance(trigger, dict)
    assert trigger["workflow_dispatch"]["inputs"]["verify_macos"] == {
        "description": "Also execute the native Go window on actual Intel and Apple silicon macOS runners",
        "type": "boolean", "required": False, "default": False,
    }


def test_macos_probe_uses_exact_pinned_cross_compilation_and_both_real_runtime_architectures() -> None:
    jobs = workflow()["jobs"]
    build, runtime = jobs["macos-build"], jobs["macos-window"]
    assert build["runs-on"] == "ubuntu-24.04" and build["env"]["CGO_ENABLED"] == "0"
    assert build["env"]["GOTOOLCHAIN"] == "local" and runtime["needs"] == "macos-build"
    assert runtime["strategy"]["fail-fast"] is False
    assert runtime["strategy"]["matrix"]["include"] == [
        {"architecture": "amd64", "runner": "macos-15-intel"}, {"architecture": "arm64", "runner": "macos-15"},
    ]
    source = "\n".join(step.get("run", "") for step in build["steps"])
    for required in ["release/native_runtime_toolchain_v1.json", "artifact['sha256']", "go test -c", "GOOS=darwin", "go build -trimpath"]:
        assert required in source
    assert runtime["if"] == "github.event_name == 'workflow_dispatch' && inputs.verify_macos"
    assert build["if"] == "github.event_name == 'workflow_dispatch' && (inputs.verify_macos || inputs.verify_macos_product)"
    for job in [build, runtime]:
        assert job["defaults"]["run"]["shell"] == "bash"
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False


def test_macos_gate_requires_actual_native_test_success_and_exports_only_reports_for_collection() -> None:
    job = workflow()["jobs"]["macos-window"]
    source = "\n".join(step.get("run", "") for step in job["steps"])
    for required in ["platform.machine()", "sourceCommit", "sourceTree", "linuxGoArchiveSha256", "hashlib.file_digest",
                     "0xfeedfacf", "TestNativeMacWindowBootAndCancel", "set -o pipefail", "authenticatedDOMObserved",
                     "contextCancellationSettled", "productionApplicationWorkloadQualified", "NOT_READY"]:
        assert required in source
    upload = job["steps"][-1]
    assert upload["if"] == "always()"
    assert set(upload["with"]["path"].splitlines()) == {"artifacts/macos-window/*.json", "artifacts/macos-window/*.log"}
    assert "fixtures" not in upload["with"]["path"]


def test_embedded_python_is_executable_source_and_writes_real_newlines() -> None:
    programs = []
    for name in ["macos-build", "macos-window"]:
        for step in workflow()["jobs"][name]["steps"]:
            programs.extend(re.findall(r"python - <<'PY'\n(.*?)\nPY", step.get("run", ""), re.DOTALL))
    assert len(programs) == 5
    for program in programs:
        compile(program, "embedded-macos-workflow.py", "exec")
        assert not any(isinstance(node, ast.Constant) and node.value == "\\n" for node in ast.walk(ast.parse(program)))


def test_job_environment_does_not_use_the_step_only_runner_context() -> None:
    for job in workflow()["jobs"].values():
        assert not any("${{ runner." in str(value) for value in job.get("env", {}).values())


def test_windows_media_prebuild_preserves_complete_graphs_and_runtime_budget() -> None:
    steps = workflow()["jobs"]["windows-app"]["steps"]
    prebuild = next(step for step in steps if step.get("name") == "Build Windows media test binaries for all retained caller graphs")
    validate = next(step for step in steps if step.get("name") == "Verify streamed media images records compact results and digest corpus on Windows")
    assert steps.index(prebuild) < steps.index(validate)
    assert prebuild["timeout-minutes"] == 20 and validate["timeout-minutes"] == 10
    assert "@('worker', 'worker-policy', 'policy', 'gateway-policy', 'gateway')" in prebuild["run"]
    assert "-- test --locked @packages --lib --no-run" in prebuild["run"]
    assert "--no-run" not in validate["run"]
    assert "$LASTEXITCODE -ne 0" in prebuild["run"]
    assert not prebuild.get("continue-on-error", False) and not validate.get("continue-on-error", False)
