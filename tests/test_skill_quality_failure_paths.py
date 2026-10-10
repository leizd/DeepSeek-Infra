from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.skills import analytics, catalog
from deepseek_infra.infra.skills import eval as skill_eval
from deepseek_infra.infra.skills import versioning


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("required schema field missing", "schema_validation_failed"),
        ("tool policy denied", "tool_policy_denied"),
        ("artifact write failed", "artifact_policy_failed"),
        ("project binding missing", "project_binding_failed"),
        ("operation timed out", "timeout"),
        ("user cancelled", "user_cancelled"),
        ("DeepSeek upstream API unavailable", "llm_api_error"),
        ("unexpected executor crash", "unknown_error"),
    ],
)
def test_skill_failure_classifier_covers_actionable_categories(message: str, category: str) -> None:
    assert analytics.classify_failure(message) == category


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  repeated   text ", "repeated text"),
        ({}, "{}"),
        ({"path": "x" * 100, "count": 2, "nested": {"x": 1}}, "path=" + "x" * 80 + ", count=2, nested=1 fields"),
        ([1, 2, 3], "3 items"),
        (None, ""),
    ],
)
def test_payload_summary_is_bounded_and_type_aware(value: Any, expected: str) -> None:
    assert analytics.summarize_payload(value) == expected


def test_corrupt_run_history_is_skipped_and_io_errors_return_empty(tmp_settings: Path) -> None:
    path = analytics.runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\nnot-json\n{"missing":"id"}\n{"skillRunId":"ok","latencyMs":"bad"}\n{"skillRunId":"valid"}\n', encoding="utf-8")
    runs = analytics._read_runs()
    assert [run["skillRunId"] for run in runs] == ["ok", "valid"]
    assert runs[0]["latencyMs"] == 0

    with patch.object(Path, "read_text", side_effect=OSError("denied")):
        assert analytics._read_runs() == []


def test_run_edges_cover_missing_unknown_and_redacted_filters(tmp_settings: Path) -> None:
    with pytest.raises(AppError):
        analytics.get_run("unknown")
    with pytest.raises(AppError):
        analytics.redact_run("unknown")
    assert analytics.delete_run("unknown")["deleted"] == 0

    analytics.append_run({"skillRunId": "one", "skillId": "skill_a", "redacted": True})
    analytics.append_run({"skillRunId": "two", "skillId": "skill_b", "status": "failed"})
    assert [row["skillRunId"] for row in analytics.list_runs(include_redacted=False, limit=0)] == ["two"]
    assert analytics.cleanup_runs(status="failed", keep_recent=1)["deleted"] == 0


def test_analytics_helpers_handle_invalid_timestamps_and_values() -> None:
    assert analytics._latency_ms("bad", "also bad") == 0
    now = datetime.now(timezone.utc)
    assert analytics._latency_ms(now.isoformat(), (now - timedelta(seconds=1)).isoformat()) == 0
    assert analytics._safe_int(None, default=-2) == 0
    assert analytics._safe_int("bad", default=3) == 3
    assert analytics._safe_int(-4) == 0
    assert analytics._strings(["a", "", "a", None, "b"]) == ["a", "b"]
    assert analytics._percentile([], 90) == 0


def test_recent_trend_ignores_invalid_and_out_of_range_dates() -> None:
    rows = analytics._recent_trend(
        [
            {"completedAt": "bad", "status": "failed"},
            {"completedAt": "2000-01-01T00:00:00+00:00", "status": "failed"},
            {"completedAt": datetime.now(timezone.utc).isoformat(), "status": "failed"},
        ],
        days=2,
    )
    assert sum(row["runs"] for row in rows) == 1
    assert sum(row["failed"] for row in rows) == 1


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"kind": "pack"}, False),
        ({"category": "other"}, False),
        ({"trustLevel": "blocked"}, False),
        ({"trusted": True}, False),
        ({"offline": True}, False),
        ({"maxRiskScore": 5}, False),
        ({"minEvalScore": 99}, False),
        ({"tool": "shell"}, False),
        ({"kind": "skill", "maxRiskScore": "bad"}, True),
    ],
)
def test_catalog_filters_reject_each_mismatched_risk_dimension(filters: dict[str, Any], expected: bool) -> None:
    item = {
        "kind": "skill",
        "category": "Research",
        "trustLevel": "needs-review",
        "requiredTools": ["web_search"],
        "riskScore": 50,
        "evalScore": 80,
    }
    assert catalog._matches_filters(item, filters) is expected


