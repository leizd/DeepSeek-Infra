use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use rusqlite::{Connection, OptionalExtension, Row, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::fmt;
use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

pub const TRANSFER_JOURNAL_RECORD_SCHEMA: &str = "federated-transfer-journal-record-v1";
pub const TRANSFER_JOURNAL_EVENT_SCHEMA: &str = "federated-transfer-journal-event-v1";
pub const TRANSFER_STATE_PAYLOAD_SCHEMA: &str = "federated-transfer-state-v1";
pub const FEDERATED_TRANSFER_IDENTITY_SCHEMA: &str = "federated-transfer-identity-v1";
const TRANSFER_IDENTITY_BINDING_SCHEMA: &str = "federated-transfer-binding-v1";
pub const TRANSFER_ID_DOMAIN: &[u8] = b"deepseek-infra:federated-transfer-identity-v1\0";

const FLEET_IDENTITY_SCHEMA: &str = "fleet-identity-v1";
const SIGNATURE_ALGORITHM: &str = "Ed25519";
const MAX_STATE_DETAILS_BYTES: usize = 64 * 1024;
const BUSY_TIMEOUT: Duration = Duration::from_secs(30);
const SENSITIVE_KEY_MARKERS: &[&str] = &[
    "credential",
    "password",
    "privatekey",
    "secretkey",
    "accesskey",
    "agesecretkey",
];
const IDENTITY_SECRET_KEY_MARKERS: &[&str] = &[
    "accesskey",
    "ageidentity",
    "ageprivateidentity",
    "credential",
    "credentialref",
    "credentialreference",
    "passphrase",
    "password",
    "privatekey",
    "privatekeyenvelope",
    "secret",
    "secretkey",
    "sessiontoken",
];
const IDENTITY_SECRET_VALUE_MARKERS: &[&str] = &[
    "age-secret-key-",
    "begin private key",
    "begin openssh private key",
];

const CREATE_JOURNAL_IDENTITY_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS federation_transfer_journal_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    local_fleet_id TEXT NOT NULL,
    root_fingerprint TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    bound_at TEXT NOT NULL
)
"#;

const CREATE_TRANSFERS_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS federation_transfers (
    transfer_id TEXT PRIMARY KEY,
    identity_digest TEXT NOT NULL,
    local_fleet_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('SENDER', 'RECEIVER')),
    source_fleet_id TEXT NOT NULL,
    destination_fleet_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    object_set_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'PROPOSED', 'GRANT_REQUESTED', 'GRANT_VERIFIED', 'TRANSFERRING',
        'REMOTE_VERIFYING', 'REMOTE_COMMITTED', 'LOCAL_RECORDED', 'SUCCEEDED'
    )),
    state_payload_digest TEXT NOT NULL,
    state_payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1)
)
"#;

const CREATE_TRANSFER_EVENTS_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS federation_transfer_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id TEXT NOT NULL,
    previous_state TEXT,
    next_state TEXT NOT NULL,
    state_payload_digest TEXT NOT NULL,
    state_payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    FOREIGN KEY(transfer_id) REFERENCES federation_transfers(transfer_id)
)
"#;

const CREATE_TRANSFER_EVENTS_INDEX_SQL: &str = r#"
CREATE UNIQUE INDEX IF NOT EXISTS idx_federation_transfer_events_revision
ON federation_transfer_events(transfer_id, revision)
"#;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FederatedTransferJournalError {
    code: &'static str,
}

impl FederatedTransferJournalError {
    fn new(code: &'static str) -> Self {
        Self { code }
    }

    pub const fn code(&self) -> &'static str {
        self.code
    }
}

impl fmt::Display for FederatedTransferJournalError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code)
    }
}

impl std::error::Error for FederatedTransferJournalError {}

fn error(code: &'static str) -> FederatedTransferJournalError {
    FederatedTransferJournalError::new(code)
}

fn database_error(_: rusqlite::Error) -> FederatedTransferJournalError {
    error("FEDERATION_TRANSFER_JOURNAL_IO_ERROR")
}

