"""Real native HTTP registration against an independent original ASGI oracle.

This initial creation workload retains the full create/upload/process migration
scope; it qualifies only the executed JSON webpage case, never the entire API.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import verify_native_media_patch as patch

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 32 * 1024**2
REQUEST_ID = "native-media-create-baseline"
PAYLOAD: dict[str, Any] = dict(type="webpage", mimeType="text/html", title="Retained creation", projectId="project-oracle",
    url="https://example.invalid/not-fetched", html="<h1>Retained snapshot</h1>", text="Original webpage text",
    metadata={"a?": "first", "a!": "last", "unicode": "原生迁移", "zero": 0})
ORACLE = r'''
import hashlib,json,sys
from pathlib import Path
from fastapi.testclient import TestClient
from deepseek_infra.web.server import create_app,settings
from deepseek_infra.infra.media import library
client=TestClient(create_app(),base_url="http://127.0.0.1")
try:
    response=client.post("/api/media",content=sys.stdin.buffer.read(),headers={"Authorization":"Bearer "+settings.auth.token,"Content-Type":"application/json","X-DeepSeek-Request-ID":"native-media-create-baseline"})
    body=response.json()
    source=library.media_dir()/body["media"]["path"] if response.status_code==200 and body["media"]["path"] else None
    print(json.dumps(dict(status=response.status_code,body=body,requestId=response.headers.get("X-DeepSeek-Request-ID"),sourceSha256=hashlib.sha256(source.read_bytes()).hexdigest() if source else None)))
finally:
    client.close()
'''


class ProbeFailure(RuntimeError):
    pass


def require(value: Any, code: str) -> None:
    if not value:
        raise ProbeFailure(code)


def same_json(left: Any, right: Any) -> bool:
    """JSON scalar types are part of parity; False must never equal zero."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return bool(left == right)


def canonical_media(record: dict[str, Any], source_suffix: str = ".html") -> dict[str, Any]:
    result = json.loads(json.dumps(record))
    media_id = result.pop("mediaId", None)
    require(isinstance(media_id, str) and re.fullmatch(r"media_[0-9a-f]{16}", media_id), "MEDIA_CREATE_ID_INVALID")
    path = result.get("path")
    require(path == (f"objects/{media_id}/source{source_suffix}" if source_suffix else ""), "MEDIA_CREATE_SOURCE_PATH_INVALID")
    result["path"] = "objects/{mediaId}/source" + source_suffix if source_suffix else ""
    for field in ("createdAt", "updatedAt"):
        value = result.pop(field, None)
        require(isinstance(value, str) and bool(value), "MEDIA_CREATE_TIME_MISSING")
    return result


def require_creation_time(record: dict[str, Any], before: datetime, after: datetime) -> None:
    require(record.get("createdAt") == record.get("updatedAt"), "MEDIA_CREATE_TIME_DIFFERS")
    try:
        observed = datetime.fromisoformat(str(record["createdAt"]).replace("Z", "+00:00"))
    except (ValueError, KeyError) as error:
        raise ProbeFailure("MEDIA_CREATE_TIME_INVALID") from error
    require(observed.tzinfo is not None and before.timestamp() - 1 <= observed.timestamp() <= after.timestamp() + 1,
        "MEDIA_CREATE_TIME_NOT_CAUSAL")


