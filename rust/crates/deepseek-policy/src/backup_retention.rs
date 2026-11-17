//! Scheduled backup retention governance engine (`backup_retention.py`).
//!
//! Grandfather-father-son bucketing in policy IANA timezone, protection of
//! latest/pinned/minimum-healthy/active-run/restore-referenced/incremental-ancestor
//! chains, stale-preview snapshot validation, journaled trash transitions,
//! and grace-period restoration or retirement.

use std::collections::{BTreeMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

pub use chrono;
use chrono::{DateTime, Datelike, Duration as ChronoDuration, FixedOffset, NaiveDate, Utc};
use serde_json::{Map, Value, json};

use crate::app_error::{AppError, codes};
use crate::backup_mirror::{dumps_indent_two, fsync_dir, now_iso, parse_iso, sha256_hex};
use crate::core_utils::python_truthy;
use crate::entropy::{Entropy, SystemEntropy};
use crate::python_json::{dumps_compact, value_str};
use crate::tzif::IanaTimeZone;

pub const RETENTION_SCHEMA_VERSION: i64 = 1;
pub const DEFAULT_RETENTION_POLICY_ID: &str = "default";
pub const TRASH_JOURNAL_NAME: &str = "trash.journal.json";

const BOUNDS: [(&str, i64, i64, i64); 7] = [
    ("keepLast", 3, 0, 1000),
    ("keepHourly", 24, 0, 24 * 366),
    ("keepDaily", 14, 0, 3660),
    ("keepWeekly", 8, 0, 520),
    ("keepMonthly", 12, 0, 240),
    ("trashGraceHours", 24, 1, 24 * 90),
    ("minimumHealthyCopies", 2, 1, 100),
];

fn invalid_payload(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    }
}

fn invalid_request_conflict(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_REQUEST,
        status: 409,
    }
}

pub fn normalize_retention_policy(
    payload: &Value,
    retention_policy_id: &str,
) -> Result<Value, AppError> {
    let Some(obj) = payload.as_object() else {
        return Err(invalid_payload("Retention policy must be an object"));
    };
    let mut normalized = Map::new();
    normalized.insert("schemaVersion".to_string(), json!(RETENTION_SCHEMA_VERSION));
    normalized.insert("retentionPolicyId".to_string(), json!(retention_policy_id));

    for (key, default, minimum, maximum) in BOUNDS {
        let val = match obj.get(key) {
            Some(v) => v
                .as_i64()
                .filter(|n| *n >= minimum && *n <= maximum)
                .ok_or_else(|| {
                    invalid_payload(format!(
                        "Retention field {key} must be between {minimum} and {maximum}"
                    ))
                })?,
            None => default,
        };
        normalized.insert(key.to_string(), json!(val));
    }

    for key in ["maxAgeDays", "maxTotalBytes"] {
        match obj.get(key) {
            None | Some(Value::Null) => {
                normalized.insert(key.to_string(), Value::Null);
            }
            Some(v) => {
                let n = v.as_i64().filter(|n| *n > 0).ok_or_else(|| {
                    invalid_payload(format!(
                        "Retention field {key} must be a positive integer or null"
                    ))
                })?;
                normalized.insert(key.to_string(), json!(n));
            }
        }
    }

    let protect_pinned = match obj.get("protectPinned") {
        None => true,
        Some(v) => python_truthy(v),
    };
    normalized.insert("protectPinned".to_string(), json!(protect_pinned));

    Ok(Value::Object(normalized))
}

pub fn get_retention_policy(
    retention_dir: &Path,
    retention_policy_id: &str,
) -> Result<Value, AppError> {
    let effective = if retention_policy_id.is_empty() {
        DEFAULT_RETENTION_POLICY_ID
    } else {
        retention_policy_id
    };
    let path = retention_dir.join(format!("{effective}.json"));
    if !path.is_file() {
        if effective == DEFAULT_RETENTION_POLICY_ID {
            return normalize_retention_policy(&json!({}), DEFAULT_RETENTION_POLICY_ID);
        }
        return Err(AppError {
            message: "Retention policy not found".to_string(),
            code: codes::NOT_FOUND,
            status: 404,
        });
    }
    let unreadable = || AppError {
        message: "Retention policy store is unreadable".to_string(),
        code: codes::INTERNAL,
        status: 500,
    };
    let text = fs::read_to_string(&path).map_err(|_| unreadable())?;
    let data: Value = serde_json::from_str(&text).map_err(|_| unreadable())?;
    let id = data
        .get("retentionPolicyId")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_else(|| effective.to_string());
    normalize_retention_policy(&data, &id)
}

