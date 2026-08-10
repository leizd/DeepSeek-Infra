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
from deepseek_infra.web.routes import a2a, backup_governance, edge, memory, rag, skills


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
