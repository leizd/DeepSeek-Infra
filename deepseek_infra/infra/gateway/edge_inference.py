"""Optional edge inference backends and edge-cloud routing decisions."""

from __future__ import annotations

import importlib.util
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepseek_infra.core.config import settings
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import latest_user_query

logger = logging.getLogger("deepseek_infra.edge")

EDGE_LOCAL_MODES = {"local", "edge", "on"}
EDGE_CLOUD_MODES = {"cloud", "remote", "off", "none", "disabled"}
EDGE_DEFAULT_MODE = "auto"
LOCAL_MODEL_ID = "edge-local"
SUPPORTED_EDGE_PROVIDERS = {"llama_cpp", "mlc", "fake"}
COMPLEX_QUERY_RE = re.compile(
    r"```|\b(code|debug|bug|traceback|exception|leetcode|algorithm|proof|integral|derivative|matrix|equation|sql|regex|api|fastapi|flask)\b|"
    "\u4ee3\u7801|\u7f16\u7a0b|\u8c03\u8bd5|\u62a5\u9519|\u7b97\u6cd5|\u8bc1\u660e|\u6570\u5b66|\u79ef\u5206|\u5fae\u5206|\u65b9\u7a0b",
    re.IGNORECASE,
)
CURRENT_QUERY_RE = re.compile(
    r"\b(latest|today|current|news|price|weather|release|version|search|browse|internet|web)\b|"
    "\u6700\u65b0|\u4eca\u5929|\u65b0\u95fb|\u4ef7\u683c|\u5929\u6c14|\u641c\u7d22|\u8054\u7f51|\u6d4f\u89c8",
    re.IGNORECASE,
)
ARTIFACT_QUERY_RE = re.compile(r"\b(ppt|powerpoint|presentation|mindmap|mind map|docx|pdf)\b", re.IGNORECASE)
SIMPLE_TASK_RE = re.compile(
    r"\b(hi|hello|chat|summarize|summary|rewrite|polish|translate|explain|outline)\b|"
    "\u4f60\u597d|\u95f2\u804a|\u603b\u7ed3|\u6982\u62ec|\u63d0\u70bc|\u6539\u5199|\u6da6\u8272|\u7ffb\u8bd1|\u89e3\u91ca",
    re.IGNORECASE,
)
QUANTIZATION_RE = re.compile(r"(?:^|[-_.])(Q[234568](?:_[A-Z0-9]+)*)(?:[-_.]|$)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EdgeOptions:
    enabled: bool
    provider: str
    model_path: str
    model_name: str
    chat_format: str
    n_ctx: int
    n_threads: int
    n_gpu_layers: int
    max_tokens: int
    temperature: float
    top_p: float
    simple_max_chars: int


@dataclass(frozen=True, slots=True)
class EdgeRouteDecision:
    use_edge: bool
    reason: str
    mode: str
    provider: str
    status: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EdgeCompletion:
    content: str
    reasoning: str
    model: str
    usage: dict[str, Any]
    provider: str


class EdgeInferenceManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded_key: tuple[Any, ...] | None = None
        self._model: Any = None

    def status(self, options: EdgeOptions | None = None) -> dict[str, Any]:
        options = options or edge_options_from_payload({})
        provider_supported = options.provider in SUPPORTED_EDGE_PROVIDERS
        dependency_available = provider_dependency_available(options.provider)
        model_path_configured = bool(options.model_path) or options.provider == "fake"
        model_path_suffix_supported = model_path_suffix_supported_for(options)
        model_path_exists = model_path_available(options)
        available = bool(options.enabled and provider_supported and dependency_available and model_path_configured and model_path_exists)
        loaded_key = self._loaded_key
        loaded = bool(loaded_key and loaded_key[0] == options.provider and loaded_key[1] == options.model_path)
        status = {
            "enabled": options.enabled,
            "provider": options.provider,
            "providerSupported": provider_supported,
            "available": available,
            "dependencyAvailable": dependency_available,
            "modelPathConfigured": model_path_configured,
            "modelPathExists": model_path_exists,
            "modelPathSuffixSupported": model_path_suffix_supported,
            "modelPath": options.model_path,
            "modelName": options.model_name,
            "loaded": loaded,
            "quantization": infer_quantization(options.model_path),
            "nCtx": options.n_ctx,
            "nThreads": options.n_threads,
            "nGpuLayers": options.n_gpu_layers,
            "maxTokens": options.max_tokens,
            "temperature": options.temperature,
            "topP": options.top_p,
            "allowModelPathOverride": settings.edge.allow_model_path_override,
        }
        status["suggestions"] = edge_status_suggestions(status)
        return status

    def complete(self, messages: list[dict[str, Any]], options: EdgeOptions) -> EdgeCompletion:
        if options.provider == "fake":
            return fake_completion(messages, options)
        backend = self._load_backend(options)
        if options.provider == "llama_cpp":
            result = backend.create_chat_completion(
                messages=messages,
                max_tokens=options.max_tokens,
                temperature=options.temperature,
                top_p=options.top_p,
                stream=False,
            )
            return completion_from_openai_result(result, provider=options.provider, fallback_model=options.model_name, messages=messages)
        if options.provider == "mlc":
            result = backend.chat.completions.create(
                messages=messages,
                model=options.model_path,
                max_tokens=options.max_tokens,
                temperature=options.temperature,
                top_p=options.top_p,
                stream=False,
            )
            return completion_from_openai_result(result, provider=options.provider, fallback_model=options.model_name, messages=messages)
        raise AppError(f"Unsupported edge inference provider: {options.provider}", code=ErrorCode.INVALID_PAYLOAD)

    def stream(self, messages: list[dict[str, Any]], options: EdgeOptions) -> Any:
        if options.provider == "fake":
            completion = fake_completion(messages, options)
            if completion.content:
                yield completion.content
            return
        backend = self._load_backend(options)
        if options.provider == "llama_cpp":
            chunks = backend.create_chat_completion(
                messages=messages,
                max_tokens=options.max_tokens,
                temperature=options.temperature,
                top_p=options.top_p,
                stream=True,
            )
        elif options.provider == "mlc":
            chunks = backend.chat.completions.create(
                messages=messages,
                model=options.model_path,
                max_tokens=options.max_tokens,
                temperature=options.temperature,
                top_p=options.top_p,
                stream=True,
            )
        else:
            raise AppError(f"Unsupported edge inference provider: {options.provider}", code=ErrorCode.INVALID_PAYLOAD)

        for chunk in chunks:
            text = content_delta_from_chunk(chunk)
            if text:
                yield text

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._loaded_key = None

    def _load_backend(self, options: EdgeOptions) -> Any:
        status = self.status(options)
        if not status["available"]:
            raise AppError(edge_unavailable_message(status), code=ErrorCode.INVALID_PAYLOAD, status=409)
        key = (
            options.provider,
            options.model_path,
            options.chat_format,
            options.n_ctx,
            options.n_threads,
            options.n_gpu_layers,
        )
        with self._lock:
            if self._model is not None and self._loaded_key == key:
                return self._model
            if options.provider == "llama_cpp":
                self._model = load_llama_cpp_model(options)
            elif options.provider == "mlc":
                self._model = load_mlc_engine(options)
            else:
                raise AppError(f"Unsupported edge inference provider: {options.provider}", code=ErrorCode.INVALID_PAYLOAD)
            self._loaded_key = key
            logger.info("edge_model_loaded", extra={"provider": options.provider, "model_path": options.model_path})
            return self._model


edge_manager = EdgeInferenceManager()


def edge_options_from_payload(payload: dict[str, Any]) -> EdgeOptions:
    provider = normalize_provider(payload.get("edgeProvider") or settings.edge.provider)
    model_path = str(settings.edge.model_path or "").strip()
    override_path = str(payload.get("edgeModelPath") or "").strip()
    if override_path and settings.edge.allow_model_path_override:
        model_path = override_path
    if provider == "llama_cpp" and model_path:
        model_path = str(Path(model_path).expanduser().resolve())
    return EdgeOptions(
        enabled=settings.edge.enabled,
        provider=provider,
        model_path=model_path,
        model_name=str(payload.get("edgeModelName") or settings.edge.model_name or LOCAL_MODEL_ID),
        chat_format=str(payload.get("edgeChatFormat") or settings.edge.chat_format or ""),
        n_ctx=positive_int(payload.get("edgeNCtx"), settings.edge.n_ctx),
        n_threads=non_negative_int(payload.get("edgeNThreads"), settings.edge.n_threads),
        n_gpu_layers=non_negative_int(payload.get("edgeNGpuLayers"), settings.edge.n_gpu_layers),
        max_tokens=positive_int(payload.get("edgeMaxTokens"), settings.edge.max_tokens),
        temperature=float_value(payload.get("edgeTemperature"), settings.edge.temperature),
        top_p=float_value(payload.get("edgeTopP"), settings.edge.top_p),
        simple_max_chars=positive_int(payload.get("edgeSimpleMaxChars"), settings.edge.simple_max_chars),
    )


def edge_inference_status(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return edge_manager.status(edge_options_from_payload(payload or {}))


def edge_unload() -> dict[str, Any]:
    edge_manager.unload()
    return {"ok": True, "edgeInference": edge_inference_status()}


def select_edge_route(payload: dict[str, Any], *, cloud_available: bool) -> EdgeRouteDecision:
    options = edge_options_from_payload(payload)
    status = edge_manager.status(options)
    return decide_edge_route(payload, options=options, status=status, cloud_available=cloud_available)


def decide_edge_route(payload: dict[str, Any], *, options: EdgeOptions, status: dict[str, Any], cloud_available: bool) -> EdgeRouteDecision:
    mode = edge_mode(payload)
    provider = options.provider
    if mode in EDGE_CLOUD_MODES:
        return EdgeRouteDecision(False, "cloud_forced", mode, provider, status)
    if mode in EDGE_LOCAL_MODES:
        if not status["available"]:
            raise AppError(edge_unavailable_message(status), code=ErrorCode.INVALID_PAYLOAD, status=409)
        return EdgeRouteDecision(True, "local_forced", mode, provider, status)
    if not status["available"]:
        return EdgeRouteDecision(False, "edge_unavailable", mode, provider, status)
    if not edge_payload_supported(payload):
        return EdgeRouteDecision(False, "unsupported_payload", mode, provider, status)
    if not cloud_available and edge_simple_enough(payload, options):
        return EdgeRouteDecision(True, "cloud_unavailable_simple_local", mode, provider, status)
    if edge_simple_enough(payload, options):
        return EdgeRouteDecision(True, "simple_task_local", mode, provider, status)
    return EdgeRouteDecision(False, "complex_task_cloud", mode, provider, status)


def edge_route_preview(payload: dict[str, Any], *, cloud_available: bool) -> dict[str, Any]:
    route = select_edge_route(payload, cloud_available=cloud_available)
    return edge_route_decision_payload(route)


def edge_route_decision_payload(route: EdgeRouteDecision) -> dict[str, Any]:
    return {
        "useEdge": route.use_edge,
        "reason": route.reason,
        "mode": route.mode,
        "provider": route.provider,
        "status": route.status,
    }


def edge_mode(payload: dict[str, Any]) -> str:
    raw = str(payload.get("edgeMode") or payload.get("localMode") or settings.edge.mode or EDGE_DEFAULT_MODE).strip().lower()
    return raw or EDGE_DEFAULT_MODE


def edge_payload_supported(payload: dict[str, Any]) -> bool:
    if payload.get("agentMode") is True:
        return False
    if str(payload.get("searchMode") or "").strip().lower() in {"on", "force", "true", "1"}:
        return False
    if has_image_attachment(payload):
        return False
    return True


def edge_simple_enough(payload: dict[str, Any], options: EdgeOptions) -> bool:
    query = latest_user_query(payload)
    if not query:
        return False
    if len(query) > options.simple_max_chars:
        return False
    if CURRENT_QUERY_RE.search(query) or COMPLEX_QUERY_RE.search(query) or ARTIFACT_QUERY_RE.search(query):
        return False
    if SIMPLE_TASK_RE.search(query):
        return True
    return len(query) <= 800 and len(chat_messages_from_payload(payload)) <= 8


def has_image_attachment(payload: dict[str, Any]) -> bool:
    for message in chat_messages_from_payload(payload):
        attachments = message.get("attachments")
        if not isinstance(attachments, list):
            continue
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            if str(attachment.get("imageData") or "").startswith("data:image/"):
                return True
    return False


def chat_messages_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, dict)]