pub fn policy_digest(retention: &Value) -> String {
    let mut body = Map::new();
    if let Some(obj) = retention.as_object() {
        for (k, v) in obj {
            if k != "retentionPolicyId" {
                body.insert(k.clone(), v.clone());
            }
        }
    }
    sha256_hex(dumps_compact(&Value::Object(body)).as_bytes())
}

pub fn parse_utc_opt(value: Option<&Value>) -> Option<DateTime<Utc>> {
    let raw = value?;
    if !python_truthy(raw) {
        return None;
    }
    let text = value_str(raw);
    parse_iso(&text, "timestamp").ok()
}

pub struct BucketSet {
    pub hourly: String,
    pub daily: String,
    pub weekly: String,
    pub monthly: String,
}

fn format_bucket_naive(naive: chrono::NaiveDateTime) -> BucketSet {
    let iso = naive.iso_week();
    BucketSet {
        hourly: naive.format("%Y-%m-%dT%H").to_string(),
        daily: naive.format("%Y-%m-%d").to_string(),
        weekly: format!("{}-W{:02}", iso.year(), iso.week()),
        monthly: naive.format("%Y-%m").to_string(),
    }
}

pub fn bucket_keys(local: DateTime<FixedOffset>) -> BucketSet {
    format_bucket_naive(local.naive_local())
}

pub fn ordered_buckets(
    now_local: DateTime<FixedOffset>,
    kind: &str,
    count: usize,
) -> HashSet<String> {
    let mut out = HashSet::with_capacity(count);
    let mut current = now_local.naive_local();
    for _ in 0..count {
        let keys = format_bucket_naive(current);
        let key = match kind {
            "hourly" => keys.hourly,
            "daily" => keys.daily,
            "weekly" => keys.weekly,
            _ => keys.monthly,
        };
        out.insert(key);
        match kind {
            "hourly" => current -= ChronoDuration::hours(1),
            "daily" => current -= ChronoDuration::days(1),
            "weekly" => current -= ChronoDuration::weeks(1),
            _ => {
                let m = current.month();
                let (year, month) = if m == 1 {
                    (current.year() - 1, 12)
                } else {
                    (current.year(), m - 1)
                };
                if let Some(d) = NaiveDate::from_ymd_opt(year, month, 1) {
                    current = d.and_time(current.time());
                }
            }
        }
    }
    out
}

pub fn collect_restore_references(restore_dir: &Path) -> HashSet<String> {
    let mut refs = HashSet::new();
    if !restore_dir.is_dir() {
        return refs;
    }
    let Ok(entries) = fs::read_dir(restore_dir) else {
        return refs;
    };
    let mut subdirs: Vec<PathBuf> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    subdirs.sort();

    fn scan(value: &Value, refs: &mut HashSet<String>) {
        match value {
            Value::Object(map) => {
                for (key, item) in map {
                    if matches!(
                        key.as_str(),
                        "filename"
                            | "ciphertextSha256"
                            | "archiveSha256"
                            | "safetyBackupId"
                            | "backupId"
                            | "sourceBackupId"
                    ) {
                        if let Some(text) = item.as_str() {
                            if !text.is_empty() {
                                refs.insert(text.to_string());
                                continue;
                            }
                        }
                    }
                    scan(item, refs);
                }
            }
            Value::Array(items) => {
                for item in items {
                    scan(item, refs);
                }
            }
            _ => {}
        }
    }

    for metadata in subdirs {
        for name in [
            "upload.json",
            "plan.json",
            "transaction.json",
            "metadata.json",
        ] {
            let path = metadata.join(name);
            if !path.is_file() {
                continue;
            }
            match fs::read_to_string(&path)
                .ok()
                .and_then(|txt| serde_json::from_str::<Value>(&txt).ok())
            {
                Some(parsed) => scan(&parsed, &mut refs),
                None => {
                    if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
                        refs.insert(stem.to_string());
                    }
                }
            }
        }
    }
    refs
}

