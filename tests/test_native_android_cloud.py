"""Offline negative report fixtures; these are never Android execution evidence."""
import argparse
import json
from pathlib import Path

import pytest

from scripts.verify_native_android_cloud import cloud_context, instrumentation_cases, validate_instrumentation
from scripts import verify_native_android_cloud as android_cloud


CASES = {"com.deepseek.mobile.OfflineTest#one", "com.deepseek.mobile.OfflineTest#two"}


def test_missing_log_exit_status_cannot_be_mistaken_for_collected_child_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def execute(command: list[str], **_kwargs: object) -> object:
        missing = command[-1].endswith("worker.log")
        # exec-out can deliver the remote error as successful transport output.
        code = 1 if missing and command[1:3] == ["shell", "-T"] else 0
        body = b"tail: files/.native-runtime-logs/worker.log: No such file or directory\n" if missing else b"child stderr\n"
        return android_cloud.subprocess.CompletedProcess(command, code, stdout=body, stderr=b"")

    monkeypatch.setattr(android_cloud.subprocess, "run", execute)
    rows = {row["service"]: row for row in android_cloud.collect_runtime_logs(tmp_path)}
    assert rows["control"]["status"] == rows["gateway"]["status"] == "COLLECTED"
    assert rows["worker"]["status"] == "UNAVAILABLE"


def test_kernel_probe_retains_only_fixed_platform_facts_and_marks_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    collect = getattr(android_cloud, "collect_device_environment", None)
    assert callable(collect), "actual Android kernel evidence is missing"
    calls: list[list[str]] = []

    def read(arguments: list[str], output: Path, timeout: int = 120) -> str:
        calls.append(arguments)
        assert arguments[:2] == ["shell", "-T"] and timeout <= 15
        if arguments[2:] == ["uname", "-r"]:
            output.write_bytes(b"3.10.0-test-fixture\n")
            return "3.10.0-test-fixture"
        output.write_bytes(b"unavailable\n")
        raise ValueError("device fact unavailable")

    monkeypatch.setattr(android_cloud, "adb", read)
    facts = collect(tmp_path)
    assert facts["kernelRelease"] == {"status": "COLLECTED", "value": "3.10.0-test-fixture"}
    assert facts["kernelMachine"]["status"] == "UNAVAILABLE"
    assert calls == [["shell", "-T", "uname", "-r"], ["shell", "-T", "uname", "-m"]]


def test_child_diagnostics_read_only_bounded_logs_from_disposable_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def read(arguments: list[str], output: Path, timeout: int = 120) -> str:
        calls.append(arguments)
        assert arguments[:5] == ["shell", "-T", "run-as", "com.deepseek.mobile.nativeverify", "tail"]
        assert arguments[5:7] == ["-c", "131072"] and timeout <= 30
        assert arguments[7] in {"files/.native-runtime-logs/gateway.log", "files/.native-runtime-logs/control.log", "files/.native-runtime-logs/worker.log"}
        output.write_text("disposable child startup diagnostic\n", encoding="utf-8")
        return "disposable child startup diagnostic"

    monkeypatch.setattr(android_cloud, "adb", read)
    results = android_cloud.collect_runtime_logs(tmp_path)
    assert len(calls) == 3 and {row["service"] for row in results} == {"gateway", "control", "worker"}
    assert all(row["status"] == "COLLECTED" for row in results)


def test_missing_child_log_preserves_other_runtime_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def read(arguments: list[str], output: Path, timeout: int = 120) -> str:
        if arguments[-1].endswith("worker.log"):
            output.write_text("tail: no such file\n", encoding="utf-8")
            raise ValueError("ADB command failed")
        output.write_text("native child error\n", encoding="utf-8")
        return "native child error"

    monkeypatch.setattr(android_cloud, "adb", read)
    results = {row["service"]: row for row in android_cloud.collect_runtime_logs(tmp_path)}
    assert results["worker"]["status"] == "UNAVAILABLE"
    assert results["gateway"]["status"] == results["control"]["status"] == "COLLECTED"
    assert (tmp_path / "runtime-gateway.log").read_text() == "native child error\n"


def test_invalid_source_binding_cannot_read_app_sandbox_or_qualify(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    supplied = tmp_path / "build-context.json"
    supplied.write_text("{}", encoding="utf-8")

    def rejected(*args: object) -> None:
        raise ValueError("rebound source binding")

    def read(arguments: list[str], output: Path, timeout: int = 120) -> str:
        assert arguments[:3] == ["logcat", "-d", "-t"]
        output.write_text("bounded logcat\n", encoding="utf-8")
        return "bounded logcat"

    monkeypatch.setattr(android_cloud, "build_report", rejected)
    monkeypatch.setattr(android_cloud, "adb", read)
    def forbidden(output: Path) -> list[dict[str, str]]:
        raise AssertionError("unverified source attempted to read an app sandbox")

    monkeypatch.setattr(android_cloud, "collect_runtime_logs", forbidden, raising=False)
    args = argparse.Namespace(output=tmp_path / "output", expected_commit="a" * 40, api_level=24,
                              build_report=supplied, root=tmp_path, apk=tmp_path / "app.apk", test_apk=tmp_path / "test.apk")
    assert android_cloud.run_device(args) == 1
    result = json.loads((args.output / "result.json").read_text())
    assert result["status"] == "FAIL" and result["error"] == "rebound source binding"
    assert result["nativeRuntimeLogs"] == []
    assert result["platformEnvironment"] == {}
    assert not result["deviceExecutionVerified"] and not result["fullMigrationQualified"]


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
    assert len(cases) == 8
    assert "com.deepseek.mobile.NativeControlLockIntegrationTest#packagedControlExcludesLegacyLockFamiliesAndReleasesOnlyAfterShutdown" in cases
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
