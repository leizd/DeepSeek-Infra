from __future__ import annotations

from pathlib import Path

from deepseek_infra.core.errors import AppError
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.skills import analytics, runner


def test_skill_run_analytics_records_success_failure_summary_and_redaction(tmp_settings: Path) -> None:
    result = runner.run_skill(
        "skill_research_brief",
        {"topic": "analytics", "depth": "quick"},
        offline=True,
        persist=True,
    )

    runs = analytics.list_runs(skill_id="skill_research_brief", limit=10)
    assert runs[0]["skillRunId"] == result["skillRunId"]
    assert runs[0]["skillVersion"]
    assert runs[0]["status"] == "completed"
    assert runs[0]["artifactCount"] >= 1
    assert runs[0]["links"]["trace"]

    try:
        runner.run_skill("skill_research_brief", {}, offline=True, persist=True)
    except AppError:
        pass

    failed = analytics.list_runs(status="failed", limit=10)
    assert failed
    assert failed[0]["failureCategory"] == "schema_validation_failed"
    assert failed[0]["diagnosticSuggestion"]

    summary = analytics.analytics_summary(scope="skill", skill_id="skill_research_brief")
    assert summary["totalRuns"] >= 2
    assert summary["failedRuns"] >= 1
    assert summary["successRuns"] >= 1
    assert summary["p90LatencyMs"] >= 0
    assert summary["topSkills"][0]["id"] == "skill_research_brief"

    redacted = analytics.redact_run(result["skillRunId"])
    assert redacted["run"]["inputSummary"] == "[redacted]"
    assert analytics.get_run(result["skillRunId"])["redacted"] is True

    cleanup = analytics.cleanup_runs(status="failed")
    assert cleanup["deleted"] >= 1


def test_skill_run_retention_cleanup_and_manual_record(tmp_settings: Path) -> None:
    skill = {"skillId": "skill_manual", "version": "1.0.0"}
    for index in range(3):
        analytics.record_failure(
            skill=skill,
            run_id=f"run-manual-{index}",
            input_data={"topic": index},
            started_at=utc_now_iso(),
            error="timeout",
            category="timeout",
            retention=2,
        )

    runs = analytics.list_runs(skill_id="skill_manual", limit=0)
    assert [run["skillRunId"] for run in runs] == ["run-manual-2", "run-manual-1"]
    assert runs[0]["failureCategory"] == "timeout"

    deleted = analytics.delete_run("run-manual-2")
    assert deleted["deleted"] == 1
    assert [run["skillRunId"] for run in analytics.list_runs(skill_id="skill_manual", limit=0)] == ["run-manual-1"]
