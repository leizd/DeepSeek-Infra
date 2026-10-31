//! Fenced creation content stays in Rust custody, outside Go control row bounds.
use crate::{WorkerRpcService, media_library, media_values};
use deepseek_policy::{
    app_error::AppError,
    core_utils::{python_truthy, text_or_empty, utc_now_iso},
    media_schema::{
        declared_media_type, guess_mime_type, normalize_media_metadata, normalize_media_path,
        normalize_media_record, normalize_mime, posix_suffix,
    },
    workspace_schema::{normalize_source_ref, validate_workspace_id},
};
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaCreatePayload, MediaCreatePreparation, MediaCreateStageRequest, MediaCreateStageResponse,
    MediaCreatedResult, MediaMutationKind, MediaObject, MediaPatchClaim, MediaPatchEffectState,
    MediaPatchOutcome, MediaUploadPayload, ReadMediaCreateResultRequest,
    ReadMediaCreateResultResponse, media_create_stage_request::Part,
    media_creation_payloads_server::MediaCreationPayloads, media_value::Kind,
};
use prost::Message as _;
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};
use tonic::{Request, Response, Status, Streaming};

pub(super) const MAX_PAYLOAD_BYTES: usize = 128 * 1024 * 1024;
const CHUNK_BYTES: usize = 256 * 1024;

fn invalid() -> Status {
    media_values::invalid()
}

pub(super) fn operation_dir(staging: &Path, operation: &str) -> Result<PathBuf, Status> {
    let nonce = operation
        .strip_prefix("media-create-")
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

pub(super) fn sync_directory(path: &Path) -> Result<(), Status> {
    #[cfg(unix)]
    fs::File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|_| invalid())?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

fn bounded_read(path: &Path) -> Result<Vec<u8>, Status> {
    media_library::reject_links(path)?;
    let file = fs::File::open(path).map_err(|_| invalid())?;
    if !file.metadata().map_err(|_| invalid())?.is_file() {
        return Err(invalid());
    }
    let mut bytes = Vec::new();
    file.take(MAX_PAYLOAD_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| invalid())?;
    if bytes.len() > MAX_PAYLOAD_BYTES {
        return Err(invalid());
    }
    media_library::reject_links(path)?;
    Ok(bytes)
}

// No replacement of an existing content identity. A retry can observe exactly
// the retained bytes; conflicting/partial content never authorizes a new write.
fn confirm_retained(path: &Path, bytes: &[u8]) -> Result<(), Status> {
    media_library::reject_links(path)?;
    let mut options = fs::OpenOptions::new();
    options.read(true);
    // FlushFileBuffers requires GENERIC_WRITE on Windows even when this
    // original-operation confirmation never writes or truncates the file.
    #[cfg(windows)]
    options.write(true);
    let file = options.open(path).map_err(|_| invalid())?;
    if !file.metadata().map_err(|_| invalid())?.is_file() {
        return Err(invalid());
    }
    let mut retained = Vec::new();
    (&file)
        .take(MAX_PAYLOAD_BYTES as u64 + 1)
        .read_to_end(&mut retained)
        .map_err(|_| invalid())?;
    if retained.len() > MAX_PAYLOAD_BYTES || retained.as_slice() != bytes {
        return Err(invalid());
    }
    file.sync_all().map_err(|_| invalid())?;
    let parent = path.parent().ok_or_else(invalid)?;
    sync_directory(parent)?;
    if let Some(grandparent) = parent.parent() {
        sync_directory(grandparent)?;
    }
    media_library::reject_links(path)?;
    if bounded_read(path)? != bytes {
        return Err(invalid());
    }
    Ok(())
}

pub(super) fn persist_bound(path: &Path, bytes: &[u8]) -> Result<(), Status> {
    if bytes.len() > MAX_PAYLOAD_BYTES {
        return Err(invalid());
    }
    media_library::reject_links(path)?;
    if path.exists() {
        return confirm_retained(path, bytes);
    }
    let parent = path.parent().ok_or_else(invalid)?;
    fs::create_dir_all(parent).map_err(|_| invalid())?;
    media_library::reject_links(path)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(|_| invalid())?;
    temporary.write_all(bytes).map_err(|_| invalid())?;
    temporary.as_file().sync_all().map_err(|_| invalid())?;
    media_library::reject_links(path)?;
    match temporary.persist_noclobber(path) {
        Ok(file) => file.sync_all().map_err(|_| invalid())?,
        Err(error) if error.error.kind() == std::io::ErrorKind::AlreadyExists => {
            return confirm_retained(path, bytes);
        }
        Err(_) => return Err(invalid()),
    }
    sync_directory(parent)?;
    if let Some(grandparent) = parent.parent() {
        sync_directory(grandparent)?;
    }
    Ok(())
}

fn first<'a>(payload: &'a Value, names: &[&str]) -> Option<&'a Value> {
    names
        .iter()
        .filter_map(|name| payload.get(*name))
        .find(|value| python_truthy(value))
}
fn text(payload: &Value, names: &[&str]) -> String {
    text_or_empty(first(payload, names))
}

