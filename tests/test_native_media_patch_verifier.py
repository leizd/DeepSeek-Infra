"""Offline guard and observation tests; these do not qualify a native mutation."""
from __future__ import annotations

import argparse
from email.message import Message
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def verifier(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("media_patch_verifier_test", ROOT / "scripts/verify_native_media_patch.py")
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_native_guard_precedes_every_subprocess(verifier: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def forbidden(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("the local guard must precede even source/toolchain subprocesses")

    monkeypatch.setattr(verifier.subprocess, "check_output", forbidden)
    with pytest.raises(verifier.ProbeFailure, match="NATIVE_MEDIA_PATCH_VERIFICATION_IS_CLOUD_ONLY"):
        verifier.run(argparse.Namespace(), {})


def test_plain_http_refusal_is_observed_without_json_decoding(verifier: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"404 page not found\n\xff"
    headers = Message()
    headers["Content-Type"] = "text/plain"

    def refused(*_args: object, **_kwargs: object) -> None:
        raise HTTPError("http://127.0.0.1/api/media/media_keep", 404, "Not Found", headers, io.BytesIO(raw))

    monkeypatch.setattr(verifier, "urlopen", refused)
    assert verifier.exchange("http://127.0.0.1", "/api/media/media_keep", "PATCH", None, {}) == (404, raw, "text/plain")


def test_response_body_bound_also_applies_to_http_errors(verifier: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def oversized(*_args: object, **_kwargs: object) -> None:
        raise HTTPError("http://127.0.0.1/api/media/media_keep", 503, "Unavailable", Message(),
                        io.BytesIO(b"x" * (verifier.LIMIT + 1)))

    monkeypatch.setattr(verifier, "urlopen", oversized)
    with pytest.raises(verifier.ProbeFailure, match="NATIVE_MEDIA_PATCH_RESPONSE_EXCEEDED_BOUND"):
        verifier.exchange("http://127.0.0.1", "/api/media/media_keep", "PATCH", None, {})


def test_comparison_preserves_nested_timestamps_and_original_input(verifier: ModuleType) -> None:
    body = {"ok": True, "media": {"mediaId": "media_keep", "updatedAt": "new", "createdAt": "retained",
                                 "metadata": {"updatedAt": "user metadata"}}}
    original = json.loads(json.dumps(body))
    compared = verifier.without_update_time(body)
    assert compared == {"ok": True, "media": {"mediaId": "media_keep", "createdAt": "retained",
                                              "metadata": {"updatedAt": "user metadata"}}}
    assert body == original


def test_lost_reply_keeps_unknown_without_another_attempt(
    verifier: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    attempts = []

    def interrupted(_args: argparse.Namespace, report: dict[str, object]) -> None:
        attempts.append("sent")
        report["nativePatchRequestStarted"] = True
        raise OSError("the response disappeared")

    monkeypatch.setattr(verifier, "run", interrupted)
    monkeypatch.setattr(sys, "argv", ["verify_native_media_patch.py", "--worker", "not-built-worker", "--edge", "not-built-edge",
                                      "--go-cache", str(tmp_path / "not-built-cache"), "--out", str(tmp_path / "reports")])
    assert verifier.main() == 1
    result = json.loads((tmp_path / "reports/result.json").read_text())
    assert attempts == ["sent"]
    assert result["nativePatchOutcome"] == "EFFECT_UNKNOWN"
    assert result["semanticStageReached"] is False
    assert result["controlledNativeMediaPatchWorkloadQualified"] is False
    assert not (tmp_path / "not-built-cache").exists()


def test_missing_generation_is_observed_without_creating_it(verifier: ModuleType, tmp_path: Path) -> None:
    assert verifier.generation_observation(tmp_path) == {"present": False, "value": 0, "sha256": None}
    assert list(tmp_path.iterdir()) == []


def test_generation_observation_retains_the_exact_counter_hash(verifier: ModuleType, tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / ".workspace-generation"
    raw = b"42\n"
    path.write_bytes(raw)
    assert verifier.generation_observation(tmp_path) == {
        "present": True, "value": 42, "sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert path.read_bytes() == raw


@pytest.mark.parametrize("raw", [b"", b"-1", b"broken", b"0" * 33])
def test_invalid_generation_cannot_be_reported_as_zero(
    verifier: ModuleType, tmp_path: Path, raw: bytes,
) -> None:
    path = tmp_path / ".workspace-generation"
    path.write_bytes(raw)
    with pytest.raises(verifier.ProbeFailure, match="MEDIA_PATCH_GENERATION_COUNTER_INVALID"):
        verifier.generation_observation(tmp_path)
    assert path.read_bytes() == raw


def test_directory_cannot_be_reported_as_a_generation(verifier: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / ".workspace-generation"
    path.mkdir()
    with pytest.raises(verifier.ProbeFailure, match="MEDIA_PATCH_GENERATION_COUNTER_INVALID"):
        verifier.generation_observation(tmp_path)
    assert path.is_dir()


@pytest.mark.parametrize("case,metadata", [
    ("top-level", {"a?": "first", "a!": "last"}),
    ("nested", {"outer": {"a?": "first", "a!": "last"}}),
    ("array", {"rows": [{"a?": "first", "a!": "last"}]}),
    ("truncated", {"k" * 80 + "z": "first", "k" * 80 + "a": "last"}),
])
def test_metadata_order_workload_keeps_the_exact_input_order(
    verifier: ModuleType, case: str, metadata: dict[str, object],
) -> None:
    patch = verifier.case_patch(case)
    assert patch["title"] == "Native media update"
    assert json.dumps(patch["metadata"]) == json.dumps(metadata)


def test_metadata_order_cases_do_not_alias_the_default_workload(verifier: ModuleType) -> None:
    original = json.dumps(verifier.PATCH)
    first = verifier.case_patch("top-level")
    first["metadata"]["a?"] = "changed"
    assert verifier.case_patch("top-level")["metadata"]["a?"] == "first"
    assert json.dumps(verifier.PATCH) == original


def test_unknown_metadata_order_workload_fails_closed(verifier: ModuleType) -> None:
    with pytest.raises(verifier.ProbeFailure, match="MEDIA_PATCH_ORDER_CASE_INVALID"):
        verifier.case_patch("unrecognized")


def test_maximum_array_workload_stays_inside_the_original_http_limit(verifier: ModuleType) -> None:
    patch = verifier.case_patch("maximum-array")
    raw = verifier.wire_payload(patch)
    assert 900_000 < len(raw) <= 1_000_000
    assert len(json.loads(raw)["metadata"]) == 499_960
    assert set(json.loads(raw)["metadata"]) == {0}


def test_wire_payload_keeps_input_order_without_formatting_expansion(verifier: ModuleType) -> None:
    assert verifier.wire_payload({"metadata": {"a?": "first", "a!": "last"}}) == (
        b'{"metadata":{"a?":"first","a!":"last"}}'
    )


def make_effect_journal(root: Path, state: str) -> Path:
    import sqlite3

    path = root / "rust-worker/media-effects.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA application_id=1146309968")
        connection.execute("CREATE TABLE media_operations(operation_id TEXT, state TEXT, claim BLOB)")
        connection.execute("INSERT INTO media_operations VALUES (?,?,?)", ("fixture-operation", state, b"fixture-claim"))
    return path


def test_effect_journal_observer_is_read_only(verifier: ModuleType, tmp_path: Path) -> None:
    import hashlib

    path = make_effect_journal(tmp_path, "APPLIED")
    original = path.read_bytes()
    result = verifier.journal_observation(tmp_path)
    assert result == {"operationId": "fixture-operation", "state": "APPLIED", "claimBytes": 13,
                      "claimSha256": hashlib.sha256(b"fixture-claim").hexdigest()}
    assert path.read_bytes() == original


@pytest.mark.parametrize("state", ["PREPARED", "APPLYING", "REFUSED"])
def test_effect_journal_observer_cannot_qualify_an_unapplied_row(
    verifier: ModuleType, tmp_path: Path, state: str,
) -> None:
    path = make_effect_journal(tmp_path, state)
    original = path.read_bytes()
    with pytest.raises(verifier.ProbeFailure, match="MEDIA_PATCH_EFFECT_JOURNAL_INVALID"):
        verifier.journal_observation(tmp_path)
    assert path.read_bytes() == original


def test_effect_journal_observer_rejects_absent_or_ambiguous_storage(verifier: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(verifier.ProbeFailure, match="MEDIA_PATCH_EFFECT_JOURNAL_INVALID"):
        verifier.journal_observation(tmp_path)
    make_effect_journal(tmp_path / "one", "APPLIED")
    make_effect_journal(tmp_path / "two", "APPLIED")
    with pytest.raises(verifier.ProbeFailure, match="MEDIA_PATCH_EFFECT_JOURNAL_INVALID"):
        verifier.journal_observation(tmp_path)
