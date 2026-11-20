"""Offline client boundaries; actual native image workloads remain cloud gates."""
from contextlib import nullcontext
import json
from pathlib import Path
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


def test_image_process_list_requests_pids_and_retains_all_native_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    listing = "PID COMMAND\n 101 /usr/local/bin/deepseek-launch --server\n 102 /usr/local/bin/deepseekd\n 103 /usr/local/bin/deepseek-worker\n 104 /usr/local/bin/deepseek-gateway\n"

    def top(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, listing, "")

    monkeypatch.setattr(probe, "command", top)
    assert probe.container_process_roles("task-owned-container") == {"deepseek-launch", "deepseekd", "deepseek-worker", "deepseek-gateway"}
    assert calls == [("docker", "top", "task-owned-container", "-eo", "pid,args")]


@pytest.mark.parametrize("listing", [
    "ARGS\n/usr/local/bin/deepseek-launch --server\n",
    "PID COMMAND\nmissing /usr/local/bin/deepseek-launch\n",
    "PID COMMAND\n0 /usr/local/bin/deepseek-launch\n",
])
def test_image_process_list_cannot_qualify_without_valid_pids(monkeypatch: pytest.MonkeyPatch, listing: str) -> None:
    monkeypatch.setattr(probe, "command", lambda *args: subprocess.CompletedProcess(args, 0, listing, ""))
    with pytest.raises(RuntimeError, match="NATIVE_IMAGE_PROCESS_LIST_"):
        probe.container_process_roles("task-owned-container")


def test_container_restart_refreshes_its_actual_published_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    case = probe.Scratch("sha256:" + "a" * 64, "b" * 40, tmp_path, "offline-fixture")
    case.token = "disposable-credential-not-for-reports"
    ports = iter(["32101", "32102"])
    calls: list[tuple[str, ...]] = []

    def inspect(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ("docker", "start"):
            return subprocess.CompletedProcess(args, 0, case.name, "")
        assert args == ("docker", "inspect", case.name)
        value = [{"State": {"Status": "running"}, "NetworkSettings": {"Ports": {
            "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": next(ports)}]}}}]
        return subprocess.CompletedProcess(args, 0, json.dumps(value), "")

    monkeypatch.setattr(probe, "command", inspect)
    assert case.published_origin() == "http://127.0.0.1:32101"
    assert case.restart() == "http://127.0.0.1:32102"
    assert calls == [("docker", "inspect", case.name), ("docker", "start", case.name), ("docker", "inspect", case.name)]
    raw = (tmp_path / (case.name + "-network.jsonl")).read_text()
    observations = [json.loads(line) for line in raw.splitlines()]
    assert [r["origin"] for r in observations] == ["http://127.0.0.1:32101", "http://127.0.0.1:32102"]
    assert all(set(r) == {"container", "sourceCommit", "observedAt", "state", "origin"} for r in observations)
    assert case.token not in raw


@pytest.mark.parametrize("binding", [None, [{"HostIp": "0.0.0.0", "HostPort": "32101"}], [{"HostIp": "127.0.0.1", "HostPort": "0"}]])
def test_container_address_refresh_preserves_port_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, binding: Any) -> None:
    case = probe.Scratch("sha256:" + "a" * 64, "b" * 40, tmp_path, "offline-fixture")
    value = [{"State": {"Status": "running"}, "NetworkSettings": {"Ports": {"8000/tcp": binding}}}]
    monkeypatch.setattr(probe, "command", lambda *args: subprocess.CompletedProcess(args, 0, json.dumps(value), ""))
    with pytest.raises(RuntimeError, match="NATIVE_IMAGE_PORT_"):
        case.published_origin()
