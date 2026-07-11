from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.gateway import edge_inference as edge


def options(provider: str = "fake", *, enabled: bool = True, model_path: str = "") -> edge.EdgeOptions:
    return edge.EdgeOptions(
        enabled=enabled,
        provider=provider,
        model_path=model_path,
        model_name="edge-model",
        chat_format="chatml",
        n_ctx=2048,
        n_threads=2,
        n_gpu_layers=1,
        max_tokens=64,
        temperature=0.2,
        top_p=0.9,
        simple_max_chars=100,
    )


def messages(text: str = "hello") -> list[dict[str, Any]]:
    return [{"role": "system", "content": "brief"}, {"role": "user", "content": text}]


def test_complete_fake_llama_mlc_and_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = edge.EdgeInferenceManager()
    fake = manager.complete(messages(), options())
    assert fake.content == "[edge fake] hello"
    assert fake.usage["total_tokens"] > 0

    llama = SimpleNamespace(
        create_chat_completion=lambda **kwargs: {
            "model": "llama",
            "choices": [{"message": {"content": "llama answer", "reasoning_content": "why"}}],
            "usage": {"total_tokens": 4},
        }
    )
    monkeypatch.setattr(manager, "_load_backend", lambda current: llama)
    completed = manager.complete(messages(), options("llama_cpp", model_path="model.gguf"))
    assert (completed.content, completed.reasoning, completed.model) == ("llama answer", "why", "llama")

    def create(**kwargs: Any) -> dict[str, Any]:
        return {"choices": [{"delta": {"content": "mlc answer"}}]}

    mlc = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(manager, "_load_backend", lambda current: mlc)
    assert manager.complete(messages(), options("mlc", model_path="model" )).content == "mlc answer"

    monkeypatch.setattr(manager, "_load_backend", lambda current: object())
    with pytest.raises(AppError, match="Unsupported edge inference provider"):
        manager.complete(messages(), options("unknown"))


def test_stream_all_providers_and_invalid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = edge.EdgeInferenceManager()
    assert list(manager.stream(messages(""), options())) == ["[edge fake] ok"]

    llama = SimpleNamespace(
        create_chat_completion=lambda **kwargs: [
            {"choices": [{"delta": {"content": "one"}}]},
            {"choices": [{"message": {"content": ""}}]},
            SimpleNamespace(choices=[{"message": {"content": "two"}}]),
        ]
    )
    monkeypatch.setattr(manager, "_load_backend", lambda current: llama)
    assert list(manager.stream(messages(), options("llama_cpp", model_path="model.gguf"))) == ["one", "two"]

    mlc = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: [{"choices": [{"delta": {"content": "mlc"}}]}])))
    monkeypatch.setattr(manager, "_load_backend", lambda current: mlc)
    assert list(manager.stream(messages(), options("mlc", model_path="model"))) == ["mlc"]

    monkeypatch.setattr(manager, "_load_backend", lambda current: object())
    with pytest.raises(AppError, match="Unsupported edge inference provider"):
        list(manager.stream(messages(), options("broken")))


