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


def public_edge_environment(source: dict[str, str], address: str, static: Path, artifacts: Path,
                            ready: dict[str, Any]) -> dict[str, str]:
    """Explicit gateway custody/control configuration after ambient identities are removed."""
    origin, bearer = ready.get("origin"), ready.get("internalBearer")
    if not isinstance(origin, str) or not origin.startswith("http://127.0.0.1:") or \
            not isinstance(bearer, str) or len(bearer.strip()) < 32:
        raise ValueError("Actual Go control readiness omitted the private loopback binding")
    environment = signing.child_environment(source)
    environment.update(GATEWAY_BIND_ADDR=address, DEEPSEEK_RUNTIME_MODE="python_disabled",
        DEEPSEEK_INFRA_STATIC_DIR=static.parent.as_posix(), AUTH_DISABLED="1",
        GO_CONTROL_ADDR=origin, DEEPSEEK_INTERNAL_BEARER=bearer,
        DEEPSEEK_AGENT_ARTIFACT_DIR=artifacts.as_posix())
    return environment


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


def preset_cases() -> list[dict[str, Any]]:
    fixture = json.loads((ROOT / "rust/crates/deepseek-policy/tests/fixtures/agent_preset_plan.json").read_text(encoding="utf-8"))
    rows = fixture["cases"]
    choices = [("full", "inspect"), ("code", "inspect"), ("research", "inspect"), ("reason", "inspect"), ("critic", "inspect"),
               ("auto", "BUG in class C"), ("auto", "最新资料"), ("auto", "复杂权衡"), ("auto", "字" * 501),
               ("auto", "代码和今天新闻"), ("leader", "inspect")]
    result = []
    for index, (preset, query) in enumerate(choices):
        row = next(value for value in rows if value["preset"] == preset and value["payload"]["messages"][0]["content"] == query)
        upstream = row["expected"] is None
        plan = [{"id":"coder","task":"write"},{"id":"critic","task":"review","depends_on":["coder"]}] if upstream else row["expected"]["agents"]
        result.append({"name":f"{index:02d}-{preset}", "preset":preset, "query":query, "plan":plan,
                       "upstreamPlanner":upstream, "confirmation":preset == "auto", "explicitConfirmation":False})
    for suffix, query, explicit in [("long", "字" * 1801, False), ("explicit", "inspect", True)]:
        result.append({"name":"full-" + suffix,"preset":"full","query":query,"plan":result[0]["plan"],
                       "upstreamPlanner":False,"confirmation":True,"explicitConfirmation":explicit})
    return result


def validate_preset_result(result: dict[str, Any], exchanges: list[dict[str, Any]], case: dict[str, Any]) -> None:
    count, confirm, planner = len(case["plan"]), case["confirmation"], case["upstreamPlanner"]
    event_count, requests = 6 + count + int(confirm), count + 1 + int(planner)
    expected_source = "upstream-provider" if planner else "retained-preset-policy"
    actual_plan = [{"id":node.get("id"),"task":node.get("task"),"depends_on":node.get("depends_on", [])} for node in result.get("actualPlan", [])]
    expected_plan = [{"id":node["id"],"task":node["task"],"depends_on":node.get("depends_on", [])} for node in case["plan"]]
    if result.get("status") != "PASS" or result.get("runStatus") != "done" or result.get("durablePhases") != 3 or \
            result.get("nextIndex") != event_count or result.get("publicEventCount") != event_count or actual_plan != expected_plan or \
            result.get("actualPreset") != case["preset"] or result.get("actualPlanningSource") != expected_source or \
            result.get("planConfirmationRequired") is not confirm or result.get("publicPlanConfirmationQualified") is not confirm or \
            result.get("noTasksBeforePublicConfirmation") is not confirm or result.get("resourcesReleased") is not True or \
            result.get("finalAnswerVisible") is not True or result.get("inputBodySeededByOfflineOracle") is not False or \
            result.get("requestBodyStoredByRust") is not True or result.get("productionRuntimeProcesses") != ["Go", "Rust"] or \
            result.get("productionProviderQualified") is not False or result.get("fullMigrationQualified") is not False or len(exchanges) != requests:
        raise ValueError("Preset process evidence differs from retained behavior or overclaims qualification")
    actions, operations = [row.get("actionId") for row in exchanges], [row.get("operationId") for row in exchanges]
    task_actions = actions[int(planner):-1]
    if any(not isinstance(value, str) or not value for value in actions + operations) or len(set(operations)) != requests or \
            len(set(task_actions)) != 1 or len(set(actions)) != 2 + int(planner) or \
            any(row.get("executionEpoch") != "1" or row.get("stream") is not False or row.get("privateReasoningInPrompt") for row in exchanges):
        raise ValueError("Preset provider calls lack distinct nodes, shared task phase or privacy fences")
    if any(not exchanges[index + int(planner)].get("predecessorSummaryInPrompt") for index, node in enumerate(case["plan"]) if node.get("depends_on")):
        raise ValueError("Preset dependency summaries are absent from actual provider prompts")


