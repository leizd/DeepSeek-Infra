//! Offline import of a fenced mirror inventory into an independent candidate.
//!
//! The source fence and every source/target byte are checked independently.
//! Importing is not production ownership admission. An imported candidate is
//! persistently denied mutations until a future verified Go admission/handback
//! protocol authorizes a transition; runtime flags cannot release this fence.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::backup_mirror::{
    MirrorStore, atomic_write, fsync_dir, generation_id, profile_id, sha256_file, sha256_hex,
};
use crate::python_json::dumps_compact;

const EXPORT_SCHEMA: &str = "python-mirror-inventory-export-v1";
const RECEIPT_SCHEMA: &str = "native-mirror-inventory-import-v1";
const DOMAIN: &str = "frontend_mirror_store";
const MAX_DOCUMENT_BYTES: u64 = 16 << 20;
const MAX_ENTRIES: usize = 100_000;
static TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct InventoryEntry {
    pub path: String,
    pub kind: String,
    pub size: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ExportManifest {
    schema: String,
    domain: String,
    transfer_id: String,
    source_root: String,
    target_root: String,
    entries: Vec<InventoryEntry>,
    empty_inventory: bool,
    source_digest: String,
    manifest_digest: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ImportReceipt {
    pub schema: String,
    pub domain: String,
    pub phase: String,
    pub transfer_id: String,
    pub source_root: String,
    pub target_root: String,
    pub staging_root: String,
    pub manifest_digest: String,
    pub source_digest: String,
    pub target_digest: String,
    pub receipt_digest: String,
}

fn error(value: impl std::fmt::Display) -> String {
    value.to_string()
}

fn canonical_digest(value: &impl Serialize) -> Result<String, String> {
    let value = serde_json::to_value(value).map_err(error)?;
    Ok(sha256_hex(dumps_compact(&value).as_bytes()))
}

fn unsigned_digest(value: &impl Serialize, field: &str) -> Result<String, String> {
    let mut value = serde_json::to_value(value).map_err(error)?;
    value
        .as_object_mut()
        .ok_or("handoff document must be an object")?
        .remove(field);
    canonical_digest(&value)
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn reject_link(path: &Path, metadata: &Metadata) -> Result<(), String> {
    #[cfg(windows)]
    let reparse = {
        use std::os::windows::fs::MetadataExt;
        metadata.file_attributes() & 0x400 != 0
    };
    #[cfg(not(windows))]
    let reparse = false;
    if metadata.file_type().is_symlink() || reparse {
        return Err(format!(
            "mirror handoff refuses a symlink or reparse point: {}",
            path.display()
        ));
    }
    Ok(())
}

fn exists(path: &Path) -> Result<bool, String> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            reject_link(path, &metadata)?;
            Ok(true)
        }
        Err(failure) if failure.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(failure) => Err(error(failure)),
    }
}

fn bound_path(path: &Path) -> Result<PathBuf, String> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir().map_err(error)?.join(path)
    };
    let mut current = PathBuf::new();
    for component in absolute.components() {
        if matches!(component, Component::ParentDir) {
            return Err("mirror handoff requires normalized explicit paths".into());
        }
        current.push(component);
        if matches!(
            component,
            Component::Prefix(_) | Component::RootDir | Component::CurDir
        ) {
            continue;
        }
        if !exists(&current)? {
            break;
        }
    }
    let mut existing = absolute.as_path();
    let mut missing = Vec::new();
    while !exists(existing)? {
        missing.push(
            existing
                .file_name()
                .ok_or("invalid handoff path")?
                .to_os_string(),
        );
        existing = existing
            .parent()
            .ok_or("handoff path has no existing ancestor")?;
    }
    let mut resolved = fs::canonicalize(existing).map_err(error)?;
    for component in missing.into_iter().rev() {
        resolved.push(component);
    }
    Ok(resolved)
}

