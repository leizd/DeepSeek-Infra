//! Target-side filesystem writer leases (`.target-lock/writer.json`).
//!
//! Mirrors `deepseek_infra.infra.workspace.backup_writer_lease` for local
//! filesystem targets: exclusive `O_EXCL` (`create_new`) creation, atomic
//! rename probing (`probe_atomic_target`), bounded expiry/preemption, fencing
//! token monotonicity, and ownership assertion across visible mutations.
//!
//! Relationship to `deepseek_transfer::writer_lease`: `deepseek-transfer` keeps
//! the dependency-free pure JSON decision replay (`replay_writer_lease_case`)
//! used by S3/transfer worker tests so transfer crates need not depend on
//! `deepseek-policy`; this module provides the live filesystem `O_EXCL` I/O
//! protocol while verifying identical semantics against the frozen v25 vector
//! corpus in unit tests below.

use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration as StdDuration;

use chrono::{DateTime, Duration, Utc};
use serde_json::{Map, Value, json};

use crate::app_error::{AppError, codes};
use crate::backup_mirror::{dumps_indent_two, fsync_dir, now_iso, parse_iso};

/// Default lease lifetime in seconds, matching `TARGET_WRITER_LEASE_SECONDS`.
pub const TARGET_WRITER_LEASE_SECONDS: i64 = 300;

static UNIQUE_SEQ: AtomicU64 = AtomicU64::new(1);

fn unique_suffix() -> u64 {
    UNIQUE_SEQ.fetch_add(1, Ordering::Relaxed)
}

fn unsupported_atomic(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_REQUEST,
        status: 503,
    }
}

fn busy_error(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_REQUEST,
        status: 423,
    }
}

fn lost_error(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_REQUEST,
        status: 409,
    }
}

fn internal_error(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INTERNAL,
        status: 500,
    }
}

/// Retry transient `PermissionDenied` errors briefly on Windows file lock races.
fn retry_permission<T>(
    mut operation: impl FnMut() -> std::io::Result<T>,
    attempts: usize,
    sleep: StdDuration,
) -> std::io::Result<T> {
    let mut last_error: Option<std::io::Error> = None;
    for attempt in 0..attempts {
        match operation() {
            Ok(value) => return Ok(value),
            Err(err) if err.kind() == ErrorKind::PermissionDenied => {
                last_error = Some(err);
                if attempt + 1 < attempts {
                    std::thread::sleep(sleep);
                }
            }
            Err(err) => return Err(err),
        }
    }
    Err(last_error.expect("attempts > 0 sets last_error on PermissionDenied"))
}

fn remove_file_if_exists(path: &Path) -> std::io::Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == ErrorKind::NotFound => Ok(()),
        Err(err) => Err(err),
    }
}

struct CleanupPair(PathBuf, PathBuf);

impl Drop for CleanupPair {
    fn drop(&mut self) {
        let _ = remove_file_if_exists(&self.0);
        let _ = remove_file_if_exists(&self.1);
    }
}

fn is_payload_active(payload: Option<&Value>, now: DateTime<Utc>) -> bool {
    let Some(Value::Object(map)) = payload else {
        return true;
    };
    let Some(Value::String(expires_at)) = map.get("expiresAt") else {
        return true;
    };
    let Ok(expiry) = parse_iso(expires_at, "expiresAt") else {
        return true;
    };
    expiry > now
}

/// Return `true` for an unexpired or unreadable existing filesystem writer lease.
pub fn active_writer_lease(root: &Path, now: Option<DateTime<Utc>>) -> bool {
    let path = root.join(".target-lock").join("writer.json");
    if !path.is_file() {
        return false;
    }
    let Ok(raw) = fs::read_to_string(&path) else {
        return true;
    };
    let parsed = serde_json::from_str::<Value>(&raw).ok();
    is_payload_active(parsed.as_ref(), now.unwrap_or_else(Utc::now))
}

