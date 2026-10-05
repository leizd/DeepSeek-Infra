from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_image_uses_native_supervisor_for_all_children() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "packaging/native/entrypoint.sh").read_text(encoding="utf-8")
    assert "./cmd/deepseek-launch" in dockerfile
    assert "COPY --from=go-builder /out/deepseek-launch /usr/local/bin/deepseek-launch" in dockerfile
    assert "exec deepseek-launch --server" in entrypoint
    assert "exec deepseek-gateway" not in entrypoint
    assert "deepseekd &" not in entrypoint
