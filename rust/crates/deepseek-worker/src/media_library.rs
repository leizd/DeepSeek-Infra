//! Media library publication planning. No authority is granted by this module.
use crate::{
    media_image::{Image, LibrarySource},
    media_records::Records,
    media_values::{from_json_legacy, invalid, to_json},
};
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
    io::Write,
    path::{Path, PathBuf},
};
use tonic::Status;

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
pub(super) fn read_snapshot(root: &Path) -> Result<Image, Status> {
    Image::capture(&library_path(root))
}
// Legacy row/test adapter. Current publication and reconciliation use images.
#[cfg(test)]
pub(super) fn read_library(root: &Path) -> Result<Vec<u8>, Status> {
    use std::io::Read as _;
    let image = read_snapshot(root)?;
    let mut bytes = Vec::new();
    image
        .reader()?
        .read_to_end(&mut bytes)
        .map_err(|_| invalid())?;
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
    // A refusal is the frozen v1 no-effect envelope, including when its
    // proposed successful result used the compact v2 PATCH representation.
    result.schema_version = 1;
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
    result.patched = None;
    result
}
pub(super) fn outcome(
    claim: &MediaPatchClaim,
    before: &(impl LibrarySource + ?Sized),
) -> Result<MediaPatchOutcome, Status> {
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
        before_sha256: before.identity().sha256,
        after_sha256: Vec::new(),
        signature: Vec::new(),
        fleet_id: claim.fleet_id.clone(),
        environment: claim.environment.clone(),
        writer_fencing_token: claim.writer_fencing_token,
        deleted_count: 0,
        created: None,
        processed: None,
        patched: None,
    })
}

pub(super) fn plan(
    root: &Path,
    claim: &MediaPatchClaim,
    before: &(impl LibrarySource + ?Sized),
) -> Result<(MediaPatchOutcome, Option<Image>), Status> {
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
        let mut items = normalized_records(before)?;
        if items.remove_all(&safe)? == 0 {
            // Missing records do not clean orphan files, normalize the store or
            // advance its generation. Preserve the original delete=0 behavior.
            result.after_sha256 = before.identity().sha256;
            return Ok((result, None));
        }
        crate::media_delete::preflight(root, &safe)?;
        result.deleted_count = 1;
        return planned_bytes(claim, result, items);
    }
    if intent.kind != MediaMutationKind::Unspecified as i32 || intent.patch.is_none() {
        return Err(invalid());
    }
    let safe = match validate_workspace_id(&intent.media_id, "media id") {
        Ok(v) => v,
        Err(e) => return Ok((refusal(result, e.status as u32, e.code, &e.message), None)),
    };
    let mut items = normalized_records(before)?;
    let Some((index, existing)) = items.first(&safe)? else {
        return Ok((
            refusal(result, 404, codes::NOT_FOUND, "Media not found"),
            None,
        ));
    };
    let (public, stored) = match prepare_media(root, claim, &existing) {
        Ok(value) => value,
        Err(e) => return Ok((refusal(result, e.status as u32, e.code, &e.message), None)),
    };
    items.replace(index, &stored)?;
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
    items: Records,
) -> Result<(MediaPatchOutcome, Option<Image>), Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    let fence = claim.fence.as_ref().ok_or_else(invalid)?;
    let publication = json!({"schemaVersion":1,"operationId":intent.operation_id,"actionId":fence.action_id,"executionEpoch":fence.execution_epoch,"requestSha256":claim.request_sha256});
    let image = image_from_records(&items, &publication)?;
    result.after_sha256 = image.identity().sha256;
    if result.media.is_none() && result.encoded_len() > 4 * 1024 * 1024 {
        return Err(Status::resource_exhausted("MEDIA_MESSAGE_LIMIT"));
    }
    Ok((result, Some(image)))
}

pub(super) fn image_from_records(items: &Records, publication: &Value) -> Result<Image, Status> {
    Image::build(|out| {
        out.write_all(br#"{"schemaVersion":"media-library.v1","media":"#)
            .map_err(|_| invalid())?;
        items.write_retained_array(out)?;
        out.write_all(br#","nativePublication":"#)
            .map_err(|_| invalid())?;
        serde_json::to_writer(&mut *out, &publication).map_err(|_| invalid())?;
        out.write_all(b"}").map_err(|_| invalid())?;
        Ok(())
    })
}

pub(super) fn normalized_records(
    before: &(impl LibrarySource + ?Sized),
) -> Result<Records, Status> {
    Records::from_reader(before.reader()?)
}
// Only compatibility tests need a materialized normalized array.
#[cfg(test)]
pub(super) fn normalized_items(before: &[u8]) -> Vec<Value> {
    let document: Value = serde_json::from_slice(before).unwrap_or(Value::Null);
    document
        .get("media")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|v| v.is_object())
        .filter_map(|v| normalize_media_record(v).ok())
        .collect()
}
fn prepare_media(
    root: &Path,
    claim: &MediaPatchClaim,
    existing: &Value,
) -> Result<(Value, Value), AppError> {
    let bad = || AppError::invalid_payload("Invalid media update");
    let intent = claim.intent.as_ref().ok_or_else(bad)?;
    let safe = validate_workspace_id(&intent.media_id, "media id")?;
    let mut merged = existing.clone();
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
    Ok((normalize_media_record(&normalized)?, normalized))
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
pub(super) fn confirm_publication(
    root: &Path,
    expected: &(impl LibrarySource + ?Sized),
) -> Result<(), Status> {
    let path = library_path(root);
    let directory = path.parent().ok_or_else(invalid)?;
    reject_links(&path)?;
    let mut options = fs::OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    options.write(true);
    let file = options.open(&path).map_err(|_| invalid())?;
    if !file.metadata().map_err(|_| invalid())?.is_file() {
        return Err(invalid());
    }
    if !crate::media_image::same(&read_snapshot(root)?, expected)? {
        return Err(Status::unavailable("EFFECT_UNKNOWN"));
    }
    sync_published(&file, directory)?;
    if !crate::media_image::same(&read_snapshot(root)?, expected)? {
        return Err(Status::unavailable("EFFECT_UNKNOWN"));
    }
    Ok(())
}

pub(super) fn publish(
    root: &Path,
    bytes: &(impl LibrarySource + ?Sized),
    deadline: Option<i64>,
) -> Result<bool, Status> {
    let path = library_path(root);
    let directory = path.parent().ok_or_else(invalid)?;
    reject_links(&path)?;
    crate::media_image::ensure_parent(&path)?;
    reject_links(&path)?;
    let mut temporary = tempfile::NamedTempFile::new_in(directory).map_err(|_| invalid())?;
    std::io::copy(&mut bytes.reader()?, temporary.as_file_mut()).map_err(|_| invalid())?;
    temporary.as_file().sync_all().map_err(|_| invalid())?;
    reject_links(&path)?;
    // The final deadline check remains immediately before atomic replacement.
    if let Some(deadline) = deadline {
        if crate::service::unix_now_seconds().map_err(|_| invalid())? >= deadline {
            return Ok(false);
        }
    }
    let file = temporary.persist(&path).map_err(|_| invalid())?;
    sync_published(&file, directory)?;
    if !crate::media_image::same(&read_snapshot(root)?, bytes)? {
        return Err(Status::unavailable("EFFECT_UNKNOWN"));
    }
    Ok(true)
}