// Pure retained JSON registration semantics. No file is written until a Go
// dispatch permit has committed and this exact preparation is journaled.
fn registration(
    payload: &Value,
    media_id: &str,
    now: i64,
) -> Result<MediaCreatePreparation, AppError> {
    let bad = || AppError::invalid_payload("Media payload must be an object");
    if !payload.is_object() {
        return Err(bad());
    }
    let media_id = validate_workspace_id(media_id, "media id")?;
    let project_id = text(payload, &["projectId"]).trim().to_string();
    let title_raw = first(payload, &["title", "name"])
        .cloned()
        .unwrap_or(json!("Media"));
    let title = text_or_empty(Some(&title_raw)).trim().to_string();
    let filename = text(payload, &["filename"]);
    let filename = if filename.is_empty() {
        title.clone()
    } else {
        filename.trim().to_string()
    };
    let mut mime = normalize_mime(first(payload, &["mimeType", "contentType"]));
    let supported_mime = mime.is_empty()
        || ["application/pdf", "text/html", "application/xhtml+xml"].contains(&mime.as_str())
        || ["image/", "audio/", "video/"]
            .iter()
            .any(|prefix| mime.starts_with(prefix));
    if !supported_mime {
        return Err(AppError::invalid_payload("Unsupported media MIME type"));
    }
    let media_type = declared_media_type(payload.get("type"), &mime, &filename)?;
    let mut metadata = normalize_media_metadata(payload.get("metadata"));
    let default_source = json!({"kind":"upload","refId":""});
    let source_input = payload
        .get("source")
        .filter(|value| value.is_object())
        .unwrap_or(&default_source);
    let mut source = normalize_source_ref(source_input);
    let mut path = normalize_media_path(payload.get("path"))?;
    if !path.is_empty() && !path.starts_with(&format!("objects/{media_id}/")) {
        return Err(AppError::invalid_payload(
            "Media source path must point to an object stored in the media library",
        ));
    }
    let mut source_suffix = String::new();
    let mut source_bytes = Vec::new();
    if media_type == "webpage" {
        let mut next_source = source.clone();
        if !python_truthy(&next_source["kind"]) {
            next_source["kind"] = json!("browser");
        }
        let url = first(payload, &["url", "sourceUrl"])
            .cloned()
            .unwrap_or_else(|| metadata.get("sourceUrl").cloned().unwrap_or(json!("")));
        next_source["url"] = url;
        source = normalize_source_ref(&next_source);
        let html = text(payload, &["html", "content"]).trim().to_string();
        let webpage_text = text(payload, &["text"]).trim().to_string();
        if !html.is_empty() {
            source_suffix = ".html".into();
            source_bytes = html.into_bytes();
            metadata
                .as_object_mut()
                .ok_or_else(bad)?
                .entry("sourceUrl")
                .or_insert_with(|| json!(text(payload, &["url", "sourceUrl"])));
        }
        if !webpage_text.is_empty() {
            metadata
                .as_object_mut()
                .ok_or_else(bad)?
                .entry("webpageText")
                .or_insert(json!(webpage_text));
        }
    } else if let Some(content) = first(payload, &["text", "transcript"]) {
        let content = text_or_empty(Some(content));
        let key = if payload.get("text").is_some_and(python_truthy)
            && !["audio", "video"].contains(&media_type.as_str())
        {
            "text"
        } else {
            "transcript"
        };
        metadata
            .as_object_mut()
            .ok_or_else(bad)?
            .entry(key)
            .or_insert(json!(content));
        if path.is_empty() {
            source_suffix = ".txt".into();
            source_bytes = content.into_bytes();
        }
    }
    if !source_suffix.is_empty() {
        if source_bytes.is_empty() || source_bytes.len() > 50 * 1024 * 1024 {
            return Err(AppError::invalid_payload(
                "Media source is empty or too large",
            ));
        }
        path = format!("objects/{media_id}/source{source_suffix}");
    }
    if let Some(pages) = payload.get("pageTexts").filter(|value| value.is_array()) {
        metadata["pageTexts"] = pages.clone();
    }
    if mime.is_empty() {
        mime = guess_mime_type(&filename);
    }
    let time = utc_now_iso(now);
    let record = normalize_media_record(
        &json!({"mediaId":media_id,"projectId":project_id,"type":media_type,
        "title":title,"mimeType":mime,"path":path,"source":source,"status":"pending",
        "createdAt":time,"updatedAt":time,"metadata":metadata}),
    )?;
    let record = normalize_media_record(&record)?;
    let record = media_values::from_json_legacy(&record).map_err(|_| bad())?;
    let Some(Kind::ObjectValue(record)) = record.kind else {
        return Err(bad());
    };
    Ok(MediaCreatePreparation {
        record: Some(record),
        source_suffix,
        source_bytes,
        processing: None,
    })
}

