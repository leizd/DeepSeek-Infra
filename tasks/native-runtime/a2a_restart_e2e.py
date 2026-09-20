"""Offline acceptance harness for the real Go/Rust A2A process boundary.

Uses isolated temporary stores and a controlled HTTP model provider. Python is
only the test driver; both production listeners run as native child processes.
This is task-lifecycle evidence, not storage-provider or full cutover evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def certificates(root: Path) -> None:
    now = datetime.now(timezone.utc)
    ca_key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "A2A isolated acceptance")])

    def builder(subject: x509.Name, key: ed25519.Ed25519PrivateKey) -> x509.CertificateBuilder:
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1)))

    ca = builder(name, ca_key).add_extension(x509.BasicConstraints(ca=True, path_length=0), True).sign(ca_key, None)
    (root / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for role, usage in [("server", ExtendedKeyUsageOID.SERVER_AUTH), ("client", ExtendedKeyUsageOID.CLIENT_AUTH)]:
        key = ed25519.Ed25519PrivateKey.generate()
        cert_builder = builder(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, role)]), key)
        cert_builder = cert_builder.add_extension(x509.ExtendedKeyUsage([usage]), False)
        cert = cert_builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                  x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False).sign(ca_key, None)
        (root / f"{role}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (root / f"{role}.key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_port(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"native process exited during startup: {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("native listener startup timed out")


def run(go_binary: Path, rust_binary: Path, artifact: Path) -> dict[str, Any]:
    checks: list[str] = []
    killed: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    received = {name: threading.Event() for name in ("go-crash", "rust-crash", "cancel")}
    release = {name: threading.Event() for name in received}
    counts_lock = threading.Lock()
    credential = secrets.token_hex(24)

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            if self.headers.get("Authorization") != f"Bearer {credential}":
                self.send_error(401)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = str(body["messages"][1]["content"])
            with counts_lock:
                counts[text] = counts.get(text, 0) + 1
            if text in received:
                received[text].set()
                if not release[text].wait(100):
                    self.send_error(504)
                    return
            payload = json.dumps({"id": "local-answer", "model": "deepseek-v4-pro", "choices": [{"message": {
                "role": "assistant", "content": "native answer: " + text}, "finish_reason": "stop"}]}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Killing/canceling the native executor intentionally closes this socket.

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    children: list[subprocess.Popen[bytes]] = []
    logs: list[Any] = []
    with tempfile.TemporaryDirectory(prefix="native-a2a-") as temporary:
        root = Path(temporary)
        certificates(root)
        (root / "static/ui").mkdir(parents=True)
        (root / "static/ui/index.html").write_text("<!doctype html><title>A2A acceptance</title>", encoding="utf-8")
        base = {key: value for key, value in os.environ.items()
                if not key.startswith(("DEEPSEEK", "GATEWAY_", "AUTH_", "A2A_"))}
        go_port, rust_port = free_port(), free_port()
        go_env = dict(base, DEEPSEEKD_LISTEN="127.0.0.1:0", DEEPSEEKD_MODE="shadow",
                      DEEPSEEKD_A2A_LISTEN=f"127.0.0.1:{go_port}", DEEPSEEKD_A2A_STORE=str(root / "go-control"),
                      DEEPSEEKD_A2A_TLS_CERT=str(root / "server.pem"), DEEPSEEKD_A2A_TLS_KEY=str(root / "server.key"),
                      DEEPSEEKD_A2A_TLS_CA=str(root / "ca.pem"))
        rust_env = dict(base, GATEWAY_BIND_ADDR=f"127.0.0.1:{rust_port}", DEEPSEEK_INFRA_ROOT=str(root),
                        DEEPSEEK_INFRA_STATIC_DIR=str(root / "static"), DEEPSEEK_RUNTIME_MODE="python_disabled",
                        AUTH_TOKEN=credential, AUTH_DISABLED="false", A2A_ENABLED="true",
                        DEEPSEEK_API_KEY=credential,
                        DEEPSEEK_API_URL=f"http://127.0.0.1:{provider.server_port}/chat/completions",
                        DEEPSEEK_A2A_CONTROL_URL=f"https://127.0.0.1:{go_port}",
                        DEEPSEEK_A2A_TLS_CA=str(root / "ca.pem"), DEEPSEEK_A2A_TLS_SERVER_NAME="localhost",
                        DEEPSEEK_A2A_TLS_CERT=str(root / "client.pem"), DEEPSEEK_A2A_TLS_KEY=str(root / "client.key"))

        def start(binary: Path, env: dict[str, str], port: int) -> subprocess.Popen[bytes]:
            log = (root / f"process-{len(children)}.log").open("wb")
            logs.append(log)
            child = subprocess.Popen([str(binary)], cwd=root, env=env, stdout=log, stderr=log,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            children.append(child)
            wait_port(child, port)
            return child

        def kill(child: subprocess.Popen[bytes], role: str) -> None:
            assert child.poll() is None, "process exited before requested crash"
            child.kill()
            child.wait(timeout=10)
            killed.append({"role": role, "pid": child.pid, "exit_code": child.returncode})

        def rpc(method: str, params: dict[str, Any], *, auth: bool = True) -> Any:
            request = Request(f"http://127.0.0.1:{rust_port}/a2a/agents/reasoner",
                              json.dumps({"jsonrpc": "2.0", "id": "acceptance", "method": method, "params": params}).encode(),
                              {"Content-Type": "application/json", **({"Authorization": f"Bearer {credential}"} if auth else {})})
            with urlopen(request, timeout=10) as response:
                data = response.read()
                if "event-stream" in response.headers.get("Content-Type", ""):
                    assert b"[DONE]" not in data
                    return [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: ")]
                return json.loads(data)

        def task_state(task_id: str, expected: str, timeout: float = 12) -> dict[str, Any]:
            deadline = time.monotonic() + timeout
            value: dict[str, Any] = {}
            while time.monotonic() < deadline:
                try:
                    value = rpc("tasks/get", {"id": task_id})
                    if value.get("result", {}).get("status", {}).get("state") == expected:
                        return dict(value["result"])
                    state = value.get("result", {}).get("status", {}).get("state")
                    if state in {"completed", "failed", "canceled"}:
                        raise AssertionError(f"unexpected terminal state, wanted {expected}: {value}")
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.1)
            raise AssertionError(f"task did not become {expected}: {value}")

        def submit(text: str) -> str:
            reply = rpc("message/send", {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]}})
            assert "result" in reply, reply
            return str(reply["result"]["id"])

        def passed(name: str) -> None:
            checks.append(name)
            print(f"PASS {name}", flush=True)

        try:
            go = start(go_binary, go_env, go_port)
            rust = start(rust_binary, rust_env, rust_port)
            try:
                rpc("tasks/list", {}, auth=False)
                raise AssertionError("unauthenticated public request accepted")
            except HTTPError as error:
                assert error.code == 401
            passed("public_authentication")
            complete_id = submit("complete")
            completed = task_state(complete_id, "completed")
            assert len(completed["artifactChunks"]) == 2
            assert completed["artifacts"][0]["parts"][0]["text"] == "native answer: complete"
            passed("native_execution_and_durable_result")
            coerced = rpc("message/send", {"contextId": False, "message": {
                "role": "user", "parts": [{"kind": "text", "text": True}],
                "contextId": [True], "messageId": None, "kind": None,
                "metadata": {"largeInteger": 9007199254740993}}})["result"]
            coerced = task_state(coerced["id"], "completed")
            assert coerced["contextId"] == "[True]"
            assert coerced["history"][0]["messageId"] is None
            assert coerced["history"][0]["kind"] is None
            assert coerced["history"][0]["metadata"]["largeInteger"] == 9007199254740993
            assert coerced["artifacts"][0]["parts"][0]["text"] == "native answer: True"
            passed("python_message_coercion_preserves_context_and_extension_metadata")
            kill(rust, "rust-edge")
            rust = start(rust_binary, rust_env, rust_port)
            assert task_state(complete_id, "completed") == completed
            events = rpc("tasks/resubscribe", {"id": complete_id, "afterChunkIndex": 0})
            assert len(events) == 3, events
            assert events[1]["result"]["chunkIndex"] == 1
            assert events[-1]["result"]["final"] is True
            passed("rust_restart_resumes_only_missing_answer_chunk")
            kill(go, "go-controller")
            unavailable = rpc("tasks/get", {"id": complete_id})
            assert unavailable["error"]["code"] == -32603, unavailable
            go = start(go_binary, go_env, go_port)
            assert task_state(complete_id, "completed") == completed
            passed("go_restart_preserves_completed_snapshot_without_fallback")
            crashed_id = submit("go-crash")
            assert received["go-crash"].wait(10)
            task_state(crashed_id, "working")
            kill(go, "go-controller-working")
            go = start(go_binary, go_env, go_port)
            recovered = task_state(crashed_id, "failed")
            assert recovered["status"]["message"]["parts"][0]["text"] == (
                "Service restarted before this task finished; please submit it again.")
            release["go-crash"].set()
            # Allow the previously executing Rust future to attempt its stale Finish RPC.
            time.sleep(1)
            assert task_state(crashed_id, "failed") == recovered
            assert len(recovered["artifactChunks"]) == 1 and not recovered["artifacts"]
            passed("go_crash_fails_unfinished_task_and_rejects_late_answer")
            canceled_id = submit("cancel")
            assert received["cancel"].wait(10)
            assert rpc("tasks/cancel", {"id": canceled_id})["result"]["status"]["state"] == "canceling"
            release["cancel"].set()
            canceled = task_state(canceled_id, "canceled")
            assert len(canceled["artifactChunks"]) == 1 and not canceled["artifacts"]
            passed("cancellation_does_not_publish_late_answer")
            expired_id = submit("rust-crash")
            assert received["rust-crash"].wait(10)
            task_state(expired_id, "working")
            kill(rust, "rust-edge-working")
            rust = start(rust_binary, rust_env, rust_port)
            print("Waiting for the actual 45-second executor lease to expire", flush=True)
            expired = task_state(expired_id, "failed", timeout=55)
            release["rust-crash"].set()
            assert expired["status"]["message"]["parts"][0]["text"] == (
                "Task executor lease expired before this task finished; please submit it again.")
            assert len(expired["artifactChunks"]) == 1 and not expired["artifacts"]
            passed("rust_crash_expires_execution_lease_without_rerun")
            with counts_lock:
                assert counts == {"complete": 1, "True": 1, "go-crash": 1, "cancel": 1, "rust-crash": 1}, counts
            assert not (root / ".a2a").exists()
            assert (root / "go-control/a2a.sqlite3").is_file()
            passed("one_provider_request_per_task_and_go_only_task_store")
            return {"schema": "native-a2a-process-proof-v1", "status": "PASS", "checks": checks,
                    "created_at": datetime.now(timezone.utc).isoformat(), "killed_processes": killed,
                    "provider_requests": counts, "binary_sha256": {
                        "go": hashlib.sha256(go_binary.read_bytes()).hexdigest(),
                        "rust": hashlib.sha256(rust_binary.read_bytes()).hexdigest()},
                    "scope": "isolated A2A lifecycle qualification; no storage-provider or production cutover claim"}
        finally:
            for event in release.values():
                event.set()
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
            for log in logs:
                log.close()
            artifact.parent.mkdir(parents=True, exist_ok=True)
            log_text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in root.glob("process-*.log"))
            artifact.with_suffix(".log").write_text(log_text.replace(credential, "[redacted]"), encoding="utf-8")
            provider.shutdown()
            provider.server_close()
            provider_thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go-binary", type=Path, required=True)
    parser.add_argument("--rust-binary", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("artifacts/a2a-control-restart-proof.json"))
    args = parser.parse_args()
    # Invalidate any previous PASS before running; a failing rerun cannot leave stale proof.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"status": "RUNNING"}) + "\n", encoding="utf-8")
    try:
        proof = run(args.go_binary.resolve(strict=True), args.rust_binary.resolve(strict=True), args.out)
    except Exception as error:
        args.out.write_text(json.dumps({"status": "FAIL", "error": str(error)}, indent=2) + "\n", encoding="utf-8")
        raise
    args.out.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
