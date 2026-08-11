from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_object_set


def _component(tmp_path: Path, component_id: str, payload: bytes, *, control: bool = False) -> backup_object_set.EncryptedComponent:
    path = tmp_path / f"{component_id}.age"
    path.write_bytes(payload)
    return backup_object_set.EncryptedComponent(
        component_id=component_id,
        path=path,
        ciphertext_digest=hashlib.sha256(payload).hexdigest(),
        ciphertext_size=len(payload),
        control=control,
    )


def test_object_set_digest_is_canonical_and_role_blind(tmp_path: Path) -> None:
    control = _component(tmp_path, "control", b"random-control", control=True)
    payload = _component(tmp_path, "p0000", b"random-payload")

    first = backup_object_set.object_set_digest([control, payload])
    second = backup_object_set.object_set_digest([payload, control])

    assert first == second
    assert len(first) == 64
    assert "control" not in backup_object_set.object_set_commitment([control, payload]).decode("ascii")
    assert "p0000" not in backup_object_set.object_set_commitment([control, payload]).decode("ascii")


def test_object_set_requires_one_control_and_unique_ciphertexts(tmp_path: Path) -> None:
    control = _component(tmp_path, "control", b"same", control=True)
    duplicate = backup_object_set.EncryptedComponent(
        component_id="p0000",
        path=control.path,
        ciphertext_digest=control.ciphertext_digest,
        ciphertext_size=control.ciphertext_size,
    )
    with pytest.raises(AppError, match="duplicate ciphertext"):
        backup_object_set.validate_components([control, duplicate])
    with pytest.raises(AppError, match="exactly one control"):
        backup_object_set.validate_components([duplicate])


def test_remote_object_inventory_contains_ciphertext_facts_only(tmp_path: Path) -> None:
    control = _component(tmp_path, "control", b"control", control=True)
    payload = _component(tmp_path, "p0000", b"payload")

    inventory = backup_object_set.remote_object_inventory([payload, control])

    assert inventory == sorted(inventory, key=lambda item: item["digest"])
    assert set(inventory[0]) == {"digest", "size"}
    assert {item["digest"] for item in inventory} == {control.ciphertext_digest, payload.ciphertext_digest}
