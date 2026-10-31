//! Media library publication planning. No authority is granted by this module.
use crate::media_values::{from_json_legacy, invalid, to_json};
use deepseek_policy::{
    app_error::{AppError, codes},
    media_schema::normalize_media_record,
    workspace_schema::validate_workspace_id,
};
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaMutationKind, MediaPatchClaim, MediaPatchEffectState, MediaPatchOutcome, media_value::Kind,
};
use prost::Message as _;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};
use tonic::Status;

pub(super) const MAX_LIBRARY_BYTES: u64 = 128 * 1024 * 1024;
pub(super) fn reject_links(path: &Path) -> Result<(), Status> {
    for ancestor in path.ancestors() {
        match fs::symlink_metadata(ancestor) {
            Ok(m) => {
                #[cfg(windows)]
                {
                    use std::os::windows::fs::MetadataExt;
                    if m.file_attributes() & 0x400 != 0 {
                        return Err(invalid());
                    }
                }
                if m.file_type().is_symlink() {
                    return Err(invalid());
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(invalid()),
        }
    }
    Ok(())
}
pub(super) fn library_path(root: &Path) -> PathBuf {
    root.join(".media").join("library.json")
}
pub(super) fn read_library(root: &Path) -> Result<Vec<u8>, Status> {
    let path = library_path(root);
    reject_links(&path)?;
    let file = match fs::File::open(&path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(_) => return Err(invalid()),
    };
    if !file.metadata().map_err(|_| invalid())?.is_file() {
        return Err(invalid());
    }
    let mut bytes = Vec::new();
    file.take(MAX_LIBRARY_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| invalid())?;
    if bytes.len() as u64 > MAX_LIBRARY_BYTES {
        return Err(Status::resource_exhausted("MEDIA_LIBRARY_LIMIT"));
    }
    reject_links(&path)?;
    Ok(bytes)
}
pub(super) fn sha(bytes: &[u8]) -> Vec<u8> {
    Sha256::digest(bytes).to_vec()
}
pub(super) fn refusal(
    mut result: MediaPatchOutcome,
    status: u32,
    code: &str,
    message: &str,
) -> MediaPatchOutcome {
    result.state = MediaPatchEffectState::Refused as i32;
    result.http_status = status;
    result.media = None;
    result.after_sha256.clear();
    result.error_code = code.into();
    result.error_message = message.into();
    result.signature.clear();
    result.deleted_count = 0;
    result.created = None;
    result.processed = None;
    result
}
pub(super) fn outcome(claim: &MediaPatchClaim, before: &[u8]) -> Result<MediaPatchOutcome, Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    Ok(MediaPatchOutcome {
        schema_version: 1,
        operation_id: intent.operation_id.clone(),
        original_fence: claim.fence.clone(),
        request_sha256: claim.request_sha256.clone(),
        state: MediaPatchEffectState::Applied as i32,
        media: None,
        http_status: 200,
        error_code: String::new(),
        error_message: String::new(),
        before_sha256: sha(before),
        after_sha256: Vec::new(),
        signature: Vec::new(),
        fleet_id: claim.fleet_id.clone(),
        environment: claim.environment.clone(),
        writer_fencing_token: claim.writer_fencing_token,
        deleted_count: 0,
        created: None,
        processed: None,
    })
}

pub(super) fn plan(
    root: &Path,
    claim: &MediaPatchClaim,
    before: &[u8],
) -> Result<(MediaPatchOutcome, Option<Vec<u8>>), Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    let mut result = outcome(claim, before)?;
    if intent.kind == MediaMutationKind::Delete as i32 {
        if intent.patch.is_some() {
            return Err(invalid());
        }
        let safe = match validate_workspace_id(&intent.media_id, "media id") {
            Ok(value) => value,
            Err(error) => {
                return Ok((
                    refusal(result, error.status as u32, error.code, &error.message),
                    None,
                ));
            }
        };
        let mut items = normalized_items(before);
        let length = items.len();
        items.retain(|item| item["mediaId"].as_str() != Some(safe.as_str()));
        if items.len() == length {
            // Missing records do not clean orphan files, normalize the store or
            // advance its generation. Preserve the original delete=0 behavior.
            result.after_sha256 = sha(before);
            return Ok((result, None));
        }
        crate::media_delete::preflight(root, &safe)?;
        if items.len() > 1000 {
            items.drain(..items.len() - 1000);
        }
        result.deleted_count = 1;
        return planned_bytes(claim, result, items);
    }
    if intent.kind != MediaMutationKind::Unspecified as i32 || intent.patch.is_none() {
        return Err(invalid());
    }
    let prepared = prepare_media(root, claim, before);
    let (public, items) = match prepared {
        Ok(v) => v,
        Err(error) => {
            return Ok((
                refusal(result, error.status as u32, error.code, &error.message),
                None,
            ));
        }
    };
    let value = from_json_legacy(&public)?;
    result.media = match value.kind {
        Some(Kind::ObjectValue(object)) => Some(object),
        _ => return Err(invalid()),
    };
    planned_bytes(claim, result, items)
}

