from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("native_media_recovery_probe", ROOT / "scripts/verify_native_media_recovery.py")
assert spec is not None and spec.loader is not None
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def journal(tmp_path: Path, application_id: int = 0x44534D50) -> Path:
    path = tmp_path / "media-effects.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA application_id={application_id}")
        db.execute("CREATE TABLE media_operations(operation_id,state,claim,preparation_sha256,before_bytes,after_bytes,planned,outcome)")
        db.execute("INSERT INTO media_operations VALUES(?,?,?,?,?,?,?,?)",
                   ("media-patch-" + "a" * 32, "APPLYING", b"typed claim", b"7" * 32, b"before", b"after", b"planned", None))
    return path


def test_recovery_observer_reads_the_original_preterminal_row_without_changing_it(tmp_path: Path) -> None:
    path = journal(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = probe.journal_snapshot(tmp_path)
    assert result["state"] == "APPLYING" and result["rowCount"] == 1
    assert result["afterSha256"] == hashlib.sha256(b"after").hexdigest()
    assert result["outcomeSha256"] is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_delete_recovery_journal_requires_its_explicit_original_mutation(tmp_path: Path) -> None:
    path = journal(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE media_operations SET operation_id=?", ("media-delete-" + "a" * 32,))
    before = path.read_bytes()
    row = probe.journal_snapshot(tmp_path, mutation="delete")
    assert row is not None and row["operationId"] == "media-delete-" + "a" * 32
    with pytest.raises(probe.patch.ProbeFailure, match="MEDIA_OPERATION_ID_INVALID"):
        probe.journal_snapshot(tmp_path)
    with pytest.raises(probe.patch.ProbeFailure, match="MUTATION_SELECTOR_INVALID"):
        probe.journal_snapshot(tmp_path, mutation="foreign")
    assert path.read_bytes() == before


def test_create_recovery_journal_reads_only_the_original_create(tmp_path: Path) -> None:
    path = journal(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE media_operations SET operation_id=?", ("media-create-" + "a" * 32,))
    before = path.read_bytes()
    row = probe.journal_snapshot(tmp_path, mutation="create")
    assert row is not None and row["operationId"] == "media-create-" + "a" * 32
    for other in ("patch", "delete"):
        with pytest.raises(probe.patch.ProbeFailure, match="MEDIA_OPERATION_ID_INVALID"):
            probe.journal_snapshot(tmp_path, mutation=other)
    assert path.read_bytes() == before


def test_creation_source_fault_path_accepts_only_the_bound_future_source_without_creating_it(tmp_path: Path) -> None:
    record = creation_record()
    assert probe.creation_source_path(tmp_path, record) == (tmp_path / ".media" / str(record["path"])).resolve()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("damage", ["foreign-id", "traversal", "absolute", "different-id", "extension", "non-string"])
def test_creation_source_fault_path_rejects_a_rebound_or_unowned_source(tmp_path: Path, damage: str) -> None:
    record = creation_record()
    if damage == "foreign-id":
        record["mediaId"] = "media_other"
    elif damage == "traversal":
        record["path"] = "objects/../elsewhere/source.html"
    elif damage == "absolute":
        record["path"] = (tmp_path / "outside/source.html").as_posix()
    elif damage == "different-id":
        record["path"] = "objects/media_aaaaaaaaaaaaaaaa/source.html"
    elif damage == "extension":
        record["path"] = "objects/media_0123456789abcdef/payload.pb"
    else:
        record["path"] = False
    with pytest.raises(probe.patch.ProbeFailure, match="MEDIA_CREATE_SOURCE"):
        probe.creation_source_path(tmp_path, record)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_creation_source_fault_filter_is_narrow_and_never_creates_the_target(tmp_path: Path, kind: str) -> None:
    record = creation_record()
    source = tmp_path / ".media" / str(record["path"])
    if kind == "file":
        source.parent.mkdir(parents=True)
        source.write_bytes(b"retained-source")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    command = probe.creation_source_fault_command(Path("strace"), tmp_path, record, tmp_path / "trace.log", kind)
    assert command[command.index("-P") + 1] == str((source if kind == "file" else source.parent).resolve())
    assert "trace=fsync,fdatasync" in command and "inject=fsync,fdatasync:error=EIO" in command
    assert "retval=" not in " ".join(command) and "delay_exit" not in " ".join(command)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
    with pytest.raises(probe.patch.ProbeFailure, match="FAULT_KIND_INVALID"):
        probe.creation_source_fault_command(Path("strace"), tmp_path, record, tmp_path / "trace.log", "all")


def test_creation_source_file_fault_refuses_an_absent_or_changed_retained_source(tmp_path: Path) -> None:
    record = creation_record()
    with pytest.raises(probe.patch.ProbeFailure):
        probe.creation_source_fault_command(Path("strace"), tmp_path, record, tmp_path / "trace.log", "file")
    source = tmp_path / ".media" / str(record["path"])
    source.parent.mkdir(parents=True)
    raw = b"retained-source"
    source.write_bytes(raw)
    before = source.stat()
    witness = probe.creation_source_witness(tmp_path, record, raw)
    assert witness == dict(sha256=hashlib.sha256(raw).hexdigest(), inode=before.st_ino, mtimeNs=before.st_mtime_ns)
    with pytest.raises(probe.patch.ProbeFailure, match="SOURCE_BYTES_DIFFER"):
        probe.creation_source_witness(tmp_path, record, b"different-source")
    assert source.read_bytes() == raw and source.stat().st_mtime_ns == before.st_mtime_ns


def test_creation_source_fault_path_never_follows_a_parent_link(tmp_path: Path) -> None:
    root, external = tmp_path / "profile", tmp_path / "external"
    root.mkdir()
    external.mkdir()
    try:
        (root / ".media").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create the controlled directory symlink")
    with pytest.raises(probe.patch.ProbeFailure, match="MEDIA_CREATE_SOURCE"):
        probe.creation_source_path(root, creation_record())
    assert list(external.iterdir()) == []


def test_creation_source_sync_workflow_retains_the_actual_native_and_original_crash_cases() -> None:
    import yaml

    job = yaml.safe_load((ROOT / ".github/workflows/native-media-recovery.yml").read_bytes())["jobs"]["media-recovery"]
    steps = job["steps"]
    source = [s for s in steps if "--recovery-source-sync-faults" in s.get("run", "")]
    assert len(source) == 1
    assert "--mutation create" in source[0]["run"] and "--sync-strace /usr/bin/strace" in source[0]["run"]
    assert "--strace \"$RUNNER_TEMP/native-media-recovery-strace\"" in source[0]["run"]
    assert source[0]["if"] == "always() && steps.build.outcome == 'success'"
    original_create = [s for s in steps if "--mutation create" in s.get("run", "") and "--recovery-source-sync-faults" not in s["run"]]
    assert len(original_create) == 1
    assert any("--recovery-sync-fault file" in s.get("run", "") for s in steps)
    assert any("--recovery-sync-fault directory" in s.get("run", "") for s in steps)


@pytest.mark.parametrize("mutation,library_fault", [("patch", "none"), ("delete", "none"), ("create", "file"), ("create", "directory")])
def test_creation_source_fault_selection_fails_before_build_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str, library_fault: str,
) -> None:
    tracer = tmp_path / "not-executed-tracer"
    tracer.write_bytes(b"offline-selection-test")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SHA", "1" * 40)

    def read_metadata(command: list[str], **_kwargs: object) -> bytes | str:
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "1" * 40 + "\n"
        if command[:2] == ["git", "status"]:
            return b""
        if command == [str(tracer), "--version"]:
            return "strace -- version 6.8\n"
        raise AssertionError("invalid source-fault selection must precede every build and launch")

    monkeypatch.setattr(probe.subprocess, "check_output", read_metadata)
    args = argparse.Namespace(strace=tracer, recovery_source_sync_faults=True, mutation=mutation, recovery_sync_fault=library_fault)
    with pytest.raises(probe.patch.ProbeFailure, match="MEDIA_CREATE_SOURCE_SYNC_SELECTION_INVALID"):
        probe.run(args, {})
    assert list(tmp_path.iterdir()) == [tracer]


def creation_record(media_id: str = "media_0123456789abcdef") -> dict[str, object]:
    return dict(mediaId=media_id, path=f"objects/{media_id}/source.html", createdAt="2026-10-30T01:00:00Z",
                updatedAt="2026-10-30T01:00:00Z", type="webpage", title="Original", metadata={"zero": 0})


def test_create_recovery_identifies_one_new_record_and_keeps_every_prior_record() -> None:
    baseline = [{"mediaId": "retained-old", "metadata": {"enabled": False}}]
    actual = creation_record()
    expected = creation_record("media_abcdef0123456789")
    raw = json.dumps({"media": [*baseline, actual]}).encode()
    assert probe.retained_created_record(raw, baseline, expected) == actual


def test_create_recovery_preserves_duplicate_original_ids_and_record_order() -> None:
    baseline = [{"mediaId": "duplicate", "title": "first"}, {"mediaId": "duplicate", "title": "second"}]
    actual, expected = creation_record(), creation_record("media_abcdef0123456789")
    assert probe.retained_created_record(json.dumps({"media": [*baseline, actual]}).encode(), baseline, expected) == actual
    for changed in [list(reversed(baseline)), [baseline[0], baseline[0]]]:
        with pytest.raises(probe.patch.ProbeFailure, match="MEDIA_CREATE_RECOVERY_RETAINED_RECORDS_DIFFER"):
            probe.retained_created_record(json.dumps({"media": [*changed, actual]}).encode(), baseline, expected)


def test_create_recovery_baseline_uses_the_real_original_route_normalization(tmp_path: Path) -> None:
    fixture = json.loads(probe.patch.FIXTURE.read_text(encoding="utf8"))
    profile = tmp_path / "original-profile"
    reference, retained = probe.original_creation_fixture(profile, tmp_path / "oracle.log", fixture["media"])
    rows = json.loads((profile / ".media/library.json").read_bytes())["media"]
    assert len(fixture["media"]) == 18 and len(retained) == 10 and len(rows) == 11
    assert probe.creation.same_json(retained, [row for row in rows if row["mediaId"] != reference["body"]["media"]["mediaId"]])
    actual = json.loads(json.dumps(reference["body"]["media"]))
    actual["mediaId"] = "media_0123456789abcdef"
    actual["path"] = "objects/media_0123456789abcdef/source.html"
    published = json.dumps({"media": [*retained, actual]}).encode()
    assert probe.retained_created_record(published, retained, reference["body"]["media"]) == actual
    with pytest.raises(probe.patch.ProbeFailure, match="MEDIA_CREATE_RECOVERY_RECORD_COUNT_DIFFERS"):
        probe.retained_created_record(published, fixture["media"], reference["body"]["media"])


@pytest.mark.parametrize("damage", ["no-new-record", "two-new-records", "changed-retained-record", "changed-scalar-type"])
def test_create_recovery_rejects_a_missing_ambiguous_or_changed_publication(damage: str) -> None:
    baseline = [{"mediaId": "retained-old", "metadata": {"enabled": False}}]
    actual, expected = creation_record(), creation_record("media_abcdef0123456789")
    rows: list[dict[str, object]] = [json.loads(json.dumps(baseline[0])), actual]
    if damage == "no-new-record":
        rows.pop()
    elif damage == "two-new-records":
        rows.append(creation_record("media_1111111111111111"))
    elif damage == "changed-retained-record":
        rows[0]["metadata"] = {"enabled": 0}
    else:
        actual["metadata"] = {"zero": False}
    with pytest.raises(probe.patch.ProbeFailure, match="MEDIA_CREATE_RECOVERY_"):
        probe.retained_created_record(json.dumps({"media": rows}).encode(), baseline, expected)


def test_recovery_observer_rejects_foreign_journals_and_multiple_originals(tmp_path: Path) -> None:
    path = journal(tmp_path, 123)
    with pytest.raises(probe.patch.ProbeFailure, match="JOURNAL_ID_DIFFERS"):
        probe.journal_snapshot(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA application_id=0x44534D50")
        db.execute("INSERT INTO media_operations SELECT * FROM media_operations")
    with pytest.raises(probe.patch.ProbeFailure, match="ORIGINAL_RUST_ROW_REQUIRED"):
        probe.journal_snapshot(tmp_path)


def test_recovery_observer_refuses_external_files_and_write_connections(tmp_path: Path) -> None:
    directory = tmp_path / "owned"
    directory.mkdir()
    path = journal(directory)
    outside = tmp_path / "external.sqlite3"
    outside.write_bytes(path.read_bytes())
    with pytest.raises(probe.patch.ProbeFailure, match="PATH_ESCAPED"):
        probe.owned_file(outside, directory)
    db = probe.readonly(path, directory)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM media_operations")
    finally:
        db.close()


@pytest.mark.parametrize("key", probe.IMMUTABLE_GO)
def test_recovery_cannot_rebind_any_original_control_identity_field(key: str) -> None:
    original: dict[str, object] = {name: index for index, name in enumerate(probe.IMMUTABLE_GO)}
    retained = dict(original, state="APPLIED")
    assert probe.same_original(original, retained, probe.IMMUTABLE_GO)
    retained[key] = -100
    assert not probe.same_original(original, retained, probe.IMMUTABLE_GO)
    del retained[key]
    assert not probe.same_original(original, retained, probe.IMMUTABLE_GO)


def test_recovery_identity_keeps_epoch_types_and_original_rust_witnesses() -> None:
    assert not probe.same_original({"epoch": 1}, {"epoch": True}, ("epoch",))
    original = {name: "original" for name in probe.IMMUTABLE_RUST}
    for key in probe.IMMUTABLE_RUST:
        changed = dict(original, state="APPLIED")
        changed[key] = "replacement"
        assert not probe.same_original(original, changed, probe.IMMUTABLE_RUST)


def test_trace_arguments_hold_real_post_rename_file_sync_exit(tmp_path: Path) -> None:
    target = tmp_path / "library.json"
    target.write_bytes(b"original")
    args = probe.trace_command(Path("/usr/bin/strace"), target, tmp_path, tmp_path / "trace.log")
    assert args[args.index("-P") + 1] == str(target.resolve())
    injection = next(value for value in args if value.startswith("inject="))
    assert injection == "inject=fsync,fdatasync:delay_exit=20s"
    assert "retval=" not in injection and "error=" not in injection
    assert "trace=fsync,fdatasync" in args


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_sync_fault_targets_only_the_retained_publication(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "library.json"
    target.write_bytes(b"original")
    args = probe.sync_fault_command(Path("/usr/bin/strace"), target, tmp_path, tmp_path / "trace.log", kind)
    expected = target.resolve() if kind == "file" else tmp_path.resolve()
    assert args[args.index("-P") + 1] == str(expected)
    assert "inject=fsync,fdatasync:error=EIO" in args
    assert "delay_exit" not in " ".join(args)
    with pytest.raises(probe.patch.ProbeFailure, match="FAULT_KIND_INVALID"):
        probe.sync_fault_command(Path("strace"), target, tmp_path, tmp_path / "trace.log", "volume")


def test_sync_failure_cannot_accept_a_terminal_or_rebound_operation() -> None:
    go: dict[str, object] = {name: index for index, name in enumerate(probe.IMMUTABLE_GO)}
    rust: dict[str, object] = {name: index for index, name in enumerate(probe.IMMUTABLE_RUST)}
    go.update(state="DISPATCHED", outcomeSha256=None)
    rust.update(state="APPLYING", outcomeSha256=None)
    probe.require_sync_failure_retains_original(go, rust, dict(go), dict(rust))
    with pytest.raises(probe.patch.ProbeFailure, match="WITHOUT_SYNC_BARRIER"):
        probe.require_sync_failure_retains_original(go, rust, dict(go), dict(rust, state="APPLIED"))
    with pytest.raises(probe.patch.ProbeFailure, match="WITHOUT_SYNC_BARRIER"):
        probe.require_sync_failure_retains_original(go, rust, dict(go, outcomeSha256="signed"), dict(rust))
    with pytest.raises(probe.patch.ProbeFailure, match="REBOUND_ORIGINAL"):
        probe.require_sync_failure_retains_original(go, rust, dict(go, epoch=99), dict(rust))


def test_recovery_requires_its_own_complete_generation_scope() -> None:
    probe.require_recovery_generation({"value": 1}, {"present": True, "value": 3})
    for wrong in (1, 2, 4, "3", True):
        with pytest.raises(probe.patch.ProbeFailure, match="GENERATION_NOT_FENCED"):
            probe.require_recovery_generation({"value": 1}, {"present": True, "value": wrong})
    with pytest.raises(probe.patch.ProbeFailure, match="GENERATION_NOT_FENCED"):
        probe.require_recovery_generation({"value": 1}, {"present": False, "value": 3})


def test_probe_refuses_local_native_execution_before_any_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(probe.patch.ProbeFailure, match="CLOUD_LINUX_ONLY"):
        probe.run(argparse.Namespace(), {})


def test_signal_refuses_reused_pids_before_touching_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(probe.patch.reads, "process_identity", lambda _pid: "replacement-start-time")

    def forbidden_signal(_pid: int, _signum: int) -> None:
        raise AssertionError("an unowned/reused process was signalled")

    monkeypatch.setattr(probe.os, "kill", forbidden_signal)
    with pytest.raises(probe.patch.ProbeFailure, match="PID_IDENTITY_CHANGED"):
        probe.signal_owned({"pid": 12345, "startTicks": "original-start-time"}, 9)


def test_cleanup_failure_cannot_leave_a_passing_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    out = tmp_path / "artifacts" / "result"
    monkeypatch.setattr(sys, "argv", ["probe", "--worker", "worker", "--edge", "edge", "--go-cache", "cache", "--out", str(out)])

    def fail_after_success(_args: argparse.Namespace, report: dict[str, object]) -> None:
        report["status"] = "PASS"
        raise probe.patch.ProbeFailure("CLEANUP_FAILED")

    monkeypatch.setattr(probe, "run", fail_after_success)
    assert probe.main() == 1
    result = json.loads((out / "result.json").read_text())
    assert result["status"] == "FAIL" and result["failureCode"] == "CLEANUP_FAILED"
    assert not result["fullMigrationQualified"]


def test_recovery_checks_the_retained_publication_even_before_settlement() -> None:
    before = {"sha256": "original", "inode": 17, "mtimeNs": 23}
    probe.require_same_publication(before, dict(before))
    for key in before:
        changed = dict(before)
        changed[key] = "replacement"
        with pytest.raises(probe.patch.ProbeFailure, match="PUBLICATION_WITNESS_CHANGED"):
            probe.require_same_publication(before, changed)


def test_standalone_recovery_prepares_the_pinned_go_before_the_process_probe() -> None:
    import yaml

    flow = yaml.safe_load((ROOT / ".github/workflows/native-media-recovery.yml").read_bytes())
    job = flow["jobs"]["media-recovery"]
    assert "strategy" not in job
    steps = job["steps"]
    names = [step.get("name", "") for step in steps]
    install = names.index("Install the repository's checksum-pinned Go for the real signing producer")
    modules = names.index("Verify Go modules before the isolated process probe")
    probe_step = names.index("Crash after actual media replacement and require autonomous original reconciliation")
    assert install < modules < probe_step
    for index in (install, modules):
        assert "if" not in steps[index], "standalone Go preparation must not depend on a missing matrix"
        assert probe.patch.signing.GO_VERSION in steps[index]["run"]


def test_recovery_tracer_capability_is_scoped_and_always_removed() -> None:
    import yaml

    steps = yaml.safe_load((ROOT / ".github/workflows/native-media-recovery.yml").read_bytes())["jobs"]["media-recovery"]["steps"]
    names = [step.get("name", "") for step in steps]
    grant = names.index("Permit only the temporary official tracer to attach")
    crash = names.index("Crash after actual media replacement and require autonomous original reconciliation")
    revoke = names.index("Remove the temporary tracer capability")
    reports = names.index("Preserve focused recovery reports only")
    assert grant < crash < revoke < reports
    assert steps[revoke]["if"] == "always()"
    assert '--strace "$RUNNER_TEMP/native-media-recovery-strace"' in steps[crash]["run"]
    assert 'install --mode=0755 /usr/bin/strace "$tracer"' in steps[grant]["run"]
    assert 'setcap cap_sys_ptrace=ep "$tracer"' in steps[grant]["run"]
    assert 'setcap -r "$tracer"' in steps[revoke]["run"]


def test_go_report_manifest_includes_every_selected_original_and_recovery_test() -> None:
    import ast
    import re
    import yaml

    job = yaml.safe_load((ROOT / ".github/workflows/native-media-development.yml").read_bytes())["jobs"]["media-go"]
    command = next(step["run"] for step in job["steps"] if step.get("name") == "Run media admission, proof, HTTP and transport regressions")
    report_code = command.split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    declared: set[str] = set()
    for node in ast.walk(ast.parse(report_code)):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith("required_"):
            declared.update(ast.literal_eval(node.value))
    expected: set[str] = set()
    for package in ["media", "store", "api", "worker"]:
        for path in (ROOT / "go/internal" / package).glob("*_test.go"):
            expected.update(name for name in re.findall(r"^func (Test\w*)\(", path.read_text(encoding="utf-8"), re.M) if re.fullmatch(job["env"]["MEDIA_GO_TEST_PATTERN"], name))
    assert declared == expected