def test_catalog_security_and_report_io_failure_paths(tmp_path: Path) -> None:
    with pytest.raises(AppError):
        catalog._enforce_install_security({"blocked": True}, approved=True)
    with pytest.raises(AppError):
        catalog._enforce_install_security({"requiresSecurityApproval": True}, approved=False)
    catalog._enforce_install_security({}, approved=False)
    assert catalog._filter_number(object()) is None
    assert catalog._string_list([" a ", "a", None, "b"]) == ["a", "b"]

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with patch.object(catalog, "REPO_ROOT", tmp_path):
        assert catalog._load_eval_report() == {}


def test_skill_eval_input_and_output_boundaries() -> None:
    assert skill_eval._sample_input(
        {
            "type": "object",
            "required": ["mode", "count", "ratio", "enabled", "name"],
            "properties": {
                "mode": {"enum": ["fast"]},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "enabled": {"type": "boolean"},
                "name": {"type": "string"},
            },
        }
    ) == {"mode": "fast", "count": 1, "ratio": 1, "enabled": True, "name": "sample name"}
    assert skill_eval._json_path({"rows": [{"name": "first"}]}, "$.rows.0.name") == "first"
    assert skill_eval._json_path({"rows": []}, "$.rows.2") is None
    assert skill_eval._json_path("text", "$.x") is None
    assert not skill_eval._content_pass({"content": "hello"}, {"expectedKeywords": ["missing"]})
    assert not skill_eval._content_pass({"content": "secret"}, {"forbidden": ["secret"]})
    assert not skill_eval._content_pass({"content": "ok"}, {"requiredOutputPaths": ["missing"]})


def test_skill_eval_artifact_and_comparison_failure_paths() -> None:
    skill = {"artifactPolicy": {"autoSave": True, "types": ["md"]}}
    assert skill_eval._artifact_pass(skill, {"expectedArtifactTypes": ["md"]}, [], [])
    assert not skill_eval._artifact_pass({}, {"expectedArtifactTypes": ["pdf"]}, [], [])
    assert not skill_eval._artifact_pass({"artifactPolicy": {"autoSave": True}}, {}, [], [])

    buckets: tuple[list[dict[str, Any]], ...] = ([], [], [], [], [])
    skill_eval._compare_item("new", "skill", {"status": "FAIL", "overallScore": 40}, None, *buckets)
    skill_eval._compare_item("fixed", "skill", {"status": "PASS", "overallScore": 100}, {"status": "FAIL", "overallScore": 50}, *buckets)
    skill_eval._compare_item("drop", "skill", {"status": "PASS", "overallScore": 80}, {"status": "PASS", "overallScore": 100}, *buckets)
    skill_eval._compare_item("better", "skill", {"status": "PASS", "overallScore": 90}, {"status": "PASS", "overallScore": 80}, *buckets)
    skill_eval._compare_item("same", "skill", {"status": "PASS", "overallScore": 90}, {"status": "PASS", "overallScore": 90}, *buckets)
    assert all(len(bucket) == 1 for bucket in buckets)


def test_versioning_helpers_handle_corrupt_snapshots_and_renames(tmp_settings: Path) -> None:
    directory = versioning.skill_history_dir("skill_demo")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bad.json").write_text("not json", encoding="utf-8")
    (directory / "wrong.json").write_text(json.dumps({"schemaVersion": "wrong"}), encoding="utf-8")
    assert versioning._load_skill_snapshots("skill_demo") == []
    assert versioning._safe_version("bad version !") == "bad_version_"
    assert versioning._rename_candidate("old", {"type": "string"}, ["used", "new"], {"used": {"type": "string"}, "new": {"type": "string"}}, {"used"}) == "new"
    assert versioning._rename_candidate("old", {"type": "string"}, ["count"], {"count": {"type": "integer"}}, set()) == ""
    assert versioning._pack_tool_ids({"skills": [None, {"allowedTools": ["b", "", "a", "b"]}]}) == ["a", "b"]
    with patch.object(versioning, "eval_aware_upgrade_gate", side_effect=RuntimeError("missing report")):
        assert versioning._score_diff("skill", "skill_demo")["status"] == "unavailable"


def test_catalog_crud_and_install_require_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError):
        catalog.catalog_get("")
    monkeypatch.setattr(catalog, "catalog_list", lambda: [])
    with pytest.raises(AppError):
        catalog.catalog_get("missing")
    monkeypatch.setattr(catalog, "catalog_get", lambda _item_id: {"kind": "skill", "skillId": "skill_x"})
    with pytest.raises(AppError):
        catalog.catalog_install("skill_x")
    with pytest.raises(AppError):
        catalog.catalog_uninstall("skill_x")