pub fn protect_snapshot_ancestors(
    records: &[Value],
    keep: &mut HashSet<String>,
    protected: &mut BTreeMap<String, String>,
    descendants: Option<&HashSet<String>>,
) -> BTreeMap<String, Vec<String>> {
    let mut by_id = BTreeMap::<String, &Value>::new();
    for rec in records {
        let id = rec.get("backupId").map(value_str).unwrap_or_default();
        by_id.insert(id, rec);
    }
    let mut required_by = BTreeMap::<String, Vec<String>>::new();
    let considered: Vec<String> = match descendants {
        Some(set) => set.iter().cloned().collect(),
        None => keep.iter().cloned().collect(),
    };
    for backup_id in considered {
        let kind = by_id
            .get(&backup_id)
            .and_then(|rec| rec.get("snapshotKind"))
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_else(|| "full".to_string());
        if kind != "incremental" {
            continue;
        }
        let mut chain = Vec::new();
        let mut seen = HashSet::new();
        let mut current = backup_id.clone();
        while !current.is_empty() {
            if !seen.insert(current.clone()) {
                break;
            }
            let Some(record) = by_id.get(&current) else {
                break;
            };
            chain.push(current.clone());
            let Some(parent) = record.get("parentBackupId").filter(|v| python_truthy(v)) else {
                break;
            };
            current = value_str(parent);
        }
        for ancestor in chain.iter().skip(1) {
            required_by
                .entry(ancestor.clone())
                .or_default()
                .push(backup_id.clone());
            keep.insert(ancestor.clone());
            protected
                .entry(ancestor.clone())
                .or_insert_with(|| "ancestor-of-kept-snapshot".to_string());
        }
    }
    required_by
}

pub fn grace_expired(record: &Value, current: DateTime<Utc>, grace: ChronoDuration) -> bool {
    match parse_utc_opt(record.get("trashedAt")) {
        Some(trashed_at) => current.signed_duration_since(trashed_at) >= grace,
        None => false,
    }
}

pub struct PreviewInput<'a> {
    pub retention: &'a Value,
    pub all_records: &'a [Value],
    pub healthy_ids: &'a [String],
    pub active_run_ids: &'a HashSet<String>,
    pub restore_references: &'a HashSet<String>,
    pub target_id_display: &'a str,
    pub target_generation: i64,
    pub catalog_head_hash: &'a str,
    pub policy_timezone: &'a str,
    pub tz: &'a IanaTimeZone,
    pub now: DateTime<Utc>,
}

fn new_retention_run_id() -> String {
    let hex16 = SystemEntropy
        .new_id()
        .unwrap_or_else(|_| "000000000000".to_string());
    let short: String = hex16.chars().take(12).collect();
    format!("rr_{short}")
}

fn backup_id_of(record: &Value) -> String {
    record.get("backupId").map(value_str).unwrap_or_default()
}

