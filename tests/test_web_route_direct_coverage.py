"""Deterministic route coverage that does not depend on TestClient worker threads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter, Request
from fastapi.routing import APIRoute

from deepseek_infra.core.errors import AppError
from deepseek_infra.web.routes import a2a, backup_governance, chat, edge, mcp, memory, rag, skills


def _request(payload: dict[str, Any] | None = None, *, query: str = "") -> Request:
    body = json.dumps(payload or {}).encode("utf-8")
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": query.encode("ascii"),
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
    )


def _endpoint(router: APIRouter, path: str, method: str = "POST") -> Callable[..., Any]:
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path == path and method in (route.methods or set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _call(endpoint: Callable[..., Any], payload: dict[str, Any] | None = None, **path_params: str) -> Any:
    return asyncio.run(endpoint(_request(payload), **path_params))


def _json(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def test_memory_actions_execute_in_calling_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "require_api_auth", lambda _request: None)
    conflicts: list[dict[str, Any]] = []
    deps = memory.MemoryRouteDeps(
        load_memories=lambda: [{"id": "m1"}],
        clear_memories=lambda: 2,
        normalize_memory_category=lambda value, content: str(value or "general"),
        normalize_memory_scope=lambda value: str(value or "global"),
        detect_memory_conflicts=lambda content, category, scope: list(conflicts),
        upsert_memory=lambda *args, **kwargs: {"id": "created"},
        delete_memories_by_query=lambda query, scopes: len(scopes),
        delete_memory_by_id=lambda memory_id: 1,
    )
    endpoint = _endpoint(memory.create_memory_router(deps), "/api/memory")

    assert _json(_call(endpoint, {"action": "list"}))["memories"] == [{"id": "m1"}]
    assert _json(_call(endpoint, {"action": "clear"}))["deleted"] == 2
    assert _json(_call(endpoint, {"action": "add", "content": "remember", "replaceIds": ["old"]}))["memory"]["id"] == "created"
    conflicts.append({"id": "other"})
    assert _call(endpoint, {"action": "add", "content": "remember"}).status_code == 409
    assert _json(_call(endpoint, {"action": "delete", "query": "remember", "scope": "project:p1"}))["deleted"] == 2
    with pytest.raises(AppError, match="Unsupported memory action"):
        _call(endpoint, {"action": "unknown"})

    conflict_endpoint = _endpoint(memory.create_memory_router(deps), "/api/memory/conflicts")
    assert _json(_call(conflict_endpoint, {"content": "remember"}))["conflicts"] == [{"id": "other"}]


def test_rag_and_edge_routes_execute_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rag, "require_api_auth", lambda _request: None)
    rag_router = rag.create_rag_router(
        rag.RagRouteDeps(
            rebuild_local_rag_index=lambda: {"rebuilt": True},
            verify_local_rag_citation=lambda item_id, snippet: {"itemId": item_id, "snippet": snippet},
            evaluate_local_rag_recall=lambda cases, k: {"cases": len(cases), "k": k},
        )
    )
    assert _json(_call(_endpoint(rag_router, "/api/rag/reindex"), {"action": "rebuild"}))["rebuilt"] is True
    citation = _json(_call(_endpoint(rag_router, "/api/rag/verify-citation"), {"itemId": "i1", "snippet": "s"}))
    assert citation["citation"]["itemId"] == "i1"
    evaluated = _json(_call(_endpoint(rag_router, "/api/rag/eval"), {"cases": [{}], "k": 3}))
    assert evaluated["eval"] == {"cases": 1, "k": 3}

    monkeypatch.setattr(edge, "require_api_auth", lambda _request: None)
    edge_router = edge.create_edge_router(edge.EdgeRouteDeps(edge_unload=lambda: {"unloaded": True}, edge_route_preview=lambda payload: payload))
    assert _json(_call(_endpoint(edge_router, "/api/edge/reload"), {"action": "reload"}))["unloaded"] is True
    assert _json(_call(_endpoint(edge_router, "/api/edge/route-preview"), {"model": "local"}))["model"] == "local"


def test_a2a_rpc_directly_covers_stream_and_non_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a, "require_api_auth", lambda _request: None)
    stream = False
    deps = a2a.A2ARouteDeps(
        a2a_enabled=lambda: True,
        agent_card=lambda *args, **kwargs: {"name": "orchestrator"},
        agent_cards=lambda *args, **kwargs: [],
        handle_a2a_message=lambda body, **kwargs: {"echo": body},
        is_stream_request=lambda body: stream,
        stream_message_events=lambda *args, **kwargs: iter([b"data: ok\n\n"]),
    )
    endpoint = _endpoint(a2a.create_a2a_router(deps), "/a2a")
    assert _json(_call(endpoint, {"jsonrpc": "2.0"}))["echo"] == {"jsonrpc": "2.0"}
    stream = True
    assert _call(endpoint, {"stream": True}).media_type == "text/event-stream"


def test_governance_remote_and_target_routes_execute_directly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(backup_governance, "require_api_auth", lambda _request: None)
    monkeypatch.setattr(backup_governance.backup_targets, "init_s3_target", lambda **kwargs: {"kind": "s3", **kwargs})
    monkeypatch.setattr(backup_governance.backup_targets, "reinitialize_target", lambda path, label="": {"path": str(path), "label": label})
    monkeypatch.setattr(backup_governance.backup_remote_restore, "restore_from_target", lambda **kwargs: {"complete": True, **kwargs})
    monkeypatch.setattr(backup_governance.backup_remote_restore, "create_restore_from_target", lambda **kwargs: {"complete": False, **kwargs})
    monkeypatch.setattr(backup_governance.backup_remote_restore, "fetch_restore_session", lambda restore_id, **kwargs: {"restoreId": restore_id, **kwargs})
    monkeypatch.setattr(backup_governance.backup_remote_restore, "materialize_federated_restore", lambda restore_id, **kwargs: {"restoreId": restore_id, **kwargs})
    router = backup_governance.create_backup_governance_router()

    create_target = _endpoint(router, "/api/workspace/backup-targets")
    s3 = _json(_call(create_target, {"kind": "s3", "bucket": "bucket", "credentialProvider": {"kind": "env"}}))
    assert s3["kind"] == "s3" and s3["bucket"] == "bucket"
    with pytest.raises(AppError, match="WebDAV"):
        _call(create_target, {"kind": "webdav"})

    register = _endpoint(router, "/api/workspace/backup-targets/register-new")
    assert _json(_call(register, {"path": str(tmp_path), "label": "new"}))["label"] == "new"

    restore = _endpoint(router, "/api/workspace/restores/from-target")
    assert _json(_call(restore, {"targetId": "t", "backupId": "b", "complete": True}))["complete"] is True
    assert _json(_call(restore, {"targetId": "t", "backupId": "b"}))["complete"] is False
    fetch = _endpoint(router, "/api/workspace/restores/{restore_id}/fetch")
    assert _json(_call(fetch, {"maxBytes": 123}, restore_id="r"))["max_bytes"] == 123
    materialize = _endpoint(router, "/api/workspace/restores/{restore_id}/materialize")
    result = _json(_call(materialize, {"mode": "replace", "previousEpoch": "old", "targetEpoch": "new"}, restore_id="r"))
    assert result["target_epoch"] == "new"


def test_skills_thread_sensitive_actions_are_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skills, "require_api_auth", lambda _request: None)
    monkeypatch.setattr(skills, "_run_skill", lambda deps, payload, skill_id: {"ok": True, "skillId": skill_id})
    monkeypatch.setattr(skills.skill_eval, "build_skill_eval_report", lambda **kwargs: kwargs)
    monkeypatch.setattr(skills.skill_security, "review_skill", lambda skill_id: {"skillId": skill_id})
    monkeypatch.setattr(skills.skill_security, "review_pack", lambda pack_id: {"packId": pack_id})
    mock = MagicMock(return_value={})
    deps = skills.SkillsRouteDeps(
        list_skills=lambda **kwargs: [{"id": "s1"}],
        list_builtin_skills=mock,
        get_skill=mock,
        create_custom_skill=mock,
        update_skill=mock,
        set_skill_disabled=mock,
        delete_skill=mock,
        import_skill_config=mock,
        export_skill_config=mock,
        run_skill=mock,
        list_packs=mock,
        get_pack=mock,
        export_pack=mock,
        import_pack=mock,
        validate_pack=mock,
        delete_pack=mock,
    )
    endpoint = _endpoint(skills.create_skills_router(deps), "/api/skills")
    assert _json(_call(endpoint, {"action": "list"}))["skills"] == [{"id": "s1"}]
    assert _json(_call(endpoint, {"action": "run", "skillId": "s1"}))["skillId"] == "s1"
    assert _json(_call(endpoint, {"action": "eval_report", "version": skills.APP_VERSION}))["report"]["version"] == skills.APP_VERSION
    assert _json(_call(endpoint, {"action": "security_review", "skillId": "s1"}))["review"]["skillId"] == "s1"
    assert _json(_call(endpoint, {"action": "security_review_pack", "packId": "p1"}))["review"]["packId"] == "p1"


def test_backup_governance_continuity_and_promotion_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_write_continuity
    monkeypatch.setattr(backup_governance, "require_api_auth", lambda _request: None)
    monkeypatch.setattr(
        backup_write_continuity,
        "promote_primary_target",
        lambda policy_id, target_id, **kw: {"status": "promoted", "targetId": target_id, "policyId": policy_id},
    )
    monkeypatch.setattr(
        backup_write_continuity,
        "get_write_continuity_state",
        lambda policy_id: {"policyId": policy_id, "state": "healthy"},
    )
    router = backup_governance.create_backup_governance_router()

    # GET /api/workspace/backup-policies/{policy_id}/continuity
    continuity_ep = _endpoint(router, "/api/workspace/backup-policies/{policy_id}/continuity", method="GET")
    assert _json(_call(continuity_ep, policy_id="pol_1"))["policyId"] == "pol_1"

    # POST /api/workspace/backup-policies/{policy_id}/promote-primary
    promote_ep = _endpoint(router, "/api/workspace/backup-policies/{policy_id}/promote-primary", method="POST")
    with pytest.raises(AppError, match="targetId is required"):
        _call(promote_ep, {}, policy_id="pol_1")

    res = _json(_call(promote_ep, {"targetId": "target_01", "expectedPolicyRevision": 1, "expectedFailoverEpoch": 0}, policy_id="pol_1"))
    assert res["status"] == "promoted"


def test_chat_and_mcp_direct_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "require_api_auth", lambda _req: None)
    monkeypatch.setattr(chat, "call_deepseek_cascade", lambda p: {"role": "assistant", "content": "hello response"})
    chat_router = chat.create_chat_router(chat.ChatRouteDeps(
        chat_event_stream=lambda p: iter([b"data: ok\n\n"]),
        conversation_search=lambda p: {"results": []},
    ))
    chat_ep = _endpoint(chat_router, "/api/chat")
    res = _json(_call(chat_ep, {"messages": [{"role": "user", "content": "hi"}]}))
    assert res["content"] == "hello response"

    # MCP 202 response
    monkeypatch.setattr(mcp, "require_api_auth", lambda _req: None)
    mcp_router = mcp.create_mcp_router(mcp.McpRouteDeps(
        mcp_enabled=lambda: True,
        list_external_mcp_tools=lambda: {"tools": []},
        handle_mcp_message=lambda msg: None,
    ))
    mcp_ep = _endpoint(mcp_router, "/mcp")
    mcp_res = asyncio.run(mcp_ep(_request({"jsonrpc": "2.0", "method": "notifications/initialized"})))
    assert mcp_res.status_code == 202


def test_server_direct_route_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.web import server as server_module

    monkeypatch.setattr(server_module, "require_api_auth", lambda _req: None)
    monkeypatch.setattr(server_module, "preflight_deepseek_payload", lambda p: None)
    monkeypatch.setattr(server_module, "create_agent_run", lambda *a, **k: {"runId": "r123", "status": "running"})
    monkeypatch.setattr(server_module.agent_run_registry, "ensure_started", lambda *a, **k: None)
    monkeypatch.setattr(server_module, "load_agent_run", lambda run_id: {"runId": run_id, "requestPayload": {}})
    monkeypatch.setattr(server_module, "save_generated_file_to_downloads", lambda *a, **k: {"saved": True})
    monkeypatch.setattr(server_module, "load_cached_file", lambda *a, **k: {"name": "f.txt", "kind": "text", "chunks": [{"index": 0, "text": "t"}]})
    monkeypatch.setattr(server_module, "file_reader_window", lambda *a, **k: {"window": []})
    monkeypatch.setattr(server_module, "file_page_text", lambda *a, **k: {"text": "page"})
    monkeypatch.setattr(server_module, "fetch_url", lambda u: {"url": u, "content": "c"})
    monkeypatch.setattr(server_module, "compress_context_payload", lambda p: {"compressed": True})
    monkeypatch.setattr(server_module, "reminder_action", lambda p: {"ok": True})
    monkeypatch.setattr(server_module, "due_reminders", lambda: [])

    srv, _ = server_module.create_server(0, host="127.0.0.1")
    app_router = srv.app.router

    def _app_ep(path: str, method: str = "POST") -> Callable[..., Any]:
        for route in app_router.routes:
            if isinstance(route, APIRoute) and route.path == path and method in (route.methods or set()):
                return route.endpoint
        raise AssertionError(f"route not found: {method} {path}")

    # 1. /api/agent-runs
    agent_runs_ep = _app_ep("/api/agent-runs")
    with pytest.raises(AppError, match="payload must be an object"):
        _call(agent_runs_ep, {"payload": "invalid"})
    res = _json(_call(agent_runs_ep, {"payload": {"prompt": "hi"}}))
    assert res["runId"] == "r123"

    # 2. /api/download-save
    assert _json(_call(_app_ep("/api/download-save"), {"id": "g1", "filename": "o.txt"}))["saved"] is True

    # 3. /api/file-chunk, file-reader, file-page-text
    assert _json(_call(_app_ep("/api/file-chunk"), {"fileId": "f1", "chunkIndex": 1}))["file"]["kind"] == "text"
    assert _json(_call(_app_ep("/api/file-reader"), {"fileId": "f1"}))["window"] == []
    assert _json(_call(_app_ep("/api/file-page-text"), {"fileId": "f1", "page": 1}))["text"] == "page"

    # 4. /api/fetch-url
    assert _json(_call(_app_ep("/api/fetch-url"), {"url": "https://example.com"}))["ok"] is True

    # 5. /api/compress-context
    assert _json(_call(_app_ep("/api/compress-context"), {"context": "test"}))["compressed"] is True

    # 6. /api/reminders and /api/reminders/due
    assert _json(_call(_app_ep("/api/reminders"), {"action": "list"}))["ok"] is True
    assert _call(_app_ep("/api/reminders/due"), {}) is not None


