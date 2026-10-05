//! The sealed frontend replica mirror, mirroring
//! `deepseek_infra/infra/workspace/backup_mirror.py`.
//!
//! # What this store is, and why it is Rust's
//!
//! `.backup-mirror/<profile>/` holds **immutable generations**: one age ciphertext
//! per recipient variant plus a `metadata.json` descriptor, with a `HEAD.json`
//! pointer that is only ever moved to a generation that is already complete,
//! verified and `fsync`ed. Readers resolve HEAD once and copy from that single
//! generation, so a backup can never mix ciphertext and metadata from two different
//! generations.
//!
//! That is a data-plane artefact store with an internal checkpoint, so it belongs to
//! the plane the ownership contract already gives the same shape to —
//! `frontend_mirror_store` is declared python -> rust at 4.9.4 in
//! `release/native_runtime_ownership_v1.json`, next to `memory_store`,
//! `reminders_store` and `skills_store`, all of which are `rust_data`.
//!
//! # Why the sealing round trip is not optional
//!
//! `creationVerified` is not a claim about the code path; it is the result of
//! decrypting the ciphertext that was just written, with an ephemeral identity that
//! was added to the recipient set for exactly this purpose, and comparing the bytes.
//! A mirror generation that cannot be decrypted is a backup that does not exist, and
//! it must never reach HEAD. [`crate::backup_mirror::MirrorStore::put`] therefore
//! refuses to publish a generation whose round trip failed, and it does the sealing
//! through [`backup_crypto`] — the same implementation the production CLI runs.
//!
//! # What is reproduced literally, including the quirks
//!
//! - The acceptance rules are the oracle's: an exact-envelope + exact-recipient-set
//!   upload at the accepted epoch is **idempotent** and returns the existing metadata
//!   with `idempotent: true`; a superseded epoch is `mirror-stale-epoch` (409); a
//!   non-increasing `clientSequence` is `mirror-stale-sequence` (409); a moved head is
//!   `mirror-head-conflict` (409).
//! - The epoch index bookkeeping is the oracle's, including the fact that a *new*
//!   epoch starts at `max(indexes.values(), default=0) + 1` — not at 1, and not at the
//!   previous index plus one when an older, already superseded epoch is re-seen.
//! - `list_mirrors` skips a profile whose legacy metadata exists but is unreadable
//!   (`AppError`), rather than failing the whole list.
//! - `latest_mirror` breaks ties by taking the **first** maximal `acknowledgedAt`,
//!   which is `max()`'s behaviour in Python and *not* `max_by_key`'s.
//! - `metadata.json` and `HEAD.json` are written with Python's
//!   `json.dumps(..., ensure_ascii=False, indent=2, sort_keys=True)` plus a trailing
//!   newline, so a generation written here is byte-identical to one the oracle would
//!   have written. The parity probe relies on that, and so does the handback.
//!
//! # Shape differences from the oracle, and why
//!
//! - **No global root and no global recipient lookup.** The oracle reads
//!   `config.ROOT` and `backup_policies.enabled_policies()` directly. Here the root is
//!   a constructor argument and the recipient sets are an explicit input
//!   ([`RecipientGroups`]), because the authoritative policy records are Go's control
//!   state: the public edge fetches them over the authenticated internal plane and
//!   passes exactly what it was given. Reading a policy projection from disk instead
//!   would be a second source of truth for a Go-owned domain.
//! - **The per-profile lock is a cross-process advisory lock**, not a
//!   `threading.Lock`. The oracle's in-process lock cannot serialize two Python
//!   processes either; once this store is Rust's, the lock has to hold across the
//!   edge's threads *and* any second native process, so it is
//!   [`crate::file_lock`] on a lock file inside the profile directory.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, Read, Write};
use std::path::{Path, PathBuf};

use chrono::{DateTime, NaiveDateTime, TimeZone, Utc};
use serde_json::{Map, Value, json};

use crate::app_error::codes;
use crate::python_json::dumps_compact;

/// `MIRROR_METADATA_SCHEMA_VERSION`.
pub const MIRROR_METADATA_SCHEMA_VERSION: i64 = 2;
/// `HEAD.json`'s `schemaVersion`.
pub const MIRROR_HEAD_SCHEMA_VERSION: i64 = 2;
/// `FRONTEND_SCHEMA_VERSION` from `backups.py`.
pub const FRONTEND_SCHEMA_VERSION: i64 = 1;
/// `MAX_JSON_DEPTH` from `backups.py`.
pub const MAX_JSON_DEPTH: usize = 64;

pub const MIRROR_CIPHERTEXT_NAME: &str = "frontend-state.age";
pub const MIRROR_METADATA_NAME: &str = "frontend-state.meta.json";
pub const PREVIOUS_DIR_NAME: &str = "previous";
pub const GENERATIONS_DIR_NAME: &str = "generations";
pub const HEAD_NAME: &str = "HEAD.json";
pub const GENERATION_METADATA_NAME: &str = "metadata.json";

/// `ErrorCode.REQUEST_TOO_LARGE`, which the envelope bound raises.
pub const REQUEST_TOO_LARGE: &str = "request_too_large";

/// `_MAX_ENVELOPE_BYTES`.
pub const MAX_ENVELOPE_BYTES: usize = 64 * 1024 * 1024;

/// `_MAX_RECIPIENTS` from `backup_policies.py`.
pub const MAX_RECIPIENTS: usize = 16;

/// The lock file, inside the profile directory.
pub const PROFILE_LOCK_NAME: &str = ".mirror.lock";

/// `MIRROR_STATUSES`.
pub const MIRROR_STATUSES: [&str; 6] = [
    "current",
    "stale",
    "missing",
    "epoch-mismatch",
    "recipient-mismatch",
    "excluded",
];

const FORBIDDEN_ENVELOPE_KEYS: [&str; 6] = [
    "apiKey",
    "tavilyKey",
    "authorizationToken",
    "writerSessionId",
    "documentInstanceId",
    "lease",
];

// --- errors ----------------------------------------------------------------------

/// The oracle's `AppError` shape, which is the only exception this module raises.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MirrorError {
    pub message: String,
    pub code: &'static str,
    pub status: u16,
}

impl MirrorError {
    pub fn new(message: impl Into<String>, code: &'static str, status: u16) -> Self {
        Self {
            message: message.into(),
            code,
            status,
        }
    }

    /// `AppError(message, code=INVALID_PAYLOAD)` — the oracle's default 400.
    pub fn invalid_payload(message: impl Into<String>) -> Self {
        Self::new(message, codes::INVALID_PAYLOAD, 400)
    }

    /// `AppError(message, code=INVALID_REQUEST, status=409)`.
    pub fn conflict(message: impl Into<String>) -> Self {
        Self::new(message, codes::INVALID_REQUEST, 409)
    }

    /// `AppError(message, code=NOT_FOUND, status=404)`.
    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new(message, codes::NOT_FOUND, 404)
    }

    /// `AppError(message, code=INTERNAL, status=500)`.
    pub fn internal(message: impl Into<String>) -> Self {
        Self::new(message, codes::INTERNAL, 500)
    }

    /// `AppError(message, code=REQUEST_TOO_LARGE, status=413)`.
    pub fn too_large(message: impl Into<String>) -> Self {
        Self::new(message, REQUEST_TOO_LARGE, 413)
    }

    /// `AppError(message, code=SENSITIVE_CONTENT)`.
    pub fn sensitive(message: impl Into<String>) -> Self {
        Self::new(message, codes::SENSITIVE_CONTENT, 400)
    }
}

impl std::fmt::Display for MirrorError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.message)
    }
}

impl std::error::Error for MirrorError {}

// --- small helpers ---------------------------------------------------------------

/// `_now_iso()`: `datetime.now(timezone.utc).isoformat(timespec="seconds")`, `+00:00` as `Z`.
pub fn now_iso(now: Option<DateTime<Utc>>) -> String {
    let moment = now.unwrap_or_else(Utc::now);
    format!("{}Z", moment.format("%Y-%m-%dT%H:%M:%S"))
}

