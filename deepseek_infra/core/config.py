"""Runtime settings, compatibility constants, and JSON logging setup."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


def _runtime_root() -> Path:
    """Directory where writable user data lives (auth token, caches, projects).

    When packaged with PyInstaller we put it next to the executable so the data
    survives across runs; ``sys._MEIPASS`` is a per-invocation temp dir and is
    not safe to write to.

    ``DEEPSEEK_INFRA_ROOT`` is preferred; ``DEEPSEEK_MOBILE_ROOT`` is kept for
    backward compatibility (v2.1.6 → future).
    """
    env_root = (
        os.environ.get("DEEPSEEK_INFRA_ROOT", "").strip()
        or os.environ.get("DEEPSEEK_MOBILE_ROOT", "").strip()
    )
    if env_root:
        return Path(env_root).expanduser().resolve()
    if getattr(sys, "frozen", False) and hasattr(sys, "executable"):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _bundled_static_dir() -> Path:
    """Directory where read-only static assets live (frozen bundle vs. repo)."""
    env_static_dir = (
        os.environ.get("DEEPSEEK_INFRA_STATIC_DIR", "").strip()
        or os.environ.get("DEEPSEEK_MOBILE_STATIC_DIR", "").strip()
    )
    if env_static_dir:
        return Path(env_static_dir).expanduser().resolve()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "static"
    return _runtime_root() / "static"


ROOT = _runtime_root()


@dataclass(frozen=True, slots=True)
class SearchSettings:
    result_limit: int = 15
    round_limit: int = 3
    content_chars: int = 1200
    raw_content_chars: int = 3500
    context_result_limit: int = 24
    cache_max_age_seconds: int = 1800

    @property
    def total_result_limit(self) -> int:
        return self.result_limit * self.round_limit


@dataclass(frozen=True, slots=True)
class FileSettings:
    upload_file_max_bytes: int = 200_000_000
    upload_max_bytes: int = 220_000_000
    cache_max_age_days: int = 14
    cache_max_bytes: int = 500_000_000
    chunk_chars: int = 6000
    chunk_overlap: int = 400
    full_context_limit: int = 60_000
    context_char_budget: int = 115_000
    context_max_chunks: int = 18
    preview_chars: int = 1800
    max_zip_entry_bytes: int = 20_000_000
    max_zip_total_bytes: int = 120_000_000


@dataclass(frozen=True, slots=True)
class ContextSettings:
    compress_max_input_chars: int = 90_000
    summary_max_chars: int = 12_000
    compress_model: str = "deepseek-v4-flash"


@dataclass(frozen=True, slots=True)
class MemorySettings:
    context_char_budget: int = 8_000
    retrieve_limit: int = 12
    max_items: int = 400


@dataclass(frozen=True, slots=True)
class AuthSettings:
    enabled: bool = True
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    allowed_hosts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OCRSettings:
    enabled: bool = False
    mode: str = "balanced"
    pdf_dpi: int = 300
    max_image_pixels: int = 16_000_000
    formula_cmd: str = ""
    formula_timeout_seconds: int = 120


@dataclass(frozen=True, slots=True)
class EdgeInferenceSettings:
    enabled: bool = False
    provider: str = "llama_cpp"
    model_path: str = ""
    model_name: str = "deepseek-r1-distill-local"
    chat_format: str = ""
    allow_model_path_override: bool = False
    n_ctx: int = 4096
    n_threads: int = 0
    n_gpu_layers: int = 0
    max_tokens: int = 1024
    temperature: float = 0.7
    top_p: float = 0.95
    simple_max_chars: int = 6000


@dataclass(frozen=True, slots=True)
class LocalRAGSettings:
    enabled: bool = True
    backend: str = "sqlite_vec"
    embedding_provider: str = "hash"
    embedding_model_path: str = ""
    tokenizer_path: str = ""
    embedding_dimensions: int = 64
    embedding_max_tokens: int = 256
    search_limit: int = 24
    # BM25 lexical scoring blended with vector similarity (hybrid retriever).
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    # Incremental indexing: skip re-embedding a document whose chunk content
    # hashes are unchanged, and reuse stored embeddings for unchanged chunks.
    incremental: bool = True


@dataclass(frozen=True, slots=True)
class TracingSettings:
    enabled: bool = True
    input_chars: int = 20_000
    output_chars: int = 20_000
    list_limit: int = 100


@dataclass(frozen=True, slots=True)
class SemanticCacheSettings:
    enabled: bool = True
    similarity_threshold: float = 0.95
    ttl_seconds: int = 7 * 24 * 60 * 60
    max_items: int = 1_000
    max_prompt_chars: int = 80_000
    max_response_chars: int = 80_000
    # Logic/schema version stamped into every entry; combined with the embedding
    # provider+dimensions it forms the cache namespace, so changing the embedding
    # model or bumping this invalidates incompatible entries instead of serving them.
    version: str = "1"
    # Heuristic answer quality below which a response is not cached (refusals,
    # fallbacks and near-empty answers score low). 0 disables quality gating.
    min_quality_score: float = 0.3
    # When true, requests carrying file/attachment context are cacheable too, but
    # only via exact-prompt match within their project scope (never fuzzy, to avoid
    # false hits from file-text-dominated embeddings). When false they are skipped.
    cache_attachments: bool = True


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    context_manager_enabled: bool = True
    context_sliding_window_messages: int = 36
    request_queue_enabled: bool = True
    request_queue_max_attempts: int = 6
    request_queue_initial_backoff_seconds: float = 2.0
    request_queue_max_backoff_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class ContextEngineSettings:
    """Prompt-cache-aware context engineering knobs.

    The engine never rewrites the cache-anchored stable prefix; it only *plans*
    a per-request token budget (for diagnostics) and, on the summary-gated
    sliding-window path, drops extra oldest history when the estimate overflows
    the per-model context window. ``token_aware_trim`` gates that extra drop.
    """

    enabled: bool = True
    token_aware_trim: bool = True
    reserve_output_tokens: int = 8_192
    safety_margin_ratio: float = 0.05
    compress_threshold_pct: float = 75.0
    default_context_window: int = 65_536
    min_keep_messages: int = 2
    model_context_windows: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType(
            {
                "deepseek-v4-pro": 131_072,
                "deepseek-v4-flash": 131_072,
            }
        )
    )


@dataclass(frozen=True, slots=True)
class ModelRouterSettings:
    """Policy-driven model routing + cascade inference.

    The router only *auto-selects* the cloud model tier (flash vs pro) when a
    request opts in (``autoRoute`` / ``model="auto"``); an explicit model choice
    is always respected. Cascade runs a cheap draft, applies a quality gate, and
    escalates to the expensive model only when the draft is insufficient.
    """

    enabled: bool = True
    cascade_enabled: bool = True
    judge_enabled: bool = False
    judge_model: str = "deepseek-v4-flash"
    judge_threshold: float = 0.6
    draft_model: str = "deepseek-v4-flash"
    refine_model: str = "deepseek-v4-pro"
    cascade_min_chars: int = 80
    # Soft cost cap (prompt tokens); 0 disables. When the estimated prompt
    # exceeds it, the cost router prefers the cheaper draft model.
    cost_budget_tokens: int = 0


@dataclass(frozen=True, slots=True)
class BudgetSettings:
    """Cost & token budget governance.

    ``pricing`` is USD per 1M tokens ``(input, output)`` per model; local / unknown
    models cost nothing. Default policy limits of 0 mean *unlimited*; a request can
    override them via a ``budget`` block. ``policy`` =
    ``downgrade_to_flash_when_exceeded`` makes the router fall back to the cheap
    model once a scope is over its daily budget.
    """

    tracking_enabled: bool = True
    max_total_tokens: int = 0
    max_agent_tokens: int = 0
    max_search_calls: int = 0
    max_tool_calls: int = 0
    max_estimated_cost_usd: float = 0.0
    policy: str = "none"
    pricing: Mapping[str, tuple[float, float]] = field(
        default_factory=lambda: MappingProxyType(
            {
                "deepseek-v4-pro": (0.55, 2.19),
                "deepseek-v4-flash": (0.27, 1.10),
            }
        )
    )


@dataclass(frozen=True, slots=True)
class AgentRuntimeSettings:
    """Durable Agent Runtime knobs.

    ``auto_resume`` decides what happens to runs left mid-flight by a crash or
    restart. Default ``False`` keeps the conservative behavior (mark them
    ``orphaned``; the user resumes manually via ``/api/agent-runs/{id}/resume``),
    so a restart never silently spends upstream tokens. Set
    ``AGENT_RUNTIME_AUTO_RESUME=1`` to resume orphaned runs from their last
    checkpoint on startup (requires a server-side ``DEEPSEEK_API_KEY`` because
    persisted runs never store credentials).
    """

    auto_resume: bool = False


@dataclass(frozen=True, slots=True)
class ToolPolicySettings:
    """Capability-based Tool Policy Engine knobs.

    The engine always validates schemas, classifies risk, runs the SSRF / path /
    sensitive-memory guards and scrubs tool results for prompt injection once a
    policy is attached to a request. The two *stricter* gates are opt-in so default
    behavior is unchanged: ``enforce_schema`` turns schema violations from warnings
    into hard denials, and ``require_confirm`` makes high-risk tools (e.g.
    ``forget_memory``) wait for an explicit approval instead of running.
    """

    enabled: bool = True
    enforce_schema: bool = False
    require_confirm: bool = False
    sanitize_results: bool = True
    audit_enabled: bool = True


@dataclass(frozen=True, slots=True)
class SchedulerSettings:
    """Local request scheduler / backpressure / rate-limit knobs.

    A small in-process admission layer in front of the single upstream chokepoint:
    priority queue + concurrency cap + token-bucket rate limit + bounded backpressure,
    plus a durable SQLite dead-letter queue for requests that exhaust retries.

    Defaults are deliberately *generous* so the live path stays transparent under
    normal/test load (``rate_per_second=0`` means unlimited); tighten via env to make
    the limits bite. ``max_queue_depth`` bounds waiting+in-flight — once exceeded the
    scheduler sheds load (fast 503) instead of growing unboundedly.
    """

    enabled: bool = True
    max_concurrency: int = 16
    max_queue_depth: int = 256
    rate_per_second: float = 0.0  # 0 = unlimited
    rate_burst: int = 0  # 0 = derive from max_concurrency
    acquire_timeout_seconds: float = 30.0
    dlq_enabled: bool = True
    dlq_max_rows: int = 500
    orphan_seconds: int = 900  # running rows older than this on boot are recovered


@dataclass(frozen=True, slots=True)
class OllamaSettings:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: int = 120


@dataclass(frozen=True, slots=True)
class MCPSettings:
    """MCP-native Tool Hub knobs.

    The hub speaks MCP JSON-RPC 2.0 over a single Streamable-HTTP style endpoint
    (``POST /mcp``, local-auth gated) and re-exposes the local tool runtime as
    standard MCP ``tools`` plus optional ``resources`` (generated artifacts) and
    ``prompts``. ``capability`` picks the Tool Policy capability profile granted to
    MCP clients — every ``tools/call`` still goes through the full policy gate
    (schema / SSRF / path / sensitive guards), so an external MCP client never gets
    more than that slice. The outbound MCP *client* (connecting external MCP
    servers) is opt-in and off by default.
    """

    enabled: bool = True
    capability: str = "full"
    expose_resources: bool = True
    expose_prompts: bool = True
    client_enabled: bool = False
    client_servers: tuple[tuple[str, str], ...] = ()  # (name, base_url)
    client_timeout_seconds: int = 30


@dataclass(frozen=True, slots=True)
class A2ASettings:
    """A2A-style Agent Mesh knobs.

    Exposes each local Seek/agent role as an A2A agent: an Agent Card for
    discovery plus a JSON-RPC task lifecycle (``message/send`` / ``message/stream``
    / ``tasks/get`` / ``tasks/cancel``). Task execution spends upstream tokens, so
    it requires a server-side ``DEEPSEEK_API_KEY``; without one tasks fail cleanly.
    ``peers`` lists external A2A agent base URLs the local mesh may delegate to.
    """

    enabled: bool = True
    default_agent: str = "reasoner"
    max_tasks: int = 200
    history_limit: int = 20
    peers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextTaintSettings:
    """Context Taint Tracking + Prompt Injection Firewall knobs.

    Every assembled request is classified into trusted (system / user / memory)
    and untrusted (web search context, uploaded-file context, external tool
    results) segments, and untrusted segments are scanned for injection,
    secret-exfiltration and tool-invocation directives. ``harden_*`` prepend an
    isolation guard to untrusted blocks (deterministic text, so prompt-cache
    prefix stability across turns is preserved). ``escalate_confirm`` makes
    high-risk / sensitive-sink tools require explicit user approval for the rest
    of a turn whose context carried injection directives.
    """

    enabled: bool = True
    harden_search_context: bool = True
    harden_file_context: bool = True
    escalate_confirm: bool = True
    max_segments: int = 24


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path = ROOT
    app_version: str = "2.1.7"
    deepseek_url: str = "https://api.deepseek.com/chat/completions"
    tavily_url: str = "https://api.tavily.com/search"
    deepseek_timeout_seconds: int = 180
    multi_agent_timeout_seconds: int = 3900
    tavily_timeout_seconds: int = 45
    # 多 Agent 一次运行的累计 token 上限（prompt+completion，跨所有 worker+综合）。默认设
    # 得很高，只作失控保护网，正常运行不会触达；综合阶段永远不受其影响。可经环境变量
    # MULTI_AGENT_TOKEN_BUDGET 收紧；设为 0 表示不限制。
    multi_agent_token_budget: int = 2_000_000
    deepseek_api_key: str = ""
    tavily_api_key: str = ""
    default_host: str = "127.0.0.1"
    default_port: int = 8000
    default_model: str = "deepseek-v4-pro"
    supported_models: tuple[str, ...] = ("deepseek-v4-pro", "deepseek-v4-flash")
    model_routes: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({"fast": "deepseek-v4-flash", "expert": "deepseek-v4-pro"})
    )
    model_aliases: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType(
            {
                "deepseek-v4-pro": "deepseek-v4-pro",
                "deepseekv4pro": "deepseek-v4-pro",
                "v4pro": "deepseek-v4-pro",
                "expert": "deepseek-v4-pro",
                "deepseek-v4-flash": "deepseek-v4-flash",
                "deepseekv4flash": "deepseek-v4-flash",
                "v4flash": "deepseek-v4-flash",
                "flash": "deepseek-v4-flash",
                "fast": "deepseek-v4-flash",
            }
        )
    )
    # 多 Agent 各角色用的模型，默认全部 deepseek-v4-pro（保持历史行为）。可用环境变量
    # AGENT_MODEL_PLANNER / _RESEARCHER / _CODER / _REASONER / _CRITIC 单独降级到
    # deepseek-v4-flash（更便宜更快，但不带 thinking 深度推理）。
    agent_models: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType(
            {
                "planner": "deepseek-v4-pro",
                "researcher": "deepseek-v4-pro",
                "coder": "deepseek-v4-pro",
                "reasoner": "deepseek-v4-pro",
                "critic": "deepseek-v4-pro",
            }
        )
    )
    search: SearchSettings = field(default_factory=SearchSettings)
    files: FileSettings = field(default_factory=FileSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    ocr: OCRSettings = field(default_factory=OCRSettings)
    edge: EdgeInferenceSettings = field(default_factory=EdgeInferenceSettings)
    local_rag: LocalRAGSettings = field(default_factory=LocalRAGSettings)
    tracing: TracingSettings = field(default_factory=TracingSettings)
    semantic_cache: SemanticCacheSettings = field(default_factory=SemanticCacheSettings)
    gateway: GatewaySettings = field(default_factory=GatewaySettings)
    context_engine: ContextEngineSettings = field(default_factory=ContextEngineSettings)
    model_router: ModelRouterSettings = field(default_factory=ModelRouterSettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    agent_runtime: AgentRuntimeSettings = field(default_factory=AgentRuntimeSettings)
    tool_policy: ToolPolicySettings = field(default_factory=ToolPolicySettings)
    scheduler: SchedulerSettings = field(default_factory=SchedulerSettings)
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    mcp: MCPSettings = field(default_factory=MCPSettings)
    a2a: A2ASettings = field(default_factory=A2ASettings)
    context_taint: ContextTaintSettings = field(default_factory=ContextTaintSettings)

    @property
    def static_dir(self) -> Path:
        return _bundled_static_dir()

    @property
    def search_cache_dir(self) -> Path:
        return self.root / ".search-cache"

    @property
    def file_cache_dir(self) -> Path:
        return self.root / ".file-cache"

    @property
    def generated_dir(self) -> Path:
        return self.root / ".generated"

    @property
    def memory_dir(self) -> Path:
        return self.root / ".memory"

    @property
    def memory_file(self) -> Path:
        return self.memory_dir / "memories.json"

    @property
    def reminders_dir(self) -> Path:
        return self.root / ".reminders"

    @property
    def reminders_file(self) -> Path:
        return self.reminders_dir / "reminders.json"

    @property
    def projects_dir(self) -> Path:
        return self.root / ".projects"

    @property
    def agent_runs_dir(self) -> Path:
        return self.root / ".agent-runs"

    @property
    def local_rag_dir(self) -> Path:
        return self.root / ".local-rag"

    @property
    def local_rag_db(self) -> Path:
        return self.local_rag_dir / "rag.sqlite3"

    @property
    def traces_dir(self) -> Path:
        return self.root / ".traces"

    @property
    def traces_db(self) -> Path:
        return self.traces_dir / "traces.sqlite3"

    @property
    def semantic_cache_dir(self) -> Path:
        return self.root / ".semantic-cache"

    @property
    def semantic_cache_db(self) -> Path:
        return self.semantic_cache_dir / "cache.sqlite3"

    @property
    def budget_dir(self) -> Path:
        return self.root / ".budget"

    @property
    def budget_db(self) -> Path:
        return self.budget_dir / "budget.sqlite3"

    @property
    def scheduler_dir(self) -> Path:
        return self.root / ".scheduler"

    @property
    def scheduler_db(self) -> Path:
        return self.scheduler_dir / "scheduler.sqlite3"

    @property
    def tool_audit_dir(self) -> Path:
        return self.root / ".tool-audit"

    @property
    def tool_audit_log(self) -> Path:
        return self.tool_audit_dir / "audit.jsonl"

    @property
    def a2a_tasks_dir(self) -> Path:
        return self.root / ".a2a"

    @property
    def request_queue_dir(self) -> Path:
        return self.root / ".request-queue"

    @property
    def request_queue_db(self) -> Path:
        return self.request_queue_dir / "queue.sqlite3"

    @property
    def auth_token_file(self) -> Path:
        return self.root / ".auth-token"

    @classmethod
    def from_env(cls, root: Path | None = None) -> "Settings":
        runtime_root = _runtime_root() if root is None else root
        auth_token = os.environ.get("AUTH_TOKEN", "").strip()
        file_defaults = FileSettings()
        ocr_defaults = OCRSettings()
        return cls(
            root=runtime_root,
            deepseek_timeout_seconds=_env_int("DEEPSEEK_TIMEOUT_SECONDS", 180),
            multi_agent_timeout_seconds=_env_int("MULTI_AGENT_TIMEOUT_SECONDS", 3900),
            tavily_timeout_seconds=_env_int("TAVILY_TIMEOUT_SECONDS", 45),
            multi_agent_token_budget=_env_int("MULTI_AGENT_TOKEN_BUDGET", 2_000_000),
            agent_models=_agent_models_from_env(),
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
            tavily_api_key=os.environ.get("TAVILY_API_KEY", "").strip(),
            default_host=os.environ.get("HOST", "127.0.0.1").strip() or "127.0.0.1",
            default_port=_env_int("PORT", 8000),
            auth=AuthSettings(
                enabled=not _env_bool("AUTH_DISABLED", False),
                token=auth_token or load_or_create_auth_token(runtime_root),
                allowed_hosts=_env_tuple("AUTH_ALLOWED_HOSTS"),
            ),
            files=FileSettings(
                upload_file_max_bytes=_env_int("UPLOAD_FILE_MAX_BYTES", file_defaults.upload_file_max_bytes),
                upload_max_bytes=_env_int("UPLOAD_MAX_BYTES", file_defaults.upload_max_bytes),
            ),
            ocr=OCRSettings(
                enabled=_env_bool("OCR_ENABLED", False),
                mode=_env_choice("OCR_MODE", {"fast", "balanced", "quality"}, ocr_defaults.mode),
                pdf_dpi=_env_int_clamped("OCR_PDF_DPI", ocr_defaults.pdf_dpi, 150, 450),
                max_image_pixels=_env_int_min("OCR_MAX_IMAGE_PIXELS", ocr_defaults.max_image_pixels, 1),
                formula_cmd=os.environ.get("OCR_FORMULA_CMD", "").strip(),
                formula_timeout_seconds=_env_int_clamped(
                    "OCR_FORMULA_TIMEOUT_SECONDS",
                    ocr_defaults.formula_timeout_seconds,
                    5,
                    600,
                ),
            ),
            edge=EdgeInferenceSettings(
                enabled=_env_bool("EDGE_INFERENCE_ENABLED", False),
                provider=_env_choice("EDGE_INFERENCE_PROVIDER", {"llama_cpp", "mlc"}, "llama_cpp"),
                model_path=os.environ.get("EDGE_MODEL_PATH", "").strip(),
                model_name=os.environ.get("EDGE_MODEL_NAME", "deepseek-r1-distill-local").strip() or "deepseek-r1-distill-local",
                chat_format=os.environ.get("EDGE_CHAT_FORMAT", "").strip(),
                allow_model_path_override=_env_bool("EDGE_ALLOW_MODEL_PATH_OVERRIDE", False),
                n_ctx=_env_int_clamped("EDGE_N_CTX", 4096, 512, 262_144),
                n_threads=_env_int_min("EDGE_N_THREADS", 0, 0),
                n_gpu_layers=_env_int_min("EDGE_N_GPU_LAYERS", 0, 0),
                max_tokens=_env_int_clamped("EDGE_MAX_TOKENS", 1024, 16, 16_384),
                temperature=_env_float_clamped("EDGE_TEMPERATURE", 0.7, 0.0, 2.0),
                top_p=_env_float_clamped("EDGE_TOP_P", 0.95, 0.05, 1.0),
                simple_max_chars=_env_int_clamped("EDGE_SIMPLE_MAX_CHARS", 6000, 256, 120_000),
            ),
            local_rag=LocalRAGSettings(
                enabled=_env_bool("LOCAL_RAG_ENABLED", True),
                backend=_env_choice("LOCAL_RAG_BACKEND", {"sqlite_vec", "sqlite"}, "sqlite_vec"),
                embedding_provider=_env_choice("LOCAL_RAG_EMBEDDING_PROVIDER", {"hash", "onnx"}, "hash"),
                embedding_model_path=os.environ.get("LOCAL_RAG_ONNX_MODEL_PATH", "").strip(),
                tokenizer_path=os.environ.get("LOCAL_RAG_TOKENIZER_PATH", "").strip(),
                embedding_dimensions=_env_int_clamped("LOCAL_RAG_EMBEDDING_DIMENSIONS", 64, 8, 4096),
                embedding_max_tokens=_env_int_clamped("LOCAL_RAG_EMBEDDING_MAX_TOKENS", 256, 16, 8192),
                search_limit=_env_int_clamped("LOCAL_RAG_SEARCH_LIMIT", 24, 1, 200),
                bm25_k1=_env_float_clamped("LOCAL_RAG_BM25_K1", 1.5, 0.0, 5.0),
                bm25_b=_env_float_clamped("LOCAL_RAG_BM25_B", 0.75, 0.0, 1.0),
                incremental=_env_bool("LOCAL_RAG_INCREMENTAL", True),
            ),
            tracing=TracingSettings(
                enabled=_env_bool("TRACE_ENABLED", True),
                input_chars=_env_int_clamped("TRACE_INPUT_CHARS", 20_000, 1_000, 200_000),
                output_chars=_env_int_clamped("TRACE_OUTPUT_CHARS", 20_000, 1_000, 200_000),
                list_limit=_env_int_clamped("TRACE_LIST_LIMIT", 100, 10, 1_000),
            ),
            semantic_cache=SemanticCacheSettings(
                enabled=_env_bool("SEMANTIC_CACHE_ENABLED", True),
                similarity_threshold=_env_float_clamped("SEMANTIC_CACHE_THRESHOLD", 0.95, 0.5, 0.999),
                ttl_seconds=_env_int_clamped("SEMANTIC_CACHE_TTL_SECONDS", 7 * 24 * 60 * 60, 60, 31_536_000),
                max_items=_env_int_clamped("SEMANTIC_CACHE_MAX_ITEMS", 1_000, 10, 50_000),
                max_prompt_chars=_env_int_clamped("SEMANTIC_CACHE_MAX_PROMPT_CHARS", 80_000, 1_000, 500_000),
                max_response_chars=_env_int_clamped("SEMANTIC_CACHE_MAX_RESPONSE_CHARS", 80_000, 1_000, 500_000),
                version=os.environ.get("SEMANTIC_CACHE_VERSION", "1").strip() or "1",
                min_quality_score=_env_float_clamped("SEMANTIC_CACHE_MIN_QUALITY", 0.3, 0.0, 1.0),
                cache_attachments=_env_bool("SEMANTIC_CACHE_ATTACHMENTS", True),
            ),
            gateway=GatewaySettings(
                context_manager_enabled=_env_bool("GATEWAY_CONTEXT_MANAGER_ENABLED", True),
                context_sliding_window_messages=_env_int_clamped("GATEWAY_CONTEXT_WINDOW_MESSAGES", 36, 8, 80),
                request_queue_enabled=_env_bool("GATEWAY_REQUEST_QUEUE_ENABLED", True),
                request_queue_max_attempts=_env_int_clamped("GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS", 6, 1, 20),
                request_queue_initial_backoff_seconds=_env_float_clamped("GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS", 2.0, 0.0, 60.0),
                request_queue_max_backoff_seconds=_env_float_clamped("GATEWAY_REQUEST_QUEUE_MAX_BACKOFF_SECONDS", 120.0, 0.0, 600.0),
            ),
            context_engine=ContextEngineSettings(
                enabled=_env_bool("CONTEXT_ENGINE_ENABLED", True),
                token_aware_trim=_env_bool("CONTEXT_ENGINE_TOKEN_AWARE_TRIM", True),
                reserve_output_tokens=_env_int_clamped("CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS", 8_192, 256, 131_072),
                safety_margin_ratio=_env_float_clamped("CONTEXT_ENGINE_SAFETY_MARGIN_RATIO", 0.05, 0.0, 0.5),
                compress_threshold_pct=_env_float_clamped("CONTEXT_ENGINE_COMPRESS_THRESHOLD_PCT", 75.0, 1.0, 100.0),
                default_context_window=_env_int_clamped("CONTEXT_ENGINE_DEFAULT_WINDOW", 65_536, 1_024, 2_000_000),
                min_keep_messages=_env_int_clamped("CONTEXT_ENGINE_MIN_KEEP_MESSAGES", 2, 1, 40),
                model_context_windows=_context_engine_windows_from_env(),
            ),
            budget=BudgetSettings(
                tracking_enabled=_env_bool("BUDGET_TRACKING_ENABLED", True),
                max_total_tokens=_env_int_min("BUDGET_MAX_TOTAL_TOKENS", 0, 0),
                max_agent_tokens=_env_int_min("BUDGET_MAX_AGENT_TOKENS", 0, 0),
                max_search_calls=_env_int_min("BUDGET_MAX_SEARCH_CALLS", 0, 0),
                max_tool_calls=_env_int_min("BUDGET_MAX_TOOL_CALLS", 0, 0),
                max_estimated_cost_usd=_env_float_clamped("BUDGET_MAX_ESTIMATED_COST_USD", 0.0, 0.0, 1_000_000.0),
                policy=_env_choice("BUDGET_POLICY", {"none", "downgrade_to_flash_when_exceeded"}, "none"),
                pricing=_budget_pricing_from_env(),
            ),
            model_router=ModelRouterSettings(
                enabled=_env_bool("MODEL_ROUTER_ENABLED", True),
                cascade_enabled=_env_bool("MODEL_ROUTER_CASCADE_ENABLED", True),
                judge_enabled=_env_bool("MODEL_ROUTER_JUDGE_ENABLED", False),
                judge_threshold=_env_float_clamped("MODEL_ROUTER_JUDGE_THRESHOLD", 0.6, 0.0, 1.0),
                cascade_min_chars=_env_int_clamped("MODEL_ROUTER_CASCADE_MIN_CHARS", 80, 1, 5_000),
                cost_budget_tokens=_env_int_min("MODEL_ROUTER_COST_BUDGET_TOKENS", 0, 0),
            ),
            agent_runtime=AgentRuntimeSettings(
                auto_resume=_env_bool("AGENT_RUNTIME_AUTO_RESUME", False),
            ),
            tool_policy=ToolPolicySettings(
                enabled=_env_bool("TOOL_POLICY_ENABLED", True),
                enforce_schema=_env_bool("TOOL_POLICY_ENFORCE_SCHEMA", False),
                require_confirm=_env_bool("TOOL_POLICY_REQUIRE_CONFIRM", False),
                sanitize_results=_env_bool("TOOL_POLICY_SANITIZE_RESULTS", True),
                audit_enabled=_env_bool("TOOL_POLICY_AUDIT_ENABLED", True),
            ),
            scheduler=SchedulerSettings(
                enabled=_env_bool("SCHEDULER_ENABLED", True),
                max_concurrency=_env_int_clamped("SCHEDULER_MAX_CONCURRENCY", 16, 1, 1024),
                max_queue_depth=_env_int_clamped("SCHEDULER_MAX_QUEUE_DEPTH", 256, 1, 100_000),
                rate_per_second=_env_float_clamped("SCHEDULER_RATE_PER_SECOND", 0.0, 0.0, 100_000.0),
                rate_burst=_env_int_min("SCHEDULER_RATE_BURST", 0, 0),
                acquire_timeout_seconds=_env_float_clamped("SCHEDULER_ACQUIRE_TIMEOUT_SECONDS", 30.0, 0.0, 3600.0),
                dlq_enabled=_env_bool("SCHEDULER_DLQ_ENABLED", True),
                dlq_max_rows=_env_int_clamped("SCHEDULER_DLQ_MAX_ROWS", 500, 10, 100_000),
                orphan_seconds=_env_int_clamped("SCHEDULER_ORPHAN_SECONDS", 900, 30, 86_400),
            ),
            ollama=OllamaSettings(
                enabled=_env_bool("OLLAMA_ENABLED", False),
                base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip() or "http://127.0.0.1:11434",
                timeout_seconds=_env_int_clamped("OLLAMA_TIMEOUT_SECONDS", 120, 5, 1800),
            ),
            mcp=MCPSettings(
                enabled=_env_bool("MCP_ENABLED", True),
                capability=os.environ.get("MCP_CAPABILITY", "full").strip() or "full",
                expose_resources=_env_bool("MCP_EXPOSE_RESOURCES", True),
                expose_prompts=_env_bool("MCP_EXPOSE_PROMPTS", True),
                client_enabled=_env_bool("MCP_CLIENT_ENABLED", False),
                client_servers=_mcp_client_servers_from_env(),
                client_timeout_seconds=_env_int_clamped("MCP_CLIENT_TIMEOUT_SECONDS", 30, 1, 600),
            ),
            a2a=A2ASettings(
                enabled=_env_bool("A2A_ENABLED", True),
                default_agent=os.environ.get("A2A_DEFAULT_AGENT", "reasoner").strip() or "reasoner",
                max_tasks=_env_int_clamped("A2A_MAX_TASKS", 200, 10, 10_000),
                history_limit=_env_int_clamped("A2A_HISTORY_LIMIT", 20, 2, 200),
                peers=_env_tuple("A2A_PEERS"),
            ),
            context_taint=ContextTaintSettings(
                enabled=_env_bool("TAINT_ENABLED", True),
                harden_search_context=_env_bool("TAINT_HARDEN_SEARCH_CONTEXT", True),
                harden_file_context=_env_bool("TAINT_HARDEN_FILE_CONTEXT", True),
                escalate_confirm=_env_bool("TAINT_ESCALATE_CONFIRM", True),
                max_segments=_env_int_clamped("TAINT_MAX_SEGMENTS", 24, 4, 200),
            ),
        )


def load_or_create_auth_token(root: Path = ROOT) -> str:
    """Keep the local auth token stable across server restarts.

    Without a stable token, a browser tab that already has an HttpOnly
    `auth_token` cookie becomes unauthorized after every process restart.
    """
    token_path = root / ".auth-token"
    try:
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing.splitlines()[0].strip()
    except OSError:
        pass

    token = secrets.token_urlsafe(24)
    try:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(token + "\n", encoding="utf-8")
    except OSError:
        return token
    return token


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_int_clamped(name: str, default: int, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, _env_int(name, default)))


def _env_int_min(name: str, default: int, minimum: int) -> int:
    return max(minimum, _env_int(name, default))


def _env_float_clamped(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _env_choice(name: str, choices: set[str], default: str) -> str:
    raw = os.environ.get(name, "").strip().lower()
    return raw if raw in choices else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_tuple(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


_AGENT_MODEL_ENV = {
    "planner": "AGENT_MODEL_PLANNER",
    "researcher": "AGENT_MODEL_RESEARCHER",
    "coder": "AGENT_MODEL_CODER",
    "reasoner": "AGENT_MODEL_REASONER",
    "critic": "AGENT_MODEL_CRITIC",
}
_AGENT_MODEL_CHOICES = {
    "pro": "deepseek-v4-pro",
    "flash": "deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek-v4-flash",
}


def _env_agent_model(name: str, default: str = "deepseek-v4-pro") -> str:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return _AGENT_MODEL_CHOICES.get(raw, default)


def _agent_models_from_env() -> Mapping[str, str]:
    return MappingProxyType({role: _env_agent_model(env) for role, env in _AGENT_MODEL_ENV.items()})


_CONTEXT_ENGINE_WINDOW_ENV = {
    "deepseek-v4-pro": "CONTEXT_ENGINE_PRO_WINDOW",
    "deepseek-v4-flash": "CONTEXT_ENGINE_FLASH_WINDOW",
}
_CONTEXT_ENGINE_WINDOW_DEFAULTS = {
    "deepseek-v4-pro": 131_072,
    "deepseek-v4-flash": 131_072,
}


_BUDGET_PRICING_DEFAULTS = {
    "deepseek-v4-pro": (0.55, 2.19),
    "deepseek-v4-flash": (0.27, 1.10),
}
_BUDGET_PRICING_ENV = {
    "deepseek-v4-pro": ("BUDGET_PRICE_PRO_INPUT", "BUDGET_PRICE_PRO_OUTPUT"),
    "deepseek-v4-flash": ("BUDGET_PRICE_FLASH_INPUT", "BUDGET_PRICE_FLASH_OUTPUT"),
}


def _budget_pricing_from_env() -> Mapping[str, tuple[float, float]]:
    pricing: dict[str, tuple[float, float]] = {}
    for model, (input_env, output_env) in _BUDGET_PRICING_ENV.items():
        default_input, default_output = _BUDGET_PRICING_DEFAULTS[model]
        pricing[model] = (
            _env_float_clamped(input_env, default_input, 0.0, 10_000.0),
            _env_float_clamped(output_env, default_output, 0.0, 10_000.0),
        )
    return MappingProxyType(pricing)


def _context_engine_windows_from_env() -> Mapping[str, int]:
    return MappingProxyType(
        {
            model: _env_int_clamped(env, _CONTEXT_ENGINE_WINDOW_DEFAULTS[model], 1_024, 2_000_000)
            for model, env in _CONTEXT_ENGINE_WINDOW_ENV.items()
        }
    )


def _mcp_client_servers_from_env() -> tuple[tuple[str, str], ...]:
    """Parse MCP_CLIENT_SERVERS: a JSON list of ``{"name": ..., "url": ...}``."""
    raw = os.environ.get("MCP_CLIENT_SERVERS", "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    servers: list[tuple[str, str]] = []
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            if name and url.startswith(("http://", "https://")):
                servers.append((name, url))
    return tuple(servers)


settings = Settings.from_env()

STATIC_DIR = settings.static_dir
APP_VERSION = settings.app_version
DEEPSEEK_URL = settings.deepseek_url
TAVILY_URL = settings.tavily_url
DEEPSEEK_TIMEOUT_SECONDS = settings.deepseek_timeout_seconds
MULTI_AGENT_TIMEOUT_SECONDS = settings.multi_agent_timeout_seconds
MULTI_AGENT_TOKEN_BUDGET = settings.multi_agent_token_budget
TAVILY_TIMEOUT_SECONDS = settings.tavily_timeout_seconds
TAVILY_API_KEY = settings.tavily_api_key
DEFAULT_HOST = settings.default_host
DEFAULT_PORT = settings.default_port
DEFAULT_MODEL = settings.default_model
SUPPORTED_MODELS = settings.supported_models
MODEL_ROUTES = settings.model_routes
MODEL_ALIASES = settings.model_aliases
AGENT_MODELS = settings.agent_models
OCR_MODE = settings.ocr.mode
OCR_PDF_DPI = settings.ocr.pdf_dpi
OCR_MAX_IMAGE_PIXELS = settings.ocr.max_image_pixels
OCR_FORMULA_CMD = settings.ocr.formula_cmd
OCR_FORMULA_TIMEOUT_SECONDS = settings.ocr.formula_timeout_seconds
EDGE_INFERENCE_ENABLED = settings.edge.enabled
EDGE_INFERENCE_PROVIDER = settings.edge.provider
EDGE_MODEL_PATH = settings.edge.model_path
EDGE_MODEL_NAME = settings.edge.model_name
OLLAMA_ENABLED = settings.ollama.enabled
OLLAMA_BASE_URL = settings.ollama.base_url
OLLAMA_TIMEOUT_SECONDS = settings.ollama.timeout_seconds
LOCAL_RAG_ENABLED = settings.local_rag.enabled
LOCAL_RAG_BACKEND = settings.local_rag.backend
LOCAL_RAG_EMBEDDING_PROVIDER = settings.local_rag.embedding_provider
LOCAL_RAG_ONNX_MODEL_PATH = settings.local_rag.embedding_model_path
LOCAL_RAG_TOKENIZER_PATH = settings.local_rag.tokenizer_path
LOCAL_RAG_EMBEDDING_DIMENSIONS = settings.local_rag.embedding_dimensions
LOCAL_RAG_EMBEDDING_MAX_TOKENS = settings.local_rag.embedding_max_tokens
LOCAL_RAG_SEARCH_LIMIT = settings.local_rag.search_limit
LOCAL_RAG_BM25_K1 = settings.local_rag.bm25_k1
LOCAL_RAG_BM25_B = settings.local_rag.bm25_b
LOCAL_RAG_INCREMENTAL = settings.local_rag.incremental

SEARCH_RESULT_LIMIT = settings.search.result_limit
SEARCH_ROUND_LIMIT = settings.search.round_limit
SEARCH_TOTAL_RESULT_LIMIT = settings.search.total_result_limit
SEARCH_CONTENT_CHARS = settings.search.content_chars
SEARCH_RAW_CONTENT_CHARS = settings.search.raw_content_chars
SEARCH_CONTEXT_RESULT_LIMIT = settings.search.context_result_limit
SEARCH_CACHE_DIR = settings.search_cache_dir
SEARCH_CACHE_MAX_AGE_SECONDS = settings.search.cache_max_age_seconds

MAX_UPLOAD_FILE_BYTES = settings.files.upload_file_max_bytes
MAX_UPLOAD_BYTES = settings.files.upload_max_bytes
FILE_CACHE_DIR = settings.file_cache_dir
GENERATED_DIR = settings.generated_dir
FILE_CHUNK_CHARS = settings.files.chunk_chars
FILE_CHUNK_OVERLAP = settings.files.chunk_overlap
FILE_FULL_CONTEXT_LIMIT = settings.files.full_context_limit
FILE_CONTEXT_CHAR_BUDGET = settings.files.context_char_budget
FILE_CONTEXT_MAX_CHUNKS = settings.files.context_max_chunks
FILE_PREVIEW_CHARS = settings.files.preview_chars
FILE_CACHE_MAX_AGE_DAYS = settings.files.cache_max_age_days
FILE_CACHE_MAX_BYTES = settings.files.cache_max_bytes
MAX_ZIP_ENTRY_BYTES = settings.files.max_zip_entry_bytes
MAX_ZIP_TOTAL_BYTES = settings.files.max_zip_total_bytes

CONTEXT_COMPRESS_MAX_INPUT_CHARS = settings.context.compress_max_input_chars
CONTEXT_SUMMARY_MAX_CHARS = settings.context.summary_max_chars
CONTEXT_COMPRESS_MODEL = settings.context.compress_model

MEMORY_DIR = settings.memory_dir
MEMORY_FILE = settings.memory_file
MEMORY_CONTEXT_CHAR_BUDGET = settings.memory.context_char_budget
MEMORY_RETRIEVE_LIMIT = settings.memory.retrieve_limit
MEMORY_MAX_ITEMS = settings.memory.max_items
REMINDERS_DIR = settings.reminders_dir
REMINDERS_FILE = settings.reminders_file
PROJECTS_DIR = settings.projects_dir
AGENT_RUNS_DIR = settings.agent_runs_dir
LOCAL_RAG_DIR = settings.local_rag_dir
LOCAL_RAG_DB = settings.local_rag_db
TRACE_ENABLED = settings.tracing.enabled
TRACE_DIR = settings.traces_dir
TRACE_DB = settings.traces_db
TRACE_INPUT_CHARS = settings.tracing.input_chars
TRACE_OUTPUT_CHARS = settings.tracing.output_chars
TRACE_LIST_LIMIT = settings.tracing.list_limit
SEMANTIC_CACHE_ENABLED = settings.semantic_cache.enabled
SEMANTIC_CACHE_DIR = settings.semantic_cache_dir
SEMANTIC_CACHE_DB = settings.semantic_cache_db
SEMANTIC_CACHE_THRESHOLD = settings.semantic_cache.similarity_threshold
SEMANTIC_CACHE_TTL_SECONDS = settings.semantic_cache.ttl_seconds
SEMANTIC_CACHE_MAX_ITEMS = settings.semantic_cache.max_items
SEMANTIC_CACHE_MAX_PROMPT_CHARS = settings.semantic_cache.max_prompt_chars
SEMANTIC_CACHE_MAX_RESPONSE_CHARS = settings.semantic_cache.max_response_chars
SEMANTIC_CACHE_VERSION = settings.semantic_cache.version
SEMANTIC_CACHE_MIN_QUALITY = settings.semantic_cache.min_quality_score
SEMANTIC_CACHE_ATTACHMENTS = settings.semantic_cache.cache_attachments
GATEWAY_CONTEXT_MANAGER_ENABLED = settings.gateway.context_manager_enabled
GATEWAY_CONTEXT_WINDOW_MESSAGES = settings.gateway.context_sliding_window_messages
GATEWAY_REQUEST_QUEUE_ENABLED = settings.gateway.request_queue_enabled
GATEWAY_REQUEST_QUEUE_DIR = settings.request_queue_dir
GATEWAY_REQUEST_QUEUE_DB = settings.request_queue_db
GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS = settings.gateway.request_queue_max_attempts
GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS = settings.gateway.request_queue_initial_backoff_seconds
GATEWAY_REQUEST_QUEUE_MAX_BACKOFF_SECONDS = settings.gateway.request_queue_max_backoff_seconds
CONTEXT_ENGINE_ENABLED = settings.context_engine.enabled
CONTEXT_ENGINE_TOKEN_AWARE_TRIM = settings.context_engine.token_aware_trim
CONTEXT_ENGINE_RESERVE_OUTPUT_TOKENS = settings.context_engine.reserve_output_tokens
CONTEXT_ENGINE_SAFETY_MARGIN_RATIO = settings.context_engine.safety_margin_ratio
CONTEXT_ENGINE_COMPRESS_THRESHOLD_PCT = settings.context_engine.compress_threshold_pct
CONTEXT_ENGINE_DEFAULT_CONTEXT_WINDOW = settings.context_engine.default_context_window
CONTEXT_ENGINE_MIN_KEEP_MESSAGES = settings.context_engine.min_keep_messages
CONTEXT_ENGINE_MODEL_CONTEXT_WINDOWS = settings.context_engine.model_context_windows
AGENT_RUNTIME_AUTO_RESUME = settings.agent_runtime.auto_resume
MODEL_ROUTER_ENABLED = settings.model_router.enabled
MODEL_ROUTER_CASCADE_ENABLED = settings.model_router.cascade_enabled
MODEL_ROUTER_JUDGE_ENABLED = settings.model_router.judge_enabled
MODEL_ROUTER_JUDGE_MODEL = settings.model_router.judge_model
MODEL_ROUTER_JUDGE_THRESHOLD = settings.model_router.judge_threshold
MODEL_ROUTER_DRAFT_MODEL = settings.model_router.draft_model
MODEL_ROUTER_REFINE_MODEL = settings.model_router.refine_model
MODEL_ROUTER_CASCADE_MIN_CHARS = settings.model_router.cascade_min_chars
MODEL_ROUTER_COST_BUDGET_TOKENS = settings.model_router.cost_budget_tokens
BUDGET_TRACKING_ENABLED = settings.budget.tracking_enabled
BUDGET_MAX_TOTAL_TOKENS = settings.budget.max_total_tokens
BUDGET_MAX_AGENT_TOKENS = settings.budget.max_agent_tokens
BUDGET_MAX_SEARCH_CALLS = settings.budget.max_search_calls
BUDGET_MAX_TOOL_CALLS = settings.budget.max_tool_calls
BUDGET_MAX_ESTIMATED_COST_USD = settings.budget.max_estimated_cost_usd
BUDGET_POLICY = settings.budget.policy
BUDGET_PRICING = settings.budget.pricing
BUDGET_DIR = settings.budget_dir
BUDGET_DB = settings.budget_db
TOOL_POLICY_ENABLED = settings.tool_policy.enabled
TOOL_POLICY_ENFORCE_SCHEMA = settings.tool_policy.enforce_schema
TOOL_POLICY_REQUIRE_CONFIRM = settings.tool_policy.require_confirm
TOOL_POLICY_SANITIZE_RESULTS = settings.tool_policy.sanitize_results
TOOL_POLICY_AUDIT_ENABLED = settings.tool_policy.audit_enabled
TOOL_POLICY_AUDIT_DIR = settings.tool_audit_dir
TOOL_POLICY_AUDIT_LOG = settings.tool_audit_log
SCHEDULER_ENABLED = settings.scheduler.enabled
SCHEDULER_MAX_CONCURRENCY = settings.scheduler.max_concurrency
SCHEDULER_MAX_QUEUE_DEPTH = settings.scheduler.max_queue_depth
SCHEDULER_RATE_PER_SECOND = settings.scheduler.rate_per_second
SCHEDULER_RATE_BURST = settings.scheduler.rate_burst
SCHEDULER_ACQUIRE_TIMEOUT_SECONDS = settings.scheduler.acquire_timeout_seconds
SCHEDULER_DLQ_ENABLED = settings.scheduler.dlq_enabled
SCHEDULER_DLQ_MAX_ROWS = settings.scheduler.dlq_max_rows
SCHEDULER_ORPHAN_SECONDS = settings.scheduler.orphan_seconds
SCHEDULER_DIR = settings.scheduler_dir
SCHEDULER_DB = settings.scheduler_db
MCP_ENABLED = settings.mcp.enabled
MCP_CAPABILITY = settings.mcp.capability
MCP_EXPOSE_RESOURCES = settings.mcp.expose_resources
MCP_EXPOSE_PROMPTS = settings.mcp.expose_prompts
MCP_CLIENT_ENABLED = settings.mcp.client_enabled
MCP_CLIENT_SERVERS = settings.mcp.client_servers
MCP_CLIENT_TIMEOUT_SECONDS = settings.mcp.client_timeout_seconds
A2A_ENABLED = settings.a2a.enabled
A2A_DEFAULT_AGENT = settings.a2a.default_agent
A2A_MAX_TASKS = settings.a2a.max_tasks
A2A_HISTORY_LIMIT = settings.a2a.history_limit
A2A_PEERS = settings.a2a.peers
A2A_TASKS_DIR = settings.a2a_tasks_dir
TAINT_ENABLED = settings.context_taint.enabled
TAINT_HARDEN_SEARCH_CONTEXT = settings.context_taint.harden_search_context
TAINT_HARDEN_FILE_CONTEXT = settings.context_taint.harden_file_context
TAINT_ESCALATE_CONFIRM = settings.context_taint.escalate_confirm
TAINT_MAX_SEGMENTS = settings.context_taint.max_segments
AUTH_TOKEN_FILE = settings.auth_token_file

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".py", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".php",
    ".rb", ".swift", ".kt", ".sql", ".sh", ".ps1", ".bat", ".log", ".ini",
    ".toml", ".env", ".rtf",
}
TRUSTED_DOMAIN_HINTS = (
    ".gov",
    ".edu",
    "wikipedia.org",
    "github.com",
    "docs.",
    "developer.",
    "support.",
    "learn.microsoft.com",
    "developer.mozilla.org",
)

LOG_RECORD_BUILTINS = set(logging.makeLogRecord({}).__dict__)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in LOG_RECORD_BUILTINS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    root_logger = logging.getLogger()
    if root_logger.handlers:
        for handler in root_logger.handlers:
            handler.setFormatter(JsonLogFormatter())
        root_logger.setLevel(level)
        return

    stream = sys.stderr if sys.stderr is not None else open(os.devnull, "w", encoding="utf-8")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    root_logger.addHandler(handler)
    root_logger.setLevel(level)




