//! Rust-only durable epoch installation. No domain rows or provider effects live here.
use std::{collections::HashSet, fs, io::Read as _, path::Path, time::Duration};

use deepseek_protocol::{ActionFence, AdmitError, admit_command, validate_fence};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use serde_json::Value;

use crate::{AuthorityRequestContext, AuthorityRequestError, WorkerAuthority, authority_request};
#[cfg(feature = "s3")]
use crate::{StorageEffectBinding, StorageEffectRecord, StorageEffectState, WorkerStorageError};

const SCHEMA: &[&str] = &[
    "CREATE TABLE worker_authority (id INTEGER PRIMARY KEY CHECK(id=1), signer TEXT NOT NULL, fleet TEXT NOT NULL, environment TEXT NOT NULL, fencing_token INTEGER NOT NULL CHECK(fencing_token>0)) STRICT",
    "CREATE TABLE epoch_installs (request_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, action_id TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch>0), fencing_token INTEGER NOT NULL CHECK(fencing_token>0), request BLOB NOT NULL, installed_at TEXT NOT NULL, UNIQUE(action_id,epoch)) STRICT",
    "CREATE TRIGGER epoch_no_update BEFORE UPDATE ON epoch_installs BEGIN SELECT RAISE(ABORT,'immutable epoch journal'); END",
    "CREATE TRIGGER epoch_no_delete BEFORE DELETE ON epoch_installs BEGIN SELECT RAISE(ABORT,'immutable epoch journal'); END",
    "CREATE TRIGGER epoch_fence BEFORE INSERT ON epoch_installs WHEN NEW.fencing_token != (SELECT fencing_token FROM worker_authority WHERE id=1) OR NEW.epoch <= COALESCE((SELECT MAX(epoch) FROM epoch_installs WHERE action_id=NEW.action_id),0) BEGIN SELECT RAISE(ABORT,'stale epoch or writer'); END",
    "CREATE TRIGGER authority_no_delete BEFORE DELETE ON worker_authority BEGIN SELECT RAISE(ABORT,'immutable authority'); END",
    "CREATE TRIGGER authority_monotonic BEFORE UPDATE ON worker_authority WHEN NEW.id != OLD.id OR NEW.signer != OLD.signer OR NEW.fleet != OLD.fleet OR NEW.environment != OLD.environment OR NEW.fencing_token <= OLD.fencing_token BEGIN SELECT RAISE(ABORT,'authority regression'); END",
    "CREATE TABLE storage_effects (action_id TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch>0), fencing_token INTEGER NOT NULL CHECK(fencing_token>0), request_id TEXT NOT NULL, nonce TEXT NOT NULL, operation_kind TEXT NOT NULL, target_key TEXT NOT NULL, payload_digest TEXT NOT NULL, expected_length INTEGER NOT NULL CHECK(expected_length>=0), expected_version TEXT, authority_principal TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('RESERVED','PENDING','DISPATCHING','CONFIRMED','COMMITTED','EFFECT_UNKNOWN','RECONCILING','REJECTED','FAILED')), etag TEXT, provider_metadata TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(action_id, epoch)) STRICT",
    "CREATE TRIGGER storage_effects_fence BEFORE INSERT ON storage_effects WHEN NEW.fencing_token != (SELECT fencing_token FROM worker_authority WHERE id=1) OR NOT EXISTS (SELECT 1 FROM epoch_installs e WHERE e.action_id=NEW.action_id AND e.epoch=NEW.epoch AND e.fencing_token=NEW.fencing_token) BEGIN SELECT RAISE(ABORT,'unauthorized storage effect or stale fence'); END",
    "CREATE TRIGGER storage_effects_no_delete BEFORE DELETE ON storage_effects BEGIN SELECT RAISE(ABORT,'immutable storage effect journal'); END",
    "CREATE TRIGGER storage_effects_monotonic BEFORE UPDATE ON storage_effects WHEN OLD.state IN ('CONFIRMED','COMMITTED','REJECTED','FAILED') BEGIN SELECT RAISE(ABORT,'cannot mutate terminal storage effect'); END",
    "CREATE TRIGGER storage_effects_transition BEFORE UPDATE ON storage_effects WHEN OLD.state NOT IN ('CONFIRMED','COMMITTED','REJECTED','FAILED') AND NOT (OLD.state = NEW.state OR (OLD.state IN ('RESERVED','PENDING') AND NEW.state IN ('DISPATCHING','CONFIRMED','COMMITTED','EFFECT_UNKNOWN','RECONCILING','REJECTED','FAILED')) OR (OLD.state='DISPATCHING' AND NEW.state IN ('CONFIRMED','COMMITTED','EFFECT_UNKNOWN','REJECTED','FAILED')) OR (OLD.state='EFFECT_UNKNOWN' AND NEW.state='RECONCILING') OR (OLD.state='RECONCILING' AND NEW.state IN ('CONFIRMED','COMMITTED','EFFECT_UNKNOWN','REJECTED','FAILED'))) BEGIN SELECT RAISE(ABORT,'illegal storage effect transition'); END",
];
const APPLICATION_ID: i64 = 0x44535741; // DSWA: DeepSeek Worker Authority

