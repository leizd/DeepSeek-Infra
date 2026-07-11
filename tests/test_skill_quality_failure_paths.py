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
