#!/usr/bin/env python3
"""Offline process qualification; Rust compilation belongs on the cloud runner."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
from rust_build import cache_environment, run_guarded  # noqa: E402

GO_VERSION = "go1.27.1"
FIXTURE = ROOT / "tests/native/agent_signing_process.go.txt"
PRIVATE_ENV = {
    "DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE",
    "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE",
    "DEEPSEEK_WORKER_TLS_KEY_FILE",
    "DEEPSEEK_WORKER_TLS_CERT_FILE",
    "DEEPSEEK_WORKER_AUTHORITY_NOW",
}


def child_environment(source: dict[str, str]) -> dict[str, str]:
    """No ambient worker identities, custody files, provider keys or fake clock."""
    return {key: value for key, value in source.items()
            if not key.upper().startswith(("DEEPSEEK_", "GO_CONTROL_", "AUTH_", "MINIO_", "AWS_"))
            and key.upper() not in {"GITHUB_TOKEN", "GH_TOKEN", "GITHUB_ACTIONS", "GOFLAGS", "RUSTUP_TOOLCHAIN"}}


def create_tls(directory: Path) -> tuple[Path, Path, Path]:
    """Disposable offline TLS fixture, using the PyCA X.509 tutorial's API."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(timezone.utc)
    authority_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "native-agent-probe-ca")])
    authority = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(authority_key.public_key())
                 .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                 .not_valid_after(now + timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                 .sign(authority_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
                   .issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .sign(authority_key, hashes.SHA256()))
    ca_file, cert_file, key_file = directory / "ca.pem", directory / "server.pem", directory / "server-key.pem"
    ca_file.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_file.chmod(0o600)
    return ca_file, cert_file, key_file


def validate_public_binding(value: object) -> dict[str, str]:
    fields = {"schema", "signerPublicKey", "fleetId", "environment", "domain", "runtime", "role"}
    if not isinstance(value, dict) or set(value) != fields or not all(isinstance(v, str) for v in value.values()):
        raise ValueError("Provisioner returned non-public or malformed metadata")
    if value["schema"] != "native-control-signer-binding-v1" or value["fleetId"] != "fleet-a" or value["environment"] != "production" or \
            value["domain"] != "action" or value["runtime"] != "go" or value["role"] != "control-plane":
        raise ValueError("Provisioner binding does not match the isolated deployment")
    public = value["signerPublicKey"]
    decoded = base64.b64decode(public + "=" * (-len(public) % 4), altchars=b"-_", validate=True)
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != public:
        raise ValueError("Provisioner public key is not canonical")
    return value


def stop_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=15)


