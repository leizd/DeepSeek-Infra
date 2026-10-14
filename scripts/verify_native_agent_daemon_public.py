#!/usr/bin/env python3
"""Run the actual native daemon and built web assets on an isolated cloud runner."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import verify_agent_signing_process as signing  # noqa: E402
import verify_native_agent_public_driver as public  # noqa: E402

RUNTIME_QUERY = "Apply the runtime revision to this retained write and review plan"
RUNTIME_CREDENTIAL = "runtime-revision-api-fixture"


def runtime_request_binding(control_path: Path, artifact_root: Path, run_id: str) -> dict[str, Any]:
    """Inspect isolated outputs read-only; this never creates runtime state."""
    with sqlite3.connect((control_path / "control.sqlite3").as_uri() + "?mode=ro", uri=True) as database:
        row = database.execute("SELECT payload_json FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise ValueError("Actual daemon runtime metadata is missing")
    metadata = json.loads(row[0])
    reference = metadata["requestRef"]
    with sqlite3.connect((artifact_root / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as database:
        row = database.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (reference["sha256"],)).fetchone()
    if not row or row[0] != reference["length"] or hashlib.sha256(row[1]).hexdigest() != reference["sha256"]:
        raise ValueError("Actual daemon runtime request binding differs")
    return {"revision": metadata.get("runtimeRevision", 0) or 1, "reference": reference, "bytes": row[1]}


def validate_runtime_update_result(result: dict[str, Any], exchanges: list[dict[str, Any]]) -> None:
    validate_daemon_result(result, exchanges)
    flags = ["runtimePayloadUpdatedAfterRestart", "currentRequestRevisionBoundByGo", "originalRuntimeRequestRetained",
             "originalCredentialCiphertextRetained", "updatedContextReachedEveryRemainingPhase"]
    if any(result.get(key) is not True for key in flags) or result.get("runtimeRevision") != 2 or result.get("metadataSchemaVersion") != 2 or \
            result.get("initialRuntimeRevision") != 1 or result.get("encryptedCredentialObjectCount") != 2 or \
            result.get("initialRequestSha256") == result.get("requestSha256") or \
            len(str(result.get("initialRequestSha256", ""))) != 64:
        raise ValueError("Actual daemon runtime update evidence is incomplete")
    if len(exchanges) != 4 or exchanges[0].get("runtimeContextUsed") is not False or \
            any(row.get("runtimeContextUsed") is not True or row.get("requestCredentialUsed") is not True for row in exchanges[1:]) or \
            any(row.get("credentialInProviderBody") is not False for row in exchanges):
        raise ValueError("Runtime changes did not reach the actual retained provider phases safely")


def daemon_environment(source: dict[str, str], seed: dict[str, Any], address: str,
                       target: str, ca: Path, bearer: str, expiry: str, public_key: str) -> dict[str, str]:
    environment = {key: value for key, value in signing.child_environment(source).items()
                   if not key.upper().startswith("DEEPSEEKD_")}
    environment.update(DEEPSEEKD_MODE="authoritative", DEEPSEEKD_LISTEN=address,
        DEEPSEEKD_PRODUCTION_STORE=seed["controlPath"], DEEPSEEKD_OWNER="actual-agent-daemon-probe",
        DEEPSEEKD_INTERNAL_BEARER=seed["internalBearer"], DEEPSEEKD_CONTROL_AUTHORITY="1",
        DEEPSEEKD_PROMOTION_SIGNER_KEY=seed["promotionPublicKey"],
        DEEPSEEKD_FLEET_ID=seed["fleetId"], DEEPSEEKD_ENVIRONMENT=seed["environment"],
        DEEPSEEK_WORKER_TARGET=target, DEEPSEEK_WORKER_TLS_CA_FILE=ca.as_posix(),
        DEEPSEEK_WORKER_TLS_SERVER_NAME="localhost", DEEPSEEK_WORKER_SERVICE_BEARER=bearer,
        DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT=expiry,
        DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=public_key,
        DEEPSEEK_WORKER_AUTHORITY_FLEET_ID=seed["fleetId"],
        DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT=seed["environment"])
    if signing.PRIVATE_ENV & set(environment) or "DEEPSEEK_API_KEY" in environment:
        raise ValueError("Private worker configuration entered deepseekd")
    return environment


def validate_daemon_result(result: dict[str, Any], exchanges: list[dict[str, Any]]) -> None:
    public.validate_driver_result(result, exchanges)
    required = ["actualDaemonCLIQualified", "actualDaemonProcessRestartQualified",
                "builtFrontendAssetsServed", "independentlyValidatedByGoStore",
                "writerChangedAcrossRestart", "noTasksBeforePublicConfirmation"]
    if any(result.get(key) is not True for key in required) or result.get("bootstrapUIIsOfflineFixture") is not False or \
            result.get("browserInteractionQualified") is not False or result.get("offlineFixtureScheduledAgent") is not False:
        raise ValueError("Actual daemon/asset evidence is incomplete or overclaims browser qualification")
    if result.get("daemonProcessStarts") != 2 or result.get("daemonRestartMode") != "graceful-awaiting-plan" or \
            result.get("writerFenceAfterRestart") != result.get("writerFenceBeforeRestart", 0) + 1 or \
            result.get("providerCallsBeforeRestart") != 1 or result.get("providerCallsAfterRestartBeforeConfirmation") != 1:
        raise ValueError("Daemon restart omitted the writer change or dispatched without confirmation")
    if result.get("publicReplayPreserved") is not True or result.get("publicNDJSONEventCount") != 9 or \
            result.get("publicEventsEqualNDJSON") is not True or \
            not re.fullmatch(r"[a-f0-9]{64}", str(result.get("frontendIndexSha256", ""))) or \
            not isinstance(result.get("frontendAssetCount"), int) or result["frontendAssetCount"] < 1:
        raise ValueError("Actual public replay or built frontend asset identity differs")


def origin_address() -> str:
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{port.getsockname()[1]}"


def request_json(origin: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = Request(origin + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={} if payload is None else {"Content-Type": "application/json"})
    with urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError("Actual daemon public request failed")
        value = json.loads(response.read(2 * 1024**2 + 1))
    if not isinstance(value, dict):
        raise ValueError("Actual daemon public response is not an object")
    return value


def wait_health(origin: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Actual deepseekd exited before readiness")
        try:
            value = request_json(origin, "/healthz")
            if value.get("mode") == "authoritative" and value.get("productionMutation") is True:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise TimeoutError("Actual authoritative deepseekd readiness timed out")


def stop_daemon(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
    if process.wait(timeout=30) != 0:
        raise RuntimeError("Actual deepseekd did not exit gracefully")


def start_bound_worker(binary: Path, environment: dict[str, str], address: str,
                       ca: Path, log_path: Path) -> subprocess.Popen[bytes]:
    with log_path.open("wb") as log:
        process = subprocess.Popen([binary.as_posix()], cwd=ROOT,
            env=dict(environment, DEEPSEEK_WORKER_LISTEN=address), stdout=log, stderr=subprocess.STDOUT)
    try:
        context = ssl.create_default_context(cafile=ca.as_posix())
        context.set_alpn_protocols(["h2"])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Actual bound Rust worker exited before readiness")
            try:
                host, port = address.rsplit(":", 1)
                with socket.create_connection((host, int(port)), timeout=1) as raw:
                    with context.wrap_socket(raw, server_hostname="localhost") as tls:
                        if tls.selected_alpn_protocol() != "h2":
                            raise ValueError("Actual worker omitted HTTP/2")
                return process
            except (ConnectionError, TimeoutError):
                time.sleep(0.1)
        raise TimeoutError("Actual bound Rust worker readiness timed out")
    except BaseException:
        signing.stop_child(process)
        raise


def writer_identity(control_path: Path) -> tuple[int, str]:
    with sqlite3.connect((control_path / "control.sqlite3").as_uri() + "?mode=ro", uri=True) as database:
        row = database.execute("SELECT fencing_token,owner_instance_id,lease_until FROM control_writer WHERE singleton=1").fetchone()
    if row is None or row[0] <= 0 or row[2] <= time.time():
        raise ValueError("Actual daemon writer is unavailable")
    return int(row[0]), str(row[1])


def serve_built_frontend(origin: str) -> tuple[str, int]:
    directory = ROOT / "static/ui"
    index = directory / "index.html"
    if not index.is_file():
        raise ValueError("Actual committed frontend has not been built")
    with urlopen(origin + "/", timeout=15) as response:
        received = response.read(2 * 1024**2 + 1)
    if received != index.read_bytes():
        raise ValueError("Native gateway did not serve the actual built frontend")
    references = re.findall(r'(?:src|href)=["\'](/ui/assets/[^"\']+)["\']', received.decode("utf-8"))
    if not references or not any(path.endswith(".js") for path in references):
        raise ValueError("Actual frontend omits its bundled application")
    for reference in set(references):
        path = (directory / urlparse(reference).path.removeprefix("/ui/")).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError("Built frontend asset path is invalid")
        with urlopen(origin + reference, timeout=15) as response:
            data = response.read(8 * 1024**2 + 1)
        if hashlib.sha256(data).digest() != hashlib.sha256(path.read_bytes()).digest():
            raise ValueError("Native frontend asset bytes differ from the exact build")
    return hashlib.sha256(received).hexdigest(), len(set(references))


def validate_final_events(event_page: dict[str, Any], stream: list[dict[str, Any]]) -> None:
    if len(stream) != 9 or event_page.get("events") != stream or stream[-1].get("type") != "done" or \
            "request-only-credential" in json.dumps(stream):
        raise ValueError("Actual daemon NDJSON and durable terminal event page differ")


def run(args: argparse.Namespace) -> dict[str, Any]:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Native daemon compilation is cloud-only on this host")
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if revision != os.environ.get("GITHUB_SHA"):
        raise ValueError("Daemon cloud source revision differs")
    exchanges: list[dict[str, Any]] = []
    runtime_update = bool(getattr(args, "runtime_update", False))
    browser_mode = bool(getattr(args, "browser", False))
    if browser_mode and runtime_update:
        raise ValueError("Browser creation and restarted runtime revision are separate acceptance cases")
    plan = {"agents": [{"role": "coder", "task": "write", "dependsOn": []},
                        {"role": "critic", "task": "review", "dependsOn": ["coder"]}]}
    with tempfile.TemporaryDirectory(prefix="native-daemon-agent-") as temporary:
        scratch = Path(temporary)
        fixture_environment = signing.cache_environment(ROOT, env=signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        (scratch / "go-tmp").mkdir()
        fixture_environment.update(GOCACHE=args.go_cache.resolve().as_posix(), GOTMPDIR=(scratch / "go-tmp").as_posix(),
            GOTOOLCHAIN="local", GOMAXPROCS="2", CGO_ENABLED="0", GOPROXY="off", GOSUMDB="off", GOWORK="off")
        version = subprocess.check_output(["go", "version"], env=fixture_environment, text=True).strip()
        if not version.startswith(f"go version {signing.GO_VERSION} "):
            raise ValueError("Native daemon Go toolchain pin differs")
        overlay = scratch / "overlay.json"
        overlay.write_text(json.dumps({"Replace": {
            (ROOT / "go/internal/api/zz_native_agent_signing_process_test.go").as_posix(): (ROOT / "tests/native/agent_signing_process.go.txt").as_posix(),
            (ROOT / "go/internal/api/zz_native_agent_daemon_process_test.go").as_posix(): (ROOT / "tests/native/agent_daemon_public_process.go.txt").as_posix()}}), encoding="utf-8")
        fixture, daemon_binary = scratch / "offline-daemon-fixture", scratch / "deepseekd"
        signing.record_command(["go", "test", "-c", "-p=2", "-overlay", overlay.as_posix(), "-o", fixture.as_posix(), "./internal/api"],
            ROOT / "go", fixture_environment, output, "go-daemon-fixture-build", timeout=600)
        signing.record_command(["go", "build", "-p=2", "-o", daemon_binary.as_posix(), "./cmd/deepseekd"],
            ROOT / "go", fixture_environment, output, "go-daemon-build", timeout=600)
        custody, case = scratch / "custody", scratch / "case"
        custody.mkdir(mode=0o700)
        case.mkdir()
        password, bundle = custody / "passphrase", custody / "bundle.json"
        password.write_bytes(secrets.token_urlsafe(48).encode())
        password.chmod(0o600)
        private_environment = signing.child_environment(dict(os.environ))
        private_environment.update(DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE=bundle.as_posix(),
            DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE=password.as_posix(),
            DEEPSEEK_WORKER_AUTHORITY_FLEET_ID="fleet-a", DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT="production")
        provisioned = subprocess.run([args.provisioner.resolve().as_posix()], cwd=ROOT, env=private_environment,
                                    capture_output=True, timeout=60, check=True)
        binding = signing.validate_public_binding(json.loads(provisioned.stdout))
        ca, certificate, key = signing.create_tls(custody)
        state, artifacts = case / "rust-state", case / "rust-artifacts"
        state.mkdir()
        artifacts.mkdir(mode=0o700)
        fixture_environment.update(DEEPSEEK_AGENT_SIGNING_PROBE_DIR=case.as_posix(),
            DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=binding["signerPublicKey"], DEEPSEEK_TEST_HEAD_SHA=revision)
        fixture_environment["DEEPSEEK_TEST_AGENT_DAEMON_STAGE"] = "seed"
        fixture_command = [fixture.as_posix(), "-test.run=^TestNativeAgentDaemonPublicProcess$", "-test.v", "-test.timeout=2m"]
        signing.record_command(fixture_command, ROOT / "go/internal/api", fixture_environment, output, "daemon-empty-ownership", timeout=150)
        seed = json.loads((case / "daemon-seed.json").read_text(encoding="utf-8"))
        if seed.get("emptyApplicationRecords") is not True:
            raise ValueError("Offline ownership fixture seeded an Agent")

        class ProviderFixture(BaseHTTPRequestHandler):
            def log_message(self, *values: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024**2:
                    self.send_error(400)
                    return
                body = json.loads(self.rfile.read(length))
                prompt = json.dumps(body, ensure_ascii=False)
                kind = "title" if browser_mode and not self.headers.get("X-DeepSeek-Action-Id") else "agent"
                exchanges.append({"actionId": self.headers.get("X-DeepSeek-Action-Id"),
                    "kind": kind,
                    "executionEpoch": self.headers.get("X-DeepSeek-Execution-Epoch"),
                    "operationId": self.headers.get("X-DeepSeek-Node-Operation-Id"), "stream": body.get("stream"),
                    "requestCredentialUsed": self.headers.get("Authorization") == "Bearer " +
                        (RUNTIME_CREDENTIAL if runtime_update and exchanges else "request-only-credential"),
                    "runtimeContextUsed": RUNTIME_QUERY in prompt,
                    "credentialInProviderBody": any(value in prompt for value in
                        ["request-only-credential", "request-only-search-credential", RUNTIME_CREDENTIAL]),
                    "selectedTaskInPrompt": "write" in prompt, "dependentTaskInPrompt": "review" in prompt,
                    "predecessorSummaryInPrompt": "Controlled public task summary" in prompt,
                    "privateReasoningInPrompt": "private-task-reasoning" in prompt,
                    "synthesisCoderInPrompt": "write" in prompt, "synthesisCriticInPrompt": "review" in prompt})
                position = len(exchanges)
                content = json.dumps(plan) if position == 1 else ("Controlled final answer" if position == 4 else "## 摘要\nControlled public task summary")
                if kind == "title":
                    content = "Native browser conversation"
                reply = json.dumps({"id": f"daemon-agent-{position}", "model": "deepseek-v4-pro",
                    "choices": [{"message": {"role": "assistant", "content": content,
                    "reasoning_content": "private-task-reasoning"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderFixture)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        daemon = worker = edge = None
        try:
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            bearer, target_address, control_address = secrets.token_urlsafe(48), origin_address(), origin_address()
            target = target_address
            control_origin = "http://" + control_address
            daemon_env = daemon_environment(dict(os.environ), seed, control_address, target, ca, bearer, expiry, binding["signerPublicKey"])

            def start_daemon(label: str) -> subprocess.Popen[bytes]:
                environment = dict(daemon_env, DEEPSEEKD_OWNER="actual-agent-daemon-" + secrets.token_hex(12))
                with (output / (label + ".log")).open("wb") as log:
                    process = subprocess.Popen([daemon_binary.as_posix()], cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
                try:
                    wait_health(control_origin, process)
                    return process
                except BaseException:
                    signing.stop_child(process)
                    raise

            def start_worker(fence: int, label: str) -> subprocess.Popen[bytes]:
                environment = dict(private_environment, DEEPSEEK_WORKER_TLS_CERT_FILE=certificate.as_posix(),
                    DEEPSEEK_WORKER_TLS_KEY_FILE=key.as_posix(), DEEPSEEK_WORKER_SERVICE_BEARER=bearer,
                    DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT=expiry, DEEPSEEK_WORKER_SERVICE_NAME="go-control-plane",
                    DEEPSEEK_WORKER_SERVICE_ROLE="controller", DEEPSEEK_WORKER_STATE_ROOT=state.as_posix(),
                    DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=binding["signerPublicKey"],
                    DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN=str(fence), GO_CONTROL_ADDR=control_origin,
                    DEEPSEEK_INTERNAL_BEARER=seed["internalBearer"], DEEPSEEK_AGENT_ARTIFACT_DIR=artifacts.as_posix(),
                    DEEPSEEK_INFRA_ROOT=case.as_posix(), DEEPSEEK_AGENT_DATA_MODE="python_disabled",
                    DEEPSEEK_API_URL=f"http://127.0.0.1:{provider.server_port}/chat", DEEPSEEK_API_KEY="isolated-fixture-credential")
                if label == "worker-after-restart":
                    environment.pop("DEEPSEEK_API_KEY", None)
                return start_bound_worker(args.worker.resolve(), environment, target_address, ca, output / (label + ".log"))

            daemon = start_daemon("daemon-before-restart")
            before_fence, before_owner = writer_identity(Path(seed["controlPath"]))
            worker = start_worker(before_fence, "worker-before-restart")
            address = origin_address()
            origin = "http://" + address
            edge_env = public.public_edge_environment(private_environment, address, ROOT / "static/ui", artifacts,
                                                      {"origin": control_origin, "internalBearer": seed["internalBearer"]},
                public.credential_worker_transport(target, ca, bearer))
            if browser_mode:
                edge_env["DEEPSEEK_API_URL"] = f"http://127.0.0.1:{provider.server_port}/chat"
            with (output / "daemon-public-edge.log").open("wb") as log:
                edge = subprocess.Popen([args.edge.resolve().as_posix()], cwd=ROOT, env=edge_env, stdout=log, stderr=subprocess.STDOUT)
            public.wait_edge(origin, edge)
            index_sha, asset_count = serve_built_frontend(origin)
            if browser_mode:
                import verify_native_agent_browser as browser

                observed = browser.run_browser_agent(origin, output)
                run_id = observed["createdRunId"]
                final = request_json(origin, "/api/agent-runs/" + run_id)["run"]
                if final.get("status") != "done" or "Controlled final answer" not in str(final.get("finalAnswer", "")):
                    raise ValueError("Browser final answer differs from the native run projection")
                stop_daemon(daemon)
                signing.write_ready(case / "daemon-inspect-input.json", {"runId": run_id, "browser": True})
                fixture_environment["DEEPSEEK_TEST_AGENT_DAEMON_STAGE"] = "inspect"
                signing.record_command(fixture_command, ROOT / "go/internal/api", fixture_environment, output,
                                       "daemon-browser-independent-inspection", timeout=150)
                result = json.loads((case / "daemon-inspected.json").read_text(encoding="utf-8"))
                result.update(browser=observed, actualDaemonCLIQualified=True, builtFrontendAssetsServed=True,
                    frontendIndexSha256=index_sha, frontendAssetCount=asset_count,
                    inputBodySeededByOfflineOracle=False, pythonMutatingPublicRequests=0,
                    productionRuntimeProcesses=["Go", "Rust"], privateCustodyVariablesExportedToGo=False,
                    productionProviderQualified=False, fullMigrationQualified=False,
                    cloudOnlyNativeBuild=True, providerExchanges=exchanges, readiness="NOT_READY")
                result.update(browser.inspect_browser_custody(artifacts, state))
                browser.validate_browser_result(result, exchanges)
                result["controlledNativeBrowserQualified"] = True
                (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return result
            created = request_json(origin, "/api/agent-runs", {"payload": {"messages": [{"role": "user", "content": "Prepare an isolated verification plan"}],
                "model": "deepseek-chat", "apiKey": "request-only-credential", "tavilyApiKey": "request-only-search-credential"}, "agentPreset": "auto", "confirmPlan": True})
            run_id = created.get("runId")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("Actual daemon public creation omitted run identity")
            deadline = time.monotonic() + 120
            while True:
                current = request_json(origin, "/api/agent-runs/" + run_id)["run"]
                if current.get("status") == "awaiting_plan":
                    break
                if current.get("status") in {"failed", "cancelled", "done"} or time.monotonic() > deadline:
                    raise ValueError("Actual daemon did not pause for confirmation")
                time.sleep(0.2)
            time.sleep(1.2)
            if len(exchanges) != 1:
                raise ValueError("Actual daemon dispatched tasks without confirmation")
            initial_runtime = runtime_request_binding(Path(seed["controlPath"]), artifacts, run_id)
            initial_ciphertexts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in (artifacts / "agent-provider-credentials-v1").glob("*.json")}
            stop_daemon(daemon)
            signing.stop_child(worker)
            daemon = start_daemon("daemon-after-restart")
            after_fence, after_owner = writer_identity(Path(seed["controlPath"]))
            if after_fence != before_fence + 1 or after_owner == before_owner:
                raise ValueError("Actual process restart did not change the writer fence and owner")
            worker = start_worker(after_fence, "worker-after-restart")
            replay = request_json(origin, "/api/agent-runs/" + run_id)["run"]
            if replay != current:
                raise ValueError("Public plan/event projection changed across daemon restart")
            time.sleep(1.2)
            if len(exchanges) != 1:
                raise ValueError("Restart dispatched tasks without public confirmation")
            runtime_payload = {"messages": [{"role": "user", "content": RUNTIME_QUERY}], "apiKey": RUNTIME_CREDENTIAL} if runtime_update else {}
            confirmation = request_json(origin, "/api/agent-runs/" + run_id + "/plan", {"payload": runtime_payload})
            if confirmation.get("started") is not True:
                raise ValueError("Actual daemon public plan confirmation failed")
            deadline = time.monotonic() + 120
            while True:
                final = request_json(origin, "/api/agent-runs/" + run_id)["run"]
                if final.get("status") == "done":
                    break
                if final.get("status") in {"failed", "cancelled"} or time.monotonic() > deadline:
                    raise ValueError("Actual restarted daemon did not finish dependent tasks and synthesis")
                time.sleep(0.2)
            final_raw = json.dumps(final, ensure_ascii=False).encode()
            if b"Controlled final answer" not in final_raw or b"request-only-credential" in final_raw:
                raise ValueError("Actual daemon final answer missing or credential leaked")
            with urlopen(origin + "/api/agent-runs/" + run_id + "/stream", timeout=15) as response:
                if not response.headers.get("Content-Type", "").startswith("application/x-ndjson"):
                    raise ValueError("Actual daemon terminal stream is not NDJSON")
                stream = [json.loads(line) for line in response.read(2 * 1024**2 + 1).splitlines() if line.strip()]
            event_page = request_json(origin, "/api/agent-runs/" + run_id + "/events")
            validate_final_events(event_page, stream)
            stop_daemon(daemon)
            signing.write_ready(case / "daemon-inspect-input.json", {"runId": run_id})
            fixture_environment["DEEPSEEK_TEST_AGENT_DAEMON_STAGE"] = "inspect"
            signing.record_command(fixture_command, ROOT / "go/internal/api", fixture_environment, output, "daemon-independent-inspection", timeout=150)
            result = json.loads((case / "daemon-inspected.json").read_text(encoding="utf-8"))
            with sqlite3.connect((artifacts / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as database:
                row = database.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (result["requestSha256"],)).fetchone()
                if not row or row[0] != result["requestLength"] or hashlib.sha256(row[1]).hexdigest() != result["requestSha256"] or \
                        any(value in row[1] for value in [b"request-only-credential", b"request-only-search-credential", RUNTIME_CREDENTIAL.encode()]):
                    raise ValueError("Actual daemon Rust request custody differs or leaks a credential")
            with sqlite3.connect((state / "rust-worker/authority.sqlite3").as_uri() + "?mode=ro", uri=True) as database:
                rows = database.execute("SELECT state,output,output_sha256 FROM agent_node_effects").fetchall()
                if len(rows) != 4 or any(row[0] != "CONFIRMED" or hashlib.sha256(row[1]).hexdigest() != row[2] for row in rows):
                    raise ValueError("Actual daemon Rust effects were not independently retained")
            result.update(publicEventCount=len(final.get("events", [])), publicNDJSONEventCount=len(stream),
                finalAnswerVisible=True, publicCreationAndDAGDriverQualified=True, publicPlanConfirmationQualified=True,
                noTasksBeforePublicConfirmation=True, actualDaemonCLIQualified=True, actualDaemonProcessRestartQualified=True,
                daemonProcessStarts=2, daemonRestartMode="graceful-awaiting-plan", writerChangedAcrossRestart=True,
                writerFenceBeforeRestart=before_fence, writerFenceAfterRestart=after_fence,
                providerCallsBeforeRestart=1, providerCallsAfterRestartBeforeConfirmation=1, publicReplayPreserved=True,
                publicEventsEqualNDJSON=True,
                builtFrontendAssetsServed=True, frontendIndexSha256=index_sha, frontendAssetCount=asset_count,
                bootstrapUIIsOfflineFixture=False, browserInteractionQualified=False, offlineFixtureScheduledAgent=False,
                inputBodySeededByOfflineOracle=False, productionRuntimeProcesses=["Go", "Rust"],
                productionProviderQualified=False, fullMigrationQualified=False, actualHTTPRequests=len(exchanges),
                providerExchanges=exchanges, cloudOnlyNativeBuild=True, privateCustodyVariablesExportedToGo=False, readiness="NOT_READY")
            if not exchanges or not all(value.get("requestCredentialUsed") is True for value in exchanges):
                raise ValueError("Actual provider did not use the request credential across restart")
            sealed = list((artifacts / "agent-provider-credentials-v1").glob("*.json"))
            if len(sealed) != (2 if runtime_update else 1) or any(secret in p.read_bytes() for p in sealed for secret in
                    (b"request-only-credential", b"request-only-search-credential", RUNTIME_CREDENTIAL.encode())):
                raise ValueError("Actual request credential ciphertext custody differs")
            result.update(requestCredentialOverrideQualified=True, credentialCustodyReopenedAcrossWorkerRestart=True,
                encryptedCredentialObjectCount=len(sealed), requestSearchCredentialEncrypted=True,
                workerRestartWithoutServerProviderCredential=True)
            validate_daemon_result(result, exchanges)
            if runtime_update:
                current_runtime = runtime_request_binding(Path(seed["controlPath"]), artifacts, run_id)
                with sqlite3.connect((artifacts / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as database:
                    retained = database.execute("SELECT bytes FROM artifacts WHERE sha256=?", (initial_runtime["reference"]["sha256"],)).fetchone()
                original_retained = bool(retained and retained[0] == initial_runtime["bytes"])
                ciphertext_retained = len(initial_ciphertexts) == 1 and all(
                    hashlib.sha256((artifacts / "agent-provider-credentials-v1" / name).read_bytes()).hexdigest() == digest
                    for name, digest in initial_ciphertexts.items())
                current_body = json.loads(current_runtime["bytes"])
                if current_body["payload"]["messages"] != runtime_payload["messages"] or current_runtime["revision"] != 2 or \
                        result["requestSha256"] != current_runtime["reference"]["sha256"]:
                    raise ValueError("Updated runtime request differs from the actual Go metadata binding")
                result.update(runtimePayloadUpdatedAfterRestart=True, currentRequestRevisionBoundByGo=True,
                    initialRuntimeRevision=initial_runtime["revision"], initialRequestSha256=initial_runtime["reference"]["sha256"],
                    originalRuntimeRequestRetained=original_retained, originalCredentialCiphertextRetained=ciphertext_retained,
                    updatedContextReachedEveryRemainingPhase=all(row["runtimeContextUsed"] for row in exchanges[1:]))
                validate_runtime_update_result(result, exchanges)
            (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            for process in [daemon, edge, worker]:
                if process is not None:
                    signing.stop_child(process)
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["worker", "edge", "provisioner", "go-cache", "out"]:
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--runtime-update", action="store_true")
    parser.add_argument("--browser", action="store_true")
    result = run(parser.parse_args())
    print(json.dumps({key: result[key] for key in ["status", "sourceCommit", "actualDaemonCLIQualified", "actualDaemonProcessRestartQualified", "builtFrontendAssetsServed", "controlledNativeBrowserQualified", "readiness"] if key in result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
