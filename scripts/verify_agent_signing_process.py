#!/usr/bin/env python3
"""Offline process qualification; Rust compilation belongs on the cloud runner."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
import threading
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
            for name in ("goControlAuthorizationRPC", "goControlAuthorizationReplayPreserved", "goControlTerminalDenial"):
                if result.get(name) is not True:
                    raise ValueError(f"Actual production Go authorization path was not qualified: {name}")
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


def validate_dependent_task_request(exchange: dict[str, object], result: dict[str, object]) -> None:
    # The admitted dependent is critic: the retained capability contract gives
    # critic no tools. Its task, fence and predecessor summary remain required.
    if exchange.get("actionId") != result["actionId"] or exchange.get("executionEpoch") != str(result["executionEpoch"]) or \
            exchange.get("operationId") != result["dependentOperationId"] or exchange.get("model") != "deepseek-v4-pro" or \
            exchange.get("toolsPresent") is not False or exchange.get("dependentTaskInPrompt") is not True or exchange.get("predecessorSummaryInPrompt") is not True:
        raise ValueError("Dependent HTTP exchange lacks verified scope, role policy or Rust-owned predecessor output")


def validate_synthesis_request(exchange: dict[str, object], result: dict[str, object]) -> None:
    if exchange.get("actionId") != result.get("synthesisActionId") or exchange.get("actionId") == result.get("actionId") or \
            exchange.get("executionEpoch") != str(result.get("synthesisExecutionEpoch")) or \
            exchange.get("operationId") != result.get("synthesisOperationId") or exchange.get("model") != "deepseek-v4-pro" or \
            exchange.get("toolsPresent") is not False or exchange.get("synthesisQueryInPrompt") is not True or \
            exchange.get("synthesisCoderInPrompt") is not True or exchange.get("synthesisCriticInPrompt") is not True or \
            type(exchange.get("predecessorSummaryCount")) is not int or exchange.get("predecessorSummaryCount") != 2 or \
            exchange.get("privateReasoningInPrompt") is not False:
        raise ValueError("Synthesis exchange lacks its own verified scope and every confirmed predecessor")


def planner_process_cases(binary: Path, go_environment: dict[str, str], worker: Path, provisioner: Path,
                          scratch: Path, output: Path, family: str = "planner") -> list[dict]:
    """Controlled HTTP fixtures exercise the production executable, not a delegate.

    Python seeds only isolated oracle input bodies. Rust validates those exact
    references and is the sole writer of node outcomes and public result bodies.
    This does not prove live upstream availability or Go phase/DAG settlement.
    """
    if family not in {"planner", "task"}:
        raise ValueError("Unknown producer family")
    custody = scratch / f"rust-{family}-custody"
    custody.mkdir(mode=0o700)
    password, bundle = custody / "passphrase", custody / "bundle.json"
    password.write_bytes(secrets.token_urlsafe(48).encode())
    password.chmod(0o600)
    provision_environment = child_environment(dict(os.environ))
    provision_environment.update({"DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE": bundle.as_posix(),
        "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE": password.as_posix(),
        "DEEPSEEK_WORKER_AUTHORITY_FLEET_ID": "fleet-a", "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT": "production"})
    provisioned = subprocess.run([provisioner.as_posix()], cwd=ROOT, env=provision_environment,
                                capture_output=True, timeout=60, check=False)
    (output / f"{family}-provisioning.json").write_text(json.dumps({"exitCode": provisioned.returncode,
        "stdoutSha256": hashlib.sha256(provisioned.stdout).hexdigest(),
        "stderrSha256": hashlib.sha256(provisioned.stderr).hexdigest()}) + "\n", encoding="utf-8")
    if provisioned.returncode:
        raise RuntimeError("Planner custody provisioning failed; private output is not retained")
    binding = validate_public_binding(json.loads(provisioned.stdout))
    ca_file, cert_file, key_file = create_tls(custody)
    results = []
    scenarios = ["planner-success", "planner-unknown"] if family == "planner" else (["synthesis-success"] if family == "synthesis" else ["task-success", "task-tools", "task-unknown"])
    for scenario in scenarios:
        case = scratch / scenario
        case.mkdir()
        artifact_root = case / "rust-artifacts"
        artifact_root.mkdir(mode=0o700)
        request_body = {"requestPayload": {"messages": [{"role": "user", "content": "Prepare an isolated verification plan"}]}}
        request_bytes = json.dumps(request_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        reference = {"sha256": hashlib.sha256(request_bytes).hexdigest(), "length": len(request_bytes)}
        reference_file = case / "artifact-reference.json"
        reference_file.write_text(json.dumps(reference) + "\n", encoding="utf-8")
        catalog = artifact_root / "objects-v1.sqlite"
        with sqlite3.connect(catalog) as db:
            db.executescript("PRAGMA application_id=1146306882; PRAGMA user_version=1; "
                "CREATE TABLE artifacts(sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64), "
                "length INTEGER NOT NULL CHECK(length>0 AND length<=134217728), "
                "bytes BLOB NOT NULL CHECK(length(bytes)=length)) STRICT; "
                "CREATE TRIGGER artifacts_no_update BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT,'immutable artifact'); END; "
                "CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT,'immutable artifact'); END;")
            db.execute("INSERT INTO artifacts(sha256,length,bytes) VALUES (?,?,?)", (reference["sha256"], reference["length"], request_bytes))
        received = []
        plan = {"summary": "Controlled provider plan", "tasks": [{"role": "coder", "task": "inspect", "dependsOn": []}]}
        response_body = {"id": "planner-fixture", "model": "deepseek-v4-pro",
            "choices": [{"message": {"role": "assistant", "content": json.dumps(plan)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
        if family in {"task", "synthesis"}:
            message: dict[str, object] = {"role": "assistant", "content": "## 摘要\nControlled public task summary", "reasoning_content": "private-task-reasoning"}
            if scenario == "task-tools":
                message = {"role": "assistant", "content": "", "reasoning_content": "private-task-reasoning",
                           "tool_calls": [{"id": "task-call-1", "type": "function",
                                           "function": {"name": "read_file", "arguments": '{"path":"private-fixture-path"}'}}]}
            response_body["choices"] = [{"message": message, "finish_reason": "tool_calls" if scenario == "task-tools" else "stop"}]
        response_bytes = json.dumps(response_body, separators=(",", ":")).encode()
        synthesis_response = dict(response_body, choices=[{"message": {"role": "assistant", "content": "Controlled final synthesis", "reasoning_content": "private-synthesis-reasoning"}, "finish_reason": "stop"}])
        synthesis_response_bytes = json.dumps(synthesis_response, separators=(",", ":")).encode()


        class ProviderFixture(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024**2:
                    self.send_error(400)
                    return
                body = json.loads(self.rfile.read(length))
                received.append({"actionId": self.headers.get("X-DeepSeek-Action-Id"),
                    "executionEpoch": self.headers.get("X-DeepSeek-Execution-Epoch"),
                    "operationId": self.headers.get("X-DeepSeek-Node-Operation-Id"),
                    "model": body.get("model"), "stream": body.get("stream"), "toolsPresent": "tools" in body,
                    "selectedTaskInPrompt": "Agent 子任务：write" in json.dumps(body, ensure_ascii=False),
                    "dependentTaskInPrompt": "Agent 子任务：review" in json.dumps(body, ensure_ascii=False),
                    "predecessorSummaryInPrompt": "Controlled public task summary" in json.dumps(body, ensure_ascii=False),
                    "predecessorSummaryCount": json.dumps(body, ensure_ascii=False).count("Controlled public task summary"),
                    "synthesisQueryInPrompt": "用户原问题：Prepare an isolated verification plan" in json.dumps(body, ensure_ascii=False),
                    "synthesisCoderInPrompt": "任务：write" in json.dumps(body, ensure_ascii=False),
                    "synthesisCriticInPrompt": "任务：review" in json.dumps(body, ensure_ascii=False),
                    "privateReasoningInPrompt": "private-task-reasoning" in json.dumps(body, ensure_ascii=False)})
                reply = synthesis_response_bytes if scenario == "synthesis-success" and len(received) == 3 else response_bytes
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply) + (64 if scenario.endswith("-unknown") else 0)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(reply)
                self.wfile.flush()
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderFixture)
        provider_thread = threading.Thread(target=server.serve_forever, daemon=True)
        provider_thread.start()
        bearer = secrets.token_urlsafe(48)
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        environment = dict(go_environment, DEEPSEEK_AGENT_SIGNING_PROBE_DIR=case.as_posix(),
            DEEPSEEK_AGENT_SIGNING_PROBE_CASE=scenario, DEEPSEEK_AGENT_SIGNING_WORKER_BEARER=bearer,
            DEEPSEEK_AGENT_PLANNER_ARTIFACT_REFERENCE_FILE=reference_file.as_posix(),
            DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=binding["signerPublicKey"])
        if PRIVATE_ENV & set(environment) or "DEEPSEEK_API_KEY" in environment:
            raise ValueError("Planner Go environment contains private Rust configuration")
        workers = []
        producer = None
        try:
            with (output / f"{scenario}-go.log").open("wb") as log:
                test_name = "TestNativeAgentPlanProcess" if family == "planner" else "TestNativeAgentTaskProcess"
                producer = subprocess.Popen([binary.as_posix(), f"-test.run=^{test_name}$", "-test.v", "-test.timeout=4m"],
                    cwd=ROOT / "go/internal/api", env=environment, stdout=log, stderr=subprocess.STDOUT)
            ready = wait_json(case / "go-ready.json", producer)
            address = ready["origin"].removeprefix("http://").split(":")
            if len(address) != 2 or address[0] != "127.0.0.1" or not 0 < int(address[1]) <= 65535 or \
                    ready["fleetId"] != binding["fleetId"] or ready["environment"] != binding["environment"]:
                raise ValueError("Planner Go control endpoint is not the isolated loopback deployment")
            state_root = case / "rust-state"
            state_root.mkdir()
            rust_environment = dict(provision_environment, DEEPSEEK_WORKER_TLS_CERT_FILE=cert_file.as_posix(),
                DEEPSEEK_WORKER_TLS_KEY_FILE=key_file.as_posix(), DEEPSEEK_WORKER_SERVICE_BEARER=bearer,
                DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT=expiry, DEEPSEEK_WORKER_SERVICE_NAME="go-control-plane",
                DEEPSEEK_WORKER_SERVICE_ROLE="controller", DEEPSEEK_WORKER_STATE_ROOT=state_root.as_posix(),
                DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=binding["signerPublicKey"],
                DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN=str(ready["fencingToken"]), GO_CONTROL_ADDR=ready["origin"],
                DEEPSEEK_INTERNAL_BEARER=ready["internalBearer"], DEEPSEEK_AGENT_ARTIFACT_DIR=artifact_root.as_posix(),
                DEEPSEEK_INFRA_ROOT=case.as_posix(), DEEPSEEK_AGENT_DATA_MODE="python_disabled",
                DEEPSEEK_API_URL=f"http://127.0.0.1:{server.server_port}/chat", DEEPSEEK_API_KEY="isolated-fixture-credential")
            live, target = start_worker(worker, rust_environment, ca_file, output / f"{scenario}-rust.log")
            workers.append(live)
            public_ready = {"Target": target, "TrustRootFile": ca_file.as_posix(), "PublicKey": binding["signerPublicKey"], "ExpiresAt": expiry}
            write_ready(case / "worker-ready.json", public_ready)
            wait_json(case / "restart-request.json", producer)
            stop_child(live)
            restarted, target = start_worker(worker, rust_environment, ca_file, output / f"{scenario}-rust-restarted.log")
            workers.append(restarted)
            write_ready(case / "worker-restarted.json", dict(public_ready, Target=target))
            if scenario == "synthesis-success":
                wait_json(case / "synthesis-restart-request.json", producer)
                stop_child(restarted)
                restarted, target = start_worker(worker, rust_environment, ca_file, output / f"{scenario}-rust-synthesis-restarted.log")
                workers.append(restarted)
                write_ready(case / "worker-synthesis-restarted.json", dict(public_ready, Target=target))
            code = producer.wait(timeout=180)
            result = json.loads((case / "process-result.json").read_text(encoding="utf-8")) if code == 0 else {}
            expected_calls = 3 if scenario == "synthesis-success" else (2 if scenario == "task-success" else 1)
            if code or result.get("status") != "PASS" or result.get("scenario") != scenario or len(received) != expected_calls:
                raise RuntimeError(f"Planner process qualification failed: {scenario}, exit={code}, HTTP requests={len(received)}")
            sent = received[0]
            if sent["actionId"] != result["actionId"] or sent["executionEpoch"] != str(result["executionEpoch"]) or \
                    sent["operationId"] != result["operationId"] or sent["model"] != "deepseek-v4-pro" or \
                    sent["toolsPresent"] != (family in {"task", "synthesis"}) or (family in {"task", "synthesis"} and not sent["selectedTaskInPrompt"]):
                raise ValueError("Actual HTTP exchange did not carry the admitted planner identity and request policy")
            if scenario in {"task-success", "synthesis-success"}:
                dependent = received[1]
                validate_dependent_task_request(dependent, result)
            if scenario == "synthesis-success":
                validate_synthesis_request(received[2], result)
            stop_child(restarted)
            journal = state_root / "rust-worker/authority.sqlite3"
            with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Rust planner journal integrity failed")
                rows = db.execute("SELECT operation_id,state,output,output_sha256 FROM agent_node_effects").fetchall()
            journal_state = "CONFIRMED" if result["state"] == "AWAITING_TOOLS" else result["state"]
            by_operation = {row[0]: row for row in rows}
            expected_operations = {result["operationId"]}
            if scenario in {"task-success", "synthesis-success"}:
                expected_operations.add(result["dependentOperationId"])
            if scenario == "synthesis-success":
                expected_operations.add(result["synthesisOperationId"])
            if set(by_operation) != expected_operations or any(row[1] != journal_state for row in rows):
                raise ValueError("Rust journal did not retain exactly the admitted nodes")
            rows = [by_operation[result["operationId"]]] + ([by_operation[result["dependentOperationId"]]] if scenario in {"task-success", "synthesis-success"} else [])
            if scenario == "synthesis-success":
                rows.append(by_operation[result["synthesisOperationId"]])
            with sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Rust planner body catalog integrity failed")
                if scenario == "planner-success":
                    output_bytes = rows[0][2]
                    envelope = json.loads(output_bytes)
                    event_ref = result["publicEvent"]
                    body = db.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (event_ref["sha256"],)).fetchone()
                    if not body or body[0] != event_ref["length"] or hashlib.sha256(body[1]).hexdigest() != event_ref["sha256"] or \
                            json.loads(body[1]) != envelope["publicEvent"] or envelope["publicEvent"]["plan"] != plan or \
                            envelope["runId"] != result["runId"] or hashlib.sha256(output_bytes).hexdigest() != result["outcomeSha256"] or \
                            rows[0][3] != result["outcomeSha256"] or envelope["providerResponseSha256"] != hashlib.sha256(response_bytes).hexdigest():
                        raise ValueError("Confirmed provider bytes, immutable outcome and Rust public projection do not match")
                    expected_plan = [{"id": node["id"], "task": node["task"], "depends_on": node.get("depends_on", [])} for node in result["normalizedPlan"]]
                    for key, expected_fields in (("normalizedPlanBody", {"type": "agent_plan", "plan": expected_plan}),
                                                 ("runStatusBody", {"type": "run_status", "status": result["planNextStatus"]})):
                        reference = result[key]
                        retained_body = db.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (reference["sha256"],)).fetchone()
                        if not retained_body or retained_body[0] != reference["length"] or hashlib.sha256(retained_body[1]).hexdigest() != reference["sha256"]:
                            raise ValueError("Settled planner projection lacks immutable Rust body custody")
                        document = json.loads(retained_body[1])
                        if any(document.get(field) != expected for field, expected in expected_fields.items()):
                            raise ValueError("Go normalized plan/status diverges from the Rust public body")
                elif family in {"task", "synthesis"} and scenario != "task-unknown":
                    output_bytes = rows[0][2]
                    envelope = json.loads(output_bytes)
                    event_ref = result["publicEvent"]
                    body = db.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (event_ref["sha256"],)).fetchone()
                    if not body or body[0] != event_ref["length"] or hashlib.sha256(body[1]).hexdigest() != event_ref["sha256"] or \
                            json.loads(body[1]) != envelope["publicEvent"] or envelope["runId"] != result["runId"] or \
                            hashlib.sha256(output_bytes).hexdigest() != result["outcomeSha256"] or rows[0][3] != result["outcomeSha256"] or \
                            envelope["providerResponseSha256"] != hashlib.sha256(response_bytes).hexdigest() or db.execute("SELECT count(*) FROM artifacts").fetchone()[0] != (4 if scenario == "synthesis-success" else (3 if scenario == "task-success" else 2)):
                        raise ValueError("Task result lacks immutable Rust custody and exact provider response binding")
                    event = envelope["publicEvent"]
                    if event["phase"] != "coder" or b"private-task-reasoning" in body[1] or b"private-fixture-path" in body[1]:
                        raise ValueError("Task public projection contains private provider data")
                    if scenario in {"task-success", "synthesis-success"} and (event["type"] != "agent_output" or event["output"]["id"] != "coder" or event["output"]["task"] != "write"):
                        raise ValueError("Task output changed the Go-selected role or task")
                    if scenario in {"task-success", "synthesis-success"}:
                        dependent_ref = result["dependentPublicEvent"]
                        dependent_body = db.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (dependent_ref["sha256"],)).fetchone()
                        dependent_bytes = rows[1][2]
                        dependent_envelope = json.loads(dependent_bytes)
                        dependent_event = dependent_envelope["publicEvent"]
                        if not dependent_body or dependent_body[0] != dependent_ref["length"] or hashlib.sha256(dependent_body[1]).hexdigest() != dependent_ref["sha256"] or \
                                json.loads(dependent_body[1]) != dependent_event or dependent_envelope["runId"] != result["runId"] or \
                                hashlib.sha256(dependent_bytes).hexdigest() != result["dependentOutcomeSha256"] or rows[1][3] != result["dependentOutcomeSha256"] or \
                                dependent_envelope["providerResponseSha256"] != hashlib.sha256(response_bytes).hexdigest() or \
                                dependent_event["phase"] != "critic" or dependent_event["type"] != "agent_output" or dependent_event["output"]["id"] != "critic" or \
                                dependent_event["output"]["task"] != "review" or b"private-task-reasoning" in dependent_body[1]:
                            raise ValueError("Dependent result lacks exact signed Rust custody")
                    if scenario == "synthesis-success":
                        synthesis_ref = result["synthesisPublicEvent"]
                        synthesis_body = db.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (synthesis_ref["sha256"],)).fetchone()
                        synthesis_bytes = rows[2][2]
                        synthesis_envelope = json.loads(synthesis_bytes)
                        if not synthesis_body or synthesis_body[0] != synthesis_ref["length"] or hashlib.sha256(synthesis_body[1]).hexdigest() != synthesis_ref["sha256"] or \
                                json.loads(synthesis_body[1]) != {"type": "content", "text": "Controlled final synthesis"} or \
                                synthesis_envelope["publicEvent"] != json.loads(synthesis_body[1]) or synthesis_envelope["runId"] != result["runId"] or \
                                hashlib.sha256(synthesis_bytes).hexdigest() != result["synthesisOutcomeSha256"] or rows[2][3] != result["synthesisOutcomeSha256"] or \
                                synthesis_envelope["providerResponseSha256"] != hashlib.sha256(synthesis_response_bytes).hexdigest() or \
                                synthesis_envelope["synthesisOutput"]["usage"] != synthesis_response["usage"] or \
                                not isinstance(synthesis_envelope["synthesisOutput"]["latencyMs"], int) or synthesis_envelope["synthesisOutput"]["latencyMs"] < 0 or \
                                b"private-synthesis-reasoning" in synthesis_bytes or b"private-task-reasoning" in synthesis_body[1]:
                            raise ValueError("Synthesis lacks exact provider body custody and private-data filtering")
                    if scenario == "task-tools" and (event["type"] != "agent_tool_request" or event.get("output") or
                            envelope["toolContinuation"]["assistant"]["reasoning_content"] != "private-task-reasoning" or
                            len(envelope["toolContinuation"]["toolCalls"]) != 1):
                        raise ValueError("Tool request was lost or became task completion")
                elif rows[0][2:] != (None, None) or db.execute("SELECT count(*) FROM artifacts").fetchone()[0] != 1:
                    raise ValueError("An uncertain HTTP effect acquired a successful result body")
            signatures = state_root / "rust-worker/control-signatures.sqlite3"
            with sqlite3.connect(signatures.as_uri() + "?mode=ro", uri=True) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Rust outcome signature journal integrity failed")
                documents = [row[0] for row in db.execute("SELECT document FROM signatures")]
            if not result.get("productionGoNodeClient"):
                raise ValueError("Planner probe bypassed the production Go node client")
            if scenario == "planner-success":
                if not all(result.get(key) for key in ("historicalReceiptAfterActionTakeover", "historicalReceiptAfterGoRestart", "takeoverDidNotReauthorizeProvider", "expiredGrantReexecutionDenied", "goPlanPhaseSettledAtomically", "goPlanPhaseReplayAfterRestart", "normalizedProjectionAfterRustRestart", "goPlanPhaseSettlementQualified")):
                    raise ValueError("Historical receipt lacks real UTC takeover, Go restart or duplicate-dispatch denial")
                receipts = [body for body in documents if hashlib.sha256(body).hexdigest() == result["outcomeReceiptSha256"]]
                if not result.get("goOutcomePersisted") or not result.get("signedOutcomeVerified") or not result.get("signedOutcomeTamperingDenied") or len(receipts) != 1 or \
                        len(receipts[0]) != result["outcomeReceiptLength"] or len(documents) != 4:
                    raise ValueError("Confirmed Rust result lacks an immutable signature verified by production Go")
                projections = [body for body in documents if hashlib.sha256(body).hexdigest() == result["projectionReceiptSha256"]]
                if len(projections) != 1 or len(projections[0]) != result["projectionReceiptLength"]:
                    raise ValueError("Planner settlement lacks a unique immutable Rust projection signature")
            elif family in {"task", "synthesis"}:
                if not all(result.get(key) is True for key in ("rustRestartPreserved", "goRestartPreserved", "duplicateProviderDispatchDenied")):
                    raise ValueError("Task custody recovery is unproven")
                if scenario in {"task-success", "synthesis-success"}:
                    if not all(result.get(key) is True for key in ("goTaskPhaseSettlementQualified", "resourcesReleased", "phaseCompletionReplayedAfterGoRestart", "publicRunRemainsRunning")) or result.get("resourcesRetained"):
                        raise ValueError("Verified task phase completion, resource release and recovery are unproven")
                elif not result.get("resourcesRetained") or result.get("resourcesReleased") or result.get("goTaskPhaseSettlementQualified"):
                    raise ValueError("Unresolved task phase released resources or claimed completion")
                if scenario in {"task-success", "synthesis-success"}:
                    receipts = [body for body in documents if hashlib.sha256(body).hexdigest() == result["outcomeReceiptSha256"]]
                    if not result.get("goOutcomePersisted") or not result.get("signedOutcomeVerified") or len(documents) != (7 if scenario == "synthesis-success" else 4) or len(receipts) != 1 or len(receipts[0]) != result["outcomeReceiptLength"]:
                        raise ValueError("Task receipt lacks actual Go verification and durable ingestion")
                    dependent_receipts = [body for body in documents if hashlib.sha256(body).hexdigest() == result["dependentReceiptSha256"]]
                    if len(dependent_receipts) != 1 or len(dependent_receipts[0]) != result["dependentReceiptLength"] or \
                            not result.get("goTaskNodeSettlementQualified") or not result.get("dependencyDispatchQualified"):
                        raise ValueError("Dependency requires independently verified Go completion and retained Rust output")
                    if scenario == "synthesis-success":
                        synthesis_receipts = [body for body in documents if hashlib.sha256(body).hexdigest() == result["synthesisReceiptSha256"]]
                        if len(synthesis_receipts) != 1 or len(synthesis_receipts[0]) != result["synthesisReceiptLength"] or \
                                not all(result.get(key) is True for key in ("synthesisGoProofPersisted", "synthesisRustRestartPreserved", "synthesisGoRestartPreserved", "synthesisDuplicateProviderDispatchDenied")) or \
                                result.get("synthesisFinalRunSettlementQualified") is not False:
                            raise ValueError("Synthesis signature recovery is unproven or final Go settlement was fabricated")
                elif not result.get("unknownHasNoReceipt") or result.get("signedOutcomeVerified") or result.get("goOutcomePersisted") or len(documents) != 2:
                    raise ValueError("Incomplete task acquired a signed completion or Go receipt")
            elif not result.get("unknownOutcomeRecordingDenied") or not result.get("unknownHasNoReceipt") or result.get("signedOutcomeVerified") or len(documents) != 2:
                raise ValueError("Uncertain effect acquired a signature or successful Go outcome")
            result.update(outcomeSignatureJournalVerified=True, actualHTTPRequestCount=len(received), httpFenceVerified=True, bodyCustodyVerified=True,
                inputBodySeededByOfflineOracle=True, outputBodyWriter="rust", provider="controlled-loopback-http-fixture")
            results.append(result)
            (output / f"{scenario}-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"scenario": scenario, "status": "PASS", "HTTPRequests": len(received), "state": result["state"]}), flush=True)
        finally:
            if producer is not None:
                stop_child(producer)
            for owned in workers:
                stop_child(owned)
            server.shutdown()
            server.server_close()
            provider_thread.join(timeout=5)
            for path in output.glob(f"{scenario}-*.log"):
                path.write_bytes(path.read_bytes().replace(bearer.encode(), b"[redacted-probe-bearer]"))
    return results


def record_command(command: list[str], cwd: Path, env: dict[str, str], output: Path, label: str, timeout: int = 300) -> None:
    completed = run_guarded(command, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    raw = (completed.stdout or b"") + (completed.stderr or b"")
    (output / f"{label}.log").write_bytes(raw)
    if completed.returncode:
        raise RuntimeError(f"{label} failed; preserved log, exit={completed.returncode}")
    if label == "go-producer" and any(f"--- PASS: TestNativeAgentSigningProducer/{phase}".encode() not in raw for phase in ("plan", "tasks", "writer-expiry")):
        raise ValueError("Producer exit success without all three nonzero scenarios is not qualification")


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
    record_command([binary.as_posix(), "-test.run=^TestNativeAgentSigningProducer$", "-test.v", "-test.timeout=120s"],
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
    planner_results = []
    task_results = []
    synthesis_results = []
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with tempfile.TemporaryDirectory(prefix="agent-signing-", dir=output.parent) as directory:
            binary, environment = compile_producer(Path(directory), output, args.go_cache.resolve())
            if not args.preflight:
                results = process_cases(binary, environment, args.worker.resolve(), args.provisioner.resolve(), Path(directory), output)
                planner_results = planner_process_cases(binary, environment, args.worker.resolve(), args.provisioner.resolve(), Path(directory), output)
                task_results = planner_process_cases(binary, environment, args.worker.resolve(), args.provisioner.resolve(), Path(directory), output, "task")
                synthesis_results = planner_process_cases(binary, environment, args.worker.resolve(), args.provisioner.resolve(), Path(directory), output, "synthesis")
        if source_inventory() != before:
            raise ValueError("Source inputs changed during process qualification")
        status = "PREFLIGHT_PASS" if args.preflight else "PASS"
        return 0
    finally:
        report: dict[str, object] = {"status": status, "checkedAt": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
                  "scope": "Go authority and Rust custody process signing" if not args.preflight else "Actual real-UTC Go producer and authenticated RPC only",
                  "rustCompiledByThisRunner": False, "rustSigningQualified": status == "PASS", "cases": results,
                  "plannerProcessCases": planner_results, "plannerControlledHTTPQualified": status == "PASS" and len(planner_results) == 2,
                  "taskProcessCases": task_results, "taskControlledHTTPQualified": status == "PASS" and len(task_results) == 3,
                  "taskNodeSettlementQualified": status == "PASS" and any(case.get("goTaskNodeSettlementQualified") is True for case in task_results),
                  "dependencyDispatchQualified": status == "PASS" and any(case.get("dependencyDispatchQualified") is True for case in task_results),
                  "taskPhaseSettlementQualified": status == "PASS" and any(case.get("goTaskPhaseSettlementQualified") is True for case in task_results),
                  "synthesisProcessCases": synthesis_results,
                  "synthesisControlledHTTPQualified": status == "PASS" and len(synthesis_results) == 1,
                  "synthesisFinalRunSettlementQualified": False,
                  "allTaskPhasesAndDAGQualified": False,
                  "runtimePythonQualified": False, "providerEffectsQualified": False, "readiness": "NOT_READY",
                  "startedAt": started_at, "sourceUnchanged": source_inventory() == before,
                  "sourceManifestSha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                  "goProcessEntryPoint": "Integration test of actual registered ControlRPC and Control store",
                  "goControlAuthorizationRPCQualified": status == "PASS" and all(
                      case.get("goControlAuthorizationRPC") is True and case.get("goControlAuthorizationReplayPreserved") is True
                      and case.get("goControlTerminalDenial") is True for case in results),
                  "fixtureMetadataArtifactReferences": True, "administrativePromotion": "Existing offline fixture key in isolated stores"}
        context_path = os.environ.get("DEEPSEEK_EVIDENCE_SOURCE_CONTEXT")
        if context_path:
            report["sourceContext"] = json.loads(Path(context_path).read_text(encoding="utf-8"))
        if not args.preflight:
            report["rustBinariesSha256"] = {str(path.name): hashlib.sha256(path.read_bytes()).hexdigest() for path in [args.worker, args.provisioner]}
        (output / "receipt.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
