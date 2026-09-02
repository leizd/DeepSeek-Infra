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
    assert "go-version: \"1.27.1\"" in workflow
    assert "python scripts/native_runtime_contract.py --check" in workflow
    assert "python scripts/control_plane_shadow.py --check" in workflow
    assert (ROOT / "go/internal/scheduler/scheduler.go").is_file()
    assert (ROOT / "go/internal/resilience/risk.go").is_file()
    assert (ROOT / "go/internal/federation/trust.go").is_file()
    assert (ROOT / "go/pkg/protocol/canonical.go").is_file()
    assert (ROOT / "scripts/native_codegen.py").is_file()
    assert (ROOT / "scripts/check_native_contract_parity.py").is_file()
    assert "go test -race ./..." in workflow


def test_workspace_includes_native_crates() -> None:
    cargo = _read("rust/Cargo.toml")
    assert "crates/deepseek-protocol" in cargo
    assert "crates/deepseek-worker" in cargo