pub fn compute_preview(input: PreviewInput<'_>) -> Value {
    let current = input.now;
    let now_local = input.tz.to_local(current);
    let grace_hours = input
        .retention
        .get("trashGraceHours")
        .and_then(Value::as_i64)
        .filter(|v| *v > 0)
        .unwrap_or(24);
    let grace = ChronoDuration::hours(grace_hours);

    let not_deleted: Vec<Value> = input
        .all_records
        .iter()
        .filter(|rec| !python_truthy(rec.get("deleted").unwrap_or(&Value::Null)))
        .cloned()
        .collect();
    let recoverable: Vec<&Value> = not_deleted
        .iter()
        .filter(|rec| !grace_expired(rec, current, grace))
        .collect();
    let mut records: Vec<Value> = not_deleted
        .iter()
        .filter(|rec| !python_truthy(rec.get("trashed").unwrap_or(&Value::Null)))
        .cloned()
        .collect();
    records.sort_by(|a, b| {
        let ka = a
            .get("createdAt")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let kb = b
            .get("createdAt")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        kb.cmp(&ka)
    });

    let mut keep = HashSet::<String>::new();
    let mut protected = BTreeMap::<String, String>::new();

    if let Some(latest) = records.first() {
        let id = backup_id_of(latest);
        protected.insert(id.clone(), "latest-successful-backup".to_string());
        keep.insert(id);
    }

    let keep_last = input
        .retention
        .get("keepLast")
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    for rec in records.iter().take(keep_last) {
        keep.insert(backup_id_of(rec));
    }

    for (kind, field) in [
        ("hourly", "keepHourly"),
        ("daily", "keepDaily"),
        ("weekly", "keepWeekly"),
        ("monthly", "keepMonthly"),
    ] {
        let limit = input
            .retention
            .get(field)
            .and_then(Value::as_u64)
            .unwrap_or(0) as usize;
        if limit == 0 {
            continue;
        }
        let wanted = ordered_buckets(now_local, kind, limit);
        for rec in &records {
            let Some(created) = parse_utc_opt(rec.get("createdAt")) else {
                continue;
            };
            let keys = bucket_keys(input.tz.to_local(created));
            let matched = match kind {
                "hourly" => wanted.contains(&keys.hourly),
                "daily" => wanted.contains(&keys.daily),
                "weekly" => wanted.contains(&keys.weekly),
                _ => wanted.contains(&keys.monthly),
            };
            if matched {
                keep.insert(backup_id_of(rec));
            }
        }
    }

    if let Some(max_age) = input
        .retention
        .get("maxAgeDays")
        .and_then(Value::as_i64)
        .filter(|v| *v > 0)
    {
        let cutoff = current - ChronoDuration::days(max_age);
        for rec in &records {
            if let Some(created) = parse_utc_opt(rec.get("createdAt")) {
                if created >= cutoff {
                    keep.insert(backup_id_of(rec));
                }
            }
        }
    }

    let min_healthy = input
        .retention
        .get("minimumHealthyCopies")
        .and_then(Value::as_u64)
        .unwrap_or(1) as usize;
    for id in input.healthy_ids.iter().take(min_healthy) {
        protected
            .entry(id.clone())
            .or_insert_with(|| "minimum-healthy-copies".to_string());
        keep.insert(id.clone());
    }

    let protect_pinned = input
        .retention
        .get("protectPinned")
        .map(python_truthy)
        .unwrap_or(true);
    for rec in &records {
        let id = backup_id_of(rec);
        let filename = rec
            .get("filename")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();
        let sha = rec
            .get("ciphertextSha256")
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or_default();

        if protect_pinned && python_truthy(rec.get("pinned").unwrap_or(&Value::Null)) {
            protected
                .entry(id.clone())
                .or_insert_with(|| "pinned".to_string());
            keep.insert(id.clone());
        }
        if input.active_run_ids.contains(&id) {
            protected
                .entry(id.clone())
                .or_insert_with(|| "active-run".to_string());
            keep.insert(id.clone());
        }
        if input.restore_references.contains(&id)
            || (!filename.is_empty() && input.restore_references.contains(&filename))
            || (!sha.is_empty() && input.restore_references.contains(&sha))
        {
            protected
                .entry(id.clone())
                .or_insert_with(|| "restore-referenced".to_string());
            keep.insert(id.clone());
        }
    }

    let mut descendants = keep.clone();
    for rec in recoverable {
        if python_truthy(rec.get("trashed").unwrap_or(&Value::Null)) {
            let kind = rec
                .get("snapshotKind")
                .filter(|v| python_truthy(v))
                .map(value_str)
                .unwrap_or_else(|| "full".to_string());
            if kind == "incremental" {
                descendants.insert(backup_id_of(rec));
            }
        }
    }
    let _ = protect_snapshot_ancestors(&not_deleted, &mut keep, &mut protected, Some(&descendants));

    if let Some(budget) = input
        .retention
        .get("maxTotalBytes")
        .and_then(Value::as_i64)
        .filter(|v| *v > 0)
    {
        let mut total: i64 = records
            .iter()
            .filter(|rec| keep.contains(&backup_id_of(rec)))
            .map(|rec| rec.get("size").and_then(Value::as_i64).unwrap_or(0))
            .sum();
        for rec in &records {
            let id = backup_id_of(rec);
            if keep.contains(&id) && !protected.contains_key(&id) && total > budget {
                keep.remove(&id);
                total -= rec.get("size").and_then(Value::as_i64).unwrap_or(0);
            }
        }
    }

    let mut trash_records = Vec::new();
    let mut trash_ids = Vec::new();
    for rec in &records {
        let id = backup_id_of(rec);
        if !keep.contains(&id) && !protected.contains_key(&id) {
            trash_ids.push(id);
            trash_records.push(rec.clone());
        }
    }

    let mut keep_sorted: Vec<String> = keep.into_iter().collect();
    keep_sorted.sort();
    let protected_list: Vec<Value> = protected
        .into_iter()
        .map(|(k, reason)| json!({"backupId": k, "reason": reason}))
        .collect();

    json!({
        "retentionRunId": new_retention_run_id(),
        "targetId": input.target_id_display,
        "targetGeneration": input.target_generation,
        "catalogHeadHash": input.catalog_head_hash,
        "policyDigest": policy_digest(input.retention),
        "policyTimezone": input.policy_timezone,
        "evaluatedAt": now_iso(Some(current)),
        "keep": keep_sorted,
        "trash": trash_ids,
        "trashRecords": trash_records,
        "protected": protected_list,
    })
}

