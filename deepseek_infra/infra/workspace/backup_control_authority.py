"""Secretless hash-chained Control Authority checkpoints (control-authority-v1).

4.6.5: crash-atomic mutation journal, monotonic head CAS, cross-replica ancestry,
durable controlBootEpoch, production replica provider integration.
Does not change Backup wire formats (object-set-v1 / Receipt v4 / Commit v4).
Credentials and Age identities must never enter checkpoints.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_control

AUTHORITY_SCHEMA = "control-authority-v1"
AUTHORITY_CHECKPOINT_PREFIX = "control/authority/checkpoints"
AUTHORITY_HEAD_KEY = "control/authority/head.json"
OUTBOX_PENDING = "pending"
OUTBOX_DURABLE = "durable"
OUTBOX_FAILED = "failed"

# Crash-atomic authority mutation journal states (schema v8 / 4.6.5 Gate F).
MUTATION_PREPARED = "prepared"
MUTATION_ANCHORING = "anchoring"
MUTATION_REMOTE_OUTCOME_UNKNOWN = "remote-outcome-unknown"
MUTATION_DURABLE = "durable"
MUTATION_SUPERSEDED = "superseded"
MUTATION_DIVERGENT = "divergent"
UNRESOLVED_MUTATION_STATES = frozenset(
    {
        MUTATION_PREPARED,
        MUTATION_ANCHORING,
        MUTATION_REMOTE_OUTCOME_UNKNOWN,
        MUTATION_DIVERGENT,
    }
)

# Process-local authority replicas. Empty ⇒ local-only mode (no RPO=0).
_AUTHORITY_ANCHOR_ROOTS: list[Path] = []
_AUTHORITY_ANCHOR_STORES: list[Any] = []

# Case-insensitive substring match against JSON object keys (recursive).
FORBIDDEN_SECRET_KEY_FRAGMENTS = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "token",
        "apikey",
        "api_key",
        "accesskey",
        "access_key",
        "secretkey",
        "secret_key",
        "privatekey",
        "private_key",
        "ageidentity",
        "age_identity",
        "identity",
        "credential",
        "oauth",
        "bearer",
        "session",
    }
)

# Keys allowed even if they contain a forbidden fragment (references only).
ALLOWED_SECRET_ADJACENT_KEYS = frozenset(
    {
        "credentialreference",
        "credential_reference",
        "credentialprovidertype",
    }
)

_TARGET_KEEP_KEYS = frozenset(
    {
        "targetId",
        "kind",
        "region",
        "failureDomain",
        "storageTier",
        "storageTierClass",
        "endpointUrl",
        "endpoint",
        "bucket",
        "prefix",
        "provider",
        "jurisdiction",
        "topologyGeneration",
        "credentialReference",
        "status",
        "drainState",
        "quotaBytes",
        "storageCostPerGibMonth",
        "egressCostPerGib",
    }
)


def authority_checkpoint_key(generation: int) -> str:
    return f"{AUTHORITY_CHECKPOINT_PREFIX}/{int(generation):016d}.json"


def authority_head_key() -> str:
    return AUTHORITY_HEAD_KEY


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _key_is_forbidden(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]", "", str(key).casefold())
    if normalized in ALLOWED_SECRET_ADJACENT_KEYS or normalized in {"credentialreference", "credentialprovider"}:
        return False
    return any(frag in normalized for frag in FORBIDDEN_SECRET_KEY_FRAGMENTS if frag != "credential")


def strip_secrets(value: Any) -> Any:
    """Recursively drop secret-bearing keys; fail closed on nested credential material."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            norm = re.sub(r"[^a-z0-9_]", "", key_s.casefold())
            if norm == "credentialreference":
                out[key_s] = str(item) if item is not None else None
                continue
            if norm == "credentialprovider" and isinstance(item, dict):
                provider_type = item.get("type") or item.get("providerType")
                if provider_type is not None:
                    out[key_s] = {"type": str(provider_type)}
                continue
            if _key_is_forbidden(key_s):
                continue
            out[key_s] = strip_secrets(item)
        return out
    if isinstance(value, list):
        return [strip_secrets(item) for item in value]
    return value


def sanitize_target_for_authority(target: dict[str, Any]) -> dict[str, Any]:
    """Target metadata for checkpoints: locator + reference, never credentials."""
    cleaned = strip_secrets(dict(target))
    assert isinstance(cleaned, dict)
    out: dict[str, Any] = {}
    for key, value in cleaned.items():
        if key in _TARGET_KEEP_KEYS or key in {"topologyGeneration", "credentialReference"}:
            out[str(key)] = value
    result = strip_secrets(out)
    assert isinstance(result, dict)
    return result


def sanitize_policy_for_authority(policy: dict[str, Any]) -> dict[str, Any]:
    return strip_secrets(dict(policy)) if isinstance(policy, dict) else {}


def _payload_for_digest(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in checkpoint.items()
        if key not in {"digest", "previousDigest", "payloadDigest"}
    }


def compute_payload_digest(checkpoint: dict[str, Any]) -> str:
    return _sha256_hex(_canonical_json(_payload_for_digest(checkpoint)))


def compute_checkpoint_digest(checkpoint: dict[str, Any]) -> str:
    envelope = {
        "authorityGeneration": int(checkpoint["authorityGeneration"]),
        "previousDigest": checkpoint.get("previousDigest"),
        "payloadDigest": str(checkpoint["payloadDigest"]),
        "schema": str(checkpoint.get("schema") or AUTHORITY_SCHEMA),
    }
    return _sha256_hex(_canonical_json(envelope))


def build_authority_checkpoint(
    *,
    generation: int,
    previous_digest: str | None,
    policies: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    receipt_mutation_generations: dict[str, int],
    promotion_epochs: dict[str, int],
    drain_generations: dict[str, int],
    placement_generations: dict[str, int],
    control_schema_version: int,
    created_at: str | None = None,
    control_boot_epoch: int | None = None,
    mutation_id: str | None = None,
) -> dict[str, Any]:
    gen = int(generation)
    if gen < 1:
        raise AppError("authority generation must be >= 1", code=ErrorCode.INVALID_REQUEST, status=400)
    body: dict[str, Any] = {
        "schema": AUTHORITY_SCHEMA,
        "authorityGeneration": gen,
        "previousDigest": previous_digest,
        "createdAt": created_at or _utc_iso(),
        "controlSchemaVersion": int(control_schema_version),
        "policies": [sanitize_policy_for_authority(p) for p in policies],
        "targets": [sanitize_target_for_authority(t) for t in targets],
        "receiptMutationGenerations": {
            str(k): int(v) for k, v in sorted((receipt_mutation_generations or {}).items())
        },
        "promotionEpochs": {str(k): int(v) for k, v in sorted((promotion_epochs or {}).items())},
        "drainGenerations": {str(k): int(v) for k, v in sorted((drain_generations or {}).items())},
        "placementGenerations": {str(k): int(v) for k, v in sorted((placement_generations or {}).items())},
    }
    # Additive control-authority-v1 fields (4.6.5) — no schema version bump.
    if control_boot_epoch is not None:
        body["controlBootEpoch"] = int(control_boot_epoch)
    if mutation_id is not None:
        body["mutationId"] = str(mutation_id)
    # Defense in depth: whole document secret strip before hashing.
    body = strip_secrets(body)
    assert isinstance(body, dict)
    body["payloadDigest"] = compute_payload_digest(body)
    body["digest"] = compute_checkpoint_digest(body)
    _assert_checkpoint_secretless(body)
    return body


