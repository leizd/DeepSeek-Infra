"""Small offline guards for the cloud-only large-library counterexample."""
from __future__ import annotations

import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def capacity_verifier(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("media_capacity_verifier_test", ROOT / "scripts/verify_native_media_patch.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("environment", [
    b"DEEPSEEK_MEDIA_PHASE_TIMING=1\0UNRELATED=not-reported\0",
    b"UNRELATED=not-reported\0DEEPSEEK_MEDIA_PHASE_TIMING=1\0",
])
def test_phase_timing_checks_the_owned_worker_without_reporting_environment(
    environment: bytes, capacity_verifier: ModuleType, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(capacity_verifier.reads, "process_identity", lambda _pid: "7")

    def read_environment(path: Path, *_args: object, **_kwargs: object) -> BytesIO:
        assert path.as_posix() == "/proc/123/environ"
        return BytesIO(environment)

    monkeypatch.setattr(Path, "open", read_environment)
    assert capacity_verifier.phase_timing_environment([
        {"pid": 123, "executable": "deepseek-worker", "startTicks": "7"},
        {"pid": 456, "executable": "deepseek-gateway", "startTicks": "8"},
    ]) is True
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("environment,reason", [
    (b"OTHER=1\0", "ENVIRONMENT_NOT_RETAINED"),
    (b"DEEPSEEK_MEDIA_PHASE_TIMING=0\0", "ENVIRONMENT_NOT_RETAINED"),
    (b"DEEPSEEK_MEDIA_PHASE_TIMING=0\0DEEPSEEK_MEDIA_PHASE_TIMING=1\0", "ENVIRONMENT_NOT_RETAINED"),
    (b"x" * (256 * 1024 + 1), "ENVIRONMENT_EXCEEDED_BOUND"),
], ids=["missing", "wrong-value", "duplicate", "oversized"])
def test_phase_timing_refuses_missing_duplicate_wrong_or_oversized_environment(
    environment: bytes, reason: str, capacity_verifier: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capacity_verifier.reads, "process_identity", lambda _pid: "7")
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: BytesIO(environment))
    with pytest.raises(capacity_verifier.ProbeFailure, match=reason):
        capacity_verifier.phase_timing_environment([{"pid": 123, "executable": "deepseek-worker", "startTicks": "7"}])


def test_phase_timing_refuses_reused_or_ambiguous_worker_identity(
    capacity_verifier: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = iter(["7", "8"])
    monkeypatch.setattr(capacity_verifier.reads, "process_identity", lambda _pid: next(identities))
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: BytesIO(b"DEEPSEEK_MEDIA_PHASE_TIMING=1\0"))
    worker = {"pid": 123, "executable": "deepseek-worker", "startTicks": "7"}
    with pytest.raises(capacity_verifier.ProbeFailure, match="WORKER_IDENTITY_CHANGED"):
        capacity_verifier.phase_timing_environment([worker])
    with pytest.raises(capacity_verifier.ProbeFailure, match="WORKER_IDENTITY_MISSING"):
        capacity_verifier.phase_timing_environment([worker, worker])


@pytest.mark.usefixtures("tmp_settings")
def test_large_fixture_retains_original_cases_and_accepts_all_metadata_in_original_schema(
    capacity_verifier: ModuleType,
) -> None:
    from deepseek_infra.infra.media import schema

    base = json.loads(capacity_verifier.FIXTURE.read_text(encoding="utf-8"))["media"]
    original = json.dumps(base, ensure_ascii=False, sort_keys=True)
    records = capacity_verifier.large_library_media(base)
    assert len(records) == 618 and records[:18] == base
    assert json.dumps(base, ensure_ascii=False, sort_keys=True) == original
    added = records[18:]
    assert len({row["mediaId"] for row in added}) == 600
    metadata_characters = 0
    for row in added:
        normalized = schema.normalize_media_record(row)
        assert normalized["mediaId"] == row["mediaId"]
        assert normalized["metadata"] == row["metadata"]
        metadata_characters += sum(len(value) for value in normalized["metadata"].values())
    assert metadata_characters == 144_000_000 > 128 * 1024**2
    # The full JSON is deliberately not serialized or written on this host.


@pytest.mark.usefixtures("tmp_settings")
def test_large_record_uses_original_default_metadata_and_preserves_other_duplicate_records(
    capacity_verifier: ModuleType,
) -> None:
    from deepseek_infra.infra.media import schema

    base = json.loads(capacity_verifier.FIXTURE.read_text(encoding="utf-8"))["media"]
    selected = next(index for index, row in enumerate(base)
                    if isinstance(row, dict) and row.get("mediaId") == capacity_verifier.MEDIA_ID)
    assert "metadata" not in base[selected]  # The actual raw fixture which failed in the cloud.
    original = json.dumps(base, ensure_ascii=False, sort_keys=True)
    records = capacity_verifier.large_record_media(base)
    assert len(records) == 18
    assert json.dumps(base, ensure_ascii=False, sort_keys=True) == original
    assert all(record == base[index] for index, record in enumerate(records) if index != selected)
    normalized = schema.normalize_media_record(records[selected])
    assert normalized["mediaId"] == capacity_verifier.MEDIA_ID and normalized["title"] == "Notes"
    assert sum(len(value) for value in normalized["metadata"].values()) == 4_320_000
    assert all(normalized["metadata"][f"retained_large_{index:02}"] == "r" * 120_000 for index in range(36))


@pytest.mark.parametrize("selection", [["--large-library-case"], ["--field-case", "all-fields"],
                                       ["--metadata-order-case", "nested"]])
def test_large_record_case_rejects_combined_workloads_before_native_startup(
    selection: list[str], capacity_verifier: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    output = tmp_path / "not-created"
    monkeypatch.setattr(sys, "argv", ["verify_native_media_patch.py", "--worker", "worker", "--edge", "edge",
        "--go-cache", "cache", "--out", str(output), "--large-record-case", *selection])
    monkeypatch.setattr(capacity_verifier, "run", lambda *_: pytest.fail("conflicting case started native work"))
    with pytest.raises(capacity_verifier.ProbeFailure, match="MEDIA_PATCH_LARGE_RECORD_CASE_SELECTION_CONFLICT"):
        capacity_verifier.main()
    assert not output.exists()


@pytest.mark.parametrize("selection", [["--field-case", "all-fields"], ["--metadata-order-case", "nested"]])
def test_large_library_case_cannot_silently_change_an_original_case(
    selection: list[str], capacity_verifier: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    output = tmp_path / "not-created"
    monkeypatch.setattr(sys, "argv", ["verify_native_media_patch.py", "--worker", "worker", "--edge", "edge",
        "--go-cache", "cache", "--out", str(output), "--large-library-case", *selection])

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("conflicting cases must be rejected before native work")

    monkeypatch.setattr(capacity_verifier, "run", forbidden)
    with pytest.raises(capacity_verifier.ProbeFailure, match="MEDIA_PATCH_CAPACITY_CASE_SELECTION_CONFLICT"):
        capacity_verifier.main()
    assert not output.exists()