fn upload_suffix(filename: &str) -> String {
    let suffix = posix_suffix(filename);
    if suffix.is_empty() {
        ".bin".into()
    } else {
        // Python lower() also lowercases non-ASCII filename extensions.
        suffix.to_lowercase()
    }
}

fn upload(
    payload: &MediaUploadPayload,
    media_id: &str,
    now: i64,
) -> Result<MediaCreatePreparation, AppError> {
    if payload.data.is_empty() {
        return Err(AppError::invalid_payload("Media source is empty"));
    }
    if payload.data.len() > 50 * 1024 * 1024 {
        return Err(AppError {
            message: "Media upload exceeds the 50 MB limit".into(),
            code: deepseek_policy::app_error::codes::UPLOAD_TOO_LARGE,
            status: 413,
        });
    }
    let filename = &payload.filename;
    if filename.is_empty() || filename.chars().count() > 180 || filename.contains(['/', '\\', '\0'])
    {
        return Err(AppError::invalid_payload("Invalid media upload filename"));
    }
    let media_id = validate_workspace_id(media_id, "media id")?;
    let mut mime = normalize_mime(Some(&json!(payload.content_type)));
    if mime.is_empty() {
        mime = guess_mime_type(filename);
    }
    if !["application/pdf", "text/html", "application/xhtml+xml"].contains(&mime.as_str())
        && !["image/", "audio/", "video/"]
            .iter()
            .any(|prefix| mime.starts_with(prefix))
    {
        return Err(AppError::invalid_payload("Unsupported media MIME type"));
    }
    let media_type = declared_media_type(None, &mime, filename)?;
    let source_suffix = upload_suffix(filename);
    let title = if payload.title.is_empty() {
        filename
    } else {
        &payload.title
    };
    let time = utc_now_iso(now);
    let record = normalize_media_record(
        &json!({"mediaId":media_id,"projectId":payload.project_id,
        "type":media_type,"title":title,"mimeType":mime,"path":format!("objects/{media_id}/source{source_suffix}"),
        "source":{"kind":"upload","refId":filename},"status":"pending","createdAt":time,"updatedAt":time,"metadata":{}}),
    )?;
    let record = normalize_media_record(&record)?;
    let record = media_values::from_json_legacy(&record)
        .map_err(|_| AppError::invalid_payload("Invalid media upload record"))?;
    let Some(Kind::ObjectValue(record)) = record.kind else {
        return Err(AppError::invalid_payload("Invalid media upload record"));
    };
    Ok(MediaCreatePreparation {
        record: Some(record),
        source_suffix,
        source_bytes: payload.data.clone(),
        processing: None,
    })
}

