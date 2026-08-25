"""Unit tests for local Storage Control Plane MinIO Evidence setup helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    path = ROOT / "scripts" / "setup_storage_control_plane_minio_e2e.py"
    spec = importlib.util.spec_from_file_location("setup_scp_minio", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evidence_env_and_shell_export_shapes() -> None:
    mod = _load()
    env = mod.evidence_env(user="u", password="p")
    assert env["AWS_ACCESS_KEY_ID"] == "u"
    assert env["AWS_SECRET_ACCESS_KEY"] == "p"
    assert env["DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E"] == "1"
    assert env["DEEPSEEK_TEST_S3_ENDPOINT_A"] == "http://127.0.0.1:9000"
    assert env["DEEPSEEK_TEST_MINIO_CONTAINER_C"] == "deepseek-minio-control-c"
    pwsh = mod.format_env_export(env, shell="pwsh")
    assert "$env:AWS_ACCESS_KEY_ID = 'u'" in pwsh
    bash = mod.format_env_export(env, shell="bash")
    assert 'export AWS_ACCESS_KEY_ID="u"' in bash or "export AWS_ACCESS_KEY_ID=" in bash
    dotenv = mod.format_env_export(env, shell="dotenv")
    assert "AWS_ACCESS_KEY_ID=u" in dotenv


def test_prerequisite_report_lists_blockers(monkeypatch: Any) -> None:
    mod = _load()
    monkeypatch.setattr(mod, "find_docker", lambda: None)
    monkeypatch.setattr(mod, "docker_available", lambda: False)
    monkeypatch.setattr(mod, "endpoint_healthy", lambda _url, timeout_seconds=2.0: False)
    monkeypatch.setattr(mod.backup_crypto, "helper_path", lambda: None)
    monkeypatch.setattr(mod.importlib.util, "find_spec", lambda _name: None)
    report = mod.prerequisite_report()
    assert report["ok"] is False
    joined = " ".join(report["errors"])
    assert "docker-cli-missing" in joined
    assert "boto3-missing" in joined
    assert "age-helper-missing" in joined
    assert "minio-unhealthy" in joined


def test_wait_for_minio_success_and_timeout(monkeypatch: Any) -> None:
    mod = _load()
    calls = {"n": 0}

    def flaky(_url: str, timeout_seconds: float = 2.0) -> bool:
        del timeout_seconds
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(mod, "endpoint_healthy", flaky)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    assert mod.wait_for_minio(attempts=5, sleep_seconds=0) == []

    monkeypatch.setattr(mod, "endpoint_healthy", lambda *_a, **_k: False)
    pending = mod.wait_for_minio(attempts=2, sleep_seconds=0)
    assert len(pending) == 3


def test_cli_env_and_doctor(monkeypatch: Any, capsys: Any) -> None:
    mod = _load()
    monkeypatch.setattr(
        mod,
        "prerequisite_report",
        lambda: {
            "ok": False,
            "errors": ["docker-cli-missing"],
            "boto3": True,
            "ageHelper": "x",
            "endpointHealth": {k: True for k, _ in mod.ENDPOINT_ENV},
        },
    )
    assert mod.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "docker-cli-missing" in out
    assert mod.main(["env", "--shell", "dotenv"]) == 0
    assert "DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E=1" in capsys.readouterr().out
