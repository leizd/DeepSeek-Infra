"""Read-only diagnostic decoder for the actual Rust media journal receipt."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
import sqlite3
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


@lru_cache(maxsize=1)
def media_outcome_type() -> Any:
    descriptor = descriptor_pb2.FileDescriptorSet.FromString((Path(__file__).resolve().parents[1] / "proto/generated/descriptor.pb").read_bytes())
    pool = descriptor_pool.DescriptorPool()
    pending = list(descriptor.file)
    while pending:
        before = len(pending)
        for document in list(pending):
            try:
                pool.Add(document)
            except TypeError:
                continue
            pending.remove(document)
        assert len(pending) < before
    return message_factory.GetMessageClass(pool.FindMessageTypeByName("deepseek.media.v1.MediaPatchOutcome"))


def compact_patch_observation(owned_root: Path) -> dict[str, Any]:
    journals = list(owned_root.rglob("media-effects.sqlite3"))
    assert len(journals) == 1
    path = journals[0]
    assert path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(owned_root.resolve())
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT operation_id,state,preparation_sha256,length(outcome),substr(outcome,1,4096) FROM media_operations LIMIT 2").fetchall()
    assert len(rows) == 1
    operation, state, preparation, size, raw = rows[0]
    assert state == "APPLIED" and 0 < size < 4096 and len(raw) == size
    cls = media_outcome_type()
    receipt = cls.FromString(raw)
    assert receipt.operation_id == operation and receipt.HasField("patched")
    assert not receipt.HasField("media") and not receipt.HasField("created") and not receipt.HasField("processed")
    assert receipt.patched.original_fence == receipt.original_fence
    receipt.signature = b""
    assert hashlib.sha256(receipt.SerializeToString(deterministic=True)).digest() == preparation
    terminal = cls.FromString(raw)
    return {"schemaVersion": terminal.schema_version, "operationId": operation, "state": state,
            "outcomeBytes": size, "outcomeSha256": hashlib.sha256(raw).hexdigest(),
            "signatureBytes": len(terminal.signature), "recordBytes": terminal.patched.record_bytes,
            "recordSha256": terminal.patched.record_sha256.hex(),
            "recordPreparationSha256": terminal.patched.preparation_sha256.hex(),
            "dispatchPreparationSha256": preparation.hex(), "dispatchDigestMatchesUnsignedOutcome": True,
            "readOnlyDiagnostic": True, "independentCryptographicSignatureVerifiedHere": False}
