from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEST = "TestNativeMacJavaScriptModalCancellationSettlesApplication"


def test_modal_probe_executes_the_actual_case_on_both_original_native_runners() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/native-windows-cloud.yml").read_text())
    job = workflow["jobs"]["macos-window"]
    assert job["timeout-minutes"] == 15
    assert job["strategy"]["matrix"]["include"] == [
        {"architecture": "amd64", "runner": "macos-15-intel"}, {"architecture": "arm64", "runner": "macos-15"},
    ]
    steps = [step for step in job["steps"] if TEST in step.get("run", "")]
    assert len(steps) == 1
    step = steps[0]
    assert "-test.timeout=45s" in step["run"] and "modal-cancel-test.log" in step["run"]
    assert "DEEPSEEK_MACOS_MODAL_REPORT" in step["env"]
    assert step.get("continue-on-error", False) is False and "|| true" not in step["run"]
    for required in ["actualJavaScriptModalObserved", "nativeMainThreadObserved", "contextCancellationSettled", "testAbortUsed"]:
        assert required in step["run"]


def test_probe_does_not_substitute_a_manual_abort_for_the_original_application_cancel() -> None:
    source = (ROOT / "go/internal/desktop/native_modal_darwin_integration_test.go").read_text()
    assert "alert('DeepSeek actual native modal cancellation fixture')" in source
    assert 'macSEL("modalWindow")' in source and 'macSEL("isMainThread")' in source
    assert "NSModalPanelRunLoopMode" in source and "performSelectorOnMainThread:withObject:waitUntilDone:modes:" in source
    assert "cancel()" in source and '"testAbortUsed": false' in source
    assert 'macSEL("abortModal")' not in source and 'macSEL("stopModal")' not in source
    assert "NATIVE_MODAL_CONTEXT_CANCELLATION_DID_NOT_SETTLE" in source
