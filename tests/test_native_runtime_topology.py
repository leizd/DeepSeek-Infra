from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_compose_topology_contains_zero_python_services() -> None:
    compose_path = ROOT / "docker-compose.native.yml"
    assert compose_path.exists()
    content = compose_path.read_text(encoding="utf-8")

    # Only deepseek-edge, deepseekd, deepseek-worker
    assert "deepseek-edge:" in content
    assert "deepseekd:" in content
    assert "deepseek-worker:" in content

    # Zero python services in default production topology
    assert "python" not in content.lower()
    assert "deepseek_infra" not in content

    # Edge owns port 8000
    assert '"127.0.0.1:8000:8000"' in content


def test_native_compose_selects_real_rust_binary_targets() -> None:
    content = (ROOT / "docker-compose.native.yml").read_text(encoding="utf-8")
    edge = content.split("  deepseek-edge:", 1)[1].split("  deepseekd:", 1)[0]
    control = content.split("  deepseekd:", 1)[1].split("  deepseek-worker:", 1)[0]
    worker = content.split("  deepseek-worker:", 1)[1].split("volumes:", 1)[0]

    assert "target: gateway" in edge
    assert "target: worker" in worker
    assert "DEEPSEEK_WORKER_LISTEN: 127.0.0.1:50052" in worker
    assert "WORKER_ROLE" not in worker
    assert "DEEPSEEKD_LISTEN: 0.0.0.0:8090" in control
    assert "DEEPSEEKD_MODE: shadow" in control
    assert "DEEPSEEKD_SHADOW_STORE: /data/go-control" in control
    assert "CONTROL_BIND_ADDR" not in control
    assert "CONTROL_MODE" not in control
    assert "authoritative" not in control


def test_deepseekd_dockerfile_is_cgo_disabled() -> None:
    dockerfile = ROOT / "go" / "Dockerfile"
    assert dockerfile.exists()
    content = dockerfile.read_text(encoding="utf-8")
    assert "CGO_ENABLED=0" in content
    assert "golang:1.27.1" in content
    assert "deepseekd" in content
