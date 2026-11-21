//! Native Age decryption, archive verification, and restore-drill engine.
//!
//! Mirrors `deepseek_infra.infra.workspace.backup_scrub` (`verify_unlock_drill`,
//! `_unlock_object_set`), `backup_object_set` (`verify_control_metadata`,
//! `extract_component_archive`), `backup_pack` (`parse_pack_index`,
//! `load_pack_index`), and `backups` (`_safe_extract_and_verify`,
//! `_verify_manifest_tree`, `_unlock_sealed_frontend`).

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use zip::ZipArchive;

use crate::app_error::{AppError, codes};
use crate::backup_mirror::{now_iso, sha256_file, validate_frontend_envelope};

pub const BACKUP_SCHEMA: &str = "deepseek-workspace-backup.v1";
pub const PACKAGE_PURPOSE: &str = "restorable-backup";
pub const MAX_EXPANDED_BYTES: u64 = 5_000_000_000;
pub const MAX_ENTRIES: usize = 10_000;
pub const MAX_PATH_LENGTH: usize = 240;
pub const MAX_JSON_DEPTH: usize = 64;
pub const MAX_COMPRESSION_RATIO: f64 = 100.0;
pub const PACK_ALIGNMENT: u64 = 8;
pub const PACK_INDEX_PATH: &str = "payload/packs/index.json";
pub const PACK_INDEX_SCHEMA_VERSION: i64 = 2;

const METADATA_ENTRIES: [&str; 6] = [
    "manifest.json",
    "checksums.sha256",
    "delta/operations.json",
    "payload/packs/index.json",
    "payload-index.json",
    "component-map.json",
];

fn invalid_payload(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    }
}

fn conflict_error(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INVALID_REQUEST,
        status: 409,
    }
}

fn not_found_error(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::NOT_FOUND,
        status: 404,
    }
}

fn too_large_error(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::UPLOAD_TOO_LARGE,
        status: 413,
    }
}

fn internal_error(message: impl Into<String>) -> AppError {
    AppError {
        message: message.into(),
        code: codes::INTERNAL,
        status: 500,
    }
}

pub fn is_plain_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Best-effort zero-overwrite and removal for staged plaintext artifacts.
pub fn scrub_plaintext_file(path: &Path) {
    if path.is_file() {
        if let Ok(meta) = fs::metadata(path) {
            let size = meta.len();
            if let Ok(mut handle) = OpenOptions::new().read(true).write(true).open(path) {
                let _ = handle.seek(SeekFrom::Start(0));
                let zeros = [0_u8; 64 * 1024];
                let mut remaining = size;
                while remaining > 0 {
                    let chunk = remaining.min(zeros.len() as u64) as usize;
                    if handle.write_all(&zeros[..chunk]).is_err() {
                        break;
                    }
                    remaining -= chunk as u64;
                }
                let _ = handle.flush();
                let _ = handle.sync_all();
            }
        }
    }
    let _ = fs::remove_file(path);
}

struct StagedDrillGuard {
    dir: PathBuf,
}

impl StagedDrillGuard {
    fn new(dir: PathBuf) -> Self {
        Self { dir }
    }

    fn cleanup(&self) {
        if let Ok(entries) = fs::read_dir(&self.dir) {
            for entry in entries.flatten() {
                let name = entry.file_name();
                let text = name.to_string_lossy();
                if text == "unlocked.dsibackup"
                    || (text.starts_with("unlocked-") && text.ends_with(".zip"))
                {
                    scrub_plaintext_file(&entry.path());
                }
            }
        }
        let verified_dir = self.dir.join("verified").join("frontend");
        scrub_plaintext_file(&verified_dir.join("state.json"));
        if let Ok(entries) = fs::read_dir(&verified_dir) {
            for entry in entries.flatten() {
                let text = entry.file_name().to_string_lossy().to_string();
                if text.starts_with(".state.json.") && text.ends_with(".tmp") {
                    scrub_plaintext_file(&entry.path());
                }
            }
        }
        let _ = fs::remove_dir_all(&self.dir);
    }
}

impl Drop for StagedDrillGuard {
    fn drop(&mut self) {
        self.cleanup();
    }
}

/// Inspect an Age ciphertext header within `backup_crypto`'s bounded header budget.
pub fn inspect_age_file_header(path: &Path) -> Result<bool, String> {
    inspect_age_file_protection(path).map(|_| true)
}

/// Inspect an Age ciphertext header and return whether it uses passphrase (`scrypt`) encryption.
pub fn inspect_age_file_protection(path: &Path) -> Result<bool, String> {
    let file = File::open(path).map_err(|err| err.to_string())?;
    backup_crypto::inspect_age_header(BufReader::new(file)).map_err(|err| err.to_string())
}

/// Generate a fresh X25519 identity pair `(secret_key, public_recipient)`.
pub fn ephemeral_identity() -> (String, String) {
    backup_crypto::ephemeral_identity()
}

/// Derive X25519 public recipients from an `AGE-SECRET-KEY-` identity string.
pub fn derive_recipients(secret: &str) -> Result<Vec<String>, AppError> {
    backup_crypto::derive_recipients(secret)
        .map_err(|err| invalid_payload(format!("Recovery key is invalid: {err}")))
}

/// Encrypt `plaintext` to `recipients` using `backup_crypto`.
pub fn encrypt_bytes_to_recipients(
    plaintext: &[u8],
    recipients: &[String],
) -> Result<Vec<u8>, AppError> {
    let mut out = Vec::new();
    backup_crypto::encrypt_recipients(plaintext, &mut out, recipients)
        .map_err(|err| internal_error(format!("Age encryption failed: {err}")))?;
    Ok(out)
}

fn decrypt_file_inner(
    src: &Path,
    dst: &Path,
    use_passphrase: bool,
    secret: &str,
) -> Result<(), AppError> {
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent).map_err(|err| internal_error(err.to_string()))?;
    }
    let input =
        File::open(src).map_err(|err| invalid_payload(format!("Backup file is missing: {err}")))?;
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let output = options
        .open(dst)
        .map_err(|err| internal_error(format!("Failed to stage decrypted payload: {err}")))?;
    let mut writer = BufWriter::new(output);
    let decrypted = if use_passphrase {
        backup_crypto::decrypt_passphrase(BufReader::new(input), &mut writer, secret.to_string())
    } else {
        backup_crypto::decrypt_identity(BufReader::new(input), &mut writer, secret.to_string())
    };
    if let Err(err) = decrypted {
        drop(writer);
        scrub_plaintext_file(dst);
        return Err(invalid_payload(format!("Backup decryption failed: {err}")));
    }
    if let Err(err) = writer.flush().and_then(|()| {
        writer
            .into_inner()
            .map_err(std::io::Error::other)?
            .sync_all()
    }) {
        scrub_plaintext_file(dst);
        return Err(internal_error(format!(
            "Failed to persist decrypted payload: {err}"
        )));
    }
    Ok(())
}

/// Decrypt `src` to `dst` using the in-process `backup_crypto` X25519 Age backend.
pub fn decrypt_file_native(src: &Path, dst: &Path, identity_secret: &str) -> Result<(), AppError> {
    let trimmed = identity_secret.trim();
    if !trimmed.starts_with("AGE-SECRET-KEY-") {
        return Err(invalid_payload(
            "Recovery unlock requires an AGE-SECRET-KEY- identity",
        ));
    }
    decrypt_file_inner(src, dst, false, trimmed)
}

/// Decrypt `src` to `dst` using the in-process `backup_crypto` passphrase (`scrypt`) backend.
pub fn decrypt_passphrase_file_native(
    src: &Path,
    dst: &Path,
    passphrase: &str,
) -> Result<(), AppError> {
    if passphrase.is_empty() || passphrase.as_bytes().contains(&0) {
        return Err(invalid_payload("Backup passphrase is invalid"));
    }
    decrypt_file_inner(src, dst, true, passphrase)
}

struct PlaintextTempGuard(PathBuf);

impl Drop for PlaintextTempGuard {
    fn drop(&mut self) {
        scrub_plaintext_file(&self.0);
    }
}

fn collect_sorted_staging_files(staging: &Path) -> Result<Vec<(String, PathBuf)>, AppError> {
    fn walk(root: &Path, dir: &Path, out: &mut Vec<(String, PathBuf)>) -> Result<(), AppError> {
        let entries = fs::read_dir(dir).map_err(|err| internal_error(err.to_string()))?;
        for entry in entries {
            let entry = entry.map_err(|err| internal_error(err.to_string()))?;
            let path = entry.path();
            let meta =
                fs::symlink_metadata(&path).map_err(|err| internal_error(err.to_string()))?;
            if meta.file_type().is_symlink() {
                continue;
            }
            if meta.is_dir() {
                walk(root, &path, out)?;
            } else if meta.is_file() {
                let rel = path
                    .strip_prefix(root)
                    .map_err(|err| internal_error(err.to_string()))?
                    .to_string_lossy()
                    .replace('\\', "/");
                out.push((rel, path));
            }
        }
        Ok(())
    }
    let mut files = Vec::new();
    walk(staging, staging, &mut files)?;
    files.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(files)
}

fn write_zip_to_path(
    staging: &Path,
    destination: &Path,
    private_mode: bool,
) -> Result<(), AppError> {
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent).map_err(|err| internal_error(err.to_string()))?;
    }
    let files = collect_sorted_staging_files(staging)?;
    let mut open_opts = OpenOptions::new();
    open_opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    if private_mode {
        use std::os::unix::fs::OpenOptionsExt;
        open_opts.mode(0o600);
    }
    #[cfg(not(unix))]
    let _ = private_mode;

    let file = open_opts
        .open(destination)
        .map_err(|err| internal_error(format!("Failed to create backup archive: {err}")))?;
    let mut writer = zip::ZipWriter::new(BufWriter::new(file));
    let mtime = zip::DateTime::from_date_and_time(1980, 1, 1, 0, 0, 0)
        .expect("1980-01-01 DOS timestamp is valid");
    let options = zip::write::FileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated)
        .compression_level(Some(6))
        .unix_permissions(0o100600)
        .last_modified_time(mtime);
    for (rel, path) in files {
        writer
            .start_file(&rel, options)
            .map_err(|err| internal_error(format!("Failed to write zip header: {err}")))?;
        let mut src = BufReader::new(
            File::open(&path)
                .map_err(|err| internal_error(format!("Failed to read staged file: {err}")))?,
        );
        std::io::copy(&mut src, &mut writer).map_err(|err| {
            internal_error(format!("Failed to stream staged file into zip: {err}"))
        })?;
    }
    let buffered = writer
        .finish()
        .map_err(|err| internal_error(format!("Failed to finalize zip archive: {err}")))?;
    let mut inner = buffered
        .into_inner()
        .map_err(|err| internal_error(format!("Failed to flush zip archive: {err}")))?;
    inner
        .flush()
        .and_then(|()| inner.sync_all())
        .map_err(|err| internal_error(format!("Failed to sync backup archive: {err}")))?;
    Ok(())
}

