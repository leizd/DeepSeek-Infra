#!/usr/bin/env python3
"""Provision real MinIO and run Rust byte tests; Python never handles payload bytes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
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
    command = [*cargo, "test", "--locked", "-p", "deepseek-storage", "--features", "s3-e2e", "--test", "s3_provider"]
    # Build before provisioning: provider lifetime is bounded by the actual tests.
    subprocess.run([*command, "--no-run"], cwd=ROOT / "rust", check=True, timeout=1200)
    with tempfile.TemporaryDirectory(prefix="deepseek-native-s3-") as directory:
        harness = RealStorageEnvironment.acquire(ROOT, Path(directory))
        try:
            environment = {**os.environ, **harness.values}
            endpoints = [environment[name] for name in ENDPOINT_NAMES[:3]]
            bucket = f"native-s3-{uuid.uuid4().hex}"
            for endpoint in endpoints:
                # Administrative setup only. No Python put/get/copy/hash of payloads.
                client = boto3.client(
                    "s3", endpoint_url=endpoint, region_name="us-east-1",
                    aws_access_key_id=environment["AWS_ACCESS_KEY_ID"],
                    aws_secret_access_key=environment["AWS_SECRET_ACCESS_KEY"],
                    config=Config(retries={"max_attempts": 0}, proxies={}, s3={"addressing_style": "path"}),
                )
                client.create_bucket(Bucket=bucket)
            environment["DEEPSEEK_NATIVE_S3_ENDPOINTS"] = ",".join(endpoints)
            environment["DEEPSEEK_NATIVE_S3_BUCKET"] = bucket
            # Prove native transport ignores ambient proxies, even without NO_PROXY.
            for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                environment[variable] = "http://127.0.0.1:1"
            environment["NO_PROXY"] = environment["no_proxy"] = ""
            return subprocess.run(
                [*command, "--", "--nocapture"], cwd=ROOT / "rust", env=environment,
                check=False, timeout=300,
            ).returncode
        finally:
            harness.close()


if __name__ == "__main__":
    raise SystemExit(main())
