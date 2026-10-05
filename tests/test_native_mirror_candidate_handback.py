"""Offline candidate handback integrity; fixture receipt is not native proof.

The Rust real-process integration generates an actual revocation receipt and
verifies the complete handback with real Age data separately.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import backup_mirror
from scripts import native_mirror_handoff as handoff
from .test_native_mirror_handoff import _source


def _candidate(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    source = _source(tmp_path / "source/.backup-mirror")
    target = tmp_path / "target/.backup-mirror"
    manifest_path = tmp_path / "export.json"
    manifest = handoff.export_and_fence(source, target, "rollback-1", manifest_path)
    shutil.copytree(source, target)
    receipt = {
        "schema": "native-mirror-inventory-import-v1", "domain": handoff.DOMAIN,
        "phase": "revoked", "transferId": "rollback-1",
        "sourceRoot": source.as_posix(), "targetRoot": target.as_posix(),
        "stagingRoot": target.with_name(target.name + ".native-staging-rollback-1").as_posix(),
        "manifestDigest": manifest["manifestDigest"], "sourceDigest": manifest["sourceDigest"],
        "targetDigest": manifest["sourceDigest"],
    }
    receipt["receiptDigest"] = handoff._digest(receipt)
    path = target.with_name(target.name + ".native-import.json")
    path.write_bytes(handoff._canonical(receipt) + b"\n")
    return source, target, manifest_path, receipt


def test_handback_preserves_inventory_and_revocation_then_replays_after_source_writes(tmp_path: Path) -> None:
    source, target, manifest, receipt = _candidate(tmp_path)
    fence = backup_mirror.mirror_handoff_fence_path(source)
    raw_fence = fence.read_bytes()
    before = handoff._inventory(source)
    result = handoff.handback_revoked_candidate(source, target, manifest)
    assert result["schema"] == "python-mirror-candidate-handback-v1"
    assert result["revocationReceiptDigest"] == receipt["receiptDigest"]
    assert result["sourceDigest"] == result["targetDigest"]
    assert not fence.exists()
    assert source.with_name(source.name + ".native-handback.json").is_file()
    assert source.with_name(source.name + ".native-handoff-rollback-1.revoked.json").read_bytes() == raw_fence
    assert handoff._inventory(source) == handoff._inventory(target) == before
    candidate_receipt = target.with_name(target.name + ".native-import.json")
    saved = candidate_receipt.read_bytes()
    # The original writer may progress after handback; replay is historical and
    # must never replace its new bytes with the old candidate.
    (source / "mirror_main/HEAD.json").write_text("new original-writer state", encoding="utf-8")
    assert handoff.handback_revoked_candidate(source, target, manifest) == result
    assert (source / "mirror_main/HEAD.json").read_text(encoding="utf-8") == "new original-writer state"
    assert candidate_receipt.read_bytes() == saved


@pytest.mark.parametrize("damage", ["source", "target", "fence", "receipt", "phase", "binding", "unknown", "stage", "source-restore", "target-restore", "source-restore-null", "target-restore-null"])
def test_handback_refuses_inconsistent_or_unrevoked_candidate_without_unfencing(tmp_path: Path, damage: str) -> None:
    source, target, manifest, receipt = _candidate(tmp_path)
    fence = backup_mirror.mirror_handoff_fence_path(source)
    receipt_path = target.with_name(target.name + ".native-import.json")
    if damage in {"source", "target"}:
        root = source if damage == "source" else target
        (root / "mirror_main/generations/gen_12345678/state.12345678.age").write_bytes(b"unexpected bytes")
    elif damage == "fence":
        fence.write_text("broken fence", encoding="utf-8")
    elif damage == "receipt":
        receipt_path.write_text("broken receipt", encoding="utf-8")
    elif damage in {"phase", "binding", "unknown"}:
        receipt.pop("receiptDigest")
        if damage == "phase":
            receipt["phase"] = "imported"
        elif damage == "binding":
            receipt["sourceRoot"] = (tmp_path / "different-source").as_posix()
        else:
            receipt["allowWrite"] = True
        receipt["receiptDigest"] = handoff._digest(receipt)
        receipt_path.write_bytes(handoff._canonical(receipt))
    elif damage == "stage":
        Path(str(receipt["stagingRoot"])).mkdir()
    else:
        root = source if damage.startswith("source-restore") else target
        (root.parent / ".workspace-restore-fence.json").write_text("null" if damage.endswith("null") else "{}", encoding="utf-8")
    before = (fence.read_bytes(), receipt_path.read_bytes())
    with pytest.raises(ValueError):
        handoff.handback_revoked_candidate(source, target, manifest)
    assert (fence.read_bytes(), receipt_path.read_bytes()) == before
    assert not source.with_name(source.name + ".native-handback.json").exists()


def test_interrupted_handback_preserves_source_fence_and_can_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, manifest, _ = _candidate(tmp_path)
    publish = handoff._publish_document

    def fail_audit(path: Path, data: bytes) -> None:
        if path.name.endswith(".native-handback-rollback-1.json"):
            raise OSError("handback audit publication failed")
        publish(path, data)

    monkeypatch.setattr(handoff, "_publish_document", fail_audit)
    with pytest.raises(OSError, match="publication failed"):
        handoff.handback_revoked_candidate(source, target, manifest)
    assert backup_mirror.mirror_handoff_fence_path(source).is_file()
    assert source.with_name(source.name + ".native-handback.json").is_file()
    monkeypatch.setattr(handoff, "_publish_document", publish)
    result = handoff.handback_revoked_candidate(source, target, manifest)
    assert result["sourceDigest"] == result["targetDigest"]
    assert not backup_mirror.mirror_handoff_fence_path(source).exists()
