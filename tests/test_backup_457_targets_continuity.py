"""Coverage tests for Backup Targets, Write Continuity, and Storage Backends (v4.5)."""

from __future__ import annotations

import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from typing import Any, cast

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_policies,
    backup_scheduler,
    backup_target_s3,
    backup_targets,
    backup_write_continuity,
    backup_writer_lease,
)


@pytest.fixture(autouse=True)
def _isolate_target_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_temp = tmp_path / "fake_temp"
    fake_temp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake_temp))


def test_backup_targets_extended_coverage(tmp_settings: Path) -> None:
    t_id = "target_ext_cov_1"
    t_path = tmp_settings / "ext_cov_root"
    backup_targets.register_filesystem_target(t_id, path=t_path, label="Extended Coverage Target")

    # 1. probe_target_capacity
    cap = backup_targets.probe_target_capacity(t_id)
    assert cap["targetId"] == t_id
    assert cap["totalBytes"] > 0
    assert cap["freeBytes"] > 0

    # 2. probe_target
    probe = backup_targets.probe_target(t_id)
    assert probe["targetId"] == t_id
    assert probe["ready"] is True
    assert probe["status"] == "ok"

    # 3. Drain state transitions
    assert backup_targets.get_target_drain_state(t_id) == "active"
    backup_targets.drain_target(t_id, reason="maintenance")
    assert backup_targets.get_target_drain_state(t_id) == "draining"
    backup_targets.mark_target_drained(t_id)
    assert backup_targets.get_target_drain_state(t_id) == "drained"
    backup_targets.activate_target(t_id)
    assert backup_targets.get_target_drain_state(t_id) == "active"

    # 4. adopt_target_incarnation
    adopted = backup_targets.adopt_target_incarnation(t_id)
    assert adopted["targetId"] == t_id

    # 5. delete_target
    del_res = backup_targets.delete_target(t_id)
    assert del_res["deleted"] is True

    # 6. reinitialize_target
    reinit = backup_targets.reinitialize_target(t_path, label="Reinitialized")
    assert "targetId" in reinit


def test_backup_targets_complete_lifecycle(tmp_settings: Path) -> None:
    # 1. init_target (new directory)
    t_root = tmp_settings / "tgt_lifecycle_1"
    t_root.mkdir(parents=True, exist_ok=True)
    rec1 = backup_targets.init_target(t_root, label="Lifecycle Target 1")
    assert rec1["targetId"].startswith("target_")
    t_id = rec1["targetId"]

    # 2. init_target (existing marker - re-registration)
    rec1_re = backup_targets.init_target(t_root, label="Lifecycle Target 1 Re")
    assert rec1_re["targetId"] == t_id

    # 3. probe_target on filesystem target
    probe_res = backup_targets.probe_target(t_id)
    assert probe_res["targetId"] == t_id
    assert probe_res["ready"] is True

    # 4. verify_target_ready
    ready_target = backup_targets.verify_target_ready(t_id)
    assert ready_target == t_root

    # 5. init_s3_target with mock store & client
    mock_s3_client = MagicMock()
    mock_store = MagicMock()
    mock_store.capabilities.return_value = SimpleNamespace(kind="s3")

    with patch("deepseek_infra.infra.workspace.backup_target_s3.open_s3_store", return_value=mock_store):
        with patch("deepseek_infra.infra.workspace.backup_target_store.read_json", return_value=None):
            with patch("deepseek_infra.infra.workspace.backup_target_store.put_json_if_absent", return_value=None):
                s3_rec = backup_targets.init_s3_target(
                    bucket="my-mock-bucket",
                    prefix="backups/",
                    client=mock_s3_client,
                    probe=False,
                )
                assert s3_rec["kind"] == "s3"
                assert s3_rec["bucket"] == "my-mock-bucket"

    # 6. delete_target
    backup_targets.delete_target(t_id)
    with pytest.raises(AppError):
        backup_targets.get_target(t_id)


