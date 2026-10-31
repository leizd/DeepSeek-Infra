//! Standalone processing retains the existing identity and source. It never
//! stages a new CREATE or repairs a missing source by copying retained bytes.
use crate::{media_create, media_library, media_process_index, media_processing, media_values};
use deepseek_policy::{
    core_utils::{python_truthy, text_or_empty},
    media_schema::normalize_media_path,
    workspace_schema::validate_workspace_id,
};
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaArray, MediaCreatePreparation, MediaMutationKind, MediaObject, MediaPatchClaim,
    MediaPatchOutcome, MediaProcessPreparation, MediaProcessedResult, MediaValue,
    ReadMediaCreateResultResponse, media_value::Kind,
};
use prost::Message as _;
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};
use tonic::Status;

const MAX_PREPARATION_BYTES: usize = 4 * media_create::MAX_PAYLOAD_BYTES;
fn unknown() -> Status {
    Status::unavailable("EFFECT_UNKNOWN")
}
fn invalid() -> Status {
    media_values::invalid()
}
fn object(value: &Value) -> Result<MediaObject, Status> {
    match media_values::from_json_legacy(value)?.kind {
        Some(Kind::ObjectValue(o)) => Ok(o),
        _ => Err(invalid()),
    }
}
fn value(record: &MediaObject) -> Result<Value, Status> {
    media_values::to_json(&MediaValue {
        kind: Some(Kind::ObjectValue(record.clone())),
    })
}

fn directory(staging: &Path, operation: &str) -> Result<PathBuf, Status> {
    let nonce = operation
        .strip_prefix("media-process-")
        .ok_or_else(invalid)?;
    if nonce.len() != 32
        || !nonce
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return Err(invalid());
    }
    let path = staging.join(operation);
    media_library::reject_links(&path)?;
    Ok(path)
}

// Match which original processors actually read a source. Audio/video and a
// webpage with imported text must not acquire a new dependency on a source file.
fn source_relative(record: &Value) -> Result<String, Status> {
    let metadata = &record["metadata"];
    let reads = match record["type"].as_str().unwrap_or("") {
        "image" | "screenshot" | "pdf" => true,
        "webpage" => metadata
            .get("webpageText")
            .filter(|v| python_truthy(v))
            .or_else(|| metadata.get("text").filter(|v| python_truthy(v)))
            .is_none_or(|v| text_or_empty(Some(v)).trim().is_empty()),
        _ => false,
    };
    if !reads {
        return Ok(String::new());
    }
    let relative = normalize_media_path(record.get("path")).map_err(|_| invalid())?;
    Ok(if relative.is_empty() {
        format!(
            "objects/{}",
            record["mediaId"].as_str().ok_or_else(invalid)?
        )
    } else {
        relative
    })
}

fn source(root: &Path, relative: &str) -> Result<(bool, Vec<u8>), Status> {
    if relative.is_empty() {
        return Ok((false, vec![]));
    }
    let normalized = normalize_media_path(Some(&json!(relative))).map_err(|_| invalid())?;
    if normalized != relative {
        return Err(invalid());
    }
    let path = root.join(".media").join(relative);
    media_library::reject_links(&path)?;
    if !path.is_file() {
        return Ok((false, vec![]));
    }
    Ok((
        true,
        media_create::bounded_read_limited(&path, media_create::MAX_PAYLOAD_BYTES)?,
    ))
}

fn file_state(path: &Path) -> Result<Option<Vec<u8>>, Status> {
    media_library::reject_links(path).map_err(|_| unknown())?;
    if !path.exists() {
        return Ok(None);
    }
    if !path.is_file() {
        return Err(unknown());
    }
    media_create::bounded_read_limited(path, media_create::MAX_PAYLOAD_BYTES)
        .map(Some)
        .map_err(|_| unknown())
}

fn segment_bytes(segments: &MediaArray) -> Result<Vec<u8>, Status> {
    let values = media_values::to_json(&MediaValue {
        kind: Some(Kind::ArrayValue(segments.clone())),
    })?;
    serde_json::to_vec_pretty(&json!({"segments":values})).map_err(|_| unknown())
}