/// Verify `O_EXCL` (`create_new`) and atomic rename semantics under `<root>/.target-lock`.
pub fn probe_atomic_target(root: &Path) -> Result<(), AppError> {
    let lock_dir = root.join(".target-lock");
    fs::create_dir_all(&lock_dir)
        .map_err(|err| unsupported_atomic(format!("unsupported-atomic-target: {err}")))?;
    let probe = lock_dir.join(format!(
        ".probe-{}-{:08x}",
        std::process::id(),
        unique_suffix()
    ));
    let renamed = lock_dir.join(format!(
        "{}.renamed",
        probe
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or(".probe")
    ));
    let _cleanup = CleanupPair(probe.clone(), renamed.clone());

    let first = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&probe)
        .map_err(|err| unsupported_atomic(format!("unsupported-atomic-target: {err}")))?;
    drop(first);

    match OpenOptions::new().write(true).create_new(true).open(&probe) {
        Ok(_) => {
            return Err(unsupported_atomic(
                "unsupported-atomic-target: exclusive create is not honored",
            ));
        }
        Err(err) if err.kind() == ErrorKind::AlreadyExists => {}
        Err(err) => {
            return Err(unsupported_atomic(format!(
                "unsupported-atomic-target: {err}"
            )));
        }
    }

    fs::rename(&probe, &renamed)
        .map_err(|err| unsupported_atomic(format!("unsupported-atomic-target: {err}")))?;
    if probe.exists() || !renamed.is_file() {
        return Err(unsupported_atomic(
            "unsupported-atomic-target: atomic rename is not honored",
        ));
    }
    Ok(())
}

fn scalar_text(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        Some(other) => other.to_string(),
    }
}

fn parse_fencing_token(map: &Map<String, Value>, falsy_default: i64) -> Option<i64> {
    match map.get("fencingToken") {
        None | Some(Value::Null) | Some(Value::Bool(false)) => Some(falsy_default),
        Some(Value::Bool(true)) => Some(1),
        Some(Value::Number(number)) => {
            let parsed = number.as_i64().or_else(|| {
                number
                    .as_f64()
                    .filter(|float| float.is_finite())
                    .map(|float| float.trunc() as i64)
            })?;
            if parsed == 0 {
                Some(falsy_default)
            } else {
                Some(parsed)
            }
        }
        Some(Value::String(text)) => {
            let trimmed = text.trim();
            if trimmed.is_empty() {
                return Some(falsy_default);
            }
            let parsed = trimmed.parse::<i64>().ok()?;
            if parsed == 0 {
                Some(falsy_default)
            } else {
                Some(parsed)
            }
        }
        Some(_) => None,
    }
}

/// Single-writer lease on one filesystem backup target root.
///
/// Dropping a held lease releases `.target-lock/writer.json` only if the file
/// still names the same `ownerRunId` and `fencingToken`.
pub struct TargetWriterLease {
    root: PathBuf,
    target_id: String,
    owner_run_id: String,
    owner_instance_id: String,
    fencing_token: i64,
    lease_seconds: i64,
    clock: Box<dyn Fn() -> DateTime<Utc> + Send + Sync>,
    acquired: bool,
}

impl TargetWriterLease {
    pub fn new(
        root: impl Into<PathBuf>,
        target_id: impl Into<String>,
        owner_run_id: impl Into<String>,
        owner_instance_id: impl Into<String>,
        fencing_token: i64,
    ) -> Self {
        Self {
            root: root.into(),
            target_id: target_id.into(),
            owner_run_id: owner_run_id.into(),
            owner_instance_id: owner_instance_id.into(),
            fencing_token,
            lease_seconds: TARGET_WRITER_LEASE_SECONDS,
            clock: Box::new(Utc::now),
            acquired: false,
        }
    }

    pub fn with_lease_seconds(mut self, lease_seconds: i64) -> Self {
        self.lease_seconds = lease_seconds;
        self
    }

    pub fn with_clock<F>(mut self, clock: F) -> Self
    where
        F: Fn() -> DateTime<Utc> + Send + Sync + 'static,
    {
        self.clock = Box::new(clock);
        self
    }

    pub fn path(&self) -> PathBuf {
        self.root.join(".target-lock").join("writer.json")
    }

    pub fn fencing_token(&self) -> i64 {
        self.fencing_token
    }

