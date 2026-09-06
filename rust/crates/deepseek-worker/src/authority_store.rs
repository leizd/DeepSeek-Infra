//! Rust-only durable epoch installation. No domain rows or provider effects live here.
use std::{collections::HashSet, fs, io::Read as _, path::Path, time::Duration};

use deepseek_protocol::{ActionFence, AdmitError, admit_command, validate_fence};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use serde_json::Value;

use crate::{AuthorityRequestContext, AuthorityRequestError, WorkerAuthority, authority_request};

const SCHEMA: &[&str] = &[
    "CREATE TABLE worker_authority (id INTEGER PRIMARY KEY CHECK(id=1), signer TEXT NOT NULL, fleet TEXT NOT NULL, environment TEXT NOT NULL, fencing_token INTEGER NOT NULL CHECK(fencing_token>0)) STRICT",
    "CREATE TABLE epoch_installs (request_id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, action_id TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch>0), fencing_token INTEGER NOT NULL CHECK(fencing_token>0), request BLOB NOT NULL, installed_at TEXT NOT NULL, UNIQUE(action_id,epoch)) STRICT",
    "CREATE TRIGGER epoch_no_update BEFORE UPDATE ON epoch_installs BEGIN SELECT RAISE(ABORT,'immutable epoch journal'); END",
    "CREATE TRIGGER epoch_no_delete BEFORE DELETE ON epoch_installs BEGIN SELECT RAISE(ABORT,'immutable epoch journal'); END",
    "CREATE TRIGGER epoch_fence BEFORE INSERT ON epoch_installs WHEN NEW.fencing_token != (SELECT fencing_token FROM worker_authority WHERE id=1) OR NEW.epoch <= COALESCE((SELECT MAX(epoch) FROM epoch_installs WHERE action_id=NEW.action_id),0) BEGIN SELECT RAISE(ABORT,'stale epoch or writer'); END",
    "CREATE TRIGGER authority_no_delete BEFORE DELETE ON worker_authority BEGIN SELECT RAISE(ABORT,'immutable authority'); END",
    "CREATE TRIGGER authority_monotonic BEFORE UPDATE ON worker_authority WHEN NEW.id != OLD.id OR NEW.signer != OLD.signer OR NEW.fleet != OLD.fleet OR NEW.environment != OLD.environment OR NEW.fencing_token <= OLD.fencing_token BEGIN SELECT RAISE(ABORT,'authority regression'); END",
];
const APPLICATION_ID: i64 = 0x44535741; // DSWA: DeepSeek Worker Authority

#[derive(Debug)]
pub(super) struct AuthorityStore {
    connection: Connection,
    fencing_token: i64,
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
        if validate_schema(&transaction, !existed)? {
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
        transaction.commit().map_err(|_| error())?;
        Ok(Self {
            connection,
            fencing_token: authority.fencing_token,
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
) -> Result<bool, AuthorityRequestError> {
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
        return Ok(true);
    }
    let mut expected: Vec<_> = SCHEMA.iter().map(|sql| sql.to_string()).collect();
    expected.sort();
    if version != 1 || application != APPLICATION_ID || actual != expected {
        return Err(error());
    }
    Ok(false)
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
