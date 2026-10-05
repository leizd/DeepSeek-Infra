"""Sealed frontend mirror parity probe, Python side (self-driving).

Proves the Rust mirror store and the Python oracle agree **in both directions**, on the
same bytes:

1. the oracle writes a mirror; the Rust store reads that directory and reports it; the
   two reports must be equal;
2. the Rust store writes a mirror from the same inputs; `metadata.json` and `HEAD.json`
   must be byte-identical to the oracle's after masking the random generation id and the
   write clock and the ciphertext hashes (randomized age is a frozen contract, so a
   ciphertext hash *cannot* agree);
3. the oracle then reads the **Rust-written** directory — `list_mirrors`,
   `mirror_status`, `mirror_files` — and decrypts the ciphertext with the real recipient
   identity, which is what a restore does. This is the handback direction: it is the
   reason the two implementations have to agree about the file format rather than only
   about a JSON report.

Usage::

    python tasks/native-runtime/backup_mirror_parity_probe.py \
        --rust-example rust/target/debug/examples/backup_mirror_parity_probe.exe

Exits non-zero if any check fails, and prints a JSON report either way.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from deepseek_infra.infra.workspace import backup_crypto, backup_mirror, backup_unattended  # noqa: E402

sha256_file = backup_unattended.sha256_file

PROFILE = "mirror_parity"
EPOCH = "epoch-parity"
SEQUENCE = 7
REPLICA = "probe-replica"
ACKNOWLEDGED_AT = "2026-09-30T12:00:00Z"
NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
_HEX64 = re.compile(r"[0-9a-f]{64}")


def mask(text: str, generation: str, created: str) -> str:
    """The Rust side's `mask`, so the two byte strings are comparable."""
    masked = text.replace(generation, "<generation>")
    if created:
        masked = masked.replace(created, "<created>")
    return _HEX64.sub("<sha256>", masked)


def envelope_bytes() -> bytes:
    """The envelope both sides seal, serialised exactly as `put_frontend_mirror` does."""
    body: dict[str, Any] = {
        "schemaVersion": 1,
        "conversations": [{"id": "c-1", "title": "parity"}, {"id": "c-2", "title": "second"}],
        "conflicts": [{"id": "x-1"}],
    }
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    body["digest"] = digest
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def oracle_write(root: Path, envelope: dict[str, Any], recipient: str) -> dict[str, Any]:
    backup_mirror.BACKUP_MIRROR_DIR = root
    return backup_mirror.put_frontend_mirror(
        PROFILE,
        envelope,
        source_epoch=EPOCH,
        recipients=[recipient],
        acknowledged_at=ACKNOWLEDGED_AT,
        client_replica_id=REPLICA,
        client_sequence=SEQUENCE,
        now=NOW,
    )


def oracle_report(root: Path) -> dict[str, Any]:
    backup_mirror.BACKUP_MIRROR_DIR = root
    mirrors = []
    for metadata in backup_mirror.list_mirrors():
        mirrors.append(
            {
                "profileId": metadata.get("profileId"),
                "generationId": metadata.get("generationId"),
                "parentGenerationId": metadata.get("parentGenerationId"),
                "sourceEpoch": metadata.get("sourceEpoch"),
                "clientSequence": metadata.get("clientSequence"),
                "clientReplicaId": metadata.get("clientReplicaId"),
                "envelopeDigest": metadata.get("envelopeDigest"),
                "recipientSetDigest": metadata.get("recipientSetDigest"),
                "recipientVariantCount": len(metadata.get("recipientVariants") or []),
                "recipientVariants": metadata.get("recipientVariants"),
                "conversations": metadata.get("conversations"),
                "conflicts": metadata.get("conflicts"),
                "createdAt": metadata.get("createdAt"),
                "acknowledgedAt": metadata.get("acknowledgedAt"),
                "creationVerified": metadata.get("creationVerified"),
                "ciphertextSha256": metadata.get("ciphertextSha256"),
                "schemaVersion": metadata.get("schemaVersion"),
            }
        )
    statuses: dict[str, Any] = {}
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not path.is_dir() or path.name == backup_mirror.PREVIOUS_DIR_NAME:
                continue
            statuses[path.name] = backup_mirror.mirror_status(path.name)
    files: dict[str, Any] = {}
    for entry in mirrors:
        profile = str(entry["profileId"])
        try:
            ciphertext, metadata_path, metadata = backup_mirror.mirror_files(profile)
            files[profile] = {
                "ciphertextSha256": sha256_file(ciphertext),
                "metadataSha256": sha256_file(metadata_path),
                "schemaVersion": metadata.get("schemaVersion"),
            }
        except Exception as exc:  # noqa: BLE001 - the probe reports the refusal
            files[profile] = {"error": str(exc), "code": getattr(getattr(exc, "code", None), "value", None)}
    heads: dict[str, Any] = {}
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue
            head = path / backup_mirror.HEAD_NAME
            if not head.is_file():
                continue
            raw = head.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            parsed["generationId"] = "<generation>"
            parsed["updatedAt"] = "<updated>"
            heads[path.name] = {
                "normalised": parsed,
                "rawSha256": sha256_file(head),
            }
    return {"mirrors": mirrors, "statuses": statuses, "files": files, "heads": heads}


