//! Append-only operation grants. Acceptance is not a provider-effect record.
use super::{AuthorityStore, error};
use crate::{
    AuthorityRequestError, StorageOperationCommand, StorageOperationGrantContext,
    StorageOperationGrantError, WorkerAuthority, authority_request, bind_storage_operation_grant,
    verify_storage_operation_grant,
};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::Value;
use std::collections::{HashMap, HashSet};

pub(super) const SCHEMA_V4: &[&str] = &[
    "CREATE TABLE storage_operation_grants (request_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, action_id TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch>0), fencing_token INTEGER NOT NULL CHECK(fencing_token>0), operation_id TEXT NOT NULL, payload_digest TEXT NOT NULL, request BLOB NOT NULL CHECK(length(request) BETWEEN 1 AND 16384), admitted_at TEXT NOT NULL) STRICT",
    "CREATE INDEX storage_grant_operation_lookup ON storage_operation_grants(operation_id)",
    "CREATE TRIGGER storage_grant_no_update BEFORE UPDATE ON storage_operation_grants BEGIN SELECT RAISE(ABORT,'immutable operation grant'); END",
    "CREATE TRIGGER storage_grant_no_delete BEFORE DELETE ON storage_operation_grants BEGIN SELECT RAISE(ABORT,'immutable operation grant'); END",
    "CREATE TRIGGER storage_grant_no_replace BEFORE INSERT ON storage_operation_grants WHEN EXISTS (SELECT 1 FROM storage_operation_grants g WHERE g.rowid=NEW.rowid OR g.request_id=NEW.request_id OR g.nonce=NEW.nonce) BEGIN SELECT RAISE(ABORT,'immutable operation grant'); END",
    "CREATE TRIGGER storage_grant_fence BEFORE INSERT ON storage_operation_grants WHEN NEW.fencing_token != (SELECT fencing_token FROM worker_authority WHERE id=1) OR NOT EXISTS (SELECT 1 FROM epoch_installs e WHERE e.action_id=NEW.action_id AND e.epoch=NEW.epoch AND e.fencing_token=NEW.fencing_token) OR NEW.epoch != (SELECT MAX(epoch) FROM epoch_installs WHERE action_id=NEW.action_id) BEGIN SELECT RAISE(ABORT,'stale grant epoch or writer'); END",
    "CREATE TRIGGER storage_grant_epoch_replay BEFORE INSERT ON storage_operation_grants WHEN EXISTS (SELECT 1 FROM epoch_installs e WHERE e.request_id=NEW.request_id OR e.nonce=NEW.nonce) BEGIN SELECT RAISE(ABORT,'reused authority request or nonce'); END",
    "CREATE TRIGGER epoch_grant_replay BEFORE INSERT ON epoch_installs WHEN EXISTS (SELECT 1 FROM storage_operation_grants g WHERE g.request_id=NEW.request_id OR g.nonce=NEW.nonce) BEGIN SELECT RAISE(ABORT,'reused grant request or nonce'); END",
    "CREATE TRIGGER storage_grant_operation_binding BEFORE INSERT ON storage_operation_grants WHEN EXISTS (SELECT 1 FROM storage_operation_grants g WHERE g.operation_id=NEW.operation_id AND (g.action_id!=NEW.action_id OR g.epoch!=NEW.epoch OR g.payload_digest!=NEW.payload_digest)) BEGIN SELECT RAISE(ABORT,'operation grant scope changed'); END",
];

fn context<'a>(
    authority: &'a WorkerAuthority,
    now: &'a str,
    token: i64,
    epoch: i64,
) -> StorageOperationGrantContext<'a> {
    StorageOperationGrantContext {
        now,
        signer_public_key: &authority.signer_public_key,
        signer_key_id: &authority.signer_key_id,
        expected_domain: "action",
        expected_operation: "execute-storage-put",
        expected_runtime: "go",
        expected_mode: "shadow",
        expected_fleet_id: &authority.fleet_id,
        expected_environment: &authority.environment,
        expected_role: "control-plane",
        current_fencing_token: token,
        live_epoch: epoch,
        seen_request_ids: HashSet::new(),
        seen_nonces: HashSet::new(),
        seen_operation_digests: HashMap::new(),
        max_future_skew_seconds: 30,
    }
}

