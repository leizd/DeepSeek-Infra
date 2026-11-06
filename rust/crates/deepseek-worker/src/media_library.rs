//! Media library publication planning. No authority is granted by this module.
use crate::{
    media_image::{Image, LibrarySource},
    media_records::Records,
    media_values::invalid,
};
use deepseek_policy::{
    app_error::{AppError, codes},
    media_records::retained::{MediaRecord, PublicRecordError},
    workspace_schema::validate_workspace_id,
};
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaFlatPatchRecord, MediaMutationKind, MediaPatchClaim, MediaPatchEffectState,
    MediaPatchOutcome,
};
use deepseek_protocol::{media_patch_result as patch_wire, media_server_error};
use prost::Message as _;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
};
use tonic::Status;

#[cfg(test)]
use deepseek_policy::media_schema::normalize_media_record;

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

pub(super) type LibraryPlan = (
    MediaPatchOutcome,
    Option<Image>,
    Option<MediaFlatPatchRecord>,
);

pub(super) fn plan(
    root: &Path,
    claim: &MediaPatchClaim,
    before: &(impl LibrarySource + ?Sized),
) -> Result<LibraryPlan, Status> {
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
                    None,
                ));
            }
        };
        let mut items = normalized_records(before)?;
        if items.remove_all(&safe)? == 0 {
            // Missing records do not clean orphan files, normalize the store or
            // advance its generation. Preserve the original delete=0 behavior.
            result.after_sha256 = before.identity().sha256;
            return Ok((result, None, None));
        }
        crate::media_delete::preflight(root, &safe)?;
        if media_server_error::negotiated(intent) && items.has_unencodable_retained_text()? {
            return Ok((
                server_error(result, MediaPatchEffectState::Refused),
                None,
                None,
            ));
        }
        result.deleted_count = 1;
        let (result, image) = planned_bytes(claim, result, items)?;
        return Ok((result, image, None));
    }
    if intent.kind != MediaMutationKind::Unspecified as i32 || intent.patch.is_none() {
        return Err(invalid());
    }
    let safe = match validate_workspace_id(&intent.media_id, "media id") {
        Ok(v) => v,
        Err(e) => {
            return Ok((
                refusal(result, e.status as u32, e.code, &e.message),
                None,
                None,
            ));
        }
    };
    let mut items = normalized_records(before)?;
    let Some((index, existing)) = items.first_retained(&safe)? else {
        return Ok((
            refusal(result, 404, codes::NOT_FOUND, "Media not found"),
            None,
            None,
        ));
    };
    let stored = match prepare_media(root, claim, &existing) {
        Ok(value) => value,
        Err(e) => {
            return Ok((
                refusal(result, e.status as u32, e.code, &e.message),
                None,
                None,
            ));
        }
    };
    items.replace_retained(index, &stored)?;
    let negotiated = media_server_error::negotiated(intent);
    if negotiated && items.has_unencodable_retained_text()? {
        return Ok((
            server_error(result, MediaPatchEffectState::Refused),
            None,
            None,
        ));
    }
    let public = match stored.normalize_again() {
        Ok(public) => public,
        Err(e) => {
            return Ok((
                refusal(result, e.status as u32, e.code, &e.message),
                None,
                None,
            ));
        }
    };
    let flat_negotiated = patch_wire::negotiated(intent, patch_wire::FLAT_VERSION);
    let record = match public.public_patch_object() {
        Ok(media) => {
            result.media = Some(media);
            // An inline receipt must also survive the journal's protobuf
            // decoder. Its nesting budget can be lower than the value limit.
            if flat_negotiated
                && result.encoded_len() <= crate::media_patch_result::INLINE_LIMIT
                && MediaPatchOutcome::decode(result.encode_to_vec().as_slice()).is_err()
            {
                result.media = None;
                public.flat_patch_record().map(Some)
            } else {
                Ok(None)
            }
        }
        Err(PublicRecordError::TooDeep) if flat_negotiated => public.flat_patch_record().map(Some),
        Err(error) => Err(error),
    };
    let flat = match record {
        Ok(record) => record,
        Err(PublicRecordError::Unencodable) if negotiated => {
            result = server_error(result, MediaPatchEffectState::Applied);
            None
        }
        Err(PublicRecordError::TooDeep)
            if negotiated && public.write_response(&mut std::io::sink()).is_err() =>
        {
            result = server_error(result, MediaPatchEffectState::Applied);
            None
        }
        Err(PublicRecordError::TooLarge) => {
            return Err(Status::resource_exhausted("MEDIA_MESSAGE_LIMIT"));
        }
        Err(_) => return Err(invalid()),
    };
    let (result, image) = planned_bytes(claim, result, items)?;
    Ok((result, image, flat))
}

fn server_error(mut result: MediaPatchOutcome, state: MediaPatchEffectState) -> MediaPatchOutcome {
    result.schema_version = media_server_error::VERSION;
    result.state = state as i32;
    result.http_status = 500;
    result.error_code = media_server_error::CODE.into();
    result.error_message = media_server_error::MESSAGE.into();
    result
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
    existing: &MediaRecord,
) -> Result<MediaRecord, AppError> {
    let bad = || AppError::invalid_payload("Invalid media update");
    let intent = claim.intent.as_ref().ok_or_else(bad)?;
    let safe = validate_workspace_id(&intent.media_id, "media id")?;
    let patch = intent.patch.as_ref().ok_or_else(bad)?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| bad())?
        .as_secs() as i64;
    let normalized = existing.apply_patch(patch, &deepseek_policy::core_utils::utc_now_iso(now))?;
    let path = normalized.object_path()?;
    if !path.is_empty() {
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
    Ok(normalized)
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
