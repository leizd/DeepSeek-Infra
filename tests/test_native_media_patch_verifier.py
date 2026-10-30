"""Offline guard and observation tests; these do not qualify a native mutation."""
from __future__ import annotations

import argparse
import errno
from email.message import Message
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import time
from types import ModuleType
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.usefixtures("tmp_settings")
@pytest.mark.parametrize("case", [
    "all-fields", "source-only", "path-only", "status-only", "type-only", "mime-only", "null-reset", "type-inference", "source-ordered",
])
def test_field_probes_are_successful_retained_original_workloads(
    case: str, verifier: ModuleType,
) -> None:
    from deepseek_infra.infra.media import library

    records = json.loads(verifier.FIXTURE.read_text(encoding="utf-8"))["media"]
    library.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    library.store_path().write_text(json.dumps({"media": records}), encoding="utf-8")
    before = library.get_media("media_keep")
    result = library.update_media("media_keep", verifier.FIELD_PATCHES[case])
    assert result["mediaId"] == before["mediaId"] and result["createdAt"] == before["createdAt"]
    assert result == library.get_media("media_keep")
    expected = {
        "all-fields": ("type", "image"), "path-only": ("path", "objects/media_keep/alternate.pdf"),
        "status-only": ("status", "processing"), "type-only": ("type", "video"),
        "mime-only": ("mimeType", "audio/ogg"), "null-reset": ("status", "pending"),
        "type-inference": ("type", "audio"),
    }
    if case in expected:
        field, value = expected[case]
        assert result[field] == value
    else:
        assert result["source"] != before["source"]


@pytest.mark.usefixtures("tmp_settings")
@pytest.mark.parametrize("case,expected_status", [
    ("all-fields", 200), ("source-only", 400), ("path-only", 400), ("status-only", 400), ("type-only", 400),
    ("mime-only", 400), ("null-reset", 200), ("type-inference", 200), ("source-ordered", 400),
])
def test_original_public_route_retains_its_three_field_boundary(
    case: str, expected_status: int, verifier: ModuleType,
) -> None:
    from fastapi.testclient import TestClient
    from deepseek_infra.infra.media import library
    from deepseek_infra.web.server import create_app, settings

    records = json.loads(verifier.FIXTURE.read_text(encoding="utf-8"))["media"]
    library.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    library.store_path().write_text(json.dumps({"media": records}), encoding="utf-8")
    before = library.get_media("media_keep")
    client = TestClient(create_app(), base_url="http://127.0.0.1")
    try:
        response = client.patch("/api/media/media_keep", json=verifier.FIELD_PATCHES[case],
                                headers={"Authorization": "Bearer " + settings.auth.token})
    finally:
        client.close()
    assert response.status_code == expected_status
    after = library.get_media("media_keep")
    if expected_status == 400:
        assert response.json()["code"] == "invalid_payload" and after == before
    else:
        assert response.json()["media"] == after
        for field in ("path", "source", "status", "type", "mimeType"):
            assert after[field] == before[field]


def test_explicit_empty_journal_observation_never_accepts_an_incomplete_effect(
    verifier: ModuleType, tmp_path: Path,
) -> None:
    journal = tmp_path / "media-effects.sqlite3"
    with sqlite3.connect(journal) as connection:
        connection.execute("PRAGMA application_id=1146309968")
        connection.execute("CREATE TABLE media_operations(operation_id,state,claim)")
    with pytest.raises(verifier.ProbeFailure, match="MEDIA_PATCH_EFFECT_JOURNAL_INVALID"):
        verifier.journal_observation(tmp_path)
    assert verifier.journal_observation(tmp_path, allow_empty=True) == {"state": "EMPTY", "operationCount": 0}
    with sqlite3.connect(journal) as connection:
        connection.execute("INSERT INTO media_operations VALUES('retained','APPLYING',X'00')")
    with pytest.raises(verifier.ProbeFailure, match="MEDIA_PATCH_EFFECT_JOURNAL_INVALID"):
        verifier.journal_observation(tmp_path, allow_empty=True)


