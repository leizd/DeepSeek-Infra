//! Rust-owned immutable Agent bodies and their public content projection.
//! Go remains the only owner of run status, plan, indices and node state.

use std::{fs, path::Path, time::Duration};

use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::core_utils::python_truthy;

pub const MAX_ARTIFACT_BYTES: u64 = 128 * 1024 * 1024;
const APPLICATION_ID: i32 = 0x44534142;
const SCHEMA_VERSION: i32 = 1;
const DATABASE: &str = "objects-v1.sqlite";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactReference {
    pub sha256: String,
    pub length: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DataError {
    NotOwned,
    Unavailable,
    Invalid,
    Corrupt,
}

pub struct ArtifactStore {
    connection: Connection,
    writable: bool,
}

fn safe_path(root: &Path) -> Result<(), DataError> {
    for ancestor in root.ancestors() {
        if ancestor.as_os_str().is_empty() {
            continue;
        }
        match fs::symlink_metadata(ancestor) {
            Ok(metadata) if metadata.is_symlink() || !metadata.is_dir() => {
                return Err(DataError::Invalid);
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(DataError::Unavailable),
        }
    }
    for name in [
        DATABASE,
        "objects-v1.sqlite-journal",
        "objects-v1.sqlite-wal",
        "objects-v1.sqlite-shm",
    ] {
        match fs::symlink_metadata(root.join(name)) {
            Ok(metadata) if metadata.is_symlink() || !metadata.is_file() => {
                return Err(DataError::Invalid);
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(DataError::Unavailable),
        }
    }
    Ok(())
}

fn validate_database(connection: &Connection) -> Result<(), DataError> {
    let identity: i32 = connection
        .query_row("PRAGMA application_id", [], |row| row.get(0))
        .map_err(|_| DataError::Corrupt)?;
    let version: i32 = connection
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .map_err(|_| DataError::Corrupt)?;
    if identity != APPLICATION_ID || version != SCHEMA_VERSION {
        return Err(DataError::Corrupt);
    }
    connection
        .prepare("SELECT sha256, length, bytes FROM artifacts")
        .map_err(|_| DataError::Corrupt)?;
    Ok(())
}

fn valid_reference(reference: &ArtifactReference) -> bool {
    reference.sha256.len() == 64
        && reference
            .sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        && reference.length > 0
        && reference.length <= MAX_ARTIFACT_BYTES
}

fn read_body(connection: &Connection, reference: &ArtifactReference) -> Result<Value, DataError> {
    if !valid_reference(reference) {
        return Err(DataError::Invalid);
    }
    let mut statement = connection
        .prepare("SELECT length, bytes FROM artifacts WHERE sha256=?1")
        .map_err(|_| DataError::Corrupt)?;
    let mut rows = statement
        .query([&reference.sha256])
        .map_err(|_| DataError::Unavailable)?;
    let row = rows
        .next()
        .map_err(|_| DataError::Corrupt)?
        .ok_or(DataError::Unavailable)?;
    let length: i64 = row.get(0).map_err(|_| DataError::Corrupt)?;
    let bytes = row
        .get_ref(1)
        .map_err(|_| DataError::Corrupt)?
        .as_blob()
        .map_err(|_| DataError::Corrupt)?;
    if length <= 0
        || length as u64 != reference.length
        || bytes.len() as u64 != reference.length
        || format!("{:x}", Sha256::digest(bytes)) != reference.sha256
    {
        return Err(DataError::Corrupt);
    }
    let value: Value = serde_json::from_slice(bytes).map_err(|_| DataError::Corrupt)?;
    if !value.is_object() || sanitize(&value) != value {
        return Err(DataError::Corrupt);
    }
    Ok(value)
}

impl ArtifactStore {
    /// Only the native process writes this new namespace. This cannot promote a
    /// Go domain, authorize a task effect, or adopt a legacy Agent database.
    pub fn open_native(root: &Path, mode: &str) -> Result<Self, DataError> {
        if !mode.trim().eq_ignore_ascii_case("python_disabled") {
            return Err(DataError::NotOwned);
        }
        safe_path(root)?;
        let database = root.join(DATABASE);
        let existed = database.exists();
        let root_existed = root.exists();
        fs::create_dir_all(root).map_err(|_| DataError::Unavailable)?;
        #[cfg(unix)]
        if !root_existed {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(root, fs::Permissions::from_mode(0o700))
                .map_err(|_| DataError::Unavailable)?;
        }
        #[cfg(not(unix))]
        let _ = root_existed;
        safe_path(root)?;
        let mut connection = Connection::open_with_flags(
            &database,
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_CREATE
                | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|_| DataError::Unavailable)?;
        #[cfg(unix)]
        if !existed {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&database, fs::Permissions::from_mode(0o600))
                .map_err(|_| DataError::Unavailable)?;
        }
        connection
            .busy_timeout(Duration::from_secs(5))
            .map_err(|_| DataError::Unavailable)?;
        if existed {
            // Inspect before changing any pragma, schema or user data.
            validate_database(&connection)?;
        } else {
            let transaction = connection
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .map_err(|_| DataError::Unavailable)?;
            let tables: i64 = transaction
                .query_row(
                    "SELECT count(*) FROM sqlite_schema WHERE type='table'",
                    [],
                    |row| row.get(0),
                )
                .map_err(|_| DataError::Corrupt)?;
            if tables == 0 {
                transaction.execute_batch(&format!("PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version={SCHEMA_VERSION};
                    CREATE TABLE artifacts(sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64),
                    length INTEGER NOT NULL CHECK(length>0 AND length<={MAX_ARTIFACT_BYTES}),
                    bytes BLOB NOT NULL CHECK(length(bytes)=length)) STRICT;
                    CREATE TRIGGER artifacts_no_update BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT,'immutable artifact'); END;
                    CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT,'immutable artifact'); END;"))
                    .map_err(|_| DataError::Corrupt)?;
            } else {
                validate_database(&transaction)?;
            }
            transaction.commit().map_err(|_| DataError::Unavailable)?;
        }
        connection
            .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")
            .map_err(|_| DataError::Unavailable)?;
        Ok(Self {
            connection,
            writable: true,
        })
    }

    pub fn open_readonly(root: &Path) -> Result<Self, DataError> {
        safe_path(root)?;
        let connection = Connection::open_with_flags(
            root.join(DATABASE),
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|_| DataError::Unavailable)?;
        connection
            .busy_timeout(Duration::from_secs(5))
            .map_err(|_| DataError::Unavailable)?;
        validate_database(&connection)?;
        connection
            .execute_batch("PRAGMA query_only=ON;")
            .map_err(|_| DataError::Unavailable)?;
        Ok(Self {
            connection,
            writable: false,
        })
    }

    pub fn put(&mut self, value: &Value) -> Result<ArtifactReference, DataError> {
        if !self.writable {
            return Err(DataError::NotOwned);
        }
        if !value.is_object() {
            return Err(DataError::Invalid);
        }
        let bytes = serde_json::to_vec(&sanitize(value)).map_err(|_| DataError::Invalid)?;
        if bytes.len() as u64 > MAX_ARTIFACT_BYTES {
            return Err(DataError::Invalid);
        }
        let reference = ArtifactReference {
            sha256: format!("{:x}", Sha256::digest(&bytes)),
            length: bytes.len() as u64,
        };
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| DataError::Unavailable)?;
        let existing: Option<i32> = transaction
            .query_row(
                "SELECT 1 FROM artifacts WHERE sha256=?1",
                [&reference.sha256],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| DataError::Unavailable)?;
        if existing.is_some() {
            read_body(&transaction, &reference)?;
        } else {
            transaction
                .execute(
                    "INSERT INTO artifacts(sha256,length,bytes) VALUES(?1,?2,?3)",
                    rusqlite::params![reference.sha256, reference.length as i64, bytes],
                )
                .map_err(|_| DataError::Unavailable)?;
        }
        transaction.commit().map_err(|_| DataError::Unavailable)?;
        Ok(reference)
    }

    pub fn get(&self, reference: &ArtifactReference) -> Result<Value, DataError> {
        read_body(&self.connection, reference)
    }
}

