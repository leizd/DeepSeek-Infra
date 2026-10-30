import argparse
import importlib.util
import sys
from pathlib import Path
import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("source_sync", SCRIPTS / "verify_native_media_source_sync.py")
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def test_native_source_sync_cloud_guard_precedes_builds(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(RuntimeError, match="SOURCE_SYNC_NATIVE_IS_CLOUD_ONLY"):
        sync.run(argparse.Namespace(), {})


@pytest.mark.parametrize(
    "code,output,trace,before,after",
    [
        (0, "test result: ok. 0 passed; 0 failed; 0 ignored", "EIO INJECTED", "same", "same"),
        (0, "test result: ok. 1 passed; 0 failed; 0 ignored", "", "same", "same"),
        (1, "test result: ok. 1 passed; 0 failed; 0 ignored", "EIO INJECTED", "same", "same"),
        (0, "test result: ok. 1 passed; 0 failed; 0 ignored", "EIO INJECTED", "before", "after"),
    ],
)
def test_source_sync_cannot_be_qualified_by_empty_tests_missing_fault_or_changed_data(code, output, trace, before, after):
    assert sync.qualify_case(code, output, trace, before, after)["status"] == "FAIL"


def test_source_sync_requires_one_passed_rust_test_real_error_and_unchanged_bytes():
    assert sync.qualify_case(0, "test result: ok. 1 passed; 0 failed; 0 ignored", "EIO INJECTED", "same", "same")["status"] == "PASS"