pub(super) fn plan(
    root: &Path,
    staging: &Path,
    claim: &MediaPatchClaim,
    before: &[u8],
) -> Result<(MediaPatchOutcome, Option<Vec<u8>>), Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    let options = intent.process.as_ref().ok_or_else(invalid)?;
    if intent.kind != MediaMutationKind::Process as i32
        || intent.patch.is_some()
        || intent.create.is_some()
    {
        return Err(invalid());
    }
    directory(staging, &intent.operation_id)?;
    let mut outcome = media_library::outcome(claim, before)?;
    let id = match validate_workspace_id(&intent.media_id, "media id") {
        Ok(id) => id,
        Err(e) => {
            return Ok((
                media_library::refusal(outcome, e.status as u32, e.code, &e.message),
                None,
            ));
        }
    };
    let mut items = media_library::normalized_items(before);
    let Some(position) = items
        .iter()
        .position(|v| v["mediaId"].as_str() == Some(&id))
    else {
        return Ok((
            media_library::refusal(outcome, 404, "not_found", "Media not found"),
            None,
        ));
    };
    let original = &items[position];
    let relative = source_relative(original)?;
    let (source_present, source_bytes) = source(root, &relative)?;
    let source_sha256 = media_library::sha(&source_bytes);
    let now = crate::service::unix_now_seconds().map_err(|_| invalid())?;
    let mut effects = media_processing::prepare_with_ocr(
        MediaCreatePreparation {
            record: Some(object(original)?),
            source_bytes,
            ..Default::default()
        },
        now,
        options.ocr_enabled.unwrap_or(false),
    )?;
    // Source content is read-only input, not a CREATE effect to republish.
    effects.source_bytes.clear();
    let effect = effects.processing.as_mut().ok_or_else(invalid)?;
    effect.force = options.force;
    let segments = effect.segments.clone().unwrap_or_default();
    let prior = file_state(&root.join(".media/segments").join(format!("{id}.json")))?;
    let before_index_items = media_process_index::snapshot(
        root,
        &id,
        original["projectId"].as_str().unwrap_or(""),
        effect,
    )?;
    outcome.http_status = effect.final_http_status;
    outcome.error_code = effect.final_error_code.clone();
    outcome.error_message = effect.final_error_message.clone();
    let indexed = if effect.final_http_status == 200 {
        effect.index_items.len() as u32
    } else {
        0
    };
    let prepared = MediaProcessPreparation {
        effects: Some(effects),
        before_segments: prior.clone().unwrap_or_default(),
        before_segments_present: prior.is_some(),
        before_index_items,
        source_relative_path: relative,
        source_present,
        source_sha256,
        result_segments: Some(segments),
    };
    let bytes = prepared.encode_to_vec();
    let record = prepared
        .effects
        .as_ref()
        .and_then(|p| p.record.as_ref())
        .ok_or_else(invalid)?;
    let record_bytes = record.encode_to_vec();
    let segments_bytes = prepared
        .result_segments
        .as_ref()
        .ok_or_else(invalid)?
        .encode_to_vec();
    outcome.processed = Some(MediaProcessedResult {
        record_sha256: media_library::sha(&record_bytes),
        record_bytes: record_bytes.len() as u64,
        preparation_sha256: media_library::sha(&bytes),
        original_fence: claim.fence.clone(),
        segments_sha256: media_library::sha(&segments_bytes),
        segments_bytes: segments_bytes.len() as u64,
        indexed,
    });
    media_create::persist_bound_limited(
        &directory(staging, &intent.operation_id)?.join("process.pb"),
        &bytes,
        MAX_PREPARATION_BYTES,
    )?;
    items[position] = value(record)?;
    if items.len() > 1000 {
        items.drain(..items.len() - 1000);
    }
    media_library::planned_bytes(claim, outcome, items)
}

pub(super) fn retained(
    staging: &Path,
    claim: &MediaPatchClaim,
    outcome: &MediaPatchOutcome,
) -> Result<MediaProcessPreparation, Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    let binding = outcome.processed.as_ref().ok_or_else(invalid)?;
    if intent.kind != MediaMutationKind::Process as i32
        || intent.process.is_none()
        || intent.patch.is_some()
        || intent.create.is_some()
        || outcome.created.is_some()
        || outcome.media.is_some()
        || outcome.deleted_count != 0
        || binding.original_fence != claim.fence
    {
        return Err(invalid());
    }
    let bytes = media_create::bounded_read_limited(
        &directory(staging, &intent.operation_id)?.join("process.pb"),
        MAX_PREPARATION_BYTES,
    )?;
    if media_library::sha(&bytes) != binding.preparation_sha256 {
        return Err(invalid());
    }
    let prepared = MediaProcessPreparation::decode(bytes.as_slice()).map_err(|_| invalid())?;
    if prepared.encode_to_vec() != bytes
        || (!prepared.before_segments_present && !prepared.before_segments.is_empty())
        || prepared.source_sha256.len() != 32
    {
        return Err(invalid());
    }
    let effects = prepared.effects.as_ref().ok_or_else(invalid)?;
    let record = effects.record.as_ref().ok_or_else(invalid)?;
    let effect = effects.processing.as_ref().ok_or_else(invalid)?;
    let segments = prepared.result_segments.as_ref().ok_or_else(invalid)?;
    let record_bytes = record.encode_to_vec();
    let segment_bytes = segments.encode_to_vec();
    if record_bytes.len() as u64 != binding.record_bytes
        || media_library::sha(&record_bytes) != binding.record_sha256
        || segment_bytes.len() as u64 != binding.segments_bytes
        || media_library::sha(&segment_bytes) != binding.segments_sha256
        || !effects.source_bytes.is_empty()
        || !effects.source_suffix.is_empty()
    {
        return Err(invalid());
    }
    if prepared.source_relative_path
        != source_relative(&value(
            effect.processing_record.as_ref().ok_or_else(invalid)?,
        )?)?
        || (!prepared.source_present && prepared.source_sha256 != media_library::sha(&[]))
    {
        return Err(invalid());
    }
    media_processing::validate(effects, claim, outcome)?;
    if effect.final_http_status == 200 {
        if effect.segments.as_ref() != Some(segments)
            || binding.indexed as usize != effect.index_items.len()
        {
            return Err(invalid());
        }
    } else if !segments.values.is_empty() || binding.indexed != 0 {
        return Err(invalid());
    }
    if !effect.index_enabled && !prepared.before_index_items.is_empty() {
        return Err(invalid());
    }
    Ok(prepared)
}