def test_catalog_install_and_uninstall_skill_binding_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    bindings = [
        {"enabledSkills": [], "defaultSkill": "", "enabledPacks": [], "enabledPackVersions": []},
        {"enabledSkills": ["skill_x", "skill_y"], "defaultSkill": "skill_x", "enabledPacks": [], "enabledPackVersions": []},
    ]
    monkeypatch.setattr(catalog.registry, "get_skill", lambda *_args, **_kwargs: {"skillId": "skill_x"})
    monkeypatch.setattr(catalog.projects, "project_skill_binding", lambda _project_id: bindings.pop(0))
    monkeypatch.setattr(catalog.projects, "set_project_skill_binding", lambda _project_id, enabled, **kwargs: {"enabled": enabled, **kwargs})
    installed = catalog._install_skill("proj_x", "skill_x")
    assert installed["enabled"] == ["skill_x"]
    assert installed["default_skill"] == "skill_x"
    uninstalled = catalog._uninstall_skill("proj_x", "skill_x")
    assert uninstalled["enabled"] == ["skill_y"]
    assert uninstalled["default_skill"] == "skill_y"


def test_catalog_uninstall_pack_preserves_shared_and_skips_corrupt_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    def export_pack(pack_id: str) -> dict[str, Any]:
        if pack_id == "pack_bad":
            raise AppError("corrupt")
        if pack_id == "pack_remove":
            return {"skills": [{"skillId": "shared"}, {"skillId": "remove"}, None]}
        return {"skills": [{"skillId": "shared"}]}

    monkeypatch.setattr(catalog.registry, "export_pack", export_pack)
    monkeypatch.setattr(
        catalog.projects,
        "project_skill_binding",
        lambda _project_id: {
            "enabledPacks": ["pack_remove", "pack_bad", "pack_keep"],
            "enabledPackVersions": [{"packId": "pack_remove"}, {"packId": "pack_keep"}, None],
            "enabledSkills": ["shared", "remove", "manual"],
            "defaultSkill": "remove",
        },
    )
    monkeypatch.setattr(catalog.projects, "set_project_skill_binding", lambda _project_id, enabled, **kwargs: {"enabled": enabled, **kwargs})
    result = catalog._uninstall_pack("proj_x", "pack_remove")
    assert result["enabled"] == ["shared", "manual"]
    assert result["default_skill"] == "shared"
    assert result["enabled_packs"] == ["pack_bad", "pack_keep"]


def test_catalog_helpers_cover_local_metadata_and_filter_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {"kind": "skill", "trustLevel": "trusted", "evalScore": 50, "requiredTools": [], "riskScore": 10}
    assert catalog._matches_filters(item, {"trusted": True, "offline": True, "maxRiskScore": 10, "minEvalScore": 50})
    assert catalog._filter_number(0) == 0.0
    assert catalog._category_for({"name": "unknown"}, []) == "General"
    assert catalog._tags_for("Office", ["fetch_url", "create_document", "search_files"], {"builtin": True}) == [
        "artifact",
        "builtin",
        "filesystem",
        "local",
        "network",
        "office",
    ]
    assert catalog._difficulty_for({"riskScore": 80}) == "advanced"
    assert catalog._difficulty_for({"riskScore": 30}) == "intermediate"
    assert catalog._difficulty_for({}) == "beginner"
    assert catalog._use_cases_for("Unknown", {"name": "Demo"}) == ["local workspace task", "Demo"]
    assert catalog._recommended_projects_for("Unknown") == ["workspace"]
    assert catalog._artifact_types({"artifactPolicy": "bad"}) == []
    assert catalog._dict_value([]) == {}
    assert catalog._string_list("bad") == []
    monkeypatch.setattr(catalog.projects, "list_projects", lambda: (_ for _ in ()).throw(AppError("unavailable")))
    assert catalog._install_counts() == {"skills": {}, "packs": {}}


def test_catalog_eval_and_install_counts_skip_partial_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        catalog,
        "_load_eval_report",
        lambda: {
            "skillResults": [None, {"skillId": "s", "overallScore": 99}],
            "packResults": [None, {"packId": "p", "overallScore": 88}],
        },
    )
    assert catalog._eval_scores() == {"skills": {"s": 99.0}, "packs": {"p": 88.0}}
    monkeypatch.setattr(
        catalog.projects,
        "list_projects",
        lambda: [None, {"skills": "bad"}, {"skills": {"enabledSkills": ["s", ""], "enabledPacks": ["p", ""]}}],
    )
    counts = catalog._install_counts()
    assert counts["skills"]["s"] == 1
    assert counts["packs"]["p"] == 1


