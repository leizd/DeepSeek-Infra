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