fn retained_payload(staging: &Path, claim: &MediaPatchClaim) -> Result<MediaCreatePayload, Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    let manifest = intent.create.as_ref().ok_or_else(invalid)?;
    let directory = operation_dir(staging, &intent.operation_id)?;
    let bytes = bounded_read(&directory.join("payload.pb"))?;
    if bytes.len() as u64 != manifest.payload_bytes
        || media_library::sha(&bytes) != manifest.payload_sha256
    {
        return Err(invalid());
    }
    let payload = MediaCreatePayload::decode(bytes.as_slice()).map_err(|_| invalid())?;
    if payload.encode_to_vec() != bytes {
        return Err(invalid());
    }
    Ok(payload)
}

pub(super) fn plan(
    root: &Path,
    staging: &Path,
    claim: &MediaPatchClaim,
    before: &[u8],
) -> Result<(MediaPatchOutcome, Option<Vec<u8>>), Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    let payload = retained_payload(staging, claim)?;
    let mut result = MediaPatchOutcome {
        schema_version: 1,
        operation_id: intent.operation_id.clone(),
        original_fence: claim.fence.clone(),
        request_sha256: claim.request_sha256.clone(),
        state: MediaPatchEffectState::Applied as i32,
        http_status: 200,
        before_sha256: media_library::sha(before),
        fleet_id: claim.fleet_id.clone(),
        environment: claim.environment.clone(),
        writer_fencing_token: claim.writer_fencing_token,
        ..Default::default()
    };
    let now = crate::service::unix_now_seconds().map_err(|_| invalid())?;
    let mut process = false;
    let prepared = match (&payload.registration, &payload.upload) {
        (Some(object), None) => {
            let value = media_values::to_json(
                &deepseek_protocol::generated::deepseek::media::v1::MediaValue {
                    kind: Some(Kind::ObjectValue(object.clone())),
                },
            )?;
            process = value.get("process").is_some_and(python_truthy);
            registration(&value, &intent.media_id, now)
        }
        (None, Some(value)) => upload(value, &intent.media_id, now),
        _ => return Err(invalid()),
    };
    let mut prepared = match prepared {
        Ok(value) => value,
        Err(error) => {
            return Ok((
                media_library::refusal(result, error.status as u32, error.code, &error.message),
                None,
            ));
        }
    };
    if process {
        prepared = crate::media_processing::prepare(prepared, now)?;
        let effect = prepared.processing.as_ref().ok_or_else(invalid)?;
        result.http_status = effect.final_http_status;
        result.error_code = effect.final_error_code.clone();
        result.error_message = effect.final_error_message.clone();
    }
    let mut items = media_library::normalized_items(before);
    if items
        .iter()
        .any(|item| item["mediaId"].as_str() == Some(&intent.media_id))
        || root.join(".media/objects").join(&intent.media_id).exists()
    {
        return Ok((
            media_library::refusal(
                result,
                409,
                "revision_conflict",
                "Media identity already exists",
            ),
            None,
        ));
    }
    let record = prepared.record.as_ref().ok_or_else(invalid)?;
    let record_bytes = record.encode_to_vec();
    let prepared_bytes = prepared.encode_to_vec();
    let directory = operation_dir(staging, &intent.operation_id)?;
    persist_bound(&directory.join("prepared.pb"), &prepared_bytes)?;
    result.created = Some(MediaCreatedResult {
        record_sha256: media_library::sha(&record_bytes),
        record_bytes: record_bytes.len() as u64,
        preparation_sha256: media_library::sha(&prepared_bytes),
        original_fence: claim.fence.clone(),
    });
    items.push(media_values::to_json(
        &deepseek_protocol::generated::deepseek::media::v1::MediaValue {
            kind: Some(Kind::ObjectValue(record.clone())),
        },
    )?);
    if items.len() > 1000 {
        items.drain(..items.len() - 1000);
    }
    media_library::planned_bytes(claim, result, items)
}