def test_skill_eval_case_file_and_crud_validation_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing.jsonl"
    assert skill_eval.load_case_file(missing) == []
    path = tmp_path / "cases.jsonl"
    path.write_text('\nnot-json\n[]\n{"caseId":"missing-skill"}\n{"caseId":"ok","skillId":"skill_x"}\n', encoding="utf-8")
    assert [case["caseId"] for case in skill_eval.load_case_file(path)] == ["ok"]
    monkeypatch.setattr(skill_eval, "user_eval_cases_path", lambda: path)
    with pytest.raises(AppError):
        skill_eval.save_eval_case({"skillId": "skill_x"})
    with pytest.raises(AppError):
        skill_eval.save_eval_case({"caseId": "c"})
    with pytest.raises(AppError):
        skill_eval.delete_eval_case("")
    with pytest.raises(AppError):
        skill_eval.delete_eval_case("absent")


def test_skill_eval_markdown_policy_selection_and_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    markdown = skill_eval.markdown_summary(
        {
            "version": "3.3.2",
            "status": "FAIL",
            "summary": {"overallScore": 50, "passRate": 0.5, "caseCount": 2, "regressionCount": 1},
            "skillResults": [None, {"skillId": "s", "overallScore": 50, "passRate": 0.5, "caseCount": 2, "failedCases": ["c"]}],
            "packResults": [None, {"packId": "p", "overallScore": 50, "passRate": 0.5, "caseCount": 2, "failedCases": ["c"]}],
        }
    )
    assert "| s | 50 |" in markdown and "| p | 50 |" in markdown
    monkeypatch.setattr(skill_eval, "skill_allowed_tools", lambda _skill: ["allowed"])
    monkeypatch.setattr(skill_eval, "evaluate_skill_tool", lambda *_args: type("Decision", (), {"allowed": True})())
    assert not skill_eval._tool_policy_pass({}, {"requiredTools": ["missing"]})
    assert not skill_eval._tool_policy_pass({}, {"deniedTools": ["allowed"]})
    assert skill_eval._tool_policy_pass({}, {})
    assert skill_eval._dedupe_cases([{"caseId": "c", "x": 1}, {"caseId": "c", "x": 2}]) == [{"caseId": "c", "x": 2}]
    assert skill_eval._dedupe_case_results([{"caseId": "c"}, {"caseId": "d"}]) == [{"caseId": "c"}, {"caseId": "d"}]
    assert skill_eval._string_list("a, b;c\nd") == ["a", "b", "c", "d"]
    assert skill_eval._string_list(1) == []
    assert skill_eval._ratio(1, 0) == 0.0


def test_skill_eval_selection_pack_fallback_and_aggregation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_eval.registry, "get_skill", lambda *_args, **_kwargs: {"skillId": "skill_x", "name": "X"})
    monkeypatch.setattr(skill_eval.registry, "get_pack", lambda _pack_id: {"packId": "pack_x"})
    monkeypatch.setattr(skill_eval.registry, "export_pack", lambda _pack_id: {"skills": [{"skillId": "skill_x"}]})
    monkeypatch.setattr(skill_eval.registry, "list_skills", lambda **_kwargs: [{"skillId": "skill_x"}])
    assert skill_eval._selected_skill_ids(scope="skill", skill_id="skill_x", pack_id="") == ["skill_x"]
    assert skill_eval._selected_skill_ids(scope="pack", skill_id="", pack_id="pack_x") == ["skill_x"]
    assert skill_eval._selected_skill_ids(scope="all", skill_id="", pack_id="") == ["skill_x"]
    cases = skill_eval._cases_for_skills(["skill_x"], [{"caseId": "other", "skillId": "other"}])
    assert cases[0]["caseId"] == "synthetic-skill_x"
    aggregated = skill_eval._aggregate_result("skillId", "skill_x", "X", [], {})
    assert aggregated["status"] == "FAIL" and aggregated["overallScore"] == 0.0
    assert skill_eval._comparison_view_from_report({"skillResults": "bad", "packResults": "bad"}) == {"skillResults": [], "packResults": []}