pub(super) fn planned_bytes(
    claim: &MediaPatchClaim,
    mut result: MediaPatchOutcome,
    items: Vec<Value>,
) -> Result<(MediaPatchOutcome, Option<Vec<u8>>), Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    // This internal commit witness is ignored by original public readers. It
    // proves publication of this exact original operation after a process death;
    // mere absence or equality with the old file can never prove non-application.
    let document = json!({"schemaVersion":"media-library.v1","media":items,"nativePublication":{"schemaVersion":1,"operationId":intent.operation_id,"actionId":claim.fence.as_ref().ok_or_else(invalid)?.action_id,"executionEpoch":claim.fence.as_ref().ok_or_else(invalid)?.execution_epoch,"requestSha256":claim.request_sha256}});
    let bytes = serde_json::to_vec_pretty(&document).map_err(|_| invalid())?;
    if bytes.len() as u64 > MAX_LIBRARY_BYTES {
        return Err(Status::resource_exhausted("MEDIA_LIBRARY_LIMIT"));
    }
    result.after_sha256 = sha(&bytes);
    if result.encoded_len() > 4 * 1024 * 1024 {
        return Err(Status::resource_exhausted("MEDIA_MESSAGE_LIMIT"));
    }
    Ok((result, Some(bytes)))
}

pub(super) fn normalized_items(before: &[u8]) -> Vec<Value> {
    let document: Value = serde_json::from_slice(before).unwrap_or(Value::Null);
    document
        .get("media")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|value| value.is_object())
        .filter_map(|value| normalize_media_record(value).ok())
        .collect()
}
fn prepare_media(
    root: &Path,
    claim: &MediaPatchClaim,
    before: &[u8],
) -> Result<(Value, Vec<Value>), AppError> {
    let bad = || AppError::invalid_payload("Invalid media update");
    let intent = claim.intent.as_ref().ok_or_else(bad)?;
    let safe = validate_workspace_id(&intent.media_id, "media id")?;
    let mut items = normalized_items(before);
    let index = items
        .iter()
        .position(|v| v["mediaId"].as_str() == Some(&safe))
        .ok_or_else(|| AppError {
            status: 404,
            code: codes::NOT_FOUND,
            message: "Media not found".into(),
        })?;
    let mut merged = items[index].clone();
    let patch = intent.patch.as_ref().ok_or_else(bad)?;
    for (key, value) in [
        ("title", patch.title.as_ref()),
        ("projectId", patch.project_id.as_ref()),
    ] {
        if let Some(value) = value {
            merged[key] = to_json(value).map_err(|_| bad())?;
        }
    }
    if let Some(metadata) = patch.metadata.as_ref() {
        let new = to_json(metadata).map_err(|_| bad())?;
        if let Some(o) = new.as_object() {
            let existing = merged["metadata"].as_object_mut().ok_or_else(bad)?;
            for (k, v) in o {
                existing.insert(k.clone(), v.clone());
            }
        }
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| bad())?
        .as_secs() as i64;
    merged["updatedAt"] = Value::String(deepseek_policy::core_utils::utc_now_iso(now));
    let normalized = normalize_media_record(&merged)?;
    if let Some(path) = normalized["path"].as_str().filter(|p| !p.is_empty()) {
        let mut parts = path.split('/');
        if parts.next() != Some("objects") || parts.next() != Some(safe.as_str()) {
            return Err(AppError::invalid_payload(
                "Media source path must point to an object stored in the media library",
            ));
        }
        reject_links(&root.join(".media").join(path)).map_err(|_| {
            AppError::invalid_payload("Media file must stay inside the media library")
        })?;
    }
    items[index] = normalized.clone();
    if items.len() > 1000 {
        items.drain(..items.len() - 1000);
    }
    Ok((normalize_media_record(&normalized)?, items))
}
fn sync_published(file: &fs::File, directory: &Path) -> Result<(), Status> {
    file.sync_all().map_err(|_| invalid())?;
    #[cfg(unix)]
    fs::File::open(directory)
        .and_then(|f| f.sync_all())
        .map_err(|_| invalid())?;
    #[cfg(not(unix))]
    let _ = directory;
    Ok(())
}

