from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
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
                   processPreparationSha256="c" * 64,
                   beforeSha256="6" * 64, afterSha256="8" * 64, plannedSha256="9" * 64, outcomeSha256=None)
    return library, journal, control


def test_unselected_process_corpus_still_requires_all_six_original_kinds() -> None:
    assert probe.selected_kinds(None) == ("webpage", "image", "screenshot", "pdf", "audio", "video")


@pytest.mark.parametrize("kind", ["webpage", "image", "screenshot", "pdf", "audio", "video"])
def test_one_matrix_cell_selects_exactly_its_explicit_original_kind(kind: str) -> None:
    assert probe.selected_kinds(kind) == (kind,)


@pytest.mark.parametrize("invalid", ["", "all", "unknown", "webpage,image", True, ["webpage"], 1])
def test_invalid_process_selector_cannot_silently_reduce_or_replace_the_corpus(invalid: Any) -> None:
    with pytest.raises(probe.create.patch.ProbeFailure, match="MEDIA_PROCESS_CRASH_KIND_INVALID"):
        probe.selected_kinds(invalid)


def publication_sequence() -> tuple[Any, bytes, bytes, dict[str, Any], dict[str, Any], dict[str, Any]]:
    library, journal, control = original()
    processing = json.dumps(library).encode()
    library["media"][0]["status"] = "ready"
    final = json.dumps(library).encode()
    journal["afterSha256"] = hashlib.sha256(final).hexdigest()
    initial = dict(sha256="d" * 64, inode=10, mtimeNs=1)
    observer = probe.PublicationObserver(initial, "media_target", "final")
    return observer, processing, final, journal, control, initial


def test_final_publication_accepts_initial_inode_reuse_only_after_observed_processing_replacement() -> None:
    observer, processing, final, journal, control, initial = publication_sequence()
    middle = dict(sha256=hashlib.sha256(processing).hexdigest(), inode=11, mtimeNs=2)
    assert not observer.observe(processing, journal, control, middle)
    assert observer.observe(final, journal, control, dict(sha256=hashlib.sha256(final).hexdigest(), inode=initial["inode"], mtimeNs=3))
    assert observer.processing == dict(publication=middle, go=control, rust=journal)


def test_final_publication_refuses_to_infer_a_missed_processing_phase() -> None:
    observer, _, final, journal, control, _ = publication_sequence()
    with pytest.raises(probe.create.patch.ProbeFailure, match="PROCESSING_PREDECESSOR_NOT_OBSERVED"):
        observer.observe(final, journal, control, dict(sha256=hashlib.sha256(final).hexdigest(), inode=12, mtimeNs=3))


@pytest.mark.parametrize("phase", ["processing", "final"])
def test_publication_cannot_qualify_an_in_place_write_at_either_boundary(phase: str) -> None:
    observer, processing, final, journal, control, initial = publication_sequence()
    middle = dict(sha256=hashlib.sha256(processing).hexdigest(), inode=11, mtimeNs=2)
    if phase == "final":
        assert not observer.observe(processing, journal, control, middle)
        raw, witness = final, dict(sha256=hashlib.sha256(final).hexdigest(), inode=middle["inode"], mtimeNs=3)
    else:
        raw, witness = processing, dict(middle, inode=initial["inode"])
    with pytest.raises(probe.create.patch.ProbeFailure, match="REAL_RENAME_NOT_OBSERVED"):
        observer.observe(raw, journal, control, witness)


@pytest.mark.parametrize("side,key", [("go", "operationId"), ("go", "epoch"), ("go", "claimWriterFence"),
                                      ("go", "requestSha256"), ("rust", "claimSha256"), ("rust", "plannedSha256"),
                                      ("rust", "processPreparationSha256")])
def test_final_publication_cannot_change_original_identity_between_the_two_real_phases(side: str, key: str) -> None:
    observer, processing, final, journal, control, _ = publication_sequence()
    assert not observer.observe(processing, journal, control, dict(sha256=hashlib.sha256(processing).hexdigest(), inode=11, mtimeNs=2))
    changed = copy.deepcopy(control if side == "go" else journal)
    changed[key] = 2 if isinstance(changed[key], int) else "e" * 64
    with pytest.raises(probe.create.patch.ProbeFailure):
        observer.observe(final, changed if side == "rust" else journal, changed if side == "go" else control,
                         dict(sha256=hashlib.sha256(final).hexdigest(), inode=12, mtimeNs=3))


def test_publication_bytes_must_match_the_same_file_witness() -> None:
    observer, processing, _, journal, control, _ = publication_sequence()
    with pytest.raises(probe.create.patch.ProbeFailure, match="PUBLICATION_BYTES_REBOUND"):
        observer.observe(processing, journal, control, dict(sha256="e" * 64, inode=11, mtimeNs=2))


