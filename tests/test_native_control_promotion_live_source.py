"""A control-domain promotion driven against a genuinely stopped Python writer.

The `go_control_authority` and `rust_data_authority` gates have been outstanding
because every existing proof fences a database whose writer was already gone, or
never had a separate process at all. That is not the same claim as "a live Python
writer was stopped and then could not write", which is what a cutover actually
requires.

This test closes that gap for real:

1. A **separate Python process** opens the control database and writes policies in a
   loop, reporting each committed revision. It is a real writer: its writes are
   committed and visible.
2. The test confirms those writes landed and keeps counting them.
3. The process is killed with SIGKILL. Not asked to stop - killed, so nothing can run
   on its way out.
4. The test waits for the OS to reap it, then fences the source and exports it.
5. A **second** Python process is started after the fence and attempts the same
   writes. Every one must be refused by the database itself.

Step 5 is the part that matters and the part earlier tests skipped: it proves the
fence holds against a *new* writer, not just that the old one is absent. The refusal
is a SQLite trigger, so it does not depend on the writer cooperating or on anything
in this test process.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import backup_control, backup_control_authority
from deepseek_infra.infra.native_runtime.authority import PythonWriterMechanicallyDeniedError
from scripts.native_control_handoff import export_and_fence

# A writer that commits policies until it is killed. Deliberately small and free of
# imports from this test module so it runs as an independent process.
WRITER = r"""
import os, sqlite3, sys, time

database, ready, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
connection = sqlite3.connect(database, timeout=30, isolation_level=None)
connection.execute("PRAGMA busy_timeout=30000")
connection.execute("PRAGMA journal_mode=WAL")
committed = 0
with open(ready, "w", encoding="utf-8") as handle:
    handle.write("ready")
while committed < limit:
    policy = "writer-%04d" % committed
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO control_policies "
            "(policy_id, revision, payload_json, topology_generation, promotion_epoch, "
            " drain_generation, placement_generation, updated_at) "
            "VALUES (?, 1, ?, 1, 1, 0, 0, '2026-01-01T00:00:00Z')",
            (policy, '{"policyId": "%s", "policyRevision": 1, "enabled": true}' % policy),
        )
        connection.execute("COMMIT")
    except sqlite3.Error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    committed += 1
    print(committed, flush=True)
    time.sleep(0.01)
print("exhausted", flush=True)
"""

# A writer that runs only after the fence exists and reports what the database does
# with each write attempt.
POST_FENCE_WRITER = r"""
import json, sqlite3, sys

database, out = sys.argv[1], sys.argv[2]
results = []
connection = sqlite3.connect(database, timeout=30, isolation_level=None)
for kind in ("insert", "update", "delete"):
    try:
        connection.execute("BEGIN IMMEDIATE")
        if kind == "insert":
            connection.execute(
                "INSERT INTO control_policies "
                "(policy_id, revision, payload_json, topology_generation, promotion_epoch, "
                " drain_generation, placement_generation, updated_at) "
                "VALUES ('post-fence', 1, '{\"policyId\": \"post-fence\", \"policyRevision\": 1}', 1, 1, 0, 0, "
                " '2026-01-01T00:00:00Z')"
            )
        elif kind == "update":
            connection.execute("UPDATE control_policies SET revision = revision + 1")
        else:
            connection.execute("DELETE FROM control_policies")
        connection.execute("COMMIT")
        results.append([kind, "allowed", ""])
    except sqlite3.Error as error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        results.append([kind, "refused", str(error)])
with open(out, "w", encoding="utf-8") as handle:
    json.dump(results, handle)