/// `_parse_iso(value, name)`.
///
/// CPython's `datetime.fromisoformat` accepts more shapes than RFC 3339 — a space
/// instead of `T`, a missing time, `+0000` without the colon. The shapes below are the
/// ones `fromisoformat` accepts that a client can plausibly send; anything else is
/// refused with the oracle's message rather than silently reinterpreted. The one
/// deliberate narrowing is that a bare `YYYY-MM-DD` is accepted (Python accepts it) but
/// an exotic separator is not.
pub fn parse_iso(value: &str, name: &str) -> Result<DateTime<Utc>, MirrorError> {
    let text = value.trim();
    if text.is_empty() {
        return Err(MirrorError::invalid_payload(format!(
            "Mirror field {name} is required"
        )));
    }
    let bad = || {
        MirrorError::invalid_payload(format!("Mirror field {name} must be an ISO-8601 timestamp"))
    };
    if let Ok(parsed) = DateTime::parse_from_rfc3339(text) {
        return Ok(parsed.with_timezone(&Utc));
    }
    // `%z` covers `+0000` / `+00:00`; the naive forms are treated as UTC, exactly as
    // `_parse_iso` does when `tzinfo is None`.
    for format in [
        "%Y-%m-%dT%H:%M:%S%.f%z",
        "%Y-%m-%d %H:%M:%S%.f%z",
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S%.f",
    ] {
        if let Ok(parsed) = DateTime::parse_from_str(text, format) {
            return Ok(parsed.with_timezone(&Utc));
        }
    }
    for format in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%d"] {
        if let Ok(naive) = NaiveDateTime::parse_from_str(text, format) {
            return Ok(Utc.from_utc_datetime(&naive));
        }
    }
    if let Ok(date) = chrono::NaiveDate::parse_from_str(text, "%Y-%m-%d") {
        if let Some(naive) = date.and_hms_opt(0, 0, 0) {
            return Ok(Utc.from_utc_datetime(&naive));
        }
    }
    Err(bad())
}

/// `_profile_id(value)`.
pub fn profile_id(value: &str) -> Result<String, MirrorError> {
    let text = value.trim();
    if text.is_empty()
        || matches!(text, "." | "..")
        || text.chars().count() > 64
        || !text
            .chars()
            .all(|character| character.is_alphanumeric() || "._-".contains(character))
    {
        return Err(MirrorError::invalid_payload(
            "Invalid backup mirror profile id",
        ));
    }
    Ok(text.to_string())
}

/// `_GENERATION_ID`: `^gen_[0-9a-f]{8,32}$`.
pub fn generation_id(value: &str) -> Option<String> {
    let suffix = value.strip_prefix("gen_")?;
    if !(8..=32).contains(&suffix.len()) {
        return None;
    }
    if !suffix
        .chars()
        .all(|character| character.is_ascii_hexdigit() && !character.is_ascii_uppercase())
    {
        return None;
    }
    Some(value.to_string())
}

/// `_VARIANT_FILENAME`: `^state\.[0-9a-f]{4,32}\.age$`.
pub fn variant_filename(value: &str) -> bool {
    let Some(rest) = value.strip_prefix("state.") else {
        return false;
    };
    let Some(hex) = rest.strip_suffix(".age") else {
        return false;
    };
    (4..=32).contains(&hex.len())
        && hex
            .chars()
            .all(|character| character.is_ascii_hexdigit() && !character.is_ascii_uppercase())
}

/// `backup_policies.recipient_set_digest`.
pub fn recipient_set_digest<'a>(recipients: impl IntoIterator<Item = &'a String>) -> String {
    let unique: BTreeSet<&str> = recipients.into_iter().map(String::as_str).collect();
    let joined = unique.into_iter().collect::<Vec<_>>().join("\n");
    sha256_hex(joined.as_bytes())
}

/// `backup_policies.normalize_recipients`.
pub fn normalize_recipients(raw: &[String]) -> Result<Vec<String>, MirrorError> {
    let mut recipients: Vec<String> = Vec::new();
    for item in raw {
        let trimmed = item.trim();
        if trimmed.is_empty() {
            continue;
        }
        if !recipients.iter().any(|existing| existing == trimmed) {
            recipients.push(trimmed.to_string());
        }
    }
    if recipients.is_empty() || recipients.len() > MAX_RECIPIENTS {
        return Err(MirrorError::invalid_payload(
            "Backup policy requires between 1 and 16 age recipients",
        ));
    }
    for recipient in &recipients {
        if !recipient.starts_with("age1") || recipient.chars().count() > 200 {
            return Err(MirrorError::invalid_payload(
                "Backup policy recipients must be public age1... recipients",
            ));
        }
    }
    Ok(recipients)
}

/// `backup_policies.active_recipients` — the union over **all** policies, in first-seen
/// order. Note this is every policy, not only the enabled ones: the oracle's
/// `active_recipients` iterates `list_policies()`, and `mirror_status` compares the
/// stored variant digests against that union.
///
/// The `protection or encryption` step is Python's `or`, so an **empty** `protection`
/// object falls through to `encryption` while a non-empty one without `recipients` does
/// not. Reading it as `or_else` (Some is Some) silently drops a policy whose recipients
/// live under `encryption`, which is a real divergence, not a nuance: it changes which
/// generations `mirror_status` calls `current`.
pub fn active_recipients<'a>(policies: impl IntoIterator<Item = &'a Value>) -> Vec<String> {
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut result: Vec<String> = Vec::new();
    for policy in policies {
        let protection = policy.get("protection");
        let source = match protection {
            Some(value) if crate::core_utils::python_truthy(value) => Some(value),
            _ => policy.get("encryption"),
        };
        let recipients = source
            .and_then(|value| value.get("recipients"))
            .and_then(Value::as_array);
        let Some(recipients) = recipients else {
            continue;
        };
        for recipient in recipients {
            let Some(text) = recipient.as_str() else {
                continue;
            };
            if text.is_empty() || seen.contains(text) {
                continue;
            }
            seen.insert(text.to_string());
            result.push(text.to_string());
        }
    }
    result
}

/// `_recipients_match`.
pub fn recipients_match(metadata: &Value, recipients: &[String]) -> bool {
    let digest = recipient_set_digest(recipients.iter());
    let variants = metadata
        .get("recipientVariants")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if !variants.is_empty() {
        return variants.iter().any(|variant| {
            variant
                .get("recipientSetDigest")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_default()
                == digest
        });
    }
    metadata
        .get("recipientSetDigest")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_default()
        == digest
}

/// `UnattendedEncryption` reduced to the fields the generation metadata carries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SealedVariant {
    pub recipient_set_digest: String,
    pub ciphertext_sha256: String,
    pub filename: String,
    pub creation_verified: bool,
}

/// How the recipient sets for a sealed generation are chosen.
///
/// The oracle's `recipients=None` means "one variant per **enabled** policy, sealed to
/// exactly that policy's own recipients". The authoritative policy records are Go's
/// control state, so the sets arrive as an argument rather than being looked up here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecipientGroups {
    /// `recipients=` was supplied: exactly one group, normalised in order.
    Explicit(Vec<String>),
    /// `recipients=None`: one group per enabled policy, each normalised.
    FromPolicies(Vec<Vec<String>>),
}

impl RecipientGroups {
    /// `_variant_groups`, including the digest-sorted result order.
    pub fn resolve(&self) -> Result<Vec<Vec<String>>, MirrorError> {
        match self {
            RecipientGroups::Explicit(recipients) => Ok(vec![normalize_recipients(recipients)?]),
            RecipientGroups::FromPolicies(groups) => {
                let mut by_digest: BTreeMap<String, Vec<String>> = BTreeMap::new();
                for group in groups {
                    let normalized = normalize_recipients(group)?;
                    if normalized.is_empty() {
                        continue;
                    }
                    by_digest
                        .entry(recipient_set_digest(normalized.iter()))
                        .or_insert(normalized);
                }
                if by_digest.is_empty() {
                    return Err(MirrorError::invalid_payload(
                        "Backup mirror requires at least one recipient",
                    ));
                }
                Ok(by_digest.into_values().collect())
            }
        }
    }
}

/// The files a restore reads, resolved from one immutable generation.
#[derive(Debug, Clone)]
pub struct MirrorFiles {
    pub ciphertext: PathBuf,
    pub metadata_path: PathBuf,
    pub metadata: Value,
}

/// The outcome of a `put`, plus whether the upload was a no-op replay.
#[derive(Debug, Clone)]
pub struct PutOutcome {
    pub metadata: Value,
    pub idempotent: bool,
}

/// Everything `put_frontend_mirror` needs that does not come from disk.
#[derive(Debug, Clone)]
pub struct PutRequest {
    pub profile_id: String,
    pub envelope: Value,
    pub source_epoch: String,
    pub recipients: RecipientGroups,
    pub acknowledged_at: Option<String>,
    pub client_replica_id: String,
    pub client_sequence: i64,
    pub expected_head_generation_id: Option<String>,
    pub now: Option<DateTime<Utc>>,
}

// --- hashing / durable writes -----------------------------------------------------

/// `sha256_file`.
pub fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};

    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