def test_real_atomic_replacements_can_return_to_the_initial_inode(tmp_path: Path) -> None:
    observer, processing, final, journal, control, _ = publication_sequence()
    library, retained, staged = (tmp_path / name for name in ["library.json", "retained-original", "next"])
    library.write_bytes(b"initial")
    os.link(library, retained)
    observer = probe.PublicationObserver(probe.file_witness(library, tmp_path), "media_target", "final")
    staged.write_bytes(processing)
    os.replace(staged, library)
    middle = probe.file_witness(library, tmp_path)
    assert not observer.observe(library.read_bytes(), journal, control, middle)
    retained.write_bytes(final)
    os.replace(retained, library)
    assert observer.observe(library.read_bytes(), journal, control, probe.file_witness(library, tmp_path))


def test_repeated_processing_observation_retains_one_unchanged_predecessor() -> None:
    observer, processing, _, journal, control, _ = publication_sequence()
    witness = dict(sha256=hashlib.sha256(processing).hexdigest(), inode=11, mtimeNs=2)
    assert not observer.observe(processing, journal, control, witness)
    assert not observer.observe(processing, journal, control, witness)
    with pytest.raises(probe.create.patch.ProbeFailure, match="PROCESSING_PREDECESSOR_CHANGED"):
        observer.observe(processing, journal, control, dict(witness, inode=12))


def test_processing_crash_still_stops_at_its_first_real_replacement() -> None:
    _, processing, _, journal, control, initial = publication_sequence()
    observer = probe.PublicationObserver(initial, "media_target", "processing")
    assert observer.observe(processing, journal, control, dict(sha256=hashlib.sha256(processing).hexdigest(), inode=11, mtimeNs=2))


def test_publication_reader_binds_bytes_and_identity_from_the_same_file(tmp_path: Path) -> None:
    library = tmp_path / "library.json"
    library.write_bytes(b"publication")
    result = probe.read_publication(library, tmp_path)
    assert result == (b"publication", probe.file_witness(library, tmp_path))


