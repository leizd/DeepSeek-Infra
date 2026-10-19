"""Offline negative report fixtures; these are never Android execution evidence."""
from pathlib import Path

import pytest

from scripts.verify_native_android_cloud import cloud_context, instrumentation_cases, validate_instrumentation


CASES = {"com.deepseek.mobile.OfflineTest#one", "com.deepseek.mobile.OfflineTest#two"}


def sample() -> str:
    lines = []
    for case in sorted(CASES):
        owner, method = case.split("#")
        for code in (1, 0):
            lines += [f"INSTRUMENTATION_STATUS: class={owner}", f"INSTRUMENTATION_STATUS: test={method}",
                      "INSTRUMENTATION_STATUS: numtests=2", f"INSTRUMENTATION_STATUS_CODE: {code}"]
    return "\n".join(lines + ["INSTRUMENTATION_RESULT: stream=", "OK (2 tests)", "INSTRUMENTATION_CODE: -1", ""])


def test_offline_protocol_example_requires_every_started_case_to_finish() -> None:
    assert validate_instrumentation(sample(), CASES) == sorted(CASES)


@pytest.mark.parametrize("before,after", [
    ("INSTRUMENTATION_CODE: -1", ""),
    ("INSTRUMENTATION_CODE: -1", "INSTRUMENTATION_CODE: 0"),
    ("OK (2 tests)", "FAILURES!!!"),
    ("OK (2 tests)", "OK (0 tests)"),
    ("numtests=2", "numtests=0"),
    ("INSTRUMENTATION_STATUS_CODE: 0", "INSTRUMENTATION_STATUS_CODE: -3"),
    ("INSTRUMENTATION_STATUS_CODE: 0", "INSTRUMENTATION_STATUS_CODE: -2"),
    ("INSTRUMENTATION_STATUS_CODE: 1", "INSTRUMENTATION_STATUS_CODE: 2"),
    ("test=two", "test=another"),
    ("OfflineTest", "DifferentTest"),
])
def test_skips_failures_missing_terminal_and_rebound_cases_cannot_qualify(before: str, after: str) -> None:
    with pytest.raises(ValueError):
        validate_instrumentation(sample().replace(before, after, 1), CASES)


def test_summary_alone_cannot_prove_any_device_test() -> None:
    with pytest.raises(ValueError):
        validate_instrumentation("OK (2 tests)\nINSTRUMENTATION_CODE: -1\n", CASES)


def test_duplicate_finished_or_unstarted_case_cannot_replace_missing_case() -> None:
    text = sample()
    first = text.index("INSTRUMENTATION_STATUS: class=", text.index("INSTRUMENTATION_STATUS_CODE: 1"))
    with pytest.raises(ValueError):
        validate_instrumentation(text[first:], CASES)
    with pytest.raises(ValueError):
        validate_instrumentation(text.replace("test=two", "test=one"), CASES)


def test_ignored_status_or_runner_crash_never_counts_as_pass() -> None:
    with pytest.raises(ValueError):
        validate_instrumentation(sample() + "INSTRUMENTATION_FAILED: Process crashed\n", CASES)
    with pytest.raises(ValueError):
        validate_instrumentation(sample() + "INSTRUMENTATION_CODE: -1\n", CASES)


def test_inventory_includes_every_existing_instrumentation_method() -> None:
    root = Path(__file__).resolve().parents[1]
    cases = instrumentation_cases(root)
    assert len(cases) == 7
    assert "com.deepseek.mobile.NativeRuntimeIntegrationTest#packagedRuntimeCreatesRunsDownloadsAndRecoversPersistedState" in cases
    assert "com.deepseek.mobile.NativeFileOcrIntegrationTest#actualImageAndRasterPdfUploadReadRenderAndPersist" in cases
    assert "com.deepseek.mobile.PlatformOcrProtocolIntegrationTest#cancelledUploadAndRestartCleanOwnedTemporaryFilesAndRotateCredential" in cases


@pytest.mark.parametrize("field,value", [("GITHUB_SHA", "b" * 40), ("GITHUB_RUN_ID", "0"), ("GITHUB_RUN_ATTEMPT", ""),
    ("GITHUB_REPOSITORY", "another/repository"), ("GITHUB_REF", ""), ("GITHUB_EVENT_NAME", "schedule"), ("unchanged", "")])
def test_cloud_context_cannot_rebind_commit_run_repository_or_candidate(monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
    values = {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": "leizd/DeepSeek-Infra",
              "GITHUB_REF": "refs/heads/codex/offline-fixture", "GITHUB_EVENT_NAME": "push"}
    for key, item in values.items():
        monkeypatch.setenv(key, item)
    if field == "unchanged":
        assert cloud_context("a" * 40)["runId"] == "123"
    else:
        monkeypatch.setenv(field, value)
        with pytest.raises(ValueError):
            cloud_context("a" * 40)


def test_inventory_does_not_skip_other_packages_or_different_class_names(tmp_path: Path) -> None:
    directory = tmp_path / "android/app/src/androidTest/java/another/package"
    directory.mkdir(parents=True)
    (directory / "DifferentName.java").write_text(
        "package another.package;\npublic class DifferentName {\n@Test public void actualWorkload() {}\n}\n", encoding="utf-8")
    assert instrumentation_cases(tmp_path) == {"another.package.DifferentName#actualWorkload"}


@pytest.mark.parametrize("event,ref,valid", [
    ("pull_request", "refs/pull/123/merge", True),
    ("workflow_dispatch", "refs/heads/codex/offline-fixture", True),
    ("pull_request", "refs/heads/codex/offline-fixture", False),
    ("pull_request", "refs/pull/123/head", False),
    ("push", "refs/pull/123/merge", False),
    ("workflow_dispatch", "refs/pull/123/merge", False),
])
def test_source_branch_and_pr_merge_candidate_context_cannot_be_confused(
    monkeypatch: pytest.MonkeyPatch, event: str, ref: str, valid: bool,
) -> None:
    values = {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": "leizd/DeepSeek-Infra",
              "GITHUB_REF": ref, "GITHUB_EVENT_NAME": event}
    for key, item in values.items():
        monkeypatch.setenv(key, item)
    if valid:
        assert cloud_context("a" * 40)["ref"] == ref
    else:
        with pytest.raises(ValueError):
            cloud_context("a" * 40)
