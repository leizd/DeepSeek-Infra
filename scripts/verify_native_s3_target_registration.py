"""Exercise the native S3 registration path against an isolated MinIO provider."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
import uuid

import boto3
from botocore.config import Config
from playwright.sync_api import sync_playwright

from verify_native_default_bootstrap import ROOT, process_roles, require_loopback_ports_available, stop, wait_public
from verify_native_target_registration_browser import TARGETS, TargetCheckFailed, json_response, open_automatic_tab, require

sys.path.insert(0, str(ROOT / "tests"))
from real_storage_environment import _DockerMinioInstance, _free_ports  # noqa: E402


def observed_objects(client: Any, bucket: str, keys: tuple[str, str]) -> list[tuple[bytes, str, str]]:
    observations = []
    for key in keys:
        result = client.get_object(Bucket=bucket, Key=key)
        with closing(result["Body"]) as stream:
            raw = stream.read(16 * 1024 + 1)
        require(0 < len(raw) <= 16 * 1024 and result["VersionId"] not in ("", "null"), "VERSIONED_FIXTURE_OBJECT")
        observations.append((raw, result["ETag"], result["VersionId"]))
    return observations


def registration_evidence(profile: Path, op: str, target: str, raw: bytes, provider: list[tuple[bytes, str, str]]) -> dict[str, Any]:
    database = profile / "go-control/control.sqlite3"
    manifest = json.loads((profile / "rust-worker/custody/manifest.json").read_bytes())
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("""
            SELECT r.target_id,r.revision,r.public_generation,r.observation,
                   c.context_json,m.action_id,m.execution_epoch,m.writer_fencing_token
            FROM control_target_registrations r
            JOIN control_target_registration_s3 c USING(operation_id)
            JOIN control_operator_mutations m USING(operation_id)
            WHERE r.operation_id=?
        """, (op,)).fetchone()
        custody = db.execute("SELECT signer_public_key,fleet_id,environment FROM control_target_registration_identity").fetchone()
        writer = db.execute("SELECT fencing_token FROM control_writer WHERE singleton=1").fetchone()
        event_counts = tuple(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in
                             ("targets", "control_target_registration_s3", "control_operator_mutations"))
    require(row is not None and row[:3] == (target, 1, 1) and row[5] == op and row[6] == 1, "BOUND_GO_REGISTRATION")
    require(writer is not None and event_counts == (1, 1, 1), "ONE_REGISTRY_MUTATION")
    require(custody == (manifest["signer_public_key"], manifest["fleet_id"], manifest["environment"]), "RUST_SIGNER_CUSTODY")
    require(json.loads(row[4])["BodySHA256"] == hashlib.sha256(raw).hexdigest(), "ORIGINAL_REQUEST_DIGEST")
    require(all(data in row[3] and etag.encode() in row[3] and version.encode() in row[3]
                for data, etag, version in provider), "SIGNED_PROVIDER_BYTES_ETAGS_VERSIONS")
    return dict(writer=writer[0], originalWriter=row[7], witnessSHA256=hashlib.sha256(row[3]).hexdigest(),
                providerObjectSHA256=[hashlib.sha256(v[0]).hexdigest() for v in provider])


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    require(os.name == "posix" and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_REPOSITORY") == "leizd/DeepSeek-Infra", "HOSTED_CLOUD_ONLY")
    sha, tree = (subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, text=True).strip()
                 for ref in ("HEAD", "HEAD^{tree}"))
    require(sha == os.environ.get("GITHUB_SHA") and not subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=ROOT), "VERIFIED_SOURCE_CHECKOUT")
    report.update(sourceCommit=sha, sourceTree=tree)
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve()
    binaries = args.bin_dir.resolve()
    require(binaries.is_relative_to(runner_temp) and all((binaries / name).is_file() for name in
            ("deepseek-launch", "deepseekd", "deepseek-worker", "deepseek-gateway")), "NATIVE_BINARIES")
    docker = shutil.which("docker")
    if docker is None:
        raise TargetCheckFailed("FIXTURE_DOCKER")
    require_loopback_ports_available()
    provider_port, application_port = _free_ports(2)
    endpoint, origin = f"http://127.0.0.1:{provider_port}", f"http://127.0.0.1:{application_port}"
    suffix = uuid.uuid4().hex
    bucket, prefix = f"native-target-{suffix}", "verification/retained"
    target, label = "target_retained_s3_fixture", "Isolated retained S3"
    instance = _DockerMinioInstance(docker, endpoint, "native-target-minio-" + suffix,
                                  "nativeverification", uuid.uuid4().hex)
    report["stage"] = "provider-start"
    try:
        instance.create()
        client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                              aws_access_key_id=instance.access_key, aws_secret_access_key=instance.secret_key,
                              config=Config(retries={"max_attempts": 0}, proxies={}, s3={"addressing_style": "path"}))
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        keys = (prefix + "/control/identity.json", prefix + "/control/head.json")
        identity = {"schemaVersion": 3, "targetId": target, "kind": "s3", "bucket": bucket, "prefix": prefix,
                    "incarnationId": "inc_fixture", "ownerInstallationId": "inst_fixture", "createdAt": "2026-01-01T00:00:00Z"}
        head = {"schemaVersion": 1, "targetGeneration": 0, "latestCommitHash": "0" * 64, "incarnationId": "inc_fixture"}
        raw = json.dumps({"kind": "s3", "endpointUrl": endpoint, "region": "us-east-1", "bucket": bucket,
                          "prefix": prefix, "probe": False, "label": label}, separators=(",", ":")).encode()
        content_type = {"Content-Type": "application/json"}
        with tempfile.TemporaryDirectory(dir=runner_temp, prefix="native-s3-registration-") as directory, ExitStack() as children:
            root = Path(directory).resolve()
            profile, runtime_temp = root / "profile", root / "runtime-temp"
            runtime_temp.mkdir()
            environment = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")}
            environment.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile),
                               DEEPSEEK_INFRA_STATIC_DIR=str(ROOT / "static"), TMPDIR=str(runtime_temp),
                               DEEPSEEK_WORKER_S3_ENDPOINT=endpoint, DEEPSEEK_WORKER_S3_REGION="us-east-1",
                               DEEPSEEK_WORKER_S3_BUCKET=bucket, DEEPSEEK_WORKER_S3_PREFIX=prefix,
                               DEEPSEEK_WORKER_S3_ACCESS_KEY=instance.access_key, DEEPSEEK_WORKER_S3_SECRET_KEY=instance.secret_key,
                               DEEPSEEK_WORKER_S3_ALLOW_HTTP_LOOPBACK="true")

            def start() -> subprocess.Popen[bytes]:
                child = subprocess.Popen([str(binaries / "deepseek-launch"), "--server", "--no-prompt",
                                          "--port", str(application_port)], env=environment, cwd=root,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                children.callback(stop, child)
                wait_public(origin, child)
                return child

            report["stage"] = "first-launch"
            process = start()
            report["firstProcesses"] = process_roles(process.pid)
            token = (profile / ".auth-token").read_text(encoding="utf8").strip()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    context = browser.new_context(service_workers="block")
                    _, initial = open_automatic_tab(context, origin, token)
                    require(initial.get("targets") == [] and initial.get("health") == [], "INITIAL_REGISTRY")
                    report["stage"] = "absent-and-partial-layout"
                    for key, fixture in zip(keys, (identity, head), strict=True):
                        denied = context.request.post(origin + TARGETS, data=raw, headers=content_type)
                        require(json_response(denied, 503, "PREEXISTING_LAYOUT").get("code") ==
                                "S3_BACKUP_TARGET_LAYOUT_INCOMPLETE", "INCOMPLETE_LAYOUT_REFUSAL")
                        client.put_object(Bucket=bucket, Key=key, Body=(json.dumps(fixture, indent=2) + "\n").encode())
                    provider = observed_objects(client, bucket, keys)
                    report["stage"] = "s3-public-registration"
                    response = context.request.post(origin + TARGETS, data=raw, headers=content_type)
                    record = json_response(response, 200, "S3_REGISTRATION")
                    operation = response.headers.get("x-deepseek-operation-id") or ""
                    require(bool(operation) and record.get("targetId") == target and record.get("kind") == "s3"
                            and record.get("lastProbe") is None and record.get("topologyGeneration") == 1, "S3_PUBLIC_RECORD")
                    original = response.body()
                    before = registration_evidence(profile, operation, target, raw, provider)
                    versions = client.list_object_versions(Bucket=bucket, Prefix=prefix + "/").get("Versions", [])
                    require(len(versions) == 2 and {v["Key"] for v in versions} == set(keys)
                            and observed_objects(client, bucket, keys) == provider, "PROVIDER_OBJECTS_UNCHANGED")
                    context.close()
                    stop(process)
                    instance.stop()
                    report["stage"] = "replay-with-provider-offline"
                    process = start()
                    report["secondProcesses"] = process_roles(process.pid)
                    context = browser.new_context(service_workers="block")
                    page, listed = open_automatic_tab(context, origin, token)
                    page.get_by_text(label, exact=True).wait_for()
                    require(listed.get("targets") == [record] and listed.get("health") == [], "S3_VISIBLE_AFTER_RESTART")
                    replay = context.request.post(origin + TARGETS, data=raw, headers={
                        **content_type, "X-DeepSeek-Operation-Id": operation})
                    require(replay.status == 200 and replay.body() == original and
                            replay.headers.get("x-deepseek-operation-id") == operation, "ORIGINAL_REPLAY_OFFLINE")
                    conflict = context.request.post(origin + TARGETS, data=raw + b" ", headers={
                        **content_type, "X-DeepSeek-Operation-Id": operation})
                    require(json_response(conflict, 409, "S3_REPLAY_CONFLICT").get("code") ==
                            "mutation_request_replay_conflict", "S3_ORIGINAL_REQUEST_SCOPE")
                    after = registration_evidence(profile, operation, target, raw, provider)
                    require(after == {**before, "writer": before["writer"] + 1}, "S3_REPLAY_RETAINS_WITNESS")
                finally:
                    browser.close()
    finally:
        instance.close()
    report.update(status="PASS", stage="completed", chromium=True, realVersionedMinIO=True,
                  providerAbsentAndPartialRefused=True, providerBytesETagsVersionsRetained=True,
                  registryVisibleAfterRestart=True, replayedWhileProviderOffline=True, registration=after)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    options = parser.parse_args()
    report: dict[str, Any] = dict(kind="native-existing-s3-target-public-v1", status="FAIL", stage="source",
                                  runId=os.environ.get("GITHUB_RUN_ID"), event=os.environ.get("GITHUB_EVENT_NAME"))
    try:
        run(options, report)
    except TargetCheckFailed as exc:
        report["failure"] = str(exc)
    except Exception as exc:
        report["failure"] = type(exc).__name__
    options.out.parent.mkdir(parents=True, exist_ok=True)
    options.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(json.dumps({key: report.get(key) for key in ("status", "stage", "failure", "sourceCommit")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