def wait_json(path: Path, process: subprocess.Popen[bytes], timeout: int = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Producer exited before {path.name}")
        if path.is_file():
            if path.stat().st_size > 65536:
                raise ValueError("Oversized process handshake")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Invalid process handshake")
            return value
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {path.name}")


def start_worker(binary: Path, environment: dict[str, str], ca_file: Path, log_path: Path) -> tuple[subprocess.Popen[bytes], str]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    target = f"127.0.0.1:{port}"
    child = dict(environment, DEEPSEEK_WORKER_LISTEN=target)
    with log_path.open("wb") as log:
        process = subprocess.Popen([binary.as_posix()], cwd=ROOT, env=child, stdout=log, stderr=subprocess.STDOUT)
    try:
        context = ssl.create_default_context(cafile=ca_file.as_posix())
        context.set_alpn_protocols(["h2"])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Production Rust worker exited before TLS readiness")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1) as raw:
                    with context.wrap_socket(raw, server_hostname="localhost") as tls:
                        if tls.selected_alpn_protocol() != "h2":
                            raise ValueError("Worker TLS did not negotiate HTTP/2")
                return process, target
            except (ConnectionError, TimeoutError):
                time.sleep(0.1)
        raise TimeoutError("Production Rust TLS worker readiness timed out")
    except BaseException:
        stop_child(process)
        raise


def write_ready(path: Path, value: dict[str, str]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def process_cases(binary: Path, go_environment: dict[str, str], worker: Path, provisioner: Path, scratch: Path, output: Path) -> list[dict]:
    custody = scratch / "rust-custody"
    custody.mkdir(mode=0o700)
    password, bundle = custody / "passphrase", custody / "bundle.json"
    password.write_bytes(secrets.token_urlsafe(48).encode())
    password.chmod(0o600)
    provision_environment = child_environment(dict(os.environ))
    provision_environment.update({"DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE": bundle.as_posix(),
        "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE": password.as_posix(),
        "DEEPSEEK_WORKER_AUTHORITY_FLEET_ID": "fleet-a", "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT": "production"})
    completed = subprocess.run([provisioner.as_posix()], cwd=ROOT, env=provision_environment, capture_output=True, timeout=60, check=False)
    (output / "provisioning.json").write_text(json.dumps({"exitCode": completed.returncode,
        "stdoutSha256": hashlib.sha256(completed.stdout).hexdigest(), "stderrSha256": hashlib.sha256(completed.stderr).hexdigest()}) + "\n")
    if completed.returncode:
        raise RuntimeError("Production Rust provisioning failed; only output hashes are retained to protect custody")
    binding = validate_public_binding(json.loads(completed.stdout))
    (output / "rust-public-binding.json").write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8")
    ca_file, cert_file, key_file = create_tls(custody)
    results = []
    for scenario in ["plan", "tasks", "writer-expiry"]:
        case = scratch / scenario
        case.mkdir()
        bearer = secrets.token_urlsafe(48)
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        environment = dict(go_environment, DEEPSEEK_AGENT_SIGNING_PROBE_DIR=case.as_posix(),
                           DEEPSEEK_AGENT_SIGNING_PROBE_CASE=scenario, DEEPSEEK_AGENT_SIGNING_WORKER_BEARER=bearer)
        if PRIVATE_ENV & set(environment):
            raise ValueError("Custody configuration would enter Go")
        go_log = output / f"{scenario}-go.log"
        with go_log.open("wb") as log:
            producer = subprocess.Popen([binary.as_posix(), "-test.run=^TestNativeAgentSigningProcess$", "-test.v", "-test.timeout=4m"],
                cwd=ROOT / "go/internal/api", env=environment, stdout=log, stderr=subprocess.STDOUT)
        workers = []
        try:
            print(json.dumps({"scenario": scenario, "phase": "PROCESS_SIGNING", "status": "RUNNING"}), flush=True)
            ready = wait_json(case / "go-ready.json", producer)
            address = ready["origin"].removeprefix("http://").split(":")
            if len(address) != 2 or address[0] != "127.0.0.1" or not 0 < int(address[1]) <= 65535 or \
                    ready["fleetId"] != binding["fleetId"] or ready["environment"] != binding["environment"]:
                raise ValueError("Go fixture did not produce the expected isolated loopback deployment")
            state_root = case / "rust-state"
            state_root.mkdir()
            rust_environment = dict(provision_environment, DEEPSEEK_WORKER_TLS_CERT_FILE=cert_file.as_posix(),
                DEEPSEEK_WORKER_TLS_KEY_FILE=key_file.as_posix(), DEEPSEEK_WORKER_SERVICE_BEARER=bearer,
                DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT=expiry, DEEPSEEK_WORKER_SERVICE_NAME="go-control-plane",
                DEEPSEEK_WORKER_SERVICE_ROLE="controller", DEEPSEEK_WORKER_STATE_ROOT=state_root.as_posix(),
                DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=binding["signerPublicKey"],
                DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN=str(ready["fencingToken"]),
                GO_CONTROL_ADDR=ready["origin"], DEEPSEEK_INTERNAL_BEARER=ready["internalBearer"])
            live, target = start_worker(worker, rust_environment, ca_file, output / f"{scenario}-rust.log")
            workers.append(live)
            public_ready = {"Target": target, "TrustRootFile": ca_file.as_posix(), "PublicKey": binding["signerPublicKey"], "ExpiresAt": expiry}
            write_ready(case / "worker-ready.json", public_ready)
            wait_json(case / "restart-request.json", producer)
            stop_child(live)
            restarted, target = start_worker(worker, rust_environment, ca_file, output / f"{scenario}-rust-restarted.log")
            workers.append(restarted)
            write_ready(case / "worker-restarted.json", dict(public_ready, Target=target))
            code = producer.wait(timeout=180)
            result = json.loads((case / "process-result.json").read_text(encoding="utf-8")) if code == 0 else {}
            if code or result.get("status") != "PASS" or result.get("scenario") != scenario:
                raise RuntimeError(f"Actual {scenario} process signing failed; preserved logs, exit={code}")
            stop_child(restarted)
            journal = state_root / "rust-worker/control-signatures.sqlite3"
            with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                receipts = db.execute("SELECT request_id,document FROM signatures ORDER BY request_id").fetchall()
            hashes = [hashlib.sha256(row[1]).hexdigest() for row in receipts]
            if integrity != "ok" or len(receipts) != 2 or result["grantSha256"] not in hashes:
                raise ValueError("Rust durable journal does not contain exactly the epoch and verified immutable grant")
            result["journalIntegrity"] = integrity
            result["journalReceiptCount"] = len(receipts)
            result["journalDocumentsSha256"] = hashes
            results.append(result)
            (output / f"{scenario}-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"scenario": scenario, "status": "PASS", "journalReceipts": len(receipts)}), flush=True)
        finally:
            stop_child(producer)
            for owned in workers:
                stop_child(owned)
            for path in output.glob(f"{scenario}-*.log"):
                path.write_bytes(path.read_bytes().replace(bearer.encode(), b"[redacted-probe-bearer]"))
    return results


def record_command(command: list[str], cwd: Path, env: dict[str, str], output: Path, label: str, timeout: int = 300) -> None:
    completed = run_guarded(command, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    raw = (completed.stdout or b"") + (completed.stderr or b"")
    (output / f"{label}.log").write_bytes(raw)
    if completed.returncode:
        raise RuntimeError(f"{label} failed; preserved log, exit={completed.returncode}")
    if label == "go-producer" and any(f"--- PASS: TestNativeAgentSigningProducer/{phase}".encode() not in raw for phase in ("plan", "tasks")):
        raise ValueError("Producer exit success without both nonzero scenarios is not qualification")


def compile_producer(scratch: Path, output: Path, cache_root: Path) -> tuple[Path, dict[str, str]]:
    overlay_path = scratch / "overlay.json"
    virtual_path = ROOT / "go/internal/api/zz_native_agent_signing_process_test.go"
    overlay_path.write_text(json.dumps({"Replace": {virtual_path.as_posix(): FIXTURE.as_posix()}}) + "\n", encoding="utf-8")
    environment = cache_environment(ROOT, env=child_environment(dict(os.environ)), target_dir=cache_root)
    temporary = scratch / "go-tmp"
    temporary.mkdir()
    environment.update({"GOCACHE": cache_root.as_posix(), "GOTMPDIR": temporary.as_posix(), "GOTOOLCHAIN": "local",
                        "GOMAXPROCS": "2", "CGO_ENABLED": "0", "GOPROXY": "off", "GOSUMDB": "off", "GOWORK": "off"})
    version = subprocess.check_output(["go", "version"], env=environment, text=True).strip()
    if not version.startswith(f"go version {GO_VERSION} "):
        raise ValueError("Go toolchain does not match the repository pin")
    formatted = subprocess.check_output(["gofmt", "-l", FIXTURE.as_posix()], env=environment, text=True).strip()
    if formatted:
        raise ValueError("Go process fixture must be gofmt clean")
    binary = scratch / ("native-agent-api-tests.exe" if os.name == "nt" else "native-agent-api-tests")
    record_command(["go", "test", "-c", "-p=2", "-overlay", overlay_path.as_posix(), "-o", binary.as_posix(), "./internal/api"],
                   ROOT / "go", environment, output, "go-build", timeout=600)
    record_command([binary.as_posix(), "-test.run=^TestNativeAgentSigningProducer$", "-test.v", "-test.timeout=60s"],
                   ROOT / "go/internal/api", environment, output, "go-producer")
    (output / "producer.json").write_text(json.dumps({"goVersion": version, "productionCGOEnabled": False,
        "realUTC": True, "productionGoAuthorityAndRPC": True, "rustSigningQualified": False,
        "fixtureSha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), "privateCustodyVariablesExportedToGo": False}) + "\n")
    return binary, environment


def source_inventory() -> list[dict[str, str]]:
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--",
        "go", "rust", "proto", "scripts/verify_agent_signing_process.py", "tests/native/agent_signing_process.go.txt"], cwd=ROOT, env=environment).decode().split("\0")
    return [{"path": name, "sha256": hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}
            for name in sorted(set(names)) if name and (ROOT / name).is_file()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="Compile and exercise the real Go producer without a Rust build")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--go-cache", type=Path, required=True)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--provisioner", type=Path)
    args = parser.parse_args()
    output = args.out.resolve()
    if output.exists():
        raise ValueError("Refusing to overwrite prior qualification evidence")
    if not args.preflight and (args.worker is None or args.provisioner is None or not args.worker.is_file() or not args.provisioner.is_file()):
        raise ValueError("Full process proof requires existing cloud-built production Rust binaries; this script never compiles Rust")
    output.mkdir(parents=True)
    before = source_inventory()
    (output / "source-manifest.json").write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")
    inputs = []
    for path in [Path(__file__).resolve(), FIXTURE]:
        raw = path.read_bytes()
        (output / path.name).write_bytes(raw)
        inputs.append({"path": path.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(raw).hexdigest()})
    (output / "probe-inputs.json").write_text(json.dumps(inputs, indent=2) + "\n", encoding="utf-8")
    status = "FAIL"
    results = []
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with tempfile.TemporaryDirectory(prefix="agent-signing-", dir=output.parent) as directory:
            binary, environment = compile_producer(Path(directory), output, args.go_cache.resolve())
            if not args.preflight:
                results = process_cases(binary, environment, args.worker.resolve(), args.provisioner.resolve(), Path(directory), output)
        if source_inventory() != before:
            raise ValueError("Source inputs changed during process qualification")
        status = "PREFLIGHT_PASS" if args.preflight else "PASS"
        return 0
    finally:
        report: dict[str, object] = {"status": status, "checkedAt": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
                  "scope": "Go authority and Rust custody process signing" if not args.preflight else "Actual real-UTC Go producer and authenticated RPC only",
                  "rustCompiledByThisRunner": False, "rustSigningQualified": status == "PASS", "cases": results,
                  "runtimePythonQualified": False, "providerEffectsQualified": False, "readiness": "NOT_READY",
                  "startedAt": started_at, "sourceUnchanged": source_inventory() == before,
                  "sourceManifestSha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                  "goProcessEntryPoint": "Integration test of actual registered ControlRPC and Control store",
                  "fixtureMetadataArtifactReferences": True, "administrativePromotion": "Existing offline fixture key in isolated stores"}
        context_path = os.environ.get("DEEPSEEK_EVIDENCE_SOURCE_CONTEXT")
        if context_path:
            report["sourceContext"] = json.loads(Path(context_path).read_text(encoding="utf-8"))
        if not args.preflight:
            report["rustBinariesSha256"] = {str(path.name): hashlib.sha256(path.read_bytes()).hexdigest() for path in [args.worker, args.provisioner]}
        (output / "receipt.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