// Additive schema extension. Never infer bindings for historical effect rows.
const SCHEMA_V2: &[&str] = &[
    "CREATE TABLE storage_effect_bindings (action_id TEXT NOT NULL, epoch INTEGER NOT NULL, target_identity TEXT NOT NULL CHECK(length(target_identity)=64 AND target_identity NOT GLOB '*[^0-9a-f]*'), expected_etag TEXT, PRIMARY KEY(action_id,epoch)) STRICT",
    "CREATE TRIGGER storage_binding_parent BEFORE INSERT ON storage_effect_bindings WHEN NOT EXISTS (SELECT 1 FROM storage_effects e WHERE e.action_id=NEW.action_id AND e.epoch=NEW.epoch AND e.state IN ('RESERVED','PENDING')) BEGIN SELECT RAISE(ABORT,'binding requires reserved effect'); END",
    "CREATE TRIGGER storage_binding_no_update BEFORE UPDATE ON storage_effect_bindings BEGIN SELECT RAISE(ABORT,'immutable storage binding'); END",
    "CREATE TRIGGER storage_binding_no_delete BEFORE DELETE ON storage_effect_bindings BEGIN SELECT RAISE(ABORT,'immutable storage binding'); END",
    "CREATE TRIGGER storage_binding_no_replace BEFORE INSERT ON storage_effect_bindings WHEN EXISTS (SELECT 1 FROM storage_effect_bindings b WHERE b.rowid=NEW.rowid OR (b.action_id=NEW.action_id AND b.epoch=NEW.epoch)) BEGIN SELECT RAISE(ABORT,'immutable storage binding'); END",
    "CREATE TRIGGER storage_effect_identity_no_replace BEFORE INSERT ON storage_effects WHEN EXISTS (SELECT 1 FROM storage_effects e WHERE e.rowid=NEW.rowid OR (e.action_id=NEW.action_id AND e.epoch=NEW.epoch)) BEGIN SELECT RAISE(ABORT,'immutable storage effect identity'); END",
    "CREATE TRIGGER storage_dispatch_bound BEFORE UPDATE ON storage_effects WHEN NEW.state='DISPATCHING' AND NOT EXISTS (SELECT 1 FROM storage_effect_bindings b WHERE b.action_id=NEW.action_id AND b.epoch=NEW.epoch) BEGIN SELECT RAISE(ABORT,'unbound storage dispatch'); END",
    "CREATE TRIGGER storage_effect_identity_immutable BEFORE UPDATE ON storage_effects WHEN NEW.rowid IS NOT OLD.rowid OR NEW.action_id IS NOT OLD.action_id OR NEW.epoch IS NOT OLD.epoch OR NEW.fencing_token IS NOT OLD.fencing_token OR NEW.request_id IS NOT OLD.request_id OR NEW.nonce IS NOT OLD.nonce OR NEW.operation_kind IS NOT OLD.operation_kind OR NEW.target_key IS NOT OLD.target_key OR NEW.payload_digest IS NOT OLD.payload_digest OR NEW.expected_length IS NOT OLD.expected_length OR NEW.expected_version IS NOT OLD.expected_version OR NEW.authority_principal IS NOT OLD.authority_principal OR NEW.created_at IS NOT OLD.created_at BEGIN SELECT RAISE(ABORT,'immutable storage effect identity'); END",
];

// The association is inserted only by the same transaction as its parent intent.
// Retain exact v1/v2 schema and never backfill an identity for historical effects.
const SCHEMA_V3: &[&str] = &[
    "CREATE TABLE storage_rpc_operations (action_id TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch>0), operation_id TEXT NOT NULL CHECK(length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 1024 AND length(trim(operation_id))>0 AND instr(operation_id,char(0))=0), PRIMARY KEY(action_id,epoch)) STRICT",
    "CREATE TRIGGER storage_rpc_parent BEFORE INSERT ON storage_rpc_operations WHEN NOT EXISTS (SELECT 1 FROM storage_effects e JOIN storage_effect_bindings b ON b.action_id=e.action_id AND b.epoch=e.epoch WHERE e.action_id=NEW.action_id AND e.epoch=NEW.epoch AND e.state IN ('RESERVED','PENDING')) BEGIN SELECT RAISE(ABORT,'operation requires bound reserved effect'); END",
    "CREATE TRIGGER storage_rpc_no_update BEFORE UPDATE ON storage_rpc_operations BEGIN SELECT RAISE(ABORT,'immutable storage operation'); END",
    "CREATE TRIGGER storage_rpc_no_delete BEFORE DELETE ON storage_rpc_operations BEGIN SELECT RAISE(ABORT,'immutable storage operation'); END",
    "CREATE TRIGGER storage_rpc_no_replace BEFORE INSERT ON storage_rpc_operations WHEN EXISTS (SELECT 1 FROM storage_rpc_operations o WHERE o.rowid=NEW.rowid OR (o.action_id=NEW.action_id AND o.epoch=NEW.epoch)) BEGIN SELECT RAISE(ABORT,'immutable storage operation'); END",
];