    pub fn owner_run_id(&self) -> &str {
        &self.owner_run_id
    }

    pub fn owner_instance_id(&self) -> &str {
        &self.owner_instance_id
    }

    fn payload(&self, acquired_at: DateTime<Utc>) -> Value {
        json!({
            "schemaVersion": 1,
            "targetId": self.target_id,
            "ownerRunId": self.owner_run_id,
            "ownerInstanceId": self.owner_instance_id,
            "fencingToken": self.fencing_token,
            "acquiredAt": now_iso(Some(acquired_at)),
            "expiresAt": now_iso(Some(acquired_at + Duration::seconds(self.lease_seconds))),
        })
    }

    fn read_existing(&self) -> Option<Map<String, Value>> {
        let path = self.path();
        let raw = retry_permission(
            || fs::read_to_string(&path),
            10,
            StdDuration::from_millis(50),
        )
        .ok()?;
        let Value::Object(map) = serde_json::from_str::<Value>(&raw).ok()? else {
            return None;
        };
        Some(map)
    }

    fn write_replace(&self, payload: &Value) -> Result<(), AppError> {
        let path = self.path();
        let parent = path
            .parent()
            .ok_or_else(|| internal_error("writer lease has no parent directory"))?;
        let tmp = parent.join(format!(
            ".writer.json.{}.{}.tmp",
            std::process::id(),
            unique_suffix()
        ));
        let serialized = format!("{}\n", dumps_indent_two(payload));
        let write_result = (|| -> std::io::Result<()> {
            let mut handle = OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .open(&tmp)?;
            handle.write_all(serialized.as_bytes())?;
            handle.flush()?;
            handle.sync_all()?;
            Ok(())
        })();
        if let Err(err) = write_result {
            let _ = remove_file_if_exists(&tmp);
            return Err(internal_error(format!(
                "Failed to write target writer lease: {err}"
            )));
        }
        if let Err(err) =
            retry_permission(|| fs::rename(&tmp, &path), 10, StdDuration::from_millis(50))
        {
            let _ = remove_file_if_exists(&tmp);
            return Err(internal_error(format!(
                "Failed to replace target writer lease: {err}"
            )));
        }
        fsync_dir(parent);
        Ok(())
    }

    fn is_expired(payload: &Map<String, Value>, now: DateTime<Utc>) -> bool {
        let expires = scalar_text(payload.get("expiresAt"));
        expires < now_iso(Some(now))
    }

    fn same_run_takeover_allowed(&self, payload: &Map<String, Value>) -> bool {
        if scalar_text(payload.get("ownerRunId")) != self.owner_run_id {
            return false;
        }
        matches!(parse_fencing_token(payload, -1), Some(token) if token < self.fencing_token)
    }

    /// Acquire the filesystem writer lease using `O_EXCL` creation or valid takeover.
    pub fn acquire(&mut self) -> Result<(), AppError> {
        probe_atomic_target(&self.root)?;
        let path = self.path();
        let parent = path
            .parent()
            .ok_or_else(|| internal_error("writer lease has no parent directory"))?;
        fs::create_dir_all(parent)
            .map_err(|err| internal_error(format!("Failed to prepare lease directory: {err}")))?;

        for _ in 0..8 {
            let now = (self.clock)();
            let payload = self.payload(now);
            let content = format!("{}\n", dumps_indent_two(&payload));

            match retry_permission(
                || OpenOptions::new().write(true).create_new(true).open(&path),
                10,
                StdDuration::from_millis(50),
            ) {
                Ok(mut file) => {
                    if let Err(err) = (|| -> std::io::Result<()> {
                        file.write_all(content.as_bytes())?;
                        file.flush()?;
                        file.sync_all()?;
                        Ok(())
                    })() {
                        drop(file);
                        let _ = remove_file_if_exists(&path);
                        return Err(internal_error(format!(
                            "Failed to persist target writer lease: {err}"
                        )));
                    }
                    fsync_dir(parent);
                    self.acquired = true;
                    return Ok(());
                }
                Err(err) if err.kind() == ErrorKind::AlreadyExists => {
                    let Some(existing) = self.read_existing() else {
                        // Holder released or left corrupt bytes between O_EXCL and read — retry.
                        continue;
                    };
                    let same_run_takeover = self.same_run_takeover_allowed(&existing);
                    if !Self::is_expired(&existing, now) && !same_run_takeover {
                        return Err(busy_error("Target writer is busy with another run"));
                    }
                    let existing_token = parse_fencing_token(&existing, 0).unwrap_or(i64::MAX);
                    if existing_token >= self.fencing_token {
                        return Err(busy_error(
                            "Target writer is held by a newer or equal fencing token",
                        ));
                    }
                    self.write_replace(&payload)?;
                    if let Some(confirmed) = self.read_existing()
                        && scalar_text(confirmed.get("ownerRunId")) == self.owner_run_id
                        && parse_fencing_token(&confirmed, -1) == Some(self.fencing_token)
                        && !Self::is_expired(&confirmed, (self.clock)())
                    {
                        self.acquired = true;
                        return Ok(());
                    }
                }
                Err(err) if err.kind() == ErrorKind::PermissionDenied => {
                    return Err(busy_error("Target writer is busy with another run"));
                }
                Err(err) => {
                    return Err(internal_error(format!(
                        "Failed to open target writer lease: {err}"
                    )));
                }
            }
        }
        Err(busy_error("Target writer is busy with another run"))
    }

