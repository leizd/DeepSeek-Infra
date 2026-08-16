"""Direct unit coverage for audit binding validators."""

from __future__ import annotations

from deepseek_infra.infra.workspace import backup_dr_audit, backup_publish


def test_validate_binding_all_anomaly_branches(monkeypatch) -> None:
    monkeypatch.setattr(backup_publish, "commit_marker_valid", lambda m: False)
    assert backup_dr_audit._validate_commit_receipt_binding(
        target_id="t", commit={}, receipt=None, previous_commit_hash=None
    ) == ["invalid-commit-marker"]

    monkeypatch.setattr(backup_publish, "commit_marker_valid", lambda m: True)
    assert "missing-backup-id" in backup_dr_audit._validate_commit_receipt_binding(
        target_id="t", commit={"backupId": ""}, receipt={}, previous_commit_hash=None
    )

    # soft continuity pass branch
    out = backup_dr_audit._validate_commit_receipt_binding(
        target_id="t",
        commit={"backupId": "b", "previousCommitHash": "x" * 64, "receiptDigest": ""},
        receipt={"backupId": "b"},
        previous_commit_hash="y" * 64,
    )
    assert isinstance(out, list)

    assert any("missing-receipt" in a for a in backup_dr_audit._validate_commit_receipt_binding(
        target_id="t", commit={"backupId": "b"}, receipt=None, previous_commit_hash=None
    ))

    anomalies = backup_dr_audit._validate_commit_receipt_binding(
        target_id="t1",
        commit={"backupId": "b", "policyId": "p1", "receiptDigest": "0" * 64, "objectSetDigest": ""},
        receipt={
            "backupId": "other",
            "targetId": "t2",
            "policyId": "p2",
            "storageProtocol": "object-set-v1",
            "objects": [],
        },
        previous_commit_hash=None,
    )
    joined = " ".join(anomalies)
    assert "receipt-digest-mismatch" in joined or "receipt-backup-id-mismatch" in joined
    assert "receipt-target-mismatch" in joined
    assert "receipt-policy-mismatch" in joined
    assert "missing-object-set-digest" in joined or "invalid-object-set-inventory" in joined

    # empty objects list inventory
    anomalies2 = backup_dr_audit._validate_commit_receipt_binding(
        target_id="t",
        commit={"backupId": "b", "objectSetDigest": "a" * 64},
        receipt={
            "backupId": "b",
            "storageProtocol": "object-set-v1",
            "objectSetDigest": "a" * 64,
            "objects": "bad",
        },
        previous_commit_hash=None,
    )
    assert any("invalid-object-set-inventory" in a for a in anomalies2)