pub fn validate_preview_snapshot(
    preview: &Value,
    retention: &Value,
    target_generation: i64,
    catalog_head_hash: &str,
) -> Result<(), AppError> {
    let head = preview
        .get("catalogHeadHash")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if head != catalog_head_hash {
        return Err(invalid_request_conflict(
            "retention-stale-snapshot: catalog head changed since preview",
        ));
    }
    let preview_gen = preview
        .get("targetGeneration")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if preview_gen != target_generation {
        return Err(invalid_request_conflict(
            "retention-stale-snapshot: target generation changed since preview",
        ));
    }
    let digest = preview
        .get("policyDigest")
        .filter(|v| python_truthy(v))
        .map(value_str)
        .unwrap_or_default();
    if digest != policy_digest(retention) {
        return Err(invalid_request_conflict(
            "retention-stale-snapshot: retention policy changed since preview",
        ));
    }
    Ok(())
}

pub fn trash_journal_write(destination: &Path, journal: &Value) -> Result<(), AppError> {
    let internal = || AppError {
        message: "Server error".to_string(),
        code: codes::INTERNAL,
        status: 500,
    };
    fs::create_dir_all(destination).map_err(|_| internal())?;
    let path = destination.join(TRASH_JOURNAL_NAME);
    let tmp = destination.join(format!(".{TRASH_JOURNAL_NAME}.{}.tmp", std::process::id()));
    let content = format!("{}\n", dumps_indent_two(journal));
    {
        let mut handle = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&tmp)
            .map_err(|_| internal())?;
        handle
            .write_all(content.as_bytes())
            .map_err(|_| internal())?;
        handle.flush().map_err(|_| internal())?;
        handle.sync_all().map_err(|_| internal())?;
    }
    fs::rename(&tmp, &path).map_err(|_| internal())?;
    fsync_dir(destination);
    Ok(())
}

