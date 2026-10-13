#!/usr/bin/env python3
"""Actual native public API/DAG process probe, built only on the cloud runner."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import verify_agent_signing_process as signing  # noqa: E402


def validate_driver_result(result: dict[str, Any], exchanges: list[dict[str, Any]]) -> None:
    if result.get("status") != "PASS" or result.get("runStatus") != "done" or result.get("nextIndex") != 9 or \
            result.get("durablePhases") != 3 or result.get("publicEventCount") != 9 or \
            result.get("resourcesReleased") is not True or result.get("finalAnswerVisible") is not True or \
            result.get("inputBodySeededByOfflineOracle") is not False or \
            result.get("productionRuntimeProcesses") != ["Go", "Rust"] or \
            result.get("publicCreationAndDAGDriverQualified") is not True or \
            result.get("publicPlanConfirmationQualified") is not True or result.get("noTasksBeforePublicConfirmation") is not True or \
            result.get("productionProviderQualified") is not False or result.get("fullMigrationQualified") is not False:
        raise ValueError("Native public driver evidence is incomplete or overclaims qualification")
    if len(exchanges) != 4:
        raise ValueError("Native public driver must make exactly four controlled provider calls")
    actions = [row.get("actionId") for row in exchanges]
    operations = [row.get("operationId") for row in exchanges]
    if any(not isinstance(value, str) or not value for value in actions + operations) or len(set(operations)) != 4 or \
            len(set(actions)) != 3 or actions[1] != actions[2] or \
            any(row.get("executionEpoch") != "1" or row.get("stream") is not False for row in exchanges):
        raise ValueError("Provider calls lack the actual distinct-node/shared-task-phase fences")
    if exchanges[1].get("selectedTaskInPrompt") is not True or exchanges[2].get("dependentTaskInPrompt") is not True or \
            exchanges[2].get("predecessorSummaryInPrompt") is not True or exchanges[2].get("privateReasoningInPrompt") is not False or \
            exchanges[3].get("synthesisCoderInPrompt") is not True or exchanges[3].get("synthesisCriticInPrompt") is not True:
        raise ValueError("Actual native DAG dependency/final request assembly is unqualified")


def wait_edge(origin: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Native public edge exited before listening")
        try:
            with urlopen(origin + "/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise TimeoutError("Native public edge did not become reachable")


def run(args: argparse.Namespace) -> dict[str, Any]:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Native public compilation and containers are cloud-only on this host")
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if source_commit != os.environ.get("GITHUB_SHA"):
        raise ValueError("Cloud checkout is not the workflow's exact source revision")
    with tempfile.TemporaryDirectory(prefix="native-public-agent-") as temporary:
        scratch = Path(temporary)
        environment = signing.cache_environment(ROOT, env=signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        go_temporary = scratch / "go-tmp"
        go_temporary.mkdir()
        environment.update(GOCACHE=args.go_cache.resolve().as_posix(), GOTMPDIR=go_temporary.as_posix(),
            GOTOOLCHAIN="local", GOMAXPROCS="2", CGO_ENABLED="0", GOPROXY="off", GOSUMDB="off", GOWORK="off")
        version = subprocess.check_output(["go", "version"], env=environment, text=True).strip()
        if not version.startswith(f"go version {signing.GO_VERSION} "):
            raise ValueError("Native Go toolchain pin differs")
        fixtures = [ROOT / "tests/native/agent_signing_process.go.txt", ROOT / "tests/native/agent_public_driver_process.go.txt"]
        overlay = scratch / "overlay.json"
        overlay.write_text(json.dumps({"Replace": {
            (ROOT / "go/internal/api/zz_native_agent_signing_process_test.go").as_posix(): fixtures[0].as_posix(),
            (ROOT / "go/internal/api/zz_native_agent_public_driver_process_test.go").as_posix(): fixtures[1].as_posix()}}), encoding="utf-8")
        binary = scratch / "native-public-agent-api-tests"
        signing.record_command(["go", "test", "-c", "-p=2", "-overlay", overlay.as_posix(), "-o", binary.as_posix(), "./internal/api"],
            ROOT / "go", environment, output, "go-public-driver-build", timeout=600)
        custody = scratch / "custody"
        custody.mkdir(mode=0o700)
        password, bundle = custody / "passphrase", custody / "bundle.json"
        password.write_bytes(secrets.token_urlsafe(48).encode())
        password.chmod(0o600)
        private_environment = signing.child_environment(dict(os.environ))
        private_environment.update(DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE=bundle.as_posix(),
            DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE=password.as_posix(),
            DEEPSEEK_WORKER_AUTHORITY_FLEET_ID="fleet-a", DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT="production")
        provisioned = subprocess.run([args.provisioner.resolve().as_posix()], cwd=ROOT, env=private_environment,
            capture_output=True, timeout=60, check=False)
        if provisioned.returncode:
            raise RuntimeError("Native public custody provisioning failed")
        binding = signing.validate_public_binding(json.loads(provisioned.stdout))
        ca, certificate, key = signing.create_tls(custody)
        case = scratch / "case"
        case.mkdir()
        artifacts = case / "rust-artifacts"
        artifacts.mkdir(mode=0o700)
        state = case / "rust-state"
        state.mkdir()
        exchanges: list[dict[str, Any]] = []
        plan = {"summary": "Controlled provider plan", "tasks": [{"role": "coder", "task": "write", "dependsOn": []},
            {"role": "critic", "task": "review", "dependsOn": ["coder"]}]}

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
                exchanges.append({"actionId": self.headers.get("X-DeepSeek-Action-Id"),
                    "executionEpoch": self.headers.get("X-DeepSeek-Execution-Epoch"),
                    "operationId": self.headers.get("X-DeepSeek-Node-Operation-Id"), "model": body.get("model"),
                    "stream": body.get("stream"), "selectedTaskInPrompt": "Agent 子任务：write" in prompt,
                    "dependentTaskInPrompt": "Agent 子任务：review" in prompt,
                    "predecessorSummaryInPrompt": "Controlled public task summary" in prompt,
                    "privateReasoningInPrompt": "private-task-reasoning" in prompt,
                    "synthesisCoderInPrompt": "任务：write" in prompt, "synthesisCriticInPrompt": "任务：review" in prompt})
                position = len(exchanges)
                content = json.dumps(plan) if position == 1 else ("Controlled final answer" if position == 4 else "## 摘要\nControlled public task summary")
                reply = json.dumps({"id": f"public-agent-{position}", "model": "deepseek-v4-pro",
                    "choices": [{"message": {"role": "assistant", "content": content, "reasoning_content": "private-task-reasoning"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        provider = ThreadingHTTPServer(("127.0.0.1", 0), ProviderFixture)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        producer = worker = edge = None
        try:
            bearer = secrets.token_urlsafe(48)
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            environment.update(DEEPSEEK_AGENT_PUBLIC_DRIVER_PROBE="1", DEEPSEEK_AGENT_SIGNING_PROBE_DIR=case.as_posix(),
                DEEPSEEK_AGENT_SIGNING_WORKER_BEARER=bearer, DEEPSEEK_TEST_HEAD_SHA=source_commit,
                DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=binding["signerPublicKey"])
            if signing.PRIVATE_ENV & set(environment) or "DEEPSEEK_API_KEY" in environment:
                raise ValueError("Private custody configuration entered Go")
            with (output / "public-driver-go.log").open("wb") as log:
                producer = subprocess.Popen([binary.as_posix(), "-test.run=^TestNativeAgentPublicDriverProcess$", "-test.v", "-test.timeout=4m"],
                    cwd=ROOT / "go/internal/api", env=environment, stdout=log, stderr=subprocess.STDOUT)
            ready = signing.wait_json(case / "go-ready.json", producer)
            worker_environment = dict(private_environment, DEEPSEEK_WORKER_TLS_CERT_FILE=certificate.as_posix(),
                DEEPSEEK_WORKER_TLS_KEY_FILE=key.as_posix(), DEEPSEEK_WORKER_SERVICE_BEARER=bearer,
                DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT=expiry, DEEPSEEK_WORKER_SERVICE_NAME="go-control-plane",
                DEEPSEEK_WORKER_SERVICE_ROLE="controller", DEEPSEEK_WORKER_STATE_ROOT=state.as_posix(),
                DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY=binding["signerPublicKey"],
                DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN=str(ready["fencingToken"]), GO_CONTROL_ADDR=ready["origin"],
                DEEPSEEK_INTERNAL_BEARER=ready["internalBearer"], DEEPSEEK_AGENT_ARTIFACT_DIR=artifacts.as_posix(),
                DEEPSEEK_INFRA_ROOT=case.as_posix(), DEEPSEEK_AGENT_DATA_MODE="python_disabled",
                DEEPSEEK_API_URL=f"http://127.0.0.1:{provider.server_port}/chat", DEEPSEEK_API_KEY="isolated-fixture-credential")
            worker, target = signing.start_worker(args.worker.resolve(), worker_environment, ca, output / "public-driver-rust.log")
            signing.write_ready(case / "worker-ready.json", {"Target": target, "TrustRootFile": ca.as_posix(),
                "PublicKey": binding["signerPublicKey"], "ExpiresAt": expiry})
            static = case / "static/ui"
            static.mkdir(parents=True)
            (static / "index.html").write_text("<!doctype html><title>Isolated native API fixture</title>", encoding="utf-8")
            with socket.socket() as port:
                port.bind(("127.0.0.1", 0))
                address = f"127.0.0.1:{port.getsockname()[1]}"
            edge_environment = signing.child_environment(worker_environment)
            edge_environment.update(GATEWAY_BIND_ADDR=address, DEEPSEEK_RUNTIME_MODE="python_disabled",
                DEEPSEEK_INFRA_STATIC_DIR=static.parent.as_posix(), AUTH_DISABLED="1")
            with (output / "public-driver-edge.log").open("wb") as log:
                edge = subprocess.Popen([args.edge.resolve().as_posix()], cwd=ROOT, env=edge_environment, stdout=log, stderr=subprocess.STDOUT)
            wait_edge("http://" + address, edge)
            if (artifacts / "objects-v1.sqlite").exists():
                with sqlite3.connect((artifacts / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as db:
                    if db.execute("SELECT count(*) FROM artifacts").fetchone()[0] != 0:
                        raise ValueError("Input custody was seeded before actual public creation")
            signing.write_ready(case / "public-ready.json", {"origin": "http://" + address})
            result = signing.wait_json(case / "result.json", producer, timeout=180)
            if producer.wait(timeout=15) != 0:
                raise RuntimeError("Actual native public driver process failed")
            validate_driver_result(result, exchanges)
            if result.get("sourceCommit") != source_commit:
                raise ValueError("Go process source revision differs")
            with sqlite3.connect((artifacts / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as db:
                row = db.execute("SELECT length,bytes FROM artifacts WHERE sha256=?", (result["requestSha256"],)).fetchone()
                if not row or row[0] != result["requestLength"] or hashlib.sha256(row[1]).hexdigest() != result["requestSha256"]:
                    raise ValueError("Actual Rust request custody digest differs")
                request = json.loads(row[1])
                if request["payload"].get("apiKey") is not None or "request-only-credential" in row[1].decode():
                    raise ValueError("Actual public request persisted a credential")
            with sqlite3.connect((state / "rust-worker/authority.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
                rows = db.execute("SELECT operation_id,state,output,output_sha256 FROM agent_node_effects").fetchall()
                if len(rows) != 4 or any(row[1] != "CONFIRMED" or hashlib.sha256(row[2]).hexdigest() != row[3] for row in rows):
                    raise ValueError("Rust actual four-node custody is missing or corrupt")
            result.update(actualHTTPRequests=len(exchanges), providerExchanges=exchanges,
                requestBodyStoredByRust=True, bootstrapUIIsOfflineFixture=True, cloudOnlyNativeBuild=True,
                privateCustodyVariablesExportedToGo=False, readiness="NOT_READY")
            (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            for process in [producer, edge, worker]:
                if process is not None:
                    signing.stop_child(process)
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["worker", "edge", "provisioner", "go-cache", "out"]:
        parser.add_argument("--" + name, type=Path, required=True)
    result = run(parser.parse_args())
    print(json.dumps({key: result[key] for key in ["status", "sourceCommit", "actualHTTPRequests", "runStatus", "nextIndex", "readiness"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