pub(super) fn retained_preparation(
    staging: &Path,
    claim: &MediaPatchClaim,
    outcome: &MediaPatchOutcome,
) -> Result<MediaCreatePreparation, Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    let created = outcome.created.as_ref().ok_or_else(invalid)?;
    if created.original_fence != claim.fence {
        return Err(invalid());
    }
    let bytes = bounded_read(&operation_dir(staging, &intent.operation_id)?.join("prepared.pb"))?;
    if media_library::sha(&bytes) != created.preparation_sha256 {
        return Err(invalid());
    }
    let prepared = MediaCreatePreparation::decode(bytes.as_slice()).map_err(|_| invalid())?;
    if prepared.encode_to_vec() != bytes {
        return Err(invalid());
    }
    let record = prepared.record.as_ref().ok_or_else(invalid)?;
    let record_bytes = record.encode_to_vec();
    if record_bytes.len() as u64 != created.record_bytes
        || media_library::sha(&record_bytes) != created.record_sha256
    {
        return Err(invalid());
    }
    let json = media_values::to_json(
        &deepseek_protocol::generated::deepseek::media::v1::MediaValue {
            kind: Some(Kind::ObjectValue(record.clone())),
        },
    )?;
    if json["mediaId"].as_str() != Some(&intent.media_id) {
        return Err(invalid());
    }
    if prepared.source_bytes.len() > 50 * 1024 * 1024 {
        return Err(invalid());
    }
    let payload = retained_payload(staging, claim)?;
    match (payload.registration, payload.upload) {
        (Some(_), None) if ["", ".html", ".txt"].contains(&prepared.source_suffix.as_str()) => {}
        (None, Some(value))
            if !value.data.is_empty()
                && !value.filename.contains(['/', '\\', '\0'])
                && prepared.source_suffix == upload_suffix(&value.filename)
                && prepared.source_bytes == value.data => {}
        _ => return Err(invalid()),
    }
    if prepared.source_suffix.is_empty() != prepared.source_bytes.is_empty() {
        return Err(invalid());
    }
    if !prepared.source_suffix.is_empty()
        && json["path"].as_str()
            != Some(&format!(
                "objects/{}/source{}",
                intent.media_id, prepared.source_suffix
            ))
    {
        return Err(invalid());
    }
    Ok(prepared)
}

pub(super) fn finish(
    root: &Path,
    staging: &Path,
    claim: &MediaPatchClaim,
    outcome: &MediaPatchOutcome,
) -> Result<(), Status> {
    let prepared = retained_preparation(staging, claim, outcome)?;
    if prepared.source_bytes.is_empty() {
        return Ok(());
    }
    let id = &claim.intent.as_ref().ok_or_else(invalid)?.media_id;
    let path = root
        .join(".media/objects")
        .join(id)
        .join(format!("source{}", prepared.source_suffix));
    persist_bound(&path, &prepared.source_bytes).map_err(|_| Status::unavailable("EFFECT_UNKNOWN"))
}

