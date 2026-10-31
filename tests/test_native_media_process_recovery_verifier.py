from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("native_media_process_crash_probe", ROOT / "scripts/verify_native_media_process_recovery.py")
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def original() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    operation = "media-process-" + "a" * 32
    publication = dict(operationId=operation, actionId=operation, executionEpoch=1, requestSha256=[7] * 32)
    library = dict(media=[dict(mediaId="media_target", status="processing")], nativePublication=publication)
    control = dict(operationId=operation, actionId=operation, epoch=1, claimWriterFence=7,
                   intentSha256="1" * 64, requestSha256=bytes([7] * 32).hex(), claimTokenSha256="2" * 64,
                   ownerSha256="3" * 64, state="DISPATCHED", outcomeSha256=None)
    journal = dict(operationId=operation, state="APPLYING", claimSha256="4" * 64, preparationSha256="5" * 64,
                   beforeSha256="6" * 64, afterSha256="8" * 64, plannedSha256="9" * 64, outcomeSha256=None)
    return library, journal, control


@pytest.mark.parametrize("phase", ["processing", "final"])
def test_process_crash_observer_requires_the_exact_original_phase_and_fence(phase: str) -> None:
    library, journal, control = original()
    if phase == "final":
        library["media"][0]["status"] = "ready"
        journal["afterSha256"] = hashlib.sha256(json.dumps(library).encode()).hexdigest()
    assert probe.held_phase(json.dumps(library).encode(), journal, control, "media_target", phase)


def test_old_library_is_not_a_processing_or_terminal_effect_witness() -> None:
    library, journal, control = original()
    library["media"][0]["status"] = "ready"
    library.pop("nativePublication")
    for phase in probe.PHASES:
        assert not probe.held_phase(json.dumps(library).encode(), journal, control, "media_target", phase)


@pytest.mark.parametrize("damage", ["journal-op", "control-op", "control-action", "marker-op", "marker-action", "epoch", "bool-epoch", "digest", "digest-size", "bool-byte"])
def test_process_crash_cannot_qualify_a_rebound_or_malformed_publication(damage: str) -> None:
    library, journal, control = original()
    marker = library["nativePublication"]
    if damage == "journal-op":
        journal["operationId"] = "media-create-" + "a" * 32
    elif damage == "control-op":
        control["operationId"] = "media-process-" + "b" * 32
    elif damage == "control-action":
        control["actionId"] = "media-process-" + "b" * 32
    elif damage == "marker-op":
        marker["operationId"] = "media-process-" + "b" * 32
    elif damage == "marker-action":
        marker["actionId"] = "media-process-" + "b" * 32
    elif damage == "epoch":
        marker["executionEpoch"] = 2
    elif damage == "bool-epoch":
        marker["executionEpoch"] = True
    elif damage == "digest":
        marker["requestSha256"][0] = 8
    elif damage == "digest-size":
        marker["requestSha256"].pop()
    else:
        marker["requestSha256"][0] = True
    with pytest.raises(probe.create.patch.ProbeFailure):
        probe.held_phase(json.dumps(library).encode(), journal, control, "media_target", "processing")


@pytest.mark.parametrize("side", ["go", "rust"])
def test_process_crash_requires_the_actual_preterminal_journal_state(side: str) -> None:
    library, journal, control = original()
    (control if side == "go" else journal)["state"] = "APPLIED"
    with pytest.raises(probe.create.patch.ProbeFailure, match="TERMINAL_PRECEDED_HOLD"):
        probe.held_phase(json.dumps(library).encode(), journal, control, "media_target", "processing")


@pytest.mark.parametrize("rows", [[], [dict(mediaId="media_target", status="processing")] * 2])
def test_process_crash_rejects_missing_or_duplicate_targets(rows: list[dict[str, Any]]) -> None:
    library, journal, control = original()
    library["media"] = rows
    with pytest.raises(probe.create.patch.ProbeFailure, match="TARGET_AMBIGUOUS"):
        probe.held_phase(json.dumps(library).encode(), journal, control, "media_target", "processing")


def settled() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    _, before_rust, before_go = original()
    after_go, after_rust = copy.deepcopy(before_go), copy.deepcopy(before_rust)
    for row in [after_go, after_rust]:
        row.update(state="APPLIED", outcomeSha256="a" * 64)
    return before_go, before_rust, after_go, after_rust


def test_process_recovery_keeps_the_original_epoch_claim_plan_and_signed_terminal_receipt() -> None:
    probe.require_recovered(*settled(), 7, 8)


@pytest.mark.parametrize("side,key", [(side, key) for side, keys in [
    ("go", probe.recovery.IMMUTABLE_GO), ("rust", probe.recovery.IMMUTABLE_RUST)] for key in keys])