def test_backup_write_continuity_governance_exhaustive(tmp_settings: Path) -> None:
    # 1. _parse_iso with invalid input returns None
    assert backup_write_continuity._parse_iso("invalid-date-string") is None
    assert backup_write_continuity._parse_iso(None) is None

    # 2. Setup policy continuity
    t1 = "target_wc_ex_1"
    t2 = "target_wc_ex_2"
    backup_targets.register_filesystem_target(t1, path=tmp_settings / "wc_ex_1", failure_domain="fd1")
    backup_targets.register_filesystem_target(t2, path=tmp_settings / "wc_ex_2", failure_domain="fd2")

    pol = {
        "policyId": "pol-wc-ex",
        "name": "Continuity Exhaustive Test",
        "targetId": t1,
    }
    backup_policies.create_policy(pol)

    state = backup_write_continuity.get_write_continuity_state("pol-wc-ex")
    assert state["configuredPrimaryTargetId"] == t1
    assert state["activeWriteTargetId"] == t1

    # 3. Promote primary with revision mismatch raises 412 (Precondition Failed)
    with pytest.raises(AppError) as exc_rev:
        backup_write_continuity.promote_primary_target(
            "pol-wc-ex",
            target_id=t2,
            expected_policy_revision=999,
        )
    assert exc_rev.value.status in {409, 412}

    # 4. Successful primary promotion with valid revision
    promoted = backup_write_continuity.promote_primary_target(
        "pol-wc-ex",
        target_id=t2,
        expected_policy_revision=1,
    )
    assert promoted["status"] == "promoted"
    assert promoted["newPrimaryTargetId"] == t2
    assert promoted["policyRevision"] == 2

    state_after = backup_write_continuity.get_write_continuity_state("pol-wc-ex")
    assert state_after["configuredPrimaryTargetId"] == t2
    assert state_after["activeWriteTargetId"] == t2


def test_backup_targets_and_scheduler_deep_branches(tmp_settings: Path) -> None:
    # 1. probe_target_capacity
    assert backup_targets.probe_target_capacity("nonexistent_target_xyz")["source"] == "unknown"

    t_quota_id = "target_quota_test"
    backup_targets.register_filesystem_target(t_quota_id, path=tmp_settings / "quota_root")
    # Update quotaBytes
    t_data = backup_targets.get_target(t_quota_id)
    t_data["quotaBytes"] = 100 * 1024 * 1024
    backup_targets._atomic_write_json(backup_targets._registry_path(t_quota_id), t_data)

    cap = backup_targets.probe_target_capacity(t_quota_id)
    assert cap["source"] in {"filesystem", "configured-quota"}
    assert cap["totalBytes"] > 0

    # 2. reinitialize_target
    reinit_dir = tmp_settings / "reinit_dir"
    reinit_dir.mkdir(parents=True, exist_ok=True)
    init_res = backup_targets.init_target(reinit_dir, label="Init")
    reinit_res = backup_targets.reinitialize_target(reinit_dir, label="Reinit")
    assert reinit_res["targetId"] != init_res["targetId"]

    # 3. adopt_target_incarnation
    t_adopt_id = "target_adopt_branch"
    t_adopt_dir = tmp_settings / "adopt_dir"
    backup_targets.register_filesystem_target(t_adopt_id, path=t_adopt_dir)

    # Missing marker
    (t_adopt_dir / backup_targets.TARGET_MARKER_NAME).unlink()
    with pytest.raises(AppError) as exc_adopt_miss:
        backup_targets.adopt_target_incarnation(t_adopt_id)
    assert exc_adopt_miss.value.status == 409

    # Corrupt marker
    (t_adopt_dir / backup_targets.TARGET_MARKER_NAME).write_text("invalid json {{{", encoding="utf-8")
    with pytest.raises(AppError) as exc_adopt_corr:
        backup_targets.adopt_target_incarnation(t_adopt_id)
    assert exc_adopt_corr.value.status == 409

    # 4. scheduler reclaim_deferred_slots empty
    deferred = backup_scheduler.reclaim_deferred_slots([], instance_id="test-inst")
    assert deferred == []


def test_backup_writer_lease_deep_exceptions_and_skew(tmp_settings: Path) -> None:
    # 1. Init without root or store raises 500
    with pytest.raises(AppError) as exc_init:
        backup_writer_lease.TargetWriterLease(
            None,
            store=None,
            target_id="t1",
            owner_run_id="r1",
            owner_instance_id="i1",
            fencing_token=1,
        )
    assert exc_init.value.status == 500

    # 2. Remote writer lease path property raises 500
    remote_lease = backup_writer_lease.TargetWriterLease(
        None,
        store=cast(Any, SimpleNamespace()),
        target_id="t1",
        owner_run_id="r1",
        owner_instance_id="i1",
        fencing_token=1,
    )
    with pytest.raises(AppError) as exc_path:
        _ = remote_lease.path
    assert exc_path.value.status == 500

    # 3. _note_server_date with invalid string returns gracefully
    remote_lease._note_server_date("invalid-server-date-string")

    # 4. _note_server_date with ISO string
    remote_lease._note_server_date(backup_writer_lease._utc_iso())
    assert isinstance(remote_lease._server_skew, timedelta)