/// Write an uncompressed/deflated `.dsibackup` ZIP archive atomically from `staging` to `target`.
pub fn write_zip_tree(staging: &Path, target: &Path) -> Result<(), AppError> {
    let parent = target
        .parent()
        .ok_or_else(|| internal_error("Backup target path has no parent"))?;
    fs::create_dir_all(parent).map_err(|err| internal_error(err.to_string()))?;
    let name = target
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("backup.dsibackup");
    let temporary = parent.join(format!(".{name}.{}.tmp", std::process::id()));
    let write_res = write_zip_to_path(staging, &temporary, false);
    if write_res.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    write_res?;
    if let Err(err) = fs::rename(&temporary, target) {
        let _ = fs::remove_file(&temporary);
        return Err(internal_error(format!(
            "Failed to publish backup archive: {err}"
        )));
    }
    crate::backup_mirror::fsync_dir(parent);
    Ok(())
}

/// Package `staging` into a `.dsibackup` (plain) or `.dsibackup.age` (encrypted) archive atomically.
pub fn encrypt_tree_zip_native(
    staging: &Path,
    target: &Path,
    protection_mode: &str,
    secret: Option<&str>,
    recipients: &[String],
) -> Result<(), AppError> {
    encrypt_tree_zip_native_with_plain_metrics(staging, target, protection_mode, secret, recipients)
        .map(|_| ())
}

/// Encrypt `staging` as a deterministic zip into `target`, returning the plaintext `(size, sha256)`.
pub fn encrypt_tree_zip_native_with_plain_metrics(
    staging: &Path,
    target: &Path,
    protection_mode: &str,
    secret: Option<&str>,
    recipients: &[String],
) -> Result<(u64, String), AppError> {
    if protection_mode == "none" {
        write_zip_tree(staging, target)?;
        let size = fs::metadata(target)
            .map(|m| m.len())
            .map_err(|err| internal_error(err.to_string()))?;
        let sha = sha256_file(target).map_err(|err| internal_error(err.to_string()))?;
        return Ok((size, sha));
    }
    let parent = target
        .parent()
        .ok_or_else(|| internal_error("Backup target path has no parent"))?;
    fs::create_dir_all(parent).map_err(|err| internal_error(err.to_string()))?;
    let name = target
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("backup.dsibackup.age");
    let pid = std::process::id();
    let plain_tmp = parent.join(format!(".{name}.{pid}.plain.zip.tmp"));
    let _plain_guard = PlaintextTempGuard(plain_tmp.clone());
    write_zip_to_path(staging, &plain_tmp, true)?;
    let plain_size = fs::metadata(&plain_tmp)
        .map(|m| m.len())
        .map_err(|err| internal_error(err.to_string()))?;
    let plain_sha = sha256_file(&plain_tmp).map_err(|err| internal_error(err.to_string()))?;

    let cipher_tmp = parent.join(format!(".{name}.{pid}.age.tmp"));
    let encrypt_result = (|| -> Result<(), AppError> {
        let input = BufReader::new(
            File::open(&plain_tmp)
                .map_err(|err| internal_error(format!("Failed to open staged zip: {err}")))?,
        );
        let output = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&cipher_tmp)
            .map_err(|err| internal_error(format!("Failed to create encrypted backup: {err}")))?;
        let mut buffered_out = BufWriter::new(output);
        match protection_mode {
            "passphrase" => {
                let passphrase = secret
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| conflict_error("Backup secret is required or expired"))?;
                backup_crypto::encrypt_passphrase(input, &mut buffered_out, passphrase.to_string())
                    .map_err(|err| internal_error(format!("Backup encryption failed: {err}")))?;
            }
            "age-recipient" => {
                backup_crypto::encrypt_recipients(input, &mut buffered_out, recipients)
                    .map_err(|err| internal_error(format!("Backup encryption failed: {err}")))?;
            }
            _ => {
                return Err(invalid_payload("Unsupported backup protection mode"));
            }
        }
        let mut inner = buffered_out
            .into_inner()
            .map_err(|err| internal_error(format!("Failed to flush encrypted backup: {err}")))?;
        inner
            .flush()
            .and_then(|()| inner.sync_all())
            .map_err(|err| internal_error(format!("Failed to sync encrypted backup: {err}")))?;
        Ok(())
    })();
    if let Err(err) = encrypt_result {
        let _ = fs::remove_file(&cipher_tmp);
        return Err(err);
    }
    if let Err(err) = fs::rename(&cipher_tmp, target) {
        let _ = fs::remove_file(&cipher_tmp);
        return Err(internal_error(format!(
            "Failed to publish encrypted backup: {err}"
        )));
    }
    crate::backup_mirror::fsync_dir(parent);
    Ok((plain_size, plain_sha))
}

/// Validate a POSIX relative archive path without traversal, drive letters, or control bytes.
pub fn validate_archive_path(raw: &str) -> Result<String, AppError> {
    if raw.is_empty()
        || raw.contains('\\')
        || raw.contains('\0')
        || raw.chars().count() > MAX_PATH_LENGTH
    {
        return Err(invalid_payload("Backup contains an invalid archive path"));
    }
    let normalized = raw.trim_end_matches('/');
    if normalized.is_empty() || normalized.starts_with('/') {
        return Err(invalid_payload("Backup contains an unsafe archive path"));
    }
    let bytes = normalized.as_bytes();
    if bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic() {
        return Err(invalid_payload("Backup contains an unsafe archive path"));
    }
    if normalized
        .split('/')
        .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err(invalid_payload("Backup contains path traversal segments"));
    }
    Ok(normalized.to_string())
}

#[derive(Debug, Clone)]
struct ArchiveEntryMeta {
    index: usize,
    normalized: String,
    is_dir: bool,
}

fn validate_archive_infos<R: Read + Seek>(
    archive: &mut ZipArchive<R>,
) -> Result<Vec<ArchiveEntryMeta>, AppError> {
    let count = archive.len();
    if count > MAX_ENTRIES {
        return Err(too_large_error("Backup contains too many files"));
    }
    let mut infos = Vec::with_capacity(count);
    let mut seen = BTreeSet::new();
    let mut declared_total = 0_u64;
    for index in 0..count {
        let file = archive
            .by_index(index)
            .map_err(|_| invalid_payload("Backup archive is unreadable"))?;
        let normalized = validate_archive_path(file.name())?;
        let folded = normalized.to_lowercase();
        if !seen.insert(folded) {
            return Err(invalid_payload(
                "Backup contains duplicate or case-colliding paths",
            ));
        }
        if let Some(mode) = file.unix_mode() {
            let file_type = mode & 0o170000;
            let is_symlink = file_type == 0o120000;
            let is_reg_or_dir = file_type == 0o100000 || file_type == 0o040000;
            if is_symlink || (file_type != 0 && !is_reg_or_dir) {
                return Err(invalid_payload("Backup contains a link or special file"));
            }
        }
        let file_size = file.size();
        let compress_size = file.compressed_size();
        if file_size > MAX_EXPANDED_BYTES {
            return Err(too_large_error("Backup entry is too large"));
        }
        declared_total = declared_total.saturating_add(file_size);
        if declared_total > MAX_EXPANDED_BYTES {
            return Err(too_large_error("Expanded backup is too large"));
        }
        if compress_size == 0 && file_size > 0 {
            return Err(invalid_payload("Suspicious compression ratio"));
        }
        if compress_size > 0 && (file_size as f64) / (compress_size as f64) > MAX_COMPRESSION_RATIO
        {
            return Err(invalid_payload("Suspicious compression ratio"));
        }
        infos.push(ArchiveEntryMeta {
            index,
            normalized,
            is_dir: file.is_dir(),
        });
    }
    Ok(infos)
}

fn target_from_relative(root: &Path, relative: &str) -> PathBuf {
    relative
        .split('/')
        .fold(root.to_path_buf(), |path, segment| path.join(segment))
}

fn extract_entry_bounded(
    source: &mut impl Read,
    target: &Path,
    actual_total: &mut u64,
) -> Result<(), AppError> {
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|err| internal_error(err.to_string()))?;
    }
    let mut output = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(target)
        .map_err(|err| internal_error(err.to_string()))?;
    let mut buffer = [0_u8; 64 * 1024];
    let mut actual_entry = 0_u64;
    loop {
        let count = source
            .read(&mut buffer)
            .map_err(|_| invalid_payload("Backup archive entry is corrupt"))?;
        if count == 0 {
            break;
        }
        actual_entry = actual_entry.saturating_add(count as u64);
        *actual_total = actual_total.saturating_add(count as u64);
        if actual_entry > MAX_EXPANDED_BYTES || *actual_total > MAX_EXPANDED_BYTES {
            return Err(too_large_error("Expanded backup is too large"));
        }
        output
            .write_all(&buffer[..count])
            .map_err(|err| internal_error(err.to_string()))?;
    }
    output
        .flush()
        .map_err(|err| internal_error(err.to_string()))?;
    Ok(())
}

fn reset_dir(path: &Path) -> Result<(), AppError> {
    if path.exists() {
        fs::remove_dir_all(path).map_err(|err| internal_error(err.to_string()))?;
    }
    fs::create_dir_all(path).map_err(|err| internal_error(err.to_string()))
}

fn read_json_object(path: &Path, message: &str) -> Result<Value, AppError> {
    let raw = fs::read_to_string(path).map_err(|_| invalid_payload(message))?;
    let value: Value = serde_json::from_str(&raw).map_err(|_| invalid_payload(message))?;
    if !value.is_object() {
        return Err(invalid_payload(message));
    }
    Ok(value)
}