pub(super) fn result(
    staging: &Path,
    claim: &MediaPatchClaim,
    outcome: &MediaPatchOutcome,
) -> Result<ReadMediaCreateResultResponse, Status> {
    let prepared = retained(staging, claim, outcome)?;
    Ok(ReadMediaCreateResultResponse {
        media: prepared.effects.and_then(|p| p.record),
        segments: prepared.result_segments,
        indexed: outcome.processed.as_ref().ok_or_else(invalid)?.indexed,
    })
}

fn replace_segments(path: &Path, before: Option<&[u8]>, after: &[u8]) -> Result<(), Status> {
    let current = file_state(path)?;
    if current.as_deref() == Some(after) {
        // Re-confirm the original replacement, including a lost sync result.
        return media_create::persist_bound(path, after).map_err(|_| unknown());
    }
    if current.as_deref() != before {
        return Err(unknown());
    }
    let parent = path.parent().ok_or_else(unknown)?;
    fs::create_dir_all(parent).map_err(|_| unknown())?;
    media_library::reject_links(path).map_err(|_| unknown())?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(|_| unknown())?;
    temporary.write_all(after).map_err(|_| unknown())?;
    temporary.as_file().sync_all().map_err(|_| unknown())?;
    if file_state(path)?.as_deref() != before {
        return Err(unknown());
    }
    let file = temporary.persist(path).map_err(|_| unknown())?;
    file.sync_all().map_err(|_| unknown())?;
    media_create::sync_directory(parent)?;
    if file_state(path)?.as_deref() != Some(after) {
        return Err(unknown());
    }
    Ok(())
}

/// Resume only after the exact original processing/final library witness exists.
/// A still-old library is not proof of no effect and grants no replacement action.
pub(super) fn publish(
    root: &Path,
    staging: &Path,
    claim: &MediaPatchClaim,
    outcome: &MediaPatchOutcome,
    before: &[u8],
    after: &[u8],
    initial: bool,
) -> Result<bool, Status> {
    let prepared = retained(staging, claim, outcome)?;
    let effects = prepared.effects.as_ref().ok_or_else(unknown)?;
    let effect = effects.processing.as_ref().ok_or_else(unknown)?;
    let id = &claim.intent.as_ref().ok_or_else(unknown)?.media_id;
    let processing = media_processing::intermediate(after, effect, id)?;
    let current = media_library::read_library(root)?;
    let final_published = current == after;
    if !final_published && current != processing {
        if !initial || current != before {
            return Err(unknown());
        }
        if !media_library::publish(root, &processing, Some(claim.lease_until_unix))
            .map_err(|_| unknown())?
        {
            return Ok(false);
        }
    } else {
        media_library::confirm_publication(root, &current).map_err(|_| unknown())?;
    }
    let (present, bytes) = source(root, &prepared.source_relative_path).map_err(|_| unknown())?;
    if present != prepared.source_present || media_library::sha(&bytes) != prepared.source_sha256 {
        return Err(unknown());
    }
    let path = root.join(".media/segments").join(format!("{id}.json"));
    let old = prepared
        .before_segments_present
        .then_some(prepared.before_segments.as_slice());
    let final_segments = effect.segments.as_ref().map(segment_bytes).transpose()?;
    let cleared = segment_bytes(&MediaArray::default())?;
    if effect.force {
        media_process_index::finish(
            root,
            claim,
            outcome,
            &prepared,
            media_process_index::Phase::Clear,
        )?;
        // If the same operation already wrote final segments, replaying its
        // earlier clear must not erase them while reconciling the index.
        if !final_segments.as_ref().is_some_and(|final_bytes| {
            file_state(&path)
                .is_ok_and(|current| current.as_deref() == Some(final_bytes.as_slice()))
        }) {
            replace_segments(&path, old, &cleared)?;
        }
    }
    if let Some(segments) = final_segments.as_ref() {
        replace_segments(
            &path,
            if effect.force {
                Some(cleared.as_slice())
            } else {
                old
            },
            segments,
        )?;
        media_process_index::finish(
            root,
            claim,
            outcome,
            &prepared,
            media_process_index::Phase::Final,
        )?;
    }
    if final_published {
        media_library::confirm_publication(root, after).map_err(|_| unknown())?;
    } else if !media_library::publish(root, after, None).map_err(|_| unknown())? {
        return Err(unknown());
    }
    Ok(true)
}