    fn assert_payload_owned(&self, existing: Option<&Map<String, Value>>) -> Result<(), AppError> {
        let Some(existing) = existing else {
            return Err(lost_error(
                "Target writer lease lost: writer.json is missing or unreadable",
            ));
        };
        let run_matches = scalar_text(existing.get("ownerRunId")) == self.owner_run_id;
        let instance_matches =
            scalar_text(existing.get("ownerInstanceId")) == self.owner_instance_id;
        let token_matches = parse_fencing_token(existing, -1) == Some(self.fencing_token);
        if !run_matches || !instance_matches || !token_matches {
            return Err(lost_error("Target writer lease lost to another writer"));
        }
        Ok(())
    }

    /// Renew an actively held lease and confirm ownership afterwards.
    pub fn renew(&mut self) -> Result<(), AppError> {
        let existing = self.read_existing();
        self.assert_payload_owned(existing.as_ref())?;
        let payload = self.payload((self.clock)());
        self.write_replace(&payload)?;
        self.assert_owned()
    }

    /// Assert that `writer.json` still exists, names this run/instance/token, and has not expired.
    pub fn assert_owned(&self) -> Result<(), AppError> {
        let existing = self.read_existing();
        self.assert_payload_owned(existing.as_ref())?;
        let map = existing
            .as_ref()
            .expect("assert_payload_owned ensured Some");
        if Self::is_expired(map, (self.clock)()) {
            return Err(lost_error("Target writer lease expired"));
        }
        Ok(())
    }

    /// Release `.target-lock/writer.json` only if it still names this `ownerRunId` and `fencingToken`.
    pub fn release(&mut self) {
        if let Some(existing) = self.read_existing()
            && scalar_text(existing.get("ownerRunId")) == self.owner_run_id
            && parse_fencing_token(&existing, -1) == Some(self.fencing_token)
        {
            let _ = retry_permission(
                || remove_file_if_exists(&self.path()),
                5,
                StdDuration::from_millis(50),
            );
        }
        self.acquired = false;
    }
}

