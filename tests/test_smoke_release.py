from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


def _load_smoke_release() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_release.py"
    spec = importlib.util.spec_from_file_location("smoke_release_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _names(stages: list[tuple[str, list[str]]]) -> list[str]:
    return [name for name, _ in stages]


def test_offline_mode_runs_doctor_evals_and_agent_only() -> None:
    mod = _load_smoke_release()
    args = mod.parse_args(["--offline"])
    stages = mod.build_stages(args)
    names = _names(stages)
    assert names == [
        "doctor",
        "workspace_core",
        "skill_system",
        "skill_workbench_ui",
        "skill_builder",
        "skill_packs",
        "skill_eval_dashboard",
        "offline_eval_suite",
        "security_corpus",
        "agent_eval",
        "baseline_compare",
    ]
    doctor_cmd = stages[0][1]
    assert "--offline" in doctor_cmd
    assert "--with-server" not in doctor_cmd
    assert not any("smoke_mcp_compat" in " ".join(cmd) for _, cmd in stages)
    assert not any("smoke_a2a_compat" in " ".join(cmd) for _, cmd in stages)


def test_with_server_mode_includes_protocol_smokes() -> None:
    mod = _load_smoke_release()
    args = mod.parse_args(["--with-server", "--base-url", "http://127.0.0.1:9000", "--token", "tok"])
    stages = mod.build_stages(args)
    names = _names(stages)
    assert names == [
        "doctor",
        "workspace_core",
        "skill_system",
        "skill_workbench_ui",
        "skill_builder",
        "skill_packs",
        "skill_eval_dashboard",
        "offline_eval_suite",
        "security_corpus",
        "agent_eval",
        "baseline_compare",
        "mcp_smoke",
        "a2a_smoke",
    ]
    doctor_cmd = stages[0][1]
    assert "--with-server" in doctor_cmd
    assert "--base-url" in doctor_cmd
    mcp_cmd = " ".join(stages[11][1])
    assert "--mcp-url" in mcp_cmd
    assert "http://127.0.0.1:9000/mcp" in mcp_cmd
    assert "tok" in mcp_cmd
    a2a_cmd = " ".join(stages[12][1])
    assert "--base-url" in a2a_cmd
    assert "http://127.0.0.1:9000" in a2a_cmd


def test_default_mode_is_offline() -> None:
    mod = _load_smoke_release()
    args = mod.parse_args([])
    assert args.offline is True
    assert args.with_server is False
    assert _names(mod.build_stages(args)) == [
        "doctor",
        "workspace_core",
        "skill_system",
        "skill_workbench_ui",
        "skill_builder",
        "skill_packs",
        "skill_eval_dashboard",
        "offline_eval_suite",
        "security_corpus",
        "agent_eval",
        "baseline_compare",
    ]


def test_skip_flags_drop_stages() -> None:
    mod = _load_smoke_release()
    args = mod.parse_args(["--offline", "--skip-doctor", "--skip-agent", "--skip-skill-builder", "--skip-skill-eval-dashboard"])
    assert _names(mod.build_stages(args)) == ["workspace_core", "skill_system", "skill_workbench_ui", "skill_packs", "offline_eval_suite", "security_corpus", "baseline_compare"]


def test_with_server_skip_protocol_keeps_evals() -> None:
    mod = _load_smoke_release()
    args = mod.parse_args(["--with-server", "--skip-mcp", "--skip-a2a", "--skip-doctor"])
    assert _names(mod.build_stages(args)) == ["workspace_core", "skill_system", "skill_workbench_ui", "skill_builder", "skill_packs", "skill_eval_dashboard", "offline_eval_suite", "security_corpus", "agent_eval", "baseline_compare"]


def test_json_mode_emits_plan_without_running(capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load_smoke_release()
    code = mod.main(["--with-server", "--base-url", "http://127.0.0.1:8000", "--token", "t", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    assert payload["mode"] == "with-server"
    stage_names = [stage["name"] for stage in payload["stages"]]
    assert stage_names == [
        "doctor",
        "workspace_core",
        "skill_system",
        "skill_workbench_ui",
        "skill_builder",
        "skill_packs",
        "skill_eval_dashboard",
        "offline_eval_suite",
        "security_corpus",
        "agent_eval",
        "baseline_compare",
        "mcp_smoke",
        "a2a_smoke",
    ]
    assert all(isinstance(stage["command"], list) for stage in payload["stages"])


def test_offline_and_with_server_are_mutually_exclusive() -> None:
    mod = _load_smoke_release()
    with pytest.raises(SystemExit):
        mod.parse_args(["--offline", "--with-server"])
