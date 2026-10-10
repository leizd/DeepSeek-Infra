from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_recovery_job, backup_recovery_state, backup_remote_restore, backups


def _session(tmp_path: Path, *, phase: str = "fetching-selected-components") -> dict[str, object]:
    digest = "a" * 64
    return {
        "restoreId": "restore-job",
        "phase": phase,
        "chain": [
            {
                "requiredComponents": [
                    {
                        "componentId": "p0000",
                        "objectDigest": digest,
                        "expectedBytes": 10,
                        "remoteETag": "etag",
                        "remoteVersionId": None,
                        "ciphertextPath": str(tmp_path / "partial.age"),
                    }
                ]
            }
        ],
    }


def test_pause_intent_converges_and_resume_revalidates_partial(tmp_path: Path) -> None:
    session = _session(tmp_path)
    chain = session["chain"]
    assert isinstance(chain, list) and isinstance(chain[0], dict)
    required = chain[0]["requiredComponents"]
    assert isinstance(required, list) and isinstance(required[0], dict)
    component = required[0]
    Path(component["ciphertextPath"]).write_bytes(b"1234")
    session["componentStates"] = {
        "a" * 64: {
            "state": "partial",
            "downloadedBytes": 4,
            "expectedBytes": 10,
            "remoteETag": "etag",
            "remoteVersionId": None,
        }
    }

    backup_recovery_job.request_pause(session)
    assert backup_recovery_job.converge(session) == "paused"
    assert session["pausedFromPhase"] == "fetching-selected-components"
    Path(component["ciphertextPath"]).write_bytes(b"bad")

    resumed = backup_recovery_job.resume(session)

    assert resumed == "fetching-selected-components"
    assert "pauseRequested" not in session
    assert backup_recovery_state.ensure_component_states(session)["a" * 64]["state"] == "queued"


def test_abort_before_transaction_cleans_scratch_and_marks_aborted(tmp_path: Path) -> None:
    session = _session(tmp_path)
    scratch = tmp_path / "payload-decrypted-a.zip"
    scratch.write_bytes(b"plaintext")
    session["scratchRoot"] = str(tmp_path)
    released: list[str] = []

    backup_recovery_job.request_abort(session)
    phase = backup_recovery_job.converge(session, release=lambda: released.append("released"))

    assert phase == "aborted"
    assert released == ["released"]
    assert not scratch.exists()


def test_abort_prepared_rolls_back_but_uncertain_commit_requires_recovery(tmp_path: Path) -> None:
    transaction = tmp_path / "transaction.json"
    for transaction_phase, expected, should_abort in (
        ("backend-staged", "rolled-back", True),
        ("commit-intent", "recovery-required", False),
        ("backend-committed", "recovery-required", False),
    ):
        transaction.write_text(json.dumps({"phase": transaction_phase}), encoding="utf-8")
        session = _session(tmp_path, phase="prepared" if should_abort else "committing")
        session["transactionPath"] = str(transaction)
        aborted: list[str] = []
        released: list[str] = []
        backup_recovery_job.request_abort(session)

        phase = backup_recovery_job.converge(
            session,
            abort_prepared=lambda: aborted.append("abort"),
            release=lambda: released.append("released"),
        )

        assert phase == expected
        assert aborted == (["abort"] if should_abort else [])
        assert released == (["released"] if should_abort else [])

    paused_uncertain = _session(tmp_path, phase="aborting")
    paused_uncertain["abortFromPhase"] = "committing"
    backup_recovery_job.request_abort(paused_uncertain)
    assert backup_recovery_job.converge(paused_uncertain) == "recovery-required"


def test_terminal_and_invalid_job_control_requests_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(AppError, match="[Tt]erminal"):
        backup_recovery_job.request_pause(_session(tmp_path, phase="complete"))
    with pytest.raises(AppError, match="not paused"):
        backup_recovery_job.resume(_session(tmp_path))


def test_control_generation_prevents_stale_worker_phase_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore-generation"
    restore_root = tmp_path / restore_id
    monkeypatch.setattr(backups, "RESTORE_DIR", tmp_path)
    restore_root.mkdir()
    session_path = restore_root / "remote-fetch.json"
    active = {"restoreId": restore_id, "phase": "fetching", "controlGeneration": 0}
    backup_remote_restore._atomic_write_json(session_path, active)

    stale_active = dict(active)
    assert backup_remote_restore.request_restore_pause(restore_id)["phase"] == "paused"
    backup_remote_restore._atomic_write_json(session_path, stale_active)
    paused = backup_remote_restore.read_restore_session(restore_id)
    assert paused is not None
    assert paused["phase"] == "paused"
    assert paused["controlGeneration"] == 1

    stale_paused = dict(paused)
    assert backup_remote_restore.resume_restore_session(restore_id)["phase"] == "fetching"
    backup_remote_restore._atomic_write_json(session_path, stale_paused)
    resumed = backup_remote_restore.read_restore_session(restore_id)
    assert resumed is not None
    assert resumed["phase"] == "fetching"
    assert resumed["controlGeneration"] == 2
    assert "pauseRequested" not in resumed