def run_rust(example: Path, args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [str(example), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"rust probe failed ({completed.returncode}): {completed.stderr.strip()[-400:]}")
    return json.loads(completed.stdout)


def scratch_root() -> Path:
    """A directory both processes can write.

    Not `tempfile.mkdtemp()`: CPython 3.13 creates that directory with a restrictive
    DACL (`OWNER RIGHTS` plus SYSTEM/Administrators only), so the *Rust* process — a
    second process, under a different token — is refused with `os error 5` when it tries
    to create its generation there, while the Python side writes happily. A scratch root
    under the repo's gitignored `artifacts/` has the inherited workspace DACL and both
    sides can write it.
    """
    base = REPO / "artifacts"
    base.mkdir(parents=True, exist_ok=True)
    import uuid

    root = base / f"mirror-parity-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-example", required=True, type=Path)
    parser.add_argument("--scratch", type=Path, help="scratch root; defaults to artifacts/mirror-parity-*")
    args = parser.parse_args(argv)

    checks: dict[str, Any] = {}
    failures: list[str] = []
    working = args.scratch.resolve() if args.scratch else scratch_root()
    working.mkdir(parents=True, exist_ok=True)
    try:
        identity = backup_crypto.generate_identity()
        recipient = str(identity["recipient"])
        identity_secret = bytearray(str(identity["identity"]).encode("utf-8"))

        envelope_raw = envelope_bytes()
        envelope_path = working / "envelope.json"
        envelope_path.write_bytes(envelope_raw)
        envelope = json.loads(envelope_raw)

        oracle_root = working / "oracle"
        native_root = working / "native"
        oracle_root.mkdir(parents=True, exist_ok=True)
        native_root.mkdir(parents=True, exist_ok=True)

        oracle_metadata = oracle_write(oracle_root, envelope, recipient)
        native_metadata = run_rust(
            args.rust_example,
            [
                "--write",
                "--root",
                str(native_root),
                "--profile",
                PROFILE,
                "--epoch",
                EPOCH,
                "--sequence",
                str(SEQUENCE),
                "--recipient",
                recipient,
                "--envelope",
                str(envelope_path),
                "--acknowledged-at",
                ACKNOWLEDGED_AT,
                "--now",
                "2026-09-30T12:00:00Z",
                "--replica",
                REPLICA,
            ],
        )

        # 1. The oracle's report of its own directory vs the Rust store's report of the
        #    same directory.
        native_read_oracle = run_rust(args.rust_example, ["--read", str(oracle_root)])
        oracle_self = oracle_report(oracle_root)
        checks["oracle-directory-read-by-both"] = {
            "equal": native_read_oracle == oracle_self,
            "oracle": oracle_self,
            "native": native_read_oracle,
        }
        if native_read_oracle != oracle_self:
            failures.append("oracle-directory-read-by-both")

        # 2. Rust's own directory, read back by the oracle — the handback direction.
        oracle_read_native = oracle_report(native_root)
        native_self = run_rust(args.rust_example, ["--read", str(native_root)])
        checks["native-directory-read-by-both"] = {
            "equal": oracle_read_native == native_self,
            "oracle": oracle_read_native,
            "native": native_self,
        }
        if oracle_read_native != native_self:
            failures.append("native-directory-read-by-both")

        # 3. Byte-identical generation descriptor and HEAD pointer.
        oracle_generation = str(oracle_metadata["generationId"])
        native_generation = str(native_metadata["metadata"]["generationId"])
        oracle_created = str(oracle_metadata["createdAt"])
        native_created = str(native_metadata["metadata"]["createdAt"])
        oracle_dir = oracle_root / PROFILE / backup_mirror.GENERATIONS_DIR_NAME / oracle_generation
        native_dir = native_root / PROFILE / backup_mirror.GENERATIONS_DIR_NAME / native_generation
        oracle_metadata_bytes = mask(
            (oracle_dir / backup_mirror.GENERATION_METADATA_NAME).read_text(encoding="utf-8"),
            oracle_generation,
            oracle_created,
        )
        native_metadata_bytes = mask(
            (native_dir / backup_mirror.GENERATION_METADATA_NAME).read_text(encoding="utf-8"),
            native_generation,
            native_created,
        )
        oracle_head_bytes = mask(
            (oracle_root / PROFILE / backup_mirror.HEAD_NAME).read_text(encoding="utf-8"),
            oracle_generation,
            oracle_created,
        )
        native_head_bytes = mask(
            (native_root / PROFILE / backup_mirror.HEAD_NAME).read_text(encoding="utf-8"),
            native_generation,
            native_created,
        )
        checks["metadata-bytes-identical"] = {
            "equal": oracle_metadata_bytes == native_metadata_bytes,
            "oracle": oracle_metadata_bytes,
            "native": native_metadata_bytes,
        }
        if oracle_metadata_bytes != native_metadata_bytes:
            failures.append("metadata-bytes-identical")
        checks["head-bytes-identical"] = {
            "equal": oracle_head_bytes == native_head_bytes,
            "oracle": oracle_head_bytes,
            "native": native_head_bytes,
        }
        if oracle_head_bytes != native_head_bytes:
            failures.append("head-bytes-identical")

        # 4. The Rust-written ciphertext decrypts, with the real recipient identity, to
        #    the exact envelope bytes both sides were given.
        ciphertext, _metadata_path, _metadata = backup_mirror.mirror_files(PROFILE, recipients=[recipient])
        decrypted = working / "decrypted.json"
        backup_crypto.decrypt_file(ciphertext, decrypted, kind="age-identity", secret=identity_secret)
        decrypted_bytes = decrypted.read_bytes()
        checks["native-ciphertext-decrypts-to-the-envelope"] = {
            "equal": decrypted_bytes == envelope_raw,
            "decryptedSha256": sha256_file(decrypted),
            "envelopeSha256": hashlib.sha256(envelope_raw).hexdigest(),
        }
        if decrypted_bytes != envelope_raw:
            failures.append("native-ciphertext-decrypts-to-the-envelope")

        # 5. The variant the generation advertises is the variant the store resolves, and
        #    its recorded hash matches the file the oracle just read.
        variant = (oracle_read_native["mirrors"][0]["recipientVariants"] or [{}])[0]
        checks["variant-hash-matches-file"] = {
            "equal": variant.get("ciphertextSha256") == sha256_file(ciphertext),
            "recorded": variant.get("ciphertextSha256"),
            "observed": sha256_file(ciphertext),
        }
        if variant.get("ciphertextSha256") != sha256_file(ciphertext):
            failures.append("variant-hash-matches-file")

        # 6. A second, identical upload is a replay and must not create a generation.
        replay = run_rust(
            args.rust_example,
            [
                "--write",
                "--root",
                str(native_root),
                "--profile",
                PROFILE,
                "--epoch",
                EPOCH,
                "--sequence",
                str(SEQUENCE),
                "--recipient",
                recipient,
                "--envelope",
                str(envelope_path),
                "--acknowledged-at",
                ACKNOWLEDGED_AT,
                "--now",
                "2026-09-30T12:00:00Z",
                "--replica",
                REPLICA,
            ],
        )
        oracle_replay = oracle_write(native_root, envelope, recipient)
        generations = sorted(
            path.name
            for path in (native_root / PROFILE / backup_mirror.GENERATIONS_DIR_NAME).iterdir()
            if path.is_dir()
        )
        checks["identical-replay-is-idempotent"] = {
            "equal": bool(replay["idempotent"]) and bool(oracle_replay.get("idempotent")) and len(generations) == 1,
            "nativeIdempotent": replay["idempotent"],
            "oracleIdempotent": bool(oracle_replay.get("idempotent")),
            "generations": generations,
        }
        if not checks["identical-replay-is-idempotent"]["equal"]:
            failures.append("identical-replay-is-idempotent")

        # 7. Corruption is refused identically: flip a ciphertext byte and both sides must
        #    report mirror-generation-corrupt rather than serving the file.
        tampered = bytearray(ciphertext.read_bytes())
        tampered[-1] ^= 1
        original = ciphertext.read_bytes()
        ciphertext.write_bytes(bytes(tampered))
        oracle_error: dict[str, Any] = {}
        try:
            backup_mirror.mirror_files(PROFILE, recipients=[recipient])
        except Exception as exc:  # noqa: BLE001 - the probe reports the refusal
            oracle_error = {
                "message": str(exc),
                "status": getattr(exc, "status", None),
                "code": getattr(getattr(exc, "code", None), "value", None),
            }
        native_error = run_rust(
            args.rust_example,
            ["--read", str(native_root)],
        )["files"][PROFILE]
        ciphertext.write_bytes(original)
        checks["corruption-is-refused-by-both"] = {
            "equal": oracle_error.get("status") == native_error.get("status")
            and oracle_error.get("message") == native_error.get("error"),
            "oracle": oracle_error,
            "native": native_error,
        }
        if not checks["corruption-is-refused-by-both"]["equal"]:
            failures.append("corruption-is-refused-by-both")

        identity_secret[:] = b"\x00" * len(identity_secret)
        json.dump(
            {"checks": checks, "failures": failures, "generations": generations},
            sys.stdout,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
        return 1 if failures else 0
    finally:
        shutil.rmtree(working, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
