"""Offline safety checks for the read-only native report transport."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location("relay_native_reports", Path(__file__).parents[1] / "scripts/relay_native_reports.py")
assert SPEC is not None and SPEC.loader is not None
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)


@pytest.mark.parametrize("url", ["http://api.github.com/x", "https://token@api.github.com/x", "https://api.github.com:444/x", "https://api.github.com.evil.test/x"])
def test_report_transport_rejects_untrusted_addresses(url: str) -> None:
    with pytest.raises(AssertionError):
        relay.checked_address(url)


def test_report_specs_cannot_select_other_repositories_paths_or_duplicates() -> None:
    assert len(relay.validate_specs(copy.deepcopy(relay.DEFAULT_SPECS))) == 3
    for key, value in [("kind", "arbitrary-cache"), ("source", "main"), ("run", True), ("event", "pull_request")]:
        invalid = copy.deepcopy(relay.DEFAULT_SPECS)
        invalid[0][key] = value
        with pytest.raises(AssertionError):
            relay.validate_specs(invalid)
    with pytest.raises(AssertionError):
        relay.validate_specs([relay.DEFAULT_SPECS[0], relay.DEFAULT_SPECS[0]])
    with pytest.raises(AssertionError):
        relay.validate_specs([{**relay.DEFAULT_SPECS[0], "url": "https://evil.test"}])


def test_report_projection_preserves_independent_coverage_and_original_ignore() -> None:
    kind, value = relay.project("coverage.lcov", b"SF:a.rs\nLF:10\nLH:9\nend_of_record\nSF:b.rs\nLF:5\nLH:4\nend_of_record\n")
    assert kind == "LCOV_LINE_TOTALS_ONLY" and value == dict(records=2, totalLines=15, coveredLines=13)
    _, tests = relay.project("coverage.log", b"test original ... ignored, needs fixture\ntest result: ok. 5 passed; 0 failed; 1 ignored;\n")
    assert tests["passed"] == 5 and tests["ignored"] == 1 and tests["originalIgnoreLines"] == ["test original ... ignored, needs fixture"]
    with pytest.raises(AssertionError):
        relay.project("coverage.log", b"test result: FAILED. 1 passed; 1 failed; 0 ignored;\n")


def test_diagnostic_redaction_retains_operation_fence_and_binary_hash() -> None:
    removed: list[str] = []
    before = dict(actionId="original", epoch=1, claimTokenSha256="a" * 64, binarySha256="b" * 64,
                  token="secret-fixture", launchProcesses=[dict(pid=12, startTicks="45", executable="worker", argv=["private-fixture"])])
    after = relay.sanitize(before, removed=removed)
    assert after["actionId"] == "original" and after["epoch"] == 1 and after["claimTokenSha256"] == "a" * 64
    assert after["binarySha256"] == "b" * 64 and after["launchProcesses"] == [dict(pid=12, startTicks="45", executable="worker")]
    assert "secret-fixture" not in json.dumps(after) and "private-fixture" not in json.dumps(after)
    assert removed == ["/token", "/launchProcesses/diagnostic-fields"]


@pytest.mark.parametrize("name", ["../escape.json", "/absolute.json", "C:/drive.json", "nested\\escape.json"])
def test_archive_member_paths_cannot_escape(name: str) -> None:
    data = io.BytesIO()
    stored_name = name.replace("\\", "/")
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(stored_name, b"{}")
    # Windows normalizes a backslash when creating ZipInfo. Mutate both ZIP
    # filename headers after writing so the reader sees the actual hostile name.
    if stored_name != name:
        data = io.BytesIO(data.getvalue().replace(stored_name.encode(), name.encode()))
    with zipfile.ZipFile(data) as archive, pytest.raises(AssertionError):
        relay.archive_members(archive)


def test_notice_chunks_round_trip_and_remain_below_observed_truncation() -> None:
    import base64
    import gzip
    value = dict(reports=[dict(kind="fixture", preserved="x" * 10000)])
    chunks = relay.notice_chunks(value)
    assert chunks and all(len(piece) <= 3000 for piece in chunks)
    assert json.loads(gzip.decompress(base64.b64decode("".join(chunks), validate=True))) == value


def test_redirect_never_forwards_api_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, count):
            assert count == 101
            return b"report"

    class Opener:
        def open(self, request, timeout):
            assert timeout == 30
            seen.append(request)
            if len(seen) == 1:
                raise relay.urllib.error.HTTPError(request.full_url, 302, "redirect", {"Location": "https://fixture.blob.core.windows.net/report"}, None)
            return Response()

    monkeypatch.setenv("GH_TOKEN", "offline-fixture-only")
    monkeypatch.setattr(relay.urllib.request, "build_opener", lambda *_: Opener())
    assert relay.fetch("https://api.github.com/report", 100, True) == b"report"
    assert seen[0].get_header("Authorization") == "Bearer offline-fixture-only"
    assert seen[1].get_header("Authorization") is None


def test_symlink_archive_members_and_oversized_envelopes_are_refused() -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        link = zipfile.ZipInfo("result.json")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, b"elsewhere")
    with zipfile.ZipFile(data) as archive, pytest.raises(AssertionError):
        relay.archive_members(archive)
    with pytest.raises(AssertionError):
        relay.notice_chunks(dict(oversized="x" * relay.MAX_ENVELOPE))