fn is_hex_digest_64(stem: &str) -> bool {
    stem.len() == 64 && stem.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

pub fn restore_trash_entry(target_root: &Path, backup_id: &str) -> Result<(), AppError> {
    let internal = || AppError {
        message: "Server error".to_string(),
        code: codes::INTERNAL,
        status: 500,
    };
    let entry = target_root.join(".trash").join(backup_id);
    if !entry.is_dir() {
        return Ok(());
    }
    let dir_entries = fs::read_dir(&entry).map_err(|_| internal())?;
    for dir_item in dir_entries.flatten() {
        let path = dir_item.path();
        let name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or_default()
            .to_string();
        if name == TRASH_JOURNAL_NAME {
            continue;
        }
        let stem = name.strip_suffix(".age").unwrap_or("");
        if is_hex_digest_64(stem) {
            let dest = target_root
                .join("objects")
                .join("sha256")
                .join(&stem[..2])
                .join(format!("{stem}.age"));
            if let Some(parent) = dest.parent() {
                fs::create_dir_all(parent).map_err(|_| internal())?;
            }
            if dest.exists() {
                let _ = fs::remove_file(&path);
            } else {
                fs::rename(&path, &dest).map_err(|_| internal())?;
            }
        } else if name.ends_with(".json") {
            let receipts = target_root.join("receipts");
            fs::create_dir_all(&receipts).map_err(|_| internal())?;
            fs::rename(&path, receipts.join(&name)).map_err(|_| internal())?;
        } else {
            let backups = target_root.join("backups");
            fs::create_dir_all(&backups).map_err(|_| internal())?;
            fs::rename(&path, backups.join(&name)).map_err(|_| internal())?;
        }
    }
    let _ = fs::remove_file(entry.join(TRASH_JOURNAL_NAME));
    let _ = fs::remove_dir(&entry);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tzif::load_iana_timezone;
    use chrono::TimeZone;
    use tempfile::tempdir;

    #[test]
    fn normalizes_and_validates_retention_policies() {
        let dir = tempdir().unwrap();
        let def = get_retention_policy(dir.path(), "default").unwrap();
        assert_eq!(def["retentionPolicyId"], "default");
        assert_eq!(def["keepLast"], 3);
        assert_eq!(def["protectPinned"], true);

        let err = get_retention_policy(dir.path(), "missing_id").unwrap_err();
        assert_eq!(err.status, 404);

        let bad = normalize_retention_policy(&json!({"keepLast": -1}), "x").unwrap_err();
        assert_eq!(bad.status, 400);
        let bad_bool = normalize_retention_policy(&json!({"maxAgeDays": true}), "x").unwrap_err();
        assert_eq!(bad_bool.status, 400);
    }

    #[test]
    fn preview_protects_pinned_incremental_ancestors_and_evicts_over_budget() {
        let tz = load_iana_timezone("UTC").unwrap();
        let now = Utc.with_ymd_and_hms(2026, 11, 17, 12, 0, 0).unwrap();
        let ret = normalize_retention_policy(
            &json!({
                "keepLast": 1,
                "keepHourly": 0,
                "keepDaily": 0,
                "keepWeekly": 0,
                "keepMonthly": 0,
                "minimumHealthyCopies": 1,
                "maxTotalBytes": 150
            }),
            "default",
        )
        .unwrap();

        let records = vec![
            json!({
                "backupId": "bk-base",
                "filename": "base.age",
                "createdAt": "2026-10-01T00:00:00Z",
                "size": 100,
                "snapshotKind": "full",
                "scrubOk": true
            }),
            json!({
                "backupId": "bk-inc",
                "filename": "inc.age",
                "createdAt": "2026-10-02T00:00:00Z",
                "size": 50,
                "snapshotKind": "incremental",
                "parentBackupId": "bk-base",
                "pinned": true
            }),
            json!({
                "backupId": "bk-old",
                "filename": "old.age",
                "createdAt": "2026-10-03T00:00:00Z",
                "size": 100
            }),
            json!({
                "backupId": "bk-latest",
                "filename": "latest.age",
                "createdAt": "2026-11-17T11:00:00Z",
                "size": 100,
                "scrubOk": true
            }),
        ];

        let preview = compute_preview(PreviewInput {
            retention: &ret,
            all_records: &records,
            healthy_ids: &["bk-latest".to_string()],
            active_run_ids: &HashSet::new(),
            restore_references: &HashSet::new(),
            target_id_display: "/tmp/root",
            target_generation: 2,
            catalog_head_hash: "abc",
            policy_timezone: "UTC",
            tz: &tz,
            now,
        });

        let keep: Vec<String> = preview["keep"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        let trash: Vec<String> = preview["trash"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        assert_eq!(keep, vec!["bk-base", "bk-inc", "bk-latest"]);
        assert_eq!(trash, vec!["bk-old"]);
    }
}
