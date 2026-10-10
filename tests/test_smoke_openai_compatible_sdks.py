from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_smoke() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_openai_compatible_sdks.py"
    spec = importlib.util.spec_from_file_location("smoke_openai_compatible_sdks_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _passing_steps() -> list[dict[str, Any]]:
    return [
        {"name": "openai.healthz", "status": "pass", "detail": "ok", "data": {}},
        {"name": "sdk.langchain.models", "status": "pass", "detail": "3 models", "data": {}},
        {"name": "sdk.langchain.chat", "status": "pass", "detail": "ok", "data": {}},
        {"name": "sdk.langchain.stream", "status": "pass", "detail": "5 chunks", "data": {}},
        {"name": "sdk.litellm.models", "status": "pass", "detail": "3 models", "data": {}},
        {"name": "sdk.litellm.chat", "status": "pass", "detail": "ok", "data": {}},
        {"name": "sdk.litellm.stream", "status": "pass", "detail": "5 chunks", "data": {}},
        {"name": "sdk.llamaindex.chat", "status": "pass", "detail": "ok", "data": {}},
    ]


def test_sdk_evidence_passes_with_all_required_checks() -> None:
    smoke = _load_smoke()
    sdks = {
        "langchain": {"modelsList": "PASS", "chatCompletion": "PASS", "streaming": "PASS"},
        "litellm": {"modelsList": "PASS", "chatCompletion": "PASS", "streaming": "PASS"},
        "llamaindex": {"chatCompletion": "PASS"},
    }
    evidence = smoke.build_evidence(
        [smoke.StepResult(**s) for s in _passing_steps()],
        sdks,
        "http://127.0.0.1:8000/v1",
        "deepseek-v4-pro",
    )

    assert evidence["schemaVersion"] == "openai-compatible-sdks-evidence.v1"
    assert evidence["status"] == "PASS"
    assert evidence["sdks"] == sdks
    assert "commit" in evidence
    assert {"os", "python", "ci"}.issubset(evidence["environment"])


def test_sdk_evidence_fails_when_any_check_fails() -> None:
    smoke = _load_smoke()
    sdks = {
        "langchain": {"modelsList": "PASS", "chatCompletion": "FAIL", "streaming": "PASS"},
        "litellm": {"modelsList": "PASS", "chatCompletion": "PASS", "streaming": "PASS"},
        "llamaindex": {"chatCompletion": "PASS"},
    }
    evidence = smoke.build_evidence(
        [smoke.StepResult(**s) for s in _passing_steps()],
        sdks,
        "http://127.0.0.1:8000/v1",
        "deepseek-v4-pro",
    )

    assert evidence["status"] == "FAIL"


def test_sdk_evidence_all_skipped_is_fail() -> None:
    smoke = _load_smoke()
    sdks = {
        "langchain": {"modelsList": "SKIPPED", "chatCompletion": "SKIPPED", "streaming": "SKIPPED"},
        "litellm": {"modelsList": "SKIPPED", "chatCompletion": "SKIPPED", "streaming": "SKIPPED"},
        "llamaindex": {"chatCompletion": "SKIPPED"},
    }
    evidence = smoke.build_evidence(
        [smoke.StepResult(name="openai.healthz", status="warn", detail="all skipped", data={})],
        sdks,
        "http://127.0.0.1:8000/v1",
        "deepseek-v4-pro",
    )

    assert evidence["status"] == "FAIL"


def test_sdk_evidence_markdown_output(monkeypatch: Any, tmp_path: Path) -> None:
    smoke = _load_smoke()
    out = tmp_path / "openai-compatible-sdks.json"
    md = tmp_path / "openai-compatible-sdks.md"

    monkeypatch.setattr(smoke, "app_version", lambda: "2.4.6")
    monkeypatch.setattr(smoke, "git_value", lambda *args: "abc1234" if args[:2] == ("rev-parse", "--short") else "")
    monkeypatch.setattr(smoke, "utc_now", lambda: "2026-06-28T00:00:00Z")
    monkeypatch.setattr(smoke, "build_environment", lambda: {"os": "Linux", "python": "3.12", "ci": True})

    def fake_request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if url.endswith("/models"):
            return {"object": "list", "data": [{"id": "deepseek-v4-pro", "object": "model"}]}
        if url.endswith("/chat/completions"):
            return {"choices": [{"message": {"content": "Hello"}}], "usage": {"total_tokens": 10}}
        return {}

    monkeypatch.setattr(smoke, "request_json", fake_request_json)

    def fake_openai_chat_stream(base_url: str, token: str, model: str, timeout: int) -> list[dict[str, Any]]:
        return [{"choices": [{"delta": {"content": "Hello"}}]}]

    monkeypatch.setattr(smoke, "_openai_chat_stream", fake_openai_chat_stream)

    def fake_try_langchain(args: Any, token: str, steps: list[Any], sdks: dict[str, Any]) -> None:
        sdks["langchain"] = {"modelsList": "PASS", "chatCompletion": "PASS", "streaming": "PASS"}
        steps.append(smoke.StepResult(name="sdk.langchain.chat", status="pass", detail="ok", data={}))

    def fake_try_litellm(args: Any, token: str, steps: list[Any], sdks: dict[str, Any]) -> None:
        sdks["litellm"] = {"modelsList": "PASS", "chatCompletion": "PASS", "streaming": "PASS"}
        steps.append(smoke.StepResult(name="sdk.litellm.chat", status="pass", detail="ok", data={}))

    def fake_try_llamaindex(args: Any, token: str, steps: list[Any], sdks: dict[str, Any]) -> None:
        sdks["llamaindex"] = {"chatCompletion": "PASS"}
        steps.append(smoke.StepResult(name="sdk.llamaindex.chat", status="pass", detail="ok", data={}))

    monkeypatch.setattr(smoke, "_try_langchain", fake_try_langchain)
    monkeypatch.setattr(smoke, "_try_litellm", fake_try_litellm)
    monkeypatch.setattr(smoke, "_try_llamaindex", fake_try_llamaindex)

    exit_code = smoke.main(["--base-url", "http://127.0.0.1:8000/v1", "--model", "deepseek-v4-pro", "--out", str(out), "--markdown", str(md)])
    assert exit_code == 0

    evidence = json.loads(out.read_text(encoding="utf-8"))
    assert evidence["version"] == "2.4.6"
    assert evidence["status"] == "PASS"
    assert evidence["sdks"]["langchain"]["chatCompletion"] == "PASS"

    md_text = md.read_text(encoding="utf-8")
    assert "OpenAI-Compatible SDKs Smoke Evidence" in md_text
    assert "langchain" in md_text
    assert "litellm" in md_text
    assert "llamaindex" in md_text


def test_sdk_smoke_skipped_when_not_installed(monkeypatch: Any) -> None:
    smoke = _load_smoke()
    steps: list[Any] = []
    sdks: dict[str, Any] = {}

    monkeypatch.setattr(smoke, "_openai_models", lambda *a, **kw: [])
    monkeypatch.setattr(smoke, "_openai_chat", lambda *a, **kw: {})

    smoke._try_langchain(smoke.parse_args(["--base-url", "http://127.0.0.1:8000/v1", "--model", "x"]), "", steps, sdks)

    assert sdks["langchain"] == {"modelsList": "SKIPPED", "chatCompletion": "SKIPPED", "streaming": "SKIPPED"}


def test_sdk_evidence_schema_tracks_required_sdks() -> None:
    schema = json.loads(Path("evals/schemas/openai_compatible_sdks_evidence.schema.json").read_text(encoding="utf-8"))
    required_sdks = schema["properties"]["sdks"]["required"]

    assert required_sdks == ["langchain", "litellm", "llamaindex"]


def test_sdk_evidence_schema_tracks_langchain_checks() -> None:
    schema = json.loads(Path("evals/schemas/openai_compatible_sdks_evidence.schema.json").read_text(encoding="utf-8"))
    langchain_checks = schema["properties"]["sdks"]["properties"]["langchain"]["required"]

    assert langchain_checks == ["modelsList", "chatCompletion", "streaming"]


def test_sdk_evidence_schema_tracks_llamaindex_checks() -> None:
    schema = json.loads(Path("evals/schemas/openai_compatible_sdks_evidence.schema.json").read_text(encoding="utf-8"))
    llamaindex_checks = schema["properties"]["sdks"]["properties"]["llamaindex"]["required"]

    assert llamaindex_checks == ["chatCompletion"]