fn path_binding(path: &Path) -> Result<String, String> {
    let value = path.to_str().ok_or("mirror handoff path must be UTF-8")?;
    #[cfg(windows)]
    let value = value.strip_prefix(r"\\?\").unwrap_or(value);
    Ok(value.replace('\\', "/"))
}

fn sibling(root: &Path, suffix: &str) -> Result<PathBuf, String> {
    let name = root
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or("invalid mirror root")?;
    Ok(root.with_file_name(format!("{name}{suffix}")))
}

pub fn receipt_path(root: &Path) -> Result<PathBuf, String> {
    sibling(root, ".native-import.json")
}

fn source_fence_path(root: &Path) -> Result<PathBuf, String> {
    sibling(root, ".native-handoff.json")
}

fn read_document<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T, String> {
    let metadata = fs::symlink_metadata(path).map_err(error)?;
    reject_link(path, &metadata)?;
    if !metadata.is_file() || metadata.len() > MAX_DOCUMENT_BYTES {
        return Err(format!(
            "invalid mirror handoff document: {}",
            path.display()
        ));
    }
    let mut raw = Vec::new();
    File::open(path)
        .map_err(error)?
        .take(MAX_DOCUMENT_BYTES + 1)
        .read_to_end(&mut raw)
        .map_err(error)?;
    if raw.len() as u64 > MAX_DOCUMENT_BYTES {
        return Err("mirror handoff document exceeds its byte limit".into());
    }
    serde_json::from_slice(&raw).map_err(error)
}

fn validate_manifest(
    manifest: &ExportManifest,
    source: &Path,
    target: &Path,
) -> Result<(), String> {
    if manifest.schema != EXPORT_SCHEMA
        || manifest.domain != DOMAIN
        || manifest.source_root != path_binding(source)?
        || manifest.target_root != path_binding(target)?
        || manifest.transfer_id.is_empty()
        || manifest.transfer_id.len() > 128
        || !manifest
            .transfer_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
        || manifest.empty_inventory != manifest.entries.is_empty()
        || manifest.entries.len() > MAX_ENTRIES
    {
        return Err("mirror export schema, identity, or path binding mismatch".into());
    }
    if !is_sha256(&manifest.source_digest)
        || !is_sha256(&manifest.manifest_digest)
        || canonical_digest(&manifest.entries)? != manifest.source_digest
        || unsigned_digest(manifest, "manifestDigest")? != manifest.manifest_digest
    {
        return Err("mirror export digest mismatch".into());
    }
    let mut previous: Option<&str> = None;
    let mut directories = BTreeSet::new();
    for entry in &manifest.entries {
        if entry.path.is_empty()
            || entry.path.contains('\\')
            || entry.path.contains(':')
            || entry
                .path
                .split('/')
                .any(|part| part.is_empty() || matches!(part, "." | ".."))
            || previous.is_some_and(|path| path >= entry.path.as_str())
        {
            return Err("unsafe, duplicate, or unordered mirror inventory path".into());
        }
        if let Some((parent, _)) = entry.path.rsplit_once('/') {
            if !directories.contains(parent) {
                return Err("mirror inventory is missing a parent directory".into());
            }
        }
        match entry.kind.as_str() {
            "directory" if entry.size == 0 && entry.sha256.is_empty() => {
                directories.insert(entry.path.as_str());
            }
            "file" if is_sha256(&entry.sha256) => {}
            _ => return Err("invalid mirror inventory entry".into()),
        }
        previous = Some(&entry.path);
    }
    Ok(())
}

fn inventory(root: &Path) -> Result<Vec<InventoryEntry>, String> {
    let metadata = fs::symlink_metadata(root).map_err(error)?;
    reject_link(root, &metadata)?;
    if !metadata.is_dir() {
        return Err("mirror inventory root is not a directory".into());
    }
    let mut entries = Vec::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(&directory).map_err(error)? {
            let path = entry.map_err(error)?.path();
            let metadata = fs::symlink_metadata(&path).map_err(error)?;
            reject_link(&path, &metadata)?;
            let relative = path_binding(path.strip_prefix(root).map_err(error)?)?;
            let (kind, size, sha256) = if metadata.is_dir() {
                pending.push(path.clone());
                ("directory", 0, String::new())
            } else if metadata.is_file() {
                let digest = sha256_file(&path).map_err(error)?;
                let after = fs::symlink_metadata(&path).map_err(error)?;
                reject_link(&path, &after)?;
                if metadata.len() != after.len()
                    || metadata.modified().map_err(error)? != after.modified().map_err(error)?
                {
                    return Err("mirror source changed during independent hashing".into());
                }
                ("file", metadata.len(), digest)
            } else {
                return Err("mirror inventory contains a non-regular entry".into());
            };
            entries.push(InventoryEntry {
                path: relative,
                kind: kind.into(),
                size,
                sha256,
            });
            if entries.len() > MAX_ENTRIES {
                return Err("mirror inventory has too many entries".into());
            }
        }
    }
    entries.sort_by(|left, right| left.path.cmp(&right.path));
    Ok(entries)
}