fn check_json_depth(value: &Value, depth: usize) -> Result<(), AppError> {
    if depth > MAX_JSON_DEPTH {
        return Err(invalid_payload("Backup JSON is too deeply nested"));
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

fn strict_non_negative_int(value: Option<&Value>) -> Option<u64> {
    match value {
        Some(Value::Number(number)) => number.as_u64(),
        _ => None,
    }
}

fn collect_relative_files(
    root: &Path,
    current: &Path,
    skip_top_control_files: bool,
    out: &mut BTreeSet<String>,
) -> Result<(), AppError> {
    let entries = fs::read_dir(current).map_err(|err| internal_error(err.to_string()))?;
    for entry in entries {
        let entry = entry.map_err(|err| internal_error(err.to_string()))?;
        let path = entry.path();
        let meta = fs::symlink_metadata(&path).map_err(|err| internal_error(err.to_string()))?;
        if meta.file_type().is_symlink() {
            return Err(invalid_payload("Extracted backup tree contains a symlink"));
        }
        if meta.is_dir() {
            collect_relative_files(root, &path, skip_top_control_files, out)?;
        } else if meta.is_file() {
            let rel = path
                .strip_prefix(root)
                .map_err(|err| internal_error(err.to_string()))?;
            let posix = rel
                .iter()
                .map(|part| part.to_string_lossy().to_string())
                .collect::<Vec<_>>()
                .join("/");
            if skip_top_control_files && (posix == "manifest.json" || posix == "checksums.sha256") {
                continue;
            }
            out.insert(posix);
        }
    }
    Ok(())
}

/// Structurally validate all entries and extract only control metadata files.
pub fn extract_archive_metadata(
    archive_path: &Path,
    destination: &Path,
) -> Result<Value, AppError> {
    reset_dir(destination)?;
    let file =
        File::open(archive_path).map_err(|_| invalid_payload("Backup archive is unreadable"))?;
    let mut archive = ZipArchive::new(BufReader::new(file))
        .map_err(|_| invalid_payload("Backup archive is unreadable"))?;
    let infos = validate_archive_infos(&mut archive)?;
    let mut actual_total = 0_u64;
    for meta in infos {
        if meta.is_dir || !METADATA_ENTRIES.contains(&meta.normalized.as_str()) {
            continue;
        }
        let target = target_from_relative(destination, &meta.normalized);
        let mut zip_file = archive
            .by_index(meta.index)
            .map_err(|_| invalid_payload("Backup archive is unreadable"))?;
        extract_entry_bounded(&mut zip_file, &target, &mut actual_total)?;
    }
    if !destination.join("manifest.json").is_file()
        || !destination.join("checksums.sha256").is_file()
    {
        return Err(invalid_payload("Backup manifest is missing"));
    }
    read_json_object(
        &destination.join("manifest.json"),
        "Backup manifest is invalid",
    )
}

/// Verify extracted object-set Control metadata against `checksums.sha256`.
pub fn verify_control_metadata(destination: &Path) -> Result<(), AppError> {
    let checksum_path = destination.join("checksums.sha256");
    if !checksum_path.is_file() {
        return Err(invalid_payload(
            "object-set Control checksum map is missing",
        ));
    }
    let raw = fs::read_to_string(&checksum_path)
        .map_err(|_| invalid_payload("object-set Control checksum map is invalid"))?;
    let mut expected = BTreeMap::<String, String>::new();
    for line in raw.lines() {
        let Some((digest, raw_path)) = line.split_once("  ") else {
            return Err(invalid_payload(
                "object-set Control checksum map is invalid",
            ));
        };
        let relative = validate_archive_path(raw_path)
            .map_err(|_| invalid_payload("object-set Control checksum map is invalid"))?;
        if relative == "checksums.sha256"
            || expected.contains_key(&relative)
            || !is_plain_sha256(digest)
        {
            return Err(invalid_payload(
                "object-set Control checksum map is invalid",
            ));
        }
        expected.insert(relative, digest.to_string());
    }
    let mut actual = BTreeSet::new();
    collect_relative_files(destination, destination, false, &mut actual)?;
    actual.remove("checksums.sha256");
    let expected_keys: BTreeSet<String> = expected.keys().cloned().collect();
    if actual != expected_keys {
        return Err(invalid_payload("object-set Control inventory mismatch"));
    }
    for (relative, digest) in expected {
        let path = target_from_relative(destination, &relative);
        let actual_digest = sha256_file(&path).map_err(|err| internal_error(err.to_string()))?;
        if actual_digest != digest {
            return Err(invalid_payload("object-set Control checksum mismatch"));
        }
    }
    Ok(())
}

/// Extract one verified payload component archive into `destination` without path overlap.
pub fn extract_component_archive(
    archive_path: &Path,
    destination: &Path,
    expected_paths: &[String],
) -> Result<(), AppError> {
    let mut expected = BTreeSet::new();
    for path in expected_paths {
        expected.insert(validate_archive_path(path)?);
    }
    let file =
        File::open(archive_path).map_err(|_| invalid_payload("Backup archive is unreadable"))?;
    let mut archive = ZipArchive::new(BufReader::new(file))
        .map_err(|_| invalid_payload("Backup archive is unreadable"))?;
    let infos = validate_archive_infos(&mut archive)?;
    let mut names = BTreeSet::new();
    let mut file_infos = Vec::new();
    for meta in infos {
        if meta.is_dir {
            continue;
        }
        if !names.insert(meta.normalized.clone()) {
            return Err(invalid_payload("object-set component inventory mismatch"));
        }
        file_infos.push(meta);
    }
    if names != expected {
        return Err(invalid_payload("object-set component inventory mismatch"));
    }
    let mut actual_total = 0_u64;
    for meta in file_infos {
        let target = target_from_relative(destination, &meta.normalized);
        if target.exists() {
            return Err(invalid_payload("object-set components overlap"));
        }
        let mut zip_file = archive
            .by_index(meta.index)
            .map_err(|_| invalid_payload("Backup archive is unreadable"))?;
        extract_entry_bounded(&mut zip_file, &target, &mut actual_total)?;
    }
    Ok(())
}

/// Safely extract and verify a legacy `.dsibackup` ZIP archive.
pub fn safe_extract_and_verify(archive_path: &Path, destination: &Path) -> Result<Value, AppError> {
    reset_dir(destination)?;
    let file =
        File::open(archive_path).map_err(|_| invalid_payload("Backup archive is unreadable"))?;
    let mut archive = ZipArchive::new(BufReader::new(file))
        .map_err(|_| invalid_payload("Backup archive is unreadable"))?;
    let infos = validate_archive_infos(&mut archive)?;
    let mut actual_total = 0_u64;
    for meta in infos {
        let target = target_from_relative(destination, &meta.normalized);
        if meta.is_dir {
            fs::create_dir_all(&target).map_err(|err| internal_error(err.to_string()))?;
            continue;
        }
        let mut zip_file = archive
            .by_index(meta.index)
            .map_err(|_| invalid_payload("Backup archive is unreadable"))?;
        extract_entry_bounded(&mut zip_file, &target, &mut actual_total)?;
    }
    if !destination.join("manifest.json").is_file()
        || !destination.join("checksums.sha256").is_file()
    {
        return Err(invalid_payload("Backup manifest is missing"));
    }
    verify_manifest_tree(destination)
}

/// Selection of archive payload paths for projected ZIP extraction.
pub struct SelectedArchiveFilter<'a> {
    pub needed_full: &'a BTreeSet<String>,
    pub needed_packs: &'a BTreeSet<String>,
    pub needed_standalone: &'a BTreeSet<String>,
}

fn declared_archive_digests(manifest: &Value) -> BTreeMap<String, String> {
    let mut digests = BTreeMap::new();
    for key in ["files", "deltaFiles"] {
        let Some(entries) = manifest.get(key).and_then(Value::as_array) else {
            continue;
        };
        for entry in entries {
            let Some(path) = entry.get("path").and_then(Value::as_str) else {
                continue;
            };
            let Some(digest) = entry.get("sha256").and_then(Value::as_str) else {
                continue;
            };
            if !path.is_empty() && !digest.is_empty() {
                digests.insert(path.to_string(), digest.to_string());
            }
        }
    }
    digests
}

/// Validate all entries in a backup archive and extract only the selected paths.
pub fn extract_selected_archive(
    archive_path: &Path,
    destination: &Path,
    filter: &SelectedArchiveFilter<'_>,
    manifest: &Value,
) -> Result<(), AppError> {
    reset_dir(destination)?;
    let is_incremental =
        manifest.get("snapshotKind").and_then(Value::as_str) == Some("incremental");
    let mut desired: BTreeSet<String> =
        ["manifest.json".to_string(), "checksums.sha256".to_string()]
            .into_iter()
            .collect();
    desired.extend(filter.needed_full.iter().cloned());
    desired.extend(filter.needed_packs.iter().cloned());
    desired.extend(filter.needed_standalone.iter().cloned());
    if is_incremental {
        desired.insert("delta/operations.json".to_string());
        desired.insert(PACK_INDEX_PATH.to_string());
    }

    let declared = declared_archive_digests(manifest);
    let file =
        File::open(archive_path).map_err(|_| invalid_payload("Backup archive is unreadable"))?;
    let mut archive = ZipArchive::new(BufReader::new(file))
        .map_err(|_| invalid_payload("Backup archive is unreadable"))?;
    let infos = validate_archive_infos(&mut archive)?;
    let available: BTreeSet<String> = infos
        .iter()
        .filter(|meta| !meta.is_dir)
        .map(|meta| meta.normalized.clone())
        .collect();
    let missing: Vec<String> = desired.difference(&available).cloned().collect();
    if !missing.is_empty() {
        return Err(invalid_payload(format!(
            "Backup is missing required entries: {}",
            missing.join(", ")
        )));
    }

    let mut actual_total = 0_u64;
    for meta in infos {
        if meta.is_dir || !desired.contains(&meta.normalized) {
            continue;
        }
        let target = target_from_relative(destination, &meta.normalized);
        let mut zip_file = archive
            .by_index(meta.index)
            .map_err(|_| invalid_payload("Backup archive is unreadable"))?;
        extract_entry_bounded(&mut zip_file, &target, &mut actual_total)?;
    }

    for relative in filter
        .needed_full
        .iter()
        .chain(filter.needed_standalone.iter())
    {
        let expected = declared
            .get(relative)
            .ok_or_else(|| invalid_payload(format!("Backup entry is undeclared: {relative}")))?;
        let path = target_from_relative(destination, relative);
        let ok = path.is_file() && sha256_file(&path).ok().as_deref() == Some(expected.as_str());
        if !ok {
            return Err(invalid_payload(format!(
                "Backup selected entry failed checksum: {relative}"
            )));
        }
    }
    if is_incremental && !target_from_relative(destination, "delta/operations.json").is_file() {
        return Err(invalid_payload(
            "Backup delta operations manifest is missing",
        ));
    }
    Ok(())
}

/// Metadata for a single `.pack` file referenced in `payload/packs/index.json`.
#[derive(Clone, Debug)]
pub struct PackSpec {
    pub path: String,
    pub size: u64,
    pub sha256: String,
}

/// Normalized entry within a delta `.pack` file.
#[derive(Clone, Debug)]
pub struct PackEntry {
    pub pack: String,
    pub offset: u64,
    pub length: u64,
    pub sha256: Option<String>,
}

/// Parsed pack index metadata without eagerly reading `.pack` files.
#[derive(Clone, Debug)]
pub struct ParsedPackIndex {
    pub packs: BTreeMap<String, PackSpec>,
    pub entries: BTreeMap<String, PackEntry>,
}

/// Parse `payloadRef` into `("standalone" | "pack-range", locator)`.
pub fn parse_payload_ref(value: Option<&Value>) -> Result<(&'static str, String), AppError> {
    match value {
        Some(Value::String(text)) if !text.is_empty() => Ok(("standalone", text.clone())),
        Some(Value::Object(map)) => {
            let kind = map.get("kind").and_then(Value::as_str).unwrap_or("");
            let locator = match kind {
                "pack-range" => map.get("blobId").and_then(Value::as_str),
                "standalone" => map.get("path").and_then(Value::as_str),
                _ => None,
            };
            match (kind, locator) {
                ("pack-range", Some(blob_id)) if !blob_id.is_empty() => {
                    Ok(("pack-range", blob_id.to_string()))
                }
                ("standalone", Some(path)) if !path.is_empty() => {
                    Ok(("standalone", path.to_string()))
                }
                _ => Err(invalid_payload(
                    "Delta payload reference is missing or invalid",
                )),
            }
        }
        _ => Err(invalid_payload(
            "Delta payload reference is missing or invalid",
        )),
    }
}

/// Parse and structurally validate `payload/packs/index.json` without hashing packfiles.
pub fn parse_pack_index_file(destination: &Path) -> Result<ParsedPackIndex, AppError> {
    let index = read_json_object(
        &target_from_relative(destination, PACK_INDEX_PATH),
        "Delta pack index is invalid",
    )?;
    let schema_version = index
        .get("schemaVersion")
        .and_then(|v| v.as_i64())
        .filter(|v| *v == 1 || *v == PACK_INDEX_SCHEMA_VERSION)
        .ok_or_else(|| invalid_payload("Delta pack index is invalid"))?;
    let raw_packs = index
        .get("packs")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_payload("Delta pack index is invalid"))?;

    let mut packs = BTreeMap::<String, PackSpec>::new();
    let mut pack_order = Vec::<String>::new();
    for raw in raw_packs {
        let Some(obj) = raw.as_object() else {
            return Err(invalid_payload("Delta pack index is invalid"));
        };
        let relative = obj
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let size = strict_non_negative_int(obj.get("size"))
            .ok_or_else(|| invalid_payload("Delta pack index is invalid"))?;
        let digest = obj
            .get("sha256")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if !relative.starts_with("payload/packs/")
            || !relative.ends_with(".pack")
            || validate_archive_path(&relative).is_err()
            || packs.contains_key(&relative)
            || !is_plain_sha256(&digest)
        {
            return Err(invalid_payload("Delta pack index is invalid"));
        }
        packs.insert(
            relative.clone(),
            PackSpec {
                path: relative.clone(),
                size,
                sha256: digest,
            },
        );
        pack_order.push(relative);
    }

    let mut ranges: BTreeMap<String, Vec<(u64, u64)>> =
        packs.keys().map(|k| (k.clone(), Vec::new())).collect();
    let mut entries = BTreeMap::<String, PackEntry>::new();

    if schema_version == 1 {
        let Some(raw_entries) = index.get("entries").and_then(Value::as_object) else {
            return Err(invalid_payload("Delta pack index is invalid"));
        };
        for (blob_id, raw) in raw_entries {
            if blob_id.is_empty() {
                return Err(invalid_payload("Delta pack entry is invalid"));
            }
            let Some(obj) = raw.as_object() else {
                return Err(invalid_payload("Delta pack entry is invalid"));
            };
            let relative = obj.get("pack").and_then(Value::as_str).unwrap_or("");
            let offset = strict_non_negative_int(obj.get("offset"))
                .ok_or_else(|| invalid_payload("Delta pack entry is invalid"))?;
            let length = strict_non_negative_int(obj.get("length"))
                .ok_or_else(|| invalid_payload("Delta pack entry is invalid"))?;
            let digest = obj.get("sha256").and_then(Value::as_str).unwrap_or("");
            let Some(spec) = packs.get(relative) else {
                return Err(invalid_payload("Delta pack entry is invalid"));
            };
            if offset % PACK_ALIGNMENT != 0
                || offset.checked_add(length).is_none_or(|end| end > spec.size)
                || !is_plain_sha256(digest)
            {
                return Err(invalid_payload("Delta pack entry is invalid"));
            }
            ranges
                .get_mut(relative)
                .expect("pack checked")
                .push((offset, offset + length));
            entries.insert(
                blob_id.clone(),
                PackEntry {
                    pack: relative.to_string(),
                    offset,
                    length,
                    sha256: Some(digest.to_string()),
                },
            );
        }
    } else {
        let Some(raw_entries) = index.get("entries").and_then(Value::as_array) else {
            return Err(invalid_payload("Delta pack index is invalid"));
        };
        for (ordinal, raw) in raw_entries.iter().enumerate() {
            let Some(arr) = raw.as_array() else {
                return Err(invalid_payload("Delta pack entry is invalid"));
            };
            if arr.len() != 3 {
                return Err(invalid_payload("Delta pack entry is invalid"));
            }
            let pack_ordinal = strict_non_negative_int(arr.first())
                .and_then(|v| usize::try_from(v).ok())
                .ok_or_else(|| invalid_payload("Delta pack entry is invalid"))?;
            let Some(relative) = pack_order.get(pack_ordinal) else {
                return Err(invalid_payload("Delta pack entry is invalid"));
            };
            let offset = strict_non_negative_int(arr.get(1))
                .ok_or_else(|| invalid_payload("Delta pack entry is invalid"))?;
            let length = strict_non_negative_int(arr.get(2))
                .ok_or_else(|| invalid_payload("Delta pack entry is invalid"))?;
            let pack_size = packs[relative].size;
            if offset % PACK_ALIGNMENT != 0
                || offset.checked_add(length).is_none_or(|end| end > pack_size)
            {
                return Err(invalid_payload("Delta pack entry is invalid"));
            }
            ranges
                .get_mut(relative)
                .expect("pack checked")
                .push((offset, offset + length));
            entries.insert(
                format!("blob_{ordinal:06}"),
                PackEntry {
                    pack: relative.clone(),
                    offset,
                    length,
                    sha256: None,
                },
            );
        }
    }

    for mut pack_ranges in ranges.into_values() {
        pack_ranges.sort_unstable();
        let mut previous_end = 0_u64;
        for (start, end) in pack_ranges {
            if start < previous_end {
                return Err(invalid_payload("Delta pack entries overlap"));
            }
            previous_end = previous_end.max(end);
        }
    }

    Ok(ParsedPackIndex { packs, entries })
}