def provider_dependency_available(provider: str) -> bool:
    if provider == "llama_cpp":
        return importlib.util.find_spec("llama_cpp") is not None
    if provider == "mlc":
        return importlib.util.find_spec("mlc_llm") is not None
    if provider == "fake":
        return True
    return False


def model_path_available(options: EdgeOptions) -> bool:
    if options.provider == "fake":
        return True
    if not options.model_path:
        return False
    if options.provider == "llama_cpp":
        return Path(options.model_path).is_file() and Path(options.model_path).suffix.lower() == ".gguf"
    if options.provider == "mlc":
        return True
    return False


def model_path_suffix_supported_for(options: EdgeOptions) -> bool:
    if options.provider == "llama_cpp" and options.model_path:
        return Path(options.model_path).suffix.lower() == ".gguf"
    return True


def load_llama_cpp_model(options: EdgeOptions) -> Any:
    from llama_cpp import Llama

    kwargs: dict[str, Any] = {
        "model_path": options.model_path,
        "n_ctx": options.n_ctx,
        "n_gpu_layers": options.n_gpu_layers,
        "verbose": False,
    }
    if options.n_threads > 0:
        kwargs["n_threads"] = options.n_threads
    if options.chat_format:
        kwargs["chat_format"] = options.chat_format
    return Llama(**kwargs)


