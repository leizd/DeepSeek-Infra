//! Real temporary SQLite admission/restart tests, not provider-effect evidence.
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_protocol::ActionFence;
use deepseek_worker::{StorageOperationCommand, Worker, WorkerAuthorityConfig};
use ed25519_dalek::{Signer as _, SigningKey};
use serde_json::Value;
use sha2::{Digest, Sha256};

#[path = "durable_operation_grants/process_restart.rs"]
mod process_restart;

struct Fixture {
    config: WorkerAuthorityConfig,
    grant: Value,
    epoch: Value,
}

fn fixture() -> Fixture {
    let grant: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v31/control/storage_operation_grant_vector.json"
    )))
    .unwrap();
    let install: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v7/control/authority_request_vector.json"
    )))
    .unwrap();
    let document: Value =
        serde_json::from_str(grant["canonical_request"].as_str().unwrap()).unwrap();
    let mut epoch: Value =
        serde_json::from_str(install["canonical_request"].as_str().unwrap()).unwrap();
    epoch["requestId"] = "1".repeat(64).into();
    epoch["nonce"] = "2".repeat(64).into();
    epoch["issuedAt"] = document["issuedAt"].clone();
    epoch["expiresAt"] = document["expiresAt"].clone();
    Fixture {
        config: WorkerAuthorityConfig {
            signer_public_key: grant["signer_public_key"].as_str().unwrap().into(),
            fleet_id: "fleet-a".into(),
            environment: "test".into(),
            fencing_token: 4,
            now: Some(grant["now"].as_str().unwrap().into()),
        },
        grant: document,
        epoch,
    }
}

fn canonical(value: &Value) -> Vec<u8> {
    fn sorted(value: &Value) -> Value {
        match value {
            Value::Object(map) => {
                let ordered: std::collections::BTreeMap<_, _> = map
                    .iter()
                    .map(|(key, value)| (key.clone(), sorted(value)))
                    .collect();
                serde_json::to_value(ordered).unwrap()
            }
            Value::Array(values) => Value::Array(values.iter().map(sorted).collect()),
            other => other.clone(),
        }
    }
    // Remain canonical even if another crate enables serde_json/preserve_order.
    serde_json::to_vec(&sorted(value)).unwrap()
}

fn signed(document: &Value, grant: bool) -> Vec<u8> {
    // Published RFC 8032 test vector, not a deployed/private runtime credential.
    let seed_hex = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
    let mut seed = [0_u8; 32];
    for (index, pair) in seed_hex.as_bytes().chunks_exact(2).enumerate() {
        seed[index] = u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap();
    }
    let mut document = document.clone();
    document.as_object_mut().unwrap().remove("signature");
    document.as_object_mut().unwrap().remove("digest");
    if grant {
        document["payloadDigest"] = format!(
            "sha256:{:x}",
            Sha256::digest(canonical(&document["payload"]))
        )
        .into();
    }
    document["digest"] = format!("sha256:{:x}", Sha256::digest(canonical(&document))).into();
    let domain = if grant {
        "deepseek-infra:control-storage-operation-grant-v1\0"
    } else {
        "deepseek-infra:control-authority-request-v1\0"
    };
    let mut message = domain.as_bytes().to_vec();
    message.extend(canonical(&document));
    document["signature"] = URL_SAFE_NO_PAD
        .encode(SigningKey::from_bytes(&seed).sign(&message).to_bytes())
        .into();
    canonical(&document)
}

fn fence() -> ActionFence {
    ActionFence {
        action_id: "act-1".into(),
        execution_epoch: 4,
    }
}

fn command(document: &Value) -> StorageOperationCommand<'_> {
    let p = &document["payload"];
    StorageOperationCommand {
        action_id: document["actionId"].as_str().unwrap(),
        execution_epoch: document["executionEpoch"].as_u64().unwrap(),
        operation_id: document["operationId"].as_str().unwrap(),
        mutation_type: p["mutationType"].as_str().unwrap(),
        provider: p["provider"].as_str().unwrap(),
        target_identity: p["targetIdentity"].as_str().unwrap(),
        bucket: p["bucket"].as_str().unwrap(),
        prefix: p["prefix"].as_str().unwrap(),
        object_key: p["objectKey"].as_str().unwrap(),
        object_digest: p["objectDigest"].as_str().unwrap(),
        expected_length: p["expectedLength"].as_u64().unwrap(),
        condition_type: p["conditionType"].as_str().unwrap(),
        expected_etag: p["expectedEtag"].as_str().unwrap(),
        claim_revision: p["claimRevision"].as_i64().unwrap(),
    }
}