struct PackIndexVerification {
    entry_ids: BTreeSet<String>,
    indexed_packs: BTreeSet<String>,
    entry_packs: BTreeMap<String, String>,
}

fn load_and_verify_pack_index(destination: &Path) -> Result<PackIndexVerification, AppError> {
    let parsed = parse_pack_index_file(destination)?;
    for spec in parsed.packs.values() {
        let pack_path = target_from_relative(destination, &spec.path);
        let ok = pack_path.is_file()
            && fs::metadata(&pack_path).map(|m| m.len()).ok() == Some(spec.size)
            && sha256_file(&pack_path).ok().as_deref() == Some(spec.sha256.as_str());
        if !ok {
            return Err(invalid_payload(format!(
                "Delta pack failed checksum: {}",
                spec.path
            )));
        }
    }
    let entry_ids: BTreeSet<String> = parsed.entries.keys().cloned().collect();
    let indexed_packs: BTreeSet<String> = parsed.packs.keys().cloned().collect();
    let entry_packs: BTreeMap<String, String> = parsed
        .entries
        .into_iter()
        .map(|(k, v)| (k, v.pack))
        .collect();
    Ok(PackIndexVerification {
        entry_ids,
        indexed_packs,
        entry_packs,
    })
}

/// Verify an extracted backup tree (`manifest.json`, `checksums.sha256`, and optional delta packs).
pub fn verify_manifest_tree(destination: &Path) -> Result<Value, AppError> {
    let manifest_path = destination.join("manifest.json");
    let checksum_path = destination.join("checksums.sha256");
    let manifest = read_json_object(&manifest_path, "Backup manifest is invalid")?;
    check_json_depth(&manifest, 0)?;

    if manifest.get("schemaVersion").and_then(Value::as_str) != Some(BACKUP_SCHEMA)
        || manifest.get("purpose").and_then(Value::as_str) != Some(PACKAGE_PURPOSE)
    {
        return Err(invalid_payload(
            "Package is not a supported restorable backup",
        ));
    }

    let declared = manifest
        .get("files")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_payload("Backup file inventory is invalid"))?;
    let is_incremental =
        manifest.get("snapshotKind").and_then(Value::as_str) == Some("incremental");
    let mut declared_paths = BTreeSet::new();

    for entry in declared {
        let Some(obj) = entry.as_object() else {
            return Err(invalid_payload("Backup file inventory is invalid"));
        };
        let relative =
            validate_archive_path(obj.get("path").and_then(Value::as_str).unwrap_or(""))?;
        if relative == "manifest.json"
            || relative == "checksums.sha256"
            || !declared_paths.insert(relative.clone())
        {
            return Err(invalid_payload(
                "Backup manifest contains duplicate entries",
            ));
        }
        if is_incremental {
            continue;
        }
        let path = target_from_relative(destination, &relative);
        let expected_size = strict_non_negative_int(obj.get("size"));
        let expected_sha = obj.get("sha256").and_then(Value::as_str);
        let valid = path.is_file()
            && fs::metadata(&path).map(|m| m.len()).ok() == expected_size
            && expected_sha.is_some()
            && sha256_file(&path).ok().as_deref() == expected_sha;
        if !valid {
            return Err(invalid_payload(format!(
                "Backup checksum mismatch: {relative}"
            )));
        }
    }

    let mut actual = BTreeSet::new();
    collect_relative_files(destination, destination, true, &mut actual)?;

    let mut auxiliary = BTreeSet::new();
    if let Some(delta_files) = manifest.get("deltaFiles") {
        if !delta_files.is_null() {
            let Some(entries) = delta_files.as_array() else {
                return Err(invalid_payload("Backup delta file inventory is invalid"));
            };
            for entry in entries {
                let Some(obj) = entry.as_object() else {
                    return Err(invalid_payload("Backup delta file inventory is invalid"));
                };
                let relative =
                    validate_archive_path(obj.get("path").and_then(Value::as_str).unwrap_or(""))?;
                if declared_paths.contains(&relative) || !auxiliary.insert(relative.clone()) {
                    return Err(invalid_payload(
                        "Backup delta file collides with a restorable path",
                    ));
                }
                let path = target_from_relative(destination, &relative);
                let expected_size = strict_non_negative_int(obj.get("size"));
                let expected_sha = obj.get("sha256").and_then(Value::as_str);
                let valid = path.is_file()
                    && fs::metadata(&path).map(|m| m.len()).ok() == expected_size
                    && expected_sha.is_some()
                    && sha256_file(&path).ok().as_deref() == expected_sha;
                if !valid {
                    return Err(invalid_payload(format!(
                        "Backup delta checksum mismatch: {relative}"
                    )));
                }
            }
        }
    }

    if is_incremental {
        if actual != auxiliary {
            let missing: Vec<_> = auxiliary.difference(&actual).cloned().collect();
            let extra: Vec<_> = actual.difference(&auxiliary).cloned().collect();
            return Err(invalid_payload(format!(
                "Backup delta payload mismatch (missing {missing:?}, extra {extra:?})"
            )));
        }
        let ops_path = destination.join("delta").join("operations.json");
        if !ops_path.is_file() {
            return Err(invalid_payload(
                "Backup delta operations manifest is missing",
            ));
        }
        let ops = read_json_object(&ops_path, "Backup delta operations manifest is invalid")?;
        let mut referenced_files = BTreeSet::new();
        let mut referenced_blobs = BTreeSet::new();

        let mut record_ref = |raw: Option<&Value>| -> Result<(), AppError> {
            let (kind, locator) = parse_payload_ref(raw)?;
            if kind == "pack-range" {
                referenced_blobs.insert(locator);
                return Ok(());
            }
            let relative = validate_archive_path(&locator)?;
            if !auxiliary.contains(&relative) {
                return Err(invalid_payload(format!(
                    "Backup delta references undeclared payload: {relative}"
                )));
            }
            referenced_files.insert(relative);
            Ok(())
        };

        if let Some(puts_value) = ops.get("put") {
            if !puts_value.is_null() {
                let Some(puts) = puts_value.as_array() else {
                    return Err(invalid_payload(
                        "Backup delta operations manifest is invalid",
                    ));
                };
                for put in puts {
                    let Some(obj) = put.as_object() else {
                        return Err(invalid_payload(
                            "Backup delta operations manifest is invalid",
                        ));
                    };
                    if obj
                        .get("storage")
                        .and_then(Value::as_str)
                        .unwrap_or("whole")
                        == "cdc"
                    {
                        let Some(chunks) = obj.get("chunks").and_then(Value::as_array) else {
                            return Err(invalid_payload("Backup delta CDC file has no chunk list"));
                        };
                        for chunk in chunks {
                            let Some(chunk_obj) = chunk.as_object() else {
                                return Err(invalid_payload(
                                    "Backup delta CDC file has an invalid chunk",
                                ));
                            };
                            if chunk_obj.get("source").and_then(Value::as_str) == Some("payload") {
                                record_ref(chunk_obj.get("payloadRef"))?;
                            }
                        }
                    } else if let Some(raw_ref) = obj.get("payloadRef") {
                        if !raw_ref.is_null()
                            && !matches!(raw_ref, Value::String(s) if s.is_empty())
                            && !matches!(raw_ref, Value::Object(m) if m.is_empty())
                        {
                            record_ref(Some(raw_ref))?;
                        }
                    }
                }
            }
        }

        if !referenced_blobs.is_empty() || auxiliary.contains(PACK_INDEX_PATH) {
            if !auxiliary.contains(PACK_INDEX_PATH) {
                return Err(invalid_payload("Backup delta pack index is undeclared"));
            }
            let PackIndexVerification {
                entry_ids,
                indexed_packs,
                entry_packs,
            } = load_and_verify_pack_index(destination)?;
            if referenced_blobs != entry_ids {
                return Err(invalid_payload("Backup delta pack has unreferenced blobs"));
            }
            if indexed_packs.is_empty() || !indexed_packs.is_subset(&auxiliary) {
                return Err(invalid_payload("Backup delta pack inventory is incomplete"));
            }
            let mut used_packs = BTreeSet::new();
            for blob_id in &referenced_blobs {
                if let Some(pack) = entry_packs.get(blob_id) {
                    used_packs.insert(pack.clone());
                }
            }
            if used_packs != indexed_packs {
                return Err(invalid_payload(
                    "Backup delta pack inventory has unreferenced packs",
                ));
            }
            referenced_files.extend(indexed_packs);
            referenced_files.insert(PACK_INDEX_PATH.to_string());
        }

        let mut expected_aux = auxiliary;
        expected_aux.remove("delta/operations.json");
        if referenced_files != expected_aux {
            return Err(invalid_payload(
                "Backup delta payload has unreferenced blobs",
            ));
        }
    } else {
        let mut expected = declared_paths;
        expected.extend(auxiliary);
        if actual != expected {
            return Err(invalid_payload(
                "Backup contains undeclared or missing restorable files",
            ));
        }
    }

    let manifest_digest =
        sha256_file(&manifest_path).map_err(|err| internal_error(err.to_string()))?;
    let expected_manifest_line = format!("{manifest_digest}  manifest.json");
    let checksum_text = fs::read_to_string(&checksum_path)
        .map_err(|_| invalid_payload("Backup checksums are invalid"))?;
    if !checksum_text
        .lines()
        .any(|line| line == expected_manifest_line)
    {
        return Err(invalid_payload("Manifest digest is invalid"));
    }

    Ok(manifest)
}

