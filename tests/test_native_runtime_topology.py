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


def test_deepseekd_dockerfile_is_cgo_disabled() -> None:
    dockerfile = ROOT / "go" / "Dockerfile"
    assert dockerfile.exists()
    content = dockerfile.read_text(encoding="utf-8")
    assert "CGO_ENABLED=0" in content
    assert "golang:1.27.1" in content
    assert "deepseekd" in content
