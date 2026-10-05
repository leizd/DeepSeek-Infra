use serde_json::Value;

use crate::model::{TaskRecord, digest};

#[derive(Debug, Clone)]
pub struct JsonlLimits {
    pub max_total_bytes: usize,
    pub max_line_bytes: usize,
    pub max_tasks: usize,
    pub max_logs_per_task_bytes: usize,
    pub max_json_depth: usize,
}

impl Default for JsonlLimits {
    fn default() -> Self {
        Self {
            max_total_bytes: 5_000_000_000,
            max_line_bytes: 1024 * 1024,
            max_tasks: 100_000,
            max_logs_per_task_bytes: 262_144,
            max_json_depth: 64,
        }
    }
}

#[derive(Debug, Clone)]
pub enum SnapshotEntry {
    Metadata {
        state_generation: i64,
        restore_epoch: String,
    },
    Task(Box<TaskRecord>),
    Idempotency {
        hash: String,
        task_id: String,
        request_hash: String,
    },
    Log {
        task_id: String,
        stdout: String,
        stderr: String,
    },
    Complete {
        state_generation: i64,
    },
}

pub struct ParsedSnapshot {
    pub entries: Vec<SnapshotEntry>,
    pub digest_hex: String,
}

pub fn parse_snapshot(source: &str) -> Result<ParsedSnapshot, String> {
    parse_snapshot_with_limits(source, &JsonlLimits::default())
}

