#!/usr/bin/env python3
"""Local Storage Control Plane MinIO Evidence environment helper.

Brings up the same five-MinIO harness CI uses, prints shell env, and can
invoke the Evidence runner or the 4.6.8 process-replacement node alone.

Formal release Evidence remains owned by CI job ``storage-control-plane-minio-e2e``
on the exact merge commit — local PASS is for development only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace import backup_crypto  # noqa: E402

COMPOSE_FILE = ROOT / "docker-compose.storage-control-minio.yml"
MINIO_IMAGE = (
    "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z"
    "@sha256:a1a8bd4ac40ad7881a245bab97323e18f971e4d4cba2c2007ec1bedd21cbaba2"
)
DEFAULT_USER = "deepseekci"
DEFAULT_PASSWORD = "local-storage-control-e2e"  # pragma: allowlist secret
DEFAULT_FEDERATION_USER = "deepseekfederation"
DEFAULT_FEDERATION_PASSWORD = "local-federation-storage-e2e"  # pragma: allowlist secret
CONTAINERS = (
    ("DEEPSEEK_TEST_MINIO_CONTAINER_A", "deepseek-minio-control-a", 9000),
    ("DEEPSEEK_TEST_MINIO_CONTAINER_B", "deepseek-minio-control-b", 9001),
    ("DEEPSEEK_TEST_MINIO_CONTAINER_C", "deepseek-minio-control-c", 9002),
    ("DEEPSEEK_TEST_MINIO_CONTAINER_D", "deepseek-minio-control-d", 9003),
    ("DEEPSEEK_TEST_MINIO_CONTAINER_E", "deepseek-minio-control-e", 9004),
)
ENDPOINT_ENV = (
    ("DEEPSEEK_TEST_S3_ENDPOINT_A", "http://127.0.0.1:9000"),
    ("DEEPSEEK_TEST_S3_ENDPOINT_B", "http://127.0.0.1:9001"),
    ("DEEPSEEK_TEST_S3_ENDPOINT_C", "http://127.0.0.1:9002"),
    ("DEEPSEEK_TEST_S3_ENDPOINT_D", "http://127.0.0.1:9003"),
    ("DEEPSEEK_TEST_S3_ENDPOINT_E", "http://127.0.0.1:9004"),
)
PROCESS_REPLACE_NODE = (
    "tests/test_backup_468_real_backup_dr_sigkill_e2e.py::"
    "test_real_three_minio_sigkill_backup_disaster_recovery_e2e"
)


def default_credentials() -> tuple[str, str]:
    user = str(os.environ.get("MINIO_ROOT_USER") or os.environ.get("AWS_ACCESS_KEY_ID") or DEFAULT_USER)
    password = str(
        os.environ.get("MINIO_ROOT_PASSWORD")
        or os.environ.get("AWS_SECRET_ACCESS_KEY")
        or DEFAULT_PASSWORD
    )
    return user, password


def federation_credentials() -> tuple[str, str]:
    user = str(os.environ.get("FEDERATION_MINIO_ROOT_USER") or DEFAULT_FEDERATION_USER)
    password = str(os.environ.get("FEDERATION_MINIO_ROOT_PASSWORD") or DEFAULT_FEDERATION_PASSWORD)
    return user, password


def evidence_env(
    *,
    user: str | None = None,
    password: str | None = None,
    federation_user: str | None = None,
    federation_password: str | None = None,
) -> dict[str, str]:
    """Canonical env block for the Storage Control Plane MinIO Evidence runner."""
    access, secret = default_credentials()
    if user is not None:
        access = user
    if password is not None:
        secret = password
    federation_access, federation_secret = federation_credentials()
    if federation_user is not None:
        federation_access = federation_user
    if federation_password is not None:
        federation_secret = federation_password
    env: dict[str, str] = {
        "MINIO_ROOT_USER": access,
        "MINIO_ROOT_PASSWORD": secret,
        "AWS_ACCESS_KEY_ID": access,
        "AWS_SECRET_ACCESS_KEY": secret,
        "FEDERATION_MINIO_ROOT_USER": federation_access,
        "FEDERATION_MINIO_ROOT_PASSWORD": federation_secret,
        "DEEPSEEK_TEST_FEDERATION_ACCESS_KEY_ID": federation_access,
        "DEEPSEEK_TEST_FEDERATION_SECRET_ACCESS_KEY": federation_secret,
        "DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E": "1",
    }
    for key, value in ENDPOINT_ENV:
        env[key] = value
    for env_name, container_name, _port in CONTAINERS:
        env[env_name] = container_name
    return env


def format_env_export(env: dict[str, str], *, shell: str) -> str:
    lines: list[str] = []
    if shell == "pwsh":
        for key, value in env.items():
            lines.append(f"$env:{key} = '{value}'")
    elif shell == "bash":
        for key, value in env.items():
            lines.append(f"export {key}={json.dumps(value)}")
    elif shell == "dotenv":
        for key, value in env.items():
            lines.append(f"{key}={value}")
    else:
        raise ValueError(f"unsupported-shell:{shell}")
    return "\n".join(lines) + "\n"


def find_docker() -> str | None:
    return shutil.which("docker")


def docker_available() -> bool:
    binary = find_docker()
    if not binary:
        return False
    try:
        completed = subprocess.run(
            [binary, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and bool(str(completed.stdout or "").strip())


def endpoint_healthy(url: str, *, timeout_seconds: float = 2.0) -> bool:
    health = url.rstrip("/") + "/minio/health/live"
    try:
        with urllib.request.urlopen(health, timeout=timeout_seconds) as response:  # noqa: S310
            return 200 <= int(response.status) < 300
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False


def wait_for_minio(*, attempts: int = 45, sleep_seconds: float = 1.0) -> list[str]:
    pending = [value for _, value in ENDPOINT_ENV]
    for _ in range(max(1, attempts)):
        pending = [url for url in pending if not endpoint_healthy(url)]
        if not pending:
            return []
        time.sleep(sleep_seconds)
    return pending


def prerequisite_report() -> dict[str, Any]:
    user, password = default_credentials()
    federation_user, federation_password = federation_credentials()
    docker_bin = find_docker()
    helper = backup_crypto.helper_path()
    boto3_ok = importlib.util.find_spec("boto3") is not None
    endpoints = {key: value for key, value in ENDPOINT_ENV}
    health = {key: endpoint_healthy(value) for key, value in ENDPOINT_ENV}
    errors: list[str] = []
    if not docker_bin:
        errors.append("docker-cli-missing: install Docker Desktop and ensure docker is on PATH")
    elif not docker_available():
        errors.append("docker-daemon-unavailable: start Docker Desktop / engine")
    if not boto3_ok:
        errors.append("boto3-missing: pip install -r requirements-s3-e2e.txt")
    if helper is None:
        errors.append("age-helper-missing: python scripts/build_backup_crypto.py")
    if not all(health.values()):
        errors.append("minio-unhealthy: run start (or docker compose up) then re-check")
    if not user or not password:
        errors.append("credentials-missing")
    if not federation_user or not federation_password:
        errors.append("federation-credentials-missing")
    if (user, password) == (federation_user, federation_password):
        errors.append("federation-credentials-must-be-distinct")
    return {
        "ok": not errors,
        "errors": errors,
        "docker": docker_bin,
        "dockerDaemon": docker_available(),
        "boto3": boto3_ok,
        "ageHelper": str(helper) if helper is not None else None,
        "composeFile": str(COMPOSE_FILE),
        "composeFileExists": COMPOSE_FILE.is_file(),
        "endpoints": endpoints,
        "endpointHealth": health,
        "credentials": {
            "sourceUser": user,
            "sourcePasswordSet": bool(password),
            "federationUser": federation_user,
            "federationPasswordSet": bool(federation_password),
            "distinct": (user, password) != (federation_user, federation_password),
        },
        "env": evidence_env(
            user=user,
            password=password,
            federation_user=federation_user,
            federation_password=federation_password,
        ),
    }


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(cmd, cwd=ROOT, env=merged, text=True, check=False)


def start_with_compose(*, user: str, password: str, federation_user: str, federation_password: str) -> int:
    docker = find_docker()
    if not docker:
        print("docker-cli-missing", file=sys.stderr)
        return 2
    if not COMPOSE_FILE.is_file():
        print(f"compose-missing:{COMPOSE_FILE}", file=sys.stderr)
        return 2
    env = {
        "MINIO_ROOT_USER": user,
        "MINIO_ROOT_PASSWORD": password,
        "FEDERATION_MINIO_ROOT_USER": federation_user,
        "FEDERATION_MINIO_ROOT_PASSWORD": federation_password,
    }
    # Prefer `docker compose` plugin; fall back to docker-compose binary.
    base = [docker, "compose", "-f", str(COMPOSE_FILE)]
    probe = _run([*base, "version"], env=env)
    if probe.returncode != 0:
        compose_bin = shutil.which("docker-compose")
        if compose_bin is None:
            print("docker-compose-unavailable", file=sys.stderr)
            return 2
        base = [compose_bin, "-f", str(COMPOSE_FILE)]
    stopped = _run([*base, "down", "--remove-orphans"], env=env)
    if stopped.returncode not in {0, 1}:
        print(stopped.stderr or stopped.stdout, file=sys.stderr)
    started = _run([*base, "up", "-d", "--pull", "missing"], env=env)
    if started.returncode != 0:
        print(started.stderr or started.stdout, file=sys.stderr)
        return started.returncode
    pending = wait_for_minio()
    if pending:
        print(f"minio-health-timeout:{','.join(pending)}", file=sys.stderr)
        return 1
    print("minio-ready")
    return 0


def start_with_docker_run(*, user: str, password: str, federation_user: str, federation_password: str) -> int:
    docker = find_docker()
    if not docker:
        print("docker-cli-missing", file=sys.stderr)
        return 2
    for _env_name, name, port in CONTAINERS:
        access = federation_user if port >= 9003 else user
        secret = federation_password if port >= 9003 else password
        env = {"MINIO_ROOT_USER": access, "MINIO_ROOT_PASSWORD": secret}
        _run([docker, "rm", "-f", name], env=env)
        cmd = [
            docker,
            "run",
            "--detach",
            "--name",
            name,
            "--publish",
            f"127.0.0.1:{port}:{port}",
            "--env",
            "MINIO_ROOT_USER",
            "--env",
            "MINIO_ROOT_PASSWORD",
            MINIO_IMAGE,
            "server",
            "/data",
            "--address",
            f":{port}",
        ]
        completed = _run(cmd, env=env)
        if completed.returncode != 0:
            print(completed.stderr or completed.stdout, file=sys.stderr)
            return completed.returncode
    pending = wait_for_minio()
    if pending:
        print(f"minio-health-timeout:{','.join(pending)}", file=sys.stderr)
        return 1
    print("minio-ready")
    return 0


def stop_minio() -> int:
    docker = find_docker()
    if not docker:
        print("docker-cli-missing", file=sys.stderr)
        return 2
    rc = 0
    if COMPOSE_FILE.is_file():
        base = [docker, "compose", "-f", str(COMPOSE_FILE), "down", "--remove-orphans"]
        completed = _run(base)
        if completed.returncode != 0:
            compose_bin = shutil.which("docker-compose")
            if compose_bin is not None:
                completed = _run([compose_bin, "-f", str(COMPOSE_FILE), "down", "--remove-orphans"])
        if completed.returncode not in {0, 1}:
            rc = completed.returncode
    for _env_name, name, _port in CONTAINERS:
        completed = _run([docker, "rm", "-f", name])
        if completed.returncode not in {0, 1}:
            rc = completed.returncode
    print("minio-stopped")
    return rc


def run_evidence(*, scenario: str, out: Path | None) -> int:
    env = os.environ.copy()
    env.update(evidence_env())
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if scenario == "all":
        target = out or (ROOT / "docs" / "evidence" / f"storage-control-plane-minio-v{version}-local.json")
        cmd = [sys.executable, str(ROOT / "scripts" / "run_storage_control_plane_minio_e2e.py"), "--out", str(target)]
        print(f"running-full-runner out={target}")
        return _run(cmd, env=env).returncode
    if scenario == "process-replace":
        cmd = [sys.executable, "-m", "pytest", "--no-cov", "-q", PROCESS_REPLACE_NODE]
        print(f"running-node {PROCESS_REPLACE_NODE}")
        return _run(cmd, env=env).returncode
    print(f"unknown-scenario:{scenario}", file=sys.stderr)
    return 2


def cmd_doctor() -> int:
    report = prerequisite_report()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


def cmd_env(shell: str) -> int:
    print(format_env_export(evidence_env(), shell=shell), end="")
    return 0


def cmd_start(mode: str) -> int:
    user, password = default_credentials()
    federation_user, federation_password = federation_credentials()
    if (user, password) == (federation_user, federation_password):
        print("federation-credentials-must-be-distinct", file=sys.stderr)
        return 2
    if mode == "compose":
        return start_with_compose(
            user=user,
            password=password,
            federation_user=federation_user,
            federation_password=federation_password,
        )
    if mode == "docker-run":
        return start_with_docker_run(
            user=user,
            password=password,
            federation_user=federation_user,
            federation_password=federation_password,
        )
    print(f"unknown-start-mode:{mode}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="JSON status of Docker/boto3/Age/MinIO health")
    env_p = sub.add_parser("env", help="Print shell exports for Evidence env")
    env_p.add_argument("--shell", choices=("pwsh", "bash", "dotenv"), default="pwsh")

    start_p = sub.add_parser("start", help="Start five MinIO containers and wait for health")
    start_p.add_argument(
        "--mode",
        choices=("compose", "docker-run"),
        default="compose",
        help="compose uses docker-compose.storage-control-minio.yml (default)",
    )

    sub.add_parser("stop", help="Stop/remove the five MinIO containers")
    sub.add_parser("check", help="Exit 0 only when full Evidence prereqs are green")

    run_p = sub.add_parser("run", help="Run Evidence tests with canonical env")
    run_p.add_argument(
        "--scenario",
        choices=("process-replace", "all"),
        default="process-replace",
        help="process-replace = 4.6.8 SIGKILL node; all = full MinIO Evidence runner",
    )
    run_p.add_argument("--out", type=Path, default=None)
    run_p.add_argument(
        "--start-if-needed",
        action="store_true",
        help="Start MinIO automatically when endpoints are unhealthy",
    )

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "env":
        return cmd_env(args.shell)
    if args.command == "start":
        return cmd_start(args.mode)
    if args.command == "stop":
        return stop_minio()
    if args.command == "check":
        report = prerequisite_report()
        if not report["ok"]:
            for item in report["errors"]:
                print(item, file=sys.stderr)
            return 1
        print("evidence-env-ready")
        return 0
    if args.command == "run":
        if args.start_if_needed and any(not endpoint_healthy(url) for _, url in ENDPOINT_ENV):
            rc = cmd_start("compose")
            if rc != 0:
                return rc
        report = prerequisite_report()
        # run does not require docker once endpoints are healthy (468 alone).
        soft_errors = [
            e
            for e in report["errors"]
            if not e.startswith("docker-") and e != "minio-unhealthy: run start (or docker compose up) then re-check"
        ]
        # Still need boto3, age, healthy endpoints
        needed = []
        if not report["boto3"]:
            needed.append("boto3-missing")
        if report["ageHelper"] is None:
            needed.append("age-helper-missing")
        if not all(report["endpointHealth"].values()):
            needed.append("minio-unhealthy")
        if needed:
            for item in needed:
                print(item, file=sys.stderr)
            if soft_errors:
                for item in soft_errors:
                    print(item, file=sys.stderr)
            return 1
        return run_evidence(scenario=args.scenario, out=args.out)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