#[tonic::async_trait]
impl MediaCreationPayloads for WorkerRpcService {
    async fn stage(
        &self,
        request: Request<Streaming<MediaCreateStageRequest>>,
    ) -> Result<Response<MediaCreateStageResponse>, Status> {
        self.media_caller(&request)?;
        let mut stream = request.into_inner();
        let first = stream.message().await?.ok_or_else(invalid)?;
        let fence = first.fence.as_ref().ok_or_else(invalid)?;
        let Some(Part::Manifest(manifest)) = first.part.as_ref() else {
            return Err(invalid());
        };
        if first.schema_version != 1 {
            return Err(invalid());
        }
        let control = self.agent_execution_control.as_ref().ok_or_else(invalid)?;
        let claim = control.media_claim(&first.operation_id, fence).await?;
        let config = self.worker.lock().await.agent_node_authority()?;
        crate::media_patch::validate_claim(
            &claim,
            &first.operation_id,
            fence,
            &config,
            crate::service::unix_now_seconds().map_err(|_| invalid())?,
        )?;
        let intent = claim.intent.as_ref().ok_or_else(invalid)?;
        if intent.kind != MediaMutationKind::Create as i32
            || intent.create.as_ref() != Some(manifest)
        {
            return Err(invalid());
        }
        let mut bytes = Vec::new();
        while let Some(message) = stream.message().await? {
            if message.schema_version != 1
                || message.operation_id != first.operation_id
                || message.fence != first.fence
            {
                return Err(invalid());
            }
            let Some(Part::Chunk(chunk)) = message.part else {
                return Err(invalid());
            };
            if chunk.is_empty()
                || chunk.len() > CHUNK_BYTES
                || bytes.len().saturating_add(chunk.len()) > manifest.payload_bytes as usize
                || bytes.len().saturating_add(chunk.len()) > MAX_PAYLOAD_BYTES
            {
                return Err(invalid());
            }
            bytes.extend(chunk);
        }
        if bytes.len() as u64 != manifest.payload_bytes
            || media_library::sha(&bytes) != manifest.payload_sha256
        {
            return Err(invalid());
        }
        let live = control.media_claim(&first.operation_id, fence).await?;
        let config = self.worker.lock().await.agent_node_authority()?;
        crate::media_patch::validate_claim(
            &live,
            &first.operation_id,
            fence,
            &config,
            crate::service::unix_now_seconds().map_err(|_| invalid())?,
        )?;
        if live != claim {
            return Err(invalid());
        }
        let journal = self
            .media_journal
            .as_ref()
            .ok_or_else(invalid)?
            .lock()
            .await;
        let path =
            operation_dir(journal.creation_staging(), &first.operation_id)?.join("payload.pb");
        persist_bound(&path, &bytes)?;
        Ok(Response::new(MediaCreateStageResponse {
            manifest: Some(manifest.clone()),
        }))
    }
    async fn read_result(
        &self,
        request: Request<ReadMediaCreateResultRequest>,
    ) -> Result<Response<ReadMediaCreateResultResponse>, Status> {
        self.media_caller(&request)?;
        let input = request.into_inner();
        let fence = input.original_fence.as_ref().ok_or_else(invalid)?;
        if input.schema_version != 1
            || input.operation_id != fence.action_id
            || fence.execution_epoch != 1
        {
            return Err(invalid());
        }
        let claim = self
            .agent_execution_control
            .as_ref()
            .ok_or_else(invalid)?
            .media_claim(&input.operation_id, fence)
            .await?;
        if claim.state != "APPLIED"
            || claim
                .intent
                .as_ref()
                .is_none_or(|intent| intent.kind != MediaMutationKind::Create as i32)
        {
            return Err(invalid());
        }
        let journal = self
            .media_journal
            .as_ref()
            .ok_or_else(invalid)?
            .lock()
            .await;
        let record: MediaObject = journal.created_record(&claim)?;
        Ok(Response::new(ReadMediaCreateResultResponse {
            media: Some(record),
            segments: None,
            indexed: 0,
        }))
    }
}

#[cfg(test)]
#[path = "media_create_tests.rs"]
mod tests;