impl AuthorityStore {
    pub(crate) fn admit_grant(
        &mut self,
        authority: &WorkerAuthority,
        bytes: &[u8],
        command: &StorageOperationCommand<'_>,
    ) -> Result<(), StorageOperationGrantError> {
        // Worker has bounded these bytes; parsing supplies only parameterized
        // lookup keys. No record is written before signature/scope validation.
        let candidate: Value = serde_json::from_slice(bytes)
            .map_err(|_| StorageOperationGrantError::new("STORAGE_OPERATION_GRANT_INVALID"))?;
        let request_id = candidate["requestId"].as_str().unwrap_or("");
        let nonce = candidate["nonce"].as_str().unwrap_or("");
        let operation = candidate["operationId"].as_str().unwrap_or("");
        // SQLite BEGIN IMMEDIATE prevents a writer takeover between the current
        // fence read and the durable append: https://www.sqlite.org/lang_transaction.html
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| error())?;
        let token: i64 = tx
            .query_row(
                "SELECT fencing_token FROM worker_authority WHERE id=1",
                [],
                |row| row.get(0),
            )
            .map_err(|_| error())?;
        if token != self.fencing_token || token != authority.fencing_token {
            return Err(StorageOperationGrantError::new(
                "STORAGE_OPERATION_GRANT_STALE_FENCING_TOKEN",
            ));
        }
        let live: i64 = tx
            .query_row(
                "SELECT COALESCE(MAX(epoch),0) FROM epoch_installs WHERE action_id=?1",
                [command.action_id],
                |row| row.get(0),
            )
            .map_err(|_| error())?;
        let previous: Option<Vec<u8>> = tx
            .query_row(
                "SELECT request FROM storage_operation_grants WHERE request_id=?1",
                [request_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| error())?;
        let exact_retry = previous.as_deref() == Some(bytes);
        let now = match &authority.now {
            Some(now) => now.clone(),
            None => authority_request::utc_z_now()?,
        };
        let mut ctx = context(authority, &now, token, live);
        if previous.is_some() && !exact_retry {
            ctx.seen_request_ids.insert(request_id.to_owned());
        }
        let epoch_request: Option<String> = tx
            .query_row(
                "SELECT request_id FROM epoch_installs WHERE request_id=?1",
                [request_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| error())?;
        ctx.seen_request_ids.extend(epoch_request);
        let seen_nonce: Option<String> = tx.query_row("SELECT nonce FROM epoch_installs WHERE nonce=?1 UNION SELECT nonce FROM storage_operation_grants WHERE nonce=?1 AND (?2=0 OR request_id!=?3)", params![nonce, exact_retry, request_id], |row| row.get(0)).optional().map_err(|_| error())?;
        ctx.seen_nonces.extend(seen_nonce);
        let prior_scope: Option<(String, i64, String)> = tx.query_row("SELECT action_id,epoch,payload_digest FROM storage_operation_grants WHERE operation_id=?1 LIMIT 1", [operation], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?))).optional().map_err(|_| error())?;
        if let Some((_, _, digest)) = &prior_scope {
            ctx.seen_operation_digests
                .insert(operation.to_owned(), digest.clone());
        }
        let verified = verify_storage_operation_grant(bytes, &ctx)?;
        bind_storage_operation_grant(&verified, command)?;
        if let Some((action, epoch, _)) = prior_scope {
            if action != command.action_id || epoch as u64 != command.execution_epoch {
                return Err(StorageOperationGrantError::new(
                    "STORAGE_OPERATION_GRANT_REPLAY_CONFLICT",
                ));
            }
        }
        validate_install_boundary(&tx, command.action_id, live, token, &now)?;
        if !exact_retry {
            tx.execute("INSERT INTO storage_operation_grants(request_id,nonce,action_id,epoch,fencing_token,operation_id,payload_digest,request,admitted_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)", params![request_id,nonce,command.action_id,command.execution_epoch as i64,token,operation,verified["payloadDigest"].as_str().ok_or_else(error)?,bytes,now]).map_err(|_| error())?;
        }
        tx.commit().map_err(|_| error())?;
        Ok(())
    }
}

fn validate_install_boundary(
    connection: &Connection,
    action: &str,
    epoch: i64,
    token: i64,
    admitted_at: &str,
) -> Result<(), AuthorityRequestError> {
    let installed_at: Option<String> = connection.query_row("SELECT installed_at FROM epoch_installs WHERE action_id=?1 AND epoch=?2 AND fencing_token=?3", params![action,epoch,token], |row| row.get(0)).optional().map_err(|_| error())?;
    let installed_at = installed_at.ok_or_else(error)?;
    if authority_request::parse_utc_z_str(admitted_at)?
        < authority_request::parse_utc_z_str(&installed_at)?
    {
        return Err(error());
    }
    Ok(())
}

pub(super) fn validate_journal(
    connection: &Connection,
    authority: &WorkerAuthority,
    head_token: i64,
) -> Result<(), AuthorityRequestError> {
    let mut statement = connection.prepare("SELECT request_id,nonce,action_id,epoch,fencing_token,operation_id,payload_digest,request,admitted_at FROM storage_operation_grants").map_err(|_| error())?;
    let mut rows = statement.query([]).map_err(|_| error())?;
    let mut scopes: HashMap<String, (String, i64, String)> = HashMap::new();
    while let Some(row) = rows.next().map_err(|_| error())? {
        let request_id: String = row.get(0).map_err(|_| error())?;
        let nonce: String = row.get(1).map_err(|_| error())?;
        let action: String = row.get(2).map_err(|_| error())?;
        let epoch: i64 = row.get(3).map_err(|_| error())?;
        let token: i64 = row.get(4).map_err(|_| error())?;
        let operation: String = row.get(5).map_err(|_| error())?;
        let digest: String = row.get(6).map_err(|_| error())?;
        let bytes = row
            .get_ref(7)
            .map_err(|_| error())?
            .as_blob()
            .map_err(|_| error())?;
        let admitted_at: String = row.get(8).map_err(|_| error())?;
        let document =
            verify_storage_operation_grant(bytes, &context(authority, &admitted_at, token, epoch))
                .map_err(|_| error())?;
        if token > head_token
            || document["requestId"].as_str() != Some(&request_id)
            || document["nonce"].as_str() != Some(&nonce)
            || document["actionId"].as_str() != Some(&action)
            || document["executionEpoch"].as_i64() != Some(epoch)
            || document["operationId"].as_str() != Some(&operation)
            || document["payloadDigest"].as_str() != Some(&digest)
        {
            return Err(error());
        }
        validate_install_boundary(connection, &action, epoch, token, &admitted_at)?;
        let reused: bool = connection
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM epoch_installs WHERE request_id=?1 OR nonce=?2)",
                params![request_id, nonce],
                |row| row.get(0),
            )
            .map_err(|_| error())?;
        if reused {
            return Err(error());
        }
        let scope = (action, epoch, digest);
        if let Some(previous) = scopes.insert(operation, scope.clone()) {
            if previous != scope {
                return Err(error());
            }
        }
    }
    Ok(())
}
