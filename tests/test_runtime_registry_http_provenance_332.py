from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.responses import Response

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.automation import history, registry
from deepseek_infra.infra.workspace import provenance
from deepseek_infra.web import http_utils


def test_http_headers_json_body_and_port_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    api = Response()
    http_utils.apply_common_headers(api, "/api/file-source")
    assert api.headers["X-Frame-Options"] == "SAMEORIGIN" and api.headers["Cache-Control"] == "no-store"
    static = Response()
    http_utils.apply_common_headers(static, "/index.html")
    assert static.headers["X-Frame-Options"] == "DENY" and static.headers["Cache-Control"] == "no-cache"
    assert http_utils.json_response({"ok": True}, status=201).status_code == 201

    request = cast(Any, SimpleNamespace(headers={"Content-Length": "0"}, body=lambda: None))
    with pytest.raises(AppError):
        asyncio.run(http_utils.read_json_body(request))
    request = cast(Any, SimpleNamespace(headers={"Content-Length": "9"}, body=lambda: _async_value(b"not-json")))
    with pytest.raises(AppError, match="Invalid JSON"):
        asyncio.run(http_utils.read_json_body(request))
    request = cast(Any, SimpleNamespace(headers={"Content-Length": "2"}, body=lambda: _async_value(b"[]")))
    with pytest.raises(AppError, match="JSON object"):
        asyncio.run(http_utils.read_json_body(request))
    request = cast(Any, SimpleNamespace(headers={"Host": "example:bad"}, scope={"server": ("h", "bad")}))
    assert http_utils.request_port(request) == 0


async def _async_value(value: bytes) -> bytes:
    return value


def test_http_auth_host_origin_cookie_and_disposition_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    assert http_utils.parse_content_length("0") == 0
    for value in ("bad", "-1"):
        with pytest.raises(AppError):
            http_utils.parse_content_length(value)
    assert http_utils.host_without_port("[::1]:8000") == "::1"
    assert http_utils.host_without_port("LOCALHOST:8000, other") == "localhost"
    monkeypatch.setattr(http_utils, "local_ip", lambda: "192.168.1.2")
    assert "192.168.1.2" in http_utils.allowed_auth_hosts()
    assert http_utils.allowed_cors_origin("", 8000) == ""
    assert http_utils.allowed_cors_origin("http://localhost:bad", 8000) == ""
    assert http_utils.allowed_cors_origin("https://localhost:8000", 8000) == ""
    assert http_utils.allowed_cors_origin("http://localhost:9000", 8000) == ""
    assert http_utils.allowed_cors_origin("http://localhost:8000/path", 8000) == ""
    assert http_utils.allowed_cors_origin("http://localhost:8000", 8000) == "http://localhost:8000"
    disposition = http_utils.content_disposition_header("attachment", "报告.pdf")
    assert "filename*=UTF-8''" in disposition
    assert "auth_token=" in http_utils.auth_cookie_header("token")
    assert "Max-Age=0" in http_utils.expired_auth_cookie_header()
    assert http_utils.auth_token_from_headers("Bearer header", "auth_token=cookie") == "header"
    assert http_utils.auth_token_from_headers("", "auth_token=cookie") == "cookie"
    assert http_utils.auth_token_from_headers("", "bad-cookie") == ""
    assert http_utils.truthy("ON") is True


def test_http_request_base_url_and_auth_rejections(monkeypatch: pytest.MonkeyPatch) -> None:
    request = cast(Any, SimpleNamespace(headers={"Host": "localhost:8123"}, scope={"server": ("127.0.0.1", 8123)}))
    assert http_utils.request_base_url(request) == "http://localhost:8123"
    bad = cast(Any, SimpleNamespace(headers={"Host": "evil.test/path"}, scope={"server": ("127.0.0.1", 8123)}))
    assert http_utils.request_base_url(bad) == "http://127.0.0.1:8123"
    with pytest.raises(AppError) as caught:
        http_utils.require_allowed_host(cast(Any, SimpleNamespace(headers={"Host": "evil.test"})))
    assert caught.value.code == ErrorCode.FORBIDDEN


