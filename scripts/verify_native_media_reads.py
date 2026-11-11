"""Cloud-only retained media reads through the normally authenticated launcher.

Legacy media fixtures are explicitly provisioned offline with the launcher
stopped. This probe does not qualify media writes, migration or a media GUI.
"""
from __future__ import annotations

import argparse
import base64
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

import verify_agent_signing_process as signing
import verify_native_default_bootstrap as bootstrap
from native_json_comparison import exact_json_values_equal, json_values_equal, load_exact_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/native_media_library_read_v1.json"
SEGMENT_FIXTURE = ROOT / "tests/fixtures/native_retained_media_segments_v1.json"
SEGMENT_FIXTURE_SHA256 = "5c31b1d41b32781e4f9d40f0430673a434c4c3e18994881aec1d0465aa7ee0fc"
LIMIT = 2 * 1024**2
ORACLE = r'''
import json, sys
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.media.library import get_media, list_media, list_segments
results = []
for case in json.load(sys.stdin):
    try:
        if case["operation"] == "list":
            media = list_media(project_id=case.get("projectId", ""),
                media_type=case.get("type", ""), status=case.get("status", ""))
        elif case["operation"] == "segments":
            get_media(case["mediaId"])
            results.append({"status": 200, "body": {"ok": True, "segments": list_segments(case["mediaId"])}})
            continue
        else:
            media = get_media(case["mediaId"])
        results.append({"status": 200, "body": {"ok": True, "media": media}})
    except AppError as error:
        results.append({"status": error.status,
            "body": {"error": str(error), "code": error.code.value}})
print(json.dumps(results))
'''
SEGMENT_HTTP_ORACLE = r'''
import json, sys
from pathlib import Path
from deepseek_infra.core.config import settings
from tests.test_web_media_routes import _running_server, _request, _auth_headers

assert sys.version_info[:2] == (3, 12) and settings.auth.enabled
profile = Path(sys.argv[1]).resolve()
assert settings.root == profile
def snapshot():
    return {str(p.relative_to(profile)): p.read_bytes() for p in (profile / ".media").rglob("*") if p.is_file()}
cases = json.load(sys.stdin)
before = snapshot()
with _running_server() as server:
    for case in cases:
        status, raw, _ = _request(server, "GET", case["uri"], headers=_auth_headers())
        assert status == case["status"] and json.loads(raw) == json.loads(case["bodyJson"]), (case["name"], status, raw)
        status, raw, _ = _request(server, "GET", case["uri"])
        assert status == 401 and json.loads(raw) == {"error": "Auth required", "code": "unauthorized"}
        assert snapshot() == before
print(json.dumps({"status": "PASS", "actualOriginalTCP": True, "cases": len(cases), "requests": 2 * len(cases),
                  "python": ".".join(map(str, sys.version_info[:3]))}))
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


def seed_segment_fixture(profile: Path, fixture: dict[str, Any]) -> dict[str, str]:
    directory = profile / ".media"
    require(not directory.exists(), "SEGMENT_FIXTURE_PROFILE_ALREADY_CONTAINS_MEDIA")
    (directory / "segments").mkdir(parents=True)
    library = base64.b64decode(fixture["libraryBase64"], validate=True)
    require(hashlib.sha256(library).hexdigest() == fixture["librarySha256"], "SEGMENT_FIXTURE_LIBRARY_DIGEST_DIFFERS")
    (directory / "library.json").write_bytes(library)
    for case in fixture["cases"]:
        media_id = case["mediaId"]
        require(isinstance(media_id, str) and len(media_id) <= 128 and media_id.startswith("media_segments_")
                and media_id.removeprefix("media_segments_").isdigit(), "SEGMENT_FIXTURE_MEDIA_ID_INVALID")
        require(case["uri"] == "/api/media/" + media_id + "/segments", "SEGMENT_FIXTURE_URI_DIFFERS")
        if case["rawBase64"] is not None:
            raw = base64.b64decode(case["rawBase64"], validate=True)
            require(hashlib.sha256(raw).hexdigest() == case["rawSha256"], "SEGMENT_FIXTURE_RECORD_DIGEST_DIFFERS")
            (directory / "segments" / (media_id + ".json")).write_bytes(raw)
    snapshot = media_snapshot(profile)
    if snapshot is None or len(snapshot) != 25:
        raise ProbeFailure("SEGMENT_FIXTURE_FILE_COUNT_DIFFERS")
    return snapshot


def original_segment_http(profile: Path, fixture: dict[str, Any], out: Path) -> dict[str, Any]:
    before = seed_segment_fixture(profile, fixture)
    environment = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    environment.update(DEEPSEEK_INFRA_ROOT=str(profile), DEEPSEEK_RUNTIME_MODE="python_authoritative",
                       AUTH_DISABLED="0", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-B", "-c", SEGMENT_HTTP_ORACLE, str(profile)], cwd=ROOT,
        env=environment, input=json.dumps(fixture["cases"]).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=120, check=False)
    require(len(result.stdout) <= LIMIT and len(result.stderr) <= LIMIT, "ORIGINAL_SEGMENT_ORACLE_OUTPUT_EXCEEDED_BOUND")
    (out / "original-segment-http.log").write_bytes(result.stderr)
    require(result.returncode == 0, "ORIGINAL_SEGMENT_HTTP_REPLAY_FAILED")
    observed = json.loads(result.stdout.splitlines()[-1])
    require(observed["status"] == "PASS" and observed["actualOriginalTCP"] and observed["cases"] == 24
            and observed["requests"] == 48 and observed["python"].startswith("3.12."), "ORIGINAL_SEGMENT_HTTP_REPORT_DIFFERS")
    require(media_snapshot(profile) == before, "ORIGINAL_SEGMENT_HTTP_CHANGED_SOURCE")
    return observed


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


def request(origin: str, uri: str, method: str, token: str | None, *, exact_numbers: bool = False) -> tuple[int, Any]:
    endpoint = urlsplit(origin)
    require(endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1" and endpoint.port is not None
            and endpoint.username is None and endpoint.password is None and not endpoint.path
            and not endpoint.query and not endpoint.fragment, "NATIVE_MEDIA_ORIGIN_IS_NOT_LOOPBACK")
    require(uri.startswith("/") and not uri.startswith("//"), "NATIVE_MEDIA_REQUEST_TARGET_INVALID")
    headers = {"Connection": "close"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    connection = HTTPConnection("127.0.0.1", endpoint.port, timeout=10)
    try:
        connection.request(method, uri, headers=headers)
        with connection.getresponse() as response:
            require(not 300 <= response.status < 400, "NATIVE_MEDIA_UNEXPECTED_REDIRECT:" + uri)
            if method == "HEAD":
                # HTTPResponse.read suppresses HEAD data by design. Inspect the
                # socket-backed file after the headers until EOF or a byte.
                wire = response.fp
                if wire is None:
                    raise ProbeFailure("NATIVE_MEDIA_HEAD_WIRE_UNAVAILABLE")
                require(not wire.read(1), "NATIVE_MEDIA_HEAD_RETURNED_BODY:" + uri)
                return response.status, None
            raw = response.read(LIMIT + 1)
            require(len(raw) <= LIMIT, "NATIVE_MEDIA_RESPONSE_EXCEEDED_BOUND")
            require(response.length in (None, 0), "NATIVE_MEDIA_RESPONSE_TRUNCATED")
            require(response.getheader("Content-Type", "").startswith("application/json"), "NATIVE_MEDIA_RESPONSE_NOT_JSON")
            return response.status, load_exact_json(raw) if exact_numbers else json.loads(raw)
    finally:
        connection.close()


def check_reads(origin: str, token: str, cases: list[dict[str, Any]],
                expected: list[dict[str, Any]], profile: Path, *, exact_numbers: bool = False) -> list[dict[str, Any]]:
    before = media_snapshot(profile)
    auth_before = (profile / ".auth-token").read_bytes()
    rows = []
    for case, original in zip(cases, expected, strict=True):
        status, body = request(origin, case["uri"], "GET", token, exact_numbers=exact_numbers)
        expected_body = load_exact_json(original["bodyJson"]) if exact_numbers else original["body"]
        same = exact_json_values_equal if exact_numbers else json_values_equal
        compare_home_media = case.get("compareHomeMedia", False)
        if compare_home_media:
            require(status == original["status"] == 200, "NATIVE_MEDIA_HOME_STATUS_DIFFERS")
            body = home_media_fields(body)
            require(set(expected_body) == {"ok", "media", "count"}, "RETAINED_HOME_PROJECTION_DIFFERS")
        row = {"uri": case["uri"], "originalStatus": original["status"],
            "nativeStatus": status, "bodyMatchesOriginal": same(body, expected_body)}
        if compare_home_media:
            row["bodyComparisonScope"] = ["ok", "recent.media", "counts.media"]
        rows.append(row)
        require(status == original["status"] and row["bodyMatchesOriginal"],
                "NATIVE_MEDIA_READ_DIFFERS_FROM_ORIGINAL:" + case["uri"])
        require((profile / ".auth-token").read_bytes() == auth_before, "NATIVE_MEDIA_GET_CHANGED_AUTH")
        require(media_snapshot(profile) == before, "NATIVE_MEDIA_GET_CHANGED_STORE")
        head_status, _ = request(origin, case["uri"], "HEAD", token)
        require(head_status == status, "NATIVE_MEDIA_HEAD_STATUS_DIFFERS:" + case["uri"])
        require((profile / ".auth-token").read_bytes() == auth_before, "NATIVE_MEDIA_HEAD_CHANGED_AUTH")
        require(media_snapshot(profile) == before, "NATIVE_MEDIA_READ_CHANGED_STORE")
    return rows


def home_media_fields(value: Any) -> dict[str, Any]:
    return {"ok": value["ok"], "media": value["recent"]["media"], "count": value["counts"]["media"]}


def frozen_retained_cases() -> tuple[list[dict[str, Any]], dict[str, str]]:
    directory = ROOT / "tests/fixtures"
    retained_path = directory / "native_media_retained_http_v1.json"
    boundary_path = directory / "native_media_retained_boundaries_v1.json"
    retained_raw, boundary_raw = retained_path.read_bytes(), boundary_path.read_bytes()
    retained, boundaries = json.loads(retained_raw), json.loads(boundary_raw)
    require(retained["schemaVersion"] == boundaries["schemaVersion"] == 1
            and retained["provenance"]["actualSocketRequests"] == 170 and len(retained["cases"]) == 34
            and boundaries["actualOriginalLoopbackRequests"] == 18 and boundaries["actualAuthenticatedRequests"] == 16
            and boundaries["unauthorized"]["status"] == boundaries["workspaceHomeUnauthorized"]["status"] == 401
            and json_values_equal(boundaries["unauthorized"]["body"], boundaries["workspaceHomeUnauthorized"]["body"])
            and len(boundaries["variants"]) == 3,
            "RETAINED_ORIGINAL_FIXTURE_CONTRACT_DIFFERS")
    for relative, digest in boundaries["originalInputHashes"].items():
        require(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest,
                "RETAINED_BOUNDARY_ORIGINAL_SOURCE_DRIFT")
    original_reader = ROOT / "deepseek_infra/infra/media/library.py"
    require(hashlib.sha256(original_reader.read_bytes()).hexdigest() == retained["provenance"]["originalReaderSha256"],
            "RETAINED_VALUE_ORIGINAL_READER_DRIFT")
    paths = {"list": "/api/media", "detail": "/api/media/media_kept",
             "independentDetail": "/api/media/media_control", "filteredList": "/api/media?type=pdf"}
    result = []
    for case in retained["cases"]:
        auth = case["results"]["unauthorized"]
        require(auth["status"] == 401, "RETAINED_VALUE_ORIGINAL_AUTH_DIFFERS")
        reads = [dict(case["results"][role], uri=uri) for role, uri in paths.items()]
        result.append(dict(case, kind="retained", reads=reads, unauthorizedBody=auth["body"]))
    expected_boundary_routes = {
        "off_window_and_segment_encoding": {
            "collection": ("/api/media", 500), "filter": ("/api/media?type=pdf", 200),
            "visible": ("/api/media/visible", 200), "unencodable": ("/api/media/unencodable", 500),
            "segments": ("/api/media/unencodable/segments", 200),
            "window_one": ("/api/workspace/home?limit=1", 200), "window_two": ("/api/workspace/home?limit=2", 500),
        },
        "scalar_sort_and_normalization": {
            "collection": ("/api/media", 200), "filter": ("/api/media?type=video", 200),
            "window_one": ("/api/workspace/home?limit=1", 200), "window_all": ("/api/workspace/home?limit=3", 200),
        },
        "normalization_precedes_encoding": {
            "collection": ("/api/media", 400), "window_one": ("/api/workspace/home?limit=1", 400),
            "unencodable": ("/api/media/unencodable", 500), "segments": ("/api/media/unencodable/segments", 200),
            "invalid_public_path": ("/api/media/invalid_public_path", 400),
        },
    }
    denied_body = boundaries["unauthorized"]["body"]
    for case in boundaries["variants"]:
        routes = {role: (read["uri"], read["status"]) for role, read in case["results"].items()}
        require(routes == expected_boundary_routes.get(case["name"]), "RETAINED_BOUNDARY_ROUTE_INVENTORY_DIFFERS")
        for read in case["results"].values():
            scope = ["ok", "recent.media", "counts.media"] if read["uri"].startswith("/api/workspace/home?") and read["status"] == 200 else None
            require(read.get("bodyComparisonScope") == scope, "RETAINED_BOUNDARY_COMPARISON_SCOPE_DIFFERS")
        result.append(dict(case, kind="boundaries", reads=list(case["results"].values()), unauthorizedBody=denied_body))
    require(len({case["name"] for case in result}) == 37
            and sum(len(case["reads"]) for case in result) == 152,
            "RETAINED_VALUE_SOCKET_INVENTORY_DIFFERS")
    for case in result:
        for read in case["reads"]:
            require(read["uri"].startswith(("/api/media", "/api/workspace/home?limit="))
                    and hashlib.sha256(read["bodyJson"].encode("utf8")).hexdigest() == read["bodySha256"]
                    and json_values_equal(json.loads(read["bodyJson"]), read["body"]),
                    "RETAINED_VALUE_ORIGINAL_RESPONSE_BINDING_DIFFERS")
    return result, {"retainedHttp": hashlib.sha256(retained_raw).hexdigest(),
                    "retainedBoundaries": hashlib.sha256(boundary_raw).hexdigest()}


def process_identity(pid: int) -> str | None:
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_bytes().rsplit(b")", 1)[1].split()
        return None if fields[0] in {b"Z", b"X"} else fields[19].decode("ascii")
    except (FileNotFoundError, ProcessLookupError):
        # A process exiting after procfs open makes the read fail with ESRCH.
        return None


def session_identities(session: int) -> dict[int, str]:
    result = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_bytes().rsplit(b")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[3]) == session and fields[0] not in {b"Z", b"X"}:
            result[int(path.name)] = fields[19].decode("ascii")
    return result


def session_group_alive(session: int) -> bool:
    # Signal zero only observes the dedicated launcher/guardian process group.
    query = getattr(os, "killpg", None)
    if not callable(query):
        raise ProbeFailure("NATIVE_MEDIA_SESSION_OBSERVATION_UNAVAILABLE")
    try:
        query(session, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop(child: subprocess.Popen[bytes], identities: list[dict[str, Any]]) -> None:
    tracked = {row["pid"]: row["startTicks"] for row in identities}
    discovery_error: Exception | None = None
    primary_error: BaseException | None = None

    def discover() -> None:
        nonlocal discovery_error
        try:
            tracked.update(session_identities(child.pid))
        except Exception as error:
            discovery_error = error

    discover()
    try:
        bootstrap.stop(child)
    except BaseException as error:
        primary_error = error
    finally:
        # The native Linux guardians own lifetime pipes, pidfds and subreapers.
        # Killing a guardian would abandon its escaped-session children. Observe
        # it until reaping completes, even if the launcher itself timed out.
        deadline = time.monotonic() + 10
        cleanup_error: Exception | None = None
        while True:
            discover()
            pending = False
            for pid, start in tracked.items():
                try:
                    pending |= process_identity(pid) == start
                except Exception as error:
                    discovery_error, pending = error, True
            try:
                pending |= session_group_alive(child.pid)
            except Exception as error:
                discovery_error, pending = error, True
            if not pending:
                break
            if time.monotonic() >= deadline:
                cleanup_error = ProbeFailure("OWNED_NATIVE_CHILD_SURVIVED_LAUNCHER_STOP")
                break
            time.sleep(0.05)
        if primary_error is not None:
            if cleanup_error is not None:
                raise primary_error from cleanup_error
            raise primary_error
        if cleanup_error is not None:
            raise cleanup_error
        if discovery_error is not None:
            raise discovery_error


def writer(profile: Path) -> tuple[int, str]:
    database = profile / "go-control/control.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT fencing_token,owner_instance_id FROM control_writer WHERE singleton=1").fetchone()
    require(row is not None, "NATIVE_CONTROL_WRITER_NOT_OBSERVED")
    return row


@contextmanager
def previous_corpus_deadline() -> Iterator[Callable[[], float]]:
    set_timer = getattr(signal, "setitimer", None)
    alarm_signal = getattr(signal, "SIGALRM", None)
    real_timer = getattr(signal, "ITIMER_REAL", None)
    if not callable(set_timer) or alarm_signal is None or real_timer is None:
        raise ProbeFailure("NATIVE_MEDIA_PHASE_DEADLINE_UNAVAILABLE")
    budget = 15 * 60
    started = time.monotonic()

    def expired(_signum: int, _frame: Any) -> None:
        raise ProbeFailure("EXISTING_MEDIA_READ_CORPUS_EXCEEDED_ORIGINAL_BUDGET")

    def finish() -> float:
        elapsed = time.monotonic() - started
        require(elapsed < budget, "EXISTING_MEDIA_READ_CORPUS_EXCEEDED_ORIGINAL_BUDGET")
        set_timer(real_timer, 0)
        return elapsed

    handler = signal.signal(alarm_signal, expired)
    try:
        set_timer(real_timer, budget)
        yield finish
    finally:
        set_timer(real_timer, 0)
        signal.signal(alarm_signal, handler)


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.name == "posix",
            "NATIVE_MEDIA_PROCESS_VERIFICATION_IS_CLOUD_ONLY")
    with previous_corpus_deadline() as finish_previous_corpus:
        run_corpus(args, report, finish_previous_corpus)


def run_corpus(args: argparse.Namespace, report: dict[str, Any], finish_previous_corpus: Callable[[], float]) -> None:
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

        def launch(label: str, *, check_startup_auth: bool = True) -> tuple[str, list[dict[str, Any]]]:
            nonlocal child, identities
            with (args.out / (label + ".log")).open("wb") as log:
                child = subprocess.Popen([str(binaries / "deepseek-launch"), "--server", "--no-prompt",
                    "--port", str(public_port)], cwd=scratch, env=launch_env, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
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
            if check_startup_auth:
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
            if getattr(args, "retained_segments", False):
                raw_segments = SEGMENT_FIXTURE.read_bytes()
                require(hashlib.sha256(raw_segments).hexdigest() == SEGMENT_FIXTURE_SHA256,
                        "RETAINED_SEGMENT_FIXTURE_CHANGED")
                segments = json.loads(raw_segments)
                require(segments["schemaVersion"] == 1 and segments["actualOriginalTCP"]
                        and segments["authEnabled"] and len(segments["cases"]) == 24 and segments["requests"] == 48,
                        "RETAINED_SEGMENT_FIXTURE_CONTRACT_DIFFERS")
                original_tree = subprocess.check_output(
                    ["git", "rev-parse", "HEAD:deepseek_infra"], cwd=ROOT, text=True).strip()
                require(original_tree == segments["originalPythonPackageTree"], "ORIGINAL_SEGMENT_PYTHON_PACKAGE_CHANGED")
                report["originalRetainedSegmentHTTP"] = original_segment_http(scratch / "segment-oracle", segments, args.out)
                segment_snapshot = seed_segment_fixture(profile, segments)
                report.update(offlineMediaFixtureSeeded=True, segmentFixtureSha256=SEGMENT_FIXTURE_SHA256,
                              originalSegmentBatchSha256=segments["originalBatchOracleSha256"],
                              originalSegmentSourceCommit=segments["originalSourceCommit"],
                              originalSegmentSourceTree=segments["originalSourceTree"])
                generations = [initial_writer]
                for label in ("segments-launch", "segments-restart"):
                    token, roles = launch(label)
                    require((profile / ".auth-token").read_bytes() == token_bytes, "SEGMENT_RESTART_REPLACED_AUTH")
                    observed = writer(profile)
                    require(observed[0] == generations[-1][0] + 1 and observed[1] != generations[-1][1],
                            "SEGMENT_RESTART_DID_NOT_ADVANCE_WRITER_FENCE")
                    generations.append(observed)
                    require(media_snapshot(profile) == segment_snapshot, "SEGMENT_START_CHANGED_SOURCE")
                    for case in segments["cases"]:
                        for method in ("GET", "HEAD"):
                            status, denied = request(origin, case["uri"], method, None)
                            require(status == 401, "SEGMENT_READ_AUTH_REFUSAL_DIFFERS_" + case["name"])
                            if method == "GET":
                                require(json_values_equal(denied, {"error": "Auth required", "code": "unauthorized"}),
                                        "SEGMENT_READ_AUTH_ERROR_BODY_DIFFERS_" + case["name"])
                    report[label] = {"processes": roles, "reads": check_reads(
                        origin, token, segments["cases"], segments["cases"], profile, exact_numbers=True)}
                    shutdown()
                    require(media_snapshot(profile) == segment_snapshot, "SEGMENT_SHUTDOWN_CHANGED_SOURCE")
                report.update(status="PASS", controlledRetainedSegmentReadsQualified=True,
                              segmentCases=24, segmentGETResponsesChecked=48, segmentHEADResponsesChecked=48,
                              segmentUnauthorizedResponsesChecked=96, retainedSegmentLaunchCount=2,
                              mediaStoreUnchangedAcrossReadsAndRestarts=True, defaultAuthRetainedAcrossRestarts=True,
                              unauthenticatedReadsDenied=True, ownedNativeChildrenStopped=True,
                              writerFencingTokens=[row[0] for row in generations], retainedMediaSnapshot=segment_snapshot)
                return
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
            unicode_path = ROOT / "tests/fixtures/native_media_discarded_unicode_v1.json"
            unicode_fixture = json.loads(unicode_path.read_text(encoding="utf-8"))
            unicode_cases = unicode_fixture["cases"]
            require(unicode_fixture["schemaVersion"] == 1 and len(unicode_cases) == 21,
                    "DISCARDED_UNICODE_ORIGINAL_FIXTURE_CONTRACT_DIFFERS")
            unicode_reads = [
                {"operation": "list", "uri": "/api/media"},
                {"operation": "get", "mediaId": "media_kept", "uri": "/api/media/media_kept"},
                {"operation": "list", "type": "image", "uri": "/api/media?type=image"},
            ]
            unicode_results = []
            for case in unicode_cases:
                # Provision the next original document only while all owned native children are stopped.
                require(child is None and not identities, "UNICODE_FIXTURE_WRITTEN_WHILE_NATIVE_RUNNING")
                source = case["document"].encode("utf-8")
                require(hashlib.sha256(source).hexdigest() == case["documentSha256"],
                        "DISCARDED_UNICODE_DOCUMENT_HASH_DIFFERS")
                (directory / "library.json").write_bytes(source)
                seeded = media_snapshot(profile)
                original = oracle(profile, unicode_reads)
                require(original[0] == {"status": 200, "body": {"ok": True, "media": case["publicRows"]}},
                        "DISCARDED_UNICODE_ORIGINAL_ORACLE_DRIFT")
                token, roles = launch("unicode-" + case["name"])
                require((profile / ".auth-token").read_bytes() == token_bytes, "UNICODE_RESTART_REPLACED_AUTH")
                observed = writer(profile)
                require(observed[0] == generations[-1][0] + 1 and observed[1] != generations[-1][1],
                        "UNICODE_RESTART_DID_NOT_ADVANCE_WRITER_FENCE")
                generations.append(observed)
                require(media_snapshot(profile) == seeded, "UNICODE_RESTART_CHANGED_MEDIA_STORE")
                reads = check_reads(origin, token, unicode_reads, original, profile)
                unicode_results.append({"case": case["name"], "documentSha256": case["documentSha256"],
                                        "reads": reads, "processes": roles})
                shutdown()
                require((profile / ".auth-token").read_bytes() == token_bytes, "UNICODE_SHUTDOWN_CHANGED_AUTH")
                require(media_snapshot(profile) == seeded, "UNICODE_SHUTDOWN_CHANGED_MEDIA_STORE")
            report.update(discardedUnicodeCases=unicode_results, discardedUnicodeCaseCount=len(unicode_results),
                discardedUnicodeHeadResponsesChecked=len(unicode_results) * len(unicode_reads),
                discardedUnicodeFixtureSha256=hashlib.sha256(unicode_path.read_bytes()).hexdigest(),
                discardedUnicodeAuthenticatedReadsMatchOriginal=True)
            selected_path = ROOT / "tests/fixtures/native_media_selected_normalization_v1.json"
            selected_fixture = json.loads(selected_path.read_text(encoding="utf8"))
            selected_variants = selected_fixture["variants"]
            require(selected_fixture["schemaVersion"] == 1 and len(selected_variants) == 2
                    and selected_fixture["actualOriginalLoopbackRequests"] == 13,
                    "SELECTED_NORMALIZATION_ORIGINAL_FIXTURE_CONTRACT_DIFFERS")
            selected_results = []
            for case in selected_variants:
                require(child is None and not identities, "SELECTED_FIXTURE_WRITTEN_WHILE_NATIVE_RUNNING")
                source = case["document"].encode("utf8")
                require(hashlib.sha256(source).hexdigest() == case["documentSha256"],
                        "SELECTED_NORMALIZATION_DOCUMENT_HASH_DIFFERS")
                (directory / "library.json").write_bytes(source)
                seeded = media_snapshot(profile)
                reads = [
                    {"operation": "list", "uri": "/api/media"},
                    {"operation": "get", "mediaId": "first", "uri": "/api/media/first"},
                    {"operation": "get", "mediaId": "later", "uri": "/api/media/later"},
                    {"operation": "list", "type": "pdf", "uri": "/api/media?type=pdf"},
                    {"operation": "segments", "mediaId": "later", "uri": "/api/media/later/segments"},
                ]
                selected_roles = ["fullList", "selectedFirst", "selectedLater", "emptyFilter", "segmentsLater"]
                if "selectedProject" in case["results"]:
                    reads.append({"operation": "list", "projectId": "proj_first", "uri": "/api/media?projectId=proj_first"})
                    selected_roles.append("selectedProject")
                original = oracle(profile, reads)
                for expected_result, role in zip(original, selected_roles, strict=True):
                    frozen = case["results"][role]
                    require(json_values_equal(expected_result, {"status": frozen["status"], "body": frozen["body"]}),
                            "SELECTED_NORMALIZATION_ORIGINAL_ORACLE_DRIFT")
                token, roles_observed = launch("selected-" + case["name"])
                require((profile / ".auth-token").read_bytes() == token_bytes, "SELECTED_RESTART_REPLACED_AUTH")
                observed = writer(profile)
                require(observed[0] == generations[-1][0] + 1 and observed[1] != generations[-1][1],
                        "SELECTED_RESTART_DID_NOT_ADVANCE_WRITER_FENCE")
                generations.append(observed)
                require(media_snapshot(profile) == seeded, "SELECTED_RESTART_CHANGED_MEDIA_STORE")
                for read in reads:
                    for method in ["GET", "HEAD"]:
                        status, denied_body = request(origin, read["uri"], method, None)
                        require(status == 401, "SELECTED_UNAUTHENTICATED_READ_NOT_DENIED")
                        if method == "GET":
                            require(json_values_equal(denied_body, case["results"]["unauthorized"]["body"]),
                                    "SELECTED_AUTH_REFUSAL_BODY_DIFFERS_FROM_ORIGINAL")
                checked = check_reads(origin, token, reads, original, profile)
                selected_results.append(dict(case=case["name"], documentSha256=case["documentSha256"],
                                             reads=checked, processes=roles_observed))
                shutdown()
                require((profile / ".auth-token").read_bytes() == token_bytes, "SELECTED_SHUTDOWN_CHANGED_AUTH")
                require(media_snapshot(profile) == seeded, "SELECTED_SHUTDOWN_CHANGED_MEDIA_STORE")
            selected_count = sum(len(case["reads"]) for case in selected_results)
            require(selected_count == 11, "SELECTED_NORMALIZATION_READ_COUNT_DIFFERS")
            report.update(selectedNormalizationCases=selected_results, selectedNormalizationCaseCount=2,
                selectedNormalizationGETResponsesChecked=selected_count,
                selectedNormalizationHEADResponsesChecked=selected_count,
                selectedNormalizationUnauthorizedResponsesChecked=selected_count * 2,
                selectedNormalizationFixtureSha256=hashlib.sha256(selected_path.read_bytes()).hexdigest(),
                selectedNormalizationAuthenticatedReadsMatchOriginal=True)
            report["previousMediaReadCorpusSeconds"] = finish_previous_corpus()
            retained_values, fixture_hashes = frozen_retained_cases()
            retained_value_results = []
            for case in retained_values:
                require(child is None and not identities, "RETAINED_VALUE_FIXTURE_WRITTEN_WHILE_NATIVE_RUNNING")
                raw = case["document"].encode("utf8")
                require(hashlib.sha256(raw).hexdigest() == case["documentSha256"],
                        "RETAINED_VALUE_DOCUMENT_HASH_DIFFERS")
                (directory / "library.json").write_bytes(raw)
                seeded = media_snapshot(profile)
                # Each new case checks its own complete auth matrix below.
                token, roles_observed = launch("retained-value-" + case["name"], check_startup_auth=False)
                require((profile / ".auth-token").read_bytes() == token_bytes, "RETAINED_VALUE_RESTART_REPLACED_AUTH")
                observed = writer(profile)
                require(observed[0] == generations[-1][0] + 1 and observed[1] not in {item[1] for item in generations},
                        "RETAINED_VALUE_RESTART_DID_NOT_ADVANCE_WRITER_FENCE")
                generations.append(observed)
                require(media_snapshot(profile) == seeded, "RETAINED_VALUE_RESTART_CHANGED_MEDIA_STORE")
                reads = []
                for frozen in case["reads"]:
                    uri = frozen["uri"]
                    compare_home = bool(frozen.get("bodyComparisonScope"))
                    reads.append({"uri": uri, "compareHomeMedia": compare_home})
                    for method in ("GET", "HEAD"):
                        denied, body = request(origin, uri, method, None)
                        require(denied == 401, "RETAINED_VALUE_UNAUTHENTICATED_READ_NOT_DENIED:" + uri)
                        if method == "GET":
                            require(json_values_equal(body, case["unauthorizedBody"]), "RETAINED_VALUE_AUTH_BODY_DIFFERS")
                        require(media_snapshot(profile) == seeded and (profile / ".auth-token").read_bytes() == token_bytes,
                                "RETAINED_VALUE_AUTH_REFUSAL_CHANGED_SOURCE")
                checked = check_reads(origin, token, reads, case["reads"], profile, exact_numbers=True)
                retained_value_results.append(dict(case=case["name"], kind=case["kind"],
                    documentSha256=case["documentSha256"], reads=checked, processes=roles_observed))
                shutdown()
                require(media_snapshot(profile) == seeded and (profile / ".auth-token").read_bytes() == token_bytes,
                        "RETAINED_VALUE_SHUTDOWN_CHANGED_SOURCE")
            retained_value_count = sum(len(case["reads"]) for case in retained_value_results)
            require(retained_value_count == 152 and len(generations) == 63, "RETAINED_VALUE_SOCKET_OBSERVATION_COUNT_DIFFERS")
            report.update(retainedValueCases=retained_value_results, retainedValueCaseCount=37,
                retainedValueGETResponsesChecked=retained_value_count, retainedValueHEADResponsesChecked=retained_value_count,
                retainedValueUnauthorizedResponsesChecked=retained_value_count * 2, retainedValueFixtureSha256=fixture_hashes,
                retainedValueAuthenticatedReadsMatchOriginal=True)
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
    parser.add_argument("--retained-segments", action="store_true", help="Replay original retained segment reads and restart")
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"kind": "native-media-library-read-process-v1", "status": "FAIL",
        "sourceCommit": os.environ.get("GITHUB_SHA"), "runId": os.environ.get("GITHUB_RUN_ID"),
        "event": os.environ.get("GITHUB_EVENT_NAME"), "controlledAuthenticatedMediaReadsQualified": False,
        "controlledRetainedSegmentReadsQualified": False, "retainedSegmentsOnly": args.retained_segments,
        "offlineMediaFixtureSeeded": False, "mediaWritesOrProcessingQualified": False,
        "mediaMigrationQualified": False, "mediaGUIQualified": False,
        "fullMigrationQualified": False, "readiness": "NOT_READY"}
    try:
        run(args, report)
    except Exception as error:
        report.update(status="FAIL", controlledAuthenticatedMediaReadsQualified=False, controlledRetainedSegmentReadsQualified=False,
            failureCode=str(error) if isinstance(error, (ProbeFailure, bootstrap.ProbeFailure)) else type(error).__name__)
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "sourceCommit", "failureCode")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