def test_backup_target_s3_checksum_and_delete_branches(tmp_settings: Path) -> None:
    s3 = backup_target_s3.S3TargetStore(
        bucket="mybucket",
        prefix="sub",
        endpoint_url="http://mock-s3.local",
    )

    # 1. put_if_match checksum mismatch raises 500
    with pytest.raises(AppError) as exc_put:
        s3.put_if_match("k1", b"hello", expected_etag="e1", checksum_sha256="wrong_sha256")
    assert exc_put.value.status == 500

    # 2. upload_part checksum mismatch raises 500
    upload = backup_target_s3.MultipartUpload(key="k1", upload_id="up1", checksum_sha256="sha1")
    with pytest.raises(AppError) as exc_part:
        s3.upload_part(upload, 1, b"hello", checksum_sha256="wrong_sha256")
    assert exc_part.value.status == 500

    # 3. delete_if_match when current is None returns False
    with patch.object(s3, "stat", return_value=None):
        with patch.object(s3, "_client_or_create", return_value=SimpleNamespace()):
            res_del_none = s3.delete_if_match("missing_key", expected_etag="e1")
            assert res_del_none is False

    # 4. delete_if_match with etag mismatch raises 412
    meta_diff = backup_target_s3.ObjectMeta(key="k1", size=5, etag="actual_etag")
    with patch.object(s3, "stat", return_value=meta_diff):
        with patch.object(s3, "_client_or_create", return_value=SimpleNamespace()):
            with pytest.raises(AppError) as exc_del_etag:
                s3.delete_if_match("k1", expected_etag="expected_diff_etag")
            assert exc_del_etag.value.status == 412


def test_backup_targets_s3_registration_branches(tmp_settings: Path) -> None:
    # 1. SDK unavailable without client raises 503
    with patch.object(backup_target_s3, "s3_sdk_available", return_value=False):
        with pytest.raises(AppError) as exc_sdk:
            backup_targets.init_s3_target(bucket="my-bkt", client=None)
        assert exc_sdk.value.status == 503

    # 2. Empty bucket raises 400
    with patch.object(backup_target_s3, "s3_sdk_available", return_value=True):
        with pytest.raises(AppError) as exc_bkt:
            backup_targets.init_s3_target(bucket="", client=SimpleNamespace())
        assert exc_bkt.value.status == 400

    # 3. aws-default-chain with profile maps to aws-profile
    fake_store = SimpleNamespace(
        capabilities=lambda: SimpleNamespace(kind="s3"),
        detect_versioning=lambda: "Enabled",
    )
    with patch.object(backup_target_s3, "open_s3_store", return_value=fake_store):
        with patch("deepseek_infra.infra.workspace.backup_target_store.read_json", return_value=None):
            with patch("deepseek_infra.infra.workspace.backup_target_store.put_json_if_absent"):
                with patch("deepseek_infra.infra.workspace.backup_target_store.probe_store_capabilities", return_value={"capabilities": {}}):
                    rec = backup_targets.init_s3_target(
                        bucket="profile-bkt",
                        credential_provider={"type": "aws-default-chain", "profile": "dev-prof"},
                        client=SimpleNamespace(),
                        probe=True,
                    )
                    assert rec["credentialProvider"]["type"] == "aws-profile"
                    assert rec["credentialProvider"]["profile"] == "dev-prof"


def test_backup_targets_adopt_target_incarnation_full_flow(tmp_settings: Path) -> None:
    t_dir = tmp_settings / "adopt_flow_tgt"
    t_dir.mkdir(parents=True, exist_ok=True)
    marker = t_dir / backup_targets.TARGET_MARKER_NAME

    record = {
        "targetId": "tgt_adopt_1",
        "targetNonce": "nonce_adopt_1",
        "path": str(t_dir),
        "kind": "filesystem",
    }

    # 1. Missing marker raises 409
    with patch.object(backup_targets, "get_target", return_value=record):
        with pytest.raises(AppError) as exc_miss:
            backup_targets.adopt_target_incarnation("tgt_adopt_1")
        assert exc_miss.value.status == 409
        assert "target marker is missing" in str(exc_miss.value)

    # 2. Replaced marker raises 409
    marker.write_text(json.dumps({"targetId": "different_id", "targetNonce": "diff_nonce"}), encoding="utf-8")
    with patch.object(backup_targets, "get_target", return_value=record):
        with pytest.raises(AppError) as exc_repl:
            backup_targets.adopt_target_incarnation("tgt_adopt_1")
        assert exc_repl.value.status == 409
        assert "target marker was replaced" in str(exc_repl.value)

    # 3. Valid marker adopted
    marker.write_text(json.dumps({
        "schemaVersion": 3,
        "targetId": "tgt_adopt_1",
        "targetNonce": "nonce_adopt_1",
        "targetGeneration": 3,
        "latestCommitHash": "a" * 64,
    }), encoding="utf-8")
    with patch.object(backup_targets, "get_target", return_value=record):
        with patch.object(backup_targets, "_write_checkpoint"):
            res = backup_targets.adopt_target_incarnation("tgt_adopt_1")
            assert res["adopted"] is True
            assert res["targetId"] == "tgt_adopt_1"
            assert "incarnationId" in res