"""


@pytest.fixture
def live_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / ".backup-control"
    root.mkdir()
    database = root / "control.sqlite3"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", database)
    monkeypatch.setenv("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
    monkeypatch.delenv("DEEPSEEK_GO_CONTROL", raising=False)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    # Create the schema and one policy so the store is a real, populated control DB
    # rather than an empty file the writer would have to initialize itself.
    backup_control.create_policy({"policyId": "seed", "policyRevision": 1, "enabled": True})
    return database, tmp_path


def _kill_hard(process: subprocess.Popen) -> None:
    """Kill the writer with no chance to run cleanup.

    POSIX gets SIGKILL, which cannot be caught, blocked or handled. Windows has no
    SIGKILL; ``terminate()`` maps to TerminateProcess, which also runs no handler in
    the target - the distinction that matters here is "no cleanup code executes",
    and both satisfy it. The test asserts the POSIX signal number only where that
    signal exists.
    """
    if hasattr(signal, "SIGKILL"):
        os.kill(process.pid, signal.SIGKILL)
    else:  # pragma: no cover - exercised on Windows
        process.terminate()


def _wait_for(path: Path, *, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path} never appeared")


def _count_policies(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT COUNT(*) FROM control_policies").fetchone()[0]


def test_live_python_writer_is_sigkilled_then_fenced_and_cannot_write_again(
    live_source: tuple[Path, Path],
) -> None:
    database, scratch = live_source
    ready = scratch / "ready"
    writer = subprocess.Popen(
        [sys.executable, "-c", WRITER, str(database), str(ready), "400"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert writer.stdout is not None, "the writer must be started with a pipe"
    try:
        _wait_for(ready)
        # Read committed revisions off the writer's own stdout, so the count is what
        # the writer observed, not what this process inferred from the file.
        committed = 0
        deadline = time.time() + 30
        while committed < 5 and time.time() < deadline:
            line = writer.stdout.readline().strip()
            if line.isdigit():
                committed = int(line)
        assert committed >= 5, f"the live writer committed only {committed} policies"
        before = _count_policies(database)
        assert before >= 5, before
    finally:
        # SIGKILL: no handler, no cleanup, nothing runs on the way out. This is the
        # only way to prove the writer is genuinely gone rather than politely paused.
        _kill_hard(writer)
        writer.wait(timeout=30)
        if writer.stdout:
            writer.stdout.close()
        if writer.stderr:
            writer.stderr.close()

    assert writer.returncode is not None
    # The kill really happened. POSIX reports the terminating signal as a negative
    # return code; Windows reports the TerminateProcess exit status instead.
    if hasattr(signal, "SIGKILL"):
        assert writer.returncode == -signal.SIGKILL, writer.returncode
    else:  # pragma: no cover - exercised on Windows
        assert writer.returncode != 0, writer.returncode

    # Fence and export the now-stopped source.
    document = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority.record_local_authority_head(document)
    checkpoint = scratch / "checkpoint.json"
    checkpoint.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    output = scratch / "export.json"
    manifest = export_and_fence(database, checkpoint, output, "policy", "transfer-live-1")
    assert manifest["schema"] == "python-control-inventory-export-v1"
    exported = {row["policy_id"] for row in manifest["rows"]}
    assert "seed" in exported
    # Everything the killed writer committed before dying is in the export.
    assert len(exported) == _count_policies(database)

    # A brand-new Python process now tries the same writes. The fence is a trigger in
    # the database, so it must refuse without this test's help.
    results_path = scratch / "post-fence.json"
    post = subprocess.run(
        [sys.executable, "-c", POST_FENCE_WRITER, str(database), str(results_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert post.returncode == 0, post.stderr
    results = json.loads(results_path.read_text(encoding="utf-8"))
    assert [row[0] for row in results] == ["insert", "update", "delete"]
    for kind, verdict, detail in results:
        assert verdict == "refused", f"{kind} was allowed after the fence: {detail}"
        assert "PYTHON_CONTROL_SOURCE_FENCED" in detail or "fenced" in detail.lower(), detail

    # And the in-process writer path is denied too, so the refusal is not merely a
    # property of the subprocess's connection.
    with pytest.raises(PythonWriterMechanicallyDeniedError, match="fenced"):
        backup_control.create_policy({"policyId": "after-kill", "policyRevision": 1})

    # Nothing the post-fence writer attempted took effect.
    assert _count_policies(database) == len(exported)


def test_the_fenced_export_still_reports_the_rows_the_killed_writer_committed(
    live_source: tuple[Path, Path],
) -> None:
    """The export is a recovery artifact, so it has to contain the last committed rows.

    Killing a writer mid-stream is exactly when a cutover is most likely to lose
    data. This asserts the export captures what survived the kill rather than a
    half-applied transaction.
    """
    database, scratch = live_source
    ready = scratch / "ready2"
    writer = subprocess.Popen(
        [sys.executable, "-c", WRITER, str(database), str(ready), "8"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert writer.stdout is not None, "the writer must be started with a pipe"
    try:
        _wait_for(ready)
        # Let it finish its small batch, then kill: the rows are committed either way
        # and the export must see all of them.
        for _ in range(8):
            writer.stdout.readline()
    finally:
        _kill_hard(writer)
        writer.wait(timeout=30)
        if writer.stdout:
            writer.stdout.close()
        if writer.stderr:
            writer.stderr.close()

    expected = _count_policies(database)
    document = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority.record_local_authority_head(document)
    checkpoint = scratch / "checkpoint2.json"
    checkpoint.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    manifest = export_and_fence(database, checkpoint, scratch / "export2.json", "policy", "transfer-live-2")
    assert len(manifest["rows"]) == expected
    revisions = {row["policy_id"]: row["revision"] for row in manifest["rows"]}
    assert revisions.get("seed") == 1
