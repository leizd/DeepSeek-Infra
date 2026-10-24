"""Offline client boundaries; actual native image workloads remain cloud gates."""
from contextlib import nullcontext
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import verify_native_default_image as probe


def test_image_health_survives_a_reset_before_gateway_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def request(url: str, **kwargs: Any) -> Any:
        attempts.append(url)
        if len(attempts) == 1:
            raise ConnectionResetError(104, "gateway has not accepted the published port")
        return nullcontext(type("Response", (), {"status": 200})())

    monkeypatch.setattr(probe, "command", lambda *args: subprocess.CompletedProcess(args, 0, "running\n", ""))
    monkeypatch.setattr(probe, "urlopen", request)
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=probe.time.monotonic, sleep=lambda seconds: None))
    probe.wait_ready("task-owned-container", "http://127.0.0.1:1234")
    assert attempts == ["http://127.0.0.1:1234/healthz"] * 2


def test_image_health_never_requests_a_stopped_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "command", lambda *args: subprocess.CompletedProcess(args, 0, "exited\n", ""))

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an exited container must not receive a probe")

    monkeypatch.setattr(probe, "urlopen", unexpected)
    with pytest.raises(RuntimeError, match="NATIVE_IMAGE_EXITED_BEFORE_READY"):
        probe.wait_ready("task-owned-container", "http://127.0.0.1:1234")


def test_image_health_keeps_its_deadline_when_connections_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 1.0, 101.0])
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda seconds: None))
    monkeypatch.setattr(probe, "command", lambda *args: subprocess.CompletedProcess(args, 0, "running\n", ""))

    def reset(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionResetError(104, "not ready")

    monkeypatch.setattr(probe, "urlopen", reset)
    with pytest.raises(RuntimeError, match="NATIVE_IMAGE_READINESS_TIMEOUT"):
        probe.wait_ready("task-owned-container", "http://127.0.0.1:1234")


def test_image_mutation_response_loss_never_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Any] = []

    def response_lost(request: Any, **kwargs: Any) -> Any:
        requests.append(request)
        raise ConnectionResetError(104, "response lost after possible effect")

    monkeypatch.setattr(probe, "urlopen", response_lost)
    with pytest.raises(ConnectionResetError):
        probe.post("http://127.0.0.1:1234", "disposable-fixture-token", {"action": "create"})
    assert len(requests) == 1