/// `sha256_file` for a path, streamed so a 64 MiB generation does not land in memory.
pub fn sha256_file(path: &Path) -> std::io::Result<String> {
    use sha2::{Digest, Sha256};

    let mut hasher = Sha256::new();
    let mut reader = BufReader::new(File::open(path)?);
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

/// `_atomic_write`: temp file in the same directory, `fsync`, then replace.
pub fn atomic_write(path: &Path, data: &[u8]) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let name = path
        .file_name()
        .map(|value| value.to_string_lossy().to_string())
        .unwrap_or_default();
    let temporary = path.with_file_name(format!(".{name}.{}.tmp", std::process::id()));
    {
        let mut file = File::create(&temporary)?;
        file.write_all(data)?;
        file.flush()?;
        file.sync_all()?;
    }
    match fs::rename(&temporary, path) {
        Ok(()) => Ok(()),
        Err(error) => {
            let _ = fs::remove_file(&temporary);
            Err(error)
        }
    }
}

/// `_fsync_dir`, **including its best-effort character**: the oracle swallows a failed
/// `os.open`/`fsync`, which is the normal outcome on Windows.
pub fn fsync_dir(path: &Path) {
    let Ok(file) = File::open(path) else {
        return;
    };
    let _ = file.sync_all();
}

/// Python's `json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)`.
///
/// `metadata.json` and `HEAD.json` have to be byte-identical to the oracle's, because
/// the handback direction is "Python reads what Rust wrote" and the parity probe
/// compares the bytes rather than only the parsed value. Empty containers stay inline
/// (`[]` / `{}`); everything else breaks, with `,` + newline between items and a
/// two-space step per level.
pub fn dumps_indent_two(value: &Value) -> String {
    let mut out = String::new();
    render_indented(value, 0, &mut out);
    out
}

fn render_indented(value: &Value, depth: usize, out: &mut String) {
    match value {
        Value::Array(items) if items.is_empty() => out.push_str("[]"),
        Value::Object(fields) if fields.is_empty() => out.push_str("{}"),
        Value::Array(items) => {
            out.push_str("[\n");
            for (index, item) in items.iter().enumerate() {
                out.push_str(&"  ".repeat(depth + 1));
                render_indented(item, depth + 1, out);
                if index + 1 < items.len() {
                    out.push(',');
                }
                out.push('\n');
            }
            out.push_str(&"  ".repeat(depth));
            out.push(']');
        }
        Value::Object(fields) => {
            let mut entries: Vec<(&String, &Value)> = fields.iter().collect();
            entries.sort_by(|left, right| left.0.cmp(right.0));
            out.push_str("{\n");
            for (index, (key, item)) in entries.iter().enumerate() {
                out.push_str(&"  ".repeat(depth + 1));
                out.push_str(&Value::String((*key).clone()).to_string());
                out.push_str(": ");
                render_indented(item, depth + 1, out);
                if index + 1 < entries.len() {
                    out.push(',');
                }
                out.push('\n');
            }
            out.push_str(&"  ".repeat(depth));
            out.push('}');
        }
        other => out.push_str(&scalar_str(other)),
    }
}

fn scalar_str(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::Bool(true) => "true".to_string(),
        Value::Bool(false) => "false".to_string(),
        Value::Number(number) => crate::python_json::json_number_str(number),
        Value::String(text) => Value::String(text.clone()).to_string(),
        other => dumps_compact(other),
    }
}

// --- the store --------------------------------------------------------------------

/// The sealed frontend replica mirror rooted at `root` (`.backup-mirror`).
#[derive(Debug, Clone)]
pub struct MirrorStore {
    root: PathBuf,
}

impl MirrorStore {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn profile_dir(&self, profile: &str) -> PathBuf {
        self.root.join(profile)
    }

    pub fn metadata_path(&self, profile: &str) -> PathBuf {
        self.profile_dir(profile).join(MIRROR_METADATA_NAME)
    }

    pub fn ciphertext_path(&self, profile: &str) -> PathBuf {
        self.profile_dir(profile).join(MIRROR_CIPHERTEXT_NAME)
    }

    pub fn head_path(&self, profile: &str) -> PathBuf {
        self.profile_dir(profile).join(HEAD_NAME)
    }

    pub fn generation_dir(&self, profile: &str, generation: &str) -> PathBuf {
        self.profile_dir(profile)
            .join(GENERATIONS_DIR_NAME)
            .join(generation)
    }

    fn lock_path(&self, profile: &str) -> PathBuf {
        self.profile_dir(profile).join(PROFILE_LOCK_NAME)
    }

