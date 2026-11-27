from __future__ import annotations

from pathlib import Path
import re

import pytest
import yaml

from scripts.native_runtime_contract import check_all


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_native_contract_check_passes() -> None:
    report = check_all()
    assert report["ok"] is True
    assert report["go"] == "1.27.1"
    # Include browser, platform OCR, typed Agent nodes and media mutation contracts.
    assert report["proto_files"] == 12
    assert (ROOT / "proto/browser/v1/browser.proto").is_file()
    assert (ROOT / "proto/platform/v1/ocr.proto").is_file()
    assert (ROOT / "proto/agent/v1/agent_node.proto").is_file()
    assert (ROOT / "proto/media/v1/media.proto").is_file()
    assert (ROOT / "go/internal/protocol/mediav1/media.pb.go").is_file()
    assert (ROOT / "go/internal/protocol/mediav1/media_grpc.pb.go").is_file()
    # Agent run and node services each retain their generated gRPC binding.
    assert report["generated_outputs"] == 21
    assert (ROOT / "go/internal/protocol/agentv1/agent_grpc.pb.go").is_file()
    assert (ROOT / "go/internal/protocol/agentv1/agent_node_grpc.pb.go").is_file()


def test_go_shadow_process_cannot_mutate() -> None:
    shadow = _read("go/internal/shadow/shadow.go")
    config = _read("go/internal/config/config.go")
    fence = _read("go/internal/protocol/fence.go")
    assert "ErrMutationDenied" in fence
    assert "return protocol.DenyMutation()" in shadow
    assert re.search(r'ModeShadow\s*=\s*"shadow"', config)
    assert "database/sql" not in shadow
    assert "sqlite" not in shadow.lower()
    assert "C.CString" not in _read("go/cmd/deepseekd/main.go")


def test_rust_worker_rejects_stale_and_unknown() -> None:
    protocol = _read("rust/crates/deepseek-protocol/src/lib.rs")
    worker = _read("rust/crates/deepseek-worker/src/lib.rs")
    assert "STALE_EXECUTION_EPOCH" in protocol
    assert "EFFECT_UNKNOWN" in protocol
    assert "UnknownEffect" in worker
    assert "unsafe" not in protocol
    assert "unsafe" not in worker


def test_ci_has_native_go_and_protocol_gates() -> None:
    workflow = _read(".github/workflows/ci.yml")
    assert "native-go:" in workflow
    assert "native-protocol:" in workflow
    # Five jobs install the pinned toolchain: the fast Go gate, the two race suites
    # that now run concurrently, and the two protocol/S3 jobs. Each copy must be
    # accompanied by the checksum - a job that installs without verifying would be
    # worse than one that does not install at all - so the two counts must agree.
    go_jobs = ("native-go", "native-go-race", "native-go-android")
    installs = workflow.count("https://go.dev/dl/go1.27.1.linux-amd64.tar.gz")
    checksums = workflow.count("63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445")
    assert installs == checksums == 5
    # And every Go job carries the no-implicit-upgrade setting.
    assert workflow.count("GOTOOLCHAIN: local") >= len(go_jobs)
    assert "python scripts/native_codegen.py --check" in workflow
    assert "--require-hashes" in workflow
    assert "-r requirements-native-protocol.txt" in workflow
    assert "protoc-36.1-linux-x86_64.zip" in workflow
    assert "protoc-gen-go@v1.36.11" in workflow
    assert "protoc-gen-go-grpc@v1.6.2" in workflow
    assert "python scripts/native_runtime_contract.py --check" in workflow
    assert "python scripts/native_runtime_evidence.py" in workflow
    assert "python scripts/control_plane_shadow.py --check --export-report artifacts/control-shadow-report.json" in workflow
    assert (ROOT / "go/internal/store/control.go").is_file()
    assert (ROOT / "release/native_runtime_go_control_store_v1.json").is_file()
    assert (ROOT / "go/internal/scheduler/scheduler.go").is_file()
    assert (ROOT / "go/internal/resilience/risk.go").is_file()
    assert (ROOT / "go/internal/federation/trust.go").is_file()
    assert (ROOT / "go/pkg/protocol/canonical.go").is_file()
    assert (ROOT / "scripts/native_codegen.py").is_file()
    assert (ROOT / "scripts/check_native_contract_parity.py").is_file()
    jobs = yaml.safe_load(workflow)["jobs"]
    # The two race suites live in their own jobs so they run in parallel; each job
    # carries exactly one variant, and together they still cover both.
    race_commands = [
        line.strip()
        for name in go_jobs
        for step in jobs[name]["steps"]
        for line in step.get("run", "").splitlines()
        if line.strip().startswith("python ../scripts/run_native_go_race.py ")
    ]
    assert len(race_commands) == 2
    # --max-parallel 4 because the standard runner has 4 vCPU: four race processes is
    # one per core, not oversubscription. It was 2 with no stated reason, which left
    # half the runner idle for the whole suite.
    assert set(race_commands) == {
        f"python ../scripts/run_native_go_race.py --variant {variant} --out ../artifacts/go-race/{variant} --store-shards {shards} --max-parallel 4"
        for variant, shards in (("default", 8), ("android", 4))
    }
    race_runner = _read("scripts/run_native_go_race.py")
    assert 'GO_TIMEOUT = "90m"' in race_runner
    assert '"-test.count=1"' in race_runner
    assert '"-test.paniconexit0"' in race_runner
    assert '"go", "test", "-race", "-json", "-count=1"' in race_runner
    assert "TEST_CORPUS_INCOMPLETE_OR_FILTER_LEAK" in race_runner
    assert "SUBTEST_TERMINAL_MISSING_OR_DUPLICATED" in race_runner
    assert "UNREVIEWED_TEST_SKIP" in race_runner
    android_job = jobs["native-go-android"]
    assert android_job["env"]["GOFLAGS"] == "-tags=deepseek_android,sqlite3_flock"
    android = next(
        step for step in android_job["steps"] if step.get("name") == "Android Go SQLite compatibility checks"
    )
    assert "check_go_coverage.py --dir . --min 95.0 --profile android-coverage.out" in android["run"]
    assert "scripts/check_go_coverage.py" in workflow