#[derive(Debug)]
pub(super) struct AuthorityStore {
    connection: Connection,
    fencing_token: i64,
    #[allow(dead_code)]
    now_override: Option<String>,
}

fn error() -> AuthorityRequestError {
    AuthorityRequestError::new("WORKER_AUTHORITY_STORE_UNAVAILABLE")
}

impl AuthorityStore {
    pub(super) fn open(
        root: &Path,
        authority: &WorkerAuthority,
    ) -> Result<Self, AuthorityRequestError> {
        // The operator owns this directory and must prevent untrusted local writers.
        // Reject links at every existing ancestor, including Windows junctions.
        let directory = root.join("rust-worker");
        let path = directory.join("authority.sqlite3");
        for ancestor in path.ancestors() {
            reject_link(ancestor)?;
        }
        fs::create_dir_all(&directory).map_err(|_| error())?;
        // Atomic creation, not an existence check followed by CREATE: two first
        // openers must not both retain permission to initialize after a crash.
        let existed = match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(file) => {
                drop(file);
                false
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => true,
            Err(_) => return Err(error()),
        };
        if existed {
            // A read-only SQLite query cannot recover a hot rollback journal.
            // Inspect the ownership marker without SQLite, then allow recovery
            // only for our file. Validate the recovered schema before mutation.
            let mut header = [0_u8; 100];
            fs::File::open(&path)
                .and_then(|mut file| file.read_exact(&mut header))
                .map_err(|_| error())?;
            if &header[..16] != b"SQLite format 3\0"
                || header[68..72] != (APPLICATION_ID as u32).to_be_bytes()
            {
                return Err(error());
            }
        }
        let mut connection = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_WRITE)
            .map_err(|_| error())?;
        connection
            .busy_timeout(Duration::from_millis(250))
            .map_err(|_| error())?;
        // FULL + rollback journal: committed epoch/nonce state survives process death.
        // Sources: https://www.sqlite.org/lang_transaction.html
        //          https://www.sqlite.org/pragma.html#pragma_synchronous
        connection
            .pragma_update(None, "synchronous", "FULL")
            .map_err(|_| error())?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| error())?;
        let schema_version = validate_schema(&transaction, !existed)?;
        if schema_version == 0 {
            for sql in SCHEMA {
                transaction.execute_batch(sql).map_err(|_| error())?;
            }
            transaction
                .pragma_update(None, "user_version", 1)
                .map_err(|_| error())?;
            transaction
                .pragma_update(None, "application_id", APPLICATION_ID)
                .map_err(|_| error())?;
            transaction
                .execute(
                    "INSERT INTO worker_authority VALUES (1,?1,?2,?3,?4)",
                    params![
                        authority.signer_public_key,
                        authority.fleet_id,
                        authority.environment,
                        authority.fencing_token
                    ],
                )
                .map_err(|_| error())?;
        }
        if schema_version < 2 {
            for sql in SCHEMA_V2 {
                transaction.execute(sql, []).map_err(|_| error())?;
            }
            transaction
                .pragma_update(None, "user_version", 2)
                .map_err(|_| error())?;
        }
        if schema_version < 3 {
            for sql in SCHEMA_V3 {
                transaction.execute(sql, []).map_err(|_| error())?;
            }
            transaction
                .pragma_update(None, "user_version", 3)
                .map_err(|_| error())?;
        }
        let (signer, fleet, environment, token): (String, String, String, i64) = transaction
            .query_row(
                "SELECT signer,fleet,environment,fencing_token FROM worker_authority WHERE id=1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .map_err(|_| error())?;
        if (signer.as_str(), fleet.as_str(), environment.as_str())
            != (
                authority.signer_public_key.as_str(),
                authority.fleet_id.as_str(),
                authority.environment.as_str(),
            )
        {
            return Err(AuthorityRequestError::new(
                "WORKER_AUTHORITY_STORE_IDENTITY_MISMATCH",
            ));
        }
        if token > authority.fencing_token {
            return Err(AuthorityRequestError::new(
                "AUTHORITY_REQUEST_STALE_FENCING_TOKEN",
            ));
        }
        validate_journal(&transaction, authority, token)?;
        if token < authority.fencing_token {
            transaction
                .execute(
                    "UPDATE worker_authority SET fencing_token=?1 WHERE id=1",
                    [authority.fencing_token],
                )
                .map_err(|_| error())?;
        }
        let now = match &authority.now {
            Some(now) => now.clone(),
            None => authority_request::utc_z_now()?,
        };
        transaction
            .execute(
                "UPDATE storage_effects SET state='EFFECT_UNKNOWN', updated_at=?1 WHERE state IN ('RESERVED', 'PENDING', 'DISPATCHING', 'RECONCILING')",
                params![now],
            )
            .map_err(|_| error())?;
        transaction.commit().map_err(|_| error())?;
        Ok(Self {
            connection,
            fencing_token: authority.fencing_token,
            now_override: authority.now.clone(),
        })
    }

    pub(super) fn admit(&self, fence: &ActionFence) -> Result<(), AdmitError> {
        validate_fence(fence)?;
        let observed: (i64, Option<i64>, Option<i64>) = self.connection.query_row(
            "SELECT a.fencing_token,e.epoch,e.fencing_token FROM worker_authority a LEFT JOIN epoch_installs e ON e.action_id=?1 AND e.epoch=(SELECT MAX(epoch) FROM epoch_installs WHERE action_id=?1) WHERE a.id=1", [&fence.action_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        ).map_err(|_| AdmitError::FenceMismatch)?;
        if observed.0 != self.fencing_token || observed.2 != Some(self.fencing_token) {
            return Err(AdmitError::FenceMismatch);
        }
        let epoch = observed
            .1
            .and_then(|epoch| u64::try_from(epoch).ok())
            .unwrap_or(0);
        admit_command(fence, epoch)
    }

    pub(super) fn installed_epoch(&self, action_id: &str) -> i64 {
        self.connection
            .query_row(
                "SELECT COALESCE(MAX(epoch),0) FROM epoch_installs WHERE action_id=?1",
                [action_id],
                |row| row.get(0),
            )
            .unwrap_or(0)
    }

    pub(super) fn install(
        &mut self,
        authority: &WorkerAuthority,
        fence: &ActionFence,
        bytes: &[u8],
    ) -> Result<ActionFence, AuthorityRequestError> {
        validate_fence(fence).map_err(AuthorityRequestError::from)?;
        if bytes.len() > crate::MAX_AUTHORITY_REQUEST_BYTES {
            return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_TOO_LARGE"));
        }
        // These untrusted IDs are only parameterized lookup keys. Nothing is written
        // until the existing verifier authenticates the entire canonical document.
        let candidate: Value = serde_json::from_slice(bytes)
            .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))?;
        let request_id = candidate
            .get("requestId")
            .and_then(Value::as_str)
            .unwrap_or("");
        let nonce = candidate.get("nonce").and_then(Value::as_str).unwrap_or("");
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| error())?;
        let token: i64 = transaction
            .query_row(
                "SELECT fencing_token FROM worker_authority WHERE id=1",
                [],
                |row| row.get(0),
            )
            .map_err(|_| error())?;
        if token != self.fencing_token {
            return Err(AuthorityRequestError::new(
                "AUTHORITY_REQUEST_STALE_FENCING_TOKEN",
            ));
        }
        let live: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(epoch),0) FROM epoch_installs WHERE action_id=?1",
                [&fence.action_id],
                |row| row.get(0),
            )
            .map_err(|_| error())?;
        let seen_request = transaction
            .query_row(
                "SELECT request_id FROM epoch_installs WHERE request_id=?1",
                [request_id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|_| error())?;
        let seen_nonce = transaction
            .query_row(
                "SELECT nonce FROM epoch_installs WHERE nonce=?1",
                [nonce],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|_| error())?;
        let now = match &authority.now {
            Some(now) => now.clone(),
            None => authority_request::utc_z_now()?,
        };
        let mut context = context(authority, &now, live);
        context.seen_request_ids.extend(seen_request);
        context.seen_nonces.extend(seen_nonce);
        let document = authority_request::verify_authority_request_document(bytes, &context)?;
        let installed = authority_request::authority_request_install_fields(&document)?;
        if installed.action_id != fence.action_id
            || installed.execution_epoch != fence.execution_epoch
        {
            return Err(AuthorityRequestError::new("FENCE_MISMATCH"));
        }
        transaction
            .execute(
                "INSERT INTO epoch_installs VALUES (?1,?2,?3,?4,?5,?6,?7)",
                params![
                    installed.request_id,
                    installed.nonce,
                    installed.action_id,
                    installed.execution_epoch as i64,
                    token,
                    bytes,
                    now
                ],
            )
            .map_err(|_| error())?;
        transaction.commit().map_err(|_| error())?;
        Ok(fence.clone())
    }

    #[cfg(feature = "s3")]
    #[allow(clippy::too_many_arguments)]
    pub(super) fn reserve_storage_mutation(
        &mut self,
        fence: &ActionFence,
        key: &str,
        payload_digest: &[u8; 32],
        expected_length: u64,
        operation_kind: &str,
        expected_version: Option<&str>,
        authority_principal: &str,
        binding: Option<&StorageEffectBinding>,
        operation_id: Option<&str>,
    ) -> Result<deepseek_storage::s3::StorageAuthorityProof, WorkerStorageError> {
        deepseek_protocol::validate_fence(fence).map_err(|_| WorkerStorageError::FenceMismatch)?;
        if key.is_empty() || key.len() > 1024 {
            return Err(WorkerStorageError::TargetMismatch);
        }
        if operation_id.is_some_and(|id| !crate::valid_storage_operation_id(id)) {
            return Err(WorkerStorageError::OperationMismatch);
        }
        let mut digest_hex = String::with_capacity(64);
        for b in payload_digest {
            use std::fmt::Write as _;
            let _ = write!(digest_hex, "{b:02x}");
        }
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| WorkerStorageError::WorkerWithoutAuthority)?;
        let token: i64 = transaction
            .query_row(
                "SELECT fencing_token FROM worker_authority WHERE id=1",
                [],
                |row| row.get(0),
            )
            .map_err(|_| WorkerStorageError::WorkerWithoutAuthority)?;
        if token != self.fencing_token {
            return Err(WorkerStorageError::StaleFencingToken);
        }
        let live_epoch: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(epoch),0) FROM epoch_installs WHERE action_id=?1",
                [&fence.action_id],
                |row| row.get(0),
            )
            .map_err(|_| WorkerStorageError::FenceMismatch)?;
        if live_epoch == 0 {
            return Err(WorkerStorageError::FenceMismatch);
        }
        if (fence.execution_epoch as i64) < live_epoch {
            return Err(WorkerStorageError::StaleEpoch);
        }
        if (fence.execution_epoch as i64) != live_epoch {
            return Err(WorkerStorageError::FenceMismatch);
        }
        let install_row: (String, String, i64) = transaction
            .query_row(
                "SELECT request_id, nonce, fencing_token FROM epoch_installs WHERE action_id=?1 AND epoch=?2",
                params![&fence.action_id, fence.execution_epoch as i64],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .map_err(|_| WorkerStorageError::FenceMismatch)?;
        if install_row.2 != token {
            return Err(WorkerStorageError::StaleFencingToken);
        }
        let (request_id, nonce) = (install_row.0, install_row.1);

        let existing: Option<(String, String, i64, String)> = transaction
            .query_row(
                "SELECT target_key, payload_digest, expected_length, state FROM storage_effects WHERE action_id=?1 AND epoch=?2",
                params![&fence.action_id, fence.execution_epoch as i64],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()
            .map_err(|_| WorkerStorageError::FenceMismatch)?;

        if let Some((target_key, stored_digest, stored_length, state)) = existing {
            let persisted_operation: Option<String> = transaction.query_row(
                "SELECT operation_id FROM storage_rpc_operations WHERE action_id=?1 AND epoch=?2",
                params![&fence.action_id, fence.execution_epoch as i64], |row| row.get(0),
            ).optional().map_err(|_| WorkerStorageError::FenceMismatch)?;
            if persisted_operation.as_deref() != operation_id {
                return Err(WorkerStorageError::OperationMismatch);
            }
            let persisted: Option<(String, Option<String>)> = transaction.query_row(
                "SELECT target_identity,expected_etag FROM storage_effect_bindings WHERE action_id=?1 AND epoch=?2",
                params![&fence.action_id, fence.execution_epoch as i64],
                |row| Ok((row.get(0)?, row.get(1)?)),
            ).optional().map_err(|_| WorkerStorageError::FenceMismatch)?;
            if target_key != key {
                return Err(WorkerStorageError::TargetMismatch);
            }
            if stored_digest != digest_hex {
                return Err(WorkerStorageError::DigestMismatch);
            }
            if stored_length != expected_length as i64
                && (persisted.is_some() || (expected_length > 0 && stored_length > 0))
            {
                return Err(WorkerStorageError::DigestMismatch);
            }
            match state.as_str() {
                "CONFIRMED" | "COMMITTED" => return Err(WorkerStorageError::ReplayRejected),
                "DISPATCHING" | "EFFECT_UNKNOWN" | "RECONCILING" => {
                    return Err(WorkerStorageError::UnknownEffectRetryBlocked);
                }
                "REJECTED" | "FAILED" => return Err(WorkerStorageError::PreconditionRejected),
                "RESERVED" | "PENDING" => {}
                _ => return Err(WorkerStorageError::FenceMismatch),
            }
            let expected =
                binding.map(|value| (value.target_identity.clone(), value.expected_etag.clone()));
            if persisted != expected {
                return Err(WorkerStorageError::TargetMismatch);
            }
        } else {
            let now = match &self.now_override {
                Some(now) => now.clone(),
                None => crate::authority_request::utc_z_now()
                    .map_err(|_| WorkerStorageError::FenceMismatch)?,
            };
            transaction
                .execute(
                    "INSERT INTO storage_effects VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,'RESERVED',NULL,NULL,?12,?12)",
                    params![
                        &fence.action_id,
                        fence.execution_epoch as i64,
                        token,
                        &request_id,
                        &nonce,
                        operation_kind,
                        key,
                        &digest_hex,
                        expected_length as i64,
                        expected_version,
                        authority_principal,
                        &now
                    ],
                )
                .map_err(|_| WorkerStorageError::FenceMismatch)?;
            if let Some(binding) = binding {
                transaction
                    .execute(
                        "INSERT INTO storage_effect_bindings VALUES (?1,?2,?3,?4)",
                        params![
                            &fence.action_id,
                            fence.execution_epoch as i64,
                            &binding.target_identity,
                            &binding.expected_etag
                        ],
                    )
                    .map_err(|_| WorkerStorageError::FenceMismatch)?;
            }
            if let Some(operation) = operation_id {
                transaction
                    .execute(
                        "INSERT INTO storage_rpc_operations VALUES (?1,?2,?3)",
                        params![&fence.action_id, fence.execution_epoch as i64, operation],
                    )
                    .map_err(|_| WorkerStorageError::FenceMismatch)?;
            }
        }
        transaction
            .commit()
            .map_err(|_| WorkerStorageError::FenceMismatch)?;

        Ok(deepseek_storage::s3::StorageAuthorityProof {
            action_id: fence.action_id.clone(),
            execution_epoch: fence.execution_epoch,
            fencing_token: token,
            request_id,
            nonce,
        })
    }

    #[cfg(feature = "s3")]
    pub(super) fn record_storage_mutation_outcome(
        &mut self,
        fence: &ActionFence,
        etag: Option<&str>,
        state: StorageEffectState,
        provider_metadata: Option<&str>,
        operation_id: Option<&str>,
    ) -> Result<(), WorkerStorageError> {
        validate_fence(fence).map_err(|_| WorkerStorageError::FenceMismatch)?;
        let epoch =
            i64::try_from(fence.execution_epoch).map_err(|_| WorkerStorageError::FenceMismatch)?;
        let now = match &self.now_override {
            Some(now) => now.clone(),
            None => crate::authority_request::utc_z_now()
                .map_err(|_| WorkerStorageError::FenceMismatch)?,
        };
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| WorkerStorageError::FenceMismatch)?;
        let token: i64 = transaction
            .query_row(
                "SELECT fencing_token FROM worker_authority WHERE id=1",
                [],
                |row| row.get(0),
            )
            .map_err(|_| WorkerStorageError::WorkerWithoutAuthority)?;
        if token != self.fencing_token {
            return Err(WorkerStorageError::StaleFencingToken);
        }
        let persisted_operation: Option<String> = transaction
            .query_row(
                "SELECT operation_id FROM storage_rpc_operations WHERE action_id=?1 AND epoch=?2",
                params![&fence.action_id, epoch],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| WorkerStorageError::FenceMismatch)?;
        if persisted_operation.as_deref() != operation_id {
            return Err(WorkerStorageError::OperationMismatch);
        }
        if state == StorageEffectState::Dispatching {
            // Reservation and dispatch are separate transactions. Another handle
            // may have claimed this intent or installed a newer epoch in between.
            // Recheck while holding the writer transaction through the transition.
            let (stored_token, current, live_epoch): (i64, String, i64) = transaction
                .query_row(
                    "SELECT fencing_token,state,(SELECT COALESCE(MAX(epoch),0) FROM epoch_installs WHERE action_id=?1) FROM storage_effects WHERE action_id=?1 AND epoch=?2",
                    params![&fence.action_id, epoch],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .map_err(|_| WorkerStorageError::FenceMismatch)?;
            if epoch < live_epoch {
                return Err(WorkerStorageError::StaleEpoch);
            }
            if epoch != live_epoch {
                return Err(WorkerStorageError::FenceMismatch);
            }
            if stored_token != token {
                return Err(WorkerStorageError::StaleFencingToken);
            }
            match StorageEffectState::from_db_str(&current) {
                Some(StorageEffectState::Reserved) => {}
                Some(
                    StorageEffectState::Dispatching
                    | StorageEffectState::EffectUnknown
                    | StorageEffectState::Reconciling,
                ) => {
                    return Err(WorkerStorageError::UnknownEffectRetryBlocked);
                }
                Some(StorageEffectState::Confirmed) => {
                    return Err(WorkerStorageError::ReplayRejected);
                }
                Some(StorageEffectState::Rejected | StorageEffectState::Failed) => {
                    return Err(WorkerStorageError::PreconditionRejected);
                }
                None => return Err(WorkerStorageError::FenceMismatch),
            }
        }
        let changed = transaction
            .execute(
                "UPDATE storage_effects SET state=?1, etag=COALESCE(?2, etag), provider_metadata=COALESCE(?3, provider_metadata), updated_at=?4 WHERE action_id=?5 AND epoch=?6",
                params![
                    state.as_str(),
                    etag,
                    provider_metadata,
                    &now,
                    &fence.action_id,
                    epoch,
                ],
            )
            .map_err(|_| WorkerStorageError::FenceMismatch)?;
        if changed == 0 {
            return Err(WorkerStorageError::FenceMismatch);
        }
        transaction
            .commit()
            .map_err(|_| WorkerStorageError::FenceMismatch)?;
        Ok(())
    }

    #[cfg(feature = "s3")]
    pub(super) fn query_storage_effect(
        &self,
        fence: &ActionFence,
    ) -> Result<Option<StorageEffectRecord>, WorkerStorageError> {
        deepseek_protocol::validate_fence(fence).map_err(|_| WorkerStorageError::FenceMismatch)?;
        let row = self
            .connection
            .query_row(
                "SELECT action_id, epoch, fencing_token, request_id, nonce, operation_kind, target_key, payload_digest, expected_length, expected_version, authority_principal, state, etag, provider_metadata, created_at, updated_at, (SELECT b.target_identity FROM storage_effect_bindings b WHERE b.action_id=storage_effects.action_id AND b.epoch=storage_effects.epoch), (SELECT b.expected_etag FROM storage_effect_bindings b WHERE b.action_id=storage_effects.action_id AND b.epoch=storage_effects.epoch), (SELECT o.operation_id FROM storage_rpc_operations o WHERE o.action_id=storage_effects.action_id AND o.epoch=storage_effects.epoch) FROM storage_effects WHERE action_id=?1 AND epoch=?2",
                params![&fence.action_id, fence.execution_epoch as i64],
                |row| {
                    let state_str: String = row.get(11)?;
                    let state = StorageEffectState::from_db_str(&state_str)
                        .ok_or(rusqlite::Error::InvalidQuery)?;
                    let epoch: i64 = row.get(1)?;
                    let expected_length: i64 = row.get(8)?;
                    let target: Option<String> = row.get(16)?;
                    let expected_etag: Option<String> = row.get(17)?;
                    Ok(StorageEffectRecord {
                        action_id: row.get(0)?,
                        execution_epoch: epoch as u64,
                        fencing_token: row.get(2)?,
                        request_id: row.get(3)?,
                        nonce: row.get(4)?,
                        operation_kind: row.get(5)?,
                        target_key: row.get(6)?,
                        payload_digest: row.get(7)?,
                        expected_length: expected_length as u64,
                        expected_version: row.get(9)?,
                        authority_principal: row.get(10)?,
                        state,
                        etag: row.get(12)?,
                        provider_metadata: row.get(13)?,
                        created_at: row.get(14)?,
                        updated_at: row.get(15)?,
                        binding: target.map(|target_identity| StorageEffectBinding { target_identity, expected_etag }),
                        operation_id: row.get(18)?,
                    })
                },
            )
            .optional()
            .map_err(|_| WorkerStorageError::FenceMismatch)?;
        Ok(row)
    }
}