def test_automation_registry_corruption_limits_duplicates_and_touch(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry.store_path().parent.mkdir(parents=True)
    registry.store_path().write_text(json.dumps({"automations": "bad"}), encoding="utf-8")
    assert registry._load_automations() == []
    registry.store_path().write_text(json.dumps({"automations": [None, {"automationId": "bad"}]}), encoding="utf-8")
    assert registry._load_automations() == []
    with pytest.raises(AppError):
        registry.get_automation("auto_missing")
    with pytest.raises(AppError):
        registry.get_template("missing")

    payload = {"automationId": "auto_demo", "name": "Demo", "trigger": {"type": "manual"}, "condition": {"type": "always"}, "action": {"type": "save_item"}}
    created = registry.create_automation(payload)
    with pytest.raises(AppError, match="already exists"):
        registry.create_automation(payload)
    monkeypatch.setattr(registry, "MAX_AUTOMATIONS", 0)
    with pytest.raises(AppError, match="Too many"):
        registry.create_automation({**payload, "automationId": "auto_other"})
    monkeypatch.setattr(registry, "MAX_AUTOMATIONS", 500)
    assert registry.list_automations(include_disabled=False) == [created]
    assert registry.delete_automation("auto_missing") == 0
    assert registry.delete_automation("auto_demo") == 1
    monkeypatch.setattr(registry, "create_automation", lambda value: value)
    assert registry.create_from_template("daily_project_summary", project_id="proj_demo", overrides=cast(Any, "bad"))["projectId"] == "proj_demo"
    registry._touch_project("")


def test_automation_history_filters_invalid_rows_and_time_boundaries(tmp_settings: Path) -> None:
    history.store_path().parent.mkdir(parents=True)
    history.store_path().write_text(json.dumps({"runs": "bad"}), encoding="utf-8")
    assert history._load_runs() == []
    history.store_path().write_text(json.dumps({"runs": [None, {"runId": "bad"}]}), encoding="utf-8")
    assert history._load_runs() == []
    with pytest.raises(AppError):
        history.get_run("run_missing")
    record = history.record_run(
        {
            "runId": "run_demo",
            "automationId": "auto_demo",
            "status": "unknown",
            "startedAtMs": "bad",
            "finishedAtMs": "bad",
            "durationMs": -1,
            "trigger": "bad",
            "outputs": {},
            "logs": "bad",
            "evidence": "bad",
        }
    )
    assert record["status"] == "failed" and record["durationMs"] == 0 and record["trigger"] == {"type": "manual"}
    assert history.latest_run("auto_demo", statuses={"completed"}) is None
    assert history.latest_run("auto_demo", statuses={"failed"}) is not None
    assert history.list_runs(status="failed", limit=0)[0]["runId"] == "run_demo"
    assert history.runs_today("auto_demo", now=datetime.now(timezone.utc)) == 1
    assert history._string_list("bad", limit=1) == []
    assert history._safe_int("bad", default=3) == 3


def test_provenance_skips_partial_objects_and_deduplicates_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    project = {
        "name": "Project",
        "conversations": [None, {}, {"id": "c", "messages": [None, {}, {"id": "m", "role": "user", "sourceRef": {}}]}],
    }
    monkeypatch.setattr(provenance.project_store, "get_project", lambda _project_id: project)
    monkeypatch.setattr(provenance.memory_store, "list_memories", lambda **_kwargs: [{}, {"id": "mem", "source": {}}])
    monkeypatch.setattr(provenance.media_library, "list_media", lambda **_kwargs: [{}, {"mediaId": "media", "source": {}}])
    monkeypatch.setattr(provenance.saved_item_store, "list_saved_items", lambda _project_id: [{}, {"savedId": "saved", "sourceRef": {}}])
    monkeypatch.setattr(provenance.artifact_store, "list_artifacts", lambda _project_id: [{}, {"artifactId": "art", "source": {}}])
    monkeypatch.setattr(provenance.automation_registry, "list_automations", lambda **_kwargs: [{}, {"automationId": "auto"}])
    monkeypatch.setattr(
        provenance.automation_history,
        "list_runs",
        lambda **_kwargs: [{}, {"runId": "run", "automationId": "", "outputs": {"artifactIds": "bad", "savedItemIds": ["saved"]}}],
    )
    monkeypatch.setattr(provenance.export_store, "list_exports", lambda _project_id: [{}, {"exportId": "exp", "includes": {"mediaIds": "bad", "artifactIds": ["art"]}}])
    graph = provenance.project_provenance("proj_demo")
    assert graph["summary"]["types"]["project"] == 1
    assert any(edge["relation"] == "produced" for edge in graph["edges"])
    assert any(edge["relation"] == "includes" for edge in graph["edges"])

    raw = provenance._Graph()
    node = raw.add_node("x", "1", "X", {"status": "ok"})
    assert raw.add_node("x", "1", "X", {}) == node
    raw.add_edge(node, node, "self")
    raw.add_edge(node, "y:2", "link")
    raw.add_edge(node, "y:2", "link")
    raw.add_source_edge("bad", node, "source")
    raw.add_source_edge({"projectId": "p", "refId": "r", "kind": "chat"}, node, "source")
    assert len(raw.edges) == 3
    assert provenance._title({}, "Default") == "Default"
    assert provenance._compact_source({"ignored": 1}) == {}