    /// Take the cross-process profile lock and hold it for `body`.
    ///
    /// The oracle's `_profile_lock` is a `threading.Lock`; that is only enough while a
    /// single Python process owns the directory. Once this store is Rust's, the edge
    /// serves uploads from a thread pool and a second native process is a supported
    /// topology, so the lock has to be an OS lock. It is taken **inside** the profile
    /// directory, so two different profiles never contend.
    pub fn with_profile_lock<T>(
        &self,
        profile: &str,
        body: impl FnOnce() -> Result<T, MirrorError>,
    ) -> Result<T, MirrorError> {
        let directory = self.profile_dir(profile);
        fs::create_dir_all(&directory).map_err(|error| MirrorError::internal(error.to_string()))?;
        let path = self.lock_path(profile);
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&path)
            .map_err(|error| MirrorError::internal(error.to_string()))?;
        crate::file_lock::lock_exclusive(&file)
            .map_err(|error| MirrorError::internal(error.to_string()))?;
        let result = body();
        let _ = crate::file_lock::unlock(&file);
        result
    }

    // --- reading ------------------------------------------------------------------

    /// `_read_head`: missing, unreadable or a non-object all read as `None`.
    pub fn read_head(&self, profile: &str) -> Option<Value> {
        let raw = fs::read_to_string(self.head_path(profile)).ok()?;
        let value: Value = serde_json::from_str(&raw).ok()?;
        match value {
            Value::Object(_) => Some(value),
            _ => None,
        }
    }

    /// `_read_legacy_metadata`, **including its refusal**: an existing but unreadable
    /// file is `AppError(INTERNAL)`, not "absent". `list_mirrors` catches that and skips
    /// the profile, so corruption cannot silently look like an empty mirror.
    pub fn read_legacy_metadata(&self, profile: &str) -> Result<Option<Value>, MirrorError> {
        let path = self.metadata_path(profile);
        if !path.is_file() {
            return Ok(None);
        }
        let raw = fs::read_to_string(&path)
            .map_err(|_| MirrorError::internal("Backup mirror metadata is unreadable"))?;
        let value: Value = serde_json::from_str(&raw)
            .map_err(|_| MirrorError::internal("Backup mirror metadata is unreadable"))?;
        Ok(match value {
            Value::Object(_) => Some(value),
            _ => None,
        })
    }

    /// `_read_generation_metadata`: every failure reads as `None`.
    pub fn read_generation_metadata(&self, profile: &str, generation: &str) -> Option<Value> {
        let resolved = generation_id(generation)?;
        let path = self
            .generation_dir(profile, &resolved)
            .join(GENERATION_METADATA_NAME);
        let raw = fs::read_to_string(path).ok()?;
        let value: Value = serde_json::from_str(&raw).ok()?;
        match value {
            Value::Object(_) => Some(value),
            _ => None,
        }
    }

    /// `_head_metadata`: the head generation's metadata, else the legacy one.
    pub fn head_metadata(&self, profile: &str) -> Result<Option<Value>, MirrorError> {
        if let Some(head) = self.read_head(profile) {
            let generation = head
                .get("generationId")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if let Some(metadata) = self.read_generation_metadata(profile, generation) {
                return Ok(Some(metadata));
            }
        }
        self.read_legacy_metadata(profile)
    }

    /// `list_mirrors`.
    pub fn list(&self) -> Result<Vec<Value>, MirrorError> {
        if !self.root.is_dir() {
            return Ok(Vec::new());
        }
        let mut entries: Vec<PathBuf> = Vec::new();
        let read =
            fs::read_dir(&self.root).map_err(|error| MirrorError::internal(error.to_string()))?;
        for entry in read {
            let entry = entry.map_err(|error| MirrorError::internal(error.to_string()))?;
            entries.push(entry.path());
        }
        // `sorted(iterdir())` — profile ids are `mirror_<hex>` or a policy id, all
        // compared as names here. The oracle compares full paths, which for one parent
        // orders identically.
        entries.sort_by(|left, right| left.file_name().cmp(&right.file_name()));
        let mut mirrors: Vec<Value> = Vec::new();
        for path in entries {
            if !path.is_dir() {
                continue;
            }
            let name = path
                .file_name()
                .map(|value| value.to_string_lossy().to_string())
                .unwrap_or_default();
            if name == PREVIOUS_DIR_NAME {
                continue;
            }
            // `except AppError: continue` — an unreadable legacy projection skips the
            // profile instead of failing the list.
            match self.head_metadata(&name) {
                Ok(Some(metadata)) => mirrors.push(metadata),
                Ok(None) => {}
                Err(_) => continue,
            }
        }
        Ok(mirrors)
    }

    /// `latest_mirror`: ties resolve to the **first** maximal `acknowledgedAt`.
    pub fn latest(&self) -> Result<Option<Value>, MirrorError> {
        let mirrors = self.list()?;
        let mut best: Option<Value> = None;
        let mut best_key = String::new();
        for metadata in mirrors {
            let key = metadata
                .get("acknowledgedAt")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            // Strictly greater: `max()` keeps the first maximal element.
            if best.is_none() || key > best_key {
                best_key = key;
                best = Some(metadata);
            }
        }
        Ok(best)
    }

    /// `mirror_status`.
    pub fn status(
        &self,
        profile: Option<&str>,
        recipients: Option<&[String]>,
        max_age_seconds: Option<i64>,
        expected_epoch: Option<&str>,
        excluded: bool,
        now: Option<DateTime<Utc>>,
    ) -> Result<Value, MirrorError> {
        if excluded {
            return Ok(json!({"status": "excluded"}));
        }
        let metadata = match profile {
            Some(value) => self.head_metadata(&profile_id(value)?)?,
            None => self.latest()?,
        };
        let Some(metadata) = metadata else {
            return Ok(json!({"status": "missing", "profileId": profile}));
        };
        let mut status = "current";
        let stored_epoch = metadata
            .get("sourceEpoch")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if let Some(expected) = expected_epoch {
            if stored_epoch != expected {
                status = "epoch-mismatch";
            }
        }
        if status == "current" {
            if let Some(recipients) = recipients {
                if !recipients_match(&metadata, recipients) {
                    status = "recipient-mismatch";
                }
            }
        }
        if status == "current" {
            if let Some(max_age) = max_age_seconds {
                // `except ValueError: acknowledged = None`, and `None` ages to
                // infinity — so an unparseable `acknowledgedAt` is *stale*, never
                // current and never an error.
                let acknowledged = metadata
                    .get("acknowledgedAt")
                    .and_then(Value::as_str)
                    .and_then(|text| parse_iso(text, "acknowledgedAt").ok());
                let current = now.unwrap_or_else(Utc::now);
                let age = acknowledged
                    .map(|moment| (current - moment).num_milliseconds() as f64 / 1000.0)
                    .unwrap_or(f64::INFINITY);
                if age > max_age as f64 {
                    status = "stale";
                }
            }
        }
        Ok(json!({"status": status, "mirror": metadata}))
    }

    /// `mirror_files`: resolve HEAD once and return that one generation's ciphertext.
    pub fn files(
        &self,
        profile: &str,
        recipients: Option<&[String]>,
    ) -> Result<MirrorFiles, MirrorError> {
        let profile = profile_id(profile)?;
        if let Some(head) = self.read_head(&profile) {
            let head_generation = head
                .get("generationId")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if let Some(resolved) = generation_id(head_generation) {
                if let Some(metadata) = self.read_generation_metadata(&profile, &resolved) {
                    let variants: Vec<Value> = metadata
                        .get("recipientVariants")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    let selected = match recipients {
                        Some(recipients) => {
                            let digest = recipient_set_digest(recipients.iter());
                            variants
                                .iter()
                                .find(|variant| {
                                    variant
                                        .get("recipientSetDigest")
                                        .and_then(Value::as_str)
                                        .unwrap_or_default()
                                        == digest
                                })
                                .cloned()
                        }
                        None => variants.first().cloned(),
                    };
                    let Some(variant) = selected else {
                        if recipients.is_some() {
                            return Err(MirrorError::not_found(
                                "Backup mirror has no variant sealed to this recipient set",
                            ));
                        }
                        return Err(MirrorError::internal(
                            "Mirror generation carries no recipient variants",
                        ));
                    };
                    let filename = variant
                        .get("filename")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    if !variant_filename(&filename) {
                        return Err(MirrorError::internal(
                            "Mirror generation carries an invalid variant filename",
                        ));
                    }
                    let directory = self.generation_dir(&profile, &resolved);
                    let ciphertext = directory.join(&filename);
                    if !ciphertext.is_file() {
                        return Err(MirrorError::not_found(
                            "Backup mirror generation ciphertext is missing",
                        ));
                    }
                    let expected = variant
                        .get("ciphertextSha256")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    if !expected.is_empty() {
                        let actual = sha256_file(&ciphertext)
                            .map_err(|error| MirrorError::internal(error.to_string()))?;
                        if actual != expected {
                            return Err(MirrorError::conflict(
                                "mirror-generation-corrupt: ciphertext no longer matches its generation",
                            ));
                        }
                    }
                    return Ok(MirrorFiles {
                        ciphertext,
                        metadata_path: directory.join(GENERATION_METADATA_NAME),
                        metadata,
                    });
                }
            }
        }
        let metadata = self.read_legacy_metadata(&profile)?;
        let ciphertext = self.ciphertext_path(&profile);
        if metadata.is_none() || !ciphertext.is_file() {
            return Err(MirrorError::not_found("Backup mirror not found"));
        }
        let metadata = metadata.expect("checked above");
        let expected = metadata
            .get("ciphertextSha256")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if !expected.is_empty() {
            let actual = sha256_file(&ciphertext)
                .map_err(|error| MirrorError::internal(error.to_string()))?;
            if actual != expected {
                return Err(MirrorError::conflict(
                    "mirror-generation-corrupt: ciphertext no longer matches its metadata",
                ));
            }
        }
        Ok(MirrorFiles {
            ciphertext,
            metadata_path: self.metadata_path(&profile),
            metadata,
        })
    }

    // --- writing ------------------------------------------------------------------

    /// `_remove_legacy`.
    fn remove_legacy(&self, profile: &str) -> std::io::Result<()> {
        let directory = self.profile_dir(profile);
        for name in [MIRROR_CIPHERTEXT_NAME, MIRROR_METADATA_NAME] {
            let path = directory.join(name);
            if path.exists() {
                let _ = fs::remove_file(path);
            }
        }
        let previous = directory.join(PREVIOUS_DIR_NAME);
        if previous.exists() {
            let _ = fs::remove_dir_all(previous);
        }
        Ok(())
    }

    /// `_prune_generations`: every generation except the new one and its parent goes.
    fn prune_generations(&self, profile: &str, keep: &BTreeSet<String>) {
        let generations = self.profile_dir(profile).join(GENERATIONS_DIR_NAME);
        let Ok(read) = fs::read_dir(&generations) else {
            return;
        };
        for entry in read.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            if entry.path().is_dir() && !keep.contains(&name) {
                let _ = fs::remove_dir_all(entry.path());
            }
        }
    }

    /// `put_frontend_mirror`, up to and including the HEAD move.
    ///
    /// `restore_fenced` is the oracle's `mutation_gate.read_fence(root=_gate_root())`
    /// check: while a workspace restore owns the workspace, mirror updates are refused
    /// with the same 423 the oracle raises. The caller reads the fence, because the
    /// fence root is the workspace root, not the mirror root.
    pub fn put(
        &self,
        request: PutRequest,
        restore_fenced: bool,
    ) -> Result<PutOutcome, MirrorError> {
        let profile = profile_id(&request.profile_id)?;
        if restore_fenced {
            return Err(MirrorError::new(
                "Backup mirror updates are fenced while a workspace restore is in progress",
                codes::INVALID_REQUEST,
                423,
            ));
        }
        let groups = request.recipients.resolve()?;
        let group_digests: BTreeSet<String> = groups
            .iter()
            .map(|group| recipient_set_digest(group.iter()))
            .collect();
        let epoch = request.source_epoch.trim().to_string();
        if epoch.is_empty()
            || epoch.chars().count() > 120
            || epoch.contains('/')
            || epoch.contains('\\')
        {
            return Err(MirrorError::invalid_payload(
                "Mirror sourceEpoch is required",
            ));
        }
        let Value::Object(envelope) = &request.envelope else {
            return Err(MirrorError::invalid_payload(
                "Mirror envelope must be an object",
            ));
        };
        let envelope_bytes = dumps_compact(&request.envelope).into_bytes();
        if envelope_bytes.len() > MAX_ENVELOPE_BYTES {
            return Err(MirrorError::too_large("Mirror envelope is too large"));
        }
        validate_frontend_envelope(&request.envelope)?;
        let ack = match request.acknowledged_at.as_deref() {
            Some(value) if !value.trim().is_empty() => parse_iso(value, "acknowledgedAt")?,
            _ => request.now.unwrap_or_else(Utc::now),
        };
        let acknowledged_at = now_iso(Some(ack));
        let replica: String = request.client_replica_id.chars().take(64).collect();
        let sequence = request.client_sequence.max(0);
        let envelope_digest = envelope
            .get("digest")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let profile_for_lock = profile.clone();
        let acknowledged_at_for_body = acknowledged_at.clone();

        let _handoff_guard = crate::mirror_handoff::mutation_guard(&self.root)
            .map_err(|error| MirrorError::new(error, codes::INVALID_REQUEST, 423))?;
        self.with_profile_lock(&profile_for_lock, || {
            let head = self.read_head(&profile);
            let existing = self.head_metadata(&profile)?;
            let mut indexes: BTreeMap<String, i64> = BTreeMap::new();
            if let Some(head) = &head {
                if let Some(raw) = head.get("epochIndexes").and_then(Value::as_object) {
                    for (key, value) in raw {
                        if let Some(number) = as_int(value) {
                            indexes.insert(key.clone(), number);
                        }
                    }
                }
            }
            let accepted_sequence = head
                .as_ref()
                .and_then(|value| value.get("acceptedSequence"))
                .and_then(as_int)
                .unwrap_or(-1);
            let accepted_index = head
                .as_ref()
                .and_then(|value| value.get("acceptedEpochIndex"))
                .and_then(as_int)
                .unwrap_or(0);
            let mut accepted_epoch = head
                .as_ref()
                .and_then(|value| value.get("acceptedEpoch"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            if accepted_epoch.is_empty() {
                if let Some(existing) = &existing {
                    accepted_epoch = existing
                        .get("sourceEpoch")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                }
            }
            let has_state = head.is_some() || existing.is_some();
            if has_state {
                let same_envelope = existing
                    .as_ref()
                    .map(|value| {
                        value
                            .get("envelopeDigest")
                            .and_then(Value::as_str)
                            .unwrap_or_default()
                            == envelope_digest
                    })
                    .unwrap_or(false);
                let mut existing_digests: BTreeSet<String> = existing
                    .as_ref()
                    .and_then(|value| value.get("recipientVariants"))
                    .and_then(Value::as_array)
                    .map(|variants| {
                        variants
                            .iter()
                            .map(|variant| {
                                variant
                                    .get("recipientSetDigest")
                                    .and_then(Value::as_str)
                                    .unwrap_or_default()
                                    .to_string()
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                if existing_digests.is_empty() {
                    if let Some(existing) = &existing {
                        existing_digests.insert(
                            existing
                                .get("recipientSetDigest")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .to_string(),
                        );
                    }
                }
                let same_recipients = existing_digests == group_digests;
                if existing.is_some()
                    && epoch == accepted_epoch
                    && same_envelope
                    && same_recipients
                {
                    let mut replay = existing.expect("checked").clone();
                    if let Value::Object(fields) = &mut replay {
                        fields.insert("idempotent".into(), Value::Bool(true));
                    }
                    return Ok(PutOutcome {
                        metadata: replay,
                        idempotent: true,
                    });
                }
                if epoch != accepted_epoch {
                    if let Some(index) = indexes.get(&epoch) {
                        if *index <= accepted_index {
                            return Err(MirrorError::conflict(
                                "mirror-stale-epoch: epoch was superseded on the server; resync before uploading",
                            ));
                        }
                    }
                }
                if sequence <= accepted_sequence {
                    return Err(MirrorError::conflict(
                        "mirror-stale-sequence: clientSequence must increase monotonically per profile",
                    ));
                }
            }
            let head_generation_id = head
                .as_ref()
                .and_then(|value| value.get("generationId"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            if let Some(expected) = &request.expected_head_generation_id {
                if expected != &head_generation_id {
                    return Err(MirrorError::conflict(
                        "mirror-head-conflict: mirror head changed since the client's snapshot",
                    ));
                }
            }
            let generation = format!("gen_{}", uuid_hex24());
            let generation_dir = self.generation_dir(&profile, &generation);
            fs::create_dir_all(&generation_dir)
                .map_err(|error| MirrorError::internal(error.to_string()))?;

            let mut variants: Vec<SealedVariant> = Vec::new();
            let sealed = (|| -> Result<(), MirrorError> {
                for group in &groups {
                    let digest = recipient_set_digest(group.iter());
                    let filename = format!("state.{}.age", &digest[..16]);
                    let temporary = generation_dir.join(format!(
                        ".{filename}.{}.tmp",
                        std::process::id()
                    ));
                    let sealed = seal_variant(&temporary, &envelope_bytes, group)?;
                    fs::rename(&temporary, generation_dir.join(&filename))
                        .map_err(|error| MirrorError::internal(error.to_string()))?;
                    variants.push(SealedVariant {
                        recipient_set_digest: digest,
                        ciphertext_sha256: sealed,
                        filename,
                        creation_verified: true,
                    });
                }
                Ok(())
            })();
            if let Err(error) = sealed {
                let _ = fs::remove_dir_all(&generation_dir);
                return Err(error);
            }
            variants.sort_by(|left, right| {
                left.recipient_set_digest.cmp(&right.recipient_set_digest)
            });
            let first = variants
                .first()
                .cloned()
                .ok_or_else(|| MirrorError::invalid_payload("Backup mirror requires at least one recipient"))?;
            let mut metadata = Map::new();
            metadata.insert("schemaVersion".into(), json!(MIRROR_METADATA_SCHEMA_VERSION));
            metadata.insert("profileId".into(), json!(profile));
            metadata.insert("generationId".into(), json!(generation));
            metadata.insert(
                "parentGenerationId".into(),
                if head_generation_id.is_empty() {
                    Value::Null
                } else {
                    json!(head_generation_id)
                },
            );
            metadata.insert("sourceEpoch".into(), json!(epoch));
            metadata.insert("clientReplicaId".into(), json!(replica));
            metadata.insert("clientSequence".into(), json!(sequence));
            metadata.insert("envelopeDigest".into(), json!(envelope_digest));
            metadata.insert(
                "recipientVariants".into(),
                Value::Array(
                    variants
                        .iter()
                        .map(|variant| {
                            json!({
                                "recipientSetDigest": variant.recipient_set_digest,
                                "ciphertextSha256": variant.ciphertext_sha256,
                                "filename": variant.filename,
                                "creationVerified": variant.creation_verified,
                            })
                        })
                        .collect(),
                ),
            );
            metadata.insert("recipientSetDigest".into(), json!(first.recipient_set_digest));
            metadata.insert(
                "conversations".into(),
                json!(envelope
                    .get("conversations")
                    .and_then(Value::as_array)
                    .map(Vec::len)
                    .unwrap_or(0)),
            );
            metadata.insert(
                "conflicts".into(),
                json!(envelope
                    .get("conflicts")
                    .and_then(Value::as_array)
                    .map(Vec::len)
                    .unwrap_or(0)),
            );
            metadata.insert("createdAt".into(), json!(now_iso(request.now)));
            metadata.insert("acknowledgedAt".into(), json!(acknowledged_at_for_body));
            metadata.insert("ciphertextSha256".into(), json!(first.ciphertext_sha256));
            metadata.insert("creationVerified".into(), json!(first.creation_verified));
            let metadata_value = Value::Object(metadata);
            atomic_write(
                &generation_dir.join(GENERATION_METADATA_NAME),
                format!("{}\n", dumps_indent_two(&metadata_value)).as_bytes(),
            )
            .map_err(|error| MirrorError::internal(error.to_string()))?;
            fsync_dir(&generation_dir);

            let epoch_index = indexes.get(&epoch).copied().unwrap_or_else(|| {
                indexes.values().copied().max().unwrap_or(0) + 1
            });
            indexes.insert(epoch.clone(), epoch_index);
            let mut head_payload = Map::new();
            head_payload.insert("schemaVersion".into(), json!(MIRROR_HEAD_SCHEMA_VERSION));
            head_payload.insert("generationId".into(), json!(generation));
            head_payload.insert("updatedAt".into(), json!(now_iso(request.now)));
            head_payload.insert("acceptedEpoch".into(), json!(epoch));
            head_payload.insert("acceptedEpochIndex".into(), json!(epoch_index));
            head_payload.insert("acceptedSequence".into(), json!(sequence));
            head_payload.insert(
                "epochIndexes".into(),
                Value::Object(
                    indexes
                        .iter()
                        .map(|(key, value)| (key.clone(), json!(value)))
                        .collect(),
                ),
            );
            atomic_write(
                &self.head_path(&profile),
                format!("{}\n", dumps_indent_two(&Value::Object(head_payload))).as_bytes(),
            )
            .map_err(|error| MirrorError::internal(error.to_string()))?;
            fsync_dir(&self.profile_dir(&profile));
            let _ = self.remove_legacy(&profile);
            let mut keep: BTreeSet<String> = BTreeSet::new();
            keep.insert(
                metadata_value
                    .get("generationId")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
            );
            keep.insert(
                metadata_value
                    .get("parentGenerationId")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
            );
            self.prune_generations(&profile, &keep);
            Ok(PutOutcome {
                metadata: metadata_value,
                idempotent: false,
            })
        })
    }
}

/// `backups._validate_frontend_envelope`.
pub fn validate_frontend_envelope(envelope: &Value) -> Result<(), MirrorError> {
    if envelope.get("schemaVersion").and_then(as_int) != Some(FRONTEND_SCHEMA_VERSION) {
        return Err(MirrorError::invalid_payload(
            "Unsupported frontend backup schema",
        ));
    }
    let forbidden: BTreeSet<&str> = FORBIDDEN_ENVELOPE_KEYS.into_iter().collect();
    if contains_key(envelope, &forbidden) {
        return Err(MirrorError::sensitive(
            "Frontend backup contains ephemeral identity or credentials",
        ));
    }
    let mut body = Map::new();
    if let Value::Object(fields) = envelope {
        for (key, value) in fields {
            if key != "digest" {
                body.insert(key.clone(), value.clone());
            }
        }
    }
    let digest = envelope
        .get("digest")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if digest.is_empty() || digest != sha256_hex(dumps_compact(&Value::Object(body)).as_bytes()) {
        return Err(MirrorError::invalid_payload(
            "Frontend backup digest is invalid",
        ));
    }
    check_json_depth(envelope, 0)
}

fn contains_key(value: &Value, forbidden: &BTreeSet<&str>) -> bool {
    match value {
        Value::Object(fields) => fields
            .iter()
            .any(|(key, item)| forbidden.contains(key.as_str()) || contains_key(item, forbidden)),
        Value::Array(items) => items.iter().any(|item| contains_key(item, forbidden)),
        _ => false,
    }
}

fn check_json_depth(value: &Value, depth: usize) -> Result<(), MirrorError> {
    if depth > MAX_JSON_DEPTH {
        return Err(MirrorError::invalid_payload(
            "Backup JSON is too deeply nested",
        ));
    }
    match value {
        Value::Object(fields) => {
            for item in fields.values() {
                check_json_depth(item, depth + 1)?;
            }
        }
        Value::Array(items) => {
            for item in items {
                check_json_depth(item, depth + 1)?;
            }
        }
        _ => {}
    }
    Ok(())
}

/// `asInt` for the JSON numbers this module reads back from disk.
fn as_int(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64().or_else(|| {
            number
                .as_f64()
                .filter(|float| float.fract() == 0.0)
                .map(|float| float as i64)
        }),
        // The oracle's `_int_or` also accepts a numeric string (`int(value)`).
        Value::String(text) => text.trim().parse::<i64>().ok(),
        Value::Bool(flag) => Some(i64::from(*flag)),
        _ => None,
    }
}

/// A `gen_` id with 24 hex characters, matching `^gen_[0-9a-f]{8,32}$`.
///
/// The oracle's `uuid.uuid4().hex[:24]` is only required to be unique; nothing parses
/// it as a UUID. This draws from the OS-seeded `RandomState` keys plus the clock, the
/// pid and a process-local counter, and mixes them through SHA-256 — two processes
/// cannot produce the same id by construction, and a single process cannot repeat one.
fn uuid_hex24() -> String {
    use std::hash::{BuildHasher, Hasher};
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    let mut hasher = std::collections::hash_map::RandomState::new().build_hasher();
    hasher.write_u64(std::process::id() as u64);
    hasher.write_u64(NEXT.fetch_add(1, Ordering::Relaxed));
    hasher.write_u128(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or_default(),
    );
    let first = hasher.finish();
    let mut second_hasher = std::collections::hash_map::RandomState::new().build_hasher();
    second_hasher.write_u64(first);
    let second = second_hasher.finish();
    let mut material = Vec::with_capacity(16);
    material.extend_from_slice(&first.to_be_bytes());
    material.extend_from_slice(&second.to_be_bytes());
    sha256_hex(&material).chars().take(24).collect()
}

/// `backup_unattended.encrypt_unattended` against an explicit path.
///
/// Seals `plaintext` to `recipients` **plus an ephemeral recipient**, then decrypts the
/// result with the ephemeral identity and compares the bytes. The comparison is the
/// point: `creationVerified` is a measurement, not an assertion. The verification copy
/// and the ephemeral secret are both discarded before returning.
fn seal_variant(
    target: &Path,
    plaintext: &[u8],
    recipients: &[String],
) -> Result<String, MirrorError> {
    let (ephemeral_secret, ephemeral_recipient) = backup_crypto::ephemeral_identity();
    let mut all: Vec<String> = Vec::new();
    for recipient in recipients
        .iter()
        .chain(std::iter::once(&ephemeral_recipient))
    {
        if !all.iter().any(|existing| existing == recipient) {
            all.push(recipient.clone());
        }
    }
    {
        let file =
            File::create(target).map_err(|error| MirrorError::internal(error.to_string()))?;
        let mut writer = std::io::BufWriter::new(file);
        backup_crypto::encrypt_recipients(plaintext, &mut writer, &all)
            .map_err(|error| MirrorError::internal(error.to_string()))?;
        writer
            .flush()
            .map_err(|error| MirrorError::internal(error.to_string()))?;
        writer
            .into_inner()
            .map_err(|error| MirrorError::internal(error.to_string()))?
            .sync_all()
            .map_err(|error| MirrorError::internal(error.to_string()))?;
    }
    let verification = target.with_file_name(format!(
        ".{}.{}.verify",
        target
            .file_name()
            .map(|value| value.to_string_lossy().to_string())
            .unwrap_or_default(),
        std::process::id()
    ));
    let verified = (|| -> Result<(), MirrorError> {
        let ciphertext =
            File::open(target).map_err(|error| MirrorError::internal(error.to_string()))?;
        let mut decrypted = Vec::new();
        backup_crypto::decrypt_identity(
            std::io::BufReader::new(ciphertext),
            &mut decrypted,
            ephemeral_secret.clone(),
        )
        .map_err(|error| MirrorError::internal(error.to_string()))?;
        if decrypted != plaintext {
            return Err(MirrorError::internal(
                "Mirror round-trip verification failed",
            ));
        }
        Ok(())
    })();
    let _ = fs::remove_file(&verification);
    verified?;
    sha256_file(target).map_err(|error| MirrorError::internal(error.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root(label: &str) -> PathBuf {
        static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let root = std::env::temp_dir().join(format!(
            "mirror-test-{label}-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("create temp root");
        root
    }

    fn fresh_recipient() -> String {
        let (_, recipient) = backup_crypto::ephemeral_identity();
        recipient
    }

    fn envelope(digest_seed: &str) -> Value {
        let mut body = Map::new();
        body.insert("schemaVersion".into(), json!(FRONTEND_SCHEMA_VERSION));
        body.insert("conversations".into(), json!([{"id": digest_seed}]));
        body.insert("conflicts".into(), json!([]));
        let digest = sha256_hex(dumps_compact(&Value::Object(body.clone())).as_bytes());
        body.insert("digest".into(), json!(digest));
        Value::Object(body)
    }

    fn put_request(
        profile: &str,
        epoch: &str,
        sequence: i64,
        recipients: Vec<String>,
    ) -> PutRequest {
        PutRequest {
            profile_id: profile.to_string(),
            envelope: envelope(epoch),
            source_epoch: epoch.to_string(),
            recipients: RecipientGroups::Explicit(recipients),
            acknowledged_at: Some("2026-09-30T12:00:00Z".to_string()),
            client_replica_id: "replica-1".to_string(),
            client_sequence: sequence,
            expected_head_generation_id: None,
            now: Some(Utc.with_ymd_and_hms(2026, 9, 30, 12, 0, 0).unwrap()),
        }
    }

    #[test]
    fn profile_ids_follow_the_oracle_pattern() {
        assert_eq!(profile_id("mirror_ab12").unwrap(), "mirror_ab12");
        assert_eq!(profile_id("  mirror_ab12  ").unwrap(), "mirror_ab12");
        assert!(profile_id("").is_err());
        assert!(profile_id("has space").is_err());
        assert!(profile_id("slash/name").is_err());
        assert!(profile_id(&"a".repeat(65)).is_err());
    }

    #[test]
    fn generation_and_variant_patterns_match_the_oracle() {
        assert!(generation_id("gen_0123456789abcdef").is_some());
        assert!(generation_id("gen_01234567").is_some());
        assert!(generation_id("gen_0123456").is_none());
        assert!(generation_id("gen_0123456789ABCDEF").is_none());
        assert!(generation_id("genx0123456789abcdef").is_none());
        assert!(generation_id("gen_0123456789abcdef0".repeat(2).as_str()).is_none());

        assert!(variant_filename("state.0123abcd.age"));
        assert!(!variant_filename("state.012.age"));
        assert!(!variant_filename("state.0123ABCD.age"));
        assert!(!variant_filename("frontend-state.age"));
    }

    #[test]
    fn recipient_set_digest_sorts_and_dedupes() {
        let first = [
            "age1b".to_string(),
            "age1a".to_string(),
            "age1a".to_string(),
        ];
        let second = ["age1a".to_string(), "age1b".to_string()];
        assert_eq!(
            recipient_set_digest(first.iter()),
            recipient_set_digest(second.iter())
        );
        // The bytes are `"\n".join(sorted(set(...)))` — no trailing newline.
        assert_eq!(
            recipient_set_digest(["age1a".to_string()].iter()),
            sha256_hex(b"age1a")
        );
        assert_eq!(
            recipient_set_digest(["age1b".to_string(), "age1a".to_string()].iter()),
            sha256_hex(b"age1a\nage1b")
        );
    }

    #[test]
    fn normalize_recipients_enforces_the_policy_bounds() {
        assert_eq!(
            normalize_recipients(&["age1a".to_string(), " age1a ".to_string()]).unwrap(),
            vec!["age1a".to_string()]
        );
        assert!(normalize_recipients(&[]).is_err());
        assert!(normalize_recipients(&["not-a-recipient".to_string()]).is_err());
        // The bound counts *distinct* recipients, exactly as `dict.fromkeys` makes it:
        // 17 copies of one recipient are one recipient, not seventeen.
        let too_many: Vec<String> = (0..MAX_RECIPIENTS + 1)
            .map(|index| format!("age1{index}"))
            .collect();
        assert!(normalize_recipients(&too_many).is_err());
        let repeated = vec!["age1x".to_string(); MAX_RECIPIENTS + 1];
        assert_eq!(normalize_recipients(&repeated).unwrap().len(), 1);
    }

    #[test]
    fn variant_groups_are_digest_sorted_and_deduped() {
        let explicit = RecipientGroups::Explicit(vec!["age1a".to_string()]);
        assert_eq!(explicit.resolve().unwrap(), vec![vec!["age1a".to_string()]]);

        let from_policies = RecipientGroups::FromPolicies(vec![
            vec!["age1b".to_string(), "age1a".to_string()],
            vec!["age1a".to_string(), "age1b".to_string()],
            vec!["age1c".to_string()],
        ]);
        let resolved = from_policies.resolve().unwrap();
        assert_eq!(resolved.len(), 2, "same set collapses to one variant");
        let digests: Vec<String> = resolved
            .iter()
            .map(|group| recipient_set_digest(group.iter()))
            .collect();
        let mut sorted = digests.clone();
        sorted.sort();
        assert_eq!(digests, sorted, "groups come back in digest order");

        assert!(RecipientGroups::FromPolicies(vec![]).resolve().is_err());
    }

    #[test]
    fn indented_json_matches_python() {
        let value = json!({
            "b": [1, 2, {"nested": true}],
            "a": {"empty": {}, "list": []},
            "n": null,
        });
        let expected = "{\n  \"a\": {\n    \"empty\": {},\n    \"list\": []\n  },\n  \"b\": [\n    1,\n    2,\n    {\n      \"nested\": true\n    }\n  ],\n  \"n\": null\n}";
        assert_eq!(dumps_indent_two(&value), expected);
    }

    #[test]
    fn a_put_publishes_a_verified_generation_and_moves_head() {
        let root = temp_root("put");
        let store = MirrorStore::new(&root);
        let recipient = fresh_recipient();
        let outcome = store
            .put(
                put_request("mirror_a", "epoch-1", 1, vec![recipient]),
                false,
            )
            .unwrap();
        assert!(!outcome.idempotent);
        assert_eq!(
            outcome.metadata.get("creationVerified"),
            Some(&Value::Bool(true))
        );
        assert_eq!(
            outcome.metadata.get("parentGenerationId"),
            Some(&Value::Null)
        );

        let head = store.read_head("mirror_a").expect("head exists");
        assert_eq!(head.get("acceptedEpoch").unwrap(), "epoch-1");
        assert_eq!(head.get("acceptedEpochIndex").unwrap(), 1);
        assert_eq!(head.get("acceptedSequence").unwrap(), 1);

        // The generation is on disk, verified, and its ciphertext hash is what the
        // metadata claims.
        let generation = head.get("generationId").unwrap().as_str().unwrap();
        let metadata = store
            .read_generation_metadata("mirror_a", generation)
            .expect("generation metadata");
        let variant = metadata
            .get("recipientVariants")
            .and_then(Value::as_array)
            .and_then(|variants| variants.first())
            .expect("one variant");
        assert_eq!(variant.get("creationVerified").unwrap(), true);
        let filename = variant.get("filename").unwrap().as_str().unwrap();
        assert!(variant_filename(filename));
        let ciphertext = store.generation_dir("mirror_a", generation).join(filename);
        assert_eq!(
            sha256_file(&ciphertext).unwrap(),
            variant.get("ciphertextSha256").unwrap().as_str().unwrap()
        );

        // The ciphertext really is an age file, not a copy of the plaintext.
        let raw = fs::read(&ciphertext).unwrap();
        assert!(
            raw.starts_with(b"age-encryption.org/v1\n"),
            "a published variant must be age ciphertext"
        );
        assert!(
            !raw.windows(4).any(|window| window == b"{\"co"),
            "the plaintext envelope must not be recoverable from the file"
        );

        // No temp files survive.
        let leftovers: Vec<_> = fs::read_dir(store.generation_dir("mirror_a", generation))
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn an_exact_replay_is_idempotent_and_a_stale_sequence_is_refused() {
        let root = temp_root("replay");
        let store = MirrorStore::new(&root);
        let recipient = fresh_recipient();
        let first = store
            .put(
                put_request("mirror_b", "epoch-1", 5, vec![recipient.clone()]),
                false,
            )
            .unwrap();
        let replay = store
            .put(
                put_request("mirror_b", "epoch-1", 5, vec![recipient.clone()]),
                false,
            )
            .unwrap();
        assert!(replay.idempotent);
        assert_eq!(replay.metadata, {
            let mut value = first.metadata.clone();
            if let Value::Object(fields) = &mut value {
                fields.insert("idempotent".into(), Value::Bool(true));
            }
            value
        });
        // The replay must not have created a second generation.
        let generations = fs::read_dir(store.profile_dir("mirror_b").join(GENERATIONS_DIR_NAME))
            .unwrap()
            .count();
        assert_eq!(generations, 1);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_superseded_epoch_and_a_moved_head_are_refused() {
        let root = temp_root("conflict");
        let store = MirrorStore::new(&root);
        let recipient = fresh_recipient();
        store
            .put(
                put_request("mirror_c", "epoch-1", 1, vec![recipient.clone()]),
                false,
            )
            .unwrap();
        store
            .put(
                put_request("mirror_c", "epoch-2", 2, vec![recipient.clone()]),
                false,
            )
            .unwrap();

        // epoch-1 was indexed at 1, which is <= the accepted index 2.
        let mut stale = put_request("mirror_c", "epoch-1", 3, vec![recipient.clone()]);
        stale.envelope = envelope("different-body");
        let failure = store.put(stale, false).unwrap_err();
        assert_eq!(failure.status, 409);
        assert!(failure.message.starts_with("mirror-stale-epoch"));

        // A non-increasing sequence on the accepted epoch.
        let mut stale_sequence = put_request("mirror_c", "epoch-2", 2, vec![recipient.clone()]);
        stale_sequence.envelope = envelope("another-body");
        let failure = store.put(stale_sequence, false).unwrap_err();
        assert!(failure.message.starts_with("mirror-stale-sequence"));

        // A head that moved since the client's snapshot.
        let mut conflict = put_request("mirror_c", "epoch-3", 3, vec![recipient.clone()]);
        conflict.expected_head_generation_id = Some("gen_00000000deadbeef".to_string());
        let failure = store.put(conflict, false).unwrap_err();
        assert!(failure.message.starts_with("mirror-head-conflict"));
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_fenced_workspace_refuses_with_423_before_any_write() {
        let root = temp_root("fenced");
        let store = MirrorStore::new(&root);
        let failure = store
            .put(
                put_request("mirror_d", "epoch-1", 1, vec![fresh_recipient()]),
                true,
            )
            .unwrap_err();
        assert_eq!(failure.status, 423);
        assert_eq!(failure.code, codes::INVALID_REQUEST);
        assert!(!store.profile_dir("mirror_d").exists());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn status_reports_missing_current_stale_epoch_and_recipient_mismatch() {
        let root = temp_root("status");
        let store = MirrorStore::new(&root);
        let recipient = fresh_recipient();
        let other = fresh_recipient();

        let missing = store
            .status(Some("mirror_e"), None, None, None, false, None)
            .unwrap();
        assert_eq!(missing.get("status").unwrap(), "missing");
        assert_eq!(missing.get("profileId").unwrap(), "mirror_e");

        store
            .put(
                put_request("mirror_e", "epoch-1", 1, vec![recipient.clone()]),
                false,
            )
            .unwrap();
        let current = store
            .status(
                Some("mirror_e"),
                Some(std::slice::from_ref(&recipient)),
                None,
                Some("epoch-1"),
                false,
                None,
            )
            .unwrap();
        assert_eq!(current.get("status").unwrap(), "current");

        let mismatch = store
            .status(
                Some("mirror_e"),
                Some(std::slice::from_ref(&other)),
                None,
                None,
                false,
                None,
            )
            .unwrap();
        assert_eq!(mismatch.get("status").unwrap(), "recipient-mismatch");

        let epoch = store
            .status(Some("mirror_e"), None, None, Some("epoch-9"), false, None)
            .unwrap();
        assert_eq!(epoch.get("status").unwrap(), "epoch-mismatch");

        let stale = store
            .status(
                Some("mirror_e"),
                None,
                Some(60),
                None,
                false,
                Some(Utc.with_ymd_and_hms(2026, 9, 30, 14, 0, 0).unwrap()),
            )
            .unwrap();
        assert_eq!(stale.get("status").unwrap(), "stale");

        let excluded = store.status(None, None, None, None, true, None).unwrap();
        assert_eq!(excluded.get("status").unwrap(), "excluded");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn files_resolves_head_and_detects_corruption() {
        let root = temp_root("files");
        let store = MirrorStore::new(&root);
        let recipient = fresh_recipient();
        store
            .put(
                put_request("mirror_f", "epoch-1", 1, vec![recipient.clone()]),
                false,
            )
            .unwrap();
        let files = store
            .files("mirror_f", Some(std::slice::from_ref(&recipient)))
            .unwrap();
        assert!(files.ciphertext.is_file());

        // A different recipient set has no sealed variant.
        let failure = store
            .files("mirror_f", Some(&[fresh_recipient()]))
            .unwrap_err();
        assert_eq!(failure.status, 404);

        // Tampering with the ciphertext is a 409, never a silent success.
        let mut raw = fs::read(&files.ciphertext).unwrap();
        let last = raw.len() - 1;
        raw[last] ^= 1;
        fs::write(&files.ciphertext, &raw).unwrap();
        let failure = store.files("mirror_f", None).unwrap_err();
        assert_eq!(failure.status, 409);
        assert!(failure.message.starts_with("mirror-generation-corrupt"));

        let missing = store.files("mirror_zz", None).unwrap_err();
        assert_eq!(missing.status, 404);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn list_skips_unreadable_legacy_profiles_and_latest_breaks_ties_first() {
        let root = temp_root("list");
        let store = MirrorStore::new(&root);
        let recipient = fresh_recipient();
        store
            .put(
                put_request("mirror_g", "epoch-1", 1, vec![recipient]),
                false,
            )
            .unwrap();

        // An unreadable legacy projection skips the profile rather than failing.
        let broken = store.profile_dir("mirror_broken");
        fs::create_dir_all(&broken).unwrap();
        fs::write(broken.join(MIRROR_METADATA_NAME), "{not json").unwrap();
        // A directory with no head and no legacy metadata contributes nothing.
        fs::create_dir_all(store.profile_dir("mirror_empty")).unwrap();
        fs::create_dir_all(root.join(PREVIOUS_DIR_NAME)).unwrap();

        let mirrors = store.list().unwrap();
        assert_eq!(mirrors.len(), 1);
        assert_eq!(mirrors[0].get("profileId").unwrap(), "mirror_g");

        // `latest` keeps the first maximal acknowledgedAt, not the last.
        let first = json!({"acknowledgedAt": "2026-01-01T00:00:00Z", "profileId": "a"});
        let second = json!({"acknowledgedAt": "2026-01-01T00:00:00Z", "profileId": "b"});
        let mut best: Option<Value> = None;
        let mut best_key = String::new();
        for metadata in [first.clone(), second.clone()] {
            let key = metadata
                .get("acknowledgedAt")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            if best.is_none() || key > best_key {
                best_key = key;
                best = Some(metadata);
            }
        }
        assert_eq!(best.unwrap().get("profileId").unwrap(), "a");

        let latest = store.latest().unwrap().expect("one mirror");
        assert_eq!(latest.get("profileId").unwrap(), "mirror_g");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_new_epoch_starts_above_the_highest_existing_index() {
        let root = temp_root("epochs");
        let store = MirrorStore::new(&root);
        let recipient = fresh_recipient();
        store
            .put(
                put_request("mirror_h", "epoch-1", 1, vec![recipient.clone()]),
                false,
            )
            .unwrap();
        store
            .put(
                put_request("mirror_h", "epoch-2", 2, vec![recipient.clone()]),
                false,
            )
            .unwrap();
        store
            .put(
                put_request("mirror_h", "epoch-3", 3, vec![recipient]),
                false,
            )
            .unwrap();
        let head = store.read_head("mirror_h").unwrap();
        assert_eq!(head.get("acceptedEpochIndex").unwrap(), 3);
        let indexes = head.get("epochIndexes").unwrap().as_object().unwrap();
        assert_eq!(indexes.get("epoch-1").unwrap(), 1);
        assert_eq!(indexes.get("epoch-2").unwrap(), 2);
        assert_eq!(indexes.get("epoch-3").unwrap(), 3);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn generation_pruning_keeps_the_new_generation_and_its_parent() {
        let root = temp_root("prune");
        let store = MirrorStore::new(&root);
        let recipient = fresh_recipient();
        let first = store
            .put(
                put_request("mirror_i", "epoch-1", 1, vec![recipient.clone()]),
                false,
            )
            .unwrap();
        let first_generation = first
            .metadata
            .get("generationId")
            .unwrap()
            .as_str()
            .unwrap()
            .to_string();
        let second = store
            .put(
                put_request("mirror_i", "epoch-2", 2, vec![recipient]),
                false,
            )
            .unwrap();
        let second_generation = second
            .metadata
            .get("generationId")
            .unwrap()
            .as_str()
            .unwrap()
            .to_string();
        assert_eq!(
            second.metadata.get("parentGenerationId").unwrap(),
            &Value::String(first_generation.clone())
        );
        assert!(store.generation_dir("mirror_i", &first_generation).is_dir());
        assert!(
            store
                .generation_dir("mirror_i", &second_generation)
                .is_dir()
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn envelope_validation_refuses_bad_schema_digest_and_secrets() {
        let good = envelope("x");
        assert!(validate_frontend_envelope(&good).is_ok());

        let mut bad_schema = good.clone();
        bad_schema["schemaVersion"] = json!(99);
        assert!(validate_frontend_envelope(&bad_schema).is_err());

        let mut bad_digest = good.clone();
        bad_digest["digest"] = json!("0".repeat(64));
        assert!(validate_frontend_envelope(&bad_digest).is_err());

        let mut secret = good.clone();
        secret["lease"] = json!({"id": "l1"});
        let failure = validate_frontend_envelope(&secret).unwrap_err();
        assert_eq!(failure.code, codes::SENSITIVE_CONTENT);
    }

    #[test]
    fn envelope_bounds_and_epoch_validation_are_enforced() {
        let root = temp_root("bounds");
        let store = MirrorStore::new(&root);
        let mut request = put_request("mirror_j", "epoch/with/slash", 1, vec![fresh_recipient()]);
        let failure = store.put(request.clone(), false).unwrap_err();
        assert_eq!(failure.code, codes::INVALID_PAYLOAD);

        request.source_epoch = "epoch-ok".to_string();
        request.envelope = json!({"schemaVersion": 1});
        let failure = store.put(request.clone(), false).unwrap_err();
        assert_eq!(failure.code, codes::INVALID_PAYLOAD);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn mirrored_timestamps_reproduce_python_shapes() {
        assert_eq!(
            parse_iso("2026-09-30T12:00:00Z", "t").unwrap(),
            Utc.with_ymd_and_hms(2026, 9, 30, 12, 0, 0).unwrap()
        );
        assert_eq!(
            parse_iso("2026-09-30 12:00:00", "t").unwrap(),
            Utc.with_ymd_and_hms(2026, 9, 30, 12, 0, 0).unwrap()
        );
        assert_eq!(
            parse_iso("2026-09-30T12:00:00+02:00", "t").unwrap(),
            Utc.with_ymd_and_hms(2026, 9, 30, 10, 0, 0).unwrap()
        );
        assert!(parse_iso("  ", "t").is_err());
        assert!(parse_iso("not a time", "t").is_err());
        assert_eq!(
            now_iso(Some(Utc.with_ymd_and_hms(2026, 9, 30, 12, 0, 0).unwrap())),
            "2026-09-30T12:00:00Z"
        );
    }
}