def test_backend_load_failure_cache_reload_and_release(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = edge.EdgeInferenceManager()
    unavailable = options("llama_cpp", model_path="missing.gguf")
    monkeypatch.setattr(manager, "status", lambda current: {"available": False, "enabled": True, "dependencyAvailable": True, "modelPathExists": False})
    with pytest.raises(AppError) as exc:
        manager._load_backend(unavailable)
    assert exc.value.status == 409

    loaded: list[str] = []
    monkeypatch.setattr(manager, "status", lambda current: {"available": True})
    def load_llama(current: edge.EdgeOptions) -> object:
        loaded.append("llama")
        return object()

    monkeypatch.setattr(edge, "load_llama_cpp_model", load_llama)
    first = manager._load_backend(unavailable)
    assert manager._load_backend(unavailable) is first
    assert loaded == ["llama"]

    def load_mlc(current: edge.EdgeOptions) -> object:
        loaded.append("mlc")
        return object()

    monkeypatch.setattr(edge, "load_mlc_engine", load_mlc)
    manager._load_backend(options("mlc", model_path="mlc-model"))
    assert loaded[-1] == "mlc"
    with pytest.raises(AppError):
        manager._load_backend(options("other"))

    manager.unload()
    assert manager._model is None
    assert manager._loaded_key is None


def test_load_optional_backend_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    llama_calls: list[dict[str, Any]] = []
    llama_module = ModuleType("llama_cpp")

    def llama_factory(**kwargs: Any) -> str:
        llama_calls.append(kwargs)
        return "llama"

    llama_module.Llama = llama_factory  # type: ignore[attr-defined]
    mlc_module = ModuleType("mlc_llm")
    mlc_module.MLCEngine = lambda **kwargs: ("mlc", kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", llama_module)
    monkeypatch.setitem(sys.modules, "mlc_llm", mlc_module)

    assert edge.load_llama_cpp_model(options("llama_cpp", model_path="model.gguf")) == "llama"
    assert llama_calls[0]["n_threads"] == 2
    no_optional = options("llama_cpp", model_path="model.gguf")
    no_optional = edge.EdgeOptions(**{**no_optional.__dict__, "n_threads": 0, "chat_format": ""}) if hasattr(no_optional, "__dict__") else edge.EdgeOptions(
        True, "llama_cpp", "model.gguf", "edge-model", "", 2048, 0, 1, 64, 0.2, 0.9, 100
    )
    edge.load_llama_cpp_model(no_optional)
    assert "n_threads" not in llama_calls[-1]
    assert "chat_format" not in llama_calls[-1]
    assert edge.load_mlc_engine(options("mlc", model_path="mlc-model")) == ("mlc", {"model": "mlc-model"})


def test_result_normalization_handles_dict_models_and_bad_shapes() -> None:
    class Dumpable:
        def model_dump(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "dumped"}}]}

    class Legacy:
        def dict(self) -> dict[str, Any]:
            return {"choices": [{"delta": {"content": "legacy"}}]}

    class Attributes:
        id = "id"
        model = "attribute-model"
        choices: list[object] = []
        usage: dict[str, object] = {}

    class BadDump:
        def model_dump(self) -> str:
            return "bad"

    assert edge.object_to_dict({"ok": True}) == {"ok": True}
    assert edge.object_to_dict(Dumpable())["choices"]
    assert edge.object_to_dict(Legacy())["choices"]
    assert edge.object_to_dict(Attributes())["model"] == "attribute-model"
    assert edge.object_to_dict(BadDump()) == {}
    assert edge.content_delta_from_chunk(Dumpable()) == "dumped"
    assert edge.content_delta_from_chunk({"choices": ["bad"]}) == ""

    completion = edge.completion_from_openai_result({"choices": "bad", "usage": "bad"}, provider="mlc", fallback_model="fallback", messages=[])
    assert completion.content == ""
    assert completion.model == "fallback"
    assert completion.usage["prompt_tokens"] == 1