def test_concurrent_pause_and_abort_serialize_to_safe_terminal_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    restore_id = "restore-concurrent-control"
    restore_root = tmp_path / restore_id
    monkeypatch.setattr(backups, "RESTORE_DIR", tmp_path)
    restore_root.mkdir()
    (restore_root / "remote-fetch.json").write_text(
        json.dumps({"restoreId": restore_id, "phase": "fetching", "controlGeneration": 0}),
        encoding="utf-8",
    )
    barrier = threading.Barrier(3)
    results: list[str] = []

    def invoke(operation: object) -> None:
        barrier.wait()
        assert callable(operation)
        results.append(str(operation(restore_id)["phase"]))

    pause = threading.Thread(target=invoke, args=(backup_remote_restore.request_restore_pause,))
    abort = threading.Thread(target=invoke, args=(backup_remote_restore.request_restore_abort,))
    pause.start()
    abort.start()
    barrier.wait()
    pause.join(timeout=5)
    abort.join(timeout=5)

    session = backup_remote_restore.read_restore_session(restore_id)
    assert session is not None
    assert session["phase"] == "aborted"
    assert session["controlGeneration"] in {1, 2}
    assert "aborted" in results


def test_job_stop_signal_and_abort_without_prepared_handler_fail_closed(tmp_path: Path) -> None:
    stopped = backup_recovery_job.RecoveryJobStopped("paused")
    assert stopped.phase == "paused"
    assert "paused" in str(stopped)

    transaction = tmp_path / "transaction.json"
    transaction.write_text(json.dumps({"phase": "prepared"}), encoding="utf-8")
    session = _session(tmp_path, phase="prepared")
    session["transactionPath"] = str(transaction)
    backup_recovery_job.request_abort(session)
    assert backup_recovery_job.converge(session) == "recovery-required"


def test_job_handles_invalid_transaction_and_repeated_pause(tmp_path: Path) -> None:
    transaction = tmp_path / "transaction.json"
    transaction.write_text("not-json", encoding="utf-8")
    session = _session(tmp_path)
    session["transactionPath"] = str(transaction)
    backup_recovery_job.request_abort(session)
    assert backup_recovery_job.converge(session) == "recovery-required"

    paused = _session(tmp_path, phase="paused")
    paused["pauseRequested"] = True
    assert backup_recovery_job.converge(paused) == "paused"
    with pytest.raises(AppError, match="Terminal"):
        backup_recovery_job.request_abort(_session(tmp_path, phase="failed"))


def test_remote_job_control_missing_terminal_and_invalid_metadata_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backups, "RESTORE_DIR", tmp_path)
    for operation in (
        backup_remote_restore.request_restore_pause,
        backup_remote_restore.resume_restore_session,
        backup_remote_restore.request_restore_abort,
    ):
        with pytest.raises(AppError, match="session not found"):
            operation("restore_missing")

    restore_id = "restore_terminal"
    session_path = tmp_path / restore_id / "remote-fetch.json"
    session_path.parent.mkdir()
    session_path.write_text("not-json", encoding="utf-8")
    backup_remote_restore._atomic_write_json(session_path, {"restoreId": restore_id, "phase": "complete"})

    assert backup_remote_restore._manifest_work({}) == (0, 0)
    assert backup_remote_restore.request_restore_pause(restore_id)["phase"] == "complete"
    assert backup_remote_restore.resume_restore_session(restore_id)["phase"] == "complete"
    assert backup_remote_restore.request_restore_abort(restore_id)["phase"] == "complete"


def test_remote_job_hold_failure_and_controlled_checkpoint_are_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backups, "RESTORE_DIR", tmp_path)
    restore_id = "restore_holdfailure"
    (tmp_path / restore_id).mkdir()
    session: dict[str, object] = {"restoreId": restore_id, "phase": "fetching", "controlGeneration": 0}
    monkeypatch.setattr(
        backup_remote_restore.backup_recovery_lease,
        "renew_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AppError("renew failed")),
    )
    with pytest.raises(AppError, match="renew failed"):
        backup_remote_restore._renew_session_holds(object(), session)
    persisted = backup_remote_restore.read_restore_session(restore_id)
    assert persisted is not None
    assert persisted["recoveryTelemetry"]["counters"]["holdRenewalFailure"] == 1

    latest = {
        "restoreId": restore_id,
        "phase": "paused",
        "controlGeneration": 2,
        "pauseRequested": True,
        "pausedFromPhase": "fetching",
    }
    monkeypatch.setattr(backup_remote_restore, "read_restore_session", lambda _restore_id: latest)
    monkeypatch.setattr(backup_remote_restore, "_converge_job_control", lambda current: str(current["phase"]))
    renewals: list[str] = []
    monkeypatch.setattr(
        backup_remote_restore,
        "_renew_session_holds",
        lambda _store, current: renewals.append(str(current["phase"])),
    )

    with pytest.raises(backup_recovery_job.RecoveryJobStopped) as stopped:
        backup_remote_restore._checkpoint_job_control(session, object())
    assert stopped.value.phase == "paused"
    assert session["controlGeneration"] == 2
    assert session["pausedFromPhase"] == "fetching"
    assert renewals == ["paused"]
