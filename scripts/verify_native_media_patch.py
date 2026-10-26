"""Cloud-only public media PATCH against an independent original-library oracle.

The retained media fixture is explicitly seeded while the native launcher is
stopped. This one workload is not authority, reconciliation or media migration
qualification. Neither the oracle nor the verifier is in the production tree.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import verify_agent_signing_process as signing
import verify_native_default_bootstrap as bootstrap
import verify_native_media_reads as reads

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/native_media_library_read_v1.json"
MEDIA_ID = "media_keep"
PATCH = {"title": "Native media update", "metadata": {"nativePatchProof": "controlled"}}
LIMIT = 2 * 1024**2
ORACLE = r'''
import json, sys
from deepseek_infra.infra.media import library
patch = json.load(sys.stdin)
media = library.update_media("media_keep", patch)
print(json.dumps({"ok": True, "media": media}))
'''


class ProbeFailure(RuntimeError):
    pass


def require(value: Any, code: str) -> None:
    if not value:
        raise ProbeFailure(code)


def seed(profile: Path, media: list[dict[str, Any]]) -> None:
    directory = profile / ".media"
    require(not directory.exists(), "MEDIA_PATCH_SEED_WOULD_REPLACE_EXISTING_MEDIA")
    directory.mkdir(parents=True)
    (directory / "library.json").write_text(json.dumps({"media": media}, ensure_ascii=False) + "\n", encoding="utf-8")


def original_patch(profile: Path) -> dict[str, Any]:
    environment = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
    environment.update(DEEPSEEK_INFRA_ROOT=str(profile), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", "-c", ORACLE], cwd=ROOT, env=environment,
        input=json.dumps(PATCH).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=True)
    require(len(result.stdout) <= LIMIT, "ORIGINAL_MEDIA_PATCH_OUTPUT_EXCEEDED_BOUND")
    body = json.loads(result.stdout)
    require(body.get("ok") is True and body.get("media", {}).get("mediaId") == MEDIA_ID,
            "ORIGINAL_MEDIA_PATCH_WORKLOAD_DID_NOT_SUCCEED")
    return body


def exchange(origin: str, path: str, method: str, token: str | None,
             payload: dict[str, Any] | None = None) -> tuple[int, bytes, str]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = None if payload is None else json.dumps(payload).encode()
    try:
        response = urlopen(Request(origin + path, method=method, headers=headers, data=data), timeout=10)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(LIMIT + 1)
        require(len(raw) <= LIMIT, "NATIVE_MEDIA_PATCH_RESPONSE_EXCEEDED_BOUND")
        return response.status, raw, response.headers.get("Content-Type", "")


def without_update_time(body: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(body))
    require(isinstance(result.get("media"), dict), "NATIVE_MEDIA_PATCH_RECORD_INVALID")
    result["media"].pop("updatedAt", None)
    return result


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.name == "posix",
            "NATIVE_MEDIA_PATCH_VERIFICATION_IS_CLOUD_ONLY")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(revision == os.environ.get("GITHUB_SHA"), "CLOUD_SOURCE_REVISION_DIFFERS")
    require(not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT),
            "CLOUD_TRACKED_SOURCE_IS_DIRTY")
    report["sourceCommit"] = revision
    fixture = json.loads(FIXTURE.read_text())
    require(fixture["schemaVersion"] == 1 and len(fixture["media"]) == 18, "MEDIA_PATCH_FIXTURE_CONTRACT_DIFFERS")
    report["fixtureSha256"] = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    report["patchSha256"] = hashlib.sha256(json.dumps(PATCH, sort_keys=True).encode()).hexdigest()
    for port in (8090, 50052):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    with tempfile.TemporaryDirectory(prefix="native-media-patch-") as temporary:
        scratch = Path(temporary)
        binaries, profile, mirror = scratch / "bin", scratch / "profile", scratch / "oracle"
        binaries.mkdir()
        (scratch / "go-tmp").mkdir()
        environment = signing.cache_environment(ROOT,
            env=signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        environment.update(CGO_ENABLED="0", GOTOOLCHAIN="local", GOMAXPROCS="2", GOPROXY="off",
            GOSUMDB="off", GOWORK="off", GOCACHE=str(args.go_cache.resolve()), GOTMPDIR=str(scratch / "go-tmp"))
        require(subprocess.check_output(["go", "version"], env=environment, text=True).startswith(
            "go version " + signing.GO_VERSION + " "), "NATIVE_GO_TOOLCHAIN_PIN_DIFFERS")
        for name in ("deepseekd", "deepseek-launch"):
            signing.record_command(["go", "build", "-p=2", "-o", str(binaries / name), "./cmd/" + name],
                ROOT / "go", environment, args.out, "build-" + name, timeout=600)
        for name, source in (("deepseek-worker", args.worker), ("deepseek-gateway", args.edge)):
            (binaries / name).symlink_to(source.resolve(strict=True))
        require(not profile.exists() and (ROOT / "static/ui/index.html").is_file(),
                "MEDIA_PATCH_PROFILE_IS_NOT_FRESH_OR_FRONTEND_MISSING")
        report.update(nativeBuildsPassed=True, profileInitiallyAbsent=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            public_port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{public_port}"
        launch_env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
        launch_env.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile),
            DEEPSEEK_INFRA_STATIC_DIR=str(ROOT / "static"))
        child: subprocess.Popen[bytes] | None = None
        identities: list[dict[str, Any]] = []

        def launch(label: str) -> str:
            nonlocal child, identities
            with (args.out / (label + ".log")).open("wb") as log:
                child = subprocess.Popen([str(binaries / "deepseek-launch"), "--server", "--no-prompt",
                    "--port", str(public_port)], cwd=scratch, env=launch_env, stdout=log, stderr=subprocess.STDOUT)
            bootstrap.wait_public(origin, child)
            rows = bootstrap.process_roles(child.pid)
            identities = [{**row, "startTicks": reads.process_identity(row["pid"])} for row in rows]
            require(all(row["startTicks"] is not None for row in identities), "MEDIA_PATCH_NATIVE_PROCESS_DISAPPEARED")
            report[label + "Processes"] = identities
            token_path = profile / ".auth-token"
            require(token_path.is_file() and not token_path.is_symlink() and token_path.stat().st_size <= 4096,
                    "MEDIA_PATCH_LOCAL_AUTH_FILE_INVALID")
            token = token_path.read_text().strip()
            require(len(token) == 64 and all(char in "0123456789abcdef" for char in token),
                    "MEDIA_PATCH_LOCAL_AUTH_TOKEN_INVALID")
            status, raw, _ = exchange(origin, "/api/control/status", "GET", token)
            require(status == 200 and json.loads(raw).get("ok") is True, "MEDIA_PATCH_CONTROL_NOT_HEALTHY")
            return token

        def shutdown() -> None:
            nonlocal child, identities
            if child is not None:
                reads.stop(child, identities)
                child, identities = None, []

        try:
            launch("empty-launch")
            require(reads.media_snapshot(profile) is None, "FRESH_NATIVE_LAUNCH_CREATED_MEDIA")
            initial_writer = reads.writer(profile)
            shutdown()
            original_auth = (profile / ".auth-token").read_bytes()
            seed(profile, fixture["media"])
            seed(mirror, fixture["media"])
            report["offlineMediaFixtureSeeded"] = True
            before = reads.media_snapshot(profile)
            expected = original_patch(mirror)
            require(reads.media_snapshot(profile) == before, "ORACLE_CHANGED_NATIVE_MEDIA_PROFILE")
            (args.out / "original-patch-oracle.json").write_text(
                json.dumps({"expectedPublicHTTPStatus": 200, "originalLibrarySucceeded": True,
                    "body": expected}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report.update(originalLibrarySucceeded=True, expectedPublicHTTPStatus=200)
            token = launch("patch-launch")
            require((profile / ".auth-token").read_bytes() == original_auth, "MEDIA_PATCH_RESTART_REPLACED_AUTH")
            current_writer = reads.writer(profile)
            require(current_writer[0] == initial_writer[0] + 1 and current_writer[1] != initial_writer[1],
                    "MEDIA_PATCH_RESTART_DID_NOT_ADVANCE_WRITER")
            status, raw, _ = exchange(origin, "/api/media/" + MEDIA_ID, "GET", token)
            require(status == 200 and json.loads(raw).get("media", {}).get("title") == "Notes",
                    "NATIVE_MEDIA_PATCH_PREREQUISITE_READ_FAILED")
            require(exchange(origin, "/api/media/" + MEDIA_ID, "PATCH", None, PATCH)[0] == 401,
                    "UNAUTHENTICATED_NATIVE_MEDIA_PATCH_WAS_NOT_DENIED")
            require(reads.media_snapshot(profile) == before, "MEDIA_CHANGED_BEFORE_AUTHENTICATED_PATCH")
            report.update(controlHealthyBeforePatch=True, originalMediaReadBeforePatch=True,
                unauthenticatedPatchDenied=True, nativePatchRequestStarted=True,
                nativePatchOutcome="EFFECT_UNKNOWN")
            started = datetime.now(timezone.utc).timestamp()
            status, raw, content_type = exchange(origin, "/api/media/" + MEDIA_ID, "PATCH", token, PATCH)
            finished = datetime.now(timezone.utc).timestamp()
            report.update(semanticStageReached=True, nativePatchReplyObserved=True, nativePatchHTTPStatus=status,
                responseSha256=hashlib.sha256(raw).hexdigest(), responseContentType=content_type,
                mediaStoreUnchangedAfterReply=reads.media_snapshot(profile) == before)
            require(status == 200, "NATIVE_MEDIA_PATCH_REFUSED")
            actual = json.loads(raw)
            require(without_update_time(actual) == without_update_time(expected), "NATIVE_MEDIA_PATCH_DIFFERS_FROM_ORIGINAL")
            stamp = actual["media"].get("updatedAt")
            require(isinstance(stamp, str) and len(stamp) <= 64, "NATIVE_MEDIA_PATCH_TIMESTAMP_INVALID")
            instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            require(instant.tzinfo is not None and started - 2 <= instant.timestamp() <= finished + 2,
                    "NATIVE_MEDIA_PATCH_TIMESTAMP_OUTSIDE_REQUEST")
            shutdown()
            token = launch("retained-restart")
            require((profile / ".auth-token").read_bytes() == original_auth, "MEDIA_PATCH_FINAL_RESTART_REPLACED_AUTH")
            status, raw, _ = exchange(origin, "/api/media/" + MEDIA_ID, "GET", token)
            require(status == 200 and json.loads(raw) == actual, "NATIVE_MEDIA_PATCH_NOT_RETAINED_AFTER_RESTART")
            shutdown()
            report.update(status="PASS", controlledNativeMediaPatchWorkloadQualified=True,
                originalResponseMatched=True, updatedAtWithinRequest=True, mediaRetainedAfterLauncherRestart=True,
                nativePatchOutcome="PUBLIC_SUCCESS_RETAINED_AUTHORITY_PROOF_NOT_ASSESSED")
        finally:
            shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--edge", type=Path, required=True)
    parser.add_argument("--go-cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"kind": "native-media-patch-process-v1", "status": "FAIL",
        "sourceCommit": os.environ.get("GITHUB_SHA"), "runId": os.environ.get("GITHUB_RUN_ID"),
        "event": os.environ.get("GITHUB_EVENT_NAME"), "semanticStageReached": False,
        "nativePatchRequestStarted": False, "nativePatchReplyObserved": False,
        "controlledNativeMediaPatchWorkloadQualified": False, "offlineMediaFixtureSeeded": False,
        "mediaPatchAuthorityRecoveryQualified": False, "allMediaWritesQualified": False,
        "mediaMigrationQualified": False, "mediaGUIQualified": False,
        "fullMigrationQualified": False, "readiness": "NOT_READY"}
    try:
        run(args, report)
    except Exception as error:
        report.update(status="FAIL", controlledNativeMediaPatchWorkloadQualified=False,
            failureCode=str(error) if isinstance(error, (ProbeFailure, reads.ProbeFailure, bootstrap.ProbeFailure))
                else type(error).__name__)
        if report["nativePatchRequestStarted"] and not report["nativePatchReplyObserved"]:
            report["nativePatchOutcome"] = "EFFECT_UNKNOWN"
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in (
        "status", "sourceCommit", "failureCode", "semanticStageReached", "nativePatchHTTPStatus")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
