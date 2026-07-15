from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deepseek_infra.core.config import (
    DEFAULT_HOST,
    FILE_CACHE_DIR,
    FILE_CHUNK_CHARS,
    GATEWAY_CONTEXT_MANAGER_ENABLED,
    GATEWAY_CONTEXT_WINDOW_MESSAGES,
    GATEWAY_REQUEST_QUEUE_DB,
    GATEWAY_REQUEST_QUEUE_ENABLED,
    GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS,
    LOCAL_RAG_DB,
    LOCAL_RAG_ENABLED,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_FILE_BYTES,
    MODEL_ALIASES,
    MULTI_AGENT_TIMEOUT_SECONDS,
    SEARCH_RESULT_LIMIT,
    SEARCH_TOTAL_RESULT_LIMIT,
    SEMANTIC_CACHE_DB,
    TRACE_DB,
    Settings,
    AGENT_RUNS_DIR,
    AUTH_TOKEN_FILE,
    BROWSER_CONTROL_ENABLED,
    BROWSER_SESSION_TTL_SECONDS,
    load_or_create_auth_token,
    settings,
)


class ConfigTests(unittest.TestCase):
    def test_nested_settings_back_compat_constants_match(self) -> None:
        self.assertEqual(settings.app_version, "4.0.0-rc.2")
        self.assertEqual(settings.default_host, "127.0.0.1")
        self.assertEqual(DEFAULT_HOST, settings.default_host)
        self.assertEqual(MULTI_AGENT_TIMEOUT_SECONDS, settings.multi_agent_timeout_seconds)
        self.assertEqual(settings.multi_agent_timeout_seconds, 3900)
        self.assertEqual(SEARCH_RESULT_LIMIT, settings.search.result_limit)
        self.assertEqual(SEARCH_TOTAL_RESULT_LIMIT, settings.search.total_result_limit)
        self.assertEqual(FILE_CHUNK_CHARS, settings.files.chunk_chars)
        self.assertEqual(FILE_CACHE_DIR, settings.file_cache_dir)
        self.assertEqual(AGENT_RUNS_DIR, settings.agent_runs_dir)
        self.assertEqual(LOCAL_RAG_DB, settings.local_rag_db)
        self.assertEqual(LOCAL_RAG_ENABLED, settings.local_rag.enabled)
        self.assertEqual(TRACE_DB, settings.traces_db)
        self.assertEqual(BROWSER_CONTROL_ENABLED, settings.browser.enabled)
        self.assertEqual(BROWSER_SESSION_TTL_SECONDS, settings.browser.session_ttl_seconds)
        self.assertEqual(SEMANTIC_CACHE_DB, settings.semantic_cache_db)
        self.assertEqual(GATEWAY_REQUEST_QUEUE_DB, settings.request_queue_db)
        self.assertEqual(GATEWAY_CONTEXT_MANAGER_ENABLED, settings.gateway.context_manager_enabled)
        self.assertEqual(GATEWAY_CONTEXT_WINDOW_MESSAGES, settings.gateway.context_sliding_window_messages)
        self.assertEqual(GATEWAY_REQUEST_QUEUE_ENABLED, settings.gateway.request_queue_enabled)
        self.assertEqual(GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS, settings.gateway.request_queue_max_attempts)
        self.assertEqual(AUTH_TOKEN_FILE, settings.auth_token_file)
        self.assertEqual(MAX_UPLOAD_FILE_BYTES, 200_000_000)
        self.assertEqual(MAX_UPLOAD_BYTES, 220_000_000)

    def test_model_aliases_are_read_only(self) -> None:
        self.assertEqual(MODEL_ALIASES.get("fast"), "deepseek-v4-flash")
        with self.assertRaises(TypeError):
            settings.model_aliases["new"] = "deepseek-v4-pro"  # type: ignore[index]

    def test_env_settings_parse_host_auth_and_ocr(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "HOST": "0.0.0.0",
                "PORT": "8012",
                "AUTH_DISABLED": "1",
                "AUTH_TOKEN": "fixed-token",
                "AUTH_ALLOWED_HOSTS": "phone.local, 192.168.1.10",
                "OCR_ENABLED": "1",
                "OCR_MODE": "quality",
                "OCR_PDF_DPI": "999",
                "OCR_MAX_IMAGE_PIXELS": "12345",
                "OCR_FORMULA_CMD": 'pix2tex "{image}"',
                "OCR_FORMULA_TIMEOUT_SECONDS": "999",
                "DEEPSEEK_TIMEOUT_SECONDS": "222",
                "DEEPSEEK_API_URL": "http://upstream.test/chat/completions",
                "MULTI_AGENT_TIMEOUT_SECONDS": "333",
                "TAVILY_TIMEOUT_SECONDS": "33",
                "UPLOAD_FILE_MAX_BYTES": "123456",
                "UPLOAD_MAX_BYTES": "234567",
                "EDGE_INFERENCE_ENABLED": "1",
                "EDGE_INFERENCE_PROVIDER": "llama_cpp",
                "EDGE_MODE": "local",
                "EDGE_MODEL_PATH": "C:\\models\\DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf",
                "EDGE_MODEL_NAME": "DeepSeek-R1-Distill-Qwen-1.5B",
                "EDGE_ALLOW_MODEL_PATH_OVERRIDE": "1",
                "EDGE_N_CTX": "8192",
                "EDGE_N_THREADS": "8",
                "EDGE_N_GPU_LAYERS": "20",
                "EDGE_MAX_TOKENS": "2048",
                "EDGE_TEMPERATURE": "0.2",
                "EDGE_TOP_P": "0.9",
                "LOCAL_RAG_ENABLED": "1",
                "LOCAL_RAG_BACKEND": "sqlite",
                "LOCAL_RAG_EMBEDDING_PROVIDER": "onnx",
                "LOCAL_RAG_ONNX_MODEL_PATH": "C:\\models\\bge-micro.onnx",
                "LOCAL_RAG_TOKENIZER_PATH": "C:\\models\\tokenizer.json",
                "LOCAL_RAG_EMBEDDING_DIMENSIONS": "384",
                "LOCAL_RAG_EMBEDDING_MAX_TOKENS": "512",
                "LOCAL_RAG_SEARCH_LIMIT": "36",
                "TRACE_ENABLED": "0",
                "TRACE_INPUT_CHARS": "999999",
                "TRACE_OUTPUT_CHARS": "500",
                "TRACE_LIST_LIMIT": "5",
                "SEMANTIC_CACHE_ENABLED": "0",
                "SEMANTIC_CACHE_THRESHOLD": "0.97",
                "SEMANTIC_CACHE_TTL_SECONDS": "120",
                "SEMANTIC_CACHE_MAX_ITEMS": "222",
                "SEMANTIC_CACHE_MAX_PROMPT_CHARS": "2222",
                "SEMANTIC_CACHE_MAX_RESPONSE_CHARS": "3333",
                "GATEWAY_CONTEXT_MANAGER_ENABLED": "0",
                "GATEWAY_CONTEXT_WINDOW_MESSAGES": "48",
                "GATEWAY_REQUEST_QUEUE_ENABLED": "0",
                "GATEWAY_REQUEST_QUEUE_MAX_ATTEMPTS": "9",
                "GATEWAY_REQUEST_QUEUE_INITIAL_BACKOFF_SECONDS": "0.5",
                "GATEWAY_REQUEST_QUEUE_MAX_BACKOFF_SECONDS": "33",
                "MCP_CLIENT_ENABLED": "1",
                "MCP_CLIENT_SERVERS": (
                    '[{"name":"github","url":"http://127.0.0.1:9001/mcp","timeoutSeconds":7},'
                    '{"name":"docs","url":"http://127.0.0.1:9002/mcp"}]'
                ),
                "MCP_CLIENT_TIMEOUT_SECONDS": "11",
                "MCP_CLIENT_MAX_RETRIES": "2",
                "MCP_CLIENT_RETRY_BACKOFF_SECONDS": "0.75",
                "MCP_CLIENT_CIRCUIT_BREAKER_FAILURES": "4",
                "MCP_CLIENT_CIRCUIT_BREAKER_RESET_SECONDS": "90",
                "BROWSER_CONTROL_ENABLED": "1",
                "BROWSER_HEADLESS": "0",
                "BROWSER_ALLOW_PRIVATE_HOSTS": "1",
                "BROWSER_REQUIRE_CONFIRM": "0",
                "BROWSER_DOWNLOAD_MAX_BYTES": "123456",
                "BROWSER_SESSION_TTL_SECONDS": "600",
            },
            clear=True,
        ):
            loaded = Settings.from_env()

        self.assertEqual(loaded.default_host, "0.0.0.0")
        self.assertEqual(loaded.default_port, 8012)
        self.assertFalse(loaded.auth.enabled)
        self.assertEqual(loaded.auth.token, "fixed-token")
        self.assertEqual(loaded.auth.allowed_hosts, ("phone.local", "192.168.1.10"))
        self.assertTrue(loaded.ocr.enabled)
        self.assertEqual(loaded.ocr.mode, "quality")
        self.assertEqual(loaded.ocr.pdf_dpi, 450)
        self.assertEqual(loaded.ocr.max_image_pixels, 12345)
        self.assertEqual(loaded.ocr.formula_cmd, 'pix2tex "{image}"')
        self.assertEqual(loaded.ocr.formula_timeout_seconds, 600)
        self.assertEqual(loaded.deepseek_timeout_seconds, 222)
        self.assertEqual(loaded.deepseek_url, "http://upstream.test/chat/completions")
        self.assertEqual(loaded.multi_agent_timeout_seconds, 333)
        self.assertEqual(loaded.tavily_timeout_seconds, 33)
        self.assertEqual(loaded.files.upload_file_max_bytes, 123456)
        self.assertEqual(loaded.files.upload_max_bytes, 234567)
        self.assertTrue(loaded.edge.enabled)
        self.assertEqual(loaded.edge.provider, "llama_cpp")
        self.assertEqual(loaded.edge.mode, "local")
        self.assertTrue(loaded.edge.model_path.endswith("Q4_K_M.gguf"))
        self.assertEqual(loaded.edge.model_name, "DeepSeek-R1-Distill-Qwen-1.5B")
        self.assertTrue(loaded.edge.allow_model_path_override)
        self.assertEqual(loaded.edge.n_ctx, 8192)
        self.assertEqual(loaded.edge.n_threads, 8)
        self.assertEqual(loaded.edge.n_gpu_layers, 20)
        self.assertEqual(loaded.edge.max_tokens, 2048)
        self.assertEqual(loaded.edge.temperature, 0.2)
        self.assertEqual(loaded.edge.top_p, 0.9)
        self.assertTrue(loaded.local_rag.enabled)
        self.assertEqual(loaded.local_rag.backend, "sqlite")
        self.assertEqual(loaded.local_rag.embedding_provider, "onnx")
        self.assertTrue(loaded.local_rag.embedding_model_path.endswith("bge-micro.onnx"))
        self.assertTrue(loaded.local_rag.tokenizer_path.endswith("tokenizer.json"))
        self.assertEqual(loaded.local_rag.embedding_dimensions, 384)
        self.assertEqual(loaded.local_rag.embedding_max_tokens, 512)
        self.assertEqual(loaded.local_rag.search_limit, 36)
        self.assertFalse(loaded.tracing.enabled)
        self.assertEqual(loaded.tracing.input_chars, 200_000)
        self.assertEqual(loaded.tracing.output_chars, 1_000)
        self.assertEqual(loaded.tracing.list_limit, 10)
        self.assertFalse(loaded.semantic_cache.enabled)
        self.assertEqual(loaded.semantic_cache.similarity_threshold, 0.97)
        self.assertEqual(loaded.semantic_cache.ttl_seconds, 120)
        self.assertEqual(loaded.semantic_cache.max_items, 222)
        self.assertEqual(loaded.semantic_cache.max_prompt_chars, 2222)
        self.assertEqual(loaded.semantic_cache.max_response_chars, 3333)
        self.assertFalse(loaded.gateway.context_manager_enabled)
        self.assertEqual(loaded.gateway.context_sliding_window_messages, 48)
        self.assertFalse(loaded.gateway.request_queue_enabled)
        self.assertEqual(loaded.gateway.request_queue_max_attempts, 9)
        self.assertEqual(loaded.gateway.request_queue_initial_backoff_seconds, 0.5)
        self.assertEqual(loaded.gateway.request_queue_max_backoff_seconds, 33)
        self.assertTrue(loaded.mcp.client_enabled)
        self.assertEqual(loaded.mcp.client_servers, (("github", "http://127.0.0.1:9001/mcp"), ("docs", "http://127.0.0.1:9002/mcp")))
        self.assertEqual(loaded.mcp.client_timeout_seconds, 11)
        self.assertEqual(loaded.mcp.client_server_timeouts["github"], 7)
        self.assertNotIn("docs", loaded.mcp.client_server_timeouts)
        self.assertEqual(loaded.mcp.client_max_retries, 2)
        self.assertEqual(loaded.mcp.client_retry_backoff_seconds, 0.75)
        self.assertEqual(loaded.mcp.client_circuit_breaker_failures, 4)
        self.assertEqual(loaded.mcp.client_circuit_breaker_reset_seconds, 90)
        self.assertTrue(loaded.browser.enabled)
        self.assertFalse(loaded.browser.headless)
        self.assertTrue(loaded.browser.allow_private_hosts)
        self.assertFalse(loaded.browser.require_confirm)
        self.assertEqual(loaded.browser.download_max_bytes, 123456)
        self.assertEqual(loaded.browser.session_ttl_seconds, 600)

    def test_edge_provider_short_alias_is_supported(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "EDGE_INFERENCE_ENABLED": "1",
                "EDGE_PROVIDER": "fake",
                "EDGE_MODE": "cloud",
            },
            clear=True,
        ):
            loaded = Settings.from_env()

        self.assertEqual(loaded.edge.provider, "fake")
        self.assertEqual(loaded.edge.mode, "cloud")

    def test_ocr_env_values_default_and_clamp(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "OCR_MODE": "nope",
                "OCR_PDF_DPI": "12",
                "OCR_MAX_IMAGE_PIXELS": "-5",
                "OCR_FORMULA_TIMEOUT_SECONDS": "1",
            },
            clear=True,
        ):
            loaded = Settings.from_env()

        self.assertEqual(loaded.ocr.mode, "balanced")
        self.assertEqual(loaded.ocr.pdf_dpi, 150)
        self.assertEqual(loaded.ocr.max_image_pixels, 1)
        self.assertEqual(loaded.ocr.formula_timeout_seconds, 5)

    def test_invalid_timeout_env_values_fall_back_to_defaults(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "DEEPSEEK_TIMEOUT_SECONDS": "not-a-number",
                "MULTI_AGENT_TIMEOUT_SECONDS": " ",
                "TAVILY_TIMEOUT_SECONDS": "",
            },
            clear=True,
        ):
            loaded = Settings.from_env()

        self.assertEqual(loaded.deepseek_timeout_seconds, 180)
        self.assertEqual(loaded.multi_agent_timeout_seconds, 3900)
        self.assertEqual(loaded.tavily_timeout_seconds, 45)

    def test_multi_agent_token_budget_defaults_and_env_override(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings.from_env().multi_agent_token_budget, 2_000_000)
        with patch.dict("os.environ", {"MULTI_AGENT_TOKEN_BUDGET": "500000"}, clear=True):
            self.assertEqual(Settings.from_env().multi_agent_token_budget, 500_000)
        with patch.dict("os.environ", {"MULTI_AGENT_TOKEN_BUDGET": "not-a-number"}, clear=True):
            self.assertEqual(Settings.from_env().multi_agent_token_budget, 2_000_000)

    def test_agent_models_default_to_pro(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            loaded = Settings.from_env()

        self.assertEqual(
            dict(loaded.agent_models),
            {
                "planner": "deepseek-v4-pro",
                "researcher": "deepseek-v4-pro",
                "coder": "deepseek-v4-pro",
                "reasoner": "deepseek-v4-pro",
                "critic": "deepseek-v4-pro",
            },
        )

    def test_agent_model_env_overrides_per_role_with_fallback(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AGENT_MODEL_PLANNER": "flash",
                "AGENT_MODEL_CRITIC": "deepseek-v4-flash",
                "AGENT_MODEL_CODER": "gpt-9",
            },
            clear=True,
        ):
            loaded = Settings.from_env()

        self.assertEqual(loaded.agent_models["planner"], "deepseek-v4-flash")
        self.assertEqual(loaded.agent_models["critic"], "deepseek-v4-flash")
        # 未识别的值回退到默认 pro，不会把脏值塞进配置
        self.assertEqual(loaded.agent_models["coder"], "deepseek-v4-pro")
        self.assertEqual(loaded.agent_models["researcher"], "deepseek-v4-pro")
        self.assertEqual(loaded.agent_models["reasoner"], "deepseek-v4-pro")
    def test_auth_token_persists_across_settings_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            root = Path(tmp)
            with self.subTest("create"):
                first = Settings.from_env(root=root)
            with self.subTest("reuse"):
                second = Settings.from_env(root=root)

        self.assertEqual(first.auth.token, second.auth.token)

    def test_skill_dirs_follow_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"AUTH_TOKEN": "token"}, clear=True):
            root = Path(tmp)
            loaded = Settings.from_env(root=root)

        self.assertEqual(loaded.skills.skills_dir, root / ".skills")
        self.assertEqual(loaded.skills.builtin_skills_dir, root / "skills" / "builtin")

    def test_android_packaging_paths_can_be_overridden_by_env(self) -> None:
        with tempfile.TemporaryDirectory() as root_tmp, tempfile.TemporaryDirectory() as static_tmp:
            with patch.dict(
                "os.environ",
                {
                    "DEEPSEEK_MOBILE_ROOT": root_tmp,
                    "DEEPSEEK_MOBILE_STATIC_DIR": static_tmp,
                    "AUTH_TOKEN": "android-token",
                },
                clear=True,
            ):
                loaded = Settings.from_env()

                self.assertEqual(loaded.root, Path(root_tmp).resolve())
                self.assertEqual(loaded.static_dir, Path(static_tmp).resolve())
                self.assertEqual(loaded.auth.token, "android-token")

    def test_load_or_create_auth_token_reuses_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".auth-token").write_text("fixed-local-token\n", encoding="utf-8")

            self.assertEqual(load_or_create_auth_token(root), "fixed-local-token")


if __name__ == "__main__":
    unittest.main()
