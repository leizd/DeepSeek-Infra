"""Encrypted object-set protocol contracts for remote backup storage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from deepseek_infra.core.errors import AppError, ErrorCode

WHOLE_AGE_V1 = "whole-age-v1"
OBJECT_SET_V1 = "object-set-v1"
CURRENT_STORAGE_PROTOCOL = OBJECT_SET_V1


@dataclass(frozen=True, slots=True)
class EncryptedComponent:
    """One independently encrypted object in a snapshot object set.

    ``component_id`` and ``control`` are local orchestration facts. They are
    never serialized into the public remote object inventory.
    """

    component_id: str
    path: Path
    ciphertext_digest: str
    ciphertext_size: int
    control: bool = False


@dataclass(frozen=True, slots=True)
class ObjectSetPackage:
    """Verified independently encrypted components for one snapshot."""

    backup_id: str
    components: tuple[EncryptedComponent, ...]
    manifest_digest: str
    coverage_digest: str
    manifest: dict[str, Any]
    creation_verified: bool = True
    chunk_records: tuple[Any, ...] = ()
    effective_files: tuple[Any, ...] = ()
    savings: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        validate_components(self.components)

    @property
    def storage_protocol(self) -> str:
        return OBJECT_SET_V1

    @property
    def control(self) -> EncryptedComponent:
        return next(item for item in self.components if item.control)

    @property
    def object_set_digest(self) -> str:
        return object_set_digest(self.components)

    @property
    def filename(self) -> str:
        return f"{self.backup_id}.object-set"

    @property
    def path(self) -> Path:
        return self.control.path

    @property
    def size(self) -> int:
        return total_ciphertext_bytes(self.components)

    @property
    def ciphertext_sha256(self) -> str:
        """Compatibility commitment; never used as an object key."""
        return self.object_set_digest


def validate_components(components: Sequence[EncryptedComponent]) -> None:
    if sum(1 for item in components if item.control) != 1:
        raise AppError("object set must contain exactly one control object", code=ErrorCode.INVALID_PAYLOAD)
    digests: set[str] = set()
    component_ids: set[str] = set()
    for item in components:
        if not item.component_id or item.component_id in component_ids:
            raise AppError("object set contains duplicate component ids", code=ErrorCode.INVALID_PAYLOAD)
        component_ids.add(item.component_id)
        if len(item.ciphertext_digest) != 64 or any(char not in "0123456789abcdef" for char in item.ciphertext_digest):
            raise AppError("object set contains an invalid ciphertext digest", code=ErrorCode.INVALID_PAYLOAD)
        if item.ciphertext_digest in digests:
            raise AppError("object set contains duplicate ciphertext digests", code=ErrorCode.INVALID_PAYLOAD)
        digests.add(item.ciphertext_digest)
        if item.ciphertext_size < 0:
            raise AppError("object set contains an invalid ciphertext size", code=ErrorCode.INVALID_PAYLOAD)


def remote_object_inventory(components: Sequence[EncryptedComponent]) -> list[dict[str, int | str]]:
    """Return the role-blind public commitment inventory."""
    validate_components(components)
    return sorted(
        ({"digest": item.ciphertext_digest, "size": int(item.ciphertext_size)} for item in components),
        key=lambda item: (str(item["digest"]), int(item["size"])),
    )


def object_set_commitment(components: Sequence[EncryptedComponent]) -> bytes:
    inventory = remote_object_inventory(components)
    return "".join(f"{item['digest']}:{item['size']}\n" for item in inventory).encode("ascii")


def object_set_digest(components: Sequence[EncryptedComponent]) -> str:
    return hashlib.sha256(object_set_commitment(components)).hexdigest()


def total_ciphertext_bytes(components: Iterable[EncryptedComponent]) -> int:
    return sum(item.ciphertext_size for item in components)