pub fn parse_snapshot_with_limits(
    source: &str,
    limits: &JsonlLimits,
) -> Result<ParsedSnapshot, String> {
    let digest_hex = digest(source);
    let mut bytes = 0usize;
    let mut saw_metadata: Option<(i64, String)> = None;
    let mut saw_complete = false;
    let mut tasks = 0usize;
    let mut task_ids = std::collections::BTreeSet::new();
    let mut log_bytes = std::collections::BTreeMap::<String, usize>::new();
    let mut entries = Vec::new();

    if !source.is_empty() && !source.ends_with('\n') {
        return Err("snapshot ends with a partial line".to_string());
    }
    for raw_line in source.split_inclusive('\n') {
        if saw_complete && !raw_line.is_empty() {
            return Err("snapshot has entries after the complete record".to_string());
        }
        let mut line = raw_line.strip_suffix('\n').unwrap_or(raw_line);
        if let Some(stripped) = line.strip_suffix('\r') {
            line = stripped;
        }
        if line.is_empty() {
            continue;
        }
        let line_bytes = line.len() + 1;
        if line.len() > limits.max_line_bytes {
            return Err("snapshot line exceeds maximum size".to_string());
        }
        bytes += line_bytes;
        if bytes > limits.max_total_bytes {
            return Err("snapshot exceeds maximum total size".to_string());
        }
        let parsed: Value = serde_json::from_str(line)
            .map_err(|_| "snapshot line is not valid JSON".to_string())?;
        assert_depth(&parsed, 1, limits.max_json_depth)?;
        let object = parsed
            .as_object()
            .ok_or_else(|| "snapshot entry must be a JSON object".to_string())?;
        if object.get("schemaVersion").and_then(Value::as_i64) != Some(1) {
            return Err("unsupported stateless MCP backup schema".to_string());
        }
        if saw_complete {
            return Err("snapshot has entries after the complete record".to_string());
        }
        let kind = object.get("type").and_then(Value::as_str).unwrap_or("");
        match kind {
            "metadata" => {
                if saw_metadata.is_some() || !task_ids.is_empty() {
                    return Err("snapshot metadata must be the first entry".to_string());
                }
                let record = object
                    .get("record")
                    .and_then(Value::as_object)
                    .unwrap_or(object);
                let metadata = (
                    required_generation(record.get("stateGeneration"), "stateGeneration")?,
                    required_string(record.get("restoreEpoch"), "restoreEpoch")?,
                );
                saw_metadata = Some(metadata.clone());
                entries.push(SnapshotEntry::Metadata {
                    state_generation: metadata.0,
                    restore_epoch: metadata.1,
                });
            }
            "task" => {
                if saw_metadata.is_none() {
                    return Err("snapshot metadata must be the first entry".to_string());
                }
                let task_value = object
                    .get("task")
                    .ok_or_else(|| "snapshot task entry is invalid".to_string())?;
                let task: TaskRecord = serde_json::from_value(task_value.clone())
                    .map_err(|_| "snapshot task entry is invalid".to_string())?;
                if task.id.is_empty() {
                    return Err("snapshot entry is missing task.id".to_string());
                }
                if !task_ids.insert(task.id.clone()) {
                    return Err("snapshot contains a duplicate task id".to_string());
                }
                tasks += 1;
                if tasks > limits.max_tasks {
                    return Err("snapshot exceeds maximum task count".to_string());
                }
                entries.push(SnapshotEntry::Task(Box::new(task)));
            }
            "idempotency" => {
                if saw_metadata.is_none() {
                    return Err("snapshot metadata must be the first entry".to_string());
                }
                let record = object
                    .get("record")
                    .and_then(Value::as_object)
                    .ok_or_else(|| "snapshot idempotency entry is invalid".to_string())?;
                let parsed_record = SnapshotEntry::Idempotency {
                    hash: required_string(record.get("hash"), "idempotency.hash")?,
                    task_id: required_string(record.get("taskId"), "idempotency.taskId")?,
                    request_hash: required_string(
                        record.get("requestHash"),
                        "idempotency.requestHash",
                    )?,
                };
                let SnapshotEntry::Idempotency { task_id, .. } = &parsed_record else {
                    unreachable!()
                };
                if !task_ids.contains(task_id) {
                    return Err(
                        "snapshot idempotency record references an unknown task".to_string()
                    );
                }
                entries.push(parsed_record);
            }
            "log" => {
                if saw_metadata.is_none() {
                    return Err("snapshot metadata must be the first entry".to_string());
                }
                let record = object
                    .get("record")
                    .and_then(Value::as_object)
                    .ok_or_else(|| "snapshot log entry is invalid".to_string())?;
                let task_id = required_string(record.get("taskId"), "log.taskId")?;
                if !task_ids.contains(&task_id) {
                    return Err("snapshot log record references an unknown task".to_string());
                }
                let stdout = record
                    .get("stdout")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                let stderr = record
                    .get("stderr")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                let size =
                    log_bytes.get(&task_id).copied().unwrap_or(0) + stdout.len() + stderr.len();
                if size > limits.max_logs_per_task_bytes {
                    return Err("snapshot task logs exceed maximum size".to_string());
                }
                log_bytes.insert(task_id.clone(), size);
                entries.push(SnapshotEntry::Log {
                    task_id,
                    stdout,
                    stderr,
                });
            }
            "complete" => {
                let metadata = saw_metadata
                    .as_ref()
                    .ok_or_else(|| "snapshot metadata must be the first entry".to_string())?;
                let generation =
                    required_generation(object.get("stateGeneration"), "complete.stateGeneration")?;
                if metadata.0 != generation {
                    return Err("snapshot metadata and complete generations differ".to_string());
                }
                saw_complete = true;
                entries.push(SnapshotEntry::Complete {
                    state_generation: generation,
                });
            }
            _ => {
                if saw_metadata.is_none() {
                    return Err("snapshot metadata must be the first entry".to_string());
                }
                return Err("snapshot entry has an unknown type".to_string());
            }
        }
    }
    if saw_metadata.is_none() || !saw_complete {
        return Err("stateless MCP backup is incomplete".to_string());
    }
    Ok(ParsedSnapshot {
        entries,
        digest_hex,
    })
}

fn required_string(value: Option<&Value>, field: &str) -> Result<String, String> {
    match value.and_then(Value::as_str) {
        Some(text) if !text.is_empty() => Ok(text.to_string()),
        _ => Err(format!("snapshot entry is missing {field}")),
    }
}

fn required_generation(value: Option<&Value>, field: &str) -> Result<i64, String> {
    match value.and_then(Value::as_i64) {
        Some(number) if number >= 0 => Ok(number),
        _ => Err(format!("snapshot entry has invalid {field}")),
    }
}

fn assert_depth(value: &Value, depth: usize, max_depth: usize) -> Result<(), String> {
    if depth > max_depth {
        return Err("snapshot entry exceeds maximum JSON depth".to_string());
    }
    match value {
        Value::Array(items) => {
            for item in items {
                assert_depth(item, depth + 1, max_depth)?;
            }
        }
        Value::Object(map) => {
            for item in map.values() {
                assert_depth(item, depth + 1, max_depth)?;
            }
        }
        _ => {}
    }
    Ok(())
}