fn open_installed(root: &std::path::Path, f: &Fixture) -> Worker {
    assert_eq!(signed(&f.grant, true), canonical(&f.grant));
    let mut worker = Worker::open_with_authority(f.config.clone(), root).unwrap();
    worker
        .install_signed_epoch(&fence(), &signed(&f.epoch, false))
        .unwrap();
    worker
}

#[test]
fn restart_retains_grant_request_nonce_and_operation_conflicts() {
    for (kind, expected) in [
        ("request", "STORAGE_OPERATION_GRANT_REPLAY"),
        ("nonce", "STORAGE_OPERATION_GRANT_NONCE_REUSE"),
        ("operation", "STORAGE_OPERATION_GRANT_REPLAY_CONFLICT"),
    ] {
        let root = tempfile::tempdir().unwrap();
        let f = fixture();
        let mut worker = open_installed(root.path(), &f);
        worker
            .admit_storage_operation_grant(&signed(&f.grant, true), &command(&f.grant))
            .unwrap();
        drop(worker);
        let mut reopened = Worker::open_with_authority(f.config, root.path()).unwrap();
        let mut substitute = f.grant.clone();
        if kind != "request" {
            substitute["requestId"] = "3".repeat(64).into();
        }
        if kind != "nonce" {
            substitute["nonce"] = "4".repeat(64).into();
        }
        substitute["payload"]["objectKey"] = "objects/replacement".into();
        assert_eq!(
            reopened
                .admit_storage_operation_grant(&signed(&substitute, true), &command(&substitute))
                .unwrap_err()
                .code,
            expected,
            "{kind}"
        );
    }
}

#[test]
fn exact_retry_after_reopen_still_expires_at_the_signed_deadline() {
    let root = tempfile::tempdir().unwrap();
    let mut f = fixture();
    let mut worker = open_installed(root.path(), &f);
    let raw = signed(&f.grant, true);
    worker
        .admit_storage_operation_grant(&raw, &command(&f.grant))
        .unwrap();
    drop(worker);
    let mut reopened = Worker::open_with_authority(f.config.clone(), root.path()).unwrap();
    reopened
        .admit_storage_operation_grant(&raw, &command(&f.grant))
        .unwrap();
    drop(reopened);
    f.config.now = Some(f.grant["expiresAt"].as_str().unwrap().into());
    let mut expired = Worker::open_with_authority(f.config, root.path()).unwrap();
    assert_eq!(
        expired
            .admit_storage_operation_grant(&raw, &command(&f.grant))
            .unwrap_err()
            .code,
        "STORAGE_OPERATION_GRANT_EXPIRED"
    );
}

#[test]
fn old_open_handle_cannot_readmit_a_grant_after_writer_takeover() {
    let root = tempfile::tempdir().unwrap();
    let mut f = fixture();
    let mut old = open_installed(root.path(), &f);
    let raw = signed(&f.grant, true);
    old.admit_storage_operation_grant(&raw, &command(&f.grant))
        .unwrap();
    f.config.fencing_token = 5;
    let _successor = Worker::open_with_authority(f.config, root.path()).unwrap();
    assert_eq!(
        old.admit_storage_operation_grant(&raw, &command(&f.grant))
            .unwrap_err()
            .code,
        "STORAGE_OPERATION_GRANT_STALE_FENCING_TOKEN"
    );
}

#[test]
fn grants_cannot_reuse_durable_epoch_request_or_nonce() {
    for (field, expected) in [
        ("requestId", "STORAGE_OPERATION_GRANT_REPLAY"),
        ("nonce", "STORAGE_OPERATION_GRANT_NONCE_REUSE"),
    ] {
        let root = tempfile::tempdir().unwrap();
        let f = fixture();
        let mut worker = open_installed(root.path(), &f);
        let mut substitute = f.grant.clone();
        substitute[field] = f.epoch[field].clone();
        assert_eq!(
            worker
                .admit_storage_operation_grant(&signed(&substitute, true), &command(&substitute))
                .unwrap_err()
                .code,
            expected
        );
    }
}

