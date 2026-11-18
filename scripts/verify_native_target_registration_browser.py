"""Drive filesystem target registration through the packaged native process roles."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlencode

from playwright.sync_api import APIResponse, BrowserContext, Page, Response, Route, sync_playwright

from verify_native_default_bootstrap import ROOT, process_roles, require_loopback_ports_available, stop, wait_public

TARGETS = "/api/workspace/backup-targets"
MARKER = ".deepseek-infra-backup-target.json"
PRIVATE_FIELD = "_nativeTargetRegistrationV1"


class TargetCheckFailed(RuntimeError):
    """Reports a controlled failure code without copying credentials or response bodies."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise TargetCheckFailed(code)


def json_response(response: APIResponse | Response, status: int, stage: str) -> dict[str, Any]:
    require(response.status == status, f"{stage}:HTTP_{response.status}")
    data = response.json()
    require(isinstance(data, dict), f"{stage}:BODY")
    return data


def open_automatic_tab(context: BrowserContext, origin: str, token: str) -> tuple[Page, dict[str, Any]]:
    page = context.new_page()
    loaded = page.goto(origin + "/?" + urlencode({"token": token}), wait_until="domcontentloaded")
    require(loaded is not None and loaded.status == 200 and page.url == origin + "/", "LOCAL_LOGIN")
    require(any(c["name"] == "auth_token" and c["httpOnly"] for c in context.cookies(origin)), "LOCAL_COOKIE")
    page.get_by_role("button", name="备份与恢复", exact=True).click()
    with page.expect_response(lambda r: r.url == origin + TARGETS and r.request.method == "GET") as arrival:
        page.get_by_role("tab", name="自动备份", exact=True).click()
    listed = json_response(arrival.value, 200, "TARGET_LIST")
    page.get_by_role("heading", name="备份目标", exact=True).wait_for()
    return page, listed


def browser_register(page: Page, origin: str, directory: Path, label: str, target_id: str) -> tuple[str, bytes, bytes]:
    page.get_by_placeholder(re.compile("^目标目录绝对路径")).fill(str(directory))
    page.get_by_placeholder("标签（可选）", exact=True).fill(label)
    with page.expect_response(lambda r: r.url == origin + TARGETS and r.request.method == "POST") as arrival:
        page.get_by_role("button", name="注册目标", exact=True).click()
    response = arrival.value
    document = json_response(response, 200, "REGISTER_TARGET")
    require(document.get("targetId") == target_id and document.get("path") == str(directory)
            and document.get("label") == label and PRIVATE_FIELD not in document, "TARGET_PROJECTION")
    page.get_by_text(label, exact=True).wait_for()
    operation = response.header_value("x-deepseek-operation-id")
    request_bytes = response.request.post_data_buffer
    if not operation or not operation.startswith("target-inspect-public-v1-") or not request_bytes:
        raise TargetCheckFailed("TARGET_OPERATION_RECEIPT")
    return operation, request_bytes, response.body()


