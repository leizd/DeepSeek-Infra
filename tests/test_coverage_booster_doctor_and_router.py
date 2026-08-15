"""Targeted test coverage boosters for runtime_doctor and model_router."""

from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.diagnostics import runtime_doctor
from deepseek_infra.infra.gateway import model_router


def test_runtime_doctor_checks(tmp_settings: Path) -> None:
    # 1. Mask token
    assert runtime_doctor.mask_token("") == ""
    assert runtime_doctor.mask_token("short") == "***"
    assert runtime_doctor.mask_token("1234567890abcdef") == "1234…cdef"

    # 2. Check python version
    res_py = runtime_doctor.check_python_version((3, 8))
    assert res_py.status == runtime_doctor.STATUS_PASS

    res_py_fail = runtime_doctor.check_python_version((99, 0))
    assert res_py_fail.status == runtime_doctor.STATUS_FAIL

    # 3. Check requirements
    res_req = runtime_doctor.check_requirements((("pytest", "pytest"),))
    assert res_req.status == runtime_doctor.STATUS_PASS

    res_req_fail = runtime_doctor.check_requirements((("nonexistent_pkg_123", "nonexistent_pkg_123"),))
    assert res_req_fail.status == runtime_doctor.STATUS_FAIL

    # 4. Check optional requirements
    res_opt = runtime_doctor.check_optional_requirements((("pytest", "pytest"),))
    assert res_opt.status == runtime_doctor.STATUS_PASS

    res_opt_warn = runtime_doctor.check_optional_requirements((("nonexistent_gui_pkg", "nonexistent_gui_pkg"),))
    assert res_opt_warn.status == runtime_doctor.STATUS_WARN

    # 5. Check env file
    env_pass = runtime_doctor.check_env_file(tmp_settings)
    assert env_pass.status in {runtime_doctor.STATUS_PASS, runtime_doctor.STATUS_WARN}

    (tmp_settings / ".env.example").write_text("DEEPSEEK_API_KEY=test", encoding="utf-8")
    env_example = runtime_doctor.check_env_file(tmp_settings)
    assert env_example.status == runtime_doctor.STATUS_WARN

    (tmp_settings / ".env").write_text("DEEPSEEK_API_KEY=test", encoding="utf-8")
    assert runtime_doctor.check_env_file(tmp_settings).status == runtime_doctor.STATUS_PASS

    # 6. Check root writable and data dirs
    rw_res = runtime_doctor.check_root_writable(tmp_settings)
    assert rw_res.status == runtime_doctor.STATUS_PASS

    dd_res = runtime_doctor.check_data_dirs(tmp_settings, (".traces", ".agent-runs"))
    assert dd_res.status == runtime_doctor.STATUS_PASS

    # 7. Check static dir
    stat_missing = runtime_doctor.check_static_dir(tmp_settings / "nonexistent_static")
    assert stat_missing.status == runtime_doctor.STATUS_FAIL

    ui_dir = tmp_settings / "static" / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    (ui_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    stat_pass = runtime_doctor.check_static_dir(tmp_settings / "static")
    assert stat_pass.status == runtime_doctor.STATUS_PASS

    # 8. Check auth token file
    token_missing = runtime_doctor.check_token_file(tmp_settings / "token_empty_dir")
    assert token_missing.status == runtime_doctor.STATUS_WARN

    token_file = tmp_settings / ".auth-token"
    token_file.write_text("secret_auth_token_12345\n", encoding="utf-8")
    token_pass = runtime_doctor.check_token_file(tmp_settings)
    assert token_pass.status == runtime_doctor.STATUS_PASS

    # 9. Run full doctor offline
    opts = runtime_doctor.DoctorOptions(
        root=tmp_settings,
        static_dir=tmp_settings / "static",
        offline=True,
    )
    results = runtime_doctor.run_doctor(opts)
    assert len(results) > 5


def test_model_router_heuristics(tmp_settings: Path) -> None:
    # 1. Query complexity heuristics
    assert model_router.query_complexity("") == "neutral"
    assert model_router.query_complexity("hi") == "simple"
    assert model_router.query_complexity("帮我写一个复杂架构的代码分析并生成思维导图和架构图") == "complex"
    assert model_router.query_complexity("a" * 1500) == "complex"

    # 2. Token estimation
    assert model_router._estimate_payload_tokens({"messages": [{"role": "user", "content": "Hello world"}]}) > 0
    assert model_router._estimate_payload_tokens({}) == 0

    # 3. Route decision
    dec_explicit = model_router.route_request({"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "hello"}]})
    assert dec_explicit.model == "deepseek-v4-pro"
    assert dec_explicit.to_dict()["model"] == "deepseek-v4-pro"

    dec_auto = model_router.route_request({"model": "auto", "messages": [{"role": "user", "content": "hello"}]})
    assert dec_auto.auto is True
