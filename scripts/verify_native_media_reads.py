"""Cloud-only retained media reads through the normally authenticated launcher.

Legacy media fixtures are explicitly provisioned offline with the launcher
stopped. This probe does not qualify media writes, migration or a media GUI.
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
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import verify_agent_signing_process as signing
import verify_native_default_bootstrap as bootstrap

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/native_media_library_read_v1.json"
LIMIT = 2 * 1024**2
ORACLE = r'''
import json, sys
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.media.library import get_media, list_media
results = []
for case in json.load(sys.stdin):
    try:
        if case["operation"] == "list":
            media = list_media(project_id=case.get("projectId", ""),
                media_type=case.get("type", ""), status=case.get("status", ""))
        else:
            media = get_media(case["mediaId"])
        results.append({"status": 200, "body": {"ok": True, "media": media}})
    except AppError as error:
        results.append({"status": error.status,
            "body": {"error": str(error), "code": error.code.value}})
print(json.dumps(results))
'''


class ProbeFailure(RuntimeError):
    pass


def require(value: Any, code: str) -> None:
    if not value:
        raise ProbeFailure(code)


def media_snapshot(profile: Path) -> dict[str, str] | None:
    directory = profile / ".media"
    if not directory.exists():
        return None
    result = {}
    for path in sorted(directory.rglob("*")):
        require(not path.is_symlink(), "MEDIA_SNAPSHOT_FOUND_UNEXPECTED_LINK")
        result[path.relative_to(directory).as_posix()] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "directory")
    return result


def oracle(profile: Path, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    before = media_snapshot(profile)
    environment = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
    environment.update(DEEPSEEK_INFRA_ROOT=str(profile), PYTHONDONTWRITEBYTECODE="1")
    child = subprocess.run([sys.executable, "-B", "-c", ORACLE], cwd=ROOT,
        env=environment, input=json.dumps(cases).encode(), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=30, check=True)
    require(len(child.stdout) <= LIMIT, "OFFLINE_MEDIA_ORACLE_OUTPUT_EXCEEDED_BOUND")
    result = json.loads(child.stdout)
    require(len(result) == len(cases), "OFFLINE_MEDIA_ORACLE_CASE_COUNT_DIFFERS")
    require(media_snapshot(profile) == before, "OFFLINE_MEDIA_ORACLE_CHANGED_STORE")
    return result


def request(origin: str, uri: str, method: str, token: str | None) -> tuple[int, Any]:
    headers = {} if token is None else {"Authorization": "Bearer " + token}
    try:
        response = urlopen(Request(origin + uri, headers=headers, method=method), timeout=10)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(LIMIT + 1)
        require(len(raw) <= LIMIT, "NATIVE_MEDIA_RESPONSE_EXCEEDED_BOUND")
        if method == "HEAD":
            require(not raw, "NATIVE_MEDIA_HEAD_RETURNED_BODY")
            body = None
        else:
            body = json.loads(raw)
        return response.status, body


def check_reads(origin: str, token: str, cases: list[dict[str, Any]],
                expected: list[dict[str, Any]], profile: Path) -> list[dict[str, Any]]:
    before = media_snapshot(profile)
    rows = []
    for case, original in zip(cases, expected, strict=True):
        status, body = request(origin, case["uri"], "GET", token)
        row = {"uri": case["uri"], "originalStatus": original["status"],
            "nativeStatus": status, "bodyMatchesOriginal": body == original["body"]}
        rows.append(row)
        require(status == original["status"] and row["bodyMatchesOriginal"],
                "NATIVE_MEDIA_READ_DIFFERS_FROM_ORIGINAL:" + case["uri"])
        head_status, _ = request(origin, case["uri"], "HEAD", token)
        require(head_status == status, "NATIVE_MEDIA_HEAD_STATUS_DIFFERS:" + case["uri"])
        require(media_snapshot(profile) == before, "NATIVE_MEDIA_READ_CHANGED_STORE")
    return rows


def process_identity(pid: int) -> str | None:
    try:
        return (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def stop(child: subprocess.Popen[bytes], identities: list[dict[str, Any]]) -> None:
    bootstrap.stop(child)
    deadline = time.monotonic() + 10
    while any(process_identity(row["pid"]) == row["startTicks"] for row in identities):
        require(time.monotonic() < deadline, "OWNED_NATIVE_CHILD_SURVIVED_LAUNCHER_STOP")
        time.sleep(0.05)


def writer(profile: Path) -> tuple[int, str]:
    database = profile / "go-control/control.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT fencing_token,owner_instance_id FROM control_writer WHERE singleton=1").fetchone()
    require(row is not None, "NATIVE_CONTROL_WRITER_NOT_OBSERVED")
    return row


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.name == "posix",
            "NATIVE_MEDIA_PROCESS_VERIFICATION_IS_CLOUD_ONLY")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(revision == os.environ.get("GITHUB_SHA"), "CLOUD_SOURCE_REVISION_DIFFERS")
    require(not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT),
            "CLOUD_TRACKED_SOURCE_IS_DIRTY")
    report["sourceCommit"] = revision
    fixture = json.loads(FIXTURE.read_text())
    require(fixture["schemaVersion"] == 1 and len(fixture["media"]) == 18 and len(fixture["cases"]) == 17,
            "RETAINED_MEDIA_FIXTURE_CONTRACT_DIFFERS")
    report["fixtureSha256"] = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    cases = fixture["cases"]
    empty_cases = [cases[0], *cases[7:10], cases[15]]
    bootstrap.require_loopback_ports_available()
    with tempfile.TemporaryDirectory(prefix="native-media-read-") as temporary:
        scratch = Path(temporary)
        binaries, profile = scratch / "bin", scratch / "profile"
        binaries.mkdir()
        (scratch / "go-tmp").mkdir()
        environment = signing.cache_environment(ROOT,
            env=signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        environment.update(CGO_ENABLED="0", GOTOOLCHAIN="local", GOMAXPROCS="2",
            GOPROXY="off", GOSUMDB="off", GOWORK="off", GOCACHE=str(args.go_cache.resolve()),
            GOTMPDIR=str(scratch / "go-tmp"))
        require(subprocess.check_output(["go", "version"], env=environment, text=True).startswith(
            "go version " + signing.GO_VERSION + " "), "NATIVE_GO_TOOLCHAIN_PIN_DIFFERS")
        for name in ("deepseekd", "deepseek-launch"):
            signing.record_command(["go", "build", "-p=2", "-o", str(binaries / name), "./cmd/" + name],
                ROOT / "go", environment, args.out, "build-" + name, timeout=600)
        for name, source in (("deepseek-worker", args.worker), ("deepseek-gateway", args.edge)):
            (binaries / name).symlink_to(source.resolve(strict=True))
        require(not profile.exists() and (ROOT / "static/ui/index.html").is_file(),
                "MEDIA_PROFILE_IS_NOT_EMPTY_OR_FRONTEND_MISSING")
        report.update(profileInitiallyAbsent=True, nativeBuildsPassed=True)
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

        def launch(label: str) -> tuple[str, list[dict[str, Any]]]:
            nonlocal child, identities
            with (args.out / (label + ".log")).open("wb") as log:
                child = subprocess.Popen([str(binaries / "deepseek-launch"), "--server", "--no-prompt",
                    "--port", str(public_port)], cwd=scratch, env=launch_env, stdout=log, stderr=subprocess.STDOUT)
            bootstrap.wait_public(origin, child)
            roles = bootstrap.process_roles(child.pid)
            identities = [{**row, "startTicks": process_identity(row["pid"])} for row in roles]
            require(all(row["startTicks"] is not None for row in identities), "NATIVE_PROCESS_IDENTITY_DISAPPEARED")
            auth = profile / ".auth-token"
            require(auth.is_file() and not auth.is_symlink() and auth.stat().st_size <= 4096,
                    "DEFAULT_LAUNCHER_AUTH_FILE_INVALID")
            token = auth.read_text().strip()
            require(len(token) == 64 and all(char in "0123456789abcdef" for char in token),
                    "DEFAULT_LAUNCHER_AUTH_TOKEN_INVALID")
            for uri in ("/api/media", "/api/media/media_keep"):
                for method in ("GET", "HEAD"):
                    require(request(origin, uri, method, None)[0] == 401, "MEDIA_UNAUTHENTICATED_READ_NOT_DENIED")
            return token, identities

        def shutdown() -> None:
            nonlocal child, identities
            if child is not None:
                stop(child, identities)
                child, identities = None, []

        try:
            # The original library is only an offline read oracle. It is never
            # a child of the native launcher or a concurrent store writer.
            expected_empty = oracle(scratch / "empty-oracle-profile", empty_cases)
            require(not profile.exists(), "OFFLINE_EMPTY_ORACLE_CREATED_NATIVE_PROFILE")
            token, first_roles = launch("empty-launch")
            require(media_snapshot(profile) is None, "EMPTY_NATIVE_START_CREATED_MEDIA_STORE")
            report["emptyProfileReads"] = check_reads(origin, token, empty_cases, expected_empty, profile)
            report["emptyProductionProcesses"] = first_roles
            initial_writer = writer(profile)
            shutdown()
            token_bytes = (profile / ".auth-token").read_bytes()
            directory = profile / ".media"
            require(not directory.exists(), "EMPTY_READS_CREATED_MEDIA_DIRECTORY")
            directory.mkdir()
            (directory / "library.json").write_text(json.dumps({"media": fixture["media"]},
                ensure_ascii=False) + "\n", encoding="utf-8")
            retained = media_snapshot(profile)
            report["offlineMediaFixtureSeeded"] = True
            expected = oracle(profile, cases)
            (args.out / "original-media-read-oracle.json").write_text(
                json.dumps({"cases": cases, "results": expected}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            generations = [initial_writer]
            for label in ("retained-launch", "retained-restart"):
                token, roles = launch(label)
                require((profile / ".auth-token").read_bytes() == token_bytes, "RESTART_REPLACED_LOCAL_AUTH")
                observed = writer(profile)
                require(observed[0] == generations[-1][0] + 1 and observed[1] != generations[-1][1],
                        "NATIVE_RESTART_DID_NOT_ADVANCE_WRITER_FENCE")
                generations.append(observed)
                require(media_snapshot(profile) == retained, "NATIVE_START_CHANGED_RETAINED_MEDIA")
                report[label] = {"processes": roles,
                    "reads": check_reads(origin, token, cases, expected, profile)}
                shutdown()
                require(media_snapshot(profile) == retained, "NATIVE_STOP_CHANGED_RETAINED_MEDIA")
            report.update(status="PASS", controlledAuthenticatedMediaReadsQualified=True,
                emptyReadCases=len(empty_cases), retainedReadCasesPerLaunch=len(cases),
                retainedLaunchCount=2, headResponsesChecked=len(empty_cases) + 2 * len(cases),
                mediaStoreUnchangedAcrossReadsAndRestarts=True, defaultAuthRetainedAcrossRestarts=True,
                unauthenticatedReadsDenied=True, ownedNativeChildrenStopped=True,
                writerFencingTokens=[row[0] for row in generations], retainedMediaSnapshot=retained)
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
    report: dict[str, Any] = {"kind": "native-media-library-read-process-v1", "status": "FAIL",
        "sourceCommit": os.environ.get("GITHUB_SHA"), "runId": os.environ.get("GITHUB_RUN_ID"),
        "event": os.environ.get("GITHUB_EVENT_NAME"), "controlledAuthenticatedMediaReadsQualified": False,
        "offlineMediaFixtureSeeded": False, "mediaWritesOrProcessingQualified": False,
        "mediaMigrationQualified": False, "mediaGUIQualified": False,
        "fullMigrationQualified": False, "readiness": "NOT_READY"}
    try:
        run(args, report)
    except Exception as error:
        report.update(status="FAIL", controlledAuthenticatedMediaReadsQualified=False,
            failureCode=str(error) if isinstance(error, (ProbeFailure, bootstrap.ProbeFailure)) else type(error).__name__)
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "sourceCommit", "failureCode")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