pub fn sanitize(value: &Value) -> Value {
    match value {
        Value::Object(object) => Value::Object(
            object
                .iter()
                .filter(|(key, _)| !matches!(key.as_str(), "apiKey" | "tavilyApiKey"))
                .map(|(key, value)| (key.clone(), sanitize(value)))
                .collect(),
        ),
        Value::Array(array) => Value::Array(array.iter().map(sanitize).collect()),
        _ => value.clone(),
    }
}

#[derive(Default)]
pub struct ContentProjection {
    outputs: Map<String, Value>,
    final_answer: String,
    diagnostics: Map<String, Value>,
}

fn text(value: Option<&Value>) -> String {
    value
        .filter(|value| python_truthy(value))
        .map(crate::python_json::value_str)
        .unwrap_or_default()
}

impl ContentProjection {
    /// Replays content only. The supplied plan comes from Go's projection;
    /// this view must never authorize dispatch, retries, or cancellation.
    pub fn apply(&mut self, event: &Value, plan: &Value) {
        match event
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
        {
            "final_reset" if event.get("scope").and_then(Value::as_str) == Some("final_answer") => {
                self.final_answer.clear()
            }
            "content" => self.final_answer.push_str(&text(event.get("text"))),
            "done" => {
                if let Some(diagnostics) = event.get("diagnostics").and_then(Value::as_object) {
                    self.diagnostics = diagnostics.clone();
                }
            }
            "error" => {
                self.diagnostics
                    .insert("error".into(), json!(text(event.get("error"))));
            }
            "agent_reset" => {
                self.outputs.remove(&text(event.get("phase")));
            }
            "agent_output" => {
                let output = event.get("output");
                let mut phase = text(event.get("phase"));
                if phase.is_empty() {
                    phase = text(output.and_then(|value| value.get("id")));
                }
                if !phase.is_empty() && output.is_some_and(Value::is_object) {
                    self.outputs.insert(phase, sanitize(output.unwrap()));
                }
            }
            "agent" | "agent_delta" | "agent_reasoning" | "agent_note" => {
                self.update_output(event, plan)
            }
            _ => {}
        }
    }

