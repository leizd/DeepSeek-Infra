from pathlib import Path

import pytest

from scripts.check_zero_python_runtime import check_compose_topology

ROOT = Path(__file__).resolve().parents[1]


def test_compose_places_control_and_worker_in_public_listeners_private_namespace() -> None:
    content = (ROOT / "docker-compose.native.yml").read_text(encoding="utf-8")
    edge = content.split("  deepseek-edge:", 1)[1].split("  deepseekd:", 1)[0]
    control = content.split("  deepseekd:", 1)[1].split("  deepseek-worker:", 1)[0]
    worker = content.split("  deepseek-worker:", 1)[1].split("  browser-engine:", 1)[0]
    assert "GO_CONTROL_ADDR: http://127.0.0.1:8090" in edge
    assert "DEEPSEEK_RUNTIME_MODE: python_disabled" in edge
    assert "DEEPSEEKD_LISTEN: 127.0.0.1:8090" in control
    assert 'network_mode: "service:deepseek-edge"' in control
    assert 'network_mode: "service:deepseek-edge"' in worker
    dependency = "depends_on:\n      deepseek-edge:\n        condition: service_started\n        restart: true"
    assert dependency in control
    assert dependency in worker


@pytest.mark.parametrize("mutation", ["control_origin", "control_namespace", "worker_namespace", "public_control", "dependency_restart", "legacy_service"])
def test_source_gate_refuses_disconnected_or_legacy_private_topology(tmp_path: Path, mutation: str) -> None:
    content = (ROOT / "docker-compose.native.yml").read_text(encoding="utf-8")
    if mutation == "control_origin":
        content = content.replace("GO_CONTROL_ADDR: http://127.0.0.1:8090", "GO_CONTROL_ADDR: http://deepseekd:8090")
    elif mutation == "control_namespace":
        content = content.replace('network_mode: "service:deepseek-edge"', 'network_mode: "bridge"', 1)
    elif mutation == "worker_namespace":
        first = content.index("  deepseek-worker:")
        content = content[:first] + content[first:].replace('network_mode: "service:deepseek-edge"', 'network_mode: "bridge"', 1)
    elif mutation == "public_control":
        content = content.replace("DEEPSEEKD_LISTEN: 127.0.0.1:8090", "DEEPSEEKD_LISTEN: 0.0.0.0:8090")
    elif mutation == "dependency_restart":
        content = content.replace("        restart: true", "        restart: false", 1)
    else:
        content += "\n  legacy-python:\n    image: python:3.12\n"
    (tmp_path / "docker-compose.native.yml").write_text(content, encoding="utf-8")
    go = tmp_path / "go"
    go.mkdir()
    (go / "Dockerfile").write_text("RUN CGO_ENABLED=0 go build ./cmd/deepseekd\n", encoding="utf-8")
    assert not check_compose_topology(tmp_path).passed