impl Drop for TargetWriterLease {
    fn drop(&mut self) {
        if self.acquired {
            self.release();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use tempfile::tempdir;

    fn utc(year: i32, month: u32, day: u32, hour: u32, min: u32, sec: u32) -> DateTime<Utc> {
        chrono::TimeZone::with_ymd_and_hms(&Utc, year, month, day, hour, min, sec)
            .single()
            .expect("valid utc timestamp")
    }

    #[test]
    fn acquires_renews_asserts_and_releases_filesystem_lease() {
        let dir = tempdir().unwrap();
        let clock_val = Arc::new(Mutex::new(utc(2026, 9, 5, 15, 0, 0)));
        let c1 = Arc::clone(&clock_val);
        let mut lease = TargetWriterLease::new(dir.path(), "managed-local", "run-1", "inst-1", 1)
            .with_clock(move || *c1.lock().unwrap());
        assert!(!active_writer_lease(
            dir.path(),
            Some(*clock_val.lock().unwrap())
        ));
        lease.acquire().unwrap();
        assert!(lease.path().is_file());
        assert!(active_writer_lease(
            dir.path(),
            Some(*clock_val.lock().unwrap())
        ));

        *clock_val.lock().unwrap() = utc(2026, 9, 5, 15, 1, 0);
        lease.renew().unwrap();
        lease.assert_owned().unwrap();

        lease.release();
        assert!(!lease.path().exists());
    }

    #[test]
    fn busy_and_equal_or_lower_token_takeover_are_refused_with_423() {
        let dir = tempdir().unwrap();
        let now = Arc::new(Mutex::new(utc(2026, 9, 5, 15, 0, 0)));
        let c1 = Arc::clone(&now);
        let mut first = TargetWriterLease::new(dir.path(), "managed-local", "run-1", "inst-1", 5)
            .with_lease_seconds(60)
            .with_clock(move || *c1.lock().unwrap());
        first.acquire().unwrap();

        let c2 = Arc::clone(&now);
        let mut contender =
            TargetWriterLease::new(dir.path(), "managed-local", "run-2", "inst-2", 99)
                .with_clock(move || *c2.lock().unwrap());
        let err = contender.acquire().unwrap_err();
        assert_eq!(err.status, 423);
        assert!(err.message.contains("busy"));

        // Advance past expiry; equal token must fail with 423.
        *now.lock().unwrap() = utc(2026, 9, 5, 15, 2, 0);
        let c3 = Arc::clone(&now);
        let mut equal = TargetWriterLease::new(dir.path(), "managed-local", "run-2", "inst-2", 5)
            .with_clock(move || *c3.lock().unwrap());
        let err = equal.acquire().unwrap_err();
        assert_eq!(err.status, 423);
        assert!(err.message.contains("fencing token"));

        // Strictly higher token takes over an expired lease.
        let c4 = Arc::clone(&now);
        let mut winner = TargetWriterLease::new(dir.path(), "managed-local", "run-2", "inst-2", 6)
            .with_clock(move || *c4.lock().unwrap());
        winner.acquire().unwrap();

        // Preempted holder fails assert_owned with 409 and release preserves winner's lock.
        let lost = first.assert_owned().unwrap_err();
        assert_eq!(lost.status, 409);
        first.release();
        assert!(winner.path().is_file());
        winner.assert_owned().unwrap();
    }

    #[test]
    fn same_run_higher_token_can_preempt_unexpired_lease() {
        let dir = tempdir().unwrap();
        let now = utc(2026, 9, 5, 15, 0, 0);
        let mut crashed = TargetWriterLease::new(dir.path(), "managed-local", "run-1", "inst-a", 1)
            .with_clock(move || now);
        crashed.acquire().unwrap();

        let mut resumed = TargetWriterLease::new(dir.path(), "managed-local", "run-1", "inst-b", 2)
            .with_clock(move || now);
        resumed.acquire().unwrap();
        assert_eq!(crashed.assert_owned().unwrap_err().status, 409);
        crashed.release();
        resumed.assert_owned().unwrap();
    }

    #[test]
    fn matches_frozen_transfer_writer_lease_vectors() {
        let fixture: Value = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../compat/native-runtime/v25/transfer/writer_lease_vector.json"
        )))
        .expect("valid writer_lease_vector.json");
        let now_str = fixture["now"].as_str().expect("frozen now");
        let now = parse_iso(now_str, "now").expect("valid frozen now");

