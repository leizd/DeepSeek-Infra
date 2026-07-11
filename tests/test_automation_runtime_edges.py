from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.automation import actions, policy, schema, triggers


def automation(action: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"automationId": "auto_edges", "name": "Edges", "action": action, **extra}


def test_run_action_rejects_unknown_and_dispatches_all_types(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="Unsupported automation action"):
        actions.run_action(automation({"type": "unknown"}), run_id="run", trigger={})

    targets = {
        "run_skill": "run_skill_action",
        "browser_snapshot": "browser_snapshot_action",
        "browser_check": "browser_check_action",
        "project_summary": "project_summary_action",
        "media_process": "media_process_action",
        "create_artifact": "create_artifact_action",
        "save_item": "save_item_action",
        "export_conversation": "export_conversation_action",
        "export_project": "export_project_action",
    }
    for action_type, function_name in targets.items():
        monkeypatch.setattr(actions, function_name, lambda *args, current=action_type, **kwargs: {"called": current})
        result = actions.run_action(automation({"type": action_type}), run_id="run", trigger={}, event={"mediaId": "media"})
        assert result == {"called": action_type}


def test_run_skill_missing_unknown_exception_and_result_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="requires skillId"):
        actions.run_skill_action(automation({"type": "run_skill"}), run_id="run")

    from deepseek_infra.infra.skills import runner as skill_runner

    monkeypatch.setattr(
        skill_runner,
        "run_skill",
        lambda *args, **kwargs: {
            "artifacts": [{"artifactId": "artifact-1"}, {"artifactId": "artifact-1"}, "bad"],
            "savedItems": [{"id": "saved-1"}],
            "skillRunId": "skill-run",
            "traceId": "trace",
            "status": "success",
        },
    )
    result = actions.run_skill_action(
        automation({"type": "run_skill", "skillId": "known", "input": "bad", "offline": "false", "securityApproved": "yes"}),
        run_id="run",
    )
    assert result["outputs"]["artifactIds"] == ["artifact-1"]
    assert result["outputs"]["savedItemIds"] == ["saved-1"]
    assert result["traceId"] == "trace"

    monkeypatch.setattr(skill_runner, "run_skill", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("executor failed")))
    with pytest.raises(RuntimeError, match="executor failed"):
        actions.run_skill_action(automation({"type": "run_skill", "skillId": "known"}), run_id="run")