@pytest.mark.parametrize("job_name", ["rust", "rust-coverage"])
def test_rust_gates_build_the_frontend_before_running_production_contracts(job_name: str) -> None:
    workflow = yaml.safe_load(_read(".github/workflows/ci.yml"))
    steps = workflow["jobs"][job_name]["steps"]
    node_index = next(index for index, step in enumerate(steps) if step.get("uses") == "actions/setup-node@v4")
    assert steps[node_index]["with"]["node-version"] == "24"
    install_index = next(index for index, step in enumerate(steps) if step.get("run") == "npm ci --prefix frontend")
    build_index = next(index for index, step in enumerate(steps) if step.get("run") == "npm run build --prefix frontend")
    gate_index = next(
        index for index, step in enumerate(steps)
        if "cargo test " in step.get("run", "") or "scripts/run_rust_coverage.py" in step.get("run", "")
    )
    assert node_index < install_index < build_index < gate_index


def test_ci_runs_real_go_to_rust_worker_boundary() -> None:
    workflow = _read(".github/workflows/ci.yml")
    native_go = workflow.split("  native-go:\n", 1)[1].split("  rust-coverage:\n", 1)[0]
    for required in (
        "dtolnay/rust-toolchain@1.85.0",
        "cargo build --locked --manifest-path ../rust/Cargo.toml -p deepseek-worker",
        "../rust/target/debug/deepseek-worker",
        "DEEPSEEK_WORKER_LISTEN: 127.0.0.1:50052",
        "DEEPSEEK_TEST_RUST_WORKER_TARGET: 127.0.0.1:50052",
        "TestRustWorkerWithoutAuthorityFailsClosedAndKeepsMissingEffectUnknown",
        "TestRustWorkerUnconfiguredInstallRemainsFailClosed",
        "TestRustWorkerInstallsEpochFromSignedAuthorityRequest",
        "DEEPSEEK_TEST_RUST_WORKER_AUTHORITY: \"1\"",
        "DEEPSEEK_WORKER_STATE_ROOT: ${{ runner.temp }}/deepseek-worker-authority",
        "DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY: 11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
        "trap cleanup EXIT",
        'kill -INT "$worker_pid"',
        'kill -TERM "$worker_pid"',
    ):
        assert required in native_go


def test_workspace_includes_native_crates() -> None:
    cargo = _read("rust/Cargo.toml")
    assert "crates/deepseek-protocol" in cargo
    assert "crates/deepseek-worker" in cargo
    assert "crates/deepseek-storage" in cargo
    assert "crates/deepseek-transfer" in cargo
    assert "crates/deepseek-federation" in cargo
    assert "crates/deepseek-proof" in cargo