def _assert_checkpoint_secretless(checkpoint: dict[str, Any]) -> None:
    blob = _canonical_json(checkpoint).casefold()
    if "age-secret-key-" in blob or "-----begin" in blob:
        raise AppError(
            "control-authority-checkpoint-contains-secrets",
            code=ErrorCode.INTERNAL,
            status=500,
        )


def verify_authority_checkpoint_integrity(checkpoint: dict[str, Any]) -> None:
    """Validate one checkpoint's schema and digests (not full-history contiguity)."""
    if str(checkpoint.get("schema") or "") != AUTHORITY_SCHEMA:
        raise AppError("control-authority-schema-mismatch", code=ErrorCode.INVALID_REQUEST, status=400)
    gen = int(checkpoint.get("authorityGeneration") or 0)
    if gen < 1:
        raise AppError("control-authority-invalid-generation", code=ErrorCode.INVALID_REQUEST, status=400)
    expected_payload = compute_payload_digest(checkpoint)
    if str(checkpoint.get("payloadDigest") or "") != expected_payload:
        raise AppError(
            f"control-authority-payload-digest-mismatch:{gen}",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    expected_digest = compute_checkpoint_digest(checkpoint)
    if str(checkpoint.get("digest") or "") != expected_digest:
        raise AppError(
            f"control-authority-digest-mismatch:{gen}",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )


def verify_authority_chain(checkpoints: list[dict[str, Any]]) -> None:
    """Require contiguous genesis→head hash chain (4.6.4 Gate D)."""
    if not checkpoints:
        raise AppError("control-authority-empty-history", code=ErrorCode.INVALID_REQUEST, status=400)
    ordered = sorted(checkpoints, key=lambda item: int(item.get("authorityGeneration") or 0))
    seen_gen: dict[int, str] = {}
    prev_digest: str | None = None
    prev_gen = 0
    for item in ordered:
        verify_authority_checkpoint_integrity(item)
        gen = int(item.get("authorityGeneration") or 0)
        if gen in seen_gen and seen_gen[gen] != str(item.get("digest") or ""):
            raise AppError(
                f"control-authority-divergent:fork-at-generation-{gen}",
                code=ErrorCode.INVALID_REQUEST,
                status=409,
            )
        if gen < prev_gen:
            raise AppError(
                f"control-authority-rollback:generation-{gen}-after-{prev_gen}",
                code=ErrorCode.INVALID_REQUEST,
                status=409,
            )
        if prev_gen == 0:
            if item.get("previousDigest") not in (None, ""):
                raise AppError(
                    "control-authority-genesis-previous-must-be-null",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )
            if gen != 1:
                raise AppError(
                    f"control-authority-gap:expected-genesis-1-got-{gen}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )
        else:
            if gen != prev_gen + 1:
                raise AppError(
                    f"control-authority-gap:expected-{prev_gen + 1}-got-{gen}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )
            if item.get("previousDigest") != prev_digest:
                raise AppError(
                    f"control-authority-divergent:broken-chain-at-{gen}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )
        seen_gen[gen] = str(item["digest"])
        prev_digest = str(item["digest"])
        prev_gen = gen


def _history_digest_map(history: list[dict[str, Any]] | None) -> dict[int, str]:
    out: dict[int, str] = {}
    for item in history or []:
        if not isinstance(item, dict):
            continue
        gen = int(item.get("authorityGeneration") or 0)
        digest = str(item.get("digest") or "")
        if gen >= 1 and digest:
            out[gen] = digest
    return out


def replica_is_canonical_ancestor(
    *,
    candidate_history: dict[int, str],
    canonical_history: dict[int, str],
    candidate_tip: int,
) -> bool:
    """True when candidate tip is a strict prefix/equal tip of canonical genesis→head history."""
    if candidate_tip < 1:
        return False
    if candidate_tip not in canonical_history:
        return False
    # Tip-only views (no full history loaded): accept when tip digest matches canonical.
    if set(candidate_history.keys()) <= {candidate_tip}:
        return candidate_history.get(candidate_tip) == canonical_history.get(candidate_tip)
    for gen in range(1, candidate_tip + 1):
        cand = candidate_history.get(gen)
        if cand is None:
            continue
        if cand != canonical_history.get(gen):
            return False
    return candidate_history.get(candidate_tip) == canonical_history.get(candidate_tip)


def select_authority_heads(replicas: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Select canonical tip via contiguous history + cross-replica ancestry (4.6.5 Gate D/F).

    Highest generation alone is insufficient: every other valid replica must share the
    same tip or be a strict ancestor (prefix) of the canonical history. Non-ancestor
    forks fail closed as control-authority-divergent.
    """
    if not replicas:
        raise AppError("control-authority-no-replicas", code=ErrorCode.INVALID_REQUEST, status=400)

    views: dict[str, dict[str, Any]] = {}
    for replica_id, head in replicas.items():
        gen = int(head.get("generation") or head.get("authorityGeneration") or 0)
        digest = str(head.get("digest") or "")
        if gen < 1 or not digest:
            raise AppError(
                f"control-authority-invalid-head:{replica_id}",
                code=ErrorCode.INVALID_REQUEST,
                status=400,
            )
        history_list = list(head.get("history") or [])
        if history_list:
            verify_authority_chain(history_list)
        digest_by_gen = _history_digest_map(history_list)
        if not digest_by_gen and digest:
            digest_by_gen = {gen: digest}
        tip_digest = digest_by_gen.get(gen) or digest
        if tip_digest != digest and digest:
            # Head digest must match history tip when both present.
            if gen in digest_by_gen and digest_by_gen[gen] != digest:
                raise AppError(
                    f"control-authority-divergent:head-history-mismatch:{replica_id}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )
        checkpoint = head.get("checkpoint")
        if not isinstance(checkpoint, dict):
            # Prefer full tip checkpoint from history.
            for item in reversed(history_list):
                if int(item.get("authorityGeneration") or 0) == gen:
                    checkpoint = item
                    break
        if not isinstance(checkpoint, dict):
            checkpoint = {
                "authorityGeneration": gen,
                "digest": tip_digest,
                "schema": AUTHORITY_SCHEMA,
            }
        views[replica_id] = {
            "replicaId": replica_id,
            "tipGeneration": gen,
            "tipDigest": tip_digest,
            "historyDigestByGeneration": digest_by_gen,
            "checkpoint": checkpoint,
            "history": history_list,
        }

    # Candidate canonical = unique maximum tip among replicas that carry full checkpoints
    # when available; still require ancestry proof for all others.
    best_gen = max(int(v["tipGeneration"]) for v in views.values())
    at_best = [v for v in views.values() if int(v["tipGeneration"]) == best_gen]
    digests_at_best = {str(v["tipDigest"]) for v in at_best}
    if len(digests_at_best) != 1:
        raise AppError(
            f"control-authority-divergent:generation-{best_gen}",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    canonical = at_best[0]
    # Prefer a replica whose checkpoint has full payload when choosing among equals.
    for item in at_best:
        ckpt = item.get("checkpoint")
        if isinstance(ckpt, dict) and "policies" in ckpt:
            canonical = item
            break
    # Merge generation→digest maps from all replicas (conflict = fork).
    canonical_history: dict[int, str] = {}
    for view in views.values():
        for gen, digest in dict(view["historyDigestByGeneration"]).items():
            existing = canonical_history.get(int(gen))
            if existing is not None and existing != digest:
                raise AppError(
                    f"control-authority-divergent:fork-at-generation-{gen}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )
            canonical_history[int(gen)] = str(digest)
    # Enrich from tip previousDigest links when full history was not loaded.
    tip_ckpt = canonical.get("checkpoint")
    if isinstance(tip_ckpt, dict):
        cursor_gen = int(tip_ckpt.get("authorityGeneration") or 0)
        cursor_prev = tip_ckpt.get("previousDigest")
        if cursor_gen >= 1:
            canonical_history.setdefault(cursor_gen, str(tip_ckpt.get("digest") or canonical["tipDigest"]))
        if cursor_gen >= 2 and cursor_prev not in (None, ""):
            canonical_history.setdefault(cursor_gen - 1, str(cursor_prev))
    if not canonical_history:
        canonical_history = {int(canonical["tipGeneration"]): str(canonical["tipDigest"])}

    lagging: list[str] = []
    for replica_id, view in views.items():
        tip = int(view["tipGeneration"])
        if tip == best_gen and str(view["tipDigest"]) == str(canonical["tipDigest"]):
            continue
        if not replica_is_canonical_ancestor(
            candidate_history=view["historyDigestByGeneration"],
            canonical_history=canonical_history,
            candidate_tip=tip,
        ):
            raise AppError(
                f"control-authority-divergent:non-ancestor:{replica_id}:tip-{tip}",
                code=ErrorCode.INVALID_REQUEST,
                status=409,
            )
        lagging.append(replica_id)

    chosen = canonical["checkpoint"]
    return {
        "generation": best_gen,
        "digest": str(canonical["tipDigest"]),
        "checkpoint": chosen,
        "replicaCount": len(replicas),
        "laggingReplicas": lagging,
        "canonicalReplicaId": str(canonical["replicaId"]),
        "historyViews": views,
        "canonicalHistory": canonical_history,
    }


def _snapshot_authority_from_conn(conn: Any, *, mutation_id: str | None = None) -> dict[str, Any]:
    """Build next checkpoint using an open control DB connection (same TX as mutation)."""
    policy_rows = conn.execute(
        """
        SELECT policy_id, revision, payload_json, promotion_epoch, drain_generation, placement_generation
        FROM control_policies
        ORDER BY policy_id
        """
    ).fetchall()
    policies_raw: list[dict[str, Any]] = []
    promotion_epochs: dict[str, int] = {}
    drain_generations: dict[str, int] = {}
    placement_generations: dict[str, int] = {}
    for row in policy_rows:
        pid = str(row["policy_id"])
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            payload = {"policyId": pid, "policyRevision": int(row["revision"] or 1)}
        if isinstance(payload, dict):
            policies_raw.append(payload)
        promotion_epochs[pid] = int(row["promotion_epoch"] or 0)
        drain_generations[pid] = int(row["drain_generation"] or 0)
        placement_generations[pid] = int(row["placement_generation"] or 0)
    target_rows = conn.execute(
        "SELECT target_id, generation, payload_json FROM control_targets ORDER BY target_id"
    ).fetchall()
    targets_raw: list[dict[str, Any]] = []
    for row in target_rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            payload = {"targetId": str(row["target_id"]), "topologyGeneration": int(row["generation"] or 1)}
        if isinstance(payload, dict):
            targets_raw.append(payload)
    mut_rows = conn.execute(
        "SELECT target_id, generation FROM target_receipt_mutations ORDER BY target_id"
    ).fetchall()
    receipt_mutation_generations = {str(r["target_id"]): int(r["generation"]) for r in mut_rows}
    head = conn.execute(
        "SELECT authority_generation, authority_digest FROM control_authority_head WHERE id = 1"
    ).fetchone()
    boot = conn.execute("SELECT boot_epoch FROM control_boot_state WHERE id = 1").fetchone()
    boot_epoch = int(boot["boot_epoch"]) if boot is not None else 1
    previous_digest = str(head["authority_digest"]) if head is not None else None
    next_gen = int(head["authority_generation"]) + 1 if head is not None else 1
    return build_authority_checkpoint(
        generation=next_gen,
        previous_digest=previous_digest,
        policies=list(policies_raw),
        targets=list(targets_raw),
        receipt_mutation_generations=receipt_mutation_generations,
        promotion_epochs=promotion_epochs,
        drain_generations=drain_generations,
        placement_generations=placement_generations,
        control_schema_version=backup_control.CONTROL_SCHEMA_VERSION,
        control_boot_epoch=boot_epoch,
        mutation_id=mutation_id,
    )


def snapshot_authority_from_control_db(*, mutation_id: str | None = None) -> dict[str, Any]:
    """Build the next authority checkpoint from live non-rebuildable control state."""
    with backup_control._connect() as conn:  # noqa: SLF001 — authority is a control peer module
        return _snapshot_authority_from_conn(conn, mutation_id=mutation_id)


def assert_logical_head_transition(
    *,
    current_generation: int | None,
    current_digest: str | None,
    candidate: dict[str, Any],
) -> None:
    """Logical monotonic CAS precondition (4.6.5 Gate C)."""
    gen = int(candidate.get("authorityGeneration") or 0)
    prev = candidate.get("previousDigest")
    if current_generation is None or int(current_generation) <= 0:
        if gen != 1 or prev not in (None, ""):
            raise AppError(
                "stale-authority-writer:expected-genesis",
                code=ErrorCode.INVALID_REQUEST,
                status=409,
            )
        return
    expected_gen = int(current_generation) + 1
    if gen != expected_gen:
        raise AppError(
            f"stale-authority-writer:expected-generation-{expected_gen}-got-{gen}",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    if str(prev or "") != str(current_digest or ""):
        raise AppError(
            "stale-authority-writer:previous-digest-mismatch",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )


def record_local_authority_head(checkpoint: dict[str, Any], *, conn: Any | None = None) -> None:
    """Persist the latest anchored authority tip with logical monotonic CAS."""
    gen = int(checkpoint["authorityGeneration"])
    digest = str(checkpoint["digest"])
    previous = checkpoint.get("previousDigest")
    payload_digest = str(checkpoint["payloadDigest"])
    now = _utc_iso()

    def _write(active: Any) -> None:
        row = active.execute(
            "SELECT authority_generation, authority_digest FROM control_authority_head WHERE id = 1"
        ).fetchone()
        current_gen = int(row["authority_generation"]) if row is not None else None
        current_digest = str(row["authority_digest"]) if row is not None else None
        # Idempotent replay of the same tip is allowed.
        if (
            current_gen is not None
            and int(current_gen) == gen
            and str(current_digest) == digest
        ):
            return
        assert_logical_head_transition(
            current_generation=current_gen,
            current_digest=current_digest,
            candidate=checkpoint,
        )
        active.execute(
            """
            INSERT INTO control_authority_head(
                id, authority_generation, authority_digest, previous_digest, payload_digest, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                authority_generation = excluded.authority_generation,
                authority_digest = excluded.authority_digest,
                previous_digest = excluded.previous_digest,
                payload_digest = excluded.payload_digest,
                updated_at = excluded.updated_at
            WHERE control_authority_head.authority_generation = excluded.authority_generation - 1
               OR control_authority_head.authority_generation = excluded.authority_generation
            """,
            (gen, digest, previous, payload_digest, now),
        )
        # Verify write took effect (CAS).
        after = active.execute(
            "SELECT authority_generation, authority_digest FROM control_authority_head WHERE id = 1"
        ).fetchone()
        if after is None or int(after["authority_generation"]) != gen or str(after["authority_digest"]) != digest:
            raise AppError(
                "stale-authority-writer:local-head-cas-failed",
                code=ErrorCode.INVALID_REQUEST,
                status=409,
            )

    if conn is not None:
        _write(conn)
        return
    with backup_control._connect() as active:  # noqa: SLF001
        backup_control._begin_immediate(active)  # noqa: SLF001
        _write(active)
        active.execute("COMMIT")


def write_authority_checkpoint_bundle(root: Path, checkpoint: dict[str, Any]) -> dict[str, Path]:
    """Write checkpoint + head.json under a filesystem authority replica root."""
    root = Path(root)
    checkpoints_dir = root / "control" / "authority" / "checkpoints"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    gen = int(checkpoint["authorityGeneration"])
    head_path = root / "control" / "authority" / "head.json"
    current_gen: int | None = None
    current_digest: str | None = None
    if head_path.is_file():
        try:
            head_existing = json.loads(head_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            head_existing = None
        if isinstance(head_existing, dict):
            current_gen = int(head_existing.get("authorityGeneration") or 0) or None
            current_digest = str(head_existing.get("digest") or "") or None
            if current_gen == gen and current_digest == str(checkpoint["digest"]):
                path = checkpoints_dir / f"{gen:016d}.json"
                return {"checkpoint": path, "head": head_path}
    assert_logical_head_transition(
        current_generation=current_gen,
        current_digest=current_digest,
        candidate=checkpoint,
    )
    path = checkpoints_dir / f"{gen:016d}.json"
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        if existing.strip() != (_canonical_json(checkpoint)).strip():
            # Allow identical bytes only.
            try:
                existing_obj = json.loads(existing)
            except json.JSONDecodeError as exc:
                raise AppError(
                    f"control-authority-checkpoint-conflict:{gen}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                ) from exc
            if _canonical_json(existing_obj) != _canonical_json(checkpoint):
                raise AppError(
                    f"control-authority-checkpoint-conflict:{gen}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=409,
                )
    else:
        path.write_text(_canonical_json(checkpoint) + "\n", encoding="utf-8")
    head_path.parent.mkdir(parents=True, exist_ok=True)
    head_doc = {
        "schema": AUTHORITY_SCHEMA,
        "authorityGeneration": gen,
        "digest": checkpoint["digest"],
        "checkpointKey": authority_checkpoint_key(gen),
        "previousDigest": checkpoint.get("previousDigest"),
    }
    head_path.write_text(_canonical_json(head_doc) + "\n", encoding="utf-8")
    return {"checkpoint": path, "head": head_path}


def _put_json_replace(store: Any, key: str, payload: dict[str, Any]) -> None:
    """Put JSON object, replacing head via etag match when already present."""
    raw = (_canonical_json(payload) + "\n").encode("utf-8")
    digest = _sha256_hex(raw)
    meta = store.stat(key)
    if meta is None:
        result = store.put_if_absent(key, raw, checksum_sha256=digest, content_type="application/json")
        if not getattr(result, "created", True) and store.get_bytes(key) != raw:
            raise OSError(f"authority-put-absent-conflict:{key}")
        return
    etag = str(getattr(meta, "etag", None) or "")
    if not etag:
        raise OSError(f"authority-put-missing-etag:{key}")
    store.put_if_match(key, raw, expected_etag=etag, checksum_sha256=digest, content_type="application/json")


def _read_store_head(store: Any) -> dict[str, Any] | None:
    raw = store.get_bytes(authority_head_key())
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_authority_checkpoint_to_store(store: Any, checkpoint: dict[str, Any]) -> dict[str, str]:
    """Write immutable generation checkpoint + logical+physical CAS head (4.6.5 Gate C)."""
    gen = int(checkpoint["authorityGeneration"])
    ckpt_key = authority_checkpoint_key(gen)
    head_key = authority_head_key()
    # Generation object is content-addressed by generation — create-once (before head CAS).
    raw = (_canonical_json(checkpoint) + "\n").encode("utf-8")
    digest = _sha256_hex(raw)
    existing = store.get_bytes(ckpt_key)
    if existing is None:
        pass  # will put after logical checks
    elif existing != raw:
        raise AppError(
            f"control-authority-checkpoint-conflict:{gen}",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    current_head = _read_store_head(store)
    current_gen = int(current_head.get("authorityGeneration") or 0) if current_head else None
    current_digest = str(current_head.get("digest") or "") if current_head else None
    if current_head is not None:
        # Idempotent: same tip already present.
        if current_gen == gen and current_digest == str(checkpoint["digest"]):
            return {"checkpointKey": ckpt_key, "headKey": head_key, "status": "idempotent"}
        assert_logical_head_transition(
            current_generation=current_gen if current_gen and current_gen > 0 else None,
            current_digest=current_digest if current_digest else None,
            candidate=checkpoint,
        )
    else:
        assert_logical_head_transition(
            current_generation=None,
            current_digest=None,
            candidate=checkpoint,
        )
    if existing is None:
        store.put_if_absent(ckpt_key, raw, checksum_sha256=digest, content_type="application/json")
    head_doc = {
        "schema": AUTHORITY_SCHEMA,
        "authorityGeneration": gen,
        "digest": checkpoint["digest"],
        "checkpointKey": ckpt_key,
        "previousDigest": checkpoint.get("previousDigest"),
    }
    _put_json_replace(store, head_key, head_doc)
    return {"checkpointKey": ckpt_key, "headKey": head_key}


def load_authority_bundle_from_store(store: Any, *, replica_id: str = "store") -> dict[str, Any]:
    """Load head + checkpoint history from a Target store authority namespace."""
    head_raw = store.get_bytes(authority_head_key())
    if head_raw is None:
        raise AppError(
            f"control-authority-head-missing:{replica_id}",
            code=ErrorCode.INVALID_REQUEST,
            status=404,
        )
    head = json.loads(head_raw.decode("utf-8"))
    if not isinstance(head, dict):
        raise AppError("control-authority-head-invalid", code=ErrorCode.INVALID_REQUEST, status=400)
    history: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = store.list_objects(f"{AUTHORITY_CHECKPOINT_PREFIX}/", cursor=cursor, limit=200)
        for meta in sorted(page.objects, key=lambda item: str(item.key)):
            raw = store.get_bytes(meta.key)
            if raw is None:
                continue
            try:
                item = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(item, dict) and item.get("digest"):
                history.append(item)
        if page.cursor is None:
            break
        cursor = page.cursor
    history.sort(key=lambda item: int(item.get("authorityGeneration") or 0))
    if history:
        verify_authority_chain(history)
    tip = history[-1] if history else None
    if tip is not None and str(tip.get("digest")) != str(head.get("digest")):
        raise AppError(
            "control-authority-head-checkpoint-mismatch",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    return {"head": head, "history": history, "checkpoint": tip, "replicaId": replica_id}


def load_authority_bundle(root: Path) -> dict[str, Any]:
    """Load head + full checkpoint history from a filesystem authority replica."""
    root = Path(root)
    head_path = root / "control" / "authority" / "head.json"
    if not head_path.is_file():
        raise AppError(
            f"control-authority-head-missing:{root}",
            code=ErrorCode.INVALID_REQUEST,
            status=404,
        )
    head = json.loads(head_path.read_text(encoding="utf-8"))
    checkpoints_dir = root / "control" / "authority" / "checkpoints"
    history: list[dict[str, Any]] = []
    if checkpoints_dir.is_dir():
        for path in sorted(checkpoints_dir.glob("*.json")):
            history.append(json.loads(path.read_text(encoding="utf-8")))
    if history:
        verify_authority_chain(history)
    tip = history[-1] if history else None
    if tip is not None and str(tip.get("digest")) != str(head.get("digest")):
        raise AppError(
            "control-authority-head-checkpoint-mismatch",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )
    return {"head": head, "history": history, "checkpoint": tip}


def apply_authority_checkpoint_to_fresh_db(checkpoint: dict[str, Any]) -> None:
    """Replay non-rebuildable authority into the current control DB (recovery use)."""
    # Tip integrity only — full genesis→head was validated when loading replica history.
    verify_authority_checkpoint_integrity(checkpoint)
    _assert_checkpoint_secretless(checkpoint)
    now = _utc_iso()
    policies = list(checkpoint.get("policies") or [])
    targets = list(checkpoint.get("targets") or [])
    promotion = dict(checkpoint.get("promotionEpochs") or {})
    drain = dict(checkpoint.get("drainGenerations") or {})
    placement = dict(checkpoint.get("placementGenerations") or {})
    mutations = dict(checkpoint.get("receiptMutationGenerations") or {})
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        for policy in policies:
            if not isinstance(policy, dict):
                continue
            clean = sanitize_policy_for_authority(policy)
            policy_id = str(clean.get("policyId") or "")
            if not policy_id:
                continue
            revision = max(1, int(clean.get("policyRevision") or 1))
            conn.execute(
                """
                INSERT INTO control_policies(
                    policy_id, revision, payload_json, topology_generation,
                    promotion_epoch, drain_generation, placement_generation, updated_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    revision = excluded.revision,
                    payload_json = excluded.payload_json,
                    promotion_epoch = excluded.promotion_epoch,
                    drain_generation = excluded.drain_generation,
                    placement_generation = excluded.placement_generation,
                    updated_at = excluded.updated_at
                """,
                (
                    policy_id,
                    revision,
                    _canonical_json(clean),
                    int(promotion.get(policy_id) or 0),
                    int(drain.get(policy_id) or 0),
                    int(placement.get(policy_id) or 0),
                    now,
                ),
            )
        for target in targets:
            if not isinstance(target, dict):
                continue
            clean = sanitize_target_for_authority(target)
            target_id = str(clean.get("targetId") or "")
            if not target_id:
                continue
            generation = max(1, int(clean.get("topologyGeneration") or 1))
            conn.execute(
                """
                INSERT INTO control_targets(target_id, generation, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    generation = excluded.generation,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (target_id, generation, _canonical_json(clean), now),
            )
        for target_id, gen in mutations.items():
            conn.execute(
                """
                INSERT INTO target_receipt_mutations(target_id, generation, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    generation = excluded.generation,
                    updated_at = excluded.updated_at
                """,
                (str(target_id), int(gen), now),
            )
        conn.execute(
            """
            INSERT INTO control_authority_head(
                id, authority_generation, authority_digest, previous_digest, payload_digest, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                authority_generation = excluded.authority_generation,
                authority_digest = excluded.authority_digest,
                previous_digest = excluded.previous_digest,
                payload_digest = excluded.payload_digest,
                updated_at = excluded.updated_at
            """,
            (
                int(checkpoint["authorityGeneration"]),
                str(checkpoint["digest"]),
                checkpoint.get("previousDigest"),
                str(checkpoint["payloadDigest"]),
                now,
            ),
        )
        conn.execute("COMMIT")


def configure_authority_anchor_roots(roots: list[Path | str] | None) -> list[Path]:
    """Configure filesystem authority replicas used for RPO=0 mutation anchoring."""
    global _AUTHORITY_ANCHOR_ROOTS
    if not roots:
        _AUTHORITY_ANCHOR_ROOTS = []
    else:
        _AUTHORITY_ANCHOR_ROOTS = [Path(item) for item in roots if str(item).strip()]
    return list(_AUTHORITY_ANCHOR_ROOTS)


def get_authority_anchor_roots() -> list[Path]:
    return list(_AUTHORITY_ANCHOR_ROOTS)


def configure_authority_anchor_stores(stores: list[Any] | None) -> int:
    """Configure Target-store authority replicas (real MinIO/S3) for RPO=0 anchoring."""
    global _AUTHORITY_ANCHOR_STORES
    _AUTHORITY_ANCHOR_STORES = list(stores or [])
    return len(_AUTHORITY_ANCHOR_STORES)


def get_authority_anchor_stores() -> list[Any]:
    return list(_AUTHORITY_ANCHOR_STORES)


def authority_anchors_configured() -> bool:
    return bool(_AUTHORITY_ANCHOR_ROOTS or _AUTHORITY_ANCHOR_STORES)


def pending_authority_outbox_count() -> int:
    with backup_control._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM control_authority_outbox WHERE state = ?",
            (OUTBOX_PENDING,),
        ).fetchone()
        mut = 0
        try:
            mut_row = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM control_authority_mutations
                WHERE state IN ({",".join("?" for _ in UNRESOLVED_MUTATION_STATES)})
                """,
                tuple(sorted(UNRESOLVED_MUTATION_STATES)),
            ).fetchone()
            mut = int(mut_row["c"] if mut_row is not None else 0)
        except Exception:
            # Pre-v8 databases may lack the journal table during migration edge windows.
            mut = 0
    return int(row["c"] if row is not None else 0) + mut


def unresolved_authority_mutation_count() -> int:
    with backup_control._connect() as conn:  # noqa: SLF001
        try:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM control_authority_mutations
                WHERE state IN ({",".join("?" for _ in UNRESOLVED_MUTATION_STATES)})
                """,
                tuple(sorted(UNRESOLVED_MUTATION_STATES)),
            ).fetchone()
        except Exception:
            return 0
    return int(row["c"] if row is not None else 0)


def _enqueue_authority_outbox(*, kind: str, checkpoint: dict[str, Any], conn: Any | None = None) -> str:
    outbox_id = f"auth_{secrets.token_hex(12)}"
    now = _utc_iso()

    def _insert(active: Any) -> None:
        active.execute(
            """
            INSERT INTO control_authority_outbox(
                outbox_id, kind, checkpoint_json, state, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (outbox_id, str(kind), _canonical_json(checkpoint), OUTBOX_PENDING, now, now),
        )

    if conn is not None:
        _insert(conn)
        return outbox_id
    with backup_control._connect() as active:  # noqa: SLF001
        backup_control._begin_immediate(active)  # noqa: SLF001
        _insert(active)
        active.execute("COMMIT")
    return outbox_id


def prepare_authority_mutation_in_tx(conn: Any, *, kind: str) -> dict[str, Any]:
    """Insert PREPARED mutation + pending outbox in the caller's IMMEDIATE transaction.

    Local non-rebuildable mutation and this intent MUST share one SQLite commit.
    """
    mutation_id = f"mut_{secrets.token_hex(12)}"
    checkpoint = _snapshot_authority_from_conn(conn, mutation_id=mutation_id)
    now = _utc_iso()
    conn.execute(
        """
        INSERT INTO control_authority_mutations(
            mutation_id, authority_generation, authority_digest, kind,
            checkpoint_json, state, error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            mutation_id,
            int(checkpoint["authorityGeneration"]),
            str(checkpoint["digest"]),
            str(kind),
            _canonical_json(checkpoint),
            MUTATION_PREPARED,
            now,
            now,
        ),
    )
    outbox_id = _enqueue_authority_outbox(kind=kind, checkpoint=checkpoint, conn=conn)
    return {
        "mutationId": mutation_id,
        "outboxId": outbox_id,
        "checkpoint": checkpoint,
        "state": MUTATION_PREPARED,
    }


def _mark_outbox(outbox_id: str, *, state: str, error: str | None = None) -> None:
    now = _utc_iso()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        conn.execute(
            """
            UPDATE control_authority_outbox
            SET state = ?, error = ?, updated_at = ?
            WHERE outbox_id = ?
            """,
            (state, error, now, outbox_id),
        )
        conn.execute("COMMIT")


def _mark_mutation(
    mutation_id: str,
    *,
    state: str,
    error: str | None = None,
    conn: Any | None = None,
) -> None:
    now = _utc_iso()

    def _update(active: Any) -> None:
        active.execute(
            """
            UPDATE control_authority_mutations
            SET state = ?, error = ?, updated_at = ?
            WHERE mutation_id = ?
            """,
            (state, error, now, mutation_id),
        )

    if conn is not None:
        _update(conn)
        return
    with backup_control._connect() as active:  # noqa: SLF001
        backup_control._begin_immediate(active)  # noqa: SLF001
        _update(active)
        active.execute("COMMIT")


def _raise_anchor(errors: list[str]) -> None:
    detail = ";".join(errors[:3]) if errors else "no-replicas"
    raise AppError(
        f"authority-rpo-zero-anchor-failed:{detail}",
        code=ErrorCode.INTERNAL,
        status=503,
    )


def _write_checkpoint_to_roots(checkpoint: dict[str, Any], roots: list[Path]) -> list[str]:
    durable: list[str] = []
    errors: list[str] = []
    for root in roots:
        try:
            write_authority_checkpoint_bundle(root, checkpoint)
            durable.append(str(root))
        except OSError as exc:
            errors.append(f"{root}:{exc}")
    if durable:
        return durable
    if errors:
        _raise_anchor(errors)
    return []


def _write_checkpoint_to_stores(checkpoint: dict[str, Any], stores: list[Any]) -> list[str]:
    durable: list[str] = []
    errors: list[str] = []
    for index, store in enumerate(stores):
        label = f"store-{index}"
        try:
            write_authority_checkpoint_to_store(store, checkpoint)
            durable.append(label)
        except (OSError, AppError) as exc:
            errors.append(f"{label}:{exc}")
    if durable:
        return durable
    if errors:
        _raise_anchor(errors)
    return []


def _complete_anchor_after_remote(
    *,
    checkpoint: dict[str, Any],
    outbox_id: str,
    mutation_id: str | None,
    kind: str,
    durable_roots: list[str],
    durable_stores: list[str],
) -> dict[str, Any]:
    record_local_authority_head(checkpoint)
    _mark_outbox(outbox_id, state=OUTBOX_DURABLE)
    if mutation_id:
        _mark_mutation(mutation_id, state=MUTATION_DURABLE)
    return {
        "status": "anchored",
        "outboxId": outbox_id,
        "mutationId": mutation_id,
        "kind": kind,
        "authorityGeneration": int(checkpoint["authorityGeneration"]),
        "authorityDigest": str(checkpoint["digest"]),
        "durableRoots": durable_roots,
        "durableStores": durable_stores,
        "rpo": 0,
    }


def anchor_prepared_mutation(
    *,
    checkpoint: dict[str, Any],
    outbox_id: str,
    mutation_id: str | None = None,
    kind: str = "authority-mutation",
    roots: list[Path | str] | None = None,
    stores: list[Any] | None = None,
    rpo_zero: bool = True,
    min_durable: int | None = None,
) -> dict[str, Any]:
    """Anchor a PREPARED checkpoint to replicas (idempotent; handles remote-outcome-unknown)."""
    from deepseek_infra.infra.workspace import backup_authority_provider

    fs_roots = [Path(item) for item in roots] if roots is not None else get_authority_anchor_roots()
    store_list = list(stores) if stores is not None else get_authority_anchor_stores()
    required = int(min_durable) if min_durable is not None else backup_authority_provider.min_durable_replicas()
    if mutation_id:
        _mark_mutation(mutation_id, state=MUTATION_ANCHORING)
    if not fs_roots and not store_list:
        if rpo_zero:
            _mark_outbox(outbox_id, state=OUTBOX_FAILED, error="no-authority-anchor-roots")
            if mutation_id:
                _mark_mutation(mutation_id, state=MUTATION_REMOTE_OUTCOME_UNKNOWN, error="no-roots")
            raise AppError(
                "authority-rpo-zero-anchor-failed:no-roots",
                code=ErrorCode.INVALID_REQUEST,
                status=503,
            )
        _mark_outbox(outbox_id, state=OUTBOX_FAILED, error="anchor-skipped-no-roots")
        if mutation_id:
            _mark_mutation(mutation_id, state=MUTATION_SUPERSEDED, error="no-roots")
        return {
            "status": "skipped",
            "reason": "no-authority-anchor-roots",
            "outboxId": outbox_id,
            "mutationId": mutation_id,
            "authorityGeneration": int(checkpoint["authorityGeneration"]),
        }
    durable_roots: list[str] = []
    durable_stores: list[str] = []
    try:
        if fs_roots:
            durable_roots = _write_checkpoint_to_roots(checkpoint, fs_roots)
        if store_list:
            durable_stores = _write_checkpoint_to_stores(checkpoint, store_list)
        durable_count = len(durable_roots) + len(durable_stores)
        if durable_count <= 0:
            raise AppError(
                "authority-rpo-zero-anchor-failed:no-durable-replica",
                code=ErrorCode.INTERNAL,
                status=503,
            )
        if durable_count < required:
            raise AppError(
                f"authority-durability-unsatisfied:need-{required}-got-{durable_count}",
                code=ErrorCode.INTERNAL,
                status=503,
            )
    except AppError as exc:
        # Timeout / ambiguous failure: mark remote-outcome-unknown for reconcile.
        if mutation_id:
            _mark_mutation(mutation_id, state=MUTATION_REMOTE_OUTCOME_UNKNOWN, error=str(exc))
        _mark_outbox(outbox_id, state=OUTBOX_PENDING, error=str(exc))
        if rpo_zero:
            raise
        return {
            "status": "failed",
            "error": str(exc),
            "outboxId": outbox_id,
            "mutationId": mutation_id,
            "authorityGeneration": int(checkpoint["authorityGeneration"]),
        }
    except OSError as exc:
        if mutation_id:
            _mark_mutation(mutation_id, state=MUTATION_REMOTE_OUTCOME_UNKNOWN, error=str(exc))
        _mark_outbox(outbox_id, state=OUTBOX_PENDING, error=str(exc))
        if rpo_zero:
            raise AppError(
                f"authority-rpo-zero-anchor-failed:{exc}",
                code=ErrorCode.INTERNAL,
                status=503,
            ) from exc
        return {
            "status": "failed",
            "error": str(exc),
            "outboxId": outbox_id,
            "mutationId": mutation_id,
            "authorityGeneration": int(checkpoint["authorityGeneration"]),
        }
    completed = _complete_anchor_after_remote(
        checkpoint=checkpoint,
        outbox_id=outbox_id,
        mutation_id=mutation_id,
        kind=kind,
        durable_roots=durable_roots,
        durable_stores=durable_stores,
    )
    completed["minDurableReplicas"] = required
    completed["durableCount"] = len(durable_roots) + len(durable_stores)
    return completed


def anchor_non_rebuildable_mutation(
    *,
    kind: str,
    roots: list[Path | str] | None = None,
    stores: list[Any] | None = None,
    rpo_zero: bool = True,
    prepared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Durably anchor the next authority generation before operator acknowledgement.

    RPO=0: when ``rpo_zero`` and any roots/stores are configured (or passed), ≥1
    replica must accept the checkpoint or the call fails closed. Local head
    advances only after a durable replica write.

    When ``prepared`` is supplied (from prepare_authority_mutation_in_tx), the
    local mutation already committed with the intent; only remote anchor runs.
    """
    if prepared is not None:
        checkpoint = prepared["checkpoint"]
        assert isinstance(checkpoint, dict)
        return anchor_prepared_mutation(
            checkpoint=checkpoint,
            outbox_id=str(prepared["outboxId"]),
            mutation_id=str(prepared.get("mutationId") or "") or None,
            kind=kind,
            roots=roots,
            stores=stores,
            rpo_zero=rpo_zero,
        )
    # Legacy path: snapshot + prepare outbox/journal then anchor (post-commit callers).
    fs_roots = [Path(item) for item in roots] if roots is not None else get_authority_anchor_roots()
    store_list = list(stores) if stores is not None else get_authority_anchor_stores()
    mutation_id = f"mut_{secrets.token_hex(12)}"
    checkpoint = snapshot_authority_from_control_db(mutation_id=mutation_id)
    now = _utc_iso()
    with backup_control._connect() as conn:  # noqa: SLF001
        backup_control._begin_immediate(conn)  # noqa: SLF001
        try:
            conn.execute(
                """
                INSERT INTO control_authority_mutations(
                    mutation_id, authority_generation, authority_digest, kind,
                    checkpoint_json, state, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    mutation_id,
                    int(checkpoint["authorityGeneration"]),
                    str(checkpoint["digest"]),
                    str(kind),
                    _canonical_json(checkpoint),
                    MUTATION_PREPARED,
                    now,
                    now,
                ),
            )
        except Exception:
            # Pre-v8 or table missing: fall back to outbox-only.
            pass
        outbox_id = _enqueue_authority_outbox(kind=kind, checkpoint=checkpoint, conn=conn)
        conn.execute("COMMIT")
    return anchor_prepared_mutation(
        checkpoint=checkpoint,
        outbox_id=outbox_id,
        mutation_id=mutation_id,
        kind=kind,
        roots=list(fs_roots),
        stores=store_list,
        rpo_zero=rpo_zero,
    )


def reconcile_remote_outcome_unknown(*, rpo_zero: bool = True) -> dict[str, Any]:
    """Re-read remote heads and mark PREPARED/UNKNOWN mutations durable when digests match."""
    with backup_control._connect() as conn:  # noqa: SLF001
        try:
            rows = conn.execute(
                """
                SELECT mutation_id, kind, checkpoint_json, state
                FROM control_authority_mutations
                WHERE state IN (?, ?, ?)
                ORDER BY created_at ASC
                """,
                (MUTATION_PREPARED, MUTATION_ANCHORING, MUTATION_REMOTE_OUTCOME_UNKNOWN),
            ).fetchall()
        except Exception:
            return {"reconciled": 0, "pending": 0}
    reconciled = 0
    roots = get_authority_anchor_roots()
    stores = get_authority_anchor_stores()
    for row in rows:
        try:
            checkpoint = json.loads(str(row["checkpoint_json"]))
        except json.JSONDecodeError:
            _mark_mutation(str(row["mutation_id"]), state=MUTATION_DIVERGENT, error="invalid-json")
            continue
        if not isinstance(checkpoint, dict):
            _mark_mutation(str(row["mutation_id"]), state=MUTATION_DIVERGENT, error="invalid-json")
            continue
        gen = int(checkpoint.get("authorityGeneration") or 0)
        digest = str(checkpoint.get("digest") or "")
        found = False
        for root in roots:
            try:
                bundle = load_authority_bundle(root)
            except AppError:
                continue
            tip = bundle.get("checkpoint")
            if isinstance(tip, dict) and int(tip.get("authorityGeneration") or 0) == gen and str(tip.get("digest")) == digest:
                found = True
                break
        if not found:
            for index, store in enumerate(stores):
                try:
                    bundle = load_authority_bundle_from_store(store, replica_id=f"store-{index}")
                except AppError:
                    continue
                tip = bundle.get("checkpoint")
                if (
                    isinstance(tip, dict)
                    and int(tip.get("authorityGeneration") or 0) == gen
                    and str(tip.get("digest")) == digest
                ):
                    found = True
                    break
        if found:
            record_local_authority_head(checkpoint)
            _mark_mutation(str(row["mutation_id"]), state=MUTATION_DURABLE)
            # Mark matching outbox durable if present.
            with backup_control._connect() as conn:  # noqa: SLF001
                conn.execute(
                    """
                    UPDATE control_authority_outbox
                    SET state = ?, updated_at = ?
                    WHERE state = ? AND checkpoint_json = ?
                    """,
                    (OUTBOX_DURABLE, _utc_iso(), OUTBOX_PENDING, _canonical_json(checkpoint)),
                )
            reconciled += 1
            continue
        # Not on remote yet — retry anchor.
        try:
            outbox_id = _enqueue_authority_outbox(kind=str(row["kind"]), checkpoint=checkpoint)
            anchor_prepared_mutation(
                checkpoint=checkpoint,
                outbox_id=outbox_id,
                mutation_id=str(row["mutation_id"]),
                kind=str(row["kind"]),
                rpo_zero=rpo_zero,
            )
            reconciled += 1
        except AppError:
            if rpo_zero:
                raise
    return {
        "reconciled": reconciled,
        "pending": unresolved_authority_mutation_count(),
    }


def repair_lagging_authority_replicas(
    *,
    canonical_history: list[dict[str, Any]],
    lagging: list[dict[str, Any]],
) -> dict[str, Any]:
    """Copy immutable canonical checkpoint bytes onto lagging ancestor replicas (Gate E)."""
    if not canonical_history:
        raise AppError("control-authority-empty-history", code=ErrorCode.INVALID_REQUEST, status=400)
    verify_authority_chain(canonical_history)
    tip = canonical_history[-1]
    repaired: list[str] = []
    for item in lagging:
        replica_id = str(item.get("replicaId") or item.get("id") or "lagging")
        tip_gen = int(item.get("tipGeneration") or item.get("generation") or 0)
        store = item.get("store")
        root = item.get("root")
        # Copy only generations tip_gen+1 .. canonical tip (immutable bytes).
        for ckpt in canonical_history:
            gen = int(ckpt["authorityGeneration"])
            if gen <= tip_gen:
                continue
            if store is not None:
                write_authority_checkpoint_to_store(store, ckpt)
            elif root is not None:
                write_authority_checkpoint_bundle(Path(root), ckpt)
            else:
                raise AppError(
                    f"control-authority-repair-missing-handle:{replica_id}",
                    code=ErrorCode.INVALID_REQUEST,
                    status=400,
                )
        repaired.append(replica_id)
    return {
        "repaired": repaired,
        "canonicalGeneration": int(tip["authorityGeneration"]),
        "canonicalDigest": str(tip["digest"]),
    }


def drain_pending_authority_outbox(*, rpo_zero: bool = True) -> dict[str, Any]:
    """Retry pending outbox + unresolved mutation journal (crash window)."""
    try:
        reconcile_remote_outcome_unknown(rpo_zero=False)
    except AppError:
        pass
    with backup_control._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            """
            SELECT outbox_id, kind, checkpoint_json FROM control_authority_outbox
            WHERE state = ?
            ORDER BY created_at ASC
            """,
            (OUTBOX_PENDING,),
        ).fetchall()
    drained = 0
    failed = 0
    roots = get_authority_anchor_roots()
    stores = get_authority_anchor_stores()
    for row in rows:
        try:
            checkpoint = json.loads(str(row["checkpoint_json"]))
        except json.JSONDecodeError:
            _mark_outbox(str(row["outbox_id"]), state=OUTBOX_FAILED, error="invalid-checkpoint-json")
            failed += 1
            continue
        if not isinstance(checkpoint, dict):
            _mark_outbox(str(row["outbox_id"]), state=OUTBOX_FAILED, error="invalid-checkpoint-json")
            failed += 1
            continue
        if not roots and not stores:
            _mark_outbox(str(row["outbox_id"]), state=OUTBOX_FAILED, error="no-roots")
            failed += 1
            if rpo_zero:
                raise AppError(
                    "authority-rpo-zero-anchor-failed:no-roots",
                    code=ErrorCode.INVALID_REQUEST,
                    status=503,
                )
            continue
        try:
            anchor_prepared_mutation(
                checkpoint=checkpoint,
                outbox_id=str(row["outbox_id"]),
                mutation_id=str(checkpoint.get("mutationId") or "") or None,
                kind=str(row["kind"]),
                rpo_zero=rpo_zero,
            )
            drained += 1
        except AppError as exc:
            _mark_outbox(str(row["outbox_id"]), state=OUTBOX_FAILED, error=str(exc))
            failed += 1
            if rpo_zero:
                raise
    return {"drained": drained, "failed": failed, "pending": pending_authority_outbox_count()}