// The journal caller holds the exclusive mutation gate and has already matched
// the original after-image. Synchronize that retained file, never republish it.
pub(super) fn confirm_publication(root: &Path, expected: &[u8]) -> Result<(), Status> {
    let path = library_path(root);
    let directory = path.parent().ok_or_else(invalid)?;
    reject_links(&path)?;
    let mut options = fs::OpenOptions::new();
    options.read(true);
    // Windows FlushFileBuffers requires a handle with GENERIC_WRITE, even
    // though this recovery path never writes or truncates any file bytes.
    #[cfg(windows)]
    options.write(true);
    let file = options.open(&path).map_err(|_| invalid())?;
    if !file.metadata().map_err(|_| invalid())?.is_file() {
        return Err(invalid());
    }
    let mut retained = Vec::new();
    (&file)
        .take(MAX_LIBRARY_BYTES + 1)
        .read_to_end(&mut retained)
        .map_err(|_| invalid())?;
    if retained.len() as u64 > MAX_LIBRARY_BYTES || retained.as_slice() != expected {
        return Err(Status::unavailable("EFFECT_UNKNOWN"));
    }
    sync_published(&file, directory)?;
    if read_library(root)? != expected {
        return Err(Status::unavailable("EFFECT_UNKNOWN"));
    }
    Ok(())
}

pub(super) fn publish(root: &Path, bytes: &[u8], deadline: Option<i64>) -> Result<bool, Status> {
    let path = library_path(root);
    let directory = path.parent().ok_or_else(invalid)?;
    reject_links(&path)?;
    fs::create_dir_all(directory).map_err(|_| invalid())?;
    reject_links(&path)?;
    // tempfile 3.27 persist atomically replaces an existing destination. Its
    // contract requires explicit file and containing-directory synchronization:
    // https://docs.rs/tempfile/3.27.0/tempfile/struct.NamedTempFile.html#method.persist
    let mut temporary = tempfile::NamedTempFile::new_in(directory).map_err(|_| invalid())?;
    temporary.write_all(bytes).map_err(|_| invalid())?;
    temporary.as_file().sync_all().map_err(|_| invalid())?;
    reject_links(&path)?;
    if let Some(deadline) = deadline {
        let now = crate::service::unix_now_seconds().map_err(|_| invalid())?;
        if now >= deadline {
            return Ok(false);
        }
    }
    let file = temporary.persist(&path).map_err(|_| invalid())?;
    sync_published(&file, directory)?;
    if read_library(root)? != bytes {
        return Err(Status::unavailable("EFFECT_UNKNOWN"));
    }
    Ok(true)
}