fn filesystem_error(_: std::io::Error) -> FederatedTransferJournalError {
    error("FEDERATION_TRANSFER_JOURNAL_IO_ERROR")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TransferRole {
    Sender,
    Receiver,
}

impl TransferRole {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Sender => "SENDER",
            Self::Receiver => "RECEIVER",
        }
    }

    fn parse(value: &str) -> Result<Self, FederatedTransferJournalError> {
        match value {
            "SENDER" => Ok(Self::Sender),
            "RECEIVER" => Ok(Self::Receiver),
            _ => Err(error("FEDERATION_TRANSFER_JOURNAL_CORRUPT")),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TransferState {
    Proposed,
    GrantRequested,
    GrantVerified,
    Transferring,
    RemoteVerifying,
    RemoteCommitted,
    LocalRecorded,
    Succeeded,
}

impl TransferState {
    pub const ALL: [Self; 8] = [
        Self::Proposed,
        Self::GrantRequested,
        Self::GrantVerified,
        Self::Transferring,
        Self::RemoteVerifying,
        Self::RemoteCommitted,
        Self::LocalRecorded,
        Self::Succeeded,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Proposed => "PROPOSED",
            Self::GrantRequested => "GRANT_REQUESTED",
            Self::GrantVerified => "GRANT_VERIFIED",
            Self::Transferring => "TRANSFERRING",
            Self::RemoteVerifying => "REMOTE_VERIFYING",
            Self::RemoteCommitted => "REMOTE_COMMITTED",
            Self::LocalRecorded => "LOCAL_RECORDED",
            Self::Succeeded => "SUCCEEDED",
        }
    }

    fn parse(value: &str) -> Result<Self, FederatedTransferJournalError> {
        Self::ALL
            .into_iter()
            .find(|state| state.as_str() == value)
            .ok_or_else(|| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))
    }

    const fn next(self) -> Option<Self> {
        match self {
            Self::Proposed => Some(Self::GrantRequested),
            Self::GrantRequested => Some(Self::GrantVerified),
            Self::GrantVerified => Some(Self::Transferring),
            Self::Transferring => Some(Self::RemoteVerifying),
            Self::RemoteVerifying => Some(Self::RemoteCommitted),
            Self::RemoteCommitted => Some(Self::LocalRecorded),
            Self::LocalRecorded => Some(Self::Succeeded),
            Self::Succeeded => None,
        }
    }

    fn revision(self) -> u64 {
        Self::ALL
            .iter()
            .position(|candidate| *candidate == self)
            .map_or(0, |index| index as u64 + 1)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ProposedTransfer {
    pub transfer_id: String,
    pub source_fleet_id: String,
    pub destination_fleet_id: String,
    pub policy_id: String,
    pub backup_id: String,
    pub object_set_digest: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TransferRecord {
    pub schema: String,
    pub transfer_id: String,
    pub identity_digest: String,
    pub local_fleet_id: String,
    pub role: TransferRole,
    pub source_fleet_id: String,
    pub destination_fleet_id: String,
    pub policy_id: String,
    pub backup_id: String,
    pub object_set_digest: String,
    pub state: TransferState,
    pub state_payload_digest: String,
    pub state_details: Value,
    pub created_at: String,
    pub updated_at: String,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TransferEvent {
    pub schema: String,
    pub sequence: u64,
    pub transfer_id: String,
    pub previous_state: Option<TransferState>,
    pub next_state: TransferState,
    pub state_payload_digest: String,
    pub state_details: Value,
    pub occurred_at: String,
    pub revision: u64,
}

#[derive(Debug, Clone)]
struct LocalFleetIdentity {
    document: Value,
    fleet_id: String,
    root_fingerprint: String,
    identity_digest: String,
    identity_json: String,
}

#[derive(Debug)]
pub struct FederatedTransferJournal {
    db_path: PathBuf,
    local_identity: LocalFleetIdentity,
}

impl FederatedTransferJournal {
    pub fn open(
        db_path: impl AsRef<Path>,
        local_identity: &Value,
    ) -> Result<Self, FederatedTransferJournalError> {
        let local_identity = validate_local_identity(local_identity)?;
        let db_path = db_path.as_ref().to_path_buf();
        if db_path.as_os_str().is_empty() {
            return Err(error("FEDERATION_TRANSFER_JOURNAL_PATH_INVALID"));
        }
        if let Some(parent) = db_path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            fs::create_dir_all(parent).map_err(filesystem_error)?;
        }
        let journal = Self {
            db_path,
            local_identity,
        };
        journal.ensure_schema()?;
        journal.bind_local_identity()?;
        Ok(journal)
    }

    pub fn db_path(&self) -> &Path {
        &self.db_path
    }

    pub fn local_identity(&self) -> Value {
        self.local_identity.document.clone()
    }

    pub fn persist_proposed_transfer(
        &self,
        proposed: &ProposedTransfer,
        now: &str,
    ) -> Result<TransferRecord, FederatedTransferJournalError> {
        validate_transfer_id(&proposed.transfer_id)?;
        validate_fleet_id(&proposed.source_fleet_id)?;
        validate_fleet_id(&proposed.destination_fleet_id)?;
        if proposed.source_fleet_id == proposed.destination_fleet_id {
            return Err(error("FEDERATION_TRANSFER_REFLECTION_REJECTED"));
        }
        validate_control_id(&proposed.policy_id, "FEDERATION_TRANSFER_POLICY_ID_INVALID")?;
        validate_control_id(&proposed.backup_id, "FEDERATION_TRANSFER_BACKUP_ID_INVALID")?;
        validate_typed_sha256(
            &proposed.object_set_digest,
            "FEDERATION_TRANSFER_OBJECT_SET_DIGEST_INVALID",
        )?;
        validate_timestamp(now, "FEDERATION_TRANSFER_TIMESTAMP_INVALID")?;

        let derived = derive_transfer_id(
            &proposed.source_fleet_id,
            &proposed.destination_fleet_id,
            &proposed.backup_id,
            &proposed.object_set_digest,
        )?;
        let identity_digest = transfer_identity_binding_digest(proposed)?;
        let initial_details = json!({"identityDigest": identity_digest});
        let (state_payload_digest, state_payload_json) = state_payload(
            &proposed.transfer_id,
            TransferState::Proposed,
            &initial_details,
        )?;

        let mut connection = self.connect()?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(database_error)?;
        if let Some(existing) = self.load_owned_record(&transaction, &proposed.transfer_id)? {
            if existing.identity_digest != identity_digest
                || existing.source_fleet_id != proposed.source_fleet_id
                || existing.destination_fleet_id != proposed.destination_fleet_id
                || existing.policy_id != proposed.policy_id
                || existing.backup_id != proposed.backup_id
                || existing.object_set_digest != proposed.object_set_digest
            {
                return Err(error("FEDERATION_TRANSFER_IDENTITY_CONFLICT"));
            }
            if proposed.transfer_id != derived {
                return Err(error("FEDERATION_TRANSFER_ID_INVALID"));
            }
            transaction.commit().map_err(database_error)?;
            return Ok(existing);
        }
        if proposed.transfer_id != derived {
            return Err(error("FEDERATION_TRANSFER_ID_INVALID"));
        }
        let role = if self.local_identity.fleet_id == proposed.source_fleet_id {
            TransferRole::Sender
        } else if self.local_identity.fleet_id == proposed.destination_fleet_id {
            TransferRole::Receiver
        } else {
            return Err(error("FEDERATION_TRANSFER_LOCAL_FLEET_NOT_PARTY"));
        };

        transaction
            .execute(
                r#"
                INSERT INTO federation_transfers (
                    transfer_id, identity_digest, local_fleet_id, role,
                    source_fleet_id, destination_fleet_id, policy_id,
                    backup_id, object_set_digest, state,
                    state_payload_digest, state_payload_json,
                    created_at, updated_at, revision
                ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?13, 1)
                "#,
                params![
                    proposed.transfer_id,
                    identity_digest,
                    self.local_identity.fleet_id,
                    role.as_str(),
                    proposed.source_fleet_id,
                    proposed.destination_fleet_id,
                    proposed.policy_id,
                    proposed.backup_id,
                    proposed.object_set_digest,
                    TransferState::Proposed.as_str(),
                    state_payload_digest,
                    state_payload_json,
                    now,
                ],
            )
            .map_err(database_error)?;
        transaction
            .execute(
                r#"
                INSERT INTO federation_transfer_events (
                    transfer_id, previous_state, next_state,
                    state_payload_digest, state_payload_json,
                    occurred_at, revision
                ) VALUES (?1, NULL, ?2, ?3, ?4, ?5, 1)
                "#,
                params![
                    proposed.transfer_id,
                    TransferState::Proposed.as_str(),
                    state_payload_digest,
                    state_payload_json,
                    now,
                ],
            )
            .map_err(database_error)?;
        let created = self
            .load_owned_record(&transaction, &proposed.transfer_id)?
            .ok_or_else(|| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
        transaction.commit().map_err(database_error)?;
        Ok(created)
    }

    pub fn advance_transfer(
        &self,
        transfer_id: &str,
        expected_revision: u64,
        next_state: TransferState,
        details: Value,
        now: &str,
    ) -> Result<TransferRecord, FederatedTransferJournalError> {
        validate_transfer_id(transfer_id)?;
        if expected_revision > i64::MAX as u64 {
            return Err(error("FEDERATION_TRANSFER_REVISION_INVALID"));
        }
        validate_timestamp(now, "FEDERATION_TRANSFER_TIMESTAMP_INVALID")?;
        let (payload_digest, payload_json) = state_payload(transfer_id, next_state, &details)?;

        let mut connection = self.connect()?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(database_error)?;
        let current = self
            .load_owned_record(&transaction, transfer_id)?
            .ok_or_else(|| error("FEDERATION_TRANSFER_NOT_FOUND"))?;
        if current.state == next_state {
            if current.state_payload_digest != payload_digest
                || canonical_state_payload(transfer_id, next_state, &current.state_details)?
                    != payload_json
            {
                return Err(error("FEDERATION_TRANSFER_STATE_CONFLICT"));
            }
            transaction.commit().map_err(database_error)?;
            return Ok(current);
        }
        if current.revision != expected_revision {
            return Err(error("FEDERATION_TRANSFER_REVISION_CONFLICT"));
        }
        if now < current.updated_at.as_str() {
            return Err(error("FEDERATION_TRANSFER_TIMESTAMP_REGRESSION"));
        }
        if current.state.next() != Some(next_state) {
            return Err(error("FEDERATION_TRANSFER_STATE_TRANSITION_INVALID"));
        }
        let next_revision = current
            .revision
            .checked_add(1)
            .filter(|revision| *revision <= i64::MAX as u64)
            .ok_or_else(|| error("FEDERATION_TRANSFER_REVISION_INVALID"))?;
        let changed = transaction
            .execute(
                r#"
                UPDATE federation_transfers
                SET state = ?1, state_payload_digest = ?2, state_payload_json = ?3,
                    updated_at = ?4, revision = ?5
                WHERE transfer_id = ?6 AND revision = ?7 AND state = ?8
                "#,
                params![
                    next_state.as_str(),
                    payload_digest,
                    payload_json,
                    now,
                    next_revision as i64,
                    transfer_id,
                    current.revision as i64,
                    current.state.as_str(),
                ],
            )
            .map_err(database_error)?;
        if changed != 1 {
            return Err(error("FEDERATION_TRANSFER_REVISION_CONFLICT"));
        }
        transaction
            .execute(
                r#"
                INSERT INTO federation_transfer_events (
                    transfer_id, previous_state, next_state,
                    state_payload_digest, state_payload_json,
                    occurred_at, revision
                ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
                "#,
                params![
                    transfer_id,
                    current.state.as_str(),
                    next_state.as_str(),
                    payload_digest,
                    payload_json,
                    now,
                    next_revision as i64,
                ],
            )
            .map_err(database_error)?;
        let updated = self
            .load_owned_record(&transaction, transfer_id)?
            .ok_or_else(|| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
        transaction.commit().map_err(database_error)?;
        Ok(updated)
    }

    pub fn get_transfer(
        &self,
        transfer_id: &str,
    ) -> Result<Option<TransferRecord>, FederatedTransferJournalError> {
        validate_transfer_id(transfer_id)?;
        self.load_owned_record(&self.connect()?, transfer_id)
    }

    pub fn list_transfers(&self) -> Result<Vec<TransferRecord>, FederatedTransferJournalError> {
        let connection = self.connect()?;
        let mut statement = connection
            .prepare("SELECT * FROM federation_transfers ORDER BY created_at, transfer_id")
            .map_err(database_error)?;
        let rows = statement
            .query_map([], raw_record_from_row)
            .map_err(database_error)?;
        let records: Vec<_> = rows
            .map(|row| {
                row.map_err(database_error)
                    .and_then(TransferRecord::try_from)
            })
            .collect::<Result<_, _>>()?;
        for record in &records {
            self.validate_owned_record(record)?;
        }
        Ok(records)
    }

    pub fn list_transfer_events(
        &self,
        transfer_id: &str,
    ) -> Result<Vec<TransferEvent>, FederatedTransferJournalError> {
        validate_transfer_id(transfer_id)?;
        let connection = self.connect()?;
        let mut statement = connection
            .prepare(
                r#"
                SELECT * FROM federation_transfer_events
                WHERE transfer_id = ?1 ORDER BY event_sequence
                "#,
            )
            .map_err(database_error)?;
        let rows = statement
            .query_map([transfer_id], raw_event_from_row)
            .map_err(database_error)?;
        let events: Vec<_> = rows
            .map(|row| {
                row.map_err(database_error)
                    .and_then(TransferEvent::try_from)
            })
            .collect::<Result<_, _>>()?;
        drop(statement);
        drop(connection);
        self.validate_event_chain(transfer_id, &events)?;
        Ok(events)
    }

    fn connect(&self) -> Result<Connection, FederatedTransferJournalError> {
        let connection = Connection::open(&self.db_path).map_err(database_error)?;
        connection
            .busy_timeout(BUSY_TIMEOUT)
            .map_err(database_error)?;
        connection
            .execute_batch("PRAGMA foreign_keys=ON;")
            .map_err(database_error)?;
        Ok(connection)
    }

    fn ensure_schema(&self) -> Result<(), FederatedTransferJournalError> {
        let connection = self.connect()?;
        connection
            .execute_batch(&format!(
                "PRAGMA journal_mode=WAL;\n{CREATE_JOURNAL_IDENTITY_SQL};\n{CREATE_TRANSFERS_SQL};\n{CREATE_TRANSFER_EVENTS_SQL};\n{CREATE_TRANSFER_EVENTS_INDEX_SQL};"
            ))
            .map_err(database_error)
    }

    fn bind_local_identity(&self) -> Result<(), FederatedTransferJournalError> {
        let mut connection = self.connect()?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(database_error)?;
        let existing = transaction
            .query_row(
                r#"
                SELECT local_fleet_id, root_fingerprint, identity_digest, identity_json
                FROM federation_transfer_journal_identity WHERE singleton = 1
                "#,
                [],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                    ))
                },
            )
            .optional()
            .map_err(database_error)?;
        if let Some((fleet_id, root_fingerprint, identity_digest, identity_json)) = existing {
            if fleet_id != self.local_identity.fleet_id
                || root_fingerprint != self.local_identity.root_fingerprint
                || identity_digest != self.local_identity.identity_digest
                || identity_json != self.local_identity.identity_json
            {
                return Err(error("FEDERATION_TRANSFER_JOURNAL_IDENTITY_CONFLICT"));
            }
        } else {
            transaction
                .execute(
                    r#"
                    INSERT INTO federation_transfer_journal_identity (
                        singleton, local_fleet_id, root_fingerprint,
                        identity_digest, identity_json, bound_at
                    ) VALUES (1, ?1, ?2, ?3, ?4, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                    "#,
                    params![
                        self.local_identity.fleet_id,
                        self.local_identity.root_fingerprint,
                        self.local_identity.identity_digest,
                        self.local_identity.identity_json,
                    ],
                )
                .map_err(database_error)?;
        }
        transaction.commit().map_err(database_error)
    }

    fn load_owned_record(
        &self,
        connection: &Connection,
        transfer_id: &str,
    ) -> Result<Option<TransferRecord>, FederatedTransferJournalError> {
        let record = load_record(connection, transfer_id)?;
        if let Some(record) = record.as_ref() {
            self.validate_owned_record(record)?;
        }
        Ok(record)
    }

    fn validate_owned_record(
        &self,
        record: &TransferRecord,
    ) -> Result<(), FederatedTransferJournalError> {
        let corrupt = || error("FEDERATION_TRANSFER_JOURNAL_CORRUPT");
        if record.local_fleet_id != self.local_identity.fleet_id
            || record.source_fleet_id == record.destination_fleet_id
            || record.created_at > record.updated_at
            || record.revision != record.state.revision()
        {
            return Err(corrupt());
        }
        let expected_role = if record.local_fleet_id == record.source_fleet_id {
            TransferRole::Sender
        } else if record.local_fleet_id == record.destination_fleet_id {
            TransferRole::Receiver
        } else {
            return Err(corrupt());
        };
        if record.role != expected_role {
            return Err(corrupt());
        }
        let proposed = ProposedTransfer {
            transfer_id: record.transfer_id.clone(),
            source_fleet_id: record.source_fleet_id.clone(),
            destination_fleet_id: record.destination_fleet_id.clone(),
            policy_id: record.policy_id.clone(),
            backup_id: record.backup_id.clone(),
            object_set_digest: record.object_set_digest.clone(),
        };
        let derived = derive_transfer_id(
            &proposed.source_fleet_id,
            &proposed.destination_fleet_id,
            &proposed.backup_id,
            &proposed.object_set_digest,
        )
        .map_err(|_| corrupt())?;
        let identity_digest = transfer_identity_binding_digest(&proposed).map_err(|_| corrupt())?;
        if record.transfer_id != derived || record.identity_digest != identity_digest {
            return Err(corrupt());
        }
        if record.state == TransferState::Proposed
            && record.state_details != json!({"identityDigest": record.identity_digest})
        {
            return Err(corrupt());
        }
        Ok(())
    }

    fn validate_event_chain(
        &self,
        transfer_id: &str,
        events: &[TransferEvent],
    ) -> Result<(), FederatedTransferJournalError> {
        let corrupt = || error("FEDERATION_TRANSFER_JOURNAL_CORRUPT");
        let record = self.get_transfer(transfer_id)?;
        let Some(record) = record else {
            return if events.is_empty() {
                Ok(())
            } else {
                Err(corrupt())
            };
        };
        if events.len() as u64 != record.revision {
            return Err(corrupt());
        }
        for (index, event) in events.iter().enumerate() {
            let expected_state = TransferState::ALL.get(index).copied().ok_or_else(corrupt)?;
            let expected_previous = index
                .checked_sub(1)
                .and_then(|previous| TransferState::ALL.get(previous).copied());
            if event.transfer_id != transfer_id
                || event.revision != index as u64 + 1
                || event.previous_state != expected_previous
                || event.next_state != expected_state
                || index > 0 && event.sequence <= events[index - 1].sequence
                || index > 0 && event.occurred_at < events[index - 1].occurred_at
            {
                return Err(corrupt());
            }
        }
        if events[0].occurred_at != record.created_at
            || events[0].state_details != json!({"identityDigest": record.identity_digest})
        {
            return Err(corrupt());
        }
        let last = events.last().ok_or_else(corrupt)?;
        if last.next_state != record.state
            || last.state_payload_digest != record.state_payload_digest
            || last.state_details != record.state_details
            || last.occurred_at != record.updated_at
        {
            return Err(corrupt());
        }
        Ok(())
    }
}

pub fn transfer_identity_document(
    source_fleet_id: &str,
    destination_fleet_id: &str,
    backup_id: &str,
    object_set_digest: &str,
) -> Result<Value, FederatedTransferJournalError> {
    validate_fleet_id(source_fleet_id)?;
    validate_fleet_id(destination_fleet_id)?;
    if source_fleet_id == destination_fleet_id {
        return Err(error("FEDERATION_TRANSFER_REFLECTION_REJECTED"));
    }
    validate_control_id(backup_id, "FEDERATION_TRANSFER_BACKUP_ID_INVALID")?;
    validate_typed_sha256(
        object_set_digest,
        "FEDERATION_TRANSFER_OBJECT_SET_DIGEST_INVALID",
    )?;
    Ok(json!({
        "schema": FEDERATED_TRANSFER_IDENTITY_SCHEMA,
        "sourceFleetId": source_fleet_id,
        "destinationFleetId": destination_fleet_id,
        "backupId": backup_id,
        "objectSetDigest": object_set_digest,
    }))
}

pub fn derive_transfer_id(
    source_fleet_id: &str,
    destination_fleet_id: &str,
    backup_id: &str,
    object_set_digest: &str,
) -> Result<String, FederatedTransferJournalError> {
    let identity = transfer_identity_document(
        source_fleet_id,
        destination_fleet_id,
        backup_id,
        object_set_digest,
    )?;
    let mut message = TRANSFER_ID_DOMAIN.to_vec();
    message.extend(canonical_bytes(&identity)?);
    Ok(typed_sha256(&message))
}

fn transfer_identity_binding_digest(
    proposed: &ProposedTransfer,
) -> Result<String, FederatedTransferJournalError> {
    let binding = json!({
        "schema": TRANSFER_IDENTITY_BINDING_SCHEMA,
        "transferId": proposed.transfer_id,
        "sourceFleetId": proposed.source_fleet_id,
        "destinationFleetId": proposed.destination_fleet_id,
        "policyId": proposed.policy_id,
        "backupId": proposed.backup_id,
        "objectSetDigest": proposed.object_set_digest,
    });
    Ok(typed_sha256(&canonical_bytes(&binding)?))
}

fn state_payload(
    transfer_id: &str,
    state: TransferState,
    details: &Value,
) -> Result<(String, String), FederatedTransferJournalError> {
    if !details.is_object() {
        return Err(error("FEDERATION_TRANSFER_STATE_DETAILS_INVALID"));
    }
    if contains_sensitive_key(details) {
        return Err(error("FEDERATION_TRANSFER_SENSITIVE_STATE_REJECTED"));
    }
    if canonical_bytes(details)?.len() > MAX_STATE_DETAILS_BYTES {
        return Err(error("FEDERATION_TRANSFER_STATE_DETAILS_TOO_LARGE"));
    }
    let payload_json = canonical_state_payload(transfer_id, state, details)?;
    Ok((typed_sha256(payload_json.as_bytes()), payload_json))
}

fn canonical_state_payload(
    transfer_id: &str,
    state: TransferState,
    details: &Value,
) -> Result<String, FederatedTransferJournalError> {
    let payload = json!({
        "schema": TRANSFER_STATE_PAYLOAD_SCHEMA,
        "transferId": transfer_id,
        "state": state.as_str(),
        "details": details,
    });
    canonical_json(&payload)
}

fn contains_sensitive_key(value: &Value) -> bool {
    match value {
        Value::Object(items) => items.iter().any(|(key, item)| {
            let normalized: String = key
                .chars()
                .flat_map(char::to_lowercase)
                .filter(char::is_ascii_alphanumeric)
                .collect();
            SENSITIVE_KEY_MARKERS
                .iter()
                .any(|marker| normalized.contains(marker))
                || contains_sensitive_key(item)
        }),
        Value::Array(items) => items.iter().any(contains_sensitive_key),
        _ => false,
    }
}

fn contains_identity_secret(value: &Value) -> bool {
    match value {
        Value::Object(items) => items.iter().any(|(key, item)| {
            let normalized: String = key
                .chars()
                .flat_map(char::to_lowercase)
                .filter(char::is_ascii_alphanumeric)
                .collect();
            IDENTITY_SECRET_KEY_MARKERS
                .iter()
                .any(|marker| normalized.contains(marker))
                || contains_identity_secret(item)
        }),
        Value::Array(items) => items.iter().any(contains_identity_secret),
        Value::String(item) => {
            let lowered = item.to_lowercase();
            IDENTITY_SECRET_VALUE_MARKERS
                .iter()
                .any(|marker| lowered.contains(marker))
        }
        _ => false,
    }
}

fn validate_local_identity(
    identity: &Value,
) -> Result<LocalFleetIdentity, FederatedTransferJournalError> {
    let object = identity
        .as_object()
        .ok_or_else(|| error("FEDERATION_ROOT_IDENTITY_INVALID"))?;
    if string_field(object, "schema") != Some(FLEET_IDENTITY_SCHEMA) {
        return Err(error("FEDERATION_ROOT_IDENTITY_SCHEMA_INVALID"));
    }
    let fleet_id =
        string_field(object, "fleetId").ok_or_else(|| error("FEDERATION_FLEET_ID_INVALID"))?;
    validate_fleet_id(fleet_id).map_err(|_| error("FEDERATION_FLEET_ID_INVALID"))?;
    if contains_identity_secret(identity) {
        return Err(error("FEDERATION_DOCUMENT_CONTAINS_SECRET"));
    }
    if string_field(object, "signatureAlgorithm") != Some(SIGNATURE_ALGORITHM) {
        return Err(error("FEDERATION_ROOT_IDENTITY_ALGORITHM_INVALID"));
    }
    let public_key = string_field(object, "rootPublicKey")
        .and_then(|value| URL_SAFE_NO_PAD.decode(value).ok())
        .filter(|value| value.len() == 32)
        .ok_or_else(|| error("FEDERATION_ROOT_PUBLIC_KEY_INVALID"))?;
    let public_digest = sha256_hex(&public_key);
    if string_field(object, "rootKeyId")
        != Some(format!("fed-root-{}", &public_digest[..24]).as_str())
    {
        return Err(error("FEDERATION_ROOT_KEY_ID_INVALID"));
    }
    let root_fingerprint = typed_sha256(&public_key);
    if string_field(object, "rootFingerprint") != Some(root_fingerprint.as_str()) {
        return Err(error("FEDERATION_ROOT_FINGERPRINT_INVALID"));
    }
    string_field(object, "createdAt")
        .ok_or_else(|| error("FEDERATION_ROOT_IDENTITY_TIMESTAMP_INVALID"))
        .and_then(|value| {
            validate_timestamp(value, "FEDERATION_ROOT_IDENTITY_TIMESTAMP_INVALID")
        })?;
    let identity_json = canonical_json(identity)?;
    Ok(LocalFleetIdentity {
        document: identity.clone(),
        fleet_id: fleet_id.to_string(),
        root_fingerprint,
        identity_digest: typed_sha256(identity_json.as_bytes()),
        identity_json,
    })
}

fn validate_fleet_id(value: &str) -> Result<(), FederatedTransferJournalError> {
    let bytes = value.as_bytes();
    if bytes.is_empty()
        || bytes.len() > 128
        || !(bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        || !bytes.iter().skip(1).all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
    {
        return Err(error("FEDERATION_TRANSFER_FLEET_ID_INVALID"));
    }
    Ok(())
}

fn validate_control_id(
    value: &str,
    code: &'static str,
) -> Result<(), FederatedTransferJournalError> {
    let bytes = value.as_bytes();
    if bytes.is_empty()
        || bytes.len() > 128
        || !bytes[0].is_ascii_alphanumeric()
        || !bytes
            .iter()
            .skip(1)
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
    {
        return Err(error(code));
    }
    Ok(())
}

fn validate_transfer_id(value: &str) -> Result<(), FederatedTransferJournalError> {
    validate_typed_sha256(value, "FEDERATION_TRANSFER_ID_INVALID")
}

fn validate_typed_sha256(
    value: &str,
    code: &'static str,
) -> Result<(), FederatedTransferJournalError> {
    let Some(digest) = value.strip_prefix("sha256:") else {
        return Err(error(code));
    };
    if digest.len() != 64
        || !digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(error(code));
    }
    Ok(())
}

fn validate_timestamp(
    value: &str,
    code: &'static str,
) -> Result<(), FederatedTransferJournalError> {
    parse_timestamp(value)
        .map(|_| ())
        .ok_or_else(|| error(code))
}

fn parse_timestamp(value: &str) -> Option<i64> {
    let bytes = value.as_bytes();
    if bytes.len() != 20
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'Z'
    {
        return None;
    }
    let year = decimal(bytes, 0, 4)? as i64;
    let month = decimal(bytes, 5, 2)? as i64;
    let day = decimal(bytes, 8, 2)? as i64;
    let hour = decimal(bytes, 11, 2)? as i64;
    let minute = decimal(bytes, 14, 2)? as i64;
    let second = decimal(bytes, 17, 2)? as i64;
    if year == 0 || !(1..=12).contains(&month) || hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let leap_year = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let days_in_month = match month {
        2 if leap_year => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    if !(1..=days_in_month).contains(&day) {
        return None;
    }
    Some(days_from_civil(year, month, day) * 86_400 + hour * 3_600 + minute * 60 + second)
}

fn decimal(bytes: &[u8], start: usize, length: usize) -> Option<u32> {
    bytes
        .get(start..start + length)?
        .iter()
        .try_fold(0_u32, |value, byte| {
            byte.is_ascii_digit()
                .then(|| value * 10 + u32::from(*byte - b'0'))
        })
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let adjusted_year = year - i64::from(month <= 2);
    let era = adjusted_year.div_euclid(400);
    let year_of_era = adjusted_year - era * 400;
    let adjusted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * adjusted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

fn string_field<'a>(value: &'a Map<String, Value>, field: &str) -> Option<&'a str> {
    value.get(field)?.as_str()
}

fn canonical_bytes(value: &Value) -> Result<Vec<u8>, FederatedTransferJournalError> {
    let mut sorted = value.clone();
    sorted.sort_all_objects();
    serde_json::to_vec(&sorted).map_err(|_| error("FEDERATION_TRANSFER_CANONICAL_PAYLOAD_INVALID"))
}

fn canonical_json(value: &Value) -> Result<String, FederatedTransferJournalError> {
    String::from_utf8(canonical_bytes(value)?)
        .map_err(|_| error("FEDERATION_TRANSFER_CANONICAL_PAYLOAD_INVALID"))
}

fn typed_sha256(bytes: &[u8]) -> String {
    format!("sha256:{}", sha256_hex(bytes))
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut encoded = String::with_capacity(digest.len() * 2);
    for byte in digest {
        let _ = write!(encoded, "{byte:02x}");
    }
    encoded
}

#[derive(Debug)]
struct RawRecord {
    transfer_id: String,
    identity_digest: String,
    local_fleet_id: String,
    role: String,
    source_fleet_id: String,
    destination_fleet_id: String,
    policy_id: String,
    backup_id: String,
    object_set_digest: String,
    state: String,
    state_payload_digest: String,
    state_payload_json: String,
    created_at: String,
    updated_at: String,
    revision: i64,
}

fn raw_record_from_row(row: &Row<'_>) -> rusqlite::Result<RawRecord> {
    Ok(RawRecord {
        transfer_id: row.get("transfer_id")?,
        identity_digest: row.get("identity_digest")?,
        local_fleet_id: row.get("local_fleet_id")?,
        role: row.get("role")?,
        source_fleet_id: row.get("source_fleet_id")?,
        destination_fleet_id: row.get("destination_fleet_id")?,
        policy_id: row.get("policy_id")?,
        backup_id: row.get("backup_id")?,
        object_set_digest: row.get("object_set_digest")?,
        state: row.get("state")?,
        state_payload_digest: row.get("state_payload_digest")?,
        state_payload_json: row.get("state_payload_json")?,
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
        revision: row.get("revision")?,
    })
}

fn load_record(
    connection: &Connection,
    transfer_id: &str,
) -> Result<Option<TransferRecord>, FederatedTransferJournalError> {
    connection
        .query_row(
            "SELECT * FROM federation_transfers WHERE transfer_id = ?1",
            [transfer_id],
            raw_record_from_row,
        )
        .optional()
        .map_err(database_error)?
        .map(TransferRecord::try_from)
        .transpose()
}

impl TryFrom<RawRecord> for TransferRecord {
    type Error = FederatedTransferJournalError;

    fn try_from(raw: RawRecord) -> Result<Self, Self::Error> {
        let state = TransferState::parse(&raw.state)?;
        let role = TransferRole::parse(&raw.role)?;
        validate_transfer_id(&raw.transfer_id)
            .map_err(|_| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
        validate_typed_sha256(&raw.identity_digest, "FEDERATION_TRANSFER_JOURNAL_CORRUPT")?;
        validate_fleet_id(&raw.local_fleet_id)
            .map_err(|_| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
        validate_fleet_id(&raw.source_fleet_id)
            .map_err(|_| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
        validate_fleet_id(&raw.destination_fleet_id)
            .map_err(|_| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
        validate_control_id(&raw.policy_id, "FEDERATION_TRANSFER_JOURNAL_CORRUPT")?;
        validate_control_id(&raw.backup_id, "FEDERATION_TRANSFER_JOURNAL_CORRUPT")?;
        validate_typed_sha256(
            &raw.object_set_digest,
            "FEDERATION_TRANSFER_JOURNAL_CORRUPT",
        )?;
        validate_typed_sha256(
            &raw.state_payload_digest,
            "FEDERATION_TRANSFER_JOURNAL_CORRUPT",
        )?;
        validate_timestamp(&raw.created_at, "FEDERATION_TRANSFER_JOURNAL_CORRUPT")?;
        validate_timestamp(&raw.updated_at, "FEDERATION_TRANSFER_JOURNAL_CORRUPT")?;
        let revision = u64::try_from(raw.revision)
            .ok()
            .filter(|revision| *revision >= 1)
            .ok_or_else(|| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
        let details = validate_stored_payload(
            &raw.state_payload_json,
            &raw.state_payload_digest,
            &raw.transfer_id,
            state,
        )?;
        Ok(Self {
            schema: TRANSFER_JOURNAL_RECORD_SCHEMA.to_string(),
            transfer_id: raw.transfer_id,
            identity_digest: raw.identity_digest,
            local_fleet_id: raw.local_fleet_id,
            role,
            source_fleet_id: raw.source_fleet_id,
            destination_fleet_id: raw.destination_fleet_id,
            policy_id: raw.policy_id,
            backup_id: raw.backup_id,
            object_set_digest: raw.object_set_digest,
            state,
            state_payload_digest: raw.state_payload_digest,
            state_details: details,
            created_at: raw.created_at,
            updated_at: raw.updated_at,
            revision,
        })
    }
}

#[derive(Debug)]
struct RawEvent {
    sequence: i64,
    transfer_id: String,
    previous_state: Option<String>,
    next_state: String,
    state_payload_digest: String,
    state_payload_json: String,
    occurred_at: String,
    revision: i64,
}

fn raw_event_from_row(row: &Row<'_>) -> rusqlite::Result<RawEvent> {
    Ok(RawEvent {
        sequence: row.get("event_sequence")?,
        transfer_id: row.get("transfer_id")?,
        previous_state: row.get("previous_state")?,
        next_state: row.get("next_state")?,
        state_payload_digest: row.get("state_payload_digest")?,
        state_payload_json: row.get("state_payload_json")?,
        occurred_at: row.get("occurred_at")?,
        revision: row.get("revision")?,
    })
}

impl TryFrom<RawEvent> for TransferEvent {
    type Error = FederatedTransferJournalError;

    fn try_from(raw: RawEvent) -> Result<Self, Self::Error> {
        let sequence = u64::try_from(raw.sequence)
            .ok()
            .filter(|sequence| *sequence >= 1)
            .ok_or_else(|| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
        let revision = u64::try_from(raw.revision)
            .ok()
            .filter(|revision| *revision >= 1)
            .ok_or_else(|| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
        validate_transfer_id(&raw.transfer_id)
            .map_err(|_| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
        let previous_state = raw
            .previous_state
            .as_deref()
            .map(TransferState::parse)
            .transpose()?;
        let next_state = TransferState::parse(&raw.next_state)?;
        validate_timestamp(&raw.occurred_at, "FEDERATION_TRANSFER_JOURNAL_CORRUPT")?;
        let details = validate_stored_payload(
            &raw.state_payload_json,
            &raw.state_payload_digest,
            &raw.transfer_id,
            next_state,
        )?;
        Ok(Self {
            schema: TRANSFER_JOURNAL_EVENT_SCHEMA.to_string(),
            sequence,
            transfer_id: raw.transfer_id,
            previous_state,
            next_state,
            state_payload_digest: raw.state_payload_digest,
            state_details: details,
            occurred_at: raw.occurred_at,
            revision,
        })
    }
}

fn validate_stored_payload(
    payload_json: &str,
    payload_digest: &str,
    transfer_id: &str,
    state: TransferState,
) -> Result<Value, FederatedTransferJournalError> {
    let payload: Value = serde_json::from_str(payload_json)
        .map_err(|_| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
    let object = payload
        .as_object()
        .ok_or_else(|| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
    if object.len() != 4
        || string_field(object, "schema") != Some(TRANSFER_STATE_PAYLOAD_SCHEMA)
        || string_field(object, "transferId") != Some(transfer_id)
        || string_field(object, "state") != Some(state.as_str())
    {
        return Err(error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"));
    }
    let details = object
        .get("details")
        .filter(|value| value.is_object())
        .ok_or_else(|| error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"))?;
    if contains_sensitive_key(details) || canonical_bytes(details)?.len() > MAX_STATE_DETAILS_BYTES
    {
        return Err(error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"));
    }
    let expected_json = canonical_state_payload(transfer_id, state, details)?;
    if expected_json != payload_json || typed_sha256(payload_json.as_bytes()) != payload_digest {
        return Err(error("FEDERATION_TRANSFER_JOURNAL_CORRUPT"));
    }
    Ok(details.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timestamp_and_identifier_validation_are_strict() {
        assert!(parse_timestamp("2024-02-29T23:59:59Z").is_some());
        assert!(parse_timestamp("2026-02-29T00:00:00Z").is_none());
        assert!(validate_fleet_id("fleet-a_1.example").is_ok());
        assert!(validate_fleet_id("Fleet-A").is_err());
        assert!(validate_control_id("target:a-1", "bad").is_ok());
        assert!(validate_control_id("target/a", "bad").is_err());
    }

    #[test]
    fn sensitive_keys_are_rejected_recursively() {
        assert!(contains_sensitive_key(&json!({
            "nested": [{"private_key": "never persist"}]
        })));
        assert!(contains_sensitive_key(&json!({
            "credentialReferenceDigest": "not accepted because credential is still sensitive"
        })));
        assert!(!contains_sensitive_key(&json!({
            "rootPublicKey": "public-material"
        })));
        assert!(contains_identity_secret(&json!({
            "note": "AGE-SECRET-KEY-MUST-NOT-LEAK"
        })));
        assert!(contains_identity_secret(&json!({
            "passphrase": "must-not-leak"
        })));
        assert!(!contains_identity_secret(&json!({
            "rootPublicKey": "public-material"
        })));
    }

    #[test]
    fn stored_sensitive_payload_is_rejected_even_with_a_matching_digest() {
        let transfer_id = format!("sha256:{}", "1".repeat(64));
        let details = json!({"receiverAccessKey": "must-not-be-journaled"});
        let payload_json =
            canonical_state_payload(&transfer_id, TransferState::GrantRequested, &details).unwrap();
        let digest = typed_sha256(payload_json.as_bytes());
        let result = validate_stored_payload(
            &payload_json,
            &digest,
            &transfer_id,
            TransferState::GrantRequested,
        );
        assert_eq!(
            result.unwrap_err().code(),
            "FEDERATION_TRANSFER_JOURNAL_CORRUPT"
        );
    }
}