fn attest(root: &Path, manifest: &ExportManifest) -> Result<String, String> {
    let observed = inventory(root)?;
    if observed != manifest.entries {
        return Err("mirror inventory differs from the fenced source manifest".into());
    }
    validate_readers(root, &observed)?;
    canonical_digest(&observed)
}

fn validate_readers(root: &Path, entries: &[InventoryEntry]) -> Result<(), String> {
    let rows: BTreeMap<&str, &InventoryEntry> = entries
        .iter()
        .map(|entry| (entry.path.as_str(), entry))
        .collect();
    for entry in entries.iter().filter(|entry| !entry.path.contains('/')) {
        if entry.kind != "directory" {
            return Err("mirror root contains an unknown file".into());
        }
        profile_id(&entry.path).map_err(error)?;
        let profile = &entry.path;
        let head = root.join(profile).join("HEAD.json");
        if exists(&head)? {
            let value: Value = read_document(&head)?;
            let generation = value
                .get("generationId")
                .and_then(Value::as_str)
                .and_then(generation_id)
                .ok_or("invalid mirror HEAD generation")?;
            if value.get("schemaVersion").and_then(Value::as_i64) != Some(2)
                || !rows.contains_key(
                    format!("{profile}/generations/{generation}/metadata.json").as_str(),
                )
            {
                return Err("mirror HEAD does not resolve a complete generation".into());
            }
        }
        // This is the production native consumer. It verifies the selected
        // ciphertext hash rather than trusting a declared creationVerified bit.
        MirrorStore::new(root).files(profile, None).map_err(error)?;
    }
    for entry in entries
        .iter()
        .filter(|entry| entry.path.ends_with("/metadata.json"))
    {
        let value: Value = read_document(&root.join(&entry.path))?;
        let variants = value
            .get("recipientVariants")
            .and_then(Value::as_array)
            .filter(|variants| !variants.is_empty())
            .ok_or("mirror generation has no variants")?;
        let parent = entry
            .path
            .rsplit_once('/')
            .ok_or("invalid mirror metadata path")?
            .0;
        for variant in variants {
            let filename = variant
                .get("filename")
                .and_then(Value::as_str)
                .filter(|name| crate::backup_mirror::variant_filename(name))
                .ok_or("unsafe mirror variant filename")?;
            let path = format!("{parent}/{filename}");
            let row = rows
                .get(path.as_str())
                .ok_or("missing mirror ciphertext variant")?;
            if row.kind != "file"
                || variant.get("ciphertextSha256").and_then(Value::as_str)
                    != Some(row.sha256.as_str())
            {
                return Err("mirror variant ciphertext hash mismatch".into());
            }
        }
    }
    Ok(())
}

struct TargetLock(File);
impl Drop for TargetLock {
    fn drop(&mut self) {
        let _ = crate::file_lock::unlock(&self.0);
    }
}

