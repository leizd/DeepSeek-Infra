use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const BACKUP_FENCE_TTL_MS: i64 = 60 * 60 * 1000;
pub const RESTORE_FENCE_TTL_MS: i64 = 60 * 60 * 1000;
pub const RESTORE_JOURNAL_TTL_MS: i64 = 24 * 60 * 60 * 1000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StoreError {
    BackupFenced,
    RestoreFenced,
    IdempotencyConflict,
    RestoreConflict,
    RestoreState(String),
    Message(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::BackupFenced => write!(formatter, "durable task mutations are fenced for backup"),
            Self::RestoreFenced => {
                write!(formatter, "durable task mutations are fenced for restore")
            }
            Self::IdempotencyConflict => {
                write!(
                    formatter,
                    "idempotency key was already used with different test arguments"
                )
            }
            Self::RestoreConflict => write!(
                formatter,
                "restore target state changed during the transaction"
            ),
            Self::RestoreState(message) | Self::Message(message) => write!(formatter, "{message}"),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TaskStatus {
    Queued,
    Running,
    Succeeded,
    Failed,
    Cancelled,
    Interrupted,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TaskArguments {
    pub target: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keyword: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub markers: Option<String>,
    pub timeout_seconds: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TaskRecord {
    pub id: String,
    pub kind: String,
    pub idempotency_key_hash: String,
    pub request_hash: String,
    pub arguments: TaskArguments,
    pub status: TaskStatus,
    pub owner_instance: Option<String>,
    pub lease_until: Option<i64>,
    pub attempts: u64,
    pub stdout: String,
    pub stderr: String,
    pub exit_code: Option<i32>,
    pub error: Option<String>,
    pub created_at: i64,
    pub updated_at: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub restore_pending: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CreateTaskInput {
    pub idempotency_key: String,
    pub arguments: TaskArguments,
    pub now: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CreateTaskResult {
    pub task: TaskRecord,
    pub deduplicated: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TaskOutcome {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: Option<i32>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BackupCapabilities {
    pub contributor_id: String,
    pub schema_version: u32,
    pub data_class: String,
    pub available: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BackupFence {
    pub backup_id: String,
    pub generation: i64,
    pub created_at: i64,
    pub expires_at: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum RestorePhase {
    Preparing,
    Prepared,
    CommitIntent,
    Committing,
    CommittedPendingComplete,
    Complete,
    Aborting,
    RolledBack,
    RecoveryRequired,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RestoreJournal {
    pub contributor_id: String,
    pub schema_version: u32,
    pub restore_id: String,
    pub transaction_digest: String,
    pub source_digest: String,
    pub prepared_digest: String,
    pub phase: RestorePhase,
    pub records: u64,
    pub imported: u64,
    pub skipped: u64,
    pub interrupted: u64,
    pub remapped: std::collections::BTreeMap<String, String>,
    pub previous_epoch: String,
    pub restore_epoch: String,
    pub created_at: i64,
    pub updated_at: i64,
}

pub fn digest(value: &str) -> String {
    hex_encode(Sha256::digest(value.as_bytes()).as_slice())
}

pub fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0xf) as usize] as char);
    }
    out
}

fn json_string(value: &str) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "\"\"".to_string())
}

fn json_opt_string(value: &Option<String>) -> String {
    match value {
        Some(text) => json_string(text),
        None => "null".to_string(),
    }
}

pub fn arguments_json(arguments: &TaskArguments) -> String {
    let mut body = format!("{{\"target\":{}", json_string(&arguments.target));
    if let Some(keyword) = &arguments.keyword {
        body.push_str(",\"keyword\":");
        body.push_str(&json_string(keyword));
    }
    if let Some(markers) = &arguments.markers {
        body.push_str(",\"markers\":");
        body.push_str(&json_string(markers));
    }
    body.push_str(&format!(
        ",\"timeoutSeconds\":{}}}",
        arguments.timeout_seconds
    ));
    body
}

pub fn canonical_request_hash(arguments: &TaskArguments) -> String {
    let keyword = match &arguments.keyword {
        Some(value) => json_string(value),
        None => "null".to_string(),
    };
    let markers = match &arguments.markers {
        Some(value) => json_string(value),
        None => "null".to_string(),
    };
    digest(&format!(
        "{{\"target\":{},\"keyword\":{},\"markers\":{},\"timeoutSeconds\":{}}}",
        json_string(&arguments.target),
        keyword,
        markers,
        arguments.timeout_seconds
    ))
}

pub fn task_json(task: &TaskRecord) -> String {
    let mut body = format!(
        "{{\"id\":{},\"kind\":{},\"idempotencyKeyHash\":{},\"requestHash\":{},\"arguments\":{},\"status\":{},\"ownerInstance\":{},\"leaseUntil\":{},\"attempts\":{},\"stdout\":{},\"stderr\":{},\"exitCode\":{},\"error\":{},\"createdAt\":{},\"updatedAt\":{}",
        json_string(&task.id),
        json_string(&task.kind),
        json_string(&task.idempotency_key_hash),
        json_string(&task.request_hash),
        arguments_json(&task.arguments),
        json_string(status_name(task.status)),
        json_opt_string(&task.owner_instance),
        match task.lease_until {
            Some(value) => value.to_string(),
            None => "null".to_string(),
        },
        task.attempts,
        json_string(&task.stdout),
        json_string(&task.stderr),
        match task.exit_code {
            Some(value) => value.to_string(),
            None => "null".to_string(),
        },
        json_opt_string(&task.error),
        task.created_at,
        task.updated_at,
    );
    if let Some(pending) = &task.restore_pending {
        body.push_str(",\"restorePending\":");
        body.push_str(&json_string(pending));
    }
    body.push('}');
    body
}

pub fn outcome_json(outcome: &TaskOutcome) -> String {
    format!(
        "{{\"stdout\":{},\"stderr\":{},\"exitCode\":{},\"error\":{}}}",
        json_string(&outcome.stdout),
        json_string(&outcome.stderr),
        match outcome.exit_code {
            Some(value) => value.to_string(),
            None => "null".to_string(),
        },
        json_opt_string(&outcome.error),
    )
}

fn status_name(status: TaskStatus) -> &'static str {
    match status {
        TaskStatus::Queued => "queued",
        TaskStatus::Running => "running",
        TaskStatus::Succeeded => "succeeded",
        TaskStatus::Failed => "failed",
        TaskStatus::Cancelled => "cancelled",
        TaskStatus::Interrupted => "interrupted",
    }
}

pub fn portable_task(task: &TaskRecord) -> TaskRecord {
    let mut portable = task.clone();
    portable.restore_pending = None;
    if task.status == TaskStatus::Running || task.status == TaskStatus::Queued {
        portable.status = TaskStatus::Interrupted;
    }
    portable.owner_instance = None;
    portable.lease_until = None;
    portable
}

pub fn task_digest(task: &TaskRecord) -> String {
    digest(&task_json(&portable_task(task)))
}

pub fn deterministic_task_id(restore_id: &str, task_id: &str, value: &str) -> String {
    let hex = digest(&format!("{restore_id} {task_id} {value}"));
    format!(
        "{}-{}-5{}-a{}-{}",
        &hex[0..8],
        &hex[8..12],
        &hex[13..16],
        &hex[17..20],
        &hex[20..32]
    )
}

pub fn make_task(input: &CreateTaskInput, id: String) -> TaskRecord {
    TaskRecord {
        id,
        kind: "test-run".to_string(),
        idempotency_key_hash: digest(&input.idempotency_key),
        request_hash: canonical_request_hash(&input.arguments),
        arguments: input.arguments.clone(),
        status: TaskStatus::Queued,
        owner_instance: None,
        lease_until: None,
        attempts: 0,
        stdout: String::new(),
        stderr: String::new(),
        exit_code: None,
        error: None,
        created_at: input.now,
        updated_at: input.now,
        restore_pending: None,
    }
}

pub fn capabilities() -> BackupCapabilities {
    BackupCapabilities {
        contributor_id: "stateless-mcp".to_string(),
        schema_version: 1,
        data_class: "durable".to_string(),
        available: true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_request_hash_matches_the_typescript_object_order() {
        let arguments = TaskArguments {
            target: "tests/test_mcp.py".to_string(),
            keyword: None,
            markers: None,
            timeout_seconds: 30,
        };
        assert_eq!(
            canonical_request_hash(&arguments),
            "bf603e3506baad5705f05d23441e7f3dff90dee143a2fdfe81c0cadefd560a8a"
        );
    }
}
