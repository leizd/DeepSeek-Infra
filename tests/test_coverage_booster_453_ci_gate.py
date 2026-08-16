"""Extra branch coverage booster for replication and governance CI gate."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
from typing import Any
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_policies,
    backup_recovery_class,
    backup_replication,
    backup_targets,
)
from deepseek_infra.web.routes import backup_governance
from deepseek_infra.web.routes.edge import EdgeRouteDeps, create_edge_router
from deepseek_infra.web.routes.rag import RagRouteDeps, create_rag_router


def _make_test_app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        code_val = exc.code.value if hasattr(exc.code, "value") else str(exc.code)
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": code_val, "message": str(exc)}},
        )

    return app


def test_replication_job_listing_and_filtering(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep_dir = tmp_settings / ".replication"
    rep_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backup_replication, "REPLICATION_DIR", rep_dir)

    # Empty dir
    assert backup_replication.list_jobs() == []

    # Corrupted / invalid files
    (rep_dir / ".hidden.json").write_text("{}", encoding="utf-8")
    (rep_dir / "bad.json").write_text("not json", encoding="utf-8")
    (rep_dir / "list.json").write_text("[]", encoding="utf-8")

    job1 = {
        "jobId": "j1",
        "policyId": "pol1",
        "backupId": "b1",
        "mode": "required",
        "phase": "queued",
        "slotDigest": "slot1",
    }
    job2 = {
        "jobId": "j2",
        "policyId": "pol1",
        "backupId": "b2",
        "mode": "required",
        "phase": "retry-wait",
        "slotDigest": "slot2",
    }
    job3 = {
        "jobId": "j3",
        "policyId": "pol2",
        "backupId": "b3",
        "mode": "optional",
        "phase": "committed",
        "slotDigest": "slot3",
    }
    (rep_dir / "job1.json").write_text(json.dumps(job1), encoding="utf-8")
    (rep_dir / "job2.json").write_text(json.dumps(job2), encoding="utf-8")
    (rep_dir / "job3.json").write_text(json.dumps(job3), encoding="utf-8")

    assert len(backup_replication.list_jobs()) == 3
    assert len(backup_replication.list_jobs(policy_id="pol1")) == 2
    assert len(backup_replication.list_jobs(backup_id="b2")) == 1
    assert len(backup_replication.list_jobs(phase="queued")) == 1
    assert len(backup_replication.list_jobs(limit=1)) == 1

    assert backup_replication.has_open_required_jobs(policy_id="pol1") is True
    assert backup_replication.has_open_required_jobs(policy_id="pol1", slot_digest="slot1") is True
    assert backup_replication.has_open_required_jobs(policy_id="pol1", slot_digest="other-slot") is False
    assert backup_replication.has_open_required_jobs(policy_id="pol2") is False


def test_enqueue_replica_jobs_variations(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep_dir = tmp_settings / ".replication"
    rep_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backup_replication, "REPLICATION_DIR", rep_dir)

    # Disabled replication
    assert backup_replication.enqueue_replica_jobs(
        policy={"policyId": "p1", "replication": {"enabled": False}},
        primary_target_id="managed-local",
        backup_id="b1",
        package=None,
        run_id="r1",
        schedule_slot="2026-08-16T12:00:00Z",
        slot_digest="s1",
    ) == []

    # Empty targets
    assert backup_replication.enqueue_replica_jobs(
        policy={"policyId": "p1", "replication": {"enabled": True, "targets": []}},
        primary_target_id="managed-local",
        backup_id="b1",
        package=None,
        run_id="r1",
        schedule_slot="2026-08-16T12:00:00Z",
        slot_digest="s1",
    ) == []

    # Valid enqueue with primary receipt
    policy = {
        "policyId": "p1",
        "replication": {
            "enabled": True,
            "targets": [
                {"targetId": "replica-1", "mode": "required", "maxAttempts": 3},
                "not-a-dict",
            ],
        },
    }
    jobs = backup_replication.enqueue_replica_jobs(
        policy=policy,
        primary_target_id="managed-local",
        backup_id="b1",
        package=None,
        run_id="r1",
        schedule_slot="2026-08-16T12:00:00Z",
        slot_digest="s1",
        primary_receipt={"objectSetDigest": "os1", "objectDigest": "ctrl1", "objects": []},
    )
    assert len(jobs) == 1
    assert jobs[0]["replicaTargetId"] == "replica-1"


def test_replication_compliance_and_lag_evaluations(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *_a, **_k: None)
    p_path = tmp_settings / "p_target"
    r_path = tmp_settings / "r_target"
    p_path.mkdir(parents=True, exist_ok=True)
    r_path.mkdir(parents=True, exist_ok=True)

    backup_targets.init_target(p_path, label="Primary")
    r_t = backup_targets.init_target(r_path, label="Replica")
    r_id = r_t["targetId"]

    # Policy disabled replication compliance
    res = backup_replication.replication_compliance(policy={"replication": {"enabled": False}}, backup_id="b1")
    assert res["enabled"] is False
    assert res["compliance"] == "healthy"

    # Policy with empty targets
    res2 = backup_replication.replication_compliance(policy={"replication": {"enabled": True, "targets": []}}, backup_id="b1")
    assert res2["compliance"] in {"healthy", "degraded"}

    # Calculate lag when policy missing
    lag_res = backup_replication.calculate_replica_lag("missing-pol", r_id)
    assert lag_res["status"] == "no-primary"


def test_source_holds_and_catalogs(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep_dir = tmp_settings / ".replication"
    monkeypatch.setattr(backup_replication, "REPLICATION_DIR", rep_dir)
    monkeypatch.setattr(backup_replication, "HOLDS_DIR", rep_dir / "holds")

    hold = backup_replication.acquire_source_hold("src-1", "pol-1", "b-1", "tester")
    assert hold.target_id == "src-1"
    assert backup_replication.is_source_held("src-1", "pol-1", "b-1") is True
    assert backup_replication.is_source_held("src-1", "pol-1", "b-2") is False

    hold.release()
    assert backup_replication.is_source_held("src-1", "pol-1", "b-1") is False


def test_policy_target_bindings_validation(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_targets, "_containment_violation", lambda *_a, **_k: None)
    p_path = tmp_settings / "p_target"
    r_path = tmp_settings / "r_target"
    p_path.mkdir(parents=True, exist_ok=True)
    r_path.mkdir(parents=True, exist_ok=True)

    p_t = backup_targets.init_target(p_path, label="Primary")
    r_t = backup_targets.init_target(r_path, label="Replica")
    p_id = p_t["targetId"]
    r_id = r_t["targetId"]

    # Valid policy
    valid_pol = {
        "policyId": "pol-ok",
        "name": "OK Policy",
        "targetId": p_id,
        "replication": {
            "enabled": True,
            "targets": [{"targetId": r_id, "mode": "required"}],
        },
    }
    backup_policies.validate_target_bindings(valid_pol)

    # Invalid primary
    invalid_p = dict(valid_pol, targetId="missing-p")
    with pytest.raises(AppError) as exc:
        backup_policies.validate_target_bindings(invalid_p)
    assert "Unregistered primary targetId" in str(exc.value)

    # Invalid replica
    invalid_r = dict(valid_pol, replication={"enabled": True, "targets": [{"targetId": "missing-r"}]})
    with pytest.raises(AppError) as exc2:
        backup_policies.validate_target_bindings(invalid_r)
    assert "Unregistered replica targetId" in str(exc2.value)


def test_recovery_class_calibration(tmp_settings: Path) -> None:
    # Empty samples calibration
    res = backup_recovery_class.calibrate_rto(target_id="target-1")
    assert "planningHeuristic" in res
    assert res["isSla"] is False


def test_governance_router_endpoints_comprehensive(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup_governance, "require_api_auth", lambda _req: None)
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_dr_audit.resume_audit", lambda _a: {"resumed": True})
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_recovery_planner.plan_recovery", lambda **_kw: {"plan": "ok"})
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_remote_restore.attempt_target_failover", lambda _r, failure_reason="": {"failover": True})
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_remote_restore.preflight_restore_session", lambda _r: {"preflight": "ok"})
    monkeypatch.setattr("deepseek_infra.infra.workspace.backup_remote_restore.fetch_restore_session", lambda _r, max_bytes=None: {"fetch": "ok"})

    app = _make_test_app()
    router = backup_governance.create_backup_governance_router()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    # Capabilities
    res = client.get("/api/workspace/backup-target-capabilities")
    assert res.status_code == 200

    # Policies list
    res_pol = client.get("/api/workspace/backup-policies")
    assert res_pol.status_code == 200

    # Resume audit
    res_aud = client.post("/api/workspace/disaster-recovery/audit/aud-1/resume")
    assert res_aud.status_code == 200

    # Plan valid
    res_plan = client.post("/api/workspace/disaster-recovery/plan", json={"policyId": "pol-1"})
    assert res_plan.status_code == 200

    # Plan invalid
    res_plan_inv = client.post("/api/workspace/disaster-recovery/plan", json={})
    assert res_plan_inv.status_code == 400

    # Replication jobs query
    res_rep = client.get("/api/workspace/disaster-recovery/replication?policyId=p1&backupId=b1")
    assert res_rep.status_code == 200

    # Failover
    res_fo = client.post("/api/workspace/disaster-recovery/failover/rest-1", json={"reason": "net"})
    assert res_fo.status_code == 200

    # Preflight
    res_pf = client.post("/api/workspace/restores/rest-1/preflight", json={})
    assert res_pf.status_code == 200

    # Preflight invalid
    res_pf_inv = client.post("/api/workspace/restores/rest-1/preflight", json={"bad": "param"})
    assert res_pf_inv.status_code == 400

    # Fetch
    res_fetch = client.post("/api/workspace/restores/rest-1/fetch", json={"maxBytes": 2048})
    assert res_fetch.status_code == 200


def test_rag_router_comprehensive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deepseek_infra.web.routes.rag.require_api_auth", lambda _req: None)
    deps = RagRouteDeps(
        rebuild_local_rag_index=lambda: {"reindexed": True},
        verify_local_rag_citation=lambda item_id, snippet: {"valid": True, "itemId": item_id},
        evaluate_local_rag_recall=lambda cases, k: {"score": 1.0, "k": k, "count": len(cases)},
    )
    app = _make_test_app()
    app.include_router(create_rag_router(deps))
    client = TestClient(app, raise_server_exceptions=False)

    # Reindex valid
    res1 = client.post("/api/rag/reindex", json={"action": "reindex"})
    assert res1.status_code == 200
    assert res1.json()["reindexed"] is True

    # Reindex invalid action
    res2 = client.post("/api/rag/reindex", json={"action": "invalid_action"})
    assert res2.status_code == 400

    # Verify citation valid
    res3 = client.post("/api/rag/verify-citation", json={"itemId": "item-1", "snippet": "abc"})
    assert res3.status_code == 200

    # Verify citation invalid
    res4 = client.post("/api/rag/verify-citation", json={"itemId": "", "snippet": "abc"})
    assert res4.status_code == 400

    # Eval valid
    res5 = client.post("/api/rag/eval", json={"cases": [{"q": "1"}], "k": 3})
    assert res5.status_code == 200

    # Eval invalid
    res6 = client.post("/api/rag/eval", json={"cases": "not-a-list"})
    assert res6.status_code == 400


def test_edge_router_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deepseek_infra.web.routes.edge.require_api_auth", lambda _req: None)
    app = _make_test_app()
    deps = EdgeRouteDeps(
        edge_unload=lambda: {"unloaded": True},
        edge_route_preview=lambda payload: {"preview": payload},
    )
    app.include_router(create_edge_router(deps))
    client = TestClient(app, raise_server_exceptions=False)

    res1 = client.post("/api/edge/reload", json={"action": "unload"})
    assert res1.status_code == 200

    res2 = client.post("/api/edge/reload", json={"action": "bad-action"})
    assert res2.status_code == 400

    res3 = client.post("/api/edge/route-preview", json={"prompt": "hello"})
    assert res3.status_code == 200


def test_replication_reconcile_and_repairs(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rep_dir = tmp_settings / ".replication"
    rep_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backup_replication, "REPLICATION_DIR", rep_dir)
    monkeypatch.setattr(backup_replication, "REPAIRS_DIR", rep_dir / "repairs")

    # Missing job execution
    with pytest.raises(AppError) as exc:
        backup_replication.execute_replication_job("missing-job-id")
    assert "not found" in str(exc.value).lower()

    # Process empty pending jobs
    stats = backup_replication.process_pending_jobs()
    assert stats == {"processed": 0, "committed": 0, "failed": 0}

    # Reconcile disabled policy
    monkeypatch.setattr(backup_policies, "get_policy", lambda pid: {"policyId": pid, "replication": {"enabled": False}})
    rec_disabled = backup_replication.reconcile_policy_replicas("p1")
    assert rec_disabled["status"] == "skipped"

    # Reconcile policy with empty targets
    monkeypatch.setattr(
        backup_policies,
        "get_policy",
        lambda pid: {"policyId": pid, "targetId": "p_target", "replication": {"enabled": True, "targets": []}},
    )
    rec_no_targets = backup_replication.reconcile_policy_replicas("p1")
    assert rec_no_targets["status"] == "noop"


def test_server_routes_and_auth_redirect(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.web.server import create_app, handle_auth_token_redirect

    # Auth redirect tests
    import dataclasses
    from deepseek_infra.core.config import settings
    new_auth = dataclasses.replace(settings.auth, enabled=True, token="secret-token")
    new_settings = dataclasses.replace(settings, auth=new_auth)
    monkeypatch.setattr("deepseek_infra.web.server.settings", new_settings)
    monkeypatch.setattr("deepseek_infra.web.http_utils.settings", new_settings)

    # Invalid token -> 401
    class DummyURL:
        path = "/"

    class DummyRequest:
        url = DummyURL()
        query_params = {"token": "wrong-token"}

    r_bad = handle_auth_token_redirect(DummyRequest())  # type: ignore[arg-type]
    assert r_bad is not None
    assert r_bad.status_code == 401

    # Valid token redirect -> 302
    class DummyValidRequest:
        url = DummyURL()
        query_params = {"token": "secret-token"}

    r_ok = handle_auth_token_redirect(DummyValidRequest())  # type: ignore[arg-type]
    assert r_ok is not None
    assert r_ok.status_code == 302

    # Server API routes
    monkeypatch.setattr("deepseek_infra.web.http_utils.require_api_auth", lambda _req: None)
    monkeypatch.setattr("deepseek_infra.web.server.require_api_auth", lambda _req: None)
    monkeypatch.setattr("deepseek_infra.web.server.save_generated_file_to_downloads", lambda _id, filename="": {"path": "/downloads/f.txt"})
    monkeypatch.setattr("deepseek_infra.web.server.fetch_url", lambda _u: {"title": "Page", "text": "Content"})
    monkeypatch.setattr("deepseek_infra.web.server.reminder_action", lambda _p: {"action": "ok"})
    monkeypatch.setattr("deepseek_infra.web.server.due_reminders", lambda: [{"reminderId": "r1"}])
    monkeypatch.setattr("deepseek_infra.web.server.file_reader_window", lambda _f, **_k: {"chunks": []})
    monkeypatch.setattr("deepseek_infra.web.server.file_page_text", lambda _f, **_k: {"text": "page1"})
    monkeypatch.setattr("deepseek_infra.web.server.compress_context_payload", lambda _p: {"compressed": True})
    monkeypatch.setattr("deepseek_infra.web.server.preflight_deepseek_payload", lambda _p: None)
    monkeypatch.setattr("deepseek_infra.web.server.create_agent_run", lambda _p, **_k: {"runId": "run-1", "phase": "running"})
    monkeypatch.setattr("deepseek_infra.web.server.agent_run_registry.ensure_started", lambda *_a, **_k: None)

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    # download-save
    assert client.post("/api/download-save", json={"id": "file-1", "filename": "file.txt"}).status_code == 200

    # fetch-url
    assert client.post("/api/fetch-url", json={"url": "http://example.com"}).status_code == 200

    # reminders
    assert client.post("/api/reminders", json={"action": "list"}).status_code == 200

    # reminders due
    assert client.post("/api/reminders/due", json={}).status_code == 200

    # file-reader
    assert client.post("/api/file-reader", json={"fileId": "fid"}).status_code == 200

    # file-page-text
    assert client.post("/api/file-page-text", json={"fileId": "fid", "page": 1}).status_code == 200

    # compress-context
    assert client.post("/api/compress-context", json={"messages": []}).status_code == 200

    # metrics
    assert client.get("/metrics").status_code == 200

    # agent-runs create invalid payload
    assert client.post("/api/agent-runs", json={"payload": "not-a-dict"}).status_code == 400

    # agent-runs create valid
    assert (
        client.post(
            "/api/agent-runs",
            json={"payload": {"model": "test"}, "confirmPlan": True, "agentPreset": "full"},
        ).status_code
        == 201
    )

    # share-target
    monkeypatch.setattr("deepseek_infra.web.server.require_allowed_host", lambda _req: None)
    monkeypatch.setattr("deepseek_infra.web.server.store_share_target_payload", lambda _p: "share-123")
    r_share = client.post(
        "/share-target",
        files={"title": (None, "Shared Title"), "text": (None, "Some text"), "url": (None, "http://example.com")},
        follow_redirects=False,
    )
    assert r_share.status_code == 303


def test_emit_cascade_as_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.web.server import emit_cascade_as_stream

    monkeypatch.setattr(
        "deepseek_infra.web.server.call_deepseek_cascade",
        lambda _p: {"id": "c1", "content": "hello", "reasoning": "thought", "usage": {"tokens": 10}},
    )
    events: list[dict[str, Any]] = []
    emit_cascade_as_stream({}, events.append)
    assert len(events) == 3
    assert events[0]["type"] == "reasoning"
    assert events[1]["type"] == "content"
    assert events[2]["type"] == "done"


def test_saved_items_edge_cases(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace import saved_items

    # missing item update
    with pytest.raises(AppError) as exc:
        saved_items.update_saved_item("proj-1", "missing-id", {"title": "new"})
    assert "not found" in str(exc.value).lower()

    # missing item require
    with pytest.raises(AppError) as exc2:
        saved_items.require_saved_item("proj-1", "missing-id")
    assert "not found" in str(exc2.value).lower()

    # corrupted store json
    p_path = tmp_settings / ".projects" / "proj-1"
    p_path.mkdir(parents=True, exist_ok=True)
    store = p_path / "saved-items.json"
    store.write_text(json.dumps({"items": "not-a-list"}), encoding="utf-8")
    assert saved_items._load_items("proj-1") == []

    # invalid elements in items
    store.write_text(
        json.dumps(
            {"items": ["not-a-dict", {"createdAtMs": "bad", "savedId": ""}, {"savedId": "s1", "title": "T", "type": "chat_snippet"}]}
        ),
        encoding="utf-8",
    )
    items = saved_items._load_items("proj-1")
    assert len(items) == 1
    assert items[0]["savedId"] == "s1"


def test_credentials_decryption_edge_cases() -> None:
    from deepseek_infra.launcher import credentials

    # Invalid envelope
    assert credentials._decrypt({}) is None
    assert credentials._decrypt({"nonce": "bad", "ciphertext": "bad", "mac": "bad"}) is None


def test_server_additional_edge_cases(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.web.server import create_app

    monkeypatch.setattr("deepseek_infra.web.http_utils.require_api_auth", lambda _req: None)
    monkeypatch.setattr("deepseek_infra.web.server.require_api_auth", lambda _req: None)
    monkeypatch.setattr("deepseek_infra.web.server.load_cached_file", lambda *_a, **_k: {"name": "f.txt", "chunks": [{"index": 0, "text": "chunk0"}]})
    monkeypatch.setattr("deepseek_infra.web.server.load_agent_run", lambda _r: {"status": "awaiting_plan", "requestPayload": {}})
    monkeypatch.setattr("deepseek_infra.web.server.public_agent_run", lambda r: r)
    monkeypatch.setattr("deepseek_infra.web.server.agent_run_registry.ensure_started", lambda *_a, **_k: True)
    monkeypatch.setattr("deepseek_infra.web.server.preflight_deepseek_payload", lambda _p: None)
    monkeypatch.setattr("deepseek_infra.web.server.agent_run_events_after", lambda _r, _a: [])
    monkeypatch.setattr("deepseek_infra.web.server.agent_run_event_stream", lambda _r, _a: iter([b"event: data\n\n"]))

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)

    # 404 handler for /api/unknown
    res_404 = client.get("/api/unknown-endpoint-xyz")
    assert res_404.status_code == 404

    # file-chunk invalid
    res_fc_bad = client.post("/api/file-chunk", json={"chunkIndex": "bad"})
    assert res_fc_bad.status_code == 400

    # file-chunk valid
    res_fc_ok = client.post("/api/file-chunk", json={"chunkIndex": 0, "fileId": "fid"})
    assert res_fc_ok.status_code == 200

    # file-chunk out of bounds index
    assert client.post("/api/file-chunk", json={"chunkIndex": 10, "fileId": "fid"}).status_code == 404

    # auth logout
    assert client.post("/api/auth/logout").status_code == 200

    # healthz & readyz
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

    # agent-runs action plan
    assert client.post("/api/agent-runs/run-1/plan", json={"plan": []}).status_code == 200

    # agent-runs detail, events, stream
    assert client.get("/api/agent-runs/run-1").status_code == 200
    assert client.get("/api/agent-runs/run-1/events").status_code == 200
    assert client.get("/api/agent-runs/run-1/stream").status_code == 200

    # agent-runs action rerun & resume
    monkeypatch.setattr("deepseek_infra.web.server.load_agent_run", lambda _r: {"status": "failed", "requestPayload": {}})
    assert client.post("/api/agent-runs/run-1/rerun", json={"agentId": "a1"}).status_code == 200
    assert client.post("/api/agent-runs/run-1/resume", json={}).status_code == 200

    # agent-runs invalid action
    assert client.post("/api/agent-runs/run-1/invalid-action", json={}).status_code == 404


def test_workspace_restores_and_governance_edge_cases(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.web.http_utils import json_response
    from deepseek_infra.web.routes.workspace import WorkspaceRouteDeps, create_workspace_router
    from deepseek_infra.infra.workspace import backups as workspace_backups

    monkeypatch.setattr("deepseek_infra.web.routes.workspace.require_api_auth", lambda _req: None)
    monkeypatch.setattr(workspace_backups, "put_session_secret", lambda _r, _b: {"ok": True})
    monkeypatch.setattr(workspace_backups, "unlock_restore", lambda _r: {"unlocked": True})
    monkeypatch.setattr(workspace_backups, "cleanup_restores", lambda: {"cleaned": 1})
    monkeypatch.setattr(workspace_backups, "list_restores", lambda: [])
    monkeypatch.setattr(workspace_backups, "get_restore", lambda _r: {"restoreId": _r})

    async def mock_read_multipart(_r: Any) -> tuple[list[dict[str, Any]], dict[str, list[str]], None]:
        return ([{"data": None, "filename": "b.zip"}], {}, None)

    deps = WorkspaceRouteDeps(
        read_multipart_files=mock_read_multipart,
    )
    router = create_workspace_router(deps)
    app = FastAPI()

    @app.exception_handler(AppError)
    async def _app_err(_: Request, exc: AppError) -> JSONResponse:
        return json_response(exc.to_response(), status=exc.status)

    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/api/workspace/restores").status_code == 200
    assert client.post("/api/workspace/restores/cleanup").status_code == 200
    assert client.get("/api/workspace/restores/r-1").status_code == 200
    assert client.put("/api/workspace/restores/r-1/secret", json={"secret": "abc"}).status_code == 200
    assert client.post("/api/workspace/restores/r-1/unlock").status_code == 200

    # finalize session
    monkeypatch.setattr(workspace_backups, "finalize_session", lambda _id, **_k: {"backupId": _id, "state": "committed"})
    assert client.post("/api/workspace/backups/b1/finalize").status_code == 200

    # inspect with multipart but invalid bytes data -> 400
    assert client.post("/api/workspace/restores/inspect", headers={"Content-Type": "multipart/form-data; boundary=xyz"}).status_code == 400


def test_governance_store_backed_branches(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.web.routes.backup_governance import BackupTargetSession, create_backup_governance_router
    from deepseek_infra.infra.workspace import backup_policies, backup_retention, backup_catalog

    class DummyStore:
        def read(self) -> bytes:
            return b""

        def write(self, _b: bytes) -> None:
            pass

    dummy_session = BackupTargetSession(
        target_id="target-remote",
        kind="s3",
        root=None,
        store=DummyStore(),
    )

    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.require_api_auth", lambda _req: None)
    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance.open_target_session", lambda _t, **_k: dummy_session)
    monkeypatch.setattr(backup_catalog, "catalog_state_store", lambda _s: {"b1": {"backupId": "b1", "policyId": "p1"}})
    monkeypatch.setattr(backup_policies, "get_policy", lambda _p: {"policyId": "p1", "retentionPolicyId": "ret1", "targetId": "target-remote"})
    monkeypatch.setattr(backup_retention, "get_retention_policy", lambda _r: {"keepLast": 5})
    monkeypatch.setattr(backup_retention, "apply_retention_store", lambda *_a, **_k: {"deleted": []})
    monkeypatch.setattr(backup_retention, "finalize_retention_store", lambda *_a, **_k: {"purged": 0})
    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance._target_writer", lambda *_a, **_k: nullcontext())

    app = FastAPI()
    app.include_router(create_backup_governance_router())
    client = TestClient(app, raise_server_exceptions=False)

    # catalog with store-backed session and policy_id filter
    res_cat = client.get("/api/workspace/backup-catalog?targetId=target-remote&policyId=p1")
    assert res_cat.status_code == 200

    # retention preview with store-backed session
    res_prev = client.post("/api/workspace/retention/preview", json={"policyId": "p1"})
    assert res_prev.status_code == 200

    # retention apply with store-backed session
    res_apply = client.post("/api/workspace/retention/apply", json={"policyId": "p1"})
    assert res_apply.status_code == 200

    # pin and unpin with store-backed session
    monkeypatch.setattr(backup_catalog, "_append_entry_store", lambda *_a, **_k: None)
    monkeypatch.setattr("deepseek_infra.web.routes.backup_governance._find_backup_session", lambda _b: (dummy_session, {"backupId": _b}))
    assert client.post("/api/workspace/backup-catalog/b1/pin").status_code == 200
    assert client.delete("/api/workspace/backup-catalog/b1/pin").status_code == 200


def test_server_multipart_edge_cases() -> None:
    from deepseek_infra.web.server import read_multipart_form

    class DummyReq:
        def __init__(self, headers: dict[str, str], body: bytes = b""):
            self.headers = headers
            self._body = body

        async def body(self) -> bytes:
            return self._body

    # missing multipart in content-type
    with pytest.raises(AppError) as exc1:
        asyncio.run(read_multipart_form(DummyReq({"Content-Type": "application/json"})))  # type: ignore[arg-type]
    assert "Expected multipart/form-data" in str(exc1.value)

    # empty content length
    with pytest.raises(AppError) as exc2:
        asyncio.run(read_multipart_form(DummyReq({"Content-Type": "multipart/form-data; boundary=xyz", "Content-Length": "0"})))  # type: ignore[arg-type]
    assert "Upload body is empty" in str(exc2.value)

    # boundary missing
    with pytest.raises(AppError) as exc3:
        asyncio.run(read_multipart_form(DummyReq({"Content-Type": "multipart/form-data", "Content-Length": "10"})))  # type: ignore[arg-type]
    assert "not multipart/form-data" in str(exc3.value)


def test_server_bind_socket_helpers() -> None:
    from deepseek_infra.web.server import open_bind_socket
    sock = open_bind_socket("127.0.0.1", 0)
    assert sock is not None
    port = sock.getsockname()[1]
    assert port > 0
    sock.close()