def register_after_lost_response(page: Page, origin: str, directory: Path, label: str, target_id: str | None) -> tuple[str, bytes, bytes]:
    committed: list[tuple[str, bytes, bytes]] = []
    endpoint = origin + TARGETS

    def lose_response(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        # This sends the original request to the real runtime. Only delivery of
        # its already-committed response to the browser is interrupted.
        upstream = route.fetch(max_retries=0)
        body = json_response(upstream, 200, "TARGET_COMMIT_BEFORE_LOSS")
        operation = route.request.headers.get("x-deepseek-operation-id")
        raw = route.request.post_data_buffer
        published_id = body.get("targetId")
        valid_id = isinstance(published_id, str) and (
            published_id == target_id if target_id is not None else re.fullmatch(r"target_[0-9a-f]{12}", published_id) is not None
        )
        if not operation or not raw or not valid_id or upstream.headers.get("x-deepseek-operation-id") != operation:
            raise TargetCheckFailed("ORIGINAL_BROWSER_OPERATION_BINDING")
        committed.append((operation, raw, upstream.body()))
        route.abort("failed")

    page.route(endpoint, lose_response)
    page.get_by_placeholder(re.compile("^目标目录绝对路径")).fill(str(directory))
    page.get_by_placeholder("标签（可选）", exact=True).fill(label)
    register = page.get_by_role("button", name="注册目标", exact=True)
    with page.expect_event("requestfailed", predicate=lambda request: request.url == endpoint and request.method == "POST"):
        register.click()
    page.unroute(endpoint, lose_response)
    require(len(committed) == 1, "LOST_RESPONSE_AFTER_DURABLE_COMMIT")
    require(page.get_by_placeholder(re.compile("^目标目录绝对路径")).input_value() == str(directory), "FORM_RETAINED_AFTER_LOSS")
    committed_target: str = json.loads(committed[0][2])["targetId"]
    recovered = browser_register(page, origin, directory, label, committed_target)
    require(recovered == committed[0], "BROWSER_RETRY_RETURNED_ORIGINAL_RESPONSE")
    return recovered


def fixture_target(directory: Path, target_id: str) -> bytes:
    directory.mkdir()
    marker = (json.dumps({"schemaVersion": 3, "targetId": target_id, "targetNonce": "00102",
                          "createdAt": "2000-01-01T00:00:00Z", "targetGeneration": 0},
                         indent=2) + "\n").encode()
    (directory / MARKER).write_bytes(marker)
    return marker


def assert_journal(profile: Path, scopes: dict[str, tuple[str, int, int]], move_operation: str, init_operation: str) -> dict[str, Any]:
    database = profile / "go-control/control.sqlite3"
    binding = json.loads((profile / "rust-worker/custody/manifest.json").read_bytes())
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("""SELECT r.operation_id,r.target_id,r.revision,r.public_generation,r.observation,
                            m.action_id,m.execution_epoch,m.writer_fencing_token
                            FROM control_target_registrations r JOIN control_operator_mutations m
                            USING(operation_id) ORDER BY r.operation_id""").fetchall()
        custody = db.execute("SELECT signer_public_key,fleet_id,environment FROM control_target_registration_identity").fetchone()
        writer = db.execute("SELECT fencing_token FROM control_writer WHERE singleton=1").fetchone()
        health_count = db.execute("SELECT COUNT(*) FROM backup_target_health").fetchone()
        imported = db.execute("SELECT COUNT(*) FROM control_inventory_imports WHERE domain='target'").fetchone()
        moves = db.execute("SELECT operation_id,length(observation) FROM control_target_registration_locations").fetchall()
        marker = db.execute("""SELECT operation_id,execution_epoch,state,claim_sha256,preparation_sha256,receipt
                               FROM control_target_marker_operations""").fetchall()
    require(custody == (binding["signer_public_key"], binding["fleet_id"], binding["environment"]), "RUST_CUSTODY_BINDING")
    require({row[0]: (row[1], row[2], row[3]) for row in rows} == scopes
            and all(row[4] and row[0] == row[5] and row[6] == 1 and row[7] == 1 for row in rows), "DURABLE_REGISTRATION_SCOPE")
    require(health_count == (0,) and imported == (0,), "NATIVE_PROFILE_NO_IMPORTED_HEALTH")
    require(len(moves) == 1 and moves[0][0] == move_operation and moves[0][1] > 0, "DURABLE_MOVE_RECEIPT")
    require(len(marker) == 1 and marker[0][:3] == (init_operation, 1, "SETTLED")
            and len(marker[0][3]) == len(marker[0][4]) == 32 and bool(marker[0][5]), "DURABLE_MARKER_EFFECT")
    if writer is None:
        raise TargetCheckFailed("CONTROL_WRITER_ABSENT")
    return {"writerFence": writer[0], "signedRegistrationReceipts": len(rows), "signedMoveReceipts": len(moves),
            "markerReceiptSha256": hashlib.sha256(marker[0][5]).hexdigest(),
            "witnessSha256": {row[1] + "@" + str(row[2]): hashlib.sha256(row[4]).hexdigest() for row in rows}}


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    require(os.name == "posix" and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_REPOSITORY") == "leizd/DeepSeek-Infra", "HOSTED_CLOUD_ONLY")
    sha, tree = (subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, text=True).strip()
                 for ref in ("HEAD", "HEAD^{tree}"))
    require(sha == os.environ.get("GITHUB_SHA") and not subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=ROOT), "VERIFIED_SOURCE_CHECKOUT")
    report.update(sourceCommit=sha, sourceTree=tree)
    require_loopback_ports_available()
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve()
    binaries = args.bin_dir.resolve()
    require(binaries.is_relative_to(runner_temp) and all((binaries / name).is_file() for name in
            ("deepseek-launch", "deepseekd", "deepseek-worker", "deepseek-gateway")), "NATIVE_BINARIES")
    with tempfile.TemporaryDirectory(dir=runner_temp, prefix="native-target-browser-") as tmp:
        root = Path(tmp).resolve()
        profile, runtime_temp = root / "profile", root / "runtime-temp"
        runtime_temp.mkdir()
        first, second, moved = root / "target-one", root / "target-two", root / "moved-target"
        first_marker = fixture_target(first, "target_one")
        second_marker = fixture_target(second, "target_two")
        nested = first / "contained"
        fixture_target(nested, "target_contained")
        clone = root / "duplicate-target"
        clone.mkdir()
        (clone / MARKER).write_bytes(first_marker)
        uninitialized = root / "uninitialized"
        uninitialized.mkdir()
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")}
        env.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile),
                   DEEPSEEK_INFRA_STATIC_DIR=str(ROOT / "static"), TMPDIR=str(runtime_temp))

        def start() -> subprocess.Popen[bytes]:
            child = subprocess.Popen([str(binaries / "deepseek-launch"), "--server", "--no-prompt",
                                      "--port", str(port)], env=env, cwd=root, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            try:
                wait_public(origin, child)
            except BaseException:
                stop(child)
                raise
            return child

        report["stage"] = "first-launch"
        process: subprocess.Popen[bytes] | None = None
        try:
            process = start()
            token = (profile / ".auth-token").read_text(encoding="utf8").strip()
            require(bool(token), "LAUNCHER_AUTH_TOKEN")
            report["firstProcesses"] = process_roles(process.pid)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    context = browser.new_context(service_workers="block")
                    anonymous = context.request.post(origin + TARGETS, data={"path": str(first)})
                    require(anonymous.status == 401, "ANONYMOUS_WRITE")
                    report["stage"] = "browser-register"
                    page, initial = open_automatic_tab(context, origin, token)
                    require(initial.get("targets") == [] and initial.get("health") == [], "INITIAL_NATIVE_LIST")
                    op_one, original_body, original_reply = register_after_lost_response(page, origin, first, "Isolated disk one", "target_one")
                    op_two, _, _ = browser_register(page, origin, second, "Isolated disk two", "target_two")
                    report["stage"] = "filesystem-denials"
                    for directory, status, code in ((nested, 400, "invalid_payload"), (clone, 409, "invalid_request")):
                        denied = context.request.post(origin + TARGETS, data={"path": str(directory)})
                        require(json_response(denied, status, "TARGET_DENIAL").get("code") == code, "TARGET_DENIAL_CODE")
                    absent_nested = first / "empty-contained"
                    absent_nested.mkdir()
                    denied = context.request.post(origin + TARGETS, data={"path": str(absent_nested)})
                    require(json_response(denied, 400, "EMPTY_TARGET_OVERLAP").get("code") == "invalid_payload"
                            and not (absent_nested / MARKER).exists(), "EMPTY_TARGET_OVERLAP_NO_MARKER")
                    denied = context.request.post(origin + TARGETS, data={"path": str(uninitialized), "maxConcurrentTransfers": -1})
                    require(json_response(denied, 400, "INVALID_INITIALIZATION_METADATA").get("code") == "invalid_payload"
                            and not (uninitialized / MARKER).exists(), "INVALID_METADATA_NO_MARKER")
                    report["stage"] = "native-marker-initialization"
                    op_init, _, init_reply = register_after_lost_response(page, origin, uninitialized, "Initialized disk", None)
                    initialized: dict[str, Any] = json.loads(init_reply)
                    initialization = (uninitialized / MARKER).read_bytes()
                    genesis = json.loads(initialization)
                    require(genesis["schemaVersion"] == 3 and genesis["targetId"] == initialized["targetId"]
                            and re.fullmatch(r"[0-9a-f]{32}", genesis["targetNonce"]) is not None
                            and genesis["targetNonce"] == initialized["targetNonce"]
                            and re.fullmatch(r"inc_[0-9a-f]{16}", genesis["incarnationId"]) is not None
                            and re.fullmatch(r"inst_[0-9a-f]{16}", genesis["ownerInstallationId"]) is not None
                            and genesis["targetGeneration"] == 0 and genesis["latestCommitHash"] == "0" * 64
                            and genesis["createdAt"] == initialized["createdAt"], "RETAINED_MARKER_GENESIS")
                    report["stage"] = "move-and-journal"
                    first.rename(moved)
                    op_move, _, _ = browser_register(page, origin, moved, "Isolated disk moved", "target_one")
                    scopes = {op_one: ("target_one", 1, 1), op_two: ("target_two", 1, 1), op_move: ("target_one", 2, 2),
                              op_init: (initialized["targetId"], 1, 1)}
                    before = assert_journal(profile, scopes, op_move, op_init)
                    context.close()
                    stop(process)
                    process = None
                    report["stage"] = "restart-and-replay"
                    process = start()
                    report["secondProcesses"] = process_roles(process.pid)
                    context = browser.new_context(service_workers="block")
                    page, listed = open_automatic_tab(context, origin, token)
                    page.get_by_text("Isolated disk moved", exact=True).wait_for()
                    page.get_by_text("Isolated disk two", exact=True).wait_for()
                    page.get_by_text("Initialized disk", exact=True).wait_for()
                    public = listed.get("targets", [])
                    require(len(public) == 3 and {doc["targetId"] for doc in public} == {"target_one", "target_two", initialized["targetId"]}
                            and all(PRIVATE_FIELD not in doc for doc in public) and listed.get("health") == [], "RESTART_LIST")
                    replay = context.request.post(origin + TARGETS, data=original_body, headers={
                        "Content-Type": "application/json", "X-DeepSeek-Operation-Id": op_one})
                    require(replay.status == 200 and replay.body() == original_reply
                            and replay.headers.get("x-deepseek-operation-id") == op_one, "ORIGINAL_REPLY_REPLAY")
                    conflict = context.request.post(origin + TARGETS, data={"path": str(moved)}, headers={
                        "X-DeepSeek-Operation-Id": op_one})
                    require(json_response(conflict, 409, "REBOUND_REPLAY").get("code") == "mutation_request_replay_conflict", "REBOUND_REPLAY_CODE")
                    after = assert_journal(profile, scopes, op_move, op_init)
                    require(after["writerFence"] == before["writerFence"] + 1 and after["witnessSha256"] == before["witnessSha256"]
                            and after["markerReceiptSha256"] == before["markerReceiptSha256"],
                            "WRITER_RESTART_BINDINGS")
                    require((moved / MARKER).read_bytes() == (clone / MARKER).read_bytes() == first_marker
                            and (second / MARKER).read_bytes() == second_marker
                            and (uninitialized / MARKER).read_bytes() == initialization, "MARKER_BYTES")
                finally:
                    browser.close()
        finally:
            stop(process)
        report.update(status="PASS", stage="completed", chromium=True, authenticatedExistingForm=True,
                      activeTargetsAfterRestart=3, durableJournal=after, previousWriterFence=before["writerFence"],
                      originalMarkerBytesRetained=True, originalHTTPResponseReplayed=True, overlapAndCloneRefused=True,
                      existingMarkerRegistrationQualified=True, browserLostCommittedResponseRecovered=True,
                      absentMarkerInitializationQualified=True,
                      initializationMarkerSha256=hashlib.sha256(initialization).hexdigest())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report: dict[str, Any] = {"kind": "native-target-public-browser-v1", "status": "FAIL",
                              "runId": os.environ.get("GITHUB_RUN_ID"), "event": os.environ.get("GITHUB_EVENT_NAME"),
                              "runAttempt": os.environ.get("GITHUB_RUN_ATTEMPT"), "stage": "source",
                              "existingMarkerRegistrationQualified": False, "fullMigrationQualified": False, "readiness": "NOT_READY"}
    try:
        run(args, report)
    except TargetCheckFailed as exc:
        report["failure"] = str(exc)
    except Exception as exc:
        # Browser exceptions may include the login URL. Store only the class.
        report["failure"] = type(exc).__name__
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(json.dumps({key: report.get(key) for key in ("status", "stage", "failure", "sourceCommit")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