def test_browser_snapshot_block_read_failure_and_close_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.browser import actions as browser_actions

    with pytest.raises(AppError, match="requires url"):
        actions.browser_snapshot_action(automation({"type": "browser_snapshot"}))

    monkeypatch.setattr(browser_actions, "execute_browser_action", lambda payload: {"ok": False, "error": "blocked"})
    with pytest.raises(AppError, match="blocked"):
        actions.browser_snapshot_action(automation({"type": "browser_snapshot", "url": "https://example.test"}))

    calls: list[str] = []

    def read_blocked(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload["action"])
        if payload["action"] == "open_url":
            return {"ok": True, "session": {"browserSessionId": "browser-1"}}
        if payload["action"] == "read_page":
            return {"ok": False, "error": "read blocked"}
        raise RuntimeError("close crashed")

    monkeypatch.setattr(browser_actions, "execute_browser_action", read_blocked)
    with pytest.raises(AppError, match="read blocked"):
        actions.browser_snapshot_action(automation({"type": "browser_snapshot", "url": "https://example.test"}))
    assert calls == ["open_url", "read_page", "close_session"]

    def close_rejected(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["action"] == "open_url":
            return {"ok": True, "session": {"browserSessionId": "browser-1"}}
        if payload["action"] == "read_page":
            return {"ok": True, "result": {"text": "body", "media": {"mediaId": "media-1"}}}
        return {"ok": False, "error": "close rejected"}

    monkeypatch.setattr(browser_actions, "execute_browser_action", close_rejected)
    result = actions.browser_snapshot_action(automation({"type": "browser_snapshot", "url": "https://example.test"}))
    assert result["outputs"]["mediaIds"] == ["media-1"]
    assert "browserSessionCloseFailed:close rejected" in result["logs"]


def test_browser_check_inline_fixture_snapshot_and_state_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert actions.browser_check_text(automation({"type": "browser_check", "text": "  hello  "})) == "hello"
    fixture = tmp_path / "page.html"
    fixture.write_text("<p> fixture </p>", encoding="utf-8")
    monkeypatch.setattr(actions, "_resolve_fixture_path", lambda value: fixture)
    assert "fixture" in actions.browser_check_text(automation({"type": "browser_check", "fixturePath": "page.html"}))
    monkeypatch.setattr(actions, "browser_snapshot_action", lambda value: {"raw": {"text": "snapshot text"}})
    assert actions.browser_check_text(automation({"type": "browser_check"})) == "snapshot text"
    monkeypatch.setattr(actions, "browser_snapshot_action", lambda value: {"raw": "bad"})
    assert actions.browser_check_text(automation({"type": "browser_check"})) == "{}"

    monkeypatch.setattr(actions, "_load_state", lambda automation_id: {})
    monkeypatch.setattr(actions, "_write_state", lambda automation_id, state: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        actions.browser_check_action(automation({"type": "browser_check", "text": "changed"}), run_id="run")


def test_media_process_event_merge_defaults_and_missing_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="requires mediaIds"):
        actions.media_process_action(automation({"type": "media_process"}), run_id="run")

    captured: dict[str, Any] = {}

    def capture(payload: dict[str, Any], *, run_id: str) -> dict[str, Any]:
        captured.update(payload)
        return {"ok": True}

    monkeypatch.setattr(actions, "run_skill_action", capture)
    result = actions.media_process_action(
        automation({"type": "media_process", "input": {"mediaIds": ["media-1", ""], "task": ""}}),
        run_id="run",
        event={"mediaId": "media-2"},
    )
    assert result == {"ok": True}
    assert captured["action"]["input"]["mediaIds"] == ["media-1", "media-2"]
    assert captured["action"]["input"]["task"] == "Write a cited media digest."


def test_artifact_save_and_export_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="requires content"):
        actions.create_artifact_action(automation({"type": "create_artifact"}), run_id="run")
    with pytest.raises(AppError, match="requires projectId"):
        actions.save_item_action(automation({"type": "save_item"}), run_id="run")
    with pytest.raises(AppError, match="requires projectId"):
        actions.export_project_action(automation({"type": "export_project"}))

    monkeypatch.setattr(actions, "_persist_content_outputs", lambda *args, **kwargs: {"persisted": kwargs["content"]})
    assert actions.create_artifact_action(automation({"type": "create_artifact", "text": "content"}), run_id="run") == {"persisted": "content"}

    monkeypatch.setattr(actions.workspace_exports, "export_conversation", lambda *args, **kwargs: {"export": "bad"})
    conversation = actions.export_conversation_action(automation({"type": "export_conversation", "conversation": "bad"}))
    assert conversation["outputs"]["exportIds"] == [""]
    monkeypatch.setattr(actions.workspace_exports, "export_project", lambda *args, **kwargs: {"export": {"exportId": "export-1"}})
    assert actions.export_project_action(automation({"type": "export_project"}, projectId="project-1"))["outputs"]["exportIds"] == ["export-1"]


def test_output_id_helpers_and_boolean_edges() -> None:
    assert actions._artifact_ids("bad") == []
    assert actions._saved_item_ids("bad") == []
    assert actions._saved_item_ids([{"savedId": "one"}, {"id": "two"}, {}, "bad"]) == ["one", "two"]
    assert actions._media_ids_from_browser_result({"result": "bad"}) == []
    assert actions._media_ids_from_browser_result({"result": {"snapshot": {"mediaId": "one"}, "screenshot": {"mediaId": "one"}}}) == ["one", "one"]
    assert actions._unique(["one", "", "one", None, "two"]) == ["one", "two"]
    assert actions._bool(None, default=True) is True
    assert actions._bool(False, default=True) is False
    assert actions._bool("ON") is True
    assert actions._bool("no") is False


def test_fixture_path_missing_and_traversal(tmp_settings: Path) -> None:
    with pytest.raises(AppError, match="outside allowed"):
        actions._resolve_fixture_path(str(Path.home() / "outside.txt"))
    with pytest.raises(AppError, match="not found"):
        actions._resolve_fixture_path("missing.html")