/// Unlock and verify the optional inner sealed frontend mirror inside `staged_root/extracted`.
pub fn unlock_sealed_frontend(
    staged_root: &Path,
    identity_secret: &str,
) -> Result<Option<Value>, AppError> {
    let extracted = staged_root.join("extracted");
    let sealed = extracted.join("frontend").join("sealed-state.age");
    if !sealed.is_file() || extracted.join("frontend").join("state.json").is_file() {
        return Ok(None);
    }
    let verified_dir = staged_root.join("verified").join("frontend");
    fs::create_dir_all(&verified_dir).map_err(|err| internal_error(err.to_string()))?;
    let target = verified_dir.join("state.json");
    let temporary = verified_dir.join(format!(".state.json.{}.tmp", std::process::id()));

    let outcome = (|| -> Result<Value, AppError> {
        decrypt_file_native(&sealed, &temporary, identity_secret)?;
        let raw = fs::read_to_string(&temporary)
            .map_err(|_| invalid_payload("Sealed frontend mirror could not be decoded"))?;
        let envelope: Value = serde_json::from_str(&raw)
            .map_err(|_| invalid_payload("Sealed frontend mirror could not be decoded"))?;
        if !envelope.is_object() {
            return Err(invalid_payload("Sealed frontend mirror payload is invalid"));
        }
        validate_frontend_envelope(&envelope).map_err(|err| AppError {
            message: err.message,
            code: err.code,
            status: err.status,
        })?;

        let metadata_path = extracted.join("frontend").join("sealed-state.meta.json");
        let metadata: Value = if metadata_path.is_file() {
            fs::read_to_string(&metadata_path)
                .ok()
                .and_then(|s| serde_json::from_str::<Value>(&s).ok())
                .filter(Value::is_object)
                .unwrap_or_else(|| json!({}))
        } else {
            json!({})
        };

        let expected_digest = metadata
            .get("envelopeDigest")
            .and_then(Value::as_str)
            .unwrap_or("");
        if !expected_digest.is_empty()
            && Some(expected_digest) != envelope.get("digest").and_then(Value::as_str)
        {
            return Err(invalid_payload(
                "Sealed frontend mirror digest does not match its metadata",
            ));
        }

        let bytes = fs::read(&temporary).map_err(|err| internal_error(err.to_string()))?;
        fs::write(&target, &bytes).map_err(|err| internal_error(err.to_string()))?;
        Ok(json!({
            "mode": "sealed-mirror",
            "profileId": metadata.get("profileId").cloned().unwrap_or(Value::Null),
            "sourceEpoch": metadata.get("sourceEpoch").cloned().unwrap_or(Value::Null),
            "envelopeDigest": envelope.get("digest").cloned().unwrap_or(Value::Null),
            "recipientSetDigest": metadata.get("recipientSetDigest").cloned().unwrap_or(Value::Null),
            "mirrorCreatedAt": metadata.get("createdAt").cloned().unwrap_or(Value::Null),
            "conversations": envelope.get("conversations").and_then(Value::as_array).map(|a| a.len()).unwrap_or(0),
            "conflicts": envelope.get("conflicts").and_then(Value::as_array).map(|a| a.len()).unwrap_or(0),
        }))
    })();

    scrub_plaintext_file(&temporary);
    outcome.map(Some)
}

#[derive(Debug, Clone)]
pub struct ObjectMemberRef {
    pub digest: String,
    pub size: u64,
    pub ciphertext_path: PathBuf,
}

#[derive(Debug, Clone)]
pub struct UnlockDrillOutcome {
    pub backup_id: String,
    pub manifest_digest: String,
    pub contributors: usize,
    pub sealed_frontend: Option<Value>,
    pub verified_at: String,
}

fn finalize_unlock_drill(
    staged: &Path,
    backup_id: &str,
    manifest: &Value,
    manifest_digest: String,
    identity_secret: &str,
) -> Result<UnlockDrillOutcome, AppError> {
    let extracted = staged.join("extracted");
    let sealed = extracted.join("frontend").join("sealed-state.age");
    let frontend = if sealed.is_file() {
        let result = unlock_sealed_frontend(staged, identity_secret)?;
        if result.is_none() {
            return Err(invalid_payload(
                "Sealed frontend mirror could not be unlocked",
            ));
        }
        result
    } else {
        None
    };
    let contributors = manifest
        .get("contributors")
        .and_then(Value::as_array)
        .map(|items| items.len())
        .unwrap_or(0);
    Ok(UnlockDrillOutcome {
        backup_id: backup_id.to_string(),
        manifest_digest,
        contributors,
        sealed_frontend: frontend,
        verified_at: now_iso(None),
    })
}

fn validate_stage_token(token: &str, message: &str) -> Result<(), AppError> {
    if token.is_empty()
        || matches!(token, "." | "..")
        || token.contains('/')
        || token.contains('\\')
        || token.contains('\0')
    {
        return Err(invalid_payload(message));
    }
    Ok(())
}