fn validate_journal(
    connection: &Connection,
    authority: &WorkerAuthority,
    head_token: i64,
) -> Result<(), AuthorityRequestError> {
    let mut statement = connection.prepare("SELECT request_id,nonce,action_id,epoch,fencing_token,request,installed_at FROM epoch_installs").map_err(|_| error())?;
    let mut rows = statement.query([]).map_err(|_| error())?;
    while let Some(row) = rows.next().map_err(|_| error())? {
        let request_id: String = row.get(0).map_err(|_| error())?;
        let nonce: String = row.get(1).map_err(|_| error())?;
        let action: String = row.get(2).map_err(|_| error())?;
        let epoch: i64 = row.get(3).map_err(|_| error())?;
        let token: i64 = row.get(4).map_err(|_| error())?;
        let request = row
            .get_ref(5)
            .map_err(|_| error())?
            .as_blob()
            .map_err(|_| error())?;
        if request.len() > crate::MAX_AUTHORITY_REQUEST_BYTES {
            return Err(error());
        }
        let at: String = row.get(6).map_err(|_| error())?;
        let mut context = context(authority, &at, 0);
        // Epoch authorization outlives the short-lived installation request. Verify
        // at its durable acceptance time, not the process restart's wall clock.
        context.current_fencing_token = token;
        let document = authority_request::verify_authority_request_document(request, &context)
            .map_err(|_| error())?;
        let fields =
            authority_request::authority_request_install_fields(&document).map_err(|_| error())?;
        if fields.request_id != request_id
            || fields.nonce != nonce
            || fields.action_id != action
            || fields.execution_epoch as i64 != epoch
            || token > head_token
        {
            return Err(error());
        }
    }
    let mut effect_stmt = connection
        .prepare("SELECT action_id,epoch,fencing_token,request_id,nonce FROM storage_effects")
        .map_err(|_| error())?;
    let mut effect_rows = effect_stmt.query([]).map_err(|_| error())?;
    while let Some(row) = effect_rows.next().map_err(|_| error())? {
        let action: String = row.get(0).map_err(|_| error())?;
        let epoch: i64 = row.get(1).map_err(|_| error())?;
        let token: i64 = row.get(2).map_err(|_| error())?;
        let req_id: String = row.get(3).map_err(|_| error())?;
        let nonce: String = row.get(4).map_err(|_| error())?;
        if token > head_token {
            return Err(error());
        }
        let matched: Option<(String, String)> = connection
            .query_row(
                "SELECT request_id, nonce FROM epoch_installs WHERE action_id=?1 AND epoch=?2 AND fencing_token=?3",
                params![action, epoch, token],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(|_| error())?;
        match matched {
            Some((expected_req, expected_nonce))
                if expected_req == req_id && expected_nonce == nonce => {}
            _ => return Err(error()),
        }
    }
    let mut operations = connection.prepare("SELECT o.operation_id,e.action_id,b.action_id FROM storage_rpc_operations o LEFT JOIN storage_effects e ON e.action_id=o.action_id AND e.epoch=o.epoch LEFT JOIN storage_effect_bindings b ON b.action_id=o.action_id AND b.epoch=o.epoch").map_err(|_| error())?;
    let mut rows = operations.query([]).map_err(|_| error())?;
    while let Some(row) = rows.next().map_err(|_| error())? {
        let id: String = row.get(0).map_err(|_| error())?;
        let effect: Option<String> = row.get(1).map_err(|_| error())?;
        let binding: Option<String> = row.get(2).map_err(|_| error())?;
        if !crate::valid_storage_operation_id(&id) || effect.is_none() || binding.is_none() {
            return Err(error());
        }
    }
    Ok(())
}

fn context<'a>(
    authority: &'a WorkerAuthority,
    now: &'a str,
    live: i64,
) -> AuthorityRequestContext<'a> {
    AuthorityRequestContext {
        now,
        signer_public_key: &authority.signer_public_key,
        signer_key_id: &authority.signer_key_id,
        expected_domain: "action",
        expected_operation: "install-epoch",
        expected_runtime: "go",
        expected_mode: "shadow",
        expected_fleet_id: &authority.fleet_id,
        expected_environment: &authority.environment,
        expected_role: "control-plane",
        current_fencing_token: authority.fencing_token,
        live_epoch: live,
        seen_request_ids: HashSet::new(),
        seen_nonces: HashSet::new(),
        max_future_skew_seconds: 30,
    }
}