def test_trigger_interval_schedule_event_and_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 7, 11, 9, 30, tzinfo=timezone.utc)
    base = {"automationId": "auto_edges", "trigger": {"type": "manual"}}
    assert triggers.trigger_matches(base, now=now) == (True, "")
    assert triggers.trigger_matches(base, trigger={"type": "event", "event": "media.ready"}, event={"type": "other"}, now=now) == (
        False,
        "event_not_matched",
    )
    assert triggers.trigger_matches(base, trigger={"type": "event", "event": "media.ready"}, event={"event": "media.ready"}, now=now) == (
        True,
        "",
    )

    monkeypatch.setattr(triggers.history, "latest_run", lambda *args, **kwargs: None)
    assert triggers.trigger_matches(base, trigger={"type": "interval", "intervalSeconds": "bad"}, now=now) == (True, "")
    recent = int(now.timestamp() * 1000)
    monkeypatch.setattr(triggers.history, "latest_run", lambda *args, **kwargs: {"startedAtMs": recent})
    assert triggers.trigger_matches(base, trigger={"type": "interval", "intervalSeconds": 60}, now=now) == (False, "interval_not_due")
    monkeypatch.setattr(triggers.history, "latest_run", lambda *args, **kwargs: {"startedAtMs": 0})
    assert triggers.trigger_matches(base, trigger={"type": "interval", "intervalSeconds": 60}, now=now) == (True, "")

    assert triggers.trigger_matches(base, trigger={"type": "schedule", "cron": "0 0 * * *"}, now=now) == (False, "schedule_not_due")
    monkeypatch.setattr(triggers.history, "latest_run", lambda *args, **kwargs: None)
    assert triggers.trigger_matches(base, trigger={"type": "schedule", "cron": "30 9 * * *"}, now=now) == (True, "")
    monkeypatch.setattr(triggers.history, "latest_run", lambda *args, **kwargs: {"startedAtMs": recent})
    assert triggers.trigger_matches(base, trigger={"type": "schedule", "cron": "30 9 * * *"}, now=now) == (False, "schedule_already_ran")
    assert triggers.trigger_matches(base, trigger={"type": "unknown"}, now=now) == (False, "unsupported_trigger")


def test_conditions_project_change_and_event_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    base = {"automationId": "auto_edges", "projectId": ""}
    assert triggers.condition_matches({**base, "condition": {"type": "always"}}) == (True, "")
    assert triggers.condition_matches({**base, "condition": {"type": "url_changed"}}) == (True, "")
    assert triggers.condition_matches({**base, "condition": {"type": "media_ready"}}, event={"event": "other"}) == (False, "media_not_ready")
    assert triggers.condition_matches({**base, "condition": {"newSavedItems": True}}, event={"type": "saved_item.created"}) == (True, "")
    assert triggers.condition_matches({**base, "condition": {"artifactCreated": True}}, event={"event": "artifact.created"}) == (True, "")
    assert triggers.condition_matches({**base, "condition": {"type": "bad"}}) == (False, "condition_not_met")
    assert triggers.project_changed(base) == (True, "")

    from deepseek_infra.infra.workspace import projects

    monkeypatch.setattr(projects, "get_project", lambda project_id: (_ for _ in ()).throw(AppError("missing")))
    bound = {**base, "projectId": "project-1"}
    assert triggers.project_changed(bound) == (False, "project_not_found")
    monkeypatch.setattr(projects, "get_project", lambda project_id: {"updatedAtMs": 20})
    monkeypatch.setattr(triggers.history, "latest_run", lambda *args, **kwargs: None)
    assert triggers.project_changed(bound) == (True, "")
    monkeypatch.setattr(triggers.history, "latest_run", lambda *args, **kwargs: {"finishedAtMs": 30})
    assert triggers.project_changed(bound) == (False, "project_unchanged")
    monkeypatch.setattr(triggers.history, "latest_run", lambda *args, **kwargs: {"startedAtMs": 10})
    assert triggers.project_changed(bound) == (True, "")


@pytest.mark.parametrize("part", ["", "*/bad", "*/0", "5-2", "a-b", "x", "99"])
def test_invalid_cron_parts_do_not_match(part: str) -> None:
    assert triggers._cron_part_matches(part, 5, 0, 59) is False


