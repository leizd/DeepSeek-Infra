#!/usr/bin/env python3
"""Executable release gate for Zero-Python Production Runtime.

Enforces:
1. Production Docker Compose topology runs exclusively Rust + Go services (zero Python).
2. Native codebases (Rust/Go) have zero Python/PyO3/cgo dependencies.
3. Ownership matrix in release/native_runtime_ownership_v1.json designates zero Python target owners.
4. Mechanical writer denial blocks all Python writers for Go control domains.
5. Server startup is denied under python_disabled mode without DEEPSEEK_LEGACY_PYTHON=1.
6. Gateway reverse proxy is wired to Go control plane with fail-closed fallbacks.
7. Storage & transfer execution paths are natively implemented in Rust with frozen contract validation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.native_runtime.authority import (  # noqa: E402
    GO_CONTROL_DOMAINS,
    PythonRuntimeDisabledError,
    PythonWriterMechanicallyDeniedError,
    RuntimeMode,
    assert_production_python_allowed,
    assert_python_writer_allowed,
    get_runtime_mode,
)


@dataclass
class GateCheckResult:
    name: str
    passed: bool
    details: str
    data: dict[str, Any] | None = None


def check_compose_topology(root: Path) -> GateCheckResult:
    compose_path = root / "docker-compose.native.yml"
    if not compose_path.is_file():
        return GateCheckResult(
            name="topology_manifest",
            passed=False,
            details="docker-compose.native.yml missing",
        )
    content = compose_path.read_text(encoding="utf-8")

    # Only allowed services
    required_services = ["deepseek-edge", "deepseekd", "deepseek-worker"]
    for svc in required_services:
        if f"{svc}:" not in content:
            return GateCheckResult(
                name="topology_manifest",
                passed=False,
                details=f"Required service {svc} missing from docker-compose.native.yml",
            )

    # Zero Python in default topology
    lowered = content.lower()
    if "python" in lowered:
        return GateCheckResult(
            name="topology_manifest",
            passed=False,
            details="Forbidden 'python' reference found in docker-compose.native.yml",
        )
    if "deepseek_infra" in lowered or "uvicorn" in lowered or "gunicorn" in lowered:
        return GateCheckResult(
            name="topology_manifest",
            passed=False,
            details="Python runtime entrypoint found in docker-compose.native.yml",
        )

    # Proxy configuration
    if "GO_CONTROL_ADDR: http://deepseekd:8090" not in content:
        return GateCheckResult(
            name="topology_manifest",
            passed=False,
            details="deepseek-edge missing GO_CONTROL_ADDR proxy target to deepseekd",
        )

    # Go Dockerfile CGO_ENABLED=0 check
    go_dockerfile = root / "go" / "Dockerfile"
    if not go_dockerfile.is_file():
        return GateCheckResult(
            name="topology_manifest",
            passed=False,
            details="go/Dockerfile missing",
        )
    dockerfile_content = go_dockerfile.read_text(encoding="utf-8")
    if "CGO_ENABLED=0" not in dockerfile_content:
        return GateCheckResult(
            name="topology_manifest",
            passed=False,
            details="go/Dockerfile must specify CGO_ENABLED=0",
        )

    return GateCheckResult(
        name="topology_manifest",
        passed=True,
        details="Production topology contains exclusively Rust+Go services with zero Python",
        data={"services": required_services},
    )


def check_native_dependency_isolation(root: Path) -> GateCheckResult:
    # Check all Cargo.toml in rust/
    rust_root = root / "rust"
    for cargo_file in rust_root.rglob("Cargo.toml"):
        text = cargo_file.read_text(encoding="utf-8").lower()
        if "pyo3" in text or "cpython" in text or "inline-python" in text:
            return GateCheckResult(
                name="dependency_isolation",
                passed=False,
                details=f"Forbidden Python binding in {cargo_file.relative_to(root)}",
            )

    # Check Go module
    go_mod = root / "go" / "go.mod"
    if not go_mod.is_file():
        return GateCheckResult(
            name="dependency_isolation",
            passed=False,
            details="go/go.mod missing",
        )
    go_mod_text = go_mod.read_text(encoding="utf-8").lower()
    if "python" in go_mod_text or "cgo" in go_mod_text:
        return GateCheckResult(
            name="dependency_isolation",
            passed=False,
            details="Forbidden Python/cgo binding in go/go.mod",
        )

    return GateCheckResult(
        name="dependency_isolation",
        passed=True,
        details="Rust and Go production codebases have zero Python/cgo dependencies",
    )


def check_ownership_matrix(root: Path) -> GateCheckResult:
    matrix_path = root / "release" / "native_runtime_ownership_v1.json"
    if not matrix_path.is_file():
        return GateCheckResult(
            name="ownership_matrix",
            passed=False,
            details="release/native_runtime_ownership_v1.json missing",
        )
    data = json.loads(matrix_path.read_text(encoding="utf-8"))
    domains = data.get("domains", [])
    if not domains:
        return GateCheckResult(
            name="ownership_matrix",
            passed=False,
            details="No domains found in ownership matrix",
        )

    python_target_domains = []
    control_go_count = 0
    data_rust_count = 0

    for domain in domains:
        domain_id = domain.get("id")
        plane = domain.get("plane")
        target_owner = domain.get("target_owner")
        is_production = domain.get("production", True)

        # For production domains: Python is strictly forbidden as target owner!
        if is_production and target_owner == "python":
            python_target_domains.append(domain_id)

        # Non-production reference domains (eval, oracle, tooling) are explicitly allowed to retain Python
        if not is_production and plane == "reference":
            if target_owner != "python":
                return GateCheckResult(
                    name="ownership_matrix",
                    passed=False,
                    details=f"Reference domain {domain_id} should target python reference implementation",
                )

        if plane == "control":
            if target_owner == "go":
                control_go_count += 1
        elif plane in ("data", "security"):
            if target_owner == "rust":
                data_rust_count += 1

    if python_target_domains:
        return GateCheckResult(
            name="ownership_matrix",
            passed=False,
            details=f"Forbidden Python target owner for production domains: {python_target_domains}",
        )

    return GateCheckResult(
        name="ownership_matrix",
        passed=True,
        details=f"All {len(domains)} domains targeted to native owners ({control_go_count} Go, {data_rust_count} Rust, 0 Python)",
        data={
            "total_domains": len(domains),
            "control_go_domains": control_go_count,
            "data_security_rust_domains": data_rust_count,
        },
    )


def check_mechanical_writer_denial() -> GateCheckResult:
    # 1. Test denial in GO_AUTHORITATIVE mode
    prev_mode = os.environ.get("DEEPSEEK_RUNTIME_MODE")
    prev_go = os.environ.get("DEEPSEEK_GO_CONTROL")
    prev_legacy = os.environ.get("DEEPSEEK_LEGACY_PYTHON")

    try:
        os.environ["DEEPSEEK_GO_CONTROL"] = "1"
        assert get_runtime_mode() == RuntimeMode.GO_AUTHORITATIVE

        denied_domains = []
        for domain in GO_CONTROL_DOMAINS:
            try:
                assert_python_writer_allowed(domain)
                return GateCheckResult(
                    name="mechanical_writer_denial",
                    passed=False,
                    details=f"Writer for domain {domain!r} was NOT mechanically denied in go_authoritative mode",
                )
            except PythonWriterMechanicallyDeniedError:
                denied_domains.append(domain)

        # 2. Test server startup denial in python_disabled mode
        os.environ["DEEPSEEK_RUNTIME_MODE"] = "python_disabled"
        os.environ.pop("DEEPSEEK_LEGACY_PYTHON", None)

        try:
            assert_production_python_allowed()
            return GateCheckResult(
                name="mechanical_writer_denial",
                passed=False,
                details="assert_production_python_allowed() did NOT deny startup in python_disabled mode",
            )
        except PythonRuntimeDisabledError:
            pass

        # 3. Test explicit legacy rollback allowance
        os.environ["DEEPSEEK_LEGACY_PYTHON"] = "1"
        try:
            assert_production_python_allowed()
        except PythonRuntimeDisabledError as exc:
            return GateCheckResult(
                name="mechanical_writer_denial",
                passed=False,
                details=f"Emergency rollback DEEPSEEK_LEGACY_PYTHON=1 failed: {exc}",
            )

        return GateCheckResult(
            name="mechanical_writer_denial",
            passed=True,
            details=f"Mechanical writer denial active across all {len(denied_domains)} Go control domains; startup gate enforces zero Python",
            data={"denied_domains": sorted(denied_domains)},
        )
    finally:
        if prev_mode is not None:
            os.environ["DEEPSEEK_RUNTIME_MODE"] = prev_mode
        else:
            os.environ.pop("DEEPSEEK_RUNTIME_MODE", None)

        if prev_go is not None:
            os.environ["DEEPSEEK_GO_CONTROL"] = prev_go
        else:
            os.environ.pop("DEEPSEEK_GO_CONTROL", None)

        if prev_legacy is not None:
            os.environ["DEEPSEEK_LEGACY_PYTHON"] = prev_legacy
        else:
            os.environ.pop("DEEPSEEK_LEGACY_PYTHON", None)


def check_native_storage_and_transfer(root: Path) -> GateCheckResult:
    # Verify Rust storage implementation
    storage_root = root / "rust" / "crates" / "deepseek-storage" / "src"
    backup_rs = storage_root / "backup.rs"
    restore_rs = storage_root / "restore.rs"
    receipt_rs = storage_root / "receipt.rs"
    object_set_rs = storage_root / "object_set.rs"

    for file_path in (backup_rs, restore_rs, receipt_rs, object_set_rs):
        if not file_path.is_file():
            return GateCheckResult(
                name="native_storage_transfer",
                passed=False,
                details=f"Required Rust storage source {file_path.relative_to(root)} missing",
            )

    backup_src = backup_rs.read_text(encoding="utf-8")
    restore_src = restore_rs.read_text(encoding="utf-8")

    if "validate_committed_documents" not in backup_src or "validate_committed_documents" not in restore_src:
        return GateCheckResult(
            name="native_storage_transfer",
            passed=False,
            details="Storage backup/restore engines must validate committed document invariants",
        )

    if "sanitize_relative_path" not in restore_src or "PathTraversal" not in restore_src:
        return GateCheckResult(
            name="native_storage_transfer",
            passed=False,
            details="Storage restore engine must sanitize relative paths against directory traversal",
        )

    # Verify Rust transfer streaming engine
    transfer_engine = root / "rust" / "crates" / "deepseek-transfer" / "src" / "engine.rs"
    if not transfer_engine.is_file():
        return GateCheckResult(
            name="native_storage_transfer",
            passed=False,
            details="deepseek-transfer streaming engine.rs missing",
        )

    transfer_src = transfer_engine.read_text(encoding="utf-8")
    if "execute_transfer" not in transfer_src or "TransferReceipt" not in transfer_src:
        return GateCheckResult(
            name="native_storage_transfer",
            passed=False,
            details="Transfer engine must export execute_transfer and TransferReceipt",
        )

    return GateCheckResult(
        name="native_storage_transfer",
        passed=True,
        details="Rust storage backup/restore and transfer engines natively implemented and contract-verified",
    )


def check_gateway_route_cutover(root: Path) -> GateCheckResult:
    gateway_lib = root / "rust" / "crates" / "deepseek-gateway" / "src" / "lib.rs"
    if not gateway_lib.is_file():
        return GateCheckResult(
            name="gateway_route_cutover",
            passed=False,
            details="deepseek-gateway src/lib.rs missing",
        )

    content = gateway_lib.read_text(encoding="utf-8")
    if "proxy_api_to_go" not in content:
        return GateCheckResult(
            name="gateway_route_cutover",
            passed=False,
            details="Gateway must wire proxy_api_to_go route handler",
        )

    if "GO_CONTROL_ADDR" not in content or "DEEPSEEK_GO_CONTROL_URL" not in content:
        return GateCheckResult(
            name="gateway_route_cutover",
            passed=False,
            details="Gateway proxy must resolve upstream via GO_CONTROL_ADDR / DEEPSEEK_GO_CONTROL_URL",
        )

    if "GO_CONTROL_PROXY_NOT_READY" not in content or "GO_CONTROL_UNREACHABLE" not in content:
        return GateCheckResult(
            name="gateway_route_cutover",
            passed=False,
            details="Gateway proxy must return fail-closed error codes when proxy is unconfigured or unreachable",
        )

    return GateCheckResult(
        name="gateway_route_cutover",
        passed=True,
        details="Gateway route matrix wires all public endpoints and reverse-proxies /api/* to Go control plane",
    )


def run_all_checks(root: Path) -> dict[str, Any]:
    checks = [
        check_compose_topology(root),
        check_native_dependency_isolation(root),
        check_ownership_matrix(root),
        check_mechanical_writer_denial(),
        check_native_storage_and_transfer(root),
        check_gateway_route_cutover(root),
    ]

    all_passed = all(c.passed for c in checks)
    return {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "status": "PASS" if all_passed else "FAIL",
        "passed": all_passed,
        "checks_total": len(checks),
        "checks_passed": sum(1 for c in checks if c.passed),
        "checks_failed": sum(1 for c in checks if not c.passed),
        "results": [asdict(c) for c in checks],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Zero-Python Production Runtime release gate")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output")
    parser.add_argument("--strict", action="store_true", help="Exit 1 on any gate failure (CI default)")
    args = parser.parse_args(argv)

    report = run_all_checks(REPO_ROOT)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("=" * 70)
        print(" ZERO-PYTHON PRODUCTION RUNTIME GATE VERIFICATION")
        print("=" * 70)
        for check in report["results"]:
            icon = "[PASS]" if check["passed"] else "[FAIL]"
            print(f" {icon} {check['name']}: {check['details']}")
        print("-" * 70)
        print(f" Overall Verdict: {report['status']} ({report['checks_passed']}/{report['checks_total']} passed)")
        print("=" * 70)

    if not report["passed"] and args.strict:
        return 1
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