def load_mlc_engine(options: EdgeOptions) -> Any:
    from mlc_llm import MLCEngine

    return MLCEngine(model=options.model_path)


def fake_completion(messages: list[dict[str, Any]], options: EdgeOptions) -> EdgeCompletion:
    query = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            query = str(message.get("content") or "")
            break
    content = f"[edge fake] {query[:120].strip() or 'ok'}"
    return EdgeCompletion(
        content=content,
        reasoning="",
        model=options.model_name or "edge-fake",
        usage=estimated_usage(messages, content),
        provider=options.provider,
    )


def completion_from_openai_result(result: Any, *, provider: str, fallback_model: str, messages: list[dict[str, Any]]) -> EdgeCompletion:
    data = object_to_dict(result)
    choices = data.get("choices") or []
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    raw_message = first_choice.get("message")
    message = raw_message if isinstance(raw_message, dict) else {}
    raw_delta = first_choice.get("delta")
    delta = raw_delta if isinstance(raw_delta, dict) else {}
    content = str(message.get("content") or delta.get("content") or "")
    reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")
    raw_usage = data.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else estimated_usage(messages, content)
    return EdgeCompletion(
        content=content,
        reasoning=reasoning,
        model=str(data.get("model") or fallback_model or LOCAL_MODEL_ID),
        usage=usage,
        provider=provider,
    )


