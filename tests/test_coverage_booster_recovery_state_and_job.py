"""Targeted tests for backup_recovery_job and backup_recovery_state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_recovery_job,
    backup_recovery_state,
)


def test_recovery_job_pause_and_resume(tmp_settings: Path) -> None:
    # 1. Terminal job cannot be paused or aborted
    term_session = {"phase": "complete"}
    with pytest.raises(AppError) as exc_info:
        backup_recovery_job.request_pause(term_session)
    assert "Terminal recovery job cannot be paused" in str(exc_info.value)

    with pytest.raises(AppError) as exc_info2:
        backup_recovery_job.request_abort(term_session)
    assert "Terminal recovery job cannot be aborted" in str(exc_info2.value)

    # 2. Pause active session
    active_session = {"phase": "fetching"}
    backup_recovery_job.request_pause(active_session)
    assert active_session["pauseRequested"] is True
    res_phase = backup_recovery_job.converge(active_session)
    assert res_phase == "paused"
    assert active_session["phase"] == "paused"
    assert active_session["pausedFromPhase"] == "fetching"

    # 3. Resume paused session
    resumed = backup_recovery_job.resume(active_session)
    assert resumed == "fetching"
    assert active_session["phase"] == "fetching"

    # 4. Resume non-paused session fails
    with pytest.raises(AppError):
        backup_recovery_job.resume(active_session)


def test_recovery_job_abort_paths(tmp_settings: Path) -> None:
    # 1. Abort with no transaction path
    s1 = {"phase": "fetching"}
    backup_recovery_job.request_abort(s1)
    released = False

    def on_release() -> None:
        nonlocal released
        released = True

    phase = backup_recovery_job.converge(s1, release=on_release)
    assert phase == "aborted"
    assert s1["phase"] == "aborted"
    assert released is True

    # 2. Abort with uncertain transaction phase
    tx_file = tmp_settings / "tx_uncertain.json"
    tx_file.write_text(json.dumps({"phase": "commit-intent"}), encoding="utf-8")
    s2 = {"phase": "fetching", "transactionPath": str(tx_file)}
    backup_recovery_job.request_abort(s2)
    p2 = backup_recovery_job.converge(s2)
    assert p2 == "recovery-required"

    # 3. Abort with prepared transaction and callback
    tx_prepared = tmp_settings / "tx_prepared.json"
    tx_prepared.write_text(json.dumps({"phase": "prepared"}), encoding="utf-8")
    s3 = {"phase": "fetching", "transactionPath": str(tx_prepared)}
    backup_recovery_job.request_abort(s3)
    aborted_prep = False

    def on_abort_prepared() -> None:
        nonlocal aborted_prep
        aborted_prep = True

    p3 = backup_recovery_job.converge(s3, abort_prepared=on_abort_prepared)
    assert p3 == "rolled-back"
    assert aborted_prep is True

    # 4. Abort with prepared transaction but NO callback
    s4 = {"phase": "fetching", "transactionPath": str(tx_prepared)}
    backup_recovery_job.request_abort(s4)
    p4 = backup_recovery_job.converge(s4)
    assert p4 == "recovery-required"


def test_recovery_job_session_lock(tmp_settings: Path) -> None:
    session_file = tmp_settings / "test_session" / "remote-fetch.json"
    with backup_recovery_job.session_lock(session_file):
        pass

    exc = backup_recovery_job.RecoveryJobStopped("paused")
    assert exc.phase == "paused"
    assert "Recovery job stopped in phase paused" in str(exc)


def test_recovery_state_component_states(tmp_settings: Path) -> None:
    c_file = tmp_settings / "cipher.bin"
    c_file.write_bytes(b"1234567890")

    d1 = "a" * 64
    d2 = "b" * 64

    session = {
        "chain": [
            {
                "backupId": "bk1",
                "requiredComponents": [
                    {
                        "componentId": "c1",
                        "objectDigest": d1,
                        "ciphertextPath": str(c_file),
                        "expectedBytes": 10,
                        "remoteETag": "etag1",
                        "remoteVersionId": "v1",
                        "priority": 1,
                    },
                    {
                        "componentId": "c2",
                        "objectDigest": d2,
                        "ciphertextPath": str(tmp_settings / "nonexistent.bin"),
                        "expectedBytes": 50,
                        "priority": 3,
                    },
                ],
            }
        ]
    }

    comps = backup_recovery_state.required_components(session)
    assert len(comps) == 2

    states = backup_recovery_state.ensure_component_states(session)
    assert d1 in states
    assert d2 in states
    assert states[d2]["state"] == "queued"
