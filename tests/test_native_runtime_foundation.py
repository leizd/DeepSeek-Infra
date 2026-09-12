from __future__ import annotations

from pathlib import Path

from scripts.native_runtime_contract import check_all


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_native_contract_check_passes() -> None:
    report = check_all()
    assert report["ok"] is True
    assert report["go"] == "1.27.1"
    assert report["proto_files"] == 7
    assert report["generated_outputs"] == 10


def test_go_shadow_process_cannot_mutate() -> None:
    shadow = _read("go/internal/shadow/shadow.go")
    config = _read("go/internal/config/config.go")
    fence = _read("go/internal/protocol/fence.go")
    assert "ErrMutationDenied" in fence
    assert "return protocol.DenyMutation()" in shadow
    assert 'ModeShadow        = "shadow"' in config
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
    assert workflow.count("https://go.dev/dl/go1.27.1.linux-amd64.tar.gz") == 2
    assert workflow.count("63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445") == 2
    assert workflow.count("GOTOOLCHAIN: local") >= 2
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
    assert "go test -race ./..." in workflow
    assert "scripts/check_go_coverage.py" in workflow


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
