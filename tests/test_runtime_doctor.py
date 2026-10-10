from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from deepseek_infra.infra.diagnostics import runtime_doctor as doctor


def test_mask_token_never_leaks_full_value() -> None:
    token = "super-secret-token-1234567890"
    masked = doctor.mask_token(token)
    assert token not in masked
    assert masked.startswith("supe")
    assert masked.endswith("7890")
    assert doctor.mask_token("") == ""
    assert doctor.mask_token("short") == "***"


def test_python_version_pass_and_fail() -> None:
    assert doctor.check_python_version((3, 0)).status == doctor.STATUS_PASS
    fail = doctor.check_python_version((99, 0))
    assert fail.status == doctor.STATUS_FAIL
    assert "older" in fail.detail


def test_requirements_fail_lists_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_import(name: str) -> bool:
        return name != "fastapi" and name != "reportlab"

    monkeypatch.setattr(doctor, "_try_import", fake_import)
    result = doctor.check_requirements((("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("reportlab", "reportlab")))
    assert result.status == doctor.STATUS_FAIL
    assert "fastapi" in result.detail and "reportlab" in result.detail
    assert "uvicorn" not in result.detail


def test_optional_requirements_warns_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_try_import", lambda name: name != "webview")
    result = doctor.check_optional_requirements((("customtkinter", "customtkinter"), ("pywebview", "webview")))
    assert result.status == doctor.STATUS_WARN
    assert "pywebview" in result.detail


def test_edge_router_doctor_passes_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.gateway import edge_inference

    monkeypatch.setattr(
        edge_inference,
        "edge_inference_status",
        lambda: {"enabled": False, "provider": "llama_cpp", "available": False, "suggestions": ["enable it"]},
    )

    result = doctor.check_edge_router()

    assert result.status == doctor.STATUS_PASS
    assert "disabled" in result.detail


def test_edge_router_doctor_warns_with_suggestions(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.gateway import edge_inference

    monkeypatch.setattr(
        edge_inference,
        "edge_inference_status",
        lambda: {
            "enabled": True,
            "provider": "llama_cpp",
            "available": False,
            "dependencyAvailable": False,
            "suggestions": ["Install llama-cpp-python.", "Check EDGE_MODEL_PATH exists."],
        },
    )

    result = doctor.check_edge_router()

    assert result.status == doctor.STATUS_WARN
    assert "llama-cpp-python" in result.detail
    assert "EDGE_MODEL_PATH" in result.detail


def test_env_file_states(tmp_path: Path) -> None:
    assert doctor.check_env_file(tmp_path).status == doctor.STATUS_WARN
    (tmp_path / ".env.example").write_text("DEEPSEEK_API_KEY=", encoding="utf-8")
    detail = doctor.check_env_file(tmp_path).detail
    assert ".env.example" in detail
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=x", encoding="utf-8")
    assert doctor.check_env_file(tmp_path).status == doctor.STATUS_PASS


def test_api_key_warning_and_masking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert doctor.check_api_key().status == doctor.STATUS_WARN
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-very-long-secret-abcdef")
    result = doctor.check_api_key()
    assert result.status == doctor.STATUS_PASS
    assert result.data["masked"] == doctor.mask_token("sk-very-long-secret-abcdef")
    assert "sk-very-long-secret-abcdef" not in result.detail


def test_root_writable_pass_and_fail(tmp_path: Path) -> None:
    assert doctor.check_root_writable(tmp_path).status == doctor.STATUS_PASS
    bogus = tmp_path / "not-a-dir.txt"
    bogus.write_text("blocker", encoding="utf-8")
    result = doctor.check_root_writable(bogus)
    assert result.status == doctor.STATUS_FAIL


def test_static_dir_states(tmp_path: Path) -> None:
    missing = tmp_path / "static"
    assert doctor.check_static_dir(missing).status == doctor.STATUS_FAIL
    missing.mkdir()
    assert doctor.check_static_dir(missing).status == doctor.STATUS_WARN
    (missing / "ui").mkdir()
    (missing / "ui" / "index.html").write_text("<html></html>", encoding="utf-8")
    assert doctor.check_static_dir(missing).status == doctor.STATUS_PASS


def test_data_dirs_pass_and_fail(tmp_path: Path) -> None:
    assert doctor.check_data_dirs(tmp_path, (".traces", ".local-rag")).status == doctor.STATUS_PASS
    assert (tmp_path / ".traces").is_dir()
    blocker = tmp_path / "block.txt"
    blocker.write_text("x", encoding="utf-8")
    result = doctor.check_data_dirs(blocker, (".traces",))
    assert result.status == doctor.STATUS_FAIL


def test_port_free_passes_and_occupied_warns() -> None:
    free = doctor.check_port("127.0.0.1", 0)
    assert free.status == doctor.STATUS_PASS
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    occupied = doctor.check_port("127.0.0.1", holder.getsockname()[1])
    try:
        assert occupied.status == doctor.STATUS_WARN
        assert occupied.data["free"] is False
    finally:
        holder.close()


def test_token_file_states_and_masking(tmp_path: Path) -> None:
    assert doctor.check_token_file(tmp_path).status == doctor.STATUS_WARN
    (tmp_path / ".auth-token").write_text("\n", encoding="utf-8")
    assert doctor.check_token_file(tmp_path).status == doctor.STATUS_WARN
    secret = "tok-abcdef1234567890"
    (tmp_path / ".auth-token").write_text(secret + "\n", encoding="utf-8")
    result = doctor.check_token_file(tmp_path)
    assert result.status == doctor.STATUS_PASS
    assert secret not in result.detail
    assert result.data["masked"] == doctor.mask_token(secret)


def test_run_doctor_offline_skips_health_probes(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "ui").mkdir()
    (static / "ui" / "index.html").write_text("ok", encoding="utf-8")
    options = doctor.DoctorOptions(root=tmp_path, static_dir=static, offline=True, required_imports=(), optional_imports=())
    results = doctor.run_doctor(options)
    names = [r.name for r in results]
    assert not any(name.startswith("health:") for name in names)
    assert "requirements" in names and "port" in names
    assert doctor.exit_code(results) == 0


def test_run_doctor_with_server_probes_health(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "ui").mkdir()
    (static / "ui" / "index.html").write_text("ok", encoding="utf-8")
    options = doctor.DoctorOptions(
        root=tmp_path,
        static_dir=static,
        offline=False,
        base_url="http://127.0.0.1:8000",
        required_imports=(),
        optional_imports=(),
    )

    fake_responses = {"http://127.0.0.1:8000/healthz": True, "http://127.0.0.1:8000/readyz": True, "http://127.0.0.1:8000/metrics": False}

    def fake_probe(url: str, timeout: float) -> tuple[bool, str]:
        return (fake_responses[url], "200" if fake_responses[url] else "fail")

    with patch.object(doctor, "_probe_url", fake_probe):
        results = doctor.run_doctor(options)
    health = [r for r in results if r.name.startswith("health:")]
    assert len(health) == 3
    assert any(r.status == doctor.STATUS_FAIL and r.name == "health:/metrics" for r in health)
    assert any(r.status == doctor.STATUS_PASS and r.name == "health:/healthz" for r in health)


def test_summarize_and_exit_code() -> None:
    results = [
        doctor.CheckResult("a", doctor.STATUS_PASS, "ok"),
        doctor.CheckResult("b", doctor.STATUS_WARN, "hmm"),
        doctor.CheckResult("c", doctor.STATUS_FAIL, "boom"),
    ]
    summary = doctor.summarize(results)
    assert summary["overall"] == doctor.STATUS_FAIL
    assert summary["counts"][doctor.STATUS_FAIL] == 1
    assert doctor.exit_code(results) == 1
    assert doctor.exit_code(results[:2]) == 0


def test_render_text_does_not_leak_token(tmp_path: Path) -> None:
    secret = "tok-supersecret-abcdef1234567890"
    (tmp_path / ".auth-token").write_text(secret, encoding="utf-8")
    options = doctor.DoctorOptions(root=tmp_path, static_dir=tmp_path, offline=True, required_imports=(), optional_imports=())
    text = doctor.render_text(doctor.run_doctor(options))
    assert secret not in text
    assert "PASS" in text and "Doctor summary" in text


def test_offline_doctor_passes_in_clean_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    static = tmp_path / "static"
    static.mkdir()
    (static / "ui").mkdir()
    (static / "ui" / "index.html").write_text("ok", encoding="utf-8")
    options = doctor.DoctorOptions(
        root=tmp_path,
        static_dir=static,
        offline=True,
        host="127.0.0.1",
        port=0,
        required_imports=(),
        optional_imports=(),
    )
    assert doctor.exit_code(doctor.run_doctor(options)) == 0