fn exclusive_file_lock(path: &Path) -> Result<TargetLock, String> {
    let parent = path.parent().ok_or("mirror lock has no parent")?;
    fs::create_dir_all(parent).map_err(error)?;
    if exists(path)? {
        let metadata = fs::symlink_metadata(path).map_err(error)?;
        if !metadata.is_file() {
            return Err("invalid mirror handoff lock file".into());
        }
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .map_err(error)?;
    crate::file_lock::lock_exclusive(&file).map_err(error)?;
    Ok(TargetLock(file))
}

fn target_lock(root: &Path) -> Result<TargetLock, String> {
    exclusive_file_lock(&sibling(root, ".native-import.lock")?)
}

fn target_workspace_lock(source: &Path, target: &Path) -> Result<Option<TargetLock>, String> {
    let parent = target.parent().ok_or("mirror target has no parent")?;
    if source.parent() == Some(parent) {
        return Ok(None);
    }
    // The source workspace gate already holds the process mutex. Lock the
    // second workspace at the OS level without nesting different-root gates.
    exclusive_file_lock(&crate::mutation_gate::lock_path(parent)).map(Some)
}

fn restore_fenced(root: &Path) -> Result<bool, String> {
    // Offline transfer never owns a restore. Any fence, including an invalid
    // or unreadable document, must block instead of being interpreted as absent.
    exists(&crate::mutation_gate::fence_path(root))
}

pub struct MutationGuard {
    _lock: TargetLock,
}

/// Serialize native writes with import and refuse every staged/revoked receipt.
/// A receipt parse error or an uninspectable path cannot restore write access.
pub fn mutation_guard(root: &Path) -> Result<MutationGuard, String> {
    let receipt = receipt_path(root)?;
    let source_fence = source_fence_path(root)?;
    let handback = sibling(root, ".native-handback.json")?;
    if exists(&handback)? {
        return Err("Backup mirror source was handed back to its original writer".into());
    }
    if exists(&source_fence)? {
        return Err("Backup mirror source is persistently fenced for native handoff".into());
    }
    if exists(&receipt)? {
        return Err("Backup mirror native handoff is awaiting ownership admission".into());
    }
    let lock = target_lock(root)?;
    if exists(&handback)? {
        return Err("Backup mirror source was handed back to its original writer".into());
    }
    if exists(&source_fence)? {
        return Err("Backup mirror source is persistently fenced for native handoff".into());
    }
    if exists(&receipt)? {
        return Err("Backup mirror native handoff is awaiting ownership admission".into());
    }
    Ok(MutationGuard { _lock: lock })
}

fn write_new_receipt(path: &Path, receipt: &ImportReceipt) -> Result<(), String> {
    let temporary = path.with_file_name(format!(
        ".{}.{}.{}.tmp",
        path.file_name().unwrap().to_string_lossy(),
        std::process::id(),
        TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(error)?;
    let result = (|| {
        file.write_all(dumps_compact(&serde_json::to_value(receipt).map_err(error)?).as_bytes())
            .map_err(error)?;
        file.sync_all().map_err(error)?;
        fs::hard_link(&temporary, path).map_err(error)?;
        fsync_dir(path.parent().unwrap());
        Ok(())
    })();
    drop(file);
    let _ = fs::remove_file(temporary);
    result
}

fn copy_temporary(path: &Path, transfer: &str) -> Result<PathBuf, String> {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or("invalid mirror copy path")?;
    Ok(path.with_file_name(format!(".{name}.native-copy-{transfer}.tmp")))
}

fn validate_partial_stage(stage: &Path, manifest: &ExportManifest) -> Result<(), String> {
    let mut permitted: BTreeMap<String, (&InventoryEntry, bool)> = BTreeMap::new();
    for entry in &manifest.entries {
        permitted.insert(entry.path.clone(), (entry, false));
        if entry.kind == "file" {
            let temporary = copy_temporary(Path::new(&entry.path), &manifest.transfer_id)?;
            permitted.insert(path_binding(&temporary)?, (entry, true));
        }
    }
    for observed in inventory(stage)? {
        let (expected, temporary) = permitted
            .get(&observed.path)
            .ok_or("unrelated data in reserved mirror staging directory")?;
        if *temporary {
            if observed.kind != "file" || observed.size > expected.size {
                return Err("invalid reserved mirror copy temporary".into());
            }
        } else if &observed != *expected {
            return Err("published staging entry differs from its reserved manifest".into());
        }
    }
    Ok(())
}

fn copy_file(
    source: &Path,
    target: &Path,
    entry: &InventoryEntry,
    transfer: &str,
) -> Result<(), String> {
    let temporary = copy_temporary(target, transfer)?;
    if exists(target)? {
        if !fs::symlink_metadata(target).map_err(error)?.is_file()
            || sha256_file(target).map_err(error)? != entry.sha256
        {
            return Err("mirror copy refuses to replace an unrelated target file".into());
        }
        if exists(&temporary)? {
            fs::remove_file(&temporary).map_err(error)?;
        }
        return Ok(());
    }
    if exists(&temporary)? && !fs::symlink_metadata(&temporary).map_err(error)?.is_file() {
        return Err("mirror copy temporary is not a regular file".into());
    }
    let mut input = File::open(source).map_err(error)?;
    let mut output = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&temporary)
        .map_err(error)?;
    let mut digest = Sha256::new();
    let mut size = 0u64;
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let count = input.read(&mut buffer).map_err(error)?;
        if count == 0 {
            break;
        }
        size = size
            .checked_add(count as u64)
            .ok_or("mirror copy size overflow")?;
        if size > entry.size {
            return Err("mirror source grew during copy".into());
        }
        digest.update(&buffer[..count]);
        output.write_all(&buffer[..count]).map_err(error)?;
    }
    if size != entry.size || format!("{:x}", digest.finalize()) != entry.sha256 {
        return Err("mirror source changed during copy".into());
    }
    output.sync_all().map_err(error)?;
    drop(output);
    fs::hard_link(&temporary, target).map_err(error)?;
    fsync_dir(target.parent().unwrap());
    fs::remove_file(temporary).map_err(error)?;
    Ok(())
}

fn expected_receipt(manifest: &ExportManifest, stage: &Path) -> Result<ImportReceipt, String> {
    let mut receipt = ImportReceipt {
        schema: RECEIPT_SCHEMA.into(),
        domain: DOMAIN.into(),
        phase: "copying".into(),
        transfer_id: manifest.transfer_id.clone(),
        source_root: manifest.source_root.clone(),
        target_root: manifest.target_root.clone(),
        staging_root: path_binding(stage)?,
        manifest_digest: manifest.manifest_digest.clone(),
        source_digest: manifest.source_digest.clone(),
        target_digest: String::new(),
        receipt_digest: String::new(),
    };
    receipt.receipt_digest = unsigned_digest(&receipt, "receiptDigest")?;
    Ok(receipt)
}

fn validate_receipt(receipt: &ImportReceipt, expected: &ImportReceipt) -> Result<(), String> {
    let mut normalized = receipt.clone();
    if !matches!(receipt.phase.as_str(), "copying" | "imported" | "revoked")
        || unsigned_digest(receipt, "receiptDigest")? != receipt.receipt_digest
        || (receipt.phase == "copying" && !receipt.target_digest.is_empty())
        || (receipt.phase != "copying" && receipt.target_digest != expected.source_digest)
    {
        return Err("invalid mirror import receipt or phase".into());
    }
    normalized.phase = expected.phase.clone();
    normalized.target_digest = expected.target_digest.clone();
    normalized.receipt_digest = unsigned_digest(&normalized, "receiptDigest")?;
    if normalized != *expected {
        return Err("mirror import receipt cannot be rebound or replaced".into());
    }
    Ok(())
}

pub fn import_inventory(
    manifest_path: &Path,
    source_root: &Path,
    target_root: &Path,
) -> Result<ImportReceipt, String> {
    let source = bound_path(source_root)?;
    let target = bound_path(target_root)?;
    let manifest_path = bound_path(manifest_path)?;
    if source.starts_with(&target)
        || target.starts_with(&source)
        || manifest_path.starts_with(&source)
        || manifest_path.starts_with(&target)
    {
        return Err(
            "mirror source, native target, and manifest must have independent paths".into(),
        );
    }
    let source_parent = source.parent().ok_or("mirror source has no parent")?;
    if !exists(&source)? {
        return Err("mirror source directory must exist".into());
    }
    let _source_gate = crate::mutation_gate::exclusive_gate(source_parent).map_err(error)?;
    if restore_fenced(source_parent)? {
        return Err("mirror import is fenced by workspace restore".into());
    }
    let manifest: ExportManifest = read_document(&manifest_path)?;
    validate_manifest(&manifest, &source, &target)?;
    let fence = source_fence_path(&source)?;
    let source_fence: ExportManifest = read_document(&fence)?;
    validate_manifest(&source_fence, &source, &target)?;
    if canonical_digest(&source_fence)? != canonical_digest(&manifest)? {
        return Err("mirror source fence differs from the export manifest".into());
    }
    attest(&source, &manifest)?;
    let _target_workspace_lock = target_workspace_lock(&source, &target)?;
    if restore_fenced(target.parent().ok_or("mirror target has no parent")?)? {
        return Err("mirror import is fenced by target workspace restore".into());
    }
    let receipt_path = receipt_path(&target)?;
    let stage = bound_path(&sibling(
        &target,
        &format!(".native-staging-{}", manifest.transfer_id),
    )?)?;
    let expected = expected_receipt(&manifest, &stage)?;
    let _target_lock = target_lock(&target)?;
    let mut receipt = if exists(&receipt_path)? {
        let receipt: ImportReceipt = read_document(&receipt_path)?;
        validate_receipt(&receipt, &expected)?;
        receipt
    } else {
        if exists(&target)? || exists(&stage)? {
            return Err(
                "mirror import refuses an existing target or unreserved staging directory".into(),
            );
        }
        write_new_receipt(&receipt_path, &expected)?;
        expected
    };
    if receipt.phase == "revoked" {
        return Err("native mirror candidate was revoked; import cannot reactivate it".into());
    }
    if exists(&target)? {
        if exists(&stage)? {
            return Err(
                "both mirror staging and published target exist; reconciliation required".into(),
            );
        }
        attest(&target, &manifest)?;
    } else {
        if receipt.phase == "imported" {
            return Err(
                "attested native mirror target is missing; no empty state is fabricated".into(),
            );
        }
        if exists(&stage)? {
            validate_partial_stage(&stage, &manifest)?;
        } else {
            fs::create_dir(&stage).map_err(error)?;
            fsync_dir(stage.parent().unwrap());
        }
        for entry in &manifest.entries {
            let destination = stage.join(&entry.path);
            if entry.kind == "directory" {
                fs::create_dir_all(&destination).map_err(error)?;
                fsync_dir(destination.parent().unwrap());
            } else {
                copy_file(
                    &source.join(&entry.path),
                    &destination,
                    entry,
                    &manifest.transfer_id,
                )?;
            }
        }
        attest(&stage, &manifest)?;
        let current_fence: ExportManifest = read_document(&fence)?;
        if canonical_digest(&current_fence)? != canonical_digest(&manifest)? {
            return Err("mirror source fence changed during copy".into());
        }
        attest(&source, &manifest)?;
        if exists(&target)? {
            return Err(
                "mirror target appeared before publication; reconciliation required".into(),
            );
        }
        fs::rename(&stage, &target).map_err(error)?;
        fsync_dir(target.parent().unwrap());
        attest(&target, &manifest)?;
    }
    if receipt.phase == "copying" {
        receipt.phase = "imported".into();
        receipt.target_digest = manifest.source_digest.clone();
        receipt.receipt_digest = unsigned_digest(&receipt, "receiptDigest")?;
        atomic_write(
            &receipt_path,
            dumps_compact(&serde_json::to_value(&receipt).map_err(error)?).as_bytes(),
        )
        .map_err(error)?;
        fsync_dir(receipt_path.parent().unwrap());
    }
    Ok(receipt)
}

/// Permanently revoke a settled, unchanged candidate before ownership admission.
/// Both inventories remain intact and fenced. Source handback is a separate,
/// independently verified offline operation; this cannot roll back active writes.
pub fn revoke_inventory(
    manifest_path: &Path,
    source_root: &Path,
    target_root: &Path,
) -> Result<ImportReceipt, String> {
    let source = bound_path(source_root)?;
    let target = bound_path(target_root)?;
    let manifest_path = bound_path(manifest_path)?;
    if source.starts_with(&target)
        || target.starts_with(&source)
        || manifest_path.starts_with(&source)
        || manifest_path.starts_with(&target)
    {
        return Err(
            "mirror source, native target, and manifest must have independent paths".into(),
        );
    }
    let source_parent = source.parent().ok_or("mirror source has no parent")?;
    if !exists(&source)? {
        return Err("mirror source directory must exist".into());
    }
    let _source_gate = crate::mutation_gate::exclusive_gate(source_parent).map_err(error)?;
    if restore_fenced(source_parent)? {
        return Err("mirror revocation is fenced by workspace restore".into());
    }
    let manifest: ExportManifest = read_document(&manifest_path)?;
    validate_manifest(&manifest, &source, &target)?;
    let stage = bound_path(&sibling(
        &target,
        &format!(".native-staging-{}", manifest.transfer_id),
    )?)?;
    let expected = expected_receipt(&manifest, &stage)?;
    let path = receipt_path(&target)?;
    let _target_workspace_lock = target_workspace_lock(&source, &target)?;
    if restore_fenced(target.parent().ok_or("mirror target has no parent")?)? {
        return Err("mirror revocation is fenced by target workspace restore".into());
    }
    let _target_lock = target_lock(&target)?;
    let mut receipt: ImportReceipt = read_document(&path)?;
    validate_receipt(&receipt, &expected)?;
    if receipt.phase == "copying" {
        return Err("unfinished mirror copy cannot be revoked as an attested candidate".into());
    }
    if exists(&stage)? {
        return Err("mirror staging state still exists; reconciliation required".into());
    }
    attest(&target, &manifest)?;
    if receipt.phase == "revoked" {
        return Ok(receipt);
    }
    let source_fence: ExportManifest = read_document(&source_fence_path(&source)?)?;
    validate_manifest(&source_fence, &source, &target)?;
    if canonical_digest(&source_fence)? != canonical_digest(&manifest)? {
        return Err("mirror source fence differs from the export manifest".into());
    }
    attest(&source, &manifest)?;
    receipt.phase = "revoked".into();
    receipt.receipt_digest = unsigned_digest(&receipt, "receiptDigest")?;
    atomic_write(
        &path,
        dumps_compact(&serde_json::to_value(&receipt).map_err(error)?).as_bytes(),
    )
    .map_err(error)?;
    fsync_dir(path.parent().unwrap());
    Ok(receipt)
}
