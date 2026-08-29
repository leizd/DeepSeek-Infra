"""Hermetic real-MinIO environment for storage integration tests."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

MINIO_IMAGE = (
    "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z"
    "@sha256:a1a8bd4ac40ad7881a245bab97323e18f971e4d4cba2c2007ec1bedd21cbaba2"
)
ENDPOINT_NAMES = tuple(f"DEEPSEEK_TEST_S3_ENDPOINT_{suffix}" for suffix in "ABC")
INSTANCE_NAMES = tuple(f"DEEPSEEK_TEST_MINIO_CONTAINER_{suffix}" for suffix in "ABC")
MANAGED_ENV_NAMES = (
    *ENDPOINT_NAMES,
    *INSTANCE_NAMES,
    "DEEPSEEK_TEST_S3_ENDPOINT",
    "DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
)


def _free_ports(count: int) -> tuple[int, ...]:
    ports: set[int] = set()
    while len(ports) < count:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            ports.add(int(listener.getsockname()[1]))
    return tuple(ports)


def _healthy(endpoint: str) -> bool:
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/minio/health/live", timeout=2.0) as response:  # noqa: S310
            return 200 <= int(response.status) < 300
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False


def _wait_until_healthy(endpoint: str, *, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _healthy(endpoint):
            return
        time.sleep(0.25)
    raise RuntimeError(f"MinIO did not become healthy: {endpoint}")


def _find_minio_binary(repository_root: Path) -> Path | None:
    explicit = os.environ.get("DEEPSEEK_TEST_MINIO_BINARY")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    discovered = shutil.which("minio")
    if discovered:
        candidates.append(Path(discovered))
    executable = "minio.exe" if os.name == "nt" else "minio"
    candidates.append(repository_root / "bin" / executable)
    if os.name == "nt":
        temp_root = Path(tempfile.gettempdir())
        candidates.extend(
            sorted(
                temp_root.glob(f"deepseek-minio-*/{executable}"),
                key=lambda item: item.stat().st_mtime if item.is_file() else 0.0,
                reverse=True,
            )
        )
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)


@dataclass
class _LocalMinioInstance:
    binary: Path
    data_dir: Path
    log_path: Path
    endpoint: str
    identity: str
    access_key: str
    secret_key: str
    process: subprocess.Popen[bytes] | None = None
    log_handle: BinaryIO | None = None

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open("ab")
        environment = os.environ.copy()
        environment.update(
            {
                "MINIO_ROOT_USER": self.access_key,
                "MINIO_ROOT_PASSWORD": self.secret_key,
                "MINIO_BROWSER": "off",
            }
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [str(self.binary), "server", str(self.data_dir), "--address", self.endpoint.removeprefix("http://")],
            env=environment,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        try:
            _wait_until_healthy(self.endpoint)
        except Exception:
            self.stop()
            tail = self.log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if self.log_path.is_file() else ""
            raise RuntimeError(f"local MinIO startup failed: {self.endpoint}\n{tail}") from None

    def stop(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            process.kill() if os.name == "nt" else process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.process = None
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None

    def close(self) -> None:
        self.stop()


@dataclass
class _DockerMinioInstance:
    docker: str
    endpoint: str
    identity: str
    access_key: str
    secret_key: str

    def _run(self, *arguments: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.docker, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def create(self) -> None:
        port = self.endpoint.rsplit(":", 1)[-1]
        completed = self._run(
            "run",
            "--detach",
            "--name",
            self.identity,
            "--publish",
            f"127.0.0.1:{port}:9000",
            "--env",
            f"MINIO_ROOT_USER={self.access_key}",
            "--env",
            f"MINIO_ROOT_PASSWORD={self.secret_key}",
            MINIO_IMAGE,
            "server",
            "/data",
            "--address",
            ":9000",
            timeout=180.0,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Docker MinIO creation failed: {completed.stderr[-4000:]}")
        try:
            _wait_until_healthy(self.endpoint)
        except Exception:
            diagnostics = self._run("logs", self.identity).stdout[-4000:]
            raise RuntimeError(f"Docker MinIO startup failed: {self.endpoint}\n{diagnostics}") from None

    def start(self) -> None:
        completed = self._run("start", self.identity)
        if completed.returncode != 0:
            raise RuntimeError(f"Docker MinIO start failed: {completed.stderr[-4000:]}")
        _wait_until_healthy(self.endpoint)

    def stop(self) -> None:
        completed = self._run("stop", self.identity)
        if completed.returncode != 0:
            raise RuntimeError(f"Docker MinIO stop failed: {completed.stderr[-4000:]}")

    def close(self) -> None:
        self._run("rm", "--force", self.identity)


Instance = _LocalMinioInstance | _DockerMinioInstance


def _close_instances(instances: dict[str, Instance]) -> None:
    for instance in reversed(tuple(instances.values())):
        try:
            instance.close()
        except Exception:
            pass


@dataclass
class RealStorageEnvironment:
    values: dict[str, str]
    instances: dict[str, Instance] = field(default_factory=dict)
    external: bool = False

    @classmethod
    def acquire(cls, repository_root: Path, work_dir: Path) -> RealStorageEnvironment:
        configured_endpoints = tuple(str(os.environ.get(name) or "").rstrip("/") for name in ENDPOINT_NAMES)
        configured_instances = tuple(str(os.environ.get(name) or "") for name in INSTANCE_NAMES)
        if all(configured_endpoints):
            if len(set(configured_endpoints)) != 3:
                raise RuntimeError("configured real S3 endpoints must be three independent URLs")
            if not all(configured_instances):
                raise RuntimeError("configured real S3 endpoints require three controllable MinIO identities")
            access_key = str(os.environ.get("AWS_ACCESS_KEY_ID") or "")
            secret_key = str(os.environ.get("AWS_SECRET_ACCESS_KEY") or "")
            if not access_key or not secret_key:
                raise RuntimeError("configured real S3 endpoints require AWS test credentials")
            return cls(
                values={
                    "DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E": "1",
                    "DEEPSEEK_TEST_S3_ENDPOINT": configured_endpoints[0],
                },
                external=True,
            )

        access_key = str(os.environ.get("MINIO_ROOT_USER") or "deepseekci")
        secret_key = str(os.environ.get("MINIO_ROOT_PASSWORD") or "local-storage-control-e2e")
        ports = _free_ports(3)
        endpoints = tuple(f"http://127.0.0.1:{port}" for port in ports)
        binary = _find_minio_binary(repository_root)
        instances: dict[str, Instance] = {}
        try:
            if binary is not None:
                for suffix, endpoint in zip("ABC", endpoints, strict=True):
                    identity = f"local-minio-{suffix.lower()}-{uuid.uuid4().hex[:10]}"
                    local_instance = _LocalMinioInstance(
                        binary=binary,
                        data_dir=work_dir / f"data-{suffix.lower()}",
                        log_path=work_dir / f"minio-{suffix.lower()}.log",
                        endpoint=endpoint,
                        identity=identity,
                        access_key=access_key,
                        secret_key=secret_key,
                    )
                    instances[identity] = local_instance
                    local_instance.start()
            else:
                docker = shutil.which("docker")
                if docker is None:
                    raise RuntimeError(
                        "real storage tests require MinIO: set DEEPSEEK_TEST_MINIO_BINARY, install minio, or start Docker"
                    )
                probe = subprocess.run(
                    [docker, "info", "--format", "{{.ServerVersion}}"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if probe.returncode != 0:
                    raise RuntimeError(f"Docker is unavailable and no MinIO binary was found: {probe.stderr[-2000:]}")
                for suffix, endpoint in zip("ABC", endpoints, strict=True):
                    identity = f"deepseek-pytest-minio-{suffix.lower()}-{uuid.uuid4().hex[:10]}"
                    docker_instance = _DockerMinioInstance(docker, endpoint, identity, access_key, secret_key)
                    instances[identity] = docker_instance
                    docker_instance.create()
        except Exception:
            _close_instances(instances)
            raise

        identities = tuple(instances)
        values = {
            "AWS_ACCESS_KEY_ID": access_key,
            "AWS_SECRET_ACCESS_KEY": secret_key,
            "MINIO_ROOT_USER": access_key,
            "MINIO_ROOT_PASSWORD": secret_key,
            "DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E": "1",
            "DEEPSEEK_TEST_S3_ENDPOINT": endpoints[0],
        }
        values.update(dict(zip(ENDPOINT_NAMES, endpoints, strict=True)))
        values.update(dict(zip(INSTANCE_NAMES, identities, strict=True)))
        return cls(values=values, instances=instances)

    def control(self, action: str, identity: str) -> None:
        if action not in {"start", "stop"}:
            raise ValueError(f"unsupported MinIO action: {action}")
        instance = self.instances.get(identity)
        if instance is not None:
            getattr(instance, action)()
            return
        if not self.external:
            raise RuntimeError(f"unknown managed MinIO identity: {identity}")
        docker = shutil.which("docker")
        if docker is None:
            raise RuntimeError(f"external MinIO identity is not locally controllable: {identity}")
        completed = subprocess.run(
            [docker, action, identity],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Docker MinIO {action} failed: {completed.stderr[-4000:]}")

    def close(self) -> None:
        errors: list[str] = []
        for instance in reversed(tuple(self.instances.values())):
            try:
                instance.close()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("; ".join(errors))


def ensure_native_backup_helpers(repository_root: Path) -> None:
    executable_suffix = ".exe" if os.name == "nt" else ""
    expected = (
        repository_root / "bin" / f"backup-crypto{executable_suffix}",
        repository_root / "bin" / f"deepseek-backup{executable_suffix}",
    )
    if all(path.is_file() for path in expected):
        return
    completed = subprocess.run(
        [sys.executable, str(repository_root / "scripts" / "build_backup_crypto.py")],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if completed.returncode != 0 or not all(path.is_file() for path in expected):
        raise RuntimeError(f"native backup helper build failed: {completed.stderr[-4000:]}")
