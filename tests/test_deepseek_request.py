from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import deepseek_infra.infra.gateway.deepseek_client as deepseek_client
from deepseek_infra.infra.gateway.deepseek_client import build_deepseek_request, call_deepseek, stream_deepseek, validate_deepseek_payload
from deepseek_infra.infra.gateway.edge_inference import EdgeCompletion, EdgeRouteDecision
from deepseek_infra.core.errors import AppError, ErrorCode


class DeepSeekRequestTests(unittest.TestCase):
    def test_validate_deepseek_payload_requires_api_key(self) -> None:
        with self.assertRaises(AppError) as cm:
            validate_deepseek_payload({"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(cm.exception.code, ErrorCode.MISSING_API_KEY)

    def test_user_image_attachment_becomes_multimodal_content(self) -> None:
        img = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        req = build_deepseek_request(
            {
                "apiKey": "k",
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "user", "content": "这是什么", "attachments": [{"kind": "image", "name": "a.png", "imageData": img}]}
                ],
            },
            stream=False,
        )
        user = next(m for m in req.body["messages"] if m["role"] == "user")
        self.assertIsInstance(user["content"], list)
        types = [part.get("type") for part in user["content"]]
        self.assertIn("text", types)
        self.assertIn("image_url", types)
        image_part = next(part for part in user["content"] if part.get("type") == "image_url")
        self.assertEqual(image_part["image_url"]["url"], img)

    def test_image_message_forces_vision_model(self) -> None:
        img = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        req = build_deepseek_request(
            {
                "apiKey": "k",
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "看图", "attachments": [{"kind": "image", "imageData": img}]}],
            },
            stream=False,
        )
        self.assertEqual(req.body["model"], "deepseek-v4-pro")

    def test_ppt_request_forces_create_pptx_tool(self) -> None:
        req = build_deepseek_request(
            {
                "apiKey": "k",
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "帮我做一个介绍 Git 的 PPT"}],
            },
            stream=True,
        )

        self.assertEqual(req.body["tool_choice"], {"type": "function", "function": {"name": "create_pptx"}})
        self.assertIn("create_pptx", [tool["function"]["name"] for tool in req.body["tools"]])
        dynamic_context = req.body["messages"][-1]["content"]
        self.assertIn("[Skill: slides]", dynamic_context)
        self.assertIn("contact-sheet", dynamic_context)
        self.assertIn("create_pptx", dynamic_context)

    def test_mindmap_request_forces_create_mindmap_tool(self) -> None:
        req = build_deepseek_request(
            {
                "apiKey": "k",
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "帮我画一个产品发布思维导图"}],
            },
            stream=True,
        )

        self.assertEqual(req.body["tool_choice"], {"type": "function", "function": {"name": "create_mindmap"}})
        self.assertIn("create_mindmap", [tool["function"]["name"] for tool in req.body["tools"]])

    def test_non_ppt_request_does_not_include_slides_skill_context(self) -> None:
        req = build_deepseek_request(
            {
                "apiKey": "k",
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "帮我总结这段话"}],
            },
            stream=False,
        )

        self.assertNotIn("[Skill: slides]", req.body["messages"][-1]["content"])

    def test_image_attachment_without_base64_stays_text_only(self) -> None:
        req = build_deepseek_request(
            {
                "apiKey": "k",
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "历史图", "attachments": [{"kind": "image", "name": "old.png"}]}],
            },
            stream=False,
        )
        user = next(m for m in req.body["messages"] if m["role"] == "user")
        self.assertIsInstance(user["content"], str)
        self.assertEqual(req.body["model"], "deepseek-v4-flash")

    def test_stream_deepseek_error_event_includes_code(self) -> None:
        events: list[dict[str, Any]] = []

        stream_deepseek({"messages": [{"role": "user", "content": "hi"}]}, events.append)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("Missing DeepSeek API Key", str(events[0]["error"]))
        self.assertEqual(events[0]["code"], ErrorCode.MISSING_API_KEY.value)

    def test_call_deepseek_routes_simple_request_to_edge_without_api_key(self) -> None:
        route = EdgeRouteDecision(
            True,
            "simple_task_local",
            "auto",
            "llama_cpp",
            {"modelName": "DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M", "quantization": "Q4_K_M", "nCtx": 4096, "nGpuLayers": 0},
        )
        completion = EdgeCompletion(
            content="local hello",
            reasoning="",
            model="DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M",
            usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            provider="llama_cpp",
        )

        with (
            patch.object(deepseek_client, "edge_route_for_payload", return_value=route),
            patch.object(deepseek_client.edge_manager, "complete", return_value=completion) as complete,
            patch("urllib.request.urlopen") as urlopen,
        ):
            result = call_deepseek({"messages": [{"role": "user", "content": "hello"}]})

        complete.assert_called_once()
        urlopen.assert_not_called()
        self.assertEqual(result["content"], "local hello")
        self.assertEqual(result["model"], "DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M")
        self.assertEqual(result["diagnostics"]["edgeInference"]["used"], True)
        self.assertEqual(result["diagnostics"]["edgeInference"]["quantization"], "Q4_K_M")

    def test_call_deepseek_falls_back_to_edge_when_cloud_is_unreachable_for_simple_task(self) -> None:
        cloud_route = EdgeRouteDecision(False, "complex_task_cloud", "auto", "llama_cpp", {"modelName": "local"})
        fallback_route = EdgeRouteDecision(True, "cloud_unavailable_simple_local", "auto", "llama_cpp", {"modelName": "local"})
        completion = EdgeCompletion(
            content="offline local answer",
            reasoning="",
            model="local",
            usage={"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8},
            provider="llama_cpp",
        )

        with (
            patch.object(deepseek_client, "edge_route_for_payload", return_value=cloud_route),
            patch.object(deepseek_client, "edge_fallback_route", return_value=fallback_route),
            patch.object(deepseek_client.edge_manager, "complete", return_value=completion),
            patch("urllib.request.urlopen", side_effect=deepseek_client.urllib.error.URLError("offline")),
        ):
            result = call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "summarize this"}]})

        self.assertEqual(result["content"], "offline local answer")
        self.assertEqual(result["diagnostics"]["edgeInference"]["routeReason"], "cloud_unavailable_simple_local")
        self.assertIn("offline", result["diagnostics"]["edgeInference"]["fallbackError"])

    def test_stream_deepseek_can_stream_from_edge_without_api_key(self) -> None:
        events: list[dict[str, Any]] = []
        route = EdgeRouteDecision(True, "simple_task_local", "auto", "llama_cpp", {"modelName": "local", "nCtx": 4096})

        with (
            patch.object(deepseek_client, "edge_route_for_payload", return_value=route),
            patch.object(deepseek_client.edge_manager, "stream", return_value=iter(["local ", "stream"])),
            patch("urllib.request.urlopen") as urlopen,
        ):
            stream_deepseek({"messages": [{"role": "user", "content": "hello"}]}, events.append)

        urlopen.assert_not_called()
        done = [event for event in events if event.get("type") == "done"][0]
        self.assertEqual(done["content"], "local stream")
        self.assertEqual(done["diagnostics"]["edgeInference"]["used"], True)

    def test_build_deepseek_request_clamps_flash_temperature(self) -> None:
        prepared = build_deepseek_request(
            {
                "apiKey": "test",
                "model": "flash",
                "temperature": 4,
                "thinkingEnabled": False,
                "messages": [{"role": "user", "content": "Hello"}],
            },
            stream=False,
        )

        self.assertEqual(prepared.body["model"], "deepseek-v4-flash")
        self.assertEqual(prepared.body["temperature"], 2)
        self.assertEqual(prepared.body["top_p"], 1.0)
        self.assertFalse(prepared.body["stream"])

    def test_build_deepseek_request_sets_flash_sampling_defaults(self) -> None:
        prepared = build_deepseek_request(
            {
                "apiKey": "test",
                "model": "flash",
                "thinkingEnabled": False,
                "messages": [{"role": "user", "content": "Hello"}],
            },
            stream=False,
        )

        self.assertEqual(prepared.body["temperature"], 1.0)
        self.assertEqual(prepared.body["top_p"], 1.0)

    def test_build_deepseek_request_accepts_configured_reasoning_effort(self) -> None:
        prepared = build_deepseek_request(
            {
                "apiKey": "test",
                "model": "expert",
                "reasoningEffort": "low",
                "messages": [{"role": "user", "content": "Plan it"}],
            },
            stream=False,
        )

        self.assertEqual(prepared.body["reasoning_effort"], "low")
        self.assertEqual(prepared.body["thinking"], {"type": "enabled"})

    def test_build_deepseek_request_defaults_invalid_reasoning_effort_to_high(self) -> None:
        prepared = build_deepseek_request(
            {
                "apiKey": "test",
                "model": "expert",
                "reasoningEffort": "expensive",
                "messages": [{"role": "user", "content": "Plan it"}],
            },
            stream=False,
        )

        self.assertEqual(prepared.body["reasoning_effort"], "high")

    def test_build_deepseek_request_includes_local_tools_by_default(self) -> None:
        prepared = build_deepseek_request(
            {"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "算一下 23!"}]},
            stream=False,
        )

        tool_names = [tool["function"]["name"] for tool in prepared.body["tools"]]
        self.assertIn("python_eval", tool_names)
        self.assertIn("search_files", tool_names)
        self.assertIn("fetch_url", tool_names)
        self.assertIn("suggest_memory", tool_names)
        self.assertIn("create_reminder", tool_names)
        self.assertIn("recall_memory", tool_names)
        self.assertIn("list_project_files", tool_names)
        self.assertIn("data_transform", tool_names)
        self.assertIn("generate_chart", tool_names)
        self.assertEqual(prepared.body["tool_choice"], "auto")
        for tool in prepared.body["tools"]:
            with self.subTest(tool=tool["function"]["name"]):
                self.assertIs(tool["function"]["strict"], True)
                self.assertIs(tool["function"]["parameters"]["additionalProperties"], False)
        self.assertIn("并行发起多个工具调用", prepared.body["messages"][0]["content"])

    def test_build_deepseek_request_can_disable_local_tools(self) -> None:
        prepared = build_deepseek_request(
            {"apiKey": "test", "model": "expert", "toolsEnabled": False, "messages": [{"role": "user", "content": "hi"}]},
            stream=False,
        )

        self.assertNotIn("tools", prepared.body)

    def test_auto_search_exposes_web_search_without_prefetching(self) -> None:
        with patch.object(deepseek_client, "search_multiple") as mocked:
            prepared_call = deepseek_client.prepare_deepseek_call(
                {
                    "apiKey": "test",
                    "model": "expert",
                    "searchEnabled": True,
                    "searchMode": "auto",
                    "messages": [{"role": "user", "content": "latest docs"}],
                },
                stream=False,
            )

        mocked.assert_not_called()
        self.assertIsNone(prepared_call.search_data)
        tool_names = [tool["function"]["name"] for tool in prepared_call.request.body["tools"]]
        self.assertIn("web_search", tool_names)

    def test_search_hint_is_trailing_dynamic_context_for_cache_friendliness(self) -> None:
        messages = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "latest docs"},
        ]
        without_search = build_deepseek_request(
            {"apiKey": "test", "model": "expert", "searchEnabled": False, "messages": messages},
            stream=False,
        )
        with_search = build_deepseek_request(
            {"apiKey": "test", "model": "expert", "searchEnabled": True, "searchMode": "auto", "messages": messages},
            stream=False,
        )

        self.assertEqual(without_search.body["messages"][:-1], with_search.body["messages"][:-1])
        self.assertEqual(without_search.body["messages"][-1]["role"], "system")
        self.assertEqual(with_search.body["messages"][-1]["role"], "system")
        self.assertIn(deepseek_client.CURRENT_TIME_CONTEXT_HEADER, without_search.body["messages"][-1]["content"])
        self.assertIn(deepseek_client.CURRENT_TIME_CONTEXT_HEADER, with_search.body["messages"][-1]["content"])
        self.assertIn(deepseek_client.WEB_SEARCH_SYSTEM_HINT, with_search.body["messages"][-1]["content"])
        self.assertNotIn(deepseek_client.WEB_SEARCH_SYSTEM_HINT, with_search.body["messages"][0]["content"])

    def test_current_time_context_includes_local_and_utc_time(self) -> None:
        context = deepseek_client.format_current_time_context(datetime(2026, 5, 30, 10, 20, 30, tzinfo=timezone.utc))

        self.assertIn(deepseek_client.CURRENT_TIME_CONTEXT_HEADER, context)
        self.assertIn("Local time: 2026-05-30T10:20:30+00:00", context)
        self.assertIn("UTC time: 2026-05-30T10:20:30Z", context)

    def test_off_search_hides_web_search_tool(self) -> None:
        prepared = build_deepseek_request(
            {
                "apiKey": "test",
                "model": "expert",
                "searchEnabled": True,
                "searchMode": "off",
                "messages": [{"role": "user", "content": "latest docs"}],
            },
            stream=False,
        )

        tool_names = [tool["function"]["name"] for tool in prepared.body["tools"]]
        self.assertNotIn("web_search", tool_names)
        self.assertNotIn("compare_search_results", tool_names)

    def test_force_search_prefetches_once_even_for_simple_question(self) -> None:
        search_data = {"status": "done", "query": "1+1=?", "results": [], "rounds": [{"round": 1, "query": "1+1?", "results": []}], "cached": False}
        with patch.object(deepseek_client, "search_multiple", return_value=search_data) as mocked:
            prepared_call = deepseek_client.prepare_deepseek_call(
                {
                    "apiKey": "test",
                    "model": "expert",
                    "searchEnabled": True,
                    "searchMode": "force",
                    "messages": [{"role": "user", "content": "1+1=?"}],
                },
                stream=False,
            )

        mocked.assert_called_once()
        self.assertEqual(prepared_call.search_data, search_data)

    def test_force_search_failure_emits_system_note(self) -> None:
        notes: list[str] = []
        search_data = {
            "status": "error",
            "query": "latest docs",
            "results": [],
            "rounds": [{"round": 1, "status": "error", "query": "latest docs", "error": "Remote end closed connection without response", "results": []}],
            "cached": False,
        }

        with patch.object(deepseek_client, "search_multiple", return_value=search_data):
            result = deepseek_client.search_if_needed(
                {
                    "searchEnabled": True,
                    "searchMode": "force",
                    "messages": [{"role": "user", "content": "latest docs"}],
                },
                system_note_callback=notes.append,
            )

        self.assertEqual(result, search_data)
        self.assertGreaterEqual(len(notes), 2)
        self.assertIn("预取搜索失败", notes[-1])
        self.assertIn("Remote end closed connection", notes[-1])

    def test_web_search_callback_deduplicates_queries_and_continues_round_index(self) -> None:
        initial_search = {
            "status": "done",
            "query": "docs",
            "results": [],
            "rounds": [{"round": 1, "status": "done", "query": "docs", "answer": "", "results": []}],
            "cached": False,
        }
        progress: list[dict[str, Any]] = []

        def fake_single_round(
            query: str,
            *,
            intent: str,
            round_index: int,
            citation_offset: int,
            tavily_api_key: str,
            progress_callback: object,
            use_cache: bool = False,
        ) -> dict[str, Any]:
            assert callable(progress_callback)
            progress_callback({"round": round_index, "query": query, "status": "done", "results": []})
            return {"query": query, "round": round_index, "intent": intent, "citation_offset": citation_offset, "results": [], "status": "done"}

        with patch.object(deepseek_client, "search_single_round", side_effect=fake_single_round) as mocked:
            callback, current_search = deepseek_client.web_search_callback_for_turn(
                {"messages": [{"role": "user", "content": "docs"}]},
                initial_search,
                progress_callback=progress.append,
            )
            cached = callback(" docs ", "general")
            fresh = callback("docs examples", "technical")

        mocked.assert_called_once()
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["round"], 1)
        self.assertEqual(fresh["round"], 2)
        self.assertEqual(fresh["citation_offset"], 0)
        current = current_search()
        assert current is not None
        self.assertEqual(current["rounds"][-1]["round"], 2)
        self.assertTrue(progress)

    def test_web_search_callback_stops_at_turn_limit(self) -> None:
        progress: list[dict[str, Any]] = []

        def fake_single_round(
            query: str,
            *,
            intent: str,
            round_index: int,
            citation_offset: int,
            tavily_api_key: str,
            progress_callback: object,
            use_cache: bool = False,
        ) -> dict[str, Any]:
            assert callable(progress_callback)
            return {"query": query, "round": round_index, "intent": intent, "citation_offset": citation_offset, "results": [], "status": "done"}

        with patch.object(deepseek_client, "search_single_round", side_effect=fake_single_round) as mocked:
            callback, current_search = deepseek_client.web_search_callback_for_turn(
                {"messages": [{"role": "user", "content": "docs"}]},
                None,
                progress_callback=progress.append,
                turn_limit=1,
            )
            first = callback("docs one", "general")
            second = callback("docs two", "general")

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(first["status"], "done")
        self.assertEqual(second["status"], "error")
        self.assertIn(deepseek_client.WEB_SEARCH_LIMIT_ERROR, second["error"])
        current = current_search()
        assert current is not None
        self.assertEqual(current["rounds"][-1]["status"], "error")

    def test_build_deepseek_request_keeps_system_stable_and_moves_dynamic_context_to_latest_user(self) -> None:
        prepared = build_deepseek_request(
            {
                "apiKey": "test",
                "model": "expert",
                "systemPrompt": "System prompt",
                "contextSummary": "Older summary",
                "continuationContext": "Continue here",
                "searchContext": "Search context",
                "messages": [
                    {"role": "user", "content": "Older question"},
                    {"role": "assistant", "content": "Older answer"},
                    {"role": "user", "content": "Question"},
                ],
            },
            stream=True,
            memory_state={"enabled": True, "notice": "", "context": "Memory context", "hitCount": 1},
        )

        system_message = prepared.body["messages"][0]
        latest_user_message = prepared.body["messages"][-1]
        self.assertEqual(prepared.body["model"], "deepseek-v4-pro")
        self.assertTrue(prepared.body["stream"])
        self.assertEqual(system_message["role"], "system")
        self.assertIn("System prompt", system_message["content"])
        # context_summary 改走 dynamic turn-context（注入 latest user），让 system 保持
        # 字面稳定 → DeepSeek prompt cache 能贯穿历史命中，不会因摘要更新而全 miss。
        self.assertNotIn("Older summary", system_message["content"])
        self.assertNotIn("Memory context", system_message["content"])
        self.assertNotIn("Search context", system_message["content"])
        self.assertIn("Older summary", latest_user_message["content"])
        self.assertIn("Memory context", latest_user_message["content"])
        self.assertIn("Search context", latest_user_message["content"])
        self.assertIn("Continue here", latest_user_message["content"])

    def test_dynamic_context_changes_only_latest_user_message(self) -> None:
        payload = {
            "apiKey": "test",
            "model": "expert",
            "systemPrompt": "System prompt",
            "contextSummary": "Older summary",
            "messages": [
                {"role": "user", "content": "First"},
                {"role": "assistant", "content": "Second"},
                {"role": "user", "content": "Latest"},
            ],
        }

        first = build_deepseek_request(payload, stream=False, memory_state={"enabled": True, "notice": "", "context": "Memory A", "hitCount": 1})
        second = build_deepseek_request(payload, stream=False, memory_state={"enabled": True, "notice": "", "context": "Memory B", "hitCount": 1})

        self.assertEqual(first.body["messages"][:-1], second.body["messages"][:-1])
        self.assertNotEqual(first.body["messages"][-1], second.body["messages"][-1])

    def test_system_prompt_stays_stable_when_history_appends(self) -> None:
        first = build_deepseek_request(
            {
                "apiKey": "test",
                "model": "expert",
                "systemPrompt": "System prompt",
                "contextSummary": "Older summary",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "q1"},
                ],
            },
            stream=False,
        )
        second = build_deepseek_request(
            {
                "apiKey": "test",
                "model": "expert",
                "systemPrompt": "System prompt",
                "contextSummary": "Older summary",
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "q1"},
                    {"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "q2"},
                ],
            },
            stream=False,
        )

        self.assertEqual(first.body["messages"][0], second.body["messages"][0])

    def test_history_prefix_stays_stable_when_appending_messages(self) -> None:
        base_messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        first = build_deepseek_request(
            {"apiKey": "test", "model": "expert", "messages": [*base_messages, {"role": "user", "content": "q1"}]},
            stream=False,
        )
        second = build_deepseek_request(
            {
                "apiKey": "test",
                "model": "expert",
                "messages": [
                    *base_messages,
                    {"role": "user", "content": "q1"},
                    {"role": "assistant", "content": "a1"},
                    {"role": "user", "content": "q2"},
                ],
            },
            stream=False,
        )

        self.assertEqual(first.body["messages"][:-1], second.body["messages"][:-3])

    def test_build_deepseek_request_requires_compression_before_over_limit_without_summary(self) -> None:
        messages = [{"role": "user", "content": f"Message {index}"} for index in range(41)]

        with self.assertRaises(AppError) as cm:
            build_deepseek_request({"apiKey": "test", "model": "expert", "messages": messages}, stream=False)

        self.assertEqual(cm.exception.code, ErrorCode.CONTEXT_COMPRESSION_REQUIRED)
        self.assertEqual(cm.exception.status, 409)

    def test_build_deepseek_request_applies_sliding_window_when_summary_exists(self) -> None:
        messages = [{"role": "user", "content": f"Message {index}"} for index in range(41)]

        prepared = build_deepseek_request(
            {"apiKey": "test", "model": "expert", "contextSummary": "Summary", "messages": messages},
            stream=False,
        )

        request_messages = [message for message in prepared.body["messages"] if message["role"] in {"user", "assistant"}]
        self.assertEqual(len(request_messages), 34)
        self.assertIn("Message 7", request_messages[0]["content"])
        self.assertIn("Message 40", request_messages[-1]["content"])
        self.assertTrue(prepared.diagnostics["contextManager"]["slidingWindowApplied"])
        self.assertEqual(prepared.diagnostics["contextManager"]["droppedMessages"], 7)

    def test_call_deepseek_adds_cache_diagnostics_from_usage(self) -> None:
        response = {
            "id": "response-id",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": "answer"}}],
            "usage": {"prompt_cache_hit_tokens": 300, "prompt_cache_miss_tokens": 100},
        }
        with patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(response).encode("utf-8"))):
            result = call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "Question"}]})

        self.assertEqual(result["diagnostics"]["cacheHitTokens"], 300)
        self.assertEqual(result["diagnostics"]["cacheMissTokens"], 100)
        self.assertEqual(result["diagnostics"]["cacheHitRate"], 75.0)

    def test_call_deepseek_uses_configured_timeout(self) -> None:
        response = {
            "id": "response-id",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": "answer"}}],
            "usage": {},
        }
        with (
            patch.object(deepseek_client, "DEEPSEEK_TIMEOUT_SECONDS", 9),
            patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(response).encode("utf-8"))) as mocked,
        ):
            call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "Question"}]})

        self.assertEqual(mocked.call_args.kwargs["timeout"], 9)

    def test_call_deepseek_executes_tool_call_before_final_answer(self) -> None:
        tool_response = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "Need exact factorial.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "python_eval", "arguments": json.dumps({"expression": "factorial(5)"})},
                            }
                        ],
                    }
                }
            ],
            "usage": {},
        }
        final_response = {
            "id": "final-response",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": "120"}}],
            "usage": {},
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=[FakeResponse(json.dumps(tool_response).encode("utf-8")), FakeResponse(json.dumps(final_response).encode("utf-8"))],
        ) as mocked:
            result = call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "23!"}]})

        second_request = mocked.call_args_list[1].args[0]
        second_body = json.loads(second_request.data.decode("utf-8"))
        assistant_messages = [message for message in second_body["messages"] if message.get("role") == "assistant"]
        tool_messages = [message for message in second_body["messages"] if message.get("role") == "tool"]
        self.assertEqual(result["content"], "120")
        self.assertEqual(result["diagnostics"]["toolCallCount"], 1)
        self.assertEqual(result["diagnostics"]["toolNames"], ["python_eval"])
        # V4-Pro thinking 模式要求带 tool_calls 的 assistant 消息回填 reasoning_content，
        # 缺失会让上游报错（“must be passed back to the API”），这里确认它被原样带回下一轮。
        self.assertEqual(assistant_messages[-1]["reasoning_content"], "Need exact factorial.")
        self.assertIn("120", tool_messages[0]["content"])

    def test_tool_loop_preserves_prompt_prefix_for_cache(self) -> None:
        # DeepSeek prompt cache 按字面前缀命中。带工具调用的回合，第二次请求的 messages
        # 必须是第一次请求 messages 的“严格前缀延伸”：只在末尾追加 assistant 工具调用 +
        # 工具结果，绝不改写已有消息或在中间插入易变内容，否则后半段历史会整段 cache miss。
        long_reasoning = "长篇推理" * 200
        tool_response = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": long_reasoning,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "python_eval", "arguments": json.dumps({"expression": "factorial(5)"})},
                            }
                        ],
                    }
                }
            ],
            "usage": {},
        }
        final_response = {
            "id": "final-response",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": "120"}}],
            "usage": {},
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=[FakeResponse(json.dumps(tool_response).encode("utf-8")), FakeResponse(json.dumps(final_response).encode("utf-8"))],
        ) as mocked:
            call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "5!"}]})

        first_messages = json.loads(mocked.call_args_list[0].args[0].data.decode("utf-8"))["messages"]
        second_messages = json.loads(mocked.call_args_list[1].args[0].data.decode("utf-8"))["messages"]
        # 第二次请求 = 第一次请求的严格前缀 + 追加的工具交换
        self.assertEqual(second_messages[: len(first_messages)], first_messages)
        self.assertGreater(len(second_messages), len(first_messages))
        # 追加的 assistant 工具调用消息必须保留 reasoning_content（V4-Pro thinking 模式硬性要求）。
        appended_assistant = next(m for m in second_messages[len(first_messages):] if m.get("role") == "assistant")
        self.assertEqual(appended_assistant["reasoning_content"], long_reasoning)

    def test_tool_round_limit_keeps_tools_prefix_stable_for_cache(self) -> None:
        # 工具轮次达上限、改为直接作答时，体量最大的收尾请求仍要保留 tools 前缀：tools 位于
        # prompt 前缀最前端（数千 token），删掉会让这次请求整段 cache miss。这里端到端验证：
        # 三次上游请求的 tools 数组都在且字面一致，收尾请求用 tool_choice="none" 禁用工具。
        tool_response = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "need a tool",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "python_eval", "arguments": json.dumps({"expression": "factorial(5)"})},
                            }
                        ],
                    }
                }
            ],
            "usage": {},
        }
        final_response = {
            "id": "final-response",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": "done"}}],
            "usage": {},
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=[
                FakeResponse(json.dumps(tool_response).encode("utf-8")),
                FakeResponse(json.dumps(tool_response).encode("utf-8")),
                FakeResponse(json.dumps(final_response).encode("utf-8")),
            ],
        ) as mocked:
            call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "5!"}]}, max_tool_rounds=1)

        bodies = [json.loads(call.args[0].data.decode("utf-8")) for call in mocked.call_args_list]
        self.assertEqual(len(bodies), 3)
        for body in bodies:
            self.assertIn("tools", body)
            self.assertEqual(body["tools"], bodies[0]["tools"])
        self.assertEqual(bodies[-1]["tool_choice"], "none")
        self.assertIn("工具调用次数已经用完", bodies[-1]["messages"][-1]["content"])

    def test_call_deepseek_falls_back_to_local_pptx_when_model_skips_tool(self) -> None:
        response = {
            "id": "final-response",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": "我无法直接生成 .pptx 文件。\n\n1. 封面 - Git 介绍\n2. 什么是版本控制？"}}],
            "usage": {},
        }
        ppt_result = {
            "fileId": "a" * 32,
            "filename": "Git介绍.pptx",
            "slideCount": 3,
            "downloadUrl": "/api/download?id=" + "a" * 32,
        }
        with (
            patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(response).encode("utf-8"))),
            patch.object(deepseek_client, "create_presentation_from_text", return_value=ppt_result),
        ):
            result = call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "帮我做一个介绍 Git 的 PPT"}]})

        self.assertIn("[Git介绍.pptx](/api/download?id=", result["content"])
        self.assertNotIn("无法直接生成", result["content"])
        self.assertEqual(result["diagnostics"]["toolNames"], ["create_pptx"])

    def test_call_deepseek_returns_artifact_link_without_second_upstream_request(self) -> None:
        tool_response = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_artifact",
                                "type": "function",
                                "function": {
                                    "name": "create_pptx",
                                    "arguments": json.dumps(
                                        {
                                            "title": "Roadmap",
                                            "subtitle": "",
                                            "slides": [{"title": "Intro", "bullets": ["A"], "layout": "quote"}],
                                        }
                                    ),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20},
        }
        ppt_result = {
            "fileId": "e" * 32,
            "filename": "Roadmap.pptx",
            "slideCount": 2,
            "downloadUrl": "/api/download?id=" + "e" * 32,
            "outline": [{"page": 1, "title": "Intro", "layout": "quote", "bullets": ["A"]}],
        }
        with (
            patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(tool_response).encode("utf-8"))) as mocked,
            patch("deepseek_infra.infra.tool_runtime.tools.create_presentation", return_value=ppt_result),
        ):
            result = call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "帮我做一个 Roadmap PPT"}]})

        self.assertEqual(mocked.call_count, 1)
        self.assertIn("[Roadmap.pptx](/api/download?id=", result["content"])
        self.assertEqual(result["diagnostics"]["toolNames"], ["create_pptx"])
        self.assertEqual(result["diagnostics"]["cacheHitRate"], 80.0)

    def test_call_deepseek_returns_document_link_without_second_upstream_request(self) -> None:
        tool_response = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_doc",
                                "type": "function",
                                "function": {
                                    "name": "create_document",
                                    "arguments": json.dumps(
                                        {
                                            "format": "docx",
                                            "title": "Plan",
                                            "subtitle": "",
                                            "sections": [
                                                {"heading": "Overview", "body": ["Text"], "bullets": [], "table": {"headers": [], "rows": []}}
                                            ],
                                        }
                                    ),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 70, "prompt_cache_miss_tokens": 30},
        }
        doc_result = {
            "fileId": "f" * 32,
            "filename": "Plan.docx",
            "format": "docx",
            "sectionCount": 1,
            "downloadUrl": "/api/download?id=" + "f" * 32,
            "outline": [{"index": 1, "heading": "Overview", "hasTable": False}],
        }
        with (
            patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(tool_response).encode("utf-8"))) as mocked,
            patch("deepseek_infra.infra.tool_runtime.tools.create_document", return_value=doc_result),
        ):
            result = call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "帮我做一个 Word 方案"}]})

        self.assertEqual(mocked.call_count, 1)
        self.assertIn("[Plan.docx](/api/download?id=", result["content"])
        self.assertEqual(result["diagnostics"]["toolNames"], ["create_document"])
        self.assertEqual(result["diagnostics"]["cacheHitRate"], 70.0)

    def test_call_deepseek_returns_mindmap_link_without_second_upstream_request(self) -> None:
        tool_response = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_map",
                                "type": "function",
                                "function": {
                                    "name": "create_mindmap",
                                    "arguments": json.dumps(
                                        {
                                            "title": "Launch",
                                            "subtitle": "",
                                            "nodes": [{"label": "Market", "children": []}],
                                        }
                                    ),
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 85, "prompt_cache_miss_tokens": 15},
        }
        map_result = {
            "fileId": "2" * 32,
            "filename": "Launch.svg",
            "format": "svg",
            "nodeCount": 2,
            "downloadUrl": "/api/download?id=" + "2" * 32,
            "outline": [{"label": "Market", "children": []}],
        }
        with (
            patch("urllib.request.urlopen", return_value=FakeResponse(json.dumps(tool_response).encode("utf-8"))) as mocked,
            patch("deepseek_infra.infra.tool_runtime.tools.create_mindmap", return_value=map_result),
        ):
            result = call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "帮我画一个 Launch 思维导图"}]})

        self.assertEqual(mocked.call_count, 1)
        self.assertIn("![Launch.svg](/api/download?id=", result["content"])
        self.assertIn("[Launch.svg](/api/download?id=", result["content"])
        self.assertEqual(result["diagnostics"]["toolNames"], ["create_mindmap"])
        self.assertEqual(result["diagnostics"]["cacheHitRate"], 85.0)

    def test_ensure_pptx_response_surfaces_existing_tool_download(self) -> None:
        body = {
            "messages": [
                {
                    "role": "tool",
                    "name": "create_pptx",
                    "content": json.dumps(
                        {
                            "ok": True,
                            "tool": "create_pptx",
                            "result": {
                                "filename": "deck.pptx",
                                "slideCount": 5,
                                "downloadUrl": "/api/download?id=" + "b" * 32,
                            },
                        }
                    ),
                }
            ]
        }

        content, created = deepseek_client.ensure_pptx_response(
            {"messages": [{"role": "user", "content": "请制作 PPT"}]},
            "已经完成。",
            body,
        )

        self.assertFalse(created)
        self.assertIn("[deck.pptx](/api/download?id=", content)

    def test_ensure_pptx_response_uses_local_absolute_download_url(self) -> None:
        body = {
            "messages": [
                {
                    "role": "tool",
                    "name": "create_pptx",
                    "content": json.dumps(
                        {
                            "ok": True,
                            "tool": "create_pptx",
                            "result": {
                                "filename": "deck.pptx",
                                "slideCount": 5,
                                "downloadUrl": "/api/download?id=" + "c" * 32,
                            },
                        }
                    ),
                }
            ]
        }

        content, created = deepseek_client.ensure_pptx_response(
            {"localBaseUrl": "http://127.0.0.1:8000", "messages": [{"role": "user", "content": "请制作 PPT"}]},
            "已经完成。",
            body,
        )

        self.assertFalse(created)
        self.assertIn("[deck.pptx](http://127.0.0.1:8000/api/download?id=", content)

    def test_existing_official_domain_pptx_link_is_rewritten_local(self) -> None:
        content, created = deepseek_client.ensure_pptx_response(
            {"localBaseUrl": "http://127.0.0.1:8000", "messages": [{"role": "user", "content": "请制作 PPT"}]},
            "下载：[deck.pptx](https://chat.deepseek.com/api/download?id=" + "d" * 32 + ")",
            {"messages": []},
        )

        self.assertFalse(created)
        self.assertIn("http://127.0.0.1:8000/api/download?id=" + "d" * 32, content)
        self.assertNotIn("chat.deepseek.com/api/download", content)

    def test_call_deepseek_preserves_upstream_tool_calls_for_cache(self) -> None:
        upstream_arguments = json.dumps({"query": "latest docs", "intent": "fresh"})
        tool_response = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "random-upstream-id",
                                "type": "function",
                                "function": {
                                    "name": "web_search",
                                    "arguments": upstream_arguments,
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {},
        }
        final_response = {
            "id": "final-response",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": "answer"}}],
            "usage": {},
        }

        def fake_single_round(
            query: str,
            *,
            intent: str,
            round_index: int,
            citation_offset: int,
            tavily_api_key: str,
            progress_callback: object,
            use_cache: bool = False,
        ) -> dict[str, Any]:
            return {
                "query": query,
                "round": round_index,
                "intent": intent,
                "results": [{"cite": "[^W1]", "title": "Docs", "url": "https://example.com", "snippet": "ok"}],
                "status": "done",
                "cached": False,
            }

        with (
            patch.object(deepseek_client, "search_single_round", side_effect=fake_single_round),
            patch(
                "urllib.request.urlopen",
                side_effect=[FakeResponse(json.dumps(tool_response).encode("utf-8")), FakeResponse(json.dumps(final_response).encode("utf-8"))],
            ) as mocked,
        ):
            result = call_deepseek(
                {
                    "apiKey": "test",
                    "model": "expert",
                    "searchEnabled": True,
                    "searchMode": "auto",
                    "messages": [{"role": "user", "content": "latest docs"}],
                }
            )

        second_request = mocked.call_args_list[1].args[0]
        second_body = json.loads(second_request.data.decode("utf-8"))
        assistant_messages = [message for message in second_body["messages"] if message.get("role") == "assistant"]
        tool_messages = [message for message in second_body["messages"] if message.get("role") == "tool"]
        tool_call = assistant_messages[-1]["tool_calls"][0]
        self.assertEqual(result["content"], "answer")
        self.assertEqual(tool_call["id"], "random-upstream-id")
        self.assertEqual(tool_call["function"]["arguments"], upstream_arguments)
        self.assertEqual(tool_messages[-1]["tool_call_id"], "random-upstream-id")
        self.assertNotIn('"cached"', tool_messages[-1]["content"])

    def test_call_deepseek_aggregates_usage_across_tool_rounds(self) -> None:
        tool_response = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "python_eval", "arguments": json.dumps({"expression": "1+1"})},
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "total_tokens": 105,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
            },
        }
        final_response = {
            "id": "final-response",
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": "2"}}],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 10,
                "total_tokens": 60,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 50,
            },
        }

        with patch(
            "urllib.request.urlopen",
            side_effect=[FakeResponse(json.dumps(tool_response).encode("utf-8")), FakeResponse(json.dumps(final_response).encode("utf-8"))],
        ):
            result = call_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "1+1"}]})

        self.assertEqual(result["usage"]["prompt_tokens"], 150)
        self.assertEqual(result["usage"]["completion_tokens"], 15)
        self.assertEqual(result["usage"]["total_tokens"], 165)
        self.assertEqual(result["diagnostics"]["cacheHitTokens"], 80)
        self.assertEqual(result["diagnostics"]["cacheMissTokens"], 70)
        self.assertEqual(result["diagnostics"]["cacheHitRate"], 53.3)

    def test_stream_deepseek_done_event_adds_cache_diagnostics_from_usage(self) -> None:
        chunk = {
            "id": "response-id",
            "model": "deepseek-v4-pro",
            "usage": {"prompt_cache_hit_tokens": 50, "prompt_cache_miss_tokens": 50},
            "choices": [{"delta": {"content": "answer"}}],
        }
        events: list[dict[str, Any]] = []
        with patch("urllib.request.urlopen", return_value=FakeStream([f"data: {json.dumps(chunk)}\n".encode("utf-8"), b"data: [DONE]\n"])):
            stream_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "Question"}]}, events.append)

        done = [event for event in events if event.get("type") == "done"][0]
        diagnostics = done["diagnostics"]
        assert isinstance(diagnostics, dict)
        self.assertEqual(diagnostics["cacheHitTokens"], 50)
        self.assertEqual(diagnostics["cacheMissTokens"], 50)
        self.assertEqual(diagnostics["cacheHitRate"], 50.0)

    def test_stream_deepseek_executes_streamed_tool_call(self) -> None:
        tool_delta = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "usage": {
                "prompt_tokens": 60,
                "completion_tokens": 4,
                "total_tokens": 64,
                "prompt_cache_hit_tokens": 40,
                "prompt_cache_miss_tokens": 20,
            },
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "Need exact factorial.",
                        "content": "Working. ",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "python_eval", "arguments": json.dumps({"expression": "factorial(5)"})},
                            }
                        ]
                    }
                }
            ],
        }
        final_delta = {
            "id": "final-response",
            "model": "deepseek-v4-pro",
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 6,
                "total_tokens": 36,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 30,
            },
            "choices": [{"delta": {"content": "120"}}],
        }
        events: list[dict[str, Any]] = []
        with patch(
            "urllib.request.urlopen",
            side_effect=[
                FakeStream([f"data: {json.dumps(tool_delta)}\n".encode("utf-8"), b"data: [DONE]\n"]),
                FakeStream([f"data: {json.dumps(final_delta)}\n".encode("utf-8"), b"data: [DONE]\n"]),
            ],
        ) as mocked:
            stream_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "5!"}]}, events.append)

        second_request = mocked.call_args_list[1].args[0]
        second_body = json.loads(second_request.data.decode("utf-8"))
        assistant_messages = [message for message in second_body["messages"] if message.get("role") == "assistant"]
        system_notes = [event for event in events if event.get("type") == "system_note"]
        done = [event for event in events if event.get("type") == "done"][0]
        diagnostics = done["diagnostics"]
        assert isinstance(diagnostics, dict)
        self.assertEqual(assistant_messages[-1]["content"], "Working. ")
        # V4-Pro thinking 模式要求回填 reasoning_content，缺失会让上游报错。
        self.assertEqual(assistant_messages[-1]["reasoning_content"], "Need exact factorial.")
        self.assertTrue(any("python_eval" in str(event.get("text")) for event in system_notes))
        self.assertEqual(done["content"], "Working. 120")
        self.assertEqual(diagnostics["toolCallCount"], 1)
        self.assertEqual(done["usage"]["prompt_tokens"], 90)
        self.assertEqual(done["usage"]["completion_tokens"], 10)
        self.assertEqual(done["usage"]["total_tokens"], 100)
        self.assertEqual(diagnostics["cacheHitTokens"], 40)
        self.assertEqual(diagnostics["cacheMissTokens"], 50)
        self.assertEqual(diagnostics["cacheHitRate"], 44.4)

    def test_stream_deepseek_returns_artifact_link_without_second_upstream_request(self) -> None:
        tool_delta = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 90, "prompt_cache_miss_tokens": 10},
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_ppt",
                                "type": "function",
                                "function": {
                                    "name": "create_pptx",
                                    "arguments": json.dumps(
                                        {
                                            "title": "Roadmap",
                                            "subtitle": "",
                                            "slides": [{"title": "Intro", "bullets": ["A"], "layout": "quote"}],
                                        }
                                    ),
                                },
                            }
                        ]
                    }
                }
            ],
        }
        ppt_result = {
            "fileId": "1" * 32,
            "filename": "Roadmap.pptx",
            "slideCount": 2,
            "downloadUrl": "/api/download?id=" + "1" * 32,
        }
        events: list[dict[str, Any]] = []
        with (
            patch("urllib.request.urlopen", return_value=FakeStream([f"data: {json.dumps(tool_delta)}\n".encode("utf-8"), b"data: [DONE]\n"])) as mocked,
            patch("deepseek_infra.infra.tool_runtime.tools.create_presentation", return_value=ppt_result),
        ):
            stream_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "帮我做 Roadmap PPT"}]}, events.append)

        done = [event for event in events if event.get("type") == "done"][0]
        self.assertEqual(mocked.call_count, 1)
        self.assertIn("[Roadmap.pptx](/api/download?id=", str(done["content"]))
        self.assertEqual(done["diagnostics"]["cacheHitRate"], 90.0)

    def test_stream_deepseek_emits_memory_suggestion_event(self) -> None:
        tool_delta = {
            "id": "tool-response",
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "suggest_memory",
                                    "arguments": json.dumps({"content": "Prefers concise answers", "category": "preference"}),
                                },
                            }
                        ]
                    }
                }
            ],
        }
        final_delta = {
            "id": "final-response",
            "model": "deepseek-v4-pro",
            "choices": [{"delta": {"content": "ok"}}],
        }
        events: list[dict[str, Any]] = []
        with patch(
            "urllib.request.urlopen",
            side_effect=[
                FakeStream([f"data: {json.dumps(tool_delta)}\n".encode("utf-8"), b"data: [DONE]\n"]),
                FakeStream([f"data: {json.dumps(final_delta)}\n".encode("utf-8"), b"data: [DONE]\n"]),
            ],
        ):
            stream_deepseek(
                {
                    "apiKey": "test",
                    "model": "expert",
                    "memoryScope": "project:alpha",
                    "messages": [{"role": "user", "content": "I prefer concise answers"}],
                },
                events.append,
            )

        suggestion = [event for event in events if event.get("type") == "memory_suggestion"][0]
        done = [event for event in events if event.get("type") == "done"][0]
        self.assertEqual(suggestion["content"], "Prefers concise answers")
        self.assertEqual(suggestion["scope"], "project:alpha")
        self.assertEqual(done["memorySuggestions"], [{key: suggestion[key] for key in ("content", "category", "scope", "conflicts")}])

    def test_stream_search_notes_use_system_note_not_reasoning(self) -> None:
        chunk = {
            "id": "response-id",
            "model": "deepseek-v4-pro",
            "choices": [{"delta": {"reasoning_content": "model reasoning", "content": "answer"}}],
        }
        search_data = {
            "status": "done",
            "query": "latest docs",
            "reason": "fresh",
            "results": [{"title": "Docs", "url": "https://example.com", "content": "ok"}],
            "rounds": [],
            "cached": False,
        }
        events: list[dict[str, Any]] = []
        with (
            patch.object(deepseek_client, "search_multiple", return_value=search_data),
            patch("urllib.request.urlopen", return_value=FakeStream([f"data: {json.dumps(chunk)}\n".encode("utf-8"), b"data: [DONE]\n"])),
        ):
            stream_deepseek(
                {
                    "apiKey": "test",
                    "model": "expert",
                    "searchEnabled": True,
                    "searchMode": "on",
                    "messages": [{"role": "user", "content": "latest docs"}],
                },
                events.append,
            )

        system_notes = [event for event in events if event.get("type") == "system_note"]
        reasoning_events = [event for event in events if event.get("type") == "reasoning"]
        done = [event for event in events if event.get("type") == "done"][0]
        self.assertGreaterEqual(len(system_notes), 2)
        self.assertEqual(reasoning_events, [{"type": "reasoning", "text": "model reasoning"}])
        self.assertEqual(done["reasoning"], "model reasoning")
        self.assertNotIn("多轮搜索", str(done["reasoning"]))

    def test_stream_deepseek_converts_sse_error_event(self) -> None:
        events: list[dict[str, Any]] = []

        with patch("urllib.request.urlopen", return_value=FakeStream([b"event: error\n", b'data: {"error":{"message":"stream failed"}}\n'])):
            stream_deepseek({"apiKey": "test", "model": "expert", "messages": [{"role": "user", "content": "Question"}]}, events.append)

        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["error"], "stream failed")
        self.assertEqual(events[-1]["code"], ErrorCode.UPSTREAM_FAILURE.value)

    def test_force_final_answer_keeps_tools_stable_and_appends_hint(self) -> None:
        body: dict[str, Any] = {
            "model": "expert",
            "tools": [{"type": "function"}],
            "tool_choice": "auto",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ],
        }
        result = deepseek_client.force_final_answer_without_tools(body)
        # 保留 tools 让 prompt 前缀稳定（删掉会让这次最大的请求整段 cache miss），
        # 改用 tool_choice="none" 禁止再调用工具。
        self.assertEqual(result["tools"], body["tools"])
        self.assertEqual(result["tool_choice"], "none")
        self.assertEqual(result["model"], "expert")
        self.assertEqual(len(result["messages"]), 3)
        self.assertEqual(result["messages"][-1]["role"], "user")
        self.assertIn("工具调用次数已经用完", result["messages"][-1]["content"])
        self.assertEqual(body["messages"][-1]["role"], "assistant")
        self.assertIn("tools", body)


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self.data


class FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def __iter__(self) -> object:
        return iter(self.lines)


if __name__ == "__main__":
    unittest.main()