@pytest.mark.parametrize("mutation", ["replacement", "in-place"])
def test_publication_reader_retries_concurrent_change_instead_of_mixing_file_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    library = tmp_path / "library.json"
    library.write_bytes(b"before")
    fstat, path_stat, calls = os.fstat, Path.stat, 0
    replaced = False

    def changed(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2 and mutation == "in-place":
            library.write_bytes(b"different-length")
        return fstat(fd)

    def changed_path(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal replaced
        # Replace after the descriptor closes: Windows forbids replacing the
        # open Python file, while this later race is real on both platforms.
        if mutation == "replacement" and calls == 2 and path == library and not replaced:
            replaced = True
            next_file = tmp_path / "next"
            next_file.write_bytes(b"after")
            os.replace(next_file, library)
        return path_stat(path, *args, **kwargs)

    monkeypatch.setattr(probe.os, "fstat", changed)
    monkeypatch.setattr(Path, "stat", changed_path)
    assert probe.read_publication(library, tmp_path) is None
    assert calls == 2


def test_publication_reader_keeps_the_original_library_byte_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library = tmp_path / "library.json"
    library.write_bytes(b"12345")
    monkeypatch.setattr(probe, "BOUND", 4)
    with pytest.raises(probe.create.patch.ProbeFailure, match="LIBRARY_BOUND"):
        probe.read_publication(library, tmp_path)


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
    return {f"native_media_process:{journal['operationId']}:{phase}": journal["processPreparationSha256"]
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
        journal["processPreparationSha256"] = "not-a-digest"
    elif damage == "missing-preparation":
        journal.pop("processPreparationSha256")
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


def prepared_process_profile(tmp_path: Path, damage: str = "") -> tuple[Path, bytes, bytes]:
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    files = descriptor_pb2.FileDescriptorSet.FromString((ROOT / "proto/generated/descriptor.pb").read_bytes())
    pool = descriptor_pool.DescriptorPool()
    for item in files.file:
        pool.Add(item)
    def message(name: str) -> Any:
        return message_factory.GetMessageClass(pool.FindMessageTypeByName("deepseek.media.v1." + name))()
    operation = "media-process-" + "a" * 32
    private = message("MediaProcessPreparation")
    private.source_relative_path = "objects/media_target/source.txt"
    private.source_sha256 = hashlib.sha256(b"").digest()
    private_bytes = private.SerializeToString(deterministic=True)
    path = tmp_path / "worker-staging" / operation / "process.pb"
    path.parent.mkdir(parents=True)
    path.write_bytes(private_bytes)
    claim = message("MediaPatchClaim")
    claim.schema_version = claim.intent.schema_version = 1
    claim.intent.operation_id = claim.fence.action_id = operation
    claim.intent.media_id = "media_target"
    claim.intent.kind = 3
    claim.fence.execution_epoch = 1
    claim.request_sha256 = hashlib.sha256(claim.intent.SerializeToString(deterministic=True)).digest()
    plan = message("MediaPatchOutcome")
    plan.schema_version = 1
    plan.operation_id = operation
    plan.original_fence.CopyFrom(claim.fence)
    plan.request_sha256 = claim.request_sha256
    plan.state = plan.DESCRIPTOR.fields_by_name["state"].enum_type.values_by_name["MEDIA_PATCH_EFFECT_STATE_APPLIED"].number
    plan.before_sha256, plan.after_sha256 = (hashlib.sha256(v).digest() for v in [b"before", b"after"])
    plan.processed.preparation_sha256 = hashlib.sha256(private_bytes).digest()
    plan.processed.original_fence.CopyFrom(claim.fence)
    if damage == "claim-operation":
        claim.intent.operation_id = "media-process-" + "b" * 32
    elif damage == "claim-kind":
        claim.intent.kind = 1
    elif damage == "plan-operation":
        plan.operation_id = "media-process-" + "b" * 32
    elif damage == "outer-fence":
        plan.original_fence.execution_epoch = 2
    elif damage == "inner-fence":
        plan.processed.original_fence.execution_epoch = 2
    elif damage == "request":
        plan.request_sha256 = bytes([8]) * 32
    elif damage == "intent-digest":
        claim.request_sha256 = plan.request_sha256 = bytes([8]) * 32
    elif damage in {"before", "after"}:
        setattr(plan, damage + "_sha256", bytes([8]) * 32)
    elif damage == "missing-process":
        plan.ClearField("processed")
    elif damage == "inner-digest-size":
        plan.processed.preparation_sha256 = b"x" * 31
    elif damage == "signature":
        plan.signature = b"x" * 64
    elif damage == "private-bytes":
        path.write_bytes(private_bytes + b"changed")
    elif damage == "private-missing":
        path.unlink()
    elif damage == "duplicate-private":
        other = tmp_path / "other-staging" / operation / "process.pb"
        other.parent.mkdir(parents=True)
        other.write_bytes(private_bytes)
    if damage in {"claim-operation", "claim-kind"}:
        claim.request_sha256 = hashlib.sha256(claim.intent.SerializeToString(deterministic=True)).digest()
        plan.request_sha256 = claim.request_sha256
    raw_plan = plan.SerializeToString(deterministic=True)
    if damage == "invalid-planned-protobuf":
        raw_plan += b"\x80"
    outer = hashlib.sha256(raw_plan).digest() if damage != "outer-digest" else b"x" * 32
    db_path = tmp_path / "worker-journal" / "media-effects.sqlite3"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA application_id=1146309968")
        db.execute("CREATE TABLE media_operations(operation_id,state,claim,preparation_sha256,before_bytes,after_bytes,planned,outcome)")
        db.execute("INSERT INTO media_operations VALUES(?,?,?,?,?,?,?,NULL)",
                   (operation, "APPLYING", claim.SerializeToString(deterministic=True), outer, b"before", b"after", raw_plan))
    return path, private_bytes, db_path.read_bytes()


def test_process_journal_binds_the_inner_private_file_hash_not_the_outer_plan_hash(tmp_path: Path) -> None:
    path, private, original_db = prepared_process_profile(tmp_path)
    observed = probe.process_journal_snapshot(tmp_path)
    assert observed is not None
    assert observed["processPreparationSha256"] == hashlib.sha256(private).hexdigest()
    assert observed["preparationSha256"] == observed["plannedSha256"] != observed["processPreparationSha256"]
    assert path.read_bytes() == private
    assert (tmp_path / "worker-journal/media-effects.sqlite3").read_bytes() == original_db


@pytest.mark.parametrize("damage", ["claim-operation", "claim-kind", "plan-operation", "outer-fence", "inner-fence", "request",
                                    "intent-digest", "before", "after", "missing-process", "inner-digest-size", "signature", "private-bytes",
                                    "private-missing", "duplicate-private", "outer-digest", "invalid-planned-protobuf"])
def test_process_journal_rejects_rebound_missing_or_corrupt_private_preparation(tmp_path: Path, damage: str) -> None:
    prepared_process_profile(tmp_path, damage)
    with pytest.raises(probe.create.patch.ProbeFailure):
        probe.process_journal_snapshot(tmp_path)


def test_process_recovery_cannot_change_the_private_preparation_binding() -> None:
    before_go, before_rust, after_go, after_rust = settled()
    after_rust["processPreparationSha256"] = "d" * 64
    with pytest.raises(probe.create.patch.ProbeFailure, match="ORIGINAL_REBOUND"):
        probe.require_recovered(before_go, before_rust, after_go, after_rust, 7, 8)


@pytest.mark.parametrize("damage", ["missing-before", "missing-after", "missing-both", "invalid-both", "bool-both"])
def test_process_recovery_requires_an_observed_valid_private_preparation(damage: str) -> None:
    before_go, before_rust, after_go, after_rust = settled()
    if damage.startswith("missing-"):
        for row in ([before_rust] if damage == "missing-before" else [after_rust] if damage == "missing-after" else [before_rust, after_rust]):
            row.pop("processPreparationSha256")
    else:
        for row in [before_rust, after_rust]:
            row["processPreparationSha256"] = True if damage == "bool-both" else "not-a-digest"
    with pytest.raises(probe.create.patch.ProbeFailure):
        probe.require_recovered(before_go, before_rust, after_go, after_rust, 7, 8)


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