def test_process_recovery_cannot_replace_any_original_control_or_effect_identity(side: str, key: str) -> None:
    before_go, before_rust, after_go, after_rust = settled()
    target = after_go if side == "go" else after_rust
    target[key] = target[key] + 1 if type(target[key]) is int else target[key] + "changed"
    with pytest.raises(probe.create.patch.ProbeFailure, match="ORIGINAL_REBOUND"):
        probe.require_recovered(before_go, before_rust, after_go, after_rust, 7, 8)


@pytest.mark.parametrize("damage", ["missing-outcomes", "different-outcome", "unknown-go", "unknown-rust", "old-writer", "bool-writer"])
def test_process_recovery_cannot_promote_unknown_absent_or_different_results(damage: str) -> None:
    before_go, before_rust, after_go, after_rust = settled()
    writer = 8
    if damage == "missing-outcomes":
        after_go["outcomeSha256"] = after_rust["outcomeSha256"] = None
    elif damage == "different-outcome":
        after_rust["outcomeSha256"] = "b" * 64
    elif damage == "unknown-go":
        after_go["state"] = "EFFECT_UNKNOWN"
    elif damage == "unknown-rust":
        after_rust["state"] = "APPLYING"
    elif damage == "old-writer":
        writer = 7
    else:
        writer = True
    with pytest.raises(probe.create.patch.ProbeFailure):
        probe.require_recovered(before_go, before_rust, after_go, after_rust, 7, writer)


def test_process_crash_index_observation_is_readonly_and_preserves_exact_foreign_columns(tmp_path: Path) -> None:
    path = tmp_path / ".local-rag/rag.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE rag_items(item_id,collection,source_id,project_id,chunk_index,embedding,metadata,vector_id,scope)")
        db.execute("INSERT INTO rag_items VALUES(?,?,?,?,?,?,?,?,?)", ("foreign", "media", "other", "p", -7, "[ 1.0 ]", "{\"x\": 1}", 91, "team"))
        db.execute("CREATE TABLE rag_meta(key,value)")
        db.execute("INSERT INTO rag_meta VALUES('embedding_provider','hash')")
    before = path.read_bytes()
    result = probe.index_snapshot(tmp_path, "sqlite")
    assert result["rows"][0]["chunk_index"] == -7
    assert result["rows"][0]["embedding"] == "[ 1.0 ]" and result["rows"][0]["metadata"] == '{"x": 1}'
    assert result["vectors"] is None and path.read_bytes() == before
    probe.require_unrelated_index(result, copy.deepcopy(result), "target")
    for damage in ("metadata", "foreign-column"):
        changed = copy.deepcopy(result)
        if damage == "metadata":
            changed["metadata"]["embedding_provider"] = "different"
        else:
            changed["rows"][0]["chunk_index"] = 0
        with pytest.raises(probe.create.patch.ProbeFailure):
            probe.require_unrelated_index(result, changed, "target")


def test_process_crash_run_fails_before_any_local_native_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe.sys, "platform", "win32")
    with pytest.raises(probe.create.patch.ProbeFailure, match="VERIFICATION_IS_CLOUD_ONLY"):
        probe.run(None, {})


def process_index_markers(journal: dict[str, Any]) -> dict[str, str]:
    return {f"native_media_process:{journal['operationId']}:{phase}": journal["preparationSha256"]
            for phase in ("clear", "final")}


def process_index_fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _, journal, _ = original()
    rows = [dict(item_id="foreign", collection="media", source_id="other", project_id="p", chunk_index=-7),
            dict(item_id="other-project", collection="media", source_id="target", project_id="other", chunk_index=42),
            dict(item_id="target", collection="media", source_id="target", project_id="p", chunk_index=0)]
    before: dict[str, Any] = dict(rows=rows, vectors=copy.deepcopy(rows), metadata={
        "embedding_provider": "hash", "embedding_dimensions": "64", "unrelated-key": "retained",
        "native_media_process:media-process-" + "b" * 32 + ":clear": "6" * 64})
    after = copy.deepcopy(before)
    after["metadata"].update(process_index_markers(journal))
    after["rows"][-1]["chunk_index"] = 1
    after["vectors"][-1]["chunk_index"] = 1
    return before, after, journal


def test_process_recovery_accepts_only_both_exact_original_index_transaction_markers() -> None:
    before, after, journal = process_index_fixture()
    untouched_before, untouched_after = copy.deepcopy(before), copy.deepcopy(after)
    probe.require_unrelated_index(before, after, "target", journal, project_id="p")
    assert (before, after) == (untouched_before, untouched_after)