def cancellation_cases() -> list[dict[str, Any]]:
    static = dict(preset_cases()[-1], name="full-awaiting-plan")
    dynamic = {"name":"auto-awaiting-plan", "preset":"auto", "query":"Prepare an isolated verification plan",
               "plan":[{"id":"coder","task":"write"},{"id":"critic","task":"review","depends_on":["coder"]}],
               "upstreamPlanner":True,"confirmation":True,"explicitConfirmation":False}
    return [dynamic, static]


def validate_cancellation_result(result: dict[str, Any], exchanges: list[dict[str, Any]], case: dict[str, Any]) -> None:
    before, after = result.get("beforeCancelIndex"), result.get("nextIndex")
    actual = [{"id":n.get("id"),"task":n.get("task"),"depends_on":n.get("depends_on",[])} for n in result.get("actualPlan",[])]
    expected = [{"id":n["id"],"task":n["task"],"depends_on":n.get("depends_on",[])} for n in case["plan"]]
    if result.get("status") != "PASS" or result.get("runStatus") != "cancelled" or type(before) is not int or before < 1 or \
            type(after) is not int or after != before + 1 or result.get("publicEventCount") != after or result.get("publicStreamEventCount") != after or \
            result.get("durablePhases") != 1 or actual != expected or result.get("actualPreset") != case["preset"] or \
            result.get("actualPlanningSource") != ("upstream-provider" if case["upstreamPlanner"] else "retained-preset-policy") or \
            any(result.get(key) is not True for key in ["resourcesReleased","noTasksAfterCancellation","repeatCancellationIdempotent", \
                "confirmationAfterCancellationDenied","planConfirmationRequired","requestBodyStoredByRust","publicCancellationWhileAwaitingPlanQualified"]) or \
            any(result.get(key) is not False for key in ["inputBodySeededByOfflineOracle","productionProviderQualified","fullMigrationQualified","activeTaskCancellationQualified"]) or \
            result.get("productionRuntimeProcesses") != ["Go","Rust"] or len(exchanges) != int(case["upstreamPlanner"]):
        raise ValueError("Public cancellation evidence is incomplete or overclaims qualification")
    if exchanges and any(not exchanges[0].get(key) for key in ["actionId","operationId","executionEpoch"]):
        raise ValueError("Actual planner HTTP exchange omitted its durable fence")