/// Run a non-mutating native unlock drill over an `object-set-v1` backup.
pub fn verify_unlock_object_set(
    staged_root: &Path,
    backup_id: &str,
    control_digest: &str,
    members: &[ObjectMemberRef],
    identity_secret: &str,
) -> Result<UnlockDrillOutcome, AppError> {
    validate_stage_token(backup_id, "Backup id is invalid")?;
    let mut committed = BTreeMap::<String, &ObjectMemberRef>::new();
    for item in members {
        committed.insert(item.digest.clone(), item);
    }
    let Some(control_member) = committed.get(control_digest).copied() else {
        return Err(invalid_payload(
            "Object-set control ciphertext is not committed",
        ));
    };
    for member in members {
        if !member.ciphertext_path.is_file() {
            return Err(not_found_error("Backup object-set member is missing"));
        }
        let actual_size = fs::metadata(&member.ciphertext_path)
            .map(|m| m.len())
            .map_err(|err| internal_error(err.to_string()))?;
        let actual_digest =
            sha256_file(&member.ciphertext_path).map_err(|err| internal_error(err.to_string()))?;
        if actual_size != member.size || actual_digest != member.digest {
            return Err(conflict_error(
                "Backup object-set member no longer matches its receipt",
            ));
        }
    }

    let staged = staged_root.join(format!("drill_{backup_id}"));
    reset_dir(&staged)?;
    let _guard = StagedDrillGuard::new(staged.clone());

    let extracted = staged.join("extracted");
    let control_plaintext = staged.join("unlocked-control.zip");
    decrypt_file_native(
        &control_member.ciphertext_path,
        &control_plaintext,
        identity_secret,
    )?;
    extract_archive_metadata(&control_plaintext, &extracted)?;
    verify_control_metadata(&extracted)?;
    let manifest_digest = sha256_file(&extracted.join("manifest.json"))
        .map_err(|err| internal_error(err.to_string()))?;

    let payload_index = read_json_object(
        &extracted.join("payload-index.json"),
        "Object-set control metadata is invalid",
    )?;
    let component_map = read_json_object(
        &extracted.join("component-map.json"),
        "Object-set control metadata is invalid",
    )?;
    let descriptors = payload_index
        .get("payloadComponents")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_payload("Object-set component inventory is invalid"))?;
    let components = component_map
        .get("components")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_payload("Object-set component inventory is invalid"))?;
    let descriptor_keys: BTreeSet<&str> = descriptors.keys().map(String::as_str).collect();
    let component_keys: BTreeSet<&str> = components.keys().map(String::as_str).collect();
    if descriptor_keys != component_keys {
        return Err(invalid_payload("Object-set component inventory is invalid"));
    }

    let mut payload_digests = BTreeSet::new();
    for component_id in descriptor_keys {
        validate_stage_token(component_id, "Object-set component id is invalid")?;
        let Some(descriptor) = descriptors[component_id].as_object() else {
            return Err(invalid_payload("Object-set component metadata is invalid"));
        };
        let Some(raw_paths) = components[component_id].as_array() else {
            return Err(invalid_payload("Object-set component metadata is invalid"));
        };
        let mut expected_paths = Vec::with_capacity(raw_paths.len());
        for raw_path in raw_paths {
            let Some(text) = raw_path.as_str() else {
                return Err(invalid_payload("Object-set component metadata is invalid"));
            };
            expected_paths.push(text.to_string());
        }

        let digest = descriptor
            .get("ciphertextDigest")
            .and_then(Value::as_str)
            .unwrap_or("");
        let expected_ciphertext_size = strict_non_negative_int(descriptor.get("ciphertextSize"));
        let Some(committed_item) = committed.get(digest).copied() else {
            return Err(invalid_payload(
                "Object-set payload is not exactly committed",
            ));
        };
        if Some(committed_item.size) != expected_ciphertext_size
            || !payload_digests.insert(digest.to_string())
        {
            return Err(invalid_payload(
                "Object-set payload is not exactly committed",
            ));
        }

        let plaintext = staged.join(format!("unlocked-{component_id}.zip"));
        decrypt_file_native(&committed_item.ciphertext_path, &plaintext, identity_secret)?;
        let expected_plain_size = strict_non_negative_int(descriptor.get("plaintextSize"));
        let expected_plain_sha = descriptor.get("plaintextSha256").and_then(Value::as_str);
        let actual_plain_size = fs::metadata(&plaintext)
            .map(|m| m.len())
            .map_err(|err| internal_error(err.to_string()))?;
        let actual_plain_sha =
            sha256_file(&plaintext).map_err(|err| internal_error(err.to_string()))?;
        if Some(actual_plain_size) != expected_plain_size
            || expected_plain_sha != Some(actual_plain_sha.as_str())
        {
            return Err(invalid_payload(
                "Object-set payload plaintext commitment mismatch",
            ));
        }
        extract_component_archive(&plaintext, &extracted, &expected_paths)?;
        scrub_plaintext_file(&plaintext);
    }

    let mut non_control_committed: BTreeSet<String> = committed.keys().cloned().collect();
    non_control_committed.remove(control_digest);
    if payload_digests != non_control_committed {
        return Err(invalid_payload(
            "Object-set receipt contains a foreign payload",
        ));
    }

    fs::remove_file(extracted.join("payload-index.json"))
        .map_err(|err| internal_error(err.to_string()))?;
    fs::remove_file(extracted.join("component-map.json"))
        .map_err(|err| internal_error(err.to_string()))?;
    let manifest = verify_manifest_tree(&extracted)?;
    scrub_plaintext_file(&control_plaintext);

    finalize_unlock_drill(
        &staged,
        backup_id,
        &manifest,
        manifest_digest,
        identity_secret,
    )
}