#[test]
fn epoch_install_cannot_reuse_a_durably_admitted_grant_nonce() {
    let root = tempfile::tempdir().unwrap();
    let f = fixture();
    let mut worker = open_installed(root.path(), &f);
    worker
        .admit_storage_operation_grant(&signed(&f.grant, true), &command(&f.grant))
        .unwrap();
    drop(worker);
    let mut reopened = Worker::open_with_authority(f.config, root.path()).unwrap();
    let mut next = f.epoch;
    next["executionEpoch"] = 5.into();
    next["requestId"] = "5".repeat(64).into();
    next["nonce"] = f.grant["nonce"].clone();
    let next_fence = ActionFence {
        execution_epoch: 5,
        ..fence()
    };
    assert_eq!(
        reopened
            .install_signed_epoch(&next_fence, &signed(&next, false))
            .unwrap_err()
            .code,
        "AUTHORITY_REQUEST_NONCE_REUSE"
    );
}

#[test]
fn persisted_operation_id_cannot_move_to_another_action_or_epoch() {
    for another_action in [false, true] {
        let root = tempfile::tempdir().unwrap();
        let f = fixture();
        let mut worker = open_installed(root.path(), &f);
        worker
            .admit_storage_operation_grant(&signed(&f.grant, true), &command(&f.grant))
            .unwrap();
        drop(worker);
        let mut reopened = Worker::open_with_authority(f.config, root.path()).unwrap();
        let mut install = f.epoch;
        let next_fence = if another_action {
            ActionFence {
                action_id: "act-2".into(),
                ..fence()
            }
        } else {
            ActionFence {
                execution_epoch: 5,
                ..fence()
            }
        };
        install["actionId"] = next_fence.action_id.clone().into();
        install["executionEpoch"] = next_fence.execution_epoch.into();
        install["requestId"] = "6".repeat(64).into();
        install["nonce"] = "7".repeat(64).into();
        reopened
            .install_signed_epoch(&next_fence, &signed(&install, false))
            .unwrap();
        let mut grant = f.grant;
        grant["actionId"] = next_fence.action_id.into();
        grant["executionEpoch"] = next_fence.execution_epoch.into();
        grant["requestId"] = "8".repeat(64).into();
        grant["nonce"] = "9".repeat(64).into();
        assert_eq!(
            reopened
                .admit_storage_operation_grant(&signed(&grant, true), &command(&grant))
                .unwrap_err()
                .code,
            "STORAGE_OPERATION_GRANT_REPLAY_CONFLICT"
        );
    }
}