def original_observation(profile: Path, diagnostic: Path, payload: dict[str, Any] | None = None, source_suffix: str = ".html") -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR",
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP"}}
    env.update(DEEPSEEK_INFRA_ROOT=str(profile), LOCAL_RAG_ENABLED="false", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-B", "-c", ORACLE], cwd=ROOT, env=env,
        input=json.dumps(PAYLOAD if payload is None else payload, ensure_ascii=False).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    require(len(result.stderr) <= LIMIT, "MEDIA_CREATE_ORACLE_DIAGNOSTIC_BOUND")
    diagnostic.write_bytes(result.stderr)
    require(result.returncode == 0 and len(result.stdout) <= LIMIT, "MEDIA_CREATE_ORIGINAL_ORACLE_FAILED")
    value = json.loads(result.stdout)
    require(value["status"] == 200 and value["body"]["ok"] is True and value["requestId"] == REQUEST_ID,
        "MEDIA_CREATE_ORIGINAL_ROUTE_DID_NOT_SUCCEED")
    canonical_media(value["body"]["media"], source_suffix)
    if payload is None:
        require(value["sourceSha256"] == hashlib.sha256(PAYLOAD["html"].encode()).hexdigest(), "MEDIA_CREATE_ORIGINAL_SOURCE_DIFFERS")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def exchange(origin: str, path: str, method: str, token: str | None, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any], dict[str, str]]:
    address = urlsplit(origin)
    require(address.scheme == "http" and address.hostname == "127.0.0.1" and address.username is None, "MEDIA_CREATE_ORIGIN_INVALID")
    require(path.startswith("/api/media"), "MEDIA_CREATE_REQUEST_PATH_INVALID")
    headers = {"Content-Type": "application/json", "X-DeepSeek-Request-ID": REQUEST_ID}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    try:
        response = build_opener(NoRedirect()).open(Request(origin + path, method=method, headers=headers, data=data), timeout=30)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(LIMIT + 1)
        require(len(raw) <= LIMIT, "MEDIA_CREATE_RESPONSE_BOUND")
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeError):
            parsed = None
        body = parsed if isinstance(parsed, dict) else dict(nonObjectJSONResponse=True,
            responseBytes=len(raw), bodySha256=hashlib.sha256(raw).hexdigest(), sample=raw[:512].decode(errors="replace"))
        return response.status, body, {key.lower(): value for key, value in response.headers.items()}


