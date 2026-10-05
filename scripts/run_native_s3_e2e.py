#!/usr/bin/env python3
"""Provision real MinIO for Rust bytes and Go control tests; Python only sets up providers."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import uuid

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from real_storage_environment import ENDPOINT_NAMES, RealStorageEnvironment  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", help="rustup toolchain override (without the leading +)")
    options = parser.parse_args()
    cargo = ["cargo", *([f"+{options.toolchain}"] if options.toolchain else [])]
    command1 = [*cargo, "test", "--locked", "-p", "deepseek-storage", "--features", "s3-e2e", "--test", "s3_provider"]
    command2 = [*cargo, "test", "--locked", "-p", "deepseek-worker", "--features", "s3-e2e", "--test", "authorized_storage_provider"]
    command3 = [
        "go", "test", "-tags=integration", "-run", "^TestRustWorkerPromotedControlWritesAndRecoversRealProviders$",
        "-count=1", "-v", "-timeout=3m", "./internal/worker",
    ]
    with tempfile.TemporaryDirectory(prefix="deepseek-native-s3-") as directory:
        # Keep the actual default production binary separate from test features.
        # Build before provisioning: provider lifetime is bounded by the tests.
        subprocess.run([*cargo, "build", "--locked", "-p", "deepseek-worker"], cwd=ROOT / "rust", check=True, timeout=1200)
        metadata = json.loads(subprocess.run(
            [*cargo, "metadata", "--locked", "--no-deps", "--format-version=1"],
            cwd=ROOT / "rust", check=True, capture_output=True, text=True, timeout=60,
        ).stdout)
        suffix = ".exe" if os.name == "nt" else ""
        build_directory = Path(metadata["target_directory"]) / "debug"
        # Keep execution in the build tree, including on Windows where identical
        # executables from workspace artifacts can be denied directory writes.
        binary_directory = tempfile.TemporaryDirectory(prefix="provider-default-", dir=build_directory)
        try:
            binary = Path(binary_directory.name) / f"deepseek-worker{suffix}"
            shutil.copy2(build_directory / f"deepseek-worker{suffix}", binary)
            binary_digest = hashlib.sha256()
            with binary.open("rb") as binary_file:
                for chunk in iter(lambda: binary_file.read(1024 * 1024), b""):
                    binary_digest.update(chunk)
            print(json.dumps({
                "schema": "native-s3-default-worker-input-v1",
                "production_binary_sha256": binary_digest.hexdigest(),
                "production_binary_bytes": binary.stat().st_size,
                "cargo_version": subprocess.run(
                    [*cargo, "--version"], check=True, capture_output=True, text=True, timeout=60,
                ).stdout.strip(),
                "go_version": subprocess.run(
                    ["go", "version"], check=True, capture_output=True, text=True, timeout=60,
                ).stdout.strip(),
                "scope": "isolated provider qualification; default binary built without test features",
            }), flush=True)
            subprocess.run([*command1, "--no-run"], cwd=ROOT / "rust", check=True, timeout=1200)
            subprocess.run([*command2, "--no-run"], cwd=ROOT / "rust", check=True, timeout=1200)
            harness = RealStorageEnvironment.acquire(ROOT, Path(directory))
        except BaseException:
            binary_directory.cleanup()
            raise
        try:
            environment = {**os.environ, **harness.values}
            environment["DEEPSEEK_TEST_RUST_WORKER_BINARY"] = str(binary)
            endpoints = [environment[name] for name in ENDPOINT_NAMES[:3]]
            bucket = f"native-s3-{uuid.uuid4().hex}"
            for index, endpoint in enumerate(endpoints):
                # Administrative setup only. No Python put/get/copy/hash of payloads.
                client = boto3.client(
                    "s3", endpoint_url=endpoint, region_name="us-east-1",
                    aws_access_key_id=environment["AWS_ACCESS_KEY_ID"],
                    aws_secret_access_key=environment["AWS_SECRET_ACCESS_KEY"],
                    config=Config(retries={"max_attempts": 0}, proxies={}, s3={"addressing_style": "path"}),
                )
                client.create_bucket(Bucket=bucket)
                if index == 2:
                    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
            environment["DEEPSEEK_NATIVE_S3_ENDPOINTS"] = ",".join(endpoints)
            environment["DEEPSEEK_NATIVE_S3_BUCKET"] = bucket
            environment["DEEPSEEK_TEST_VERSIONED_PROVIDER_INDEX"] = "2"
            # Prove native transport ignores ambient proxies, even without NO_PROXY.
            for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                environment[variable] = "http://127.0.0.1:1"
            environment["NO_PROXY"] = environment["no_proxy"] = ""
            code1 = subprocess.run(
                [*command1, "--", "--nocapture"], cwd=ROOT / "rust", env=environment,
                check=False, timeout=300,
            ).returncode
            if code1 != 0:
                return code1
            code2 = subprocess.run(
                [*command2, "--", "--nocapture"], cwd=ROOT / "rust", env=environment,
                check=False, timeout=300,
            ).returncode
            if code2 != 0:
                return code2
            environment["CGO_ENABLED"] = "0"
            environment["GOTOOLCHAIN"] = "local"
            return subprocess.run(command3, cwd=ROOT / "go", env=environment, check=False, timeout=300).returncode
        finally:
            try:
                harness.close()
            finally:
                binary_directory.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