def test_routing_empty_input_unsupported_payload_and_cpu_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    available = {"available": True}
    fake = options()
    assert edge.decide_edge_route({"edgeMode": "cloud"}, options=fake, status=available, cloud_available=False).reason == "cloud_forced"
    assert edge.decide_edge_route({}, options=fake, status={"available": False}, cloud_available=True).reason == "edge_unavailable"
    assert edge.decide_edge_route({"agentMode": True}, options=fake, status=available, cloud_available=True).reason == "unsupported_payload"
    assert edge.decide_edge_route({"searchMode": "force"}, options=fake, status=available, cloud_available=True).reason == "unsupported_payload"
    assert edge.edge_simple_enough({}, fake) is False
    assert edge.edge_simple_enough({"messages": [{"role": "user", "content": "x" * 101}]}, fake) is False
    assert edge.edge_simple_enough({"messages": [{"role": "user", "content": "debug code"}]}, fake) is False
    assert edge.edge_simple_enough({"messages": [{"role": "user", "content": "hello"}]}, fake) is True
    assert edge.chat_messages_from_payload({"messages": "bad"}) == []
    assert edge.has_image_attachment({"messages": [{"attachments": ["bad", {"imageData": "text"}]}]}) is False

    monkeypatch.setattr(edge, "select_edge_route", lambda payload, cloud_available: edge.EdgeRouteDecision(True, "cpu_fallback", "auto", "fake", {}))
    assert edge.edge_route_preview({}, cloud_available=False)["reason"] == "cpu_fallback"
    monkeypatch.setattr(edge.edge_manager, "unload", lambda: None)
    monkeypatch.setattr(edge, "edge_inference_status", lambda: {"available": False})
    assert edge.edge_unload()["edgeInference"] == {"available": False}


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        ({"enabled": False}, "disabled"),
        ({"enabled": True, "providerSupported": False, "provider": "bad"}, "Unsupported"),
        ({"enabled": True, "dependencyAvailable": False, "provider": "llama_cpp"}, "llama-cpp-python"),
        ({"enabled": True, "dependencyAvailable": True, "modelPathConfigured": False}, "not configured"),
        ({"enabled": True, "dependencyAvailable": True, "modelPathConfigured": True, "modelPathSuffixSupported": False}, ".gguf"),
        ({"enabled": True, "dependencyAvailable": True, "modelPathConfigured": True, "modelPathExists": False}, "does not exist"),
        ({"enabled": True, "dependencyAvailable": True, "modelPathConfigured": True, "modelPathExists": True}, "unavailable"),
    ],
)
def test_unavailable_messages(status: dict[str, Any], fragment: str) -> None:
    assert fragment in edge.edge_unavailable_message(status)


def test_status_suggestions_dependency_path_suffix_and_missing_file() -> None:
    status = {
        "enabled": True,
        "provider": "mlc",
        "providerSupported": True,
        "dependencyAvailable": False,
        "modelPathConfigured": True,
        "modelPathSuffixSupported": False,
        "modelPathExists": False,
    }
    suggestions = edge.edge_status_suggestions(status)
    assert any("mlc-llm" in item for item in suggestions)
    assert any(".gguf" in item for item in suggestions)
    assert any("exists" in item for item in suggestions)
    assert edge.edge_status_suggestions({"enabled": False, "providerSupported": False}) == [
        "Set EDGE_INFERENCE_ENABLED=1 to enable local model routing.",
        "Set EDGE_PROVIDER or EDGE_INFERENCE_PROVIDER to llama_cpp, mlc, or fake.",
    ]


def test_provider_model_and_numeric_validation(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    model = tmp_path / "model.Q4_K_M.gguf"
    model.write_bytes(b"model")
    assert edge.normalize_provider("llama-cpp") == "llama_cpp"
    assert edge.normalize_provider("MLC_LLM") == "mlc"
    assert edge.normalize_provider("dry-run") == "fake"
    assert edge.normalize_provider("custom") == "custom"
    assert edge.model_path_available(options("llama_cpp", model_path=str(model))) is True
    assert edge.model_path_available(options("llama_cpp", model_path=str(model.with_suffix(".bin")))) is False
    assert edge.model_path_available(options("mlc", model_path="model")) is True
    assert edge.model_path_available(options("other", model_path="model")) is False
    assert edge.model_path_suffix_supported_for(options("llama_cpp", model_path="bad.bin")) is False
    assert edge.infer_quantization(str(model)) == "Q4_K_M"
    assert edge.infer_quantization("plain.gguf") == ""
    assert edge.positive_int("bad", 3) == 3
    assert edge.non_negative_int(-2, 3) == 0
    assert edge.non_negative_int("bad", 3) == 3
    assert edge.float_value("bad", 0.5) == 0.5
    monkeypatch.setattr(edge.importlib.util, "find_spec", lambda name: object() if name == "llama_cpp" else None)
    assert edge.provider_dependency_available("llama_cpp") is True
    assert edge.provider_dependency_available("mlc") is False
    assert edge.provider_dependency_available("fake") is True
    assert edge.provider_dependency_available("other") is False


def test_estimated_usage_and_fake_completion_empty_content() -> None:
    usage = edge.estimated_usage([], "")
    assert usage == {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}
    completion = edge.fake_completion([{"role": "assistant", "content": "ignored"}], options())
    assert completion.content == "[edge fake] ok"
