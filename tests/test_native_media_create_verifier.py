"""Original API and evidence-boundary checks; no local native compilation."""
import argparse
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("verify_native_media_create", SCRIPTS / "verify_native_media_create.py")
assert SPEC is not None and SPEC.loader is not None
creation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(creation)


@pytest.mark.parametrize("left,right", [(False, 0), (True, 1), (1, 1.0), ({"zero":False}, {"zero":0}), ([True], [1])])
def test_parity_comparison_retains_exact_json_scalar_types(left, right) -> None:
    assert creation.same_json(left, right) is False
    assert creation.same_json(left, left) is True


def test_actual_original_asgi_create_writes_the_original_source_and_metadata(tmp_path: Path) -> None:
    value = creation.original_observation(tmp_path / "original-profile", tmp_path / "oracle.log")
    record = value["body"]["media"]
    assert value["status"] == 200 and record["type"] == "webpage" and record["status"] == "pending"
    assert record["metadata"]["a"] == "last" and record["metadata"]["unicode"] == "原生迁移"
    assert record["metadata"]["webpageText"] == "Original webpage text"
    assert (tmp_path / "original-profile/.media" / record["path"]).read_bytes() == creation.PAYLOAD["html"].encode()
    assert value["requestId"] == creation.REQUEST_ID
    normalized = creation.canonical_media(record)
    assert normalized["path"] == "objects/{mediaId}/source.html" and "mediaId" not in normalized


@pytest.mark.parametrize("kind,mime", [("image", "image/png"), ("pdf", "application/pdf"), ("audio", "audio/mpeg"),
    ("video", "video/mp4"), ("webpage", "text/html"), ("screenshot", "image/png")])
def test_actual_original_asgi_retains_all_six_registration_kinds_without_source(tmp_path: Path, kind: str, mime: str) -> None:
    value = creation.original_observation(tmp_path / "original-profile", tmp_path / "oracle.log",
        dict(type=kind, mimeType=mime, metadata=dict(zero=0, disabled=False)), "")
    record = value["body"]["media"]
    assert record["type"] == kind and record["path"] == "" and value["sourceSha256"] is None
    assert type(record["metadata"]["zero"]) is int and type(record["metadata"]["disabled"]) is bool


@pytest.mark.parametrize("media_id,path", [
    ("wrong", "objects/wrong/source.html"),
    ("media_" + "a" * 16, "../outside.html"),
    ("media_" + "a" * 16, "objects/media_" + "b" * 16 + "/source.html"),
    ("media_" + "a" * 16, "objects/media_" + "a" * 16 + "/different.html"),
])
def test_record_identity_cannot_hide_an_unbound_or_escaped_source(media_id: str, path: str) -> None:
    with pytest.raises(creation.ProbeFailure):
        creation.canonical_media(dict(mediaId=media_id, path=path, createdAt="2026-01-01T00:00:00Z", updatedAt="2026-01-01T00:00:00Z"))


@pytest.mark.parametrize("timestamp", ["invalid", "2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z", "2026-01-01T00:00:00"])
def test_creation_timestamp_must_be_causal_and_timezone_aware(timestamp: str) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(creation.ProbeFailure):
        creation.require_creation_time(dict(createdAt=timestamp, updatedAt=timestamp), now, now)


def test_native_compilation_guard_precedes_all_tools_and_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(creation.ProbeFailure, match="MEDIA_CREATE_VERIFICATION_IS_CLOUD_ONLY"):
        creation.run(argparse.Namespace(), {})


@pytest.mark.parametrize("origin", ["https://127.0.0.1", "http://127.0.0.1.evil.test", "http://token@127.0.0.1"])
def test_http_probe_cannot_send_credentials_to_another_origin(origin: str) -> None:
    with pytest.raises(creation.ProbeFailure, match="MEDIA_CREATE_ORIGIN_INVALID"):
        creation.exchange(origin, "/api/media", "POST", "offline-fixture", {})


def test_missing_method_with_empty_body_remains_an_actual_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 405
        headers = {"Allow": "GET"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == creation.LIMIT + 1
            return b""

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(creation, "build_opener", lambda *_: Opener())
    status, body, headers = creation.exchange("http://127.0.0.1:12345", "/api/media", "POST", "fixture", {})
    assert status == 405 and body["nonObjectJSONResponse"] is True and headers == {"allow": "GET"}