@pytest.mark.parametrize("damage", ["missing-clear", "missing-final", "clear-digest", "final-digest", "foreign-operation",
                                    "extra-phase", "provider", "dimensions", "foreign-metadata", "removed-metadata",
                                    "foreign-marker", "other-project-row", "other-project-vector"])
def test_process_recovery_rejects_changed_namespace_or_unbound_transaction_markers(damage: str) -> None:
    before, after, journal = process_index_fixture()
    keys = list(process_index_markers(journal))
    if damage.startswith("missing-"):
        after["metadata"].pop(keys[0 if damage == "missing-clear" else 1])
    elif damage.endswith("-digest"):
        after["metadata"][keys[0 if damage == "clear-digest" else 1]] = "0" * 64
    elif damage == "foreign-operation":
        after["metadata"][keys[0].replace("a" * 32, "c" * 32)] = after["metadata"].pop(keys[0])
    elif damage == "extra-phase":
        after["metadata"][keys[0].replace(":clear", ":other")] = "5" * 64
    elif damage == "provider":
        after["metadata"]["embedding_provider"] = "different"
    elif damage == "dimensions":
        after["metadata"]["embedding_dimensions"] = "65"
    elif damage == "foreign-metadata":
        after["metadata"]["unrelated-key"] = "changed"
    elif damage == "removed-metadata":
        after["metadata"].pop("unrelated-key")
    elif damage == "foreign-marker":
        after["metadata"]["native_media_process:media-process-" + "b" * 32 + ":clear"] = "changed"
    else:
        after["vectors" if damage == "other-project-vector" else "rows"][1]["chunk_index"] = 0
    with pytest.raises(probe.create.patch.ProbeFailure):
        probe.require_unrelated_index(before, after, "target", journal, project_id="p")


@pytest.mark.parametrize("damage", ["operation", "preparation", "missing-preparation", "preexisting-clear", "preexisting-final"])
def test_process_recovery_refuses_invalid_or_preexisting_original_markers(damage: str) -> None:
    before, after, journal = process_index_fixture()
    if damage == "operation":
        journal["operationId"] = "media-create-" + "a" * 32
    elif damage == "preparation":
        journal["preparationSha256"] = "not-a-digest"
    elif damage == "missing-preparation":
        journal.pop("preparationSha256")
    else:
        phase = damage.removeprefix("preexisting-")
        key = f"native_media_process:{journal['operationId']}:{phase}"
        before["metadata"][key] = after["metadata"][key]
    with pytest.raises(probe.create.patch.ProbeFailure):
        probe.require_unrelated_index(before, after, "target", journal, project_id="p")


def test_process_crash_runtime_cannot_launch_outside_cloud_even_on_linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    runtime = probe.Runtime(tmp_path / "bin", tmp_path / "profile", 18123, "sqlite", 64, tmp_path, {})
    with pytest.raises(probe.create.patch.ProbeFailure, match="VERIFICATION_IS_CLOUD_ONLY"):
        runtime.launch("refused-local-launch")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status,healthy", [(200, True), (200, False), (503, False)])
def test_process_launch_health_uses_the_control_observer_without_widening_media_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int, healthy: bool,
) -> None:
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    profile = tmp_path / "profile"
    profile.mkdir()
    token = "7" * 64
    (profile / ".auth-token").write_text(token)
    child = SimpleNamespace(pid=100)
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(probe.create.patch.bootstrap, "wait_public", lambda *args: None)
    roles = ["deepseek-launch", "deepseek-launch", "deepseek-worker", "deepseek-launch", "deepseekd", "deepseek-launch", "deepseek-gateway"]
    monkeypatch.setattr(probe.create.patch.bootstrap, "process_roles", lambda pid: [dict(pid=100+i, executable=role) for i, role in enumerate(roles)])
    monkeypatch.setattr(probe.create.patch.reads, "process_identity", lambda pid: pid + 2000)
    calls = []

    def control_exchange(origin: str, path: str, method: str, auth: str) -> tuple[int, bytes, str]:
        calls.append((origin, path, method, auth))
        return status, json.dumps(dict(ok=healthy)).encode(), "application/json"

    monkeypatch.setattr(probe.create.patch, "exchange", control_exchange)
    runtime = probe.Runtime(tmp_path / "bin", profile, 18123, "sqlite", 64, tmp_path, {})
    if status == 200 and healthy:
        assert runtime.launch("health") == token
    else:
        with pytest.raises(probe.create.patch.ProbeFailure, match="CONTROL_UNHEALTHY"):
            runtime.launch("health")
    assert calls == [("http://127.0.0.1:18123", "/api/control/status", "GET", token)]