fn validate_schema(
    connection: &Connection,
    allow_empty: bool,
) -> Result<u8, AuthorityRequestError> {
    let version: i64 = connection
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .map_err(|_| error())?;
    let application: i64 = connection
        .pragma_query_value(None, "application_id", |row| row.get(0))
        .map_err(|_| error())?;
    let mut statement = connection
        .prepare("SELECT sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*' ORDER BY sql")
        .map_err(|_| error())?;
    let actual = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|_| error())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| error())?;
    if allow_empty && version == 0 && application == 0 && actual.is_empty() {
        return Ok(0);
    }
    let mut expected: Vec<_> = SCHEMA.iter().map(|sql| sql.to_string()).collect();
    if version >= 2 {
        expected.extend(SCHEMA_V2.iter().map(|sql| sql.to_string()));
    }
    if version >= 3 {
        expected.extend(SCHEMA_V3.iter().map(|sql| sql.to_string()));
    }
    expected.sort();
    if !matches!(version, 1..=3) || application != APPLICATION_ID || actual != expected {
        return Err(error());
    }
    Ok(version as u8)
}

fn reject_link(path: &Path) -> Result<(), AuthorityRequestError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            #[cfg(windows)]
            {
                use std::os::windows::fs::MetadataExt;
                if metadata.file_attributes() & 0x400 != 0 {
                    return Err(error());
                }
            }
            if metadata.file_type().is_symlink() {
                return Err(error());
            }
            Ok(())
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(error()),
    }
}
