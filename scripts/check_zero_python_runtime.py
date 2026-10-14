#!/usr/bin/env python3
"""Source-contract checks for Zero-Python Production Runtime.

Enforces:
1. Docker Compose topology declares exclusively Rust + Go services (zero Python).
2. Native codebases (Rust/Go) have zero Python/PyO3/cgo dependencies.
3. Ownership matrix in release/native_runtime_ownership_v1.json designates zero Python target owners.
4. Mechanical writer denial blocks all Python writers for Go control domains.
5. Server startup is denied under python_disabled mode without DEEPSEEK_LEGACY_PYTHON=1.
6. Gateway reverse proxy is wired to Go control plane with fail-closed fallbacks.
7. Storage & transfer sources contain required native declarations and validation guards.

These checks do not observe built images, running processes, provider effects,
full route parity, or production ownership. Deployment qualification is separate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
    RUST_DATA_DOMAINS,
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
    executable_lines = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith("#"))
    # This mode denies the legacy runtime. Ignore only that exact environment
    # declaration; Python images, services and commands remain forbidden.
    scanned = re.sub(r"(?m)^\s+DEEPSEEK_RUNTIME_MODE: python_disabled\s*$", "", executable_lines)
    lowered = scanned.lower()
    if "python" in lowered:
        return GateCheckResult(
            name="topology_manifest",
            passed=False,
            details="Forbidden 'python' reference found in docker-compose.native.yml",
        )
    if re.search(r"\b(?:deepseek_infra|uvicorn|gunicorn)\b", lowered):
        return GateCheckResult(
            name="topology_manifest",
            passed=False,
            details="Python runtime entrypoint found in docker-compose.native.yml",
        )

    # Proxy configuration
    edge = _compose_service_text(executable_lines, "deepseek-edge")
    control = _compose_service_text(executable_lines, "deepseekd")
    worker = _compose_service_text(executable_lines, "deepseek-worker")
    if "GO_CONTROL_ADDR: http://127.0.0.1:8090" not in edge or "DEEPSEEK_RUNTIME_MODE: python_disabled" not in edge:
        return GateCheckResult(
            name="topology_manifest",
            passed=False,
            details="deepseek-edge requires the private loopback control origin and disabled legacy runtime",
        )
    if (
        'network_mode: "service:deepseek-edge"' not in control
        or 'network_mode: "service:deepseek-edge"' not in worker
        or "DEEPSEEKD_LISTEN: 127.0.0.1:8090" not in control
        or "DEEPSEEK_WORKER_LISTEN: 127.0.0.1:50052" not in worker
        or "deepseek-edge:\n        condition: service_started\n        restart: true" not in control
        or "deepseek-edge:\n        condition: service_started\n        restart: true" not in worker
    ):
        return GateCheckResult(
            name="topology_manifest",
            passed=False,
            details="Go control and Rust worker require private loopback listeners, the edge namespace and dependent restart",
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
        details="Native topology declares exclusively Rust+Go services with zero Python",
        data={"services": required_services},
    )


def _compose_service_text(content: str, service: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(service)}:\s*\n(.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", content)
    return match.group(1) if match else ""


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

        # The data plane is denied in the same mode, and deliberately *not* under
        # go_authoritative: ADR-0049 hands the control plane over first ("4.9.3 makes Go control
        # domains authoritative one at a time"), so during that window the data plane can still be
        # Python's. Checking both halves here is what keeps the asymmetry honest.
        denied_data_domains = []
        for domain in RUST_DATA_DOMAINS:
            try:
                assert_python_writer_allowed(domain)
            except PythonWriterMechanicallyDeniedError:
                denied_data_domains.append(domain)
            else:
                return GateCheckResult(
                    name="mechanical_writer_denial",
                    passed=False,
                    details=f"Writer for data domain {domain!r} was NOT mechanically denied in python_disabled mode",
                )

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
            details=(
                f"Mechanical writer denial active across all {len(denied_domains)} Go control domains "
                f"and all {len(denied_data_domains)} Rust data domains; startup gate enforces zero Python"
            ),
            data={
                "denied_domains": sorted(denied_domains),
                "denied_data_domains": sorted(denied_data_domains),
            },
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
        details="Native storage/transfer sources contain required declarations and validation guards",
    )


def check_gateway_route_cutover(root: Path) -> GateCheckResult:
    gateway_src = root / "rust" / "crates" / "deepseek-gateway" / "src"
    gateway_lib = gateway_src / "lib.rs"
    if not gateway_lib.is_file():
        return GateCheckResult(
            name="gateway_route_cutover",
            passed=False,
            details="deepseek-gateway src/lib.rs missing",
        )

    content = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(gateway_src.rglob("*.rs"))
    )
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
        details="Gateway sources declare the Go reverse proxy and fail-closed errors; route parity is unverified",
    )


def check_container_image_isolation(root: Path) -> GateCheckResult:
    from scripts.check_native_images import run_all_audits

    report = run_all_audits(root)
    if not report["passed"]:
        violations: list[str] = []
        for r in report["results"]:
            if not r["passed"]:
                violations.extend(f"{r['target']}: {v}" for v in r["violations"])
        return GateCheckResult(
            name="container_image_isolation",
            passed=False,
            details=f"Native container image audit failed: {'; '.join(violations)}",
            data=report,
        )

    return GateCheckResult(
        name="container_image_isolation",
        passed=True,
        details=f"All {report['audits_total']} container recipes pass source isolation checks; built images are unverified",
        data=report,
    )


def _native_supervisor_contract(path: Path, root: Path, text: str) -> bool:
    """Allow the bounded native launcher, never arbitrary control-plane exec."""
    if path.relative_to(root).as_posix() != "go/internal/launch/plan.go":
        return False
    calls = re.findall(r"exec\.Command(?:Context)?\s*\(([^)]*)\)", text)
    if [re.sub(r"\s+", "", call) for call in calls] != ["ctx,process.Path"]:
        return False
    names = re.search(r"names\s*:=\s*\[\]string\s*\{([^}]+)\}", text)
    if names is None or re.findall(r'"([^"]+)"', names.group(1)) != ["deepseekd", "deepseek-worker", "deepseek-gateway"]:
        return False
    guard = text.find("if legacyCommand(process.Path)")
    spawn = text.find("exec.CommandContext")
    legacy = re.search(r"func legacyCommand\(path string\) bool\s*\{(.*?)\n\}", text, re.DOTALL)
    if not 0 <= guard < spawn or legacy is None:
        return False
    blocked = ["python", "python.exe", "python3", "python3.exe", "node", "node.exe", "py", "py.exe"]
    return (all(f'"{name}"' in legacy.group(1) for name in blocked)
            and "return true" in legacy.group(1) and 'runtimeMode = "python_disabled"' in text)


def _native_linux_guardian_contract(path: Path, root: Path, text: str) -> bool:
    """A same-binary guardian admits the same four native targets only."""
    if path.relative_to(root).as_posix() != "go/internal/launch/guardian_linux.go":
        return False
    calls = re.findall(r"exec\.Command(?:Context)?\s*\(([^)]*)\)", text)
    if [re.sub(r"\s+", "", call) for call in calls] != ["process.Path"]:
        return False
    guard = text.find("if !guardianProcessAllowed(process)")
    spawn = text.find("exec.Command(process.Path)")
    fence = root / "go/internal/launch/process_fence_linux.go"
    plan = root / "go/internal/launch/plan.go"
    if not 0 <= guard < spawn or not fence.is_file() or not plan.is_file():
        return False
    fence_text = fence.read_text(encoding="utf-8")
    cases = re.search(r"case ([^:]+):", fence_text)
    names = re.findall(r'"([^"]+)"', cases.group(1)) if cases else []
    if names != ["deepseekd", "deepseek-worker", "deepseek-gateway", "deepseek-desktop"]:
        return False
    plan_text = plan.read_text(encoding="utf-8")
    return (_native_supervisor_contract(plan, root, plan_text)
            and "fence.prepare(process, cmd.Env)" in plan_text
            and 'guardianArgument = "--deepseek-internal-guardian-v1"' in fence_text
            and "filepath.IsAbs(process.Path) && !legacyCommand(process.Path)" in fence_text
            and all(token in text for token in [
                'os.NewFile(3, "native-root-lifetime")', "unix.CloseOnExec(int(pipe.Fd()))",
                "unix.PR_SET_CHILD_SUBREAPER", "unix.PidfdOpen", "unix.PidfdSendSignal",
                "if guardianChild(pid)", "killOwnedChildren()", "Setsid: true",
            ]))


def _native_browser_hook_contract(path: Path, root: Path, text: str) -> bool:
    """Permit only the retained Android ACTION_VIEW helper, not business exec."""
    if path.relative_to(root).as_posix() != "go/internal/desktop/browser_termux.go":
        return False
    calls = re.findall(r"exec\.Command(?:Context)?\s*\(([^)]*)\)", text)
    if [re.sub(r"\s+", "", call) for call in calls] != ["ctx,path,raw"]:
        return False
    if re.findall(r'exec\.LookPath\("([^"]+)"\)', text) != ["termux-open-url"]:
        return False
    guard = text.find("if err := validateBrowserURL(raw); err != nil")
    spawn = text.find("exec.CommandContext(ctx, path, raw)")
    keys = re.search(r"for _, key := range \[\]string\{([^}]+)\}", text)
    allowed = ["PATH", "HOME", "PREFIX", "TMPDIR", "LANG", "LC_ALL", "TERMUX_VERSION", "TERMUX__USER_ID",
               "ANDROID_ROOT", "ANDROID_DATA", "ANDROID_ART_ROOT", "ANDROID_I18N_ROOT", "ANDROID_TZDATA_ROOT",
               "BOOTCLASSPATH", "DEX2OATBOOTCLASSPATH", "SYSTEMSERVERCLASSPATH"]
    validator = path.with_name("browser.go")
    if not validator.is_file():
        return False
    validation = validator.read_text(encoding="utf-8")
    return (0 <= guard < spawn
            and "context.WithTimeout(ctx, 5*time.Second)" in text
            and "cmd.Env = termuxBrowserEnvironment()" in text
            and keys is not None and re.findall(r'"([^"]+)"', keys.group(1)) == allowed
            and not re.search(r"cmd\.(?:Args|Path|ExtraFiles)\s*=", text)
            and all(token in validation for token in ['u.Scheme != "http"', 'u.Scheme != "https"', "u.User != nil",
                                                     "net.ParseIP(u.Hostname())", "url.ParseQuery(u.RawQuery)",
                                                     "number < 1 || number > 65535", "strings.ContainsAny(raw"]))


def check_process_tree_isolation(root: Path) -> GateCheckResult:
    # 1. Audit Rust crates for forbidden process invocations
    rust_root = root / "rust" / "crates"
    forbidden_tokens = ["python", "python3", "pyo3", "cpython"]
    rust_violations: list[str] = []
    for rs_file in rust_root.rglob("*.rs"):
        if "tests" in rs_file.parts:
            continue
        text = rs_file.read_text(encoding="utf-8").lower()
        if "command::new" in text:
            for token in forbidden_tokens:
                if f'"{token}"' in text or f"'{token}'" in text:
                    rust_violations.append(f"{rs_file.relative_to(root)} spawns {token}")

    if rust_violations:
        return GateCheckResult(
            name="process_tree_isolation",
            passed=False,
            details=f"Rust production code spawns forbidden process: {'; '.join(rust_violations)}",
        )

    # The native supervisor legitimately starts Rust/Go children. Its bounded
    # binary set and interpreter guard are checked; other exec imports fail.
    go_root = root / "go"
    go_violations: list[str] = []
    native_supervisors = 0
    browser_handlers = 0
    for go_file in go_root.rglob("*.go"):
        if "_test.go" in go_file.name:
            continue
        text = go_file.read_text(encoding="utf-8")
        if '"os/exec"' in text:
            if (_native_supervisor_contract(go_file, root, text)
                    or _native_linux_guardian_contract(go_file, root, text)):
                native_supervisors += 1
            elif _native_browser_hook_contract(go_file, root, text):
                browser_handlers += 1
            else:
                go_violations.append(f"{go_file.relative_to(root)} imports os/exec outside the bounded native supervisor")

    if go_violations:
        return GateCheckResult(
            name="process_tree_isolation",
            passed=False,
            details=f"Go production code imports os/exec: {'; '.join(go_violations)}",
        )

    return GateCheckResult(
        name="process_tree_isolation",
        passed=True,
        details="Source invocation checks pass, including the bounded native supervisor; deployment process observation remains required",
        data={
            "rust_production_crates_checked": True,
            "go_production_packages_checked": True,
            "external_executors_detected": 0,
            "native_supervisors_checked": native_supervisors,
            "platform_browser_handlers_checked": browser_handlers,
            "external_executor_scope": "forbidden business/interpreter executors; platform browser hooks counted separately",
            "scope": "source_contract",
        },
    )


def run_all_checks(root: Path) -> dict[str, Any]:
    checks = [
        check_compose_topology(root),
        check_native_dependency_isolation(root),
        check_ownership_matrix(root),
        check_mechanical_writer_denial(),
        check_native_storage_and_transfer(root),
        check_gateway_route_cutover(root),
        check_container_image_isolation(root),
        check_process_tree_isolation(root),
    ]

    all_passed = all(c.passed for c in checks)
    return {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "scope": "source_contract",
        "deployment_verified": False,
        "status": "PASS" if all_passed else "FAIL",
        "passed": all_passed,
        "checks_total": len(checks),
        "checks_passed": sum(1 for c in checks if c.passed),
        "checks_failed": sum(1 for c in checks if not c.passed),
        "results": [asdict(c) for c in checks],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Zero-Python Production Runtime source contracts")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output")
    parser.add_argument("--strict", action="store_true", help="Exit 1 on any gate failure (CI default)")
    args = parser.parse_args(argv)

    report = run_all_checks(REPO_ROOT)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("=" * 70)
        print(" ZERO-PYTHON RUNTIME SOURCE-CONTRACT CHECKS")
        print("=" * 70)
        for check in report["results"]:
            icon = "[PASS]" if check["passed"] else "[FAIL]"
            print(f" {icon} {check['name']}: {check['details']}")
        print("-" * 70)
        print(f" Source-contract verdict: {report['status']} ({report['checks_passed']}/{report['checks_total']} passed)")
        print(" Deployment/process/provider qualification: UNVERIFIED")
        print("=" * 70)

    if not report["passed"] and args.strict:
        return 1
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
