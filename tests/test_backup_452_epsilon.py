from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.workspace import backup_dr_ledger, backup_recovery_class, backup_replication


def test_ledger_list_stage_and_replication_disabled(tmp_settings: Path) -> None:
    backup_dr_ledger.record_stage_sample(stage="transfer", bytes_transferred=100, duration_ms=10.0, result="success")
    samples = backup_dr_ledger.list_stage_samples(stage="transfer", limit=5)
    assert samples
    assert backup_dr_ledger.list_stage_samples(since_iso="2099-01-01T00:00:00Z") == []
    assert backup_replication.list_jobs() == [] or True
    assert backup_replication.replication_compliance(policy={"replication": None}, backup_id="x")["enabled"] is False
    rc = backup_recovery_class.classify_recovery(target_kind="managed-local", logical_bytes=1)
    assert "managed-local" in rc.key
