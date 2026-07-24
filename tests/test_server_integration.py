from __future__ import annotations

import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from email.message import Message
import urllib.error
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import deepseek_infra.infra.rag.files as files_module
import deepseek_infra.infra.agent_runtime.agent_runs as agent_runs_module
import deepseek_infra.infra.data.memory as memory_module
import deepseek_infra.infra.data.projects as projects_module
import deepseek_infra.web.server as server_module
from deepseek_infra.core.errors import ErrorCode


class ServerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.running_server, _ = server_module.create_server(0, host="127.0.0.1")
        self.thread = threading.Thread(target=self.running_server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.running_server.shutdown()
        self.running_server.server_close()
        self.thread.join(timeout=5)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, http.client.HTTPResponse]:
        request_headers = {"Authorization": f"Bearer {server_module.settings.auth.token}"}
        if headers:
            request_headers.update(headers)
        raw_body = b""
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.running_server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=raw_body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, data, response
        finally:
            connection.close()

    def request_raw(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, http.client.HTTPResponse]:
        request_headers = {"Authorization": f"Bearer {server_module.settings.auth.token}"}
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.running_server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, data, response
        finally:
            connection.close()

    def request_json(self, method: str, path: str, *, body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
        status, data, _ = self.request(method, path, body=body)
        return status, json.loads(data.decode("utf-8") or "{}")

    def multipart_body(self, *, filename: str = "note.txt", content: bytes = b"hello", boundary: str = "----testboundary") -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
            "Content-Type: text/plain\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")

    def test_chat_returns_invalid_payload_when_messages_missing(self) -> None:
        status, payload = self.request_json("POST", "/api/chat", body={"apiKey": "fake"})

        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], ErrorCode.INVALID_PAYLOAD.value)

    def test_chat_returns_missing_key_when_no_api_key_available(self) -> None:
        status, payload = self.request_json("POST", "/api/chat", body={"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], ErrorCode.MISSING_API_KEY.value)

    def test_auth_logout_clears_auth_cookie(self) -> None:
        status, payload, response = self.request("POST", "/api/auth/logout", body={})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload.decode("utf-8")), {"ok": True})
        cookie = response.getheader("Set-Cookie") or ""
        self.assertIn("auth_token=", cookie)
        self.assertIn("Max-Age=0", cookie)

    def test_download_serves_generated_pptx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            deck = Path(tmp) / "deck.pptx"
            deck.write_bytes(b"pptx-bytes")
            with patch.object(server_module, "resolve_generated_file", return_value=deck):
                status, data, response = self.request("GET", "/api/download?id=" + "a" * 32)

        self.assertEqual(status, 200)
        self.assertEqual(data, b"pptx-bytes")
        self.assertEqual(response.getheader("Content-Type"), "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        self.assertIn("presentation.pptx", response.getheader("Content-Disposition") or "")

    def test_download_serves_generated_docx_with_correct_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "report.docx"
            doc.write_bytes(b"docx-bytes")
            with patch.object(server_module, "resolve_generated_file", return_value=doc):
                status, data, response = self.request("GET", "/api/download?id=" + "b" * 32)

        self.assertEqual(status, 200)
        self.assertEqual(data, b"docx-bytes")
        self.assertEqual(
            response.getheader("Content-Type"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertIn("document.docx", response.getheader("Content-Disposition") or "")

    def test_download_serves_generated_svg_mindmap_with_correct_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mindmap = Path(tmp) / "map.svg"
            mindmap.write_text("<svg></svg>", encoding="utf-8")
            with patch.object(server_module, "resolve_generated_file", return_value=mindmap):
                status, data, response = self.request("GET", "/api/download?id=" + "c" * 32)

        self.assertEqual(status, 200)
        self.assertEqual(data, b"<svg></svg>")
        self.assertEqual(response.getheader("Content-Type"), "image/svg+xml")
        self.assertIn("mindmap.svg", response.getheader("Content-Disposition") or "")

    def test_download_serves_generated_svg_mindmap_inline_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mindmap = Path(tmp) / "map.svg"
            mindmap.write_text("<svg></svg>", encoding="utf-8")
            with patch.object(server_module, "resolve_generated_file", return_value=mindmap):
                status, data, response = self.request("GET", "/api/download?id=" + "d" * 32 + "&inline=1")

        self.assertEqual(status, 200)
        self.assertEqual(data, b"<svg></svg>")
        self.assertEqual(response.getheader("Content-Type"), "image/svg+xml")
        self.assertTrue((response.getheader("Content-Disposition") or "").startswith("inline;"))

    def test_download_save_returns_local_path(self) -> None:
        expected = {"ok": True, "filename": "deck.pptx", "path": r"C:\Users\me\Downloads\deck.pptx"}
        with patch.object(server_module, "save_generated_file_to_downloads", return_value=expected) as save_file:
            status, payload = self.request_json(
                "POST",
                "/api/download-save",
                body={"id": "a" * 32, "filename": "deck.pptx"},
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["path"], expected["path"])
        save_file.assert_called_once_with("a" * 32, filename="deck.pptx")

    def test_chat_injects_local_base_url_for_download_links(self) -> None:
        # Non-stream chat dispatches through the cascade entry (which runs plain
        # call_deepseek unless cascade is requested).
        with patch("deepseek_infra.web.routes.chat.call_deepseek_cascade", return_value={"content": "ok"}) as mocked:
            status, payload = self.request_json("POST", "/api/chat", body={"apiKey": "k", "messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(status, 200)
        self.assertEqual(payload["content"], "ok")
        self.assertEqual(mocked.call_args.args[0]["localBaseUrl"], f"http://127.0.0.1:{self.running_server.server_address[1]}")

    def test_memory_add_reports_conflict_and_can_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_dir = Path(tmp) / ".memory"
            with patch.object(memory_module, "MEMORY_DIR", memory_dir), patch.object(memory_module, "MEMORY_FILE", memory_dir / "memories.json"):
                first_status, first_payload = self.request_json(
                    "POST",
                    "/api/memory",
                    body={"action": "add", "content": "我喜欢 Vue", "category": "preference", "scope": "project:web"},
                )
                conflict_status, conflict_payload = self.request_json(
                    "POST",
                    "/api/memory",
                    body={"action": "add", "content": "我换用 React 了", "category": "preference", "scope": "project:web"},
                )
                conflict_id = cast(list[dict[str, Any]], conflict_payload["conflicts"])[0]["id"]
                replace_status, replace_payload = self.request_json(
                    "POST",
                    "/api/memory",
                    body={
                        "action": "add",
                        "content": "我换用 React 了",
                        "category": "preference",
                        "scope": "project:web",
                        "replaceIds": [conflict_id],
                    },
                )

        self.assertEqual(first_status, 200)
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict_payload["code"], ErrorCode.MEMORY_CONFLICT.value)
        self.assertEqual(replace_status, 200)
        self.assertEqual(cast(dict[str, Any], replace_payload["memory"])["content"], "我换用 React 了")

    def test_conversation_search_returns_matching_message(self) -> None:
        status, payload = self.request_json(
            "POST",
            "/api/conversations/search",
            body={
                "query": "晨会",
                "conversations": [
                    {
                        "id": "c1",
                        "title": "工作",
                        "favorite": True,
                        "tags": ["会议"],
                        "messages": [{"id": "m1", "role": "user", "content": "准备晨会要点"}],
                    }
                ],
            },
        )

        self.assertEqual(status, 200)
        results = cast(list[dict[str, Any]], payload["results"])
        self.assertEqual(results[0]["id"], "c1")
        matches = cast(list[dict[str, Any]], results[0]["matches"])
        self.assertEqual(matches[0]["messageId"], "m1")

    def test_project_routes_and_project_file_chunk_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / ".projects"
            with patch.object(projects_module, "PROJECTS_DIR", project_dir), patch.object(files_module, "PROJECTS_DIR", project_dir):
                create_status, create_payload = self.request_json("POST", "/api/projects", body={"action": "create", "name": "Docs"})
                project = cast(dict[str, Any], create_payload["project"])
                added = projects_module.add_project_files(
                    str(project["id"]),
                    [{"filename": "guide.txt", "content_type": "text/plain", "data": b"alpha beta gamma"}],
                )
                list_status, list_payload = self.request_json("POST", "/api/projects", body={"action": "list"})
                chunk_status, chunk_payload = self.request_json(
                    "POST",
                    "/api/file-chunk",
                    body={"fileId": added[0]["fileId"], "projectId": project["id"], "chunkIndex": 1},
                )
                reader_status, reader_payload = self.request_json(
                    "POST",
                    "/api/file-reader",
                    body={"fileId": added[0]["fileId"], "projectId": project["id"], "chunkStart": 1, "chunkCount": 6},
                )
                page_text_status, page_text_payload = self.request_json(
                    "POST",
                    "/api/file-page-text",
                    body={"fileId": added[0]["fileId"], "projectId": project["id"], "page": 1},
                )
                source_status, source_data, source_response = self.request_raw(
                    "GET",
                    f"/api/file-source?fileId={added[0]['fileId']}&projectId={project['id']}",
                )
                bad_status, bad_payload = self.request_json(
                    "POST",
                    "/api/file-chunk",
                    body={"fileId": added[0]["fileId"], "projectId": project["id"], "chunkIndex": "bad"},
                )

        self.assertEqual(create_status, 200)
        self.assertEqual(list_status, 200)
        self.assertEqual(cast(list[dict[str, Any]], list_payload["projects"])[0]["name"], "Docs")
        self.assertEqual(chunk_status, 200)
        self.assertIn("alpha beta gamma", cast(dict[str, Any], chunk_payload["chunk"])["text"])
        self.assertEqual(reader_status, 200)
        self.assertEqual(cast(dict[str, Any], reader_payload["file"])["name"], "guide.txt")
        self.assertIn("alpha beta gamma", cast(list[dict[str, Any]], reader_payload["chunks"])[0]["text"])
        self.assertEqual(page_text_status, 200)
        self.assertIn("alpha beta gamma", cast(dict[str, Any], page_text_payload["page"])["text"])
        self.assertEqual(source_status, 200)
        self.assertEqual(source_data, b"alpha beta gamma")
        self.assertIn("text/plain", source_response.getheader("Content-Type") or "")
        self.assertIn("inline", source_response.getheader("Content-Disposition") or "")
        self.assertEqual(source_response.getheader("X-Frame-Options"), "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", source_response.getheader("Content-Security-Policy") or "")
        self.assertEqual(bad_status, 400)
        self.assertEqual(bad_payload["code"], ErrorCode.INVALID_PAYLOAD.value)

    def test_file_page_image_route_returns_rendered_png(self) -> None:
        png = b"\x89PNG\r\n\x1a\nrendered"
        with patch.object(server_module, "file_page_image", return_value=({"name": "scan.pdf"}, png, 2, 4)) as mocked:
            status, data, response = self.request_raw(
                "GET",
                "/api/file-page-image?fileId=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&page=2&scale=1.4",
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, png)
        self.assertEqual(response.getheader("Content-Type"), "image/png")
        self.assertEqual(response.getheader("X-File-Page"), "2")
        self.assertEqual(response.getheader("X-File-Page-Count"), "4")
        self.assertIn("inline", response.getheader("Content-Disposition") or "")
        mocked.assert_called_once_with("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", project_id=None, page="2", scale="1.4")

    def test_file_page_layout_route_returns_word_coordinates(self) -> None:
        layout = {
            "ok": True,
            "page": {
                "index": 2,
                "pageCount": 4,
                "width": 200,
                "height": 100,
                "text": "hello",
                "hasText": True,
                "words": [{"text": "hello", "left": 10, "top": 20, "width": 12, "height": 5}],
            },
        }
        with patch.object(server_module, "file_page_layout", return_value=layout) as mocked:
            status, payload = self.request_json(
                "GET",
                "/api/file-page-layout?fileId=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&projectId=proj_123&page=2",
            )

        self.assertEqual(status, 200)
        self.assertEqual(cast(dict[str, Any], payload["page"])["words"][0]["text"], "hello")
        mocked.assert_called_once_with("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", project_id="proj_123", page="2")

    def test_file_page_search_route_returns_matches(self) -> None:
        result = {
            "ok": True,
            "query": "beta",
            "pageCount": 2,
            "matches": [{"page": 2, "snippet": "second beta", "index": 0}],
            "truncated": False,
        }
        with patch.object(server_module, "file_page_search", return_value=result) as mocked:
            status, payload = self.request_json(
                "GET",
                "/api/file-page-search?fileId=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&query=beta",
            )

        self.assertEqual(status, 200)
        self.assertEqual(cast(list[dict[str, Any]], payload["matches"])[0]["page"], 2)
        mocked.assert_called_once_with("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", project_id=None, query="beta")

    def test_fetch_url_route_returns_extracted_page(self) -> None:
        page = {"url": "https://example.com/", "contentType": "text/html", "text": "Example page", "charCount": 12}
        with patch.object(server_module, "fetch_url", return_value=page) as mocked:
            status, payload = self.request_json("POST", "/api/fetch-url", body={"url": "https://example.com/"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["page"], page)
        mocked.assert_called_once_with("https://example.com/")

    def test_translate_multipart_error_maps_library_http_status(self) -> None:
        exc = RuntimeError("too large")
        setattr(exc, "http_status", 413)

        translated = server_module.translate_multipart_error(exc)

        self.assertIsNotNone(translated)
        assert translated is not None
        self.assertEqual(translated.status, 413)
        self.assertEqual(translated.code, ErrorCode.UPLOAD_TOO_LARGE)

    def test_stream_chat_validates_payload_before_sending_ndjson_headers(self) -> None:
        for body, expected_status, expected_code in [
            ({"stream": True, "messages": [{"role": "user", "content": "hi"}]}, 400, ErrorCode.MISSING_API_KEY.value),
            ({"apiKey": "fake", "stream": True, "messages": []}, 400, ErrorCode.INVALID_PAYLOAD.value),
            ({"apiKey": "fake", "stream": True, "messages": [{"role": "assistant", "content": "hi"}]}, 400, ErrorCode.INVALID_PAYLOAD.value),
            (
                {"apiKey": "fake", "stream": True, "messages": [{"role": "user", "content": f"msg {index}"} for index in range(41)]},
                409,
                ErrorCode.CONTEXT_COMPRESSION_REQUIRED.value,
            ),
        ]:
            with self.subTest(expected_code=expected_code):
                status, payload = self.request_json("POST", "/api/chat", body=body)

                self.assertEqual(status, expected_status)
                self.assertEqual(payload["code"], expected_code)

    def test_stream_chat_emits_done_event(self) -> None:
        chunk = {
            "id": "chatcmpl-test",
            "model": "deepseek-v4-pro",
            "choices": [{"delta": {"content": "hello world"}}],
            "usage": {"prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 1},
        }
        stream = FakeStream([f"data: {json.dumps(chunk)}\n".encode("utf-8"), b"data: [DONE]\n"])

        with patch("urllib.request.urlopen", return_value=stream):
            status, data, _ = self.request(
                "POST",
                "/api/chat",
                body={"apiKey": "fake", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )

        events = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
        self.assertEqual(status, 200)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["content"], "hello world")
        self.assertEqual(events[-1]["diagnostics"]["cacheHitRate"], 50.0)

    def test_stream_chat_emits_error_event_for_upstream_error(self) -> None:
        upstream_error = urllib.error.HTTPError(
            url="https://api.deepseek.com",
            code=500,
            msg="boom",
            hdrs=Message(),
            fp=io.BytesIO(b'{"error":{"message":"upstream failed"}}'),
        )

        with patch("urllib.request.urlopen", side_effect=upstream_error):
            status, data, _ = self.request(
                "POST",
                "/api/chat",
                body={"apiKey": "fake", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )

        events = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
        self.assertEqual(status, 200)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["code"], ErrorCode.UPSTREAM_FAILURE.value)
        self.assertIn("upstream failed", events[-1]["error"])

    def test_stream_chat_maps_upstream_sse_error_event(self) -> None:
        stream = FakeStream([b"event: error\n", b'data: {"error":{"message":"bad stream"}}\n'])

        with patch("urllib.request.urlopen", return_value=stream):
            status, data, _ = self.request(
                "POST",
                "/api/chat",
                body={"apiKey": "fake", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
            )

        events = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
        self.assertEqual(status, 200)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["error"], "bad stream")

    def test_agent_run_routes_persist_replay_and_stream_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / ".agent-runs"
            with (
                patch.object(agent_runs_module, "AGENT_RUNS_DIR", runs_dir),
                patch.object(server_module.agent_run_registry, "ensure_started", return_value=True),
            ):
                create_status, create_payload = self.request_json(
                    "POST",
                    "/api/agent-runs",
                    body={
                        "payload": {
                            "apiKey": "secret-key",
                            "stream": True,
                            "model": "deepseek-v4-pro",
                            "messages": [{"role": "user", "content": "hi"}],
                        },
                        "confirmPlan": True,
                        "agentPreset": "auto",
                    },
                )
                run_id = str(create_payload["runId"])
                agent_runs_module.append_event(run_id, {"type": "content", "text": "hello"})
                agent_runs_module.append_event(run_id, {"type": "done", "content": "hello", "diagnostics": {"ok": True}})

                detail_status, detail_payload = self.request_json("GET", f"/api/agent-runs/{run_id}")
                events_status, events_payload = self.request_json("GET", f"/api/agent-runs/{run_id}/events?after=0")
                stream_status, stream_data, _ = self.request("GET", f"/api/agent-runs/{run_id}/stream?after=-1")
                second_stream_status, second_stream_data, _ = self.request("GET", f"/api/agent-runs/{run_id}/stream?after=0")
                run_file_text = (runs_dir / f"{run_id}.json").read_text(encoding="utf-8")

        self.assertEqual(create_status, 201)
        self.assertEqual(detail_status, 200)
        self.assertEqual(events_status, 200)
        self.assertEqual(stream_status, 200)
        self.assertEqual(second_stream_status, 200)
        self.assertNotIn("requestPayload", cast(dict[str, Any], detail_payload["run"]))
        self.assertEqual(cast(dict[str, Any], detail_payload["run"])["finalAnswer"], "hello")
        self.assertNotIn("secret-key", run_file_text)
        self.assertEqual(cast(list[dict[str, Any]], events_payload["events"])[0]["index"], 1)
        stream_events = [json.loads(line) for line in stream_data.decode("utf-8").splitlines() if line]
        second_stream_events = [json.loads(line) for line in second_stream_data.decode("utf-8").splitlines() if line]
        self.assertEqual([event["type"] for event in stream_events], ["content", "done"])
        self.assertEqual([event["type"] for event in second_stream_events], ["done"])

    def test_static_directory_listing_is_disabled(self) -> None:
        status, _, response = self.request_raw("GET", "/icons/")

        self.assertEqual(status, 404)
        self.assertEqual(response.getheader("Cache-Control"), "no-cache")

    def test_config_returns_upload_limits(self) -> None:
        status, data, _ = self.request_raw("GET", "/api/config")

        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["uploadLimits"],
            {"fileMaxBytes": 200_000_000, "requestMaxBytes": 220_000_000, "maxFiles": server_module.MAX_MULTIPART_FILES},
        )
        self.assertEqual(payload["ocr"], {"enabled": server_module.settings.ocr.enabled, "mode": "balanced", "localOnly": False})
        self.assertIn("edgeInference", payload)
        self.assertIn("provider", payload["edgeInference"])
        self.assertIn("localRag", payload)
        self.assertIn("embeddingProvider", payload["localRag"])
        self.assertIn("tracing", payload)
        self.assertIn("traceCount", payload["tracing"])
        self.assertIn("semanticCache", payload)
        self.assertIn("similarityThreshold", payload["semanticCache"])
        self.assertIn("gateway", payload)
        self.assertIn("contextManager", payload["gateway"])
        self.assertIn("requestQueue", payload["gateway"])

    def test_local_rag_status_and_reindex_routes(self) -> None:
        status, payload = self.request_json("GET", "/api/rag/status")
        rebuild_status, rebuild_payload = self.request_json("POST", "/api/rag/reindex", body={"action": "reindex"})

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("localRag", payload)
        self.assertEqual(rebuild_status, 200)
        self.assertTrue(rebuild_payload["ok"])
        self.assertIn("localRag", rebuild_payload)

    def test_trace_semantic_cache_and_gateway_routes(self) -> None:
        trace_payload = {"traceId": "trace-1", "status": "completed", "spans": [], "summary": {"spanCount": 0}}
        export_payload = {**trace_payload, "_export": {"redaction": {"applied": True}}}
        gateway_payload = {"contextManager": {"enabled": True}, "requestQueue": {"enabled": True, "counts": {}}}
        with (
            patch.object(server_module, "list_traces", return_value=[trace_payload]),
            patch.object(server_module, "get_trace", return_value=trace_payload),
            patch.object(server_module, "export_trace", return_value=export_payload),
            patch.object(server_module, "semantic_cache_status", return_value={"enabled": True, "items": 0}),
            patch.object(server_module, "gateway_status", return_value=gateway_payload),
        ):
            traces_status, traces_payload = self.request_json("GET", "/api/traces")
            detail_status, detail_payload = self.request_json("GET", "/api/traces/trace-1")
            export_status, export_data, export_response = self.request("GET", "/api/traces/trace-1/export.json")
            viewer_status, viewer_data, viewer_response = self.request("GET", "/trace/trace-1")
            cache_status, cache_payload = self.request_json("GET", "/api/semantic-cache/status")
            gateway_status_code, gateway_response = self.request_json("GET", "/api/gateway/status")

        self.assertEqual(traces_status, 200)
        self.assertEqual(cast(list[dict[str, Any]], traces_payload["traces"])[0]["traceId"], "trace-1")
        self.assertEqual(detail_status, 200)
        self.assertEqual(cast(dict[str, Any], detail_payload["trace"])["traceId"], "trace-1")
        self.assertEqual(export_status, 200)
        self.assertEqual(json.loads(export_data.decode("utf-8"))["_export"]["redaction"]["applied"], True)
        self.assertIn('attachment; filename="trace-trace-1.json"', export_response.getheader("Content-Disposition") or "")
        self.assertEqual(viewer_status, 200)
        self.assertIn(b'<div id="root"></div>', viewer_data)
        self.assertIn("text/html", viewer_response.getheader("Content-Type") or "")
        self.assertEqual(cache_status, 200)
        self.assertEqual(cast(dict[str, Any], cache_payload["semanticCache"])["items"], 0)
        self.assertEqual(gateway_status_code, 200)
        self.assertEqual(gateway_response["gateway"], gateway_payload)

    def test_pwa_icon_static_assets_are_served_with_image_types(self) -> None:
        for path, expected_type in [
            ("/favicon.ico", "image/x-icon"),
            ("/icons/favicon.svg", "image/svg+xml"),
            ("/icons/favicon-32x32.png", "image/png"),
            ("/icons/pwa-192x192.png", "image/png"),
        ]:
            with self.subTest(path=path):
                status, data, response = self.request_raw("GET", path)

                self.assertEqual(status, 200)
                self.assertGreater(len(data), 0)
                self.assertEqual((response.getheader("Content-Type") or "").split(";", 1)[0], expected_type)

    def test_cache_headers_differ_for_api_and_static_routes(self) -> None:
        api_status, _, api_response = self.request_raw("GET", "/api/config")
        static_status, _, static_response = self.request_raw("GET", "/vendor/katex/katex.min.js")
        index_status, _, index_response = self.request_raw("GET", "/")
        pointer_status, pointer_data, pointer_response = self.request_raw("GET", "/ui/workspace-assets.json")
        pointer = json.loads(pointer_data.decode("utf-8"))
        build_id = str(pointer["buildId"])
        immutable_paths = [
            f"/ui/workspace-assets-{build_id}.json",
            f"/sw-{build_id}.js",
            str(cast(list[str], pointer["core"])[0]),
        ]

        self.assertEqual(api_status, 200)
        self.assertEqual(static_status, 200)
        self.assertEqual(index_status, 200)
        self.assertEqual(pointer_status, 200)
        self.assertEqual(api_response.getheader("Cache-Control"), "no-store")
        self.assertEqual(static_response.getheader("Cache-Control"), "no-cache")
        self.assertEqual(index_response.getheader("Cache-Control"), "no-store")
        self.assertEqual(pointer_response.getheader("Cache-Control"), "no-store")
        for path in immutable_paths:
            with self.subTest(path=path):
                immutable_status, _, immutable_response = self.request_raw("GET", path)
                self.assertEqual(immutable_status, 200)
                self.assertEqual(
                    immutable_response.getheader("Cache-Control"),
                    "public, max-age=31536000, immutable",
                )
        self.assertEqual(static_response.getheader("X-Frame-Options"), "DENY")
        csp = static_response.getheader("Content-Security-Policy") or ""
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("img-src 'self' data: http: https:", csp)

    def test_cors_preflight_only_allows_local_origins(self) -> None:
        port = self.running_server.server_address[1]
        allowed_origin = f"http://127.0.0.1:{port}"

        allowed_status, _, allowed_response = self.request_raw("OPTIONS", "/api/config", headers={"Origin": allowed_origin})
        blocked_status, _, blocked_response = self.request_raw("OPTIONS", "/api/config", headers={"Origin": "https://evil.example"})

        self.assertEqual(allowed_status, 204)
        self.assertEqual(allowed_response.getheader("Access-Control-Allow-Origin"), allowed_origin)
        self.assertEqual(blocked_status, 204)
        self.assertIsNone(blocked_response.getheader("Access-Control-Allow-Origin"))

    def test_cors_preflight_rejects_origin_with_path_query_or_fragment(self) -> None:
        port = self.running_server.server_address[1]

        for suffix in ["/spoofed", "?token=spoofed", "#spoofed"]:
            with self.subTest(suffix=suffix):
                status, _, response = self.request_raw(
                    "OPTIONS",
                    "/api/config",
                    headers={"Origin": f"http://127.0.0.1:{port}{suffix}"},
                )

                self.assertEqual(status, 204)
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    @unittest.skipIf(server_module.multipart_module is None, "multipart dependency is not installed")
    def test_multipart_upload_parses_ocr_field_after_file(self) -> None:
        boundary = "----testboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="scan.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
            "%PDF\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="ocrEnabled"\r\n\r\n'
            "1\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="apiKey"\r\n\r\n'
            "sk-upload\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        def fake_extract(
            filename: str,
            content_type: str,
            data: bytes,
            *,
            ocr_enabled: bool,
            ocr_api_key: str,
        ) -> dict[str, object]:
            self.assertEqual(filename, "scan.pdf")
            self.assertEqual(content_type, "application/pdf")
            self.assertEqual(data, b"%PDF")
            self.assertTrue(ocr_enabled)
            self.assertEqual(ocr_api_key, "sk-upload")
            return {"name": filename, "fileId": "a" * 32, "kind": "pdf", "charCount": 3, "chunkCount": 1}

        with patch.object(server_module, "extract_uploaded_file", side_effect=fake_extract):
            status, data, _ = self.request_raw(
                "POST",
                "/api/file-text",
                body=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )

        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["files"][0]["name"], "scan.pdf")

    @unittest.skipIf(server_module.multipart_module is None, "multipart dependency is not installed")
    def test_multipart_upload_rejects_oversized_request_body(self) -> None:
        boundary = "----smallrequest"
        body = self.multipart_body(content=b"hello world", boundary=boundary)

        with patch.object(server_module, "MAX_UPLOAD_BYTES", 20):
            status, data, _ = self.request_raw(
                "POST",
                "/api/file-text",
                body=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )

        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(status, 413)
        self.assertEqual(payload["code"], ErrorCode.UPLOAD_TOO_LARGE.value)

    @unittest.skipIf(server_module.multipart_module is None, "multipart dependency is not installed")
    def test_multipart_upload_rejects_single_file_above_limit_on_all_upload_routes(self) -> None:
        for path in ["/api/file-text", "/api/project-files?projectId=test-project", "/share-target"]:
            with self.subTest(path=path):
                boundary = "----smallfile"
                body = self.multipart_body(content=b"12345", boundary=boundary)

                with patch.object(server_module, "MAX_UPLOAD_FILE_BYTES", 4), patch.object(server_module, "MAX_UPLOAD_BYTES", 10_000):
                    status, data, _ = self.request_raw(
                        "POST",
                        path,
                        body=body,
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    )

                payload = json.loads(data.decode("utf-8"))
                self.assertEqual(status, 413)
                self.assertEqual(payload["code"], ErrorCode.UPLOAD_TOO_LARGE.value)

    @unittest.skipIf(server_module.multipart_module is None, "multipart dependency is not installed")
    def test_share_target_imports_prompt_and_files(self) -> None:
        boundary = "----sharetarget"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="title"\r\n\r\n'
            "Article title\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="url"\r\n\r\n'
            "https://example.com/read\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="note.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "shared note\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        def fake_extract(
            filename: str,
            content_type: str,
            data: bytes,
            *,
            ocr_enabled: bool,
            ocr_api_key: str,
        ) -> dict[str, object]:
            self.assertEqual(filename, "note.txt")
            self.assertEqual(content_type, "text/plain")
            self.assertEqual(data, b"shared note")
            self.assertFalse(ocr_enabled)
            self.assertEqual(ocr_api_key, "")
            return {"name": filename, "fileId": "b" * 32, "kind": "text", "charCount": 11, "chunkCount": 1}

        with patch.object(server_module, "extract_uploaded_file", side_effect=fake_extract):
            connection = http.client.HTTPConnection("127.0.0.1", self.running_server.server_address[1], timeout=5)
            try:
                connection.request(
                    "POST",
                    "/share-target",
                    body=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                )
                response = connection.getresponse()
                status = response.status
                response.read()
            finally:
                connection.close()

        self.assertEqual(status, 303)
        location = response.getheader("Location") or ""
        share_id = parse_qs(urlsplit(location).query).get("share", [""])[0]
        self.assertTrue(share_id)

        get_status, data, _ = self.request("GET", f"/api/share-target?id={share_id}")
        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(get_status, 200)
        self.assertIn("Article title", payload["share"]["prompt"])
        self.assertIn("https://example.com/read", payload["share"]["prompt"])
        self.assertEqual(payload["share"]["attachments"][0]["name"], "note.txt")

    def test_multipart_upload_reports_incompatible_namespace(self) -> None:
        boundary = "----testboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="scan.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
            "%PDF\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        class IncompatibleMultipart:
            MultipartParser = object

        with patch.object(server_module, "multipart_module", IncompatibleMultipart):
            status, data, _ = self.request_raw(
                "POST",
                "/api/file-text",
                body=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )

        payload = json.loads(data.decode("utf-8"))
        self.assertEqual(status, 500)
        self.assertEqual(payload["code"], ErrorCode.INTERNAL.value)
        self.assertIn("Multipart parser dependency", payload["error"])


class PythonwStartupTests(unittest.TestCase):
    """launch.bat starts the app under pythonw, where sys.stdout/stderr are None.

    uvicorn configures logging inside Config.__init__ and its default formatter
    calls sys.stdout.isatty() unless use_colors is set, which used to raise and
    surface as "Unable to configure formatter 'default'" so the window never opened.
    """

    def test_create_server_without_console_streams(self) -> None:
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            server, port = server_module.create_server(0, host="127.0.0.1")
        try:
            self.assertGreater(port, 0)
        finally:
            server.server_close()


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