/// Run a non-mutating native unlock drill over a legacy whole-`.dsibackup.age` backup.
pub fn verify_unlock_legacy(
    staged_root: &Path,
    backup_id: &str,
    source_path: &Path,
    expected_ciphertext_sha256: &str,
    expected_manifest_digest: &str,
    identity_secret: &str,
) -> Result<UnlockDrillOutcome, AppError> {
    validate_stage_token(backup_id, "Backup id is invalid")?;
    if !source_path.is_file() {
        return Err(not_found_error("Backup file is missing"));
    }
    let actual_cipher_sha =
        sha256_file(source_path).map_err(|err| internal_error(err.to_string()))?;
    if actual_cipher_sha != expected_ciphertext_sha256 {
        return Err(conflict_error(
            "Backup ciphertext no longer matches its receipt",
        ));
    }

    let staged = staged_root.join(format!("drill_{backup_id}"));
    reset_dir(&staged)?;
    let _guard = StagedDrillGuard::new(staged.clone());

    let extracted = staged.join("extracted");
    let decrypted = staged.join("unlocked.dsibackup");
    decrypt_file_native(source_path, &decrypted, identity_secret)?;
    let manifest = safe_extract_and_verify(&decrypted, &extracted)?;
    let manifest_digest = sha256_file(&extracted.join("manifest.json"))
        .map_err(|err| internal_error(err.to_string()))?;
    if manifest_digest != expected_manifest_digest {
        return Err(invalid_payload(
            "Backup manifest digest does not match its receipt",
        ));
    }
    scrub_plaintext_file(&decrypted);

    finalize_unlock_drill(
        &staged,
        backup_id,
        &manifest,
        manifest_digest,
        identity_secret,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backup_mirror::sha256_hex;
    use tempfile::tempdir;
    use zip::write::FileOptions;
    use zip::{CompressionMethod, ZipWriter};

    /// Build a complete (non-incremental) backup tree with one restorable file
    /// and a matching `checksums.sha256`, then let a caller break one piece.
    fn complete_backup(root: &Path, files: &[(&str, &[u8])]) {
        std::fs::create_dir_all(root).unwrap();
        let mut inventory = Vec::new();
        let mut lines = Vec::new();
        for (relative, content) in files {
            let path = root.join(relative);
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).unwrap();
            }
            std::fs::write(&path, content).unwrap();
            let digest = sha256_file(&path).unwrap();
            inventory.push(serde_json::json!({
                "path": relative,
                "size": content.len(),
                "sha256": digest,
            }));
            lines.push(format!("{digest}  {relative}"));
        }
        let manifest = serde_json::json!({
            "schemaVersion": BACKUP_SCHEMA,
            "purpose": PACKAGE_PURPOSE,
            "files": inventory,
        });
        let manifest_path = root.join("manifest.json");
        std::fs::write(&manifest_path, manifest.to_string()).unwrap();
        let manifest_digest = sha256_file(&manifest_path).unwrap();
        lines.push(format!("{manifest_digest}  manifest.json"));
        std::fs::write(root.join("checksums.sha256"), lines.join("\n")).unwrap();
    }

    #[test]
    fn manifest_tree_accepts_a_complete_backup() {
        let root = pack_root("manifest-ok");
        complete_backup(
            &root,
            &[("data/one.txt", b"hello"), ("data/two.txt", b"world")],
        );
        let manifest = verify_manifest_tree(&root).unwrap();
        assert_eq!(manifest["schemaVersion"], BACKUP_SCHEMA);
        assert_eq!(manifest["files"].as_array().unwrap().len(), 2);
        let _ = std::fs::remove_dir_all(&root);
    }

    /// label, the mutation that breaks a valid tree, and the message it must yield.
    type BrokenTree = (&'static str, fn(&Path), &'static str);

    #[test]
    fn manifest_tree_refuses_every_broken_shape() {
        let cases: Vec<BrokenTree> = vec![
            (
                "missing manifest",
                |root| {
                    let _ = std::fs::remove_file(root.join("manifest.json"));
                },
                "Backup manifest is invalid",
            ),
            (
                "wrong schema",
                |root| {
                    let manifest = serde_json::json!({
                        "schemaVersion": "other", "purpose": PACKAGE_PURPOSE, "files": []
                    });
                    std::fs::write(root.join("manifest.json"), manifest.to_string()).unwrap();
                },
                "Package is not a supported restorable backup",
            ),
            (
                "wrong purpose",
                |root| {
                    let manifest = serde_json::json!({
                        "schemaVersion": BACKUP_SCHEMA, "purpose": "other", "files": []
                    });
                    std::fs::write(root.join("manifest.json"), manifest.to_string()).unwrap();
                },
                "Package is not a supported restorable backup",
            ),
            (
                "files not an array",
                |root| {
                    let manifest = serde_json::json!({
                        "schemaVersion": BACKUP_SCHEMA, "purpose": PACKAGE_PURPOSE, "files": {}
                    });
                    std::fs::write(root.join("manifest.json"), manifest.to_string()).unwrap();
                },
                "Backup file inventory is invalid",
            ),
            (
                "file entry not an object",
                |root| {
                    let manifest = serde_json::json!({
                        "schemaVersion": BACKUP_SCHEMA, "purpose": PACKAGE_PURPOSE, "files": [1]
                    });
                    std::fs::write(root.join("manifest.json"), manifest.to_string()).unwrap();
                },
                "Backup file inventory is invalid",
            ),
            (
                "duplicate entry",
                |root| {
                    // The first entry passes its own checksum check before the
                    // duplicate is noticed on the second, so both must describe
                    // the real file rather than a placeholder digest.
                    let digest = sha256_file(&root.join("data/one.txt")).unwrap();
                    let manifest = serde_json::json!({
                        "schemaVersion": BACKUP_SCHEMA,
                        "purpose": PACKAGE_PURPOSE,
                        "files": [
                            {"path": "data/one.txt", "size": 5, "sha256": digest},
                            {"path": "data/one.txt", "size": 5, "sha256": digest}
                        ]
                    });
                    std::fs::write(root.join("manifest.json"), manifest.to_string()).unwrap();
                },
                "Backup manifest contains duplicate entries",
            ),
            (
                "entry names the manifest itself",
                |root| {
                    let manifest = serde_json::json!({
                        "schemaVersion": BACKUP_SCHEMA, "purpose": PACKAGE_PURPOSE,
                        "files": [{"path": "manifest.json", "size": 0, "sha256": "a".repeat(64)}]
                    });
                    std::fs::write(root.join("manifest.json"), manifest.to_string()).unwrap();
                },
                "Backup manifest contains duplicate entries",
            ),
            (
                "checksum mismatch",
                |root| {
                    std::fs::write(root.join("data/one.txt"), b"changed").unwrap();
                },
                "Backup checksum mismatch: data/one.txt",
            ),
            (
                "undeclared file present",
                |root| {
                    std::fs::write(root.join("data/extra.txt"), b"extra").unwrap();
                },
                "Backup contains undeclared or missing restorable files",
            ),
            (
                "missing checksums",
                |root| {
                    let _ = std::fs::remove_file(root.join("checksums.sha256"));
                },
                "Backup checksums are invalid",
            ),
            (
                "manifest line absent from checksums",
                |root| {
                    std::fs::write(root.join("checksums.sha256"), "").unwrap();
                },
                "Manifest digest is invalid",
            ),
        ];
        for (label, mutate, expected) in cases {
            let root = pack_root(&format!("bad-{}", label.replace(' ', "-")));
            complete_backup(&root, &[("data/one.txt", b"hello")]);
            mutate(&root);
            let error = verify_manifest_tree(&root).expect_err(&format!("{label} was accepted"));
            assert_eq!(error.message, expected, "{label}");
            let _ = std::fs::remove_dir_all(&root);
        }
    }

    /// Write a `payload/packs/index.json` under `root` and parse it back.
    fn pack_index(root: &Path, document: serde_json::Value) -> Result<ParsedPackIndex, AppError> {
        let dir = root.join("payload").join("packs");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("index.json"), document.to_string()).unwrap();
        parse_pack_index_file(root)
    }

    fn pack_root(tag: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "pack-index-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    const DIGEST: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    /// Schema 2 lists entries as `[packOrdinal, offset, length]` triples and
    /// names the resulting blobs positionally; there is no per-entry digest.
    #[test]
    fn pack_index_schema_two_accepts_aligned_entries() {
        let root = pack_root("v2-ok");
        let parsed = pack_index(
            &root,
            serde_json::json!({
                "schemaVersion": 2,
                "packs": [{"path": "payload/packs/one.pack", "size": 64, "sha256": DIGEST}],
                "entries": [[0, 8, 16]]
            }),
        )
        .unwrap();
        assert_eq!(parsed.packs.len(), 1);
        assert_eq!(parsed.entries.len(), 1);
        let entry = &parsed.entries["blob_000000"];
        assert_eq!(entry.pack, "payload/packs/one.pack");
        assert_eq!(entry.offset, 8);
        assert_eq!(entry.length, 16);
        assert_eq!(entry.sha256, None);
        // A pack-only index declares no blobs, which is legal.
        let parsed = pack_index(
            &root,
            serde_json::json!({
                "schemaVersion": 2,
                "packs": [{"path": "payload/packs/one.pack", "size": 64, "sha256": DIGEST}],
                "entries": []
            }),
        )
        .unwrap();
        assert!(parsed.entries.is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }

    /// Schema 1 keys entries by blob id in an object, and each entry must carry
    /// a hex `sha256` - unlike schema 2, which has no per-entry digest at all.
    #[test]
    fn pack_index_schema_one_requires_a_hex_digest_per_entry() {
        let root = pack_root("v1-ok");
        let parsed = pack_index(
            &root,
            serde_json::json!({
                "schemaVersion": 1,
                "packs": [{"path": "payload/packs/one.pack", "size": 64, "sha256": DIGEST}],
                "entries": {"blob-a": {"pack": "payload/packs/one.pack", "offset": 0, "length": 8, "sha256": DIGEST}}
            }),
        )
        .unwrap();
        assert_eq!(parsed.entries["blob-a"].sha256.as_deref(), Some(DIGEST));
        // Omitting the digest is refused.
        let missing = pack_index(
            &root,
            serde_json::json!({
                "schemaVersion": 1,
                "packs": [{"path": "payload/packs/one.pack", "size": 64, "sha256": DIGEST}],
                "entries": {"blob-a": {"pack": "payload/packs/one.pack", "offset": 0, "length": 8}}
            }),
        )
        .unwrap_err();
        assert_eq!(missing.message, "Delta pack entry is invalid");
        let _ = std::fs::remove_dir_all(&root);
    }

    /// Every rejection below is the same `Delta pack index is invalid` /
    /// `Delta pack entry is invalid` pair the route surfaces.
    #[test]
    fn pack_index_refuses_malformed_documents() {
        let invalid_index = "Delta pack index is invalid";
        let invalid_entry = "Delta pack entry is invalid";
        let cases: Vec<(&str, serde_json::Value, &str)> = vec![
            (
                "missing schema",
                serde_json::json!({"packs": [], "entries": {}}),
                invalid_index,
            ),
            (
                "wrong schema",
                serde_json::json!({"schemaVersion": 3, "packs": [], "entries": {}}),
                invalid_index,
            ),
            (
                "packs not an array",
                serde_json::json!({"schemaVersion": 2, "packs": {}, "entries": {}}),
                invalid_index,
            ),
            (
                "pack not an object",
                serde_json::json!({"schemaVersion": 2, "packs": [1], "entries": {}}),
                invalid_index,
            ),
            (
                "pack outside payload",
                serde_json::json!({"schemaVersion": 2, "packs": [{"path": "other/one.pack", "size": 8, "sha256": DIGEST}], "entries": {}}),
                invalid_index,
            ),
            (
                "pack without suffix",
                serde_json::json!({"schemaVersion": 2, "packs": [{"path": "payload/packs/one.zip", "size": 8, "sha256": DIGEST}], "entries": {}}),
                invalid_index,
            ),
            (
                "pack size not an int",
                serde_json::json!({"schemaVersion": 2, "packs": [{"path": "payload/packs/one.pack", "size": "8", "sha256": DIGEST}], "entries": {}}),
                invalid_index,
            ),
            (
                "pack digest not hex",
                serde_json::json!({"schemaVersion": 2, "packs": [{"path": "payload/packs/one.pack", "size": 8, "sha256": "zz"}], "entries": {}}),
                invalid_index,
            ),
            (
                "duplicate pack",
                serde_json::json!({"schemaVersion": 2, "packs": [
                {"path": "payload/packs/one.pack", "size": 8, "sha256": DIGEST},
                {"path": "payload/packs/one.pack", "size": 8, "sha256": DIGEST}], "entries": {}}),
                invalid_index,
            ),
            (
                "entries not an array in schema 2",
                serde_json::json!({"schemaVersion": 2, "packs": [], "entries": {}}),
                invalid_index,
            ),
            (
                "entries not an object in schema 1",
                serde_json::json!({"schemaVersion": 1, "packs": [], "entries": []}),
                invalid_index,
            ),
            (
                "entry arity",
                serde_json::json!({"schemaVersion": 2, "packs": [{"path": "payload/packs/one.pack", "size": 64, "sha256": DIGEST}], "entries": [[0, 0]]}),
                invalid_entry,
            ),
            (
                "entry pack ordinal out of range",
                serde_json::json!({"schemaVersion": 2, "packs": [{"path": "payload/packs/one.pack", "size": 64, "sha256": DIGEST}], "entries": [[9, 0, 8]]}),
                invalid_entry,
            ),
            (
                "entry offset not an int",
                serde_json::json!({"schemaVersion": 2, "packs": [{"path": "payload/packs/one.pack", "size": 64, "sha256": DIGEST}], "entries": [[0, "0", 8]]}),
                invalid_entry,
            ),
            (
                "entry unaligned in schema 2",
                serde_json::json!({"schemaVersion": 2, "packs": [{"path": "payload/packs/one.pack", "size": 64, "sha256": DIGEST}], "entries": [[0, 3, 8]]}),
                invalid_entry,
            ),
            (
                "schema 1 empty blob id",
                serde_json::json!({"schemaVersion": 1, "packs": [], "entries": {"": {"pack": "payload/packs/one.pack", "offset": 0, "length": 0, "sha256": DIGEST}}}),
                invalid_entry,
            ),
            (
                "schema 1 entry not an object",
                serde_json::json!({"schemaVersion": 1, "packs": [], "entries": {"a": 1}}),
                invalid_entry,
            ),
            (
                "schema 1 entry names an unknown pack",
                serde_json::json!({"schemaVersion": 1, "packs": [], "entries": {"a": {"pack": "payload/packs/absent.pack", "offset": 0, "length": 0, "sha256": DIGEST}}}),
                invalid_entry,
            ),
            (
                "schema 1 unaligned offset",
                serde_json::json!({"schemaVersion": 1, "packs": [{"path": "payload/packs/one.pack", "size": 64, "sha256": DIGEST}], "entries": {"a": {"pack": "payload/packs/one.pack", "offset": 3, "length": 8, "sha256": DIGEST}}}),
                invalid_entry,
            ),
            (
                "schema 1 range past the pack",
                serde_json::json!({"schemaVersion": 1, "packs": [{"path": "payload/packs/one.pack", "size": 8, "sha256": DIGEST}], "entries": {"a": {"pack": "payload/packs/one.pack", "offset": 8, "length": 8, "sha256": DIGEST}}}),
                invalid_entry,
            ),
            (
                "schema 1 entry digest not hex",
                serde_json::json!({"schemaVersion": 1, "packs": [{"path": "payload/packs/one.pack", "size": 64, "sha256": DIGEST}], "entries": {"a": {"pack": "payload/packs/one.pack", "offset": 0, "length": 8, "sha256": "nope"}}}),
                invalid_entry,
            ),
            (
                "schema 2 range past the pack",
                serde_json::json!({"schemaVersion": 2, "packs": [{"path": "payload/packs/one.pack", "size": 8, "sha256": DIGEST}], "entries": [[0, 8, 8]]}),
                invalid_entry,
            ),
        ];
        for (label, document, expected) in cases {
            let root = pack_root(&format!("bad-{}", label.replace(' ', "-")));
            let error = pack_index(&root, document)
                .err()
                .unwrap_or_else(|| panic!("{label} was accepted"));
            assert_eq!(error.message, expected, "{label}");
            let _ = std::fs::remove_dir_all(&root);
        }
    }

    /// A missing index file is refused before any parsing happens.
    #[test]
    fn pack_index_refuses_a_missing_file() {
        let root = pack_root("missing");
        let error = parse_pack_index_file(&root).expect_err("missing index accepted");
        assert_eq!(error.message, "Delta pack index is invalid");
        let _ = std::fs::remove_dir_all(&root);
    }

    /// `parse_payload_ref` maps a bare string to standalone and a table with
    /// `pack`/`offset`/`length` to a pack range.
    #[test]
    fn payload_ref_forms() {
        assert_eq!(
            parse_payload_ref(Some(&serde_json::json!("object-1"))).unwrap(),
            ("standalone", "object-1".to_string())
        );
        // The object form carries an explicit `kind` and a matching locator.
        assert_eq!(
            parse_payload_ref(Some(
                &serde_json::json!({"kind": "pack-range", "blobId": "blob-a"})
            ))
            .unwrap(),
            ("pack-range", "blob-a".to_string())
        );
        assert_eq!(
            parse_payload_ref(Some(
                &serde_json::json!({"kind": "standalone", "path": "payload/x.bin"})
            ))
            .unwrap(),
            ("standalone", "payload/x.bin".to_string())
        );
        assert!(parse_payload_ref(None).is_err());
        assert!(parse_payload_ref(Some(&serde_json::json!(""))).is_err());
        // An unknown kind, a missing locator and an empty locator all refuse.
        for bad in [
            serde_json::json!({"kind": "other", "blobId": "b"}),
            serde_json::json!({"kind": "pack-range"}),
            serde_json::json!({"kind": "pack-range", "blobId": ""}),
            serde_json::json!({"kind": "standalone", "path": ""}),
        ] {
            let error = parse_payload_ref(Some(&bad)).expect_err("accepted");
            assert_eq!(
                error.message,
                "Delta payload reference is missing or invalid"
            );
        }
    }

    fn render_sha256_manifest(entries: &[(&str, &[u8])]) -> String {
        use std::fmt::Write as _;

        let mut text = String::new();
        for (name, bytes) in entries {
            let _ = writeln!(text, "{}  {name}", sha256_hex(bytes));
        }
        text
    }

    fn write_test_zip(path: &Path, entries: &[(&str, &[u8])]) {
        let file = File::create(path).unwrap();
        let mut writer = ZipWriter::new(file);
        let opts = FileOptions::default().compression_method(CompressionMethod::Stored);
        for (name, content) in entries {
            writer.start_file(*name, opts).unwrap();
            writer.write_all(content).unwrap();
        }
        writer.finish().unwrap();
    }

    fn encrypt_file_with_recipient(src: &Path, dst: &Path, recipient: &str) {
        let bytes = fs::read(src).unwrap();
        let sealed = encrypt_bytes_to_recipients(&bytes, &[recipient.to_string()]).unwrap();
        fs::write(dst, sealed).unwrap();
    }

    #[test]
    fn verify_unlock_object_set_decrypts_verifies_and_scrubs_staging() {
        let dir = tempdir().unwrap();
        let (secret, recipient) = backup_crypto::ephemeral_identity();

        let payload_bytes = br#"{"hello":"world"}"#;
        let payload_rel = "payload/projects/p1/state.json";
        let payload_zip = dir.path().join("payload.zip");
        write_test_zip(&payload_zip, &[(payload_rel, payload_bytes)]);
        let payload_plain_size = fs::metadata(&payload_zip).unwrap().len();
        let payload_plain_sha = sha256_file(&payload_zip).unwrap();

        let payload_age = dir.path().join("payload.age");
        encrypt_file_with_recipient(&payload_zip, &payload_age, &recipient);
        let payload_cipher_size = fs::metadata(&payload_age).unwrap().len();
        let payload_cipher_sha = sha256_file(&payload_age).unwrap();

        let manifest = serde_json::to_vec(&json!({
            "schemaVersion": BACKUP_SCHEMA,
            "purpose": PACKAGE_PURPOSE,
            "snapshotKind": "full",
            "contributors": [{"id": "projects"}],
            "files": [{
                "path": payload_rel,
                "size": payload_bytes.len(),
                "sha256": sha256_hex(payload_bytes),
            }],
        }))
        .unwrap();
        let manifest_sha = sha256_hex(&manifest);
        let payload_index = serde_json::to_vec(&json!({
            "schemaVersion": 1,
            "storageProtocol": "object-set-v1",
            "snapshotFormat": "full",
            "payloadComponents": {
                "projects-p1": {
                    "ciphertextDigest": payload_cipher_sha,
                    "ciphertextSize": payload_cipher_size,
                    "plaintextSize": payload_plain_size,
                    "plaintextSha256": payload_plain_sha,
                }
            }
        }))
        .unwrap();
        let component_map = serde_json::to_vec(&json!({
            "components": {
                "projects-p1": [payload_rel]
            }
        }))
        .unwrap();
        let control_map_entries = [
            ("component-map.json", component_map.as_slice()),
            ("manifest.json", manifest.as_slice()),
            ("payload-index.json", payload_index.as_slice()),
        ];
        let checksum_text = render_sha256_manifest(&control_map_entries);

        let control_zip = dir.path().join("control.zip");
        write_test_zip(
            &control_zip,
            &[
                ("component-map.json", &component_map),
                ("manifest.json", &manifest),
                ("payload-index.json", &payload_index),
                ("checksums.sha256", checksum_text.as_bytes()),
            ],
        );
        let control_age = dir.path().join("control.age");
        encrypt_file_with_recipient(&control_zip, &control_age, &recipient);
        let control_size = fs::metadata(&control_age).unwrap().len();
        let control_sha = sha256_file(&control_age).unwrap();

        let staged_root = dir.path().join("restore-staging");
        let outcome = verify_unlock_object_set(
            &staged_root,
            "bak_123",
            &control_sha,
            &[
                ObjectMemberRef {
                    digest: control_sha.clone(),
                    size: control_size,
                    ciphertext_path: control_age.clone(),
                },
                ObjectMemberRef {
                    digest: payload_cipher_sha.clone(),
                    size: payload_cipher_size,
                    ciphertext_path: payload_age.clone(),
                },
            ],
            &secret,
        )
        .unwrap();

        assert_eq!(outcome.backup_id, "bak_123");
        assert_eq!(outcome.manifest_digest, manifest_sha);
        assert_eq!(outcome.contributors, 1);
        assert!(!staged_root.join("drill_bak_123").exists());
    }

    #[test]
    fn verify_manifest_tree_accepts_null_delta_files_and_rejects_component_traversal() {
        let dir = tempdir().unwrap();
        let file_bytes = br#"{"x":1}"#;
        let rel = "payload/projects/p1/x.json";
        let full_dir = dir.path().join("full");
        fs::create_dir_all(full_dir.join("payload/projects/p1")).unwrap();
        fs::write(full_dir.join(rel), file_bytes).unwrap();
        let manifest_bytes = serde_json::to_vec(&json!({
            "schemaVersion": BACKUP_SCHEMA,
            "purpose": PACKAGE_PURPOSE,
            "snapshotKind": "full",
            "deltaFiles": null,
            "files": [{
                "path": rel,
                "size": file_bytes.len(),
                "sha256": sha256_hex(file_bytes),
            }],
        }))
        .unwrap();
        fs::write(full_dir.join("manifest.json"), &manifest_bytes).unwrap();
        fs::write(
            full_dir.join("checksums.sha256"),
            format!("{}  manifest.json\n", sha256_hex(&manifest_bytes)),
        )
        .unwrap();
        verify_manifest_tree(&full_dir).unwrap();

        let (secret, recipient) = backup_crypto::ephemeral_identity();
        let payload_zip = dir.path().join("p.zip");
        write_test_zip(&payload_zip, &[(rel, file_bytes)]);
        let payload_age = dir.path().join("p.age");
        encrypt_file_with_recipient(&payload_zip, &payload_age, &recipient);
        let payload_sha = sha256_file(&payload_age).unwrap();
        let payload_size = fs::metadata(&payload_age).unwrap().len();
        let p_index = serde_json::to_vec(&json!({
            "payloadComponents": {
                "../evil": {
                    "ciphertextDigest": payload_sha,
                    "ciphertextSize": payload_size,
                    "plaintextSize": fs::metadata(&payload_zip).unwrap().len(),
                    "plaintextSha256": sha256_file(&payload_zip).unwrap(),
                }
            }
        }))
        .unwrap();
        let comp_map = serde_json::to_vec(&json!({"components": {"../evil": [rel]}})).unwrap();
        let checksum_text = render_sha256_manifest(&[
            ("component-map.json", comp_map.as_slice()),
            ("manifest.json", manifest_bytes.as_slice()),
            ("payload-index.json", p_index.as_slice()),
        ]);
        let control_zip = dir.path().join("c.zip");
        write_test_zip(
            &control_zip,
            &[
                ("component-map.json", &comp_map),
                ("manifest.json", &manifest_bytes),
                ("payload-index.json", &p_index),
                ("checksums.sha256", checksum_text.as_bytes()),
            ],
        );
        let control_age = dir.path().join("c.age");
        encrypt_file_with_recipient(&control_zip, &control_age, &recipient);
        let control_sha = sha256_file(&control_age).unwrap();
        let control_size = fs::metadata(&control_age).unwrap().len();

        let err = verify_unlock_object_set(
            &dir.path().join("staging"),
            "bak_trav",
            &control_sha,
            &[
                ObjectMemberRef {
                    digest: control_sha.clone(),
                    size: control_size,
                    ciphertext_path: control_age,
                },
                ObjectMemberRef {
                    digest: payload_sha,
                    size: payload_size,
                    ciphertext_path: payload_age,
                },
            ],
            &secret,
        )
        .unwrap_err();
        assert_eq!(err.status, 400);
        assert!(err.message.contains("component id is invalid"));
    }

    #[test]
    fn encrypt_tree_zip_native_round_trips_passphrase_and_age_recipient() {
        let dir = tempdir().unwrap();
        let staging = dir.path().join("staging");
        fs::create_dir_all(staging.join("payload/projects/p1")).unwrap();
        let project_bytes = br#"{"id":"p1"}"#;
        fs::write(
            staging.join("payload/projects/p1/project.json"),
            project_bytes,
        )
        .unwrap();
        let mut manifest_obj = json!({
            "schemaVersion": BACKUP_SCHEMA,
            "purpose": PACKAGE_PURPOSE,
            "files": [
                {
                    "contributorId": "projects",
                    "path": "payload/projects/p1/project.json",
                    "size": project_bytes.len(),
                    "sha256": sha256_hex(project_bytes),
                }
            ],
        });
        let manifest_bytes = crate::python_json::dumps_compact(&manifest_obj).into_bytes();
        fs::write(staging.join("manifest.json"), &manifest_bytes).unwrap();
        let checksum_text = render_sha256_manifest(&[
            ("payload/projects/p1/project.json", project_bytes),
            ("manifest.json", manifest_bytes.as_slice()),
        ]);
        fs::write(staging.join("checksums.sha256"), checksum_text).unwrap();

        let pass_age = dir.path().join("out/pass.dsibackup.age");
        encrypt_tree_zip_native(&staging, &pass_age, "passphrase", Some("hunter2"), &[]).unwrap();
        assert!(inspect_age_file_protection(&pass_age).unwrap());
        let pass_zip = dir.path().join("out/pass.zip");
        decrypt_passphrase_file_native(&pass_age, &pass_zip, "hunter2").unwrap();
        let verified_pass =
            safe_extract_and_verify(&pass_zip, &dir.path().join("ex-pass")).unwrap();
        assert_eq!(verified_pass["schemaVersion"], BACKUP_SCHEMA);

        let (secret, recipient) = ephemeral_identity();
        let key_age = dir.path().join("out/key.dsibackup.age");
        encrypt_tree_zip_native(&staging, &key_age, "age-recipient", None, &[recipient]).unwrap();
        assert!(!inspect_age_file_protection(&key_age).unwrap());
        let key_zip = dir.path().join("out/key.zip");
        decrypt_file_native(&key_age, &key_zip, &secret).unwrap();
        let verified_key = safe_extract_and_verify(&key_zip, &dir.path().join("ex-key")).unwrap();
        assert_eq!(verified_key["purpose"], PACKAGE_PURPOSE);
        manifest_obj["verified"] = Value::Bool(true);
        assert_eq!(manifest_obj["verified"], Value::Bool(true));
    }
}