        for case in fixture["cases"].as_array().expect("cases array") {
            let op = case["op"].as_str().expect("op");
            let name = case["name"].as_str().unwrap_or("");
            let expected = &case["expected"];
            if expected.get("status").and_then(Value::as_str) == Some("error")
                && expected.get("oracle_exception").is_some()
            {
                let existing = case["existing"].as_object().expect("existing map");
                assert!(
                    parse_fencing_token(existing, 0).is_none(),
                    "{name}: expected non-integer fencingToken failure"
                );
                continue;
            }
            match op {
                "payload" => {
                    let lease_seconds = case
                        .get("lease_seconds")
                        .and_then(Value::as_i64)
                        .unwrap_or(TARGET_WRITER_LEASE_SECONDS);
                    let lease = TargetWriterLease::new(
                        "/tmp",
                        scalar_text(case.get("target_id")),
                        scalar_text(case.get("owner_run_id")),
                        scalar_text(case.get("owner_instance_id")),
                        case["fencing_token"].as_i64().unwrap(),
                    )
                    .with_lease_seconds(lease_seconds);
                    assert_eq!(lease.payload(now), expected["payload"], "{name}");
                }
                "expired" => {
                    let actual = case
                        .get("existing")
                        .and_then(Value::as_object)
                        .is_none_or(|map| TargetWriterLease::is_expired(map, now));
                    assert_eq!(actual, expected["expired"].as_bool().unwrap(), "{name}");
                }
                "active" => {
                    let payload = case.get("payload").filter(|v| !v.is_null());
                    let actual = is_payload_active(payload, now);
                    assert_eq!(actual, expected["active"].as_bool().unwrap(), "{name}");
                }
                "same-run-takeover" => {
                    let lease = TargetWriterLease::new(
                        "/tmp",
                        "",
                        scalar_text(case.get("owner_run_id")),
                        "",
                        case["fencing_token"].as_i64().unwrap(),
                    );
                    let actual = case
                        .get("existing")
                        .and_then(Value::as_object)
                        .is_some_and(|map| lease.same_run_takeover_allowed(map));
                    assert_eq!(actual, expected["allowed"].as_bool().unwrap(), "{name}");
                }
                "acquire" => {
                    let fencing = case["fencing_token"].as_i64().unwrap();
                    let lease = TargetWriterLease::new(
                        "/tmp",
                        scalar_text(case.get("target_id")),
                        scalar_text(case.get("owner_run_id")),
                        scalar_text(case.get("owner_instance_id")),
                        fencing,
                    );
                    let decision = match case.get("existing").and_then(Value::as_object) {
                        None => "create",
                        Some(existing) => {
                            let same_run = lease.same_run_takeover_allowed(existing);
                            if !TargetWriterLease::is_expired(existing, now) && !same_run {
                                "busy"
                            } else if parse_fencing_token(existing, 0).unwrap_or(i64::MAX)
                                >= fencing
                            {
                                "newer-token"
                            } else {
                                "preempt"
                            }
                        }
                    };
                    assert_eq!(decision, expected["decision"].as_str().unwrap(), "{name}");
                }
                "assert-owned" => {
                    let lease = TargetWriterLease::new(
                        "/tmp",
                        "",
                        scalar_text(case.get("owner_run_id")),
                        scalar_text(case.get("owner_instance_id")),
                        case["fencing_token"].as_i64().unwrap(),
                    );
                    let status = match case.get("existing").and_then(Value::as_object) {
                        None => "missing",
                        Some(existing) => {
                            if lease.assert_payload_owned(Some(existing)).is_err() {
                                "stolen"
                            } else if TargetWriterLease::is_expired(existing, now) {
                                "expired"
                            } else {
                                "ok"
                            }
                        }
                    };
                    let want = if expected["status"] == "ok" {
                        "ok"
                    } else {
                        expected["error"].as_str().unwrap()
                    };
                    assert_eq!(status, want, "{name}");
                }
                "release-allowed" => {
                    let run_id = scalar_text(case.get("owner_run_id"));
                    let fencing = case["fencing_token"].as_i64().unwrap();
                    let allowed =
                        case.get("existing")
                            .and_then(Value::as_object)
                            .is_some_and(|existing| {
                                scalar_text(existing.get("ownerRunId")) == run_id
                                    && parse_fencing_token(existing, -1) == Some(fencing)
                            });
                    assert_eq!(allowed, expected["delete"].as_bool().unwrap(), "{name}");
                }
                other => panic!("unexpected op {other}"),
            }
        }
    }
}