def run(args: argparse.Namespace) -> dict[str, Any]:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Native public compilation and containers are cloud-only on this host")
    preset_case = getattr(args, "preset_case", None)
    cancel_after_plan = bool(getattr(args, "cancel_after_plan", False))
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
                upstream = preset_case is None or preset_case["upstreamPlanner"]
                final_position = 4 if preset_case is None else len(preset_case["plan"]) + 1 + int(upstream)
                content = json.dumps({"agents":plan["tasks"]}) if upstream and position == 1 else ("Controlled final answer" if position == final_position else "## 摘要\nControlled public task summary")
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
            if cancel_after_plan:
                environment["DEEPSEEK_TEST_AGENT_CANCEL_AFTER_PLAN"] = "1"
            if signing.PRIVATE_ENV & set(environment) or "DEEPSEEK_API_KEY" in environment:
                raise ValueError("Private custody configuration entered Go")
            if preset_case is not None:
                environment.update(DEEPSEEK_TEST_AGENT_PRESET=preset_case["preset"], DEEPSEEK_TEST_AGENT_QUERY=preset_case["query"],
                    DEEPSEEK_TEST_AGENT_PLAN=json.dumps(preset_case["plan"], ensure_ascii=False),
                    DEEPSEEK_TEST_AGENT_CONFIRMATION=str(preset_case["confirmation"]).lower(),
                    DEEPSEEK_TEST_AGENT_EXPLICIT_CONFIRMATION=str(preset_case["explicitConfirmation"]).lower())
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
            edge_environment = public_edge_environment(worker_environment, address, static, artifacts, ready)
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
            if preset_case is None:
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
                expected_nodes = 1 if cancel_after_plan else (4 if preset_case is None else len(preset_case["plan"]) + 2)
                if len(rows) != expected_nodes or any(row[1] != "CONFIRMED" or hashlib.sha256(row[2]).hexdigest() != row[3] for row in rows):
                    raise ValueError("Rust actual four-node custody is missing or corrupt")
                leader = next((json.loads(row[2]) for row in rows if json.loads(row[2]).get("publicEvent", {}).get("type") == "agent_plan"), None)
                if leader is None:
                    raise ValueError("Rust retained planning outcome is missing")
                result["actualPlanningSource"] = "retained-preset-policy" if leader.get("planningSource") == "retained-preset-policy" else "upstream-provider"
            result.update(actualHTTPRequests=len(exchanges), providerExchanges=exchanges,
                requestBodyStoredByRust=True, bootstrapUIIsOfflineFixture=True, cloudOnlyNativeBuild=True,
                privateCustodyVariablesExportedToGo=False, readiness="NOT_READY")
            if cancel_after_plan:
                assert preset_case is not None
                validate_cancellation_result(result, exchanges, preset_case)
            elif preset_case is not None:
                validate_preset_result(result, exchanges, preset_case)
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preset-matrix", action="store_true")
    mode.add_argument("--cancellation-matrix", action="store_true")
    args = parser.parse_args()
    if args.preset_matrix or args.cancellation_matrix:
        output = args.out.resolve()
        cases = cancellation_cases() if args.cancellation_matrix else preset_cases()
        results = []
        for case in cases:
            child = argparse.Namespace(**vars(args))
            child.out, child.preset_case = output / case["name"], case
            child.cancel_after_plan = args.cancellation_matrix
            report = run(child)
            results.append({"name":case["name"],"preset":case["preset"],"status":report["status"],"sourceCommit":report["sourceCommit"],
                            "actualHTTPRequests":report["actualHTTPRequests"],"nextIndex":report["nextIndex"],"actualPlanningSource":report["actualPlanningSource"]})
            print(json.dumps(results[-1]), flush=True)
        result = {"status":"PASS","sourceCommit":results[0]["sourceCommit"],"caseCount":len(results),"cases":results,
                  "allInputsCreatedThroughRustHTTP":True,"cancellationWhileAwaitingPlanQualified":args.cancellation_matrix,"activeTaskCancellationQualified":False,"runtimePython":False,"realProviderQualified":False,"fullMigrationQualified":False,"readiness":"NOT_READY"}
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        result = run(args)
        print(json.dumps({key: result[key] for key in ["status", "sourceCommit", "actualHTTPRequests", "runStatus", "nextIndex", "readiness"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
