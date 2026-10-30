"""Exercise the default native launcher with an empty profile, without seeding.

Python is an offline HTTP provider/oracle only. The production launcher must
provision its own private transport and Rust custody and retain its own stores.
Native compilation and execution are restricted to GitHub-hosted verification.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import verify_agent_signing_process as signing

ROOT = Path(__file__).resolve().parents[1]
REQUEST_KEY = "isolated-default-bootstrap-request-credential"


class ProbeFailure(RuntimeError):
    def __init__(self, code: str, *, semantic: bool = False, http_status: int | None = None):
        super().__init__(code)
        self.semantic, self.http_status = semantic, http_status
        self.body_sha256: str | None = None


def request(origin: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = None if body is None else json.dumps(body).encode()
    try:
        with urlopen(Request(origin + path, data=raw, headers={"Content-Type": "application/json"}), timeout=10) as response:
            result = json.loads(response.read(2 * 1024**2 + 1))
    except HTTPError as error:
        # The response may contain request data. Preserve its digest, never its body.
        failure = ProbeFailure("DEFAULT_NATIVE_PUBLIC_REQUEST_REFUSED", semantic=True, http_status=error.code)
        failure.body_sha256 = hashlib.sha256(error.read(2 * 1024**2)).hexdigest()
        raise failure from None
    if not isinstance(result, dict):
        raise ProbeFailure("DEFAULT_NATIVE_PUBLIC_RESPONSE_INVALID", semantic=True)
    return result


def wait_public(origin: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 45
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with urlopen(origin, timeout=1) as response:
                if response.status == 200 and b"<html" in response.read(4096).lower():
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.2)
    raise ProbeFailure("DEFAULT_NATIVE_LAUNCHER_DID_NOT_SERVE_BUILT_FRONTEND")


def wait_run(origin: str, run_id: str, expected: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        run = request(origin, "/api/agent-runs/" + run_id).get("run", {})
        if run.get("status") == expected:
            return run
        if run.get("status") in {"failed", "cancelled", "done"}:
            raise ProbeFailure("DEFAULT_NATIVE_AGENT_TERMINATED_BEFORE_" + expected.upper(), semantic=True)
        time.sleep(0.2)
    raise ProbeFailure("DEFAULT_NATIVE_AGENT_TIMED_OUT_BEFORE_" + expected.upper(), semantic=True)


def process_roles(parent: int, required: set[str] | None = None) -> list[dict[str, Any]]:
    """Observe actual executable names and only credential variable names."""
    pending, result, seen = [parent], [], set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            base = Path("/proc") / str(pid)
            executable = Path(os.readlink(base / "exe")).name
            # Go may fork from any runtime OS thread; /task/<pid>/children alone
            # omits valid children created by another thread in the supervisor.
            children: set[str] = set()
            for task in (base / "task").iterdir():
                try:
                    children.update((task / "children").read_text().split())
                except FileNotFoundError:
                    continue
            keys = {item.split(b"=", 1)[0].decode("ascii", "replace")
                    for item in (base / "environ").read_bytes().split(b"\0") if item}
        except (OSError, ValueError):
            continue
        if executable in {"python", "python3", "node"} or executable.startswith("python3."):
            raise ProbeFailure("LEGACY_RUNTIME_IN_DEFAULT_NATIVE_PROCESS_TREE", semantic=True)
        if executable == "deepseekd" and signing.PRIVATE_ENV & keys:
            raise ProbeFailure("RUST_PRIVATE_CUSTODY_CONFIGURATION_ENTERED_GO", semantic=True)
        result.append({"pid": pid, "executable": executable})
        pending.extend(int(value) for value in children)
    expected = {"deepseekd", "deepseek-worker", "deepseek-gateway"} if required is None else required
    if not expected <= {row["executable"] for row in result}:
        raise ProbeFailure("DEFAULT_NATIVE_PRODUCTION_PROCESS_MISSING", semantic=True)
    return result


def stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            raise ProbeFailure("DEFAULT_NATIVE_LAUNCHER_DID_NOT_STOP")


def require_loopback_ports_available(ports: tuple[int, ...] = (8090, 50052)) -> None:
    """Check the private ports before a probe builds or launches native processes."""
    for port in ports:
        try:
            # create_server reuses POSIX TIME_WAIT addresses and checks listen(),
            # while still refusing a live listener. Never enable shared REUSEPORT.
            with socket.create_server(("127.0.0.1", port), reuse_port=False):
                pass
        except OSError as error:
            raise ProbeFailure(f"NATIVE_LOOPBACK_PORT_UNAVAILABLE:{port}:{error.errno}") from error


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.name != "posix":
        raise ProbeFailure("NATIVE_DEFAULT_BOOTSTRAP_VERIFICATION_IS_CLOUD_ONLY")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if revision != os.environ.get("GITHUB_SHA"):
        raise ProbeFailure("CLOUD_SOURCE_REVISION_DIFFERS")
    report["sourceCommit"] = revision
    # These are the production launcher's fixed private ports. Never share a probe
    # with another runtime and never change a real user's listener configuration.
    require_loopback_ports_available()
    with tempfile.TemporaryDirectory(prefix="native-default-bootstrap-") as temporary:
        scratch = Path(temporary)
        binaries, profile = scratch / "bin", scratch / "profile"
        binaries.mkdir()
        (scratch / "go-tmp").mkdir()
        build_env = signing.cache_environment(ROOT, env=signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        build_env.update(CGO_ENABLED="0", GOTOOLCHAIN="local", GOMAXPROCS="2", GOPROXY="off", GOSUMDB="off", GOWORK="off",
                         GOCACHE=args.go_cache.resolve().as_posix(), GOTMPDIR=(scratch / "go-tmp").as_posix())
        if not subprocess.check_output(["go", "version"], env=build_env, text=True).startswith("go version " + signing.GO_VERSION + " "):
            raise ProbeFailure("NATIVE_GO_TOOLCHAIN_PIN_DIFFERS")
        for name in ("deepseekd", "deepseek-launch"):
            signing.record_command(["go", "build", "-p=2", "-o", str(binaries / name), "./cmd/" + name],
                                   ROOT / "go", build_env, args.out, "build-" + name, timeout=600)
        for name, source in (("deepseek-worker", args.worker), ("deepseek-gateway", args.edge)):
            (binaries / name).symlink_to(source.resolve())
        if profile.exists() or not (ROOT / "static/ui/index.html").is_file():
            raise ProbeFailure("DEFAULT_BOOTSTRAP_FIXTURE_IS_NOT_EMPTY_OR_FRONTEND_MISSING")
        report.update(nativeBuildsPassed=True, profileInitiallyAbsent=True, offlineStoreSeeding=False,
                      manualCustodyProvisioning=False, manualPrivateTransportConfiguration=False)
        exchanges: list[dict[str, Any]] = []

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024**2:
                    self.send_error(400)
                    return
                body = self.rfile.read(length)
                position = len(exchanges)
                exchanges.append({"actionId": self.headers.get("X-DeepSeek-Action-Id"),
                    "executionEpoch": self.headers.get("X-DeepSeek-Execution-Epoch"),
                    "operationId": self.headers.get("X-DeepSeek-Node-Operation-Id"),
                    "requestCredentialUsed": self.headers.get("Authorization") == "Bearer " + REQUEST_KEY,
                    "credentialInProviderBody": REQUEST_KEY.encode() in body,
                    "predecessorSummaryUsed": b"Controlled default coder summary" in body})
                content = (json.dumps({"agents": [{"role": "coder", "task": "write", "dependsOn": []},
                           {"role": "critic", "task": "review", "dependsOn": ["coder"]}]}) if position == 0 else
                           "Controlled default final answer" if position == 3 else
                           "## 摘要\nControlled default coder summary" if position == 1 else "Controlled default critic summary")
                raw = json.dumps({"id": "default-bootstrap-" + str(position), "model": "deepseek-v4-pro",
                    "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        with socket.socket() as public_port:
            public_port.bind(("127.0.0.1", 0))
            port = public_port.getsockname()[1]
        environment = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
        environment.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile),
            DEEPSEEK_INFRA_STATIC_DIR=str(ROOT / "static"),
            DEEPSEEK_API_URL=f"http://127.0.0.1:{provider.server_port}/chat", DEEPSEEK_API_KEY="isolated-server-fallback")
        process: subprocess.Popen[bytes] | None = None
        origin = f"http://127.0.0.1:{port}"

        def launch(label: str) -> subprocess.Popen[bytes]:
            with (args.out / (label + ".log")).open("wb") as log:
                child = subprocess.Popen([str(binaries / "deepseek-launch"), "--server", "--no-prompt",
                    "--auth-disabled", "--port", str(port)], cwd=scratch, env=environment, stdout=log, stderr=subprocess.STDOUT)
            return child

        def start(label: str) -> subprocess.Popen[bytes]:
            child = launch(label)
            try:
                wait_public(origin, child)
                return child
            except BaseException:
                stop(child)
                raise

        try:
            # Hold only control admission, after the real Rust worker has supplied
            # its public binding. The isolated Go fixture cannot create a database
            # or perform a provider request. No production crash hook is added.
            holder = scratch / "control-holder.go"
            holder.write_text('''package main
import ("os"; "path/filepath"; "strconv"; "time")
func main() {
    path := filepath.Join(filepath.Dir(os.Args[0]), "control-held")
    if os.WriteFile(path, []byte(strconv.Itoa(os.Getpid())), 0600) != nil { os.Exit(1) }
    for { time.Sleep(time.Second) }
}
''', encoding="utf-8")
            actual_control = binaries / "deepseekd-original"
            (binaries / "deepseekd").replace(actual_control)
            signing.record_command(["go", "build", "-p=2", "-o", str(binaries / "deepseekd"), str(holder)],
                                   ROOT / "go", build_env, args.out, "build-control-holder", timeout=300)
            process = launch("interrupted-before-control")
            metadata = profile / "go-control/native-profile"
            preparation = metadata / "signer-public-preparation"
            pin_path = metadata / "signer-public-pin"
            birth_path = profile / "rust-worker/custody/native-origin.pb"
            control = profile / "go-control/control.sqlite3"
            intent = profile / "go-control/native-origin-intent.pb"
            publication = profile / "rust-worker/native-origin-publication.pb"
            held = binaries / "control-held"
            deadline = time.monotonic() + 30
            while not (held.is_file() and pin_path.is_file() and preparation.is_file() and birth_path.is_file()):
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise ProbeFailure("EARLY_NATIVE_PUBLIC_PIN_BOUNDARY_NOT_OBSERVED", semantic=True)
                time.sleep(0.025)
            original_pin, original_birth = pin_path.read_bytes(), birth_path.read_bytes()
            if preparation.read_bytes() != original_pin or control.exists() or intent.exists() or publication.exists() or exchanges:
                raise ProbeFailure("EARLY_NATIVE_INTERRUPTION_ALREADY_CREATED_CONTROL", semantic=True)
            held_pid = int(held.read_text())
            interrupted_processes = process_roles(process.pid, {"deepseek-worker", "deepseekd"})
            if not any(row["pid"] == held_pid and row["executable"] == "deepseekd" for row in interrupted_processes):
                raise ProbeFailure("EARLY_NATIVE_CONTROL_HOLDER_IDENTITY_DIFFERS", semantic=True)
            process.kill()  # Actual SIGKILL; guardian EOF must fence every child.
            process.wait(timeout=10)
            process = None
            deadline = time.monotonic() + 5
            while any(Path(f"/proc/{row['pid']}").exists() for row in interrupted_processes):
                if time.monotonic() >= deadline:
                    raise ProbeFailure("EARLY_NATIVE_CONTROL_HOLDER_SURVIVED_SUPERVISION", semantic=True)
                time.sleep(0.025)
            actual_control.replace(binaries / "deepseekd")
            report.update(earlyPinInterruptionObserved=True, earlyControlAdmissionHeldByIsolatedGoFixture=True,
                          actualRustBindingBeforeInterruption=True, earlyGoDatabaseAbsent=True,
                          earlyGoCreationIntentAbsent=True, earlyProviderRequests=0,
                          earlyInterruptionUsedSIGKILL=True, interruptedNativeChildrenFenced=True,
                          originalEarlyBirthSha256=hashlib.sha256(original_birth).hexdigest())
            process = start("first-launch")
            original_publication = publication.read_bytes()
            with closing(sqlite3.connect(control.as_uri() + "?mode=ro", uri=True)) as db:
                native_origin = db.execute("SELECT origin,state,publication FROM control_native_origin WHERE singleton=1").fetchone()
            if pin_path.read_bytes() != original_pin or birth_path.read_bytes() != original_birth or \
                    intent.read_bytes() != original_birth or preparation.exists() or not original_publication or \
                    native_origin != (original_birth, "published", original_publication):
                raise ProbeFailure("EARLY_NATIVE_RECOVERY_REPLACED_OR_LOST_ORIGINAL_CUSTODY", semantic=True)
            report.update(earlyPinRecoveryQualified=True, originalEarlyBirthAndIntentRetained=True,
                          publicPreparationRemovedAfterControlReady=True, powerLossRecoveryQualified=False)
            report["actualProductionProcesses"] = process_roles(process.pid)
            created = request(origin, "/api/agent-runs", {"payload": {"messages": [{"role": "user", "content":
                "Prepare an isolated default bootstrap verification plan"}], "apiKey": REQUEST_KEY, "model": "deepseek-chat"},
                "agentPreset": "auto", "confirmPlan": True})
            run_id = created.get("runId")
            if not isinstance(run_id, str) or not run_id:
                raise ProbeFailure("DEFAULT_NATIVE_CREATION_MISSING_RUN_ID", semantic=True)
            before = wait_run(origin, run_id, "awaiting_plan")
            if len(exchanges) != 1:
                raise ProbeFailure("DEFAULT_NATIVE_TASKS_STARTED_BEFORE_CONFIRMATION", semantic=True)
            control = profile / "go-control/control.sqlite3"
            with sqlite3.connect(control.as_uri() + "?mode=ro", uri=True) as db:
                before_writer = db.execute("SELECT fencing_token,owner_instance_id FROM control_writer WHERE singleton=1").fetchone()
            stop(process)
            process = None
            process = start("second-launch")
            if birth_path.read_bytes() != original_birth or publication.read_bytes() != original_publication:
                raise ProbeFailure("NATIVE_RESTART_REPLACED_EARLY_ORIGIN_OR_PUBLICATION", semantic=True)
            report["originalEarlyPublicationRetained"] = True
            report["restartedProductionProcesses"] = process_roles(process.pid)
            with sqlite3.connect(control.as_uri() + "?mode=ro", uri=True) as db:
                after_writer = db.execute("SELECT fencing_token,owner_instance_id FROM control_writer WHERE singleton=1").fetchone()
            if before_writer is None or after_writer is None or after_writer[0] != before_writer[0] + 1 or after_writer[1] == before_writer[1]:
                raise ProbeFailure("DEFAULT_NATIVE_RESTART_DID_NOT_ADVANCE_WRITER_FENCE", semantic=True)
            if request(origin, "/api/agent-runs/" + run_id).get("run") != before or len(exchanges) != 1:
                raise ProbeFailure("DEFAULT_NATIVE_RESTART_CHANGED_PENDING_RUN", semantic=True)
            if request(origin, "/api/agent-runs/" + run_id + "/plan", {"payload": {}}).get("started") is not True:
                raise ProbeFailure("DEFAULT_NATIVE_PLAN_CONFIRMATION_REFUSED", semantic=True)
            final = wait_run(origin, run_id, "done")
            encoded = json.dumps(final).encode()
            if len(exchanges) != 4 or any(not all(row.get(key) for key in ("actionId", "executionEpoch", "operationId", "requestCredentialUsed"))
                    or row["credentialInProviderBody"] for row in exchanges) or not exchanges[2]["predecessorSummaryUsed"] or \
                    b"Controlled default final answer" not in encoded or REQUEST_KEY.encode() in encoded or final.get("nextIndex") != 9:
                raise ProbeFailure("DEFAULT_NATIVE_PUBLIC_DAG_OR_CUSTODY_PROOF_INCOMPLETE", semantic=True)
            report.update(status="PASS", controlledDefaultBootstrapQualified=True,
                          pendingRunRetainedAcrossLauncherRestart=True, writerFenceAdvanced=True,
                          providerRequestCount=4, finalNextIndex=9, requestCredentialUsedByProvider=True)
        finally:
            try:
                stop(process)
            finally:
                provider.shutdown()
                provider.server_close()
                thread.join(timeout=5)
                (args.out / "provider-exchanges.json").write_text(json.dumps(exchanges, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--edge", type=Path, required=True)
    parser.add_argument("--go-cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"kind": "native-default-bootstrap-process-v1", "status": "FAIL",
        "sourceCommit": os.environ.get("GITHUB_SHA"), "runId": os.environ.get("GITHUB_RUN_ID"),
        "event": os.environ.get("GITHUB_EVENT_NAME"), "controlledDefaultBootstrapQualified": False,
        "fullMigrationQualified": False, "productionProviderQualified": False, "readiness": "NOT_READY"}
    try:
        run(args, report)
    except Exception as error:
        report.update(status="FAIL", controlledDefaultBootstrapQualified=False,
                      failureCode=str(error) if isinstance(error, ProbeFailure) else type(error).__name__,
                      semanticStageReached=isinstance(error, ProbeFailure) and error.semantic)
        if isinstance(error, ProbeFailure) and error.http_status is not None:
            report.update(httpStatus=error.http_status, responseSha256=error.body_sha256)
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "sourceCommit", "failureCode", "semanticStageReached")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