def test_versioning_missing_current_pack_and_public_revision_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.skills import registry

    monkeypatch.setattr(versioning, "_load_skill_snapshots", lambda _skill_id: [])
    monkeypatch.setattr(versioning, "_load_pack_snapshots", lambda _pack_id: [])
    monkeypatch.setattr(registry, "get_skill", lambda *_args, **_kwargs: (_ for _ in ()).throw(AppError("missing")))
    monkeypatch.setattr(registry, "get_pack", lambda *_args, **_kwargs: (_ for _ in ()).throw(AppError("missing")))
    assert versioning.list_skill_versions("skill_missing") == []
    assert versioning.list_pack_versions("pack_missing") == []
    assert versioning._public_revision({"metadata": {"version": "1"}, "path": 3, "current": 1}) == {
        "version": "1",
        "path": "3",
        "current": True,
    }
    assert versioning._strings("bad") == []


def test_versioning_load_pack_snapshots_skips_malformed_payloads(tmp_settings: Path) -> None:
    directory = versioning.pack_history_dir("pack_demo")
    directory.mkdir(parents=True)
    (directory / "wrong.json").write_text(json.dumps({"schemaVersion": versioning.PACK_REVISION_SCHEMA, "metadata": [], "pack": {}}), encoding="utf-8")
    (directory / "valid.json").write_text(
        json.dumps({"schemaVersion": versioning.PACK_REVISION_SCHEMA, "metadata": {"createdAt": "1"}, "pack": {"packId": "pack_demo"}}),
        encoding="utf-8",
    )
    snapshots = versioning._load_pack_snapshots("pack_demo")
    assert len(snapshots) == 1 and snapshots[0]["pack"]["packId"] == "pack_demo"


def test_versioning_pack_rollback_builtin_and_custom_project_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.skills import registry

    target = {"metadata": {"version": "1.0.0"}, "pack": {"packId": "pack_demo", "name": "Demo", "description": "Demo", "version": "1.0.0", "skills": []}}
    monkeypatch.setattr(versioning, "_resolve_pack_revision", lambda *_args: target)
    monkeypatch.setattr(registry, "get_pack", lambda _pack_id: {"packId": "pack_demo", "version": "2.0.0", "builtin": True})
    with pytest.raises(AppError):
        versioning.rollback_pack("pack_demo", "1.0.0")

    monkeypatch.setattr(registry, "get_pack", lambda _pack_id: {"packId": "pack_demo", "version": "2.0.0", "builtin": False})
    monkeypatch.setattr(versioning, "snapshot_pack", lambda pack, **_kwargs: {"revisionId": "rev", "version": pack.get("version")})
    monkeypatch.setattr(versioning, "_pack_for_snapshot", lambda pack: dict(pack))
    monkeypatch.setattr(registry, "write_custom_pack", lambda _pack: None)
    monkeypatch.setattr(registry, "public_pack", lambda pack, **_kwargs: pack)
    monkeypatch.setattr(versioning.projects, "enable_pack_for_project", lambda *_args, **_kwargs: {"enabled": True})
    result = versioning.rollback_pack("pack_demo", "1.0.0", project_id="proj_demo")
    assert result["projectBinding"] == {"enabled": True}
    assert result["pack"]["version"] == "1.0.0"


def test_versioning_pack_upgrade_current_custom_and_builtin_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.skills import registry

    current = {"packId": "pack_demo", "version": "2.0.0", "builtin": False, "skills": []}
    monkeypatch.setattr(registry, "get_pack", lambda _pack_id: current)
    monkeypatch.setattr(versioning, "eval_aware_upgrade_gate", lambda **_kwargs: {"status": "PASS"})
    assert versioning.upgrade_pack("pack_demo", "latest")["targetVersion"] == "2.0.0"

    target = {"pack": {"packId": "pack_demo", "name": "Demo", "description": "Demo", "version": "1.0.0", "skills": []}}
    monkeypatch.setattr(versioning, "_resolve_pack_revision", lambda *_args: target)
    monkeypatch.setattr(versioning, "_pack_for_snapshot", lambda pack: dict(pack))
    monkeypatch.setattr(registry, "write_custom_pack", lambda _pack: None)
    monkeypatch.setattr(versioning, "snapshot_pack", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(versioning.projects, "enable_pack_for_project", lambda *_args, **_kwargs: {"enabled": True})
    result = versioning.upgrade_pack("pack_demo", "1.0.0", project_id="proj_demo")
    assert result["projectBinding"] == {"enabled": True}
    current["builtin"] = True
    with pytest.raises(AppError):
        versioning.upgrade_pack("pack_demo", "1.0.0")