def test_timeout_diagnostics_read_only_metadata_without_private_payloads(tmp_path: Path, verifier: ModuleType) -> None:
    profile = tmp_path / "owned"
    profile.mkdir()
    database = profile / "control.sqlite3"
    secret = b"PRIVATE-DIAGNOSTIC-SENTINEL"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE control_media_operations(operation_id,state,epoch,writer_fencing_token,lease_until,intent,outcome)")
        connection.execute("INSERT INTO control_media_operations VALUES(?,?,?,?,?,?,?)", ("media-patch-owned", "DISPATCHED", 1, 2, 1000, secret, secret))
    before = database.read_bytes()
    observed = verifier.uncertain_request_snapshot(profile, [])
    assert observed["go"]["status"] == "OBSERVED"
    assert observed["go"]["rows"][0]["intentBytes"] == len(secret)
    assert observed["go"]["rows"][0]["state"] == "DISPATCHED"
    assert secret.decode() not in json.dumps(observed)
    assert database.read_bytes() == before


def test_timeout_diagnostics_refuse_writes_and_external_paths(tmp_path: Path, verifier: ModuleType) -> None:
    database = tmp_path / "control.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE control_media_operations(operation_id)")
        connection.execute("INSERT INTO control_media_operations VALUES('retained')")
    before = database.read_bytes()
    observed = verifier.diagnostic_table(tmp_path, "control.sqlite3", "DELETE FROM control_media_operations", ())
    assert observed["status"] == "UNAVAILABLE" and observed["error"] == "OperationalError"
    assert verifier.diagnostic_table(tmp_path, "../outside.sqlite3", "SELECT 1", ()) == {"status": "REFUSED"}
    assert database.read_bytes() == before


def test_timeout_diagnostics_bind_cpu_counters_to_the_owned_process_identity(tmp_path: Path, verifier: ModuleType) -> None:
    process = tmp_path / "1234"
    process.mkdir()
    fields = ["0"] * 22
    fields[0], fields[11], fields[12], fields[19], fields[21] = "S", "7", "5", "300", "400"
    (process / "stat").write_text("1234 (test (thread)) " + " ".join(fields), encoding="utf-8")
    identities = [{"pid": 1234, "executable": "deepseekd", "startTicks": "300"}]
    observed = verifier.request_resources(identities, tmp_path)
    assert observed[0]["cpuTicks"] == 12 and observed[0]["rssPages"] == 400
    identities[0]["startTicks"] = "301"
    changed = verifier.request_resources(identities, tmp_path)
    assert changed[0]["status"] == "IDENTITY_CHANGED" and "cpuTicks" not in changed[0]


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


def test_loopback_preflight_refuses_a_live_listener_and_preserves_it(verifier: ModuleType) -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(2)
        host, port = listener.getsockname()
        with pytest.raises(verifier.bootstrap.ProbeFailure, match=f"NATIVE_LOOPBACK_PORT_UNAVAILABLE:{port}:"):
            verifier.bootstrap.require_loopback_ports_available((port,))
        # The probe may not terminate, replace, or borrow the other listener.
        with socket.create_connection((host, port), timeout=2) as client:
            client.sendall(b"retained-listener")
            accepted, _ = listener.accept()
            with accepted:
                assert accepted.recv(64) == b"retained-listener"


def test_loopback_preflight_releases_its_temporary_listener(verifier: ModuleType) -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        host, port = listener.getsockname()
    verifier.bootstrap.require_loopback_ports_available((port,))
    with socket.create_server((host, port)) as replacement:
        assert replacement.getsockname() == (host, port)


@pytest.mark.skipif(os.name != "posix" or not Path("/proc/net/tcp").is_file(), reason="requires the Linux TCP TIME_WAIT kernel state")
def test_loopback_preflight_accepts_stopped_listener_time_wait(verifier: ModuleType) -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(2)
        host, port = listener.getsockname()
        with socket.create_connection((host, port), timeout=2) as client:
            accepted, _ = listener.accept()
            with accepted:
                accepted.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
            # Server closes first; the peer's FIN leaves its port in TIME_WAIT.
        deadline = time.monotonic() + 2
        while not any(row.split()[1] == f"0100007F:{port:04X}" and row.split()[3] == "06"
                      for row in Path("/proc/net/tcp").read_text().splitlines()[1:]):
            assert time.monotonic() < deadline, "controlled TCP connection did not enter TIME_WAIT"
            time.sleep(0.01)
    # This is the exact old bind-only preflight; its failure is independently observed.
    with socket.socket() as old_probe:
        with pytest.raises(OSError) as original:
            old_probe.bind((host, port))
    assert original.value.errno == errno.EADDRINUSE
    verifier.bootstrap.require_loopback_ports_available((port,))
    with socket.create_server((host, port)) as replacement:
        assert replacement.getsockname() == (host, port)


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