def content_delta_from_chunk(chunk: Any) -> str:
    data = object_to_dict(chunk)
    choices = data.get("choices") or []
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    raw_delta = first_choice.get("delta")
    delta = raw_delta if isinstance(raw_delta, dict) else {}
    raw_message = first_choice.get("message")
    message = raw_message if isinstance(raw_message, dict) else {}
    return str(delta.get("content") or message.get("content") or "")


def object_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dumped if isinstance(dumped, dict) else {}
    result: dict[str, Any] = {}
    for name in ("id", "model", "choices", "usage"):
        if hasattr(value, name):
            result[name] = getattr(value, name)
    return result


def estimated_usage(messages: list[dict[str, Any]], content: str) -> dict[str, int]:
    prompt_chars = sum(len(str(message.get("content") or "")) for message in messages)
    completion_chars = len(str(content or ""))
    prompt_tokens = max(1, prompt_chars // 4)
    completion_tokens = max(1, completion_chars // 4) if completion_chars else 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def edge_unavailable_message(status: dict[str, Any]) -> str:
    if not status.get("enabled"):
        return "Edge inference is disabled. Set EDGE_INFERENCE_ENABLED=1 to enable local model routing."
    if not status.get("providerSupported", True):
        provider = status.get("provider") or "unknown"
        return f"Unsupported edge inference provider: {provider}. Use llama_cpp, mlc, or fake."
    if not status.get("dependencyAvailable"):
        provider = status.get("provider") or "llama_cpp"
        requirement = "llama-cpp-python" if provider == "llama_cpp" else "mlc-llm"
        return f"Edge inference dependency is not installed. Install {requirement} for provider {provider}."
    if not status.get("modelPathConfigured"):
        return "Edge model path is not configured. Set EDGE_MODEL_PATH to a local model path."
    if not status.get("modelPathSuffixSupported", True):
        return "Edge model path must point to a .gguf file for the llama_cpp provider."
    if not status.get("modelPathExists"):
        return "Edge model path does not exist or is not a supported GGUF file."
    return "Edge inference is unavailable."


def edge_status_suggestions(status: dict[str, Any]) -> list[str]:
    suggestions: list[str] = []
    provider_supported = bool(status.get("providerSupported", True))
    if not status.get("enabled"):
        suggestions.append("Set EDGE_INFERENCE_ENABLED=1 to enable local model routing.")
    if not provider_supported:
        suggestions.append("Set EDGE_PROVIDER or EDGE_INFERENCE_PROVIDER to llama_cpp, mlc, or fake.")
    if status.get("enabled") and provider_supported and not status.get("dependencyAvailable"):
        provider = str(status.get("provider") or "llama_cpp")
        requirement = "llama-cpp-python" if provider == "llama_cpp" else "mlc-llm"
        suggestions.append(f"Install {requirement} or choose another edge provider.")
    if status.get("enabled") and not status.get("modelPathConfigured"):
        suggestions.append("Set EDGE_MODEL_PATH to a local model file.")
    if status.get("enabled") and not status.get("modelPathSuffixSupported", True):
        suggestions.append("Use a .gguf model file with the llama_cpp provider.")
    if status.get("enabled") and status.get("modelPathConfigured") and not status.get("modelPathExists"):
        suggestions.append("Check EDGE_MODEL_PATH exists and is readable.")
    return suggestions


def infer_quantization(model_path: str) -> str:
    match = QUANTIZATION_RE.search(Path(str(model_path or "")).name)
    return match.group(1).upper() if match else ""


def normalize_provider(value: object) -> str:
    raw = str(value or "llama_cpp").strip().lower().replace("-", "_")
    if raw in {"llama", "llamacpp", "llama_cpp", "gguf"}:
        return "llama_cpp"
    if raw in {"mlc", "mlc_llm", "mlcllm"}:
        return "mlc"
    if raw in {"fake", "test", "dry_run", "dryrun"}:
        return "fake"
    return raw


def positive_int(value: object, default: int) -> int:
    try:
        return max(1, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return max(1, int(default))


def non_negative_int(value: object, default: int) -> int:
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return max(0, int(default))


def float_value(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(default)