#[test]
fn rejected_grant_never_consumes_a_durable_request_or_nonce() {
    let root = tempfile::tempdir().unwrap();
    let f = fixture();
    let mut worker = open_installed(root.path(), &f);
    let mut invalid = f.grant.clone();
    invalid["signature"] = "invalid".into();
    assert_eq!(
        worker
            .admit_storage_operation_grant(&canonical(&invalid), &command(&f.grant))
            .unwrap_err()
            .code,
        "STORAGE_OPERATION_GRANT_SIGNATURE_INVALID"
    );
    drop(worker);
    let mut reopened = Worker::open_with_authority(f.config, root.path()).unwrap();
    reopened
        .admit_storage_operation_grant(&signed(&f.grant, true), &command(&f.grant))
        .unwrap();
    let database = rusqlite::Connection::open_with_flags(
        root.path().join("rust-worker/authority.sqlite3"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let effects: i64 = database
        .query_row("SELECT COUNT(*) FROM storage_effects", [], |row| row.get(0))
        .unwrap();
    assert_eq!(effects, 0, "admission is not evidence of a provider effect");
}

#[test]
fn clock_rollback_cannot_append_a_grant_before_its_epoch_installation() {
    let root = tempfile::tempdir().unwrap();
    let mut f = fixture();
    drop(open_installed(root.path(), &f));
    let original_now = f.config.now.clone();
    // Inside the grant's signed validity window, but before the epoch was
    // installed. Such an append would make the next journal reopen fail.
    f.config.now = Some("2026-09-13T00:00:35Z".into());
    let mut rolled_back = Worker::open_with_authority(f.config.clone(), root.path()).unwrap();
    assert_eq!(
        rolled_back
            .admit_storage_operation_grant(&signed(&f.grant, true), &command(&f.grant))
            .unwrap_err()
            .code,
        "WORKER_AUTHORITY_STORE_UNAVAILABLE"
    );
    drop(rolled_back);
    f.config.now = original_now;
    let mut recovered = Worker::open_with_authority(f.config, root.path()).unwrap();
    recovered
        .admit_storage_operation_grant(&signed(&f.grant, true), &command(&f.grant))
        .unwrap();
}

#[test]
fn grant_journal_rejects_updates_deletes_and_replacements() {
    let root = tempfile::tempdir().unwrap();
    let f = fixture();
    let mut worker = open_installed(root.path(), &f);
    let raw = signed(&f.grant, true);
    worker
        .admit_storage_operation_grant(&raw, &command(&f.grant))
        .unwrap();
    drop(worker);
    let database =
        rusqlite::Connection::open(root.path().join("rust-worker/authority.sqlite3")).unwrap();
    for statement in [
        "UPDATE storage_operation_grants SET admitted_at='2026-09-13T00:00:39Z'",
        "DELETE FROM storage_operation_grants",
        "INSERT OR REPLACE INTO storage_operation_grants SELECT * FROM storage_operation_grants",
        "INSERT OR REPLACE INTO storage_operation_grants(rowid,request_id,nonce,action_id,epoch,fencing_token,operation_id,payload_digest,request,admitted_at) SELECT rowid,printf('%064d',3),printf('%064d',4),action_id,epoch,fencing_token,operation_id,payload_digest,request,admitted_at FROM storage_operation_grants",
    ] {
        assert!(
            database.execute(statement, []).is_err(),
            "mutable grant journal: {statement}"
        );
        let stored: Vec<u8> = database
            .query_row("SELECT request FROM storage_operation_grants", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(stored, raw);
    }
    Worker::open_with_authority(f.config, root.path()).unwrap();
}

#[test]
fn reopening_rejects_corrupted_grant_history_even_with_restored_guards() {
    for kind in [
        "signature",
        "action",
        "operation",
        "digest",
        "epoch",
        "token",
        "time",
    ] {
        let root = tempfile::tempdir().unwrap();
        let f = fixture();
        let mut worker = open_installed(root.path(), &f);
        worker
            .admit_storage_operation_grant(&signed(&f.grant, true), &command(&f.grant))
            .unwrap();
        drop(worker);
        let database =
            rusqlite::Connection::open(root.path().join("rust-worker/authority.sqlite3")).unwrap();
        let guard: String = database
            .query_row(
                "SELECT sql FROM sqlite_schema WHERE name='storage_grant_no_update'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        // Deliberate isolated corruption, never a way to manufacture provider
        // evidence. Restore the exact schema so only history validation rejects it.
        database
            .execute("DROP TRIGGER storage_grant_no_update", [])
            .unwrap();
        if kind == "signature" {
            let mut changed = f.grant.clone();
            changed["signature"] = "invalid".into();
            database
                .execute(
                    "UPDATE storage_operation_grants SET request=?1",
                    [canonical(&changed)],
                )
                .unwrap();
        } else {
            let statement = match kind {
                "action" => "UPDATE storage_operation_grants SET action_id='act-2'",
                "operation" => "UPDATE storage_operation_grants SET operation_id='other-operation'",
                "digest" => "UPDATE storage_operation_grants SET payload_digest='wrong-digest'",
                "epoch" => "UPDATE storage_operation_grants SET epoch=5",
                "token" => "UPDATE storage_operation_grants SET fencing_token=5",
                "time" => "UPDATE storage_operation_grants SET admitted_at='2026-09-13T00:00:35Z'",
                _ => unreachable!(),
            };
            database.execute(statement, []).unwrap();
        }
        database.execute(&guard, []).unwrap();
        assert_eq!(
            Worker::open_with_authority(f.config, root.path())
                .unwrap_err()
                .code,
            "WORKER_AUTHORITY_STORE_UNAVAILABLE",
            "{kind}"
        );
    }
}

#[test]
fn reauthorization_preserves_prior_admission_and_immutable_operation_scope() {
    let root = tempfile::tempdir().unwrap();
    let mut f = fixture();
    let original_time = f.config.now.clone().unwrap();
    let raw = signed(&f.grant, true);
    let mut worker = open_installed(root.path(), &f);
    worker
        .admit_storage_operation_grant(&raw, &command(&f.grant))
        .unwrap();
    drop(worker);
    f.config.now = Some("2026-09-13T00:00:50Z".into());
    let mut reopened = Worker::open_with_authority(f.config.clone(), root.path()).unwrap();
    reopened
        .admit_storage_operation_grant(&raw, &command(&f.grant))
        .unwrap();
    let database = rusqlite::Connection::open_with_flags(
        root.path().join("rust-worker/authority.sqlite3"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let unchanged: String = database
        .query_row(
            "SELECT admitted_at FROM storage_operation_grants",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(
        unchanged, original_time,
        "an exact retry must not rewrite admission history"
    );
    let mut renewed = f.grant;
    renewed["requestId"] = "3".repeat(64).into();
    renewed["nonce"] = "4".repeat(64).into();
    renewed["issuedAt"] = "2026-09-13T00:00:50Z".into();
    renewed["expiresAt"] = "2026-09-13T00:05:50Z".into();
    reopened
        .admit_storage_operation_grant(&signed(&renewed, true), &command(&renewed))
        .unwrap();
    let count: i64 = database
        .query_row("SELECT COUNT(*) FROM storage_operation_grants", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(count, 2);
    drop(reopened);
    // Historical rows remain authentic after both execution grants expire.
    f.config.now = Some("2026-09-13T00:06:00Z".into());
    Worker::open_with_authority(f.config, root.path()).unwrap();
}

#[test]
fn v3_migration_preserves_epoch_bytes_without_inventing_grants() {
    let root = tempfile::tempdir().unwrap();
    let f = fixture();
    drop(open_installed(root.path(), &f));
    let database =
        rusqlite::Connection::open(root.path().join("rust-worker/authority.sqlite3")).unwrap();
    let original: Vec<u8> = database
        .query_row("SELECT request FROM epoch_installs", [], |row| row.get(0))
        .unwrap();
    // Only this isolated fixture is reduced to the exact v3 schema. There is
    // no production downgrade path and no grant/effect row is fabricated.
    let guards: Vec<String> = database.prepare("SELECT name FROM sqlite_schema WHERE type='trigger' AND (name LIKE 'storage_grant_%' OR name='epoch_grant_replay')").unwrap()
        .query_map([], |row| row.get(0)).unwrap().collect::<Result<_, _>>().unwrap();
    for name in guards {
        database
            .execute(&format!("DROP TRIGGER {name}"), [])
            .unwrap();
    }
    database
        .execute("DROP TABLE storage_operation_grants", [])
        .unwrap();
    database.pragma_update(None, "user_version", 3).unwrap();
    let wrong = WorkerAuthorityConfig {
        fleet_id: "wrong-fleet".into(),
        ..f.config.clone()
    };
    assert!(Worker::open_with_authority(wrong, root.path()).is_err());
    let version: i64 = database
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap();
    assert_eq!(
        version, 3,
        "a rejected migration must roll back its schema writes"
    );
    let mut migrated = Worker::open_with_authority(f.config, root.path()).unwrap();
    let version: i64 = database
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap();
    assert_eq!(version, 4);
    let retained: Vec<u8> = database
        .query_row("SELECT request FROM epoch_installs", [], |row| row.get(0))
        .unwrap();
    assert_eq!(retained, original);
    let count: i64 = database
        .query_row("SELECT COUNT(*) FROM storage_operation_grants", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(count, 0);
    migrated
        .admit_storage_operation_grant(&signed(&f.grant, true), &command(&f.grant))
        .unwrap();
}