def test_cron_naive_time_lists_ranges_and_safe_int() -> None:
    naive = datetime(2026, 7, 5, 9, 0)
    assert triggers.cron_matches("0 9 5 7 0,7", naive) is True
    assert triggers.cron_matches("bad", naive) is False
    assert triggers._cron_field_matches("1,5,9", 5, 0, 10) is True
    assert triggers._cron_field_matches("1,5,9", 6, 0, 10) is False
    assert triggers._safe_int(None, default=7) == 7
    assert triggers._utc(naive).tzinfo == timezone.utc


def test_policy_disable_confirmation_limits_browser_and_scheduled_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy.history, "runs_today", lambda *args, **kwargs: 0)
    monkeypatch.setattr(policy.config, "AUTOMATION_ENABLED", False)
    assert policy.evaluate(automation({"type": "save_item"})).reasons == ("automation_disabled",)
    monkeypatch.setattr(policy.config, "AUTOMATION_ENABLED", True)
    assert policy.evaluate(automation({"type": "save_item"}, policy={"requiresConfirmation": True})).needs_confirmation is True

    monkeypatch.setattr(policy.history, "runs_today", lambda *args, **kwargs: 3)
    assert policy.evaluate(automation({"type": "save_item"}, policy={"maxRunsPerDay": 2})).reasons == ("max_runs_per_day_exceeded",)
    monkeypatch.setattr(policy.history, "runs_today", lambda *args, **kwargs: 0)
    assert policy.evaluate(automation({"type": "browser_snapshot"}, policy={})).reasons == ("browser_not_allowed",)
    assert policy.evaluate(automation({"type": "browser_snapshot"}, policy={"allowBrowser": True, "browserMode": "disabled"})).reasons == (
        "browser_mode_disabled",
    )
    assert policy.evaluate(
        automation({"type": "browser_snapshot", "url": "https://example.test"}, policy={"allowBrowser": True, "allowNetwork": False})
    ).reasons == ("network_not_allowed",)
    assert policy.evaluate(
        automation({"type": "browser_snapshot", "url": "http://127.0.0.1"}, policy={"allowBrowser": True, "allowNetwork": True})
    ).risk == "critical"
    assert policy.evaluate(automation({"type": "unknown"})).reasons == ("unsupported_action",)

    monkeypatch.setattr(policy.config, "AUTOMATION_REQUIRE_CONFIRM_FOR_BROWSER_WRITE", True)
    write = policy.evaluate(automation({"type": "save_item", "browserAction": "type_text"}))
    assert write.needs_confirmation is True
    scheduled = policy.evaluate(
        automation({"type": "run_skill", "allowNetwork": True}, trigger={"type": "schedule"}),
    )
    assert scheduled.needs_confirmation is True
    assert policy.evaluate(automation({"type": "save_item"})).allowed is True
    assert policy._network_url("http://[") is False


def test_schema_rejects_invalid_trigger_condition_action_and_normalizes_edges() -> None:
    with pytest.raises(AppError, match="payload must be an object"):
        schema.normalize_automation("bad")  # type: ignore[arg-type]
    with pytest.raises(AppError, match="trigger type"):
        schema.normalize_trigger({"type": "bad"})
    with pytest.raises(AppError, match="event trigger"):
        schema.normalize_trigger({"type": "event", "event": "bad"})
    assert schema.normalize_trigger({"type": "interval", "intervalSeconds": 30})["intervalSeconds"] == 30
    with pytest.raises(AppError, match="condition type"):
        schema.normalize_condition({"type": "bad"})
    with pytest.raises(AppError, match="action type"):
        schema.normalize_action({"type": "bad"})
    with pytest.raises(AppError, match="input must be an object"):
        schema.normalize_action({"type": "run_skill", "input": []})
    assert schema.normalize_output({"artifactType": ".md", "saveToProject": "false"}) == {
        "saveToProject": False,
        "createArtifact": True,
        "artifactType": "markdown",
    }
    normalized = schema.normalize_policy({"browserMode": "bad", "retry": {"maxAttempts": 99, "backoffSeconds": 99999}})
    assert normalized["browserMode"] == "read_only"
    assert normalized["retry"] == {"maxAttempts": 5, "backoffSeconds": 3600}
    assert schema.public_run_outputs({"artifactIds": ["one", "one", "", "x" * 200]})["artifactIds"] == ["one", "x" * 120]
    assert schema._bool(None, default=True) is True
