from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from deepseek_infra.infra.browser import session


def _runner_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "evals" / "runners" / "run_browser_eval.py"
    spec = importlib.util.spec_from_file_location("browser_eval_runner_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_managed_browser_sessions_closes_runtime_before_profile_cleanup(monkeypatch) -> None:
    run_browser_eval = _runner_module()
    calls: list[str] = []
    monkeypatch.setattr(session, "reset_sessions_for_tests", lambda: calls.append("closed"))

    with run_browser_eval.managed_browser_sessions():
        calls.append("body")

    assert calls == ["body", "closed"]


def test_managed_browser_sessions_closes_after_failure(monkeypatch) -> None:
    run_browser_eval = _runner_module()
    calls: list[str] = []
    monkeypatch.setattr(session, "reset_sessions_for_tests", lambda: calls.append("closed"))

    try:
        with run_browser_eval.managed_browser_sessions():
            raise RuntimeError("eval failed")
    except RuntimeError:
        pass

    assert calls == ["closed"]