def observation_counts(report: dict[str, Any], observed: int) -> dict[str, int]:
    json_cases = 1 + len(report["jsonRegistrationCases"])
    uploaded = int(report.get("actualMultipartStoredRecords", 0))
    require(observed == json_cases + uploaded, "MEDIA_CREATE_OBSERVATION_COUNTS_DIFFER")
    return dict(actualJSONRegistrationCases=json_cases, actualRetainedMediaObservations=observed)


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    require(os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux", "MEDIA_CREATE_VERIFICATION_IS_CLOUD_ONLY")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(revision == os.environ.get("GITHUB_SHA"), "MEDIA_CREATE_SOURCE_DIFFERS")
    require(not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT), "MEDIA_CREATE_SOURCE_IS_DIRTY")
    report["sourceCommit"] = revision
    with tempfile.TemporaryDirectory(prefix="native-media-create-") as temporary:
        scratch = Path(temporary)
        binaries, profile, mirror = scratch / "bin", scratch / "profile", scratch / "oracle"
        binaries.mkdir()
        (scratch / "go-tmp").mkdir()
        env = patch.signing.cache_environment(ROOT, env=patch.signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        env.update(CGO_ENABLED="0", GOTOOLCHAIN="local", GOMAXPROCS="2", GOPROXY="off", GOSUMDB="off",
            GOWORK="off", GOCACHE=str(args.go_cache.resolve()), GOTMPDIR=str(scratch / "go-tmp"))
        require(subprocess.check_output(["go", "version"], env=env, text=True).startswith(
            "go version " + patch.signing.GO_VERSION + " "), "MEDIA_CREATE_GO_PIN_DIFFERS")
        for name in ("deepseekd", "deepseek-launch"):
            patch.signing.record_command(["go", "build", "-p=2", "-o", str(binaries / name), "./cmd/" + name],
                ROOT / "go", env, args.out, "build-" + name, timeout=600)
        for name, source in (("deepseek-worker", args.worker), ("deepseek-gateway", args.edge)):
            (binaries / name).symlink_to(source.resolve(strict=True))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        launch_env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
        launch_env.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile),
            DEEPSEEK_INFRA_STATIC_DIR=str(ROOT / "static"), LOCAL_RAG_ENABLED="false")
        if getattr(args, "verify_processing", False):
            launch_env.update(LOCAL_RAG_ENABLED="true", LOCAL_RAG_BACKEND="sqlite")
        child: subprocess.Popen[bytes] | None = None
        identities: list[dict[str, Any]] = []

        def launch(label: str) -> str:
            nonlocal child, identities
            with (args.out / (label + ".log")).open("wb") as log:
                child = subprocess.Popen([str(binaries / "deepseek-launch"), "--server", "--no-prompt", "--port", origin.rsplit(":", 1)[1]],
                    cwd=scratch, env=launch_env, stdout=log, stderr=subprocess.STDOUT)
            patch.bootstrap.wait_public(origin, child)
            identities = [{**row, "startTicks": patch.reads.process_identity(row["pid"])} for row in patch.bootstrap.process_roles(child.pid)]
            require(all(row["startTicks"] is not None for row in identities), "MEDIA_CREATE_PROCESS_DISAPPEARED")
            names = {row["executable"] for row in identities}
            require({"deepseek-launch", "deepseekd", "deepseek-worker", "deepseek-gateway"} <= names, "MEDIA_CREATE_NATIVE_ROLES_MISSING")
            require(not any(name.startswith(("python", "node")) for name in names), "MEDIA_CREATE_NON_NATIVE_PROCESS")
            report[label + "Processes"] = identities
            return (profile / ".auth-token").read_text().strip()

        def stop() -> None:
            nonlocal child, identities
            if child is not None:
                patch.reads.stop(child, identities)
                child, identities = None, []

        try:
            oracle = original_observation(mirror, args.out / "original-asgi.log")
            report.update(originalOracle=oracle, actualOriginalASGIRouteCompared=True)
            token = launch("create-launch")
            require(not (profile / ".media").exists(), "MEDIA_CREATE_FRESH_PROFILE_HAS_MEDIA")
            generation = patch.generation_observation(profile)
            unauthenticated, _, _ = exchange(origin, "/api/media", "POST", None, PAYLOAD)
            require(not (profile / ".media").exists() and patch.generation_observation(profile) == generation, "MEDIA_CREATE_AUTH_REFUSAL_CHANGED_DATA")
            before = datetime.now(timezone.utc)
            status, body, response_headers = exchange(origin, "/api/media", "POST", token, PAYLOAD)
            after = datetime.now(timezone.utc)
            report.update(semanticStageReached=True, nativeCreateHTTPStatus=status, nativeCreateResponse=body, unauthenticatedStatus=unauthenticated)
            require(status == 200, "NATIVE_MEDIA_CREATE_PUBLIC_ROUTE_MISSING" if status in {404, 405, 501} else "MEDIA_CREATE_PUBLIC_RESULT_DIFFERS")
            require(unauthenticated == 401 and body.get("ok") is True, "MEDIA_CREATE_AUTH_OR_SUCCESS_DIFFERS")
            require(response_headers.get("x-deepseek-request-id") == REQUEST_ID, "MEDIA_CREATE_CORRELATION_ID_DIFFERS")
            record = body["media"]
            require(same_json(canonical_media(record), canonical_media(oracle["body"]["media"])), "MEDIA_CREATE_RECORD_DIFFERS")
            require_creation_time(record, before, after)
            source = profile / ".media" / record["path"]
            require(source.resolve(strict=True).is_relative_to((profile / ".media").resolve()), "MEDIA_CREATE_SOURCE_ESCAPED_PROFILE")
            require(hashlib.sha256(source.read_bytes()).hexdigest() == oracle["sourceSha256"], "MEDIA_CREATE_SOURCE_BYTES_DIFFER")
            read_status, retained, _ = exchange(origin, "/api/media/" + record["mediaId"], "GET", token)
            require(read_status == 200 and same_json(retained, body), "MEDIA_CREATE_PUBLIC_READ_DIFFERS")
            observations = [(record["mediaId"], body, oracle["sourceSha256"])]
            cases: list[tuple[str, dict[str, Any], str]] = [
                (kind, dict(type=kind, mimeType=mime, title="Native " + kind, metadata=dict(zero=0, disabled=False)), "")
                for kind, mime in [("image", "image/png"), ("pdf", "application/pdf"), ("audio", "audio/mpeg"),
                    ("video", "video/mp4"), ("webpage", "text/html"), ("screenshot", "image/png")]
            ]
            cases.extend([
                ("audio-text", dict(type="audio", mimeType="audio/mpeg", text="  imported transcript\n"), ".txt"),
                ("video-transcript", dict(type="video", mimeType="video/mp4", transcript="retained transcript"), ".txt"),
                ("image-text", dict(type="image", mimeType="image/png", text="retained image text"), ".txt"),
                ("large-record", dict(type="pdf", mimeType="application/pdf", metadata={f"field{i:03}": "z" * 9000 for i in range(500)}), ""),
            ])
            report["jsonRegistrationCases"] = []
            for label, payload, suffix in cases:
                reference = original_observation(mirror / label, args.out / ("oracle-" + label + ".log"), payload, suffix)
                started = datetime.now(timezone.utc)
                actual_status, actual_body, actual_headers = exchange(origin, "/api/media", "POST", token, payload)
                completed = datetime.now(timezone.utc)
                require(actual_status == reference["status"] == 200 and actual_body.get("ok") is True, "MEDIA_CREATE_CASE_FAILED:" + label)
                require(actual_headers.get("x-deepseek-request-id") == REQUEST_ID, "MEDIA_CREATE_CASE_CORRELATION_DIFFERS:" + label)
                expected_record = canonical_media(reference["body"]["media"], suffix)
                actual_record = canonical_media(actual_body["media"], suffix)
                require(same_json(actual_record, expected_record), "MEDIA_CREATE_CASE_RECORD_DIFFERS:" + label)
                require_creation_time(actual_body["media"], started, completed)
                actual_file = profile / ".media" / actual_body["media"]["path"] if suffix else None
                actual_sha = hashlib.sha256(actual_file.read_bytes()).hexdigest() if actual_file else None
                require(actual_sha == reference["sourceSha256"], "MEDIA_CREATE_CASE_SOURCE_DIFFERS:" + label)
                encoded = json.dumps(actual_record, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
                report["jsonRegistrationCases"].append(dict(case=label, status=actual_status, mediaId=actual_body["media"]["mediaId"],
                    canonicalRecordSha256=hashlib.sha256(encoded).hexdigest(), publicRecordBytes=len(encoded), sourceSha256=actual_sha))
                observations.append((actual_body["media"]["mediaId"], actual_body, actual_sha))
            require(report["jsonRegistrationCases"][-1]["publicRecordBytes"] > 4 * 1024**2, "MEDIA_CREATE_LARGE_RECORD_NOT_EXERCISED")
            if getattr(args, "verify_uploads", False):
                import verify_native_media_upload as upload
                observations.extend(upload.verify_cases(origin, token, profile, mirror / "uploads", args.out, report))
            library = profile / ".media/library.json"
            published = library.read_bytes()
            stop()
            token = launch("retained-launch")
            read_status, retained, _ = exchange(origin, "/api/media/" + record["mediaId"], "GET", token)
            require(read_status == 200 and same_json(retained, body) and library.read_bytes() == published, "MEDIA_CREATE_RESTART_DIFFERS")
            require(hashlib.sha256(source.read_bytes()).hexdigest() == oracle["sourceSha256"], "MEDIA_CREATE_RESTART_SOURCE_DIFFERS")
            for media_id, original_body, original_sha in observations:
                read_status, retained_body, _ = exchange(origin, "/api/media/" + media_id, "GET", token)
                require(read_status == 200 and same_json(retained_body, original_body), "MEDIA_CREATE_CASE_RESTART_DIFFERS")
                if original_sha is not None:
                    retained_file = profile / ".media" / original_body["media"]["path"]
                    require(hashlib.sha256(retained_file.read_bytes()).hexdigest() == original_sha, "MEDIA_CREATE_CASE_RESTART_SOURCE_DIFFERS")
            report.update(actualNativeJSONWebpageCreateQualified=True, publicReadAndRestartRetained=True,
                allSixJSONMediaKindsQualified=True, largeJSONRecordBeyondControlRowQualified=True,
                **observation_counts(report, len(observations)))
            if getattr(args, "verify_processing", False):
                import verify_native_media_processing as processing
                processing.verify(origin, token, profile, mirror / "processing", args.out, report)
            report["status"] = "PASS"
        finally:
            stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--edge", type=Path, required=True)
    parser.add_argument("--go-cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify-uploads", action="store_true", help="also verify original multipart ingestion boundaries")
    parser.add_argument("--verify-processing", action="store_true", help="require real processing/index/force parity after retained creation checks")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = dict(kind="native-media-create-process-v1", status="FAIL", sourceCommit=os.environ.get("GITHUB_SHA"),
        runId=os.environ.get("GITHUB_RUN_ID"), event=os.environ.get("GITHUB_EVENT_NAME"), semanticStageReached=False,
        actualNativeJSONWebpageCreateQualified=False, allMediaCreateFormsQualified=False, uploadParityQualified=False,
        processPipelineQualified=False, authorityRecoveryQualified=False, fullMigrationQualified=False, readiness="NOT_READY")
    import verify_native_media_upload as upload
    try:
        run(args, report)
    except (ProbeFailure, upload.create.ProbeFailure, patch.ProbeFailure, patch.bootstrap.ProbeFailure, patch.reads.ProbeFailure) as error:
        report.update(failureCode=str(error))
    except Exception as error:
        report.update(failureCode="MEDIA_CREATE_VERIFIER_ERROR", exceptionType=type(error).__name__)
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