    fn update_output(&mut self, event: &Value, plan: &Value) {
        let phase = text(event.get("phase"));
        if phase.is_empty() || phase == "leader" {
            return;
        }
        let name = text(event.get("name"));
        let default_name = crate::multi_agent::profile(&phase)
            .map(|profile| profile.0)
            .unwrap_or(&phase);
        let task = plan
            .as_array()
            .into_iter()
            .flatten()
            .find(|node| node.get("id").and_then(Value::as_str) == Some(phase.as_str()))
            .map(|node| text(node.get("task")))
            .unwrap_or_default();
        let output = self.outputs.entry(phase.clone()).or_insert_with(|| {
            json!({
            "id":phase,"name":if name.is_empty() { default_name } else { &name },"task":task,
            "content":"","summary":"","evidence":"","risks":"","full_output":""})
        });
        match event
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
        {
            "agent" => {
                for (key, fallback) in [("name", phase.as_str()), ("status", ""), ("text", "")] {
                    let incoming = text(event.get(key));
                    let old = text(output.get(key));
                    output[key] = json!(if !incoming.is_empty() {
                        incoming
                    } else if !old.is_empty() {
                        old
                    } else {
                        fallback.into()
                    });
                }
                if let Some(duration) = event.get("durationMs") {
                    output["duration_ms"] = duration.clone();
                }
            }
            "agent_delta" => {
                let content = text(output.get("content")) + &text(event.get("text"));
                output["content"] = json!(content);
                for (key, value) in crate::multi_agent::structured_output(&content)
                    .as_object()
                    .unwrap()
                {
                    output[key] = value.clone();
                }
            }
            "agent_reasoning" => {
                output["reasoning"] =
                    json!(text(output.get("reasoning")) + &text(event.get("text")));
            }
            "agent_note" => {
                let mut notes = output
                    .get("notes")
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                notes.push(json!(text(event.get("text"))));
                if notes.len() > 20 {
                    notes.drain(..notes.len() - 20);
                }
                output["notes"] = json!(notes);
            }
            _ => {}
        }
    }

    pub fn public_value(&self) -> Value {
        json!({"agentOutputs": self.outputs, "finalAnswer": self.final_answer, "diagnostics": self.diagnostics})
    }
}

#[cfg(test)]
#[path = "agent_run_data_tests.rs"]
mod tests;
