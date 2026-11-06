//! Compact PATCH result custody. A retained record is evidence, never authority.
use crate::{
    WorkerRpcService,
    media_create::persist_bound_limited,
    media_library::{reject_links, sha},
    media_values::invalid,
};
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaMutationKind, MediaPatchClaim, MediaPatchEffectState, MediaPatchOutcome,
    MediaPatchPreparation, MediaPatchResultChunk, MediaPatchedResult, ReadMediaPatchResultRequest,
};
use futures_util::{Stream, StreamExt, stream};
use prost::Message as _;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    pin::Pin,
};
use tokio::io::AsyncReadExt;
use tonic::{Request, Response, Status};

pub(super) const CHUNK_BYTES: usize = 256 * 1024;
pub(super) type ResultStream =
    Pin<Box<dyn Stream<Item = Result<MediaPatchResultChunk, Status>> + Send + 'static>>;

pub(super) fn record_path(staging: &Path, operation: &str) -> Result<PathBuf, Status> {
    if operation.len() > 128
        || operation
            .strip_prefix("media-patch-")
            .is_none_or(str::is_empty)
    {
        return Err(invalid());
    }
    // Original PATCH identities were not restricted to CREATE's 32-hex nonce
    // grammar. Hash the complete identity into a fixed path component instead
    // of either widening CREATE's helper or using caller text as a pathname.
    let identity = format!("{:x}", Sha256::digest(operation.as_bytes()));
    let path = staging
        .join("patch-results")
        .join(identity)
        .join("record.pb");
    reject_links(&path)?;
    Ok(path)
}

fn preparation(claim: &MediaPatchClaim, record_sha256: &[u8], record_bytes: u64) -> Vec<u8> {
    let mut original = claim.clone();
    original.state = "CLAIMED".into();
    MediaPatchPreparation {
        schema_version: 1,
        claim: Some(original),
        record_sha256: record_sha256.to_vec(),
        record_bytes,
    }
    .encode_to_vec()
}

fn prepare_custody_root(staging: &Path) -> Result<(), Status> {
    let directory = staging.join("patch-results");
    reject_links(&directory)?;
    fs::create_dir_all(&directory).map_err(|_| invalid())?;
    reject_links(&directory)?;
    // The journal already established state/rust-worker and its SQLite identity.
    // Persist both new custody levels and their links through the existing state
    // directory before persist_bound_limited creates the operation's directory.
    // That helper then synchronizes the operation directory and custody root.
    #[cfg(unix)]
    {
        let worker = staging.parent().ok_or_else(invalid)?;
        let state = worker.parent().ok_or_else(invalid)?;
        for parent in [directory.as_path(), staging, worker, state] {
            fs::File::open(parent)
                .and_then(|file| file.sync_all())
                .map_err(|_| invalid())?;
        }
    }
    Ok(())
}

pub(super) fn prepare(
    staging: &Path,
    claim: &MediaPatchClaim,
    mut planned: MediaPatchOutcome,
) -> Result<MediaPatchOutcome, Status> {
    if planned.encoded_len() <= 2 * 1024 * 1024
        || claim.intent.as_ref().is_none_or(|i| {
            i.kind != MediaMutationKind::Unspecified as i32
                || i.patch.is_none()
                || !matches!(
                    i.max_outcome_schema_version,
                    2 | deepseek_protocol::media_server_error::VERSION
                )
        })
        || planned.state != MediaPatchEffectState::Applied as i32
    {
        return Ok(planned);
    }
    let record = planned.media.take().ok_or_else(invalid)?.encode_to_vec();
    let digest = sha(&record);
    let length = record.len() as u64;
    let binding = preparation(claim, &digest, length);
    prepare_custody_root(staging)?;
    let path = record_path(staging, &planned.operation_id)?;
    // The caller still holds the original mutation gate. Persist exact content
    // without replacing any previous preparation; the journal is inserted only
    // after its file and containing directories have been synchronized.
    persist_bound_limited(&path, &record, record.len())?;
    planned.schema_version = 2;
    planned.patched = Some(MediaPatchedResult {
        record_sha256: digest,
        record_bytes: length,
        preparation_sha256: sha(&binding),
        original_fence: claim.fence.clone(),
    });
    Ok(planned)
}

pub(super) fn open_record(
    staging: &Path,
    claim: &MediaPatchClaim,
    receipt: &MediaPatchedResult,
) -> Result<fs::File, Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    if intent.kind != MediaMutationKind::Unspecified as i32
        || intent.patch.is_none()
        || !matches!(
            intent.max_outcome_schema_version,
            2 | deepseek_protocol::media_server_error::VERSION
        )
        || receipt.original_fence != claim.fence
        || receipt.record_sha256.len() != 32
        || receipt.record_bytes == 0
        || receipt.preparation_sha256
            != sha(&preparation(
                claim,
                &receipt.record_sha256,
                receipt.record_bytes,
            ))
    {
        return Err(invalid());
    }
    let path = record_path(staging, &intent.operation_id)?;
    reject_links(&path)?;
    let mut file = fs::File::open(&path).map_err(|_| invalid())?;
    let metadata = file.metadata().map_err(|_| invalid())?;
    if !metadata.is_file() || metadata.len() != receipt.record_bytes {
        return Err(invalid());
    }
    let mut hash = Sha256::new();
    let mut buffer = vec![0; CHUNK_BYTES];
    let mut length = 0u64;
    loop {
        let count = file.read(&mut buffer).map_err(|_| invalid())?;
        if count == 0 {
            break;
        }
        length = length.checked_add(count as u64).ok_or_else(invalid)?;
        if length > receipt.record_bytes {
            return Err(invalid());
        }
        hash.update(&buffer[..count]);
    }
    if length != receipt.record_bytes || hash.finalize()[..] != receipt.record_sha256 {
        return Err(invalid());
    }
    reject_links(&path)?;
    file.seek(SeekFrom::Start(0)).map_err(|_| invalid())?;
    Ok(file)
}

pub(super) fn record_stream(file: fs::File, manifest: MediaPatchedResult) -> ResultStream {
    let total = manifest.record_bytes;
    let first = MediaPatchResultChunk {
        schema_version: 1,
        manifest: Some(manifest),
        offset: 0,
        record_chunk: Vec::new(),
    };
    let chunks = stream::try_unfold(
        (tokio::fs::File::from_std(file), 0u64),
        move |(mut file, offset)| async move {
            if offset == total {
                let mut extra = [0u8; 1];
                if file.read(&mut extra).await.map_err(|_| invalid())? != 0 {
                    return Err(invalid());
                }
                return Ok(None);
            }
            let count =
                usize::try_from((total - offset).min(CHUNK_BYTES as u64)).map_err(|_| invalid())?;
            let mut bytes = vec![0; count];
            file.read_exact(&mut bytes).await.map_err(|_| invalid())?;
            Ok(Some((
                MediaPatchResultChunk {
                    schema_version: 1,
                    manifest: None,
                    offset,
                    record_chunk: bytes,
                },
                (file, offset + count as u64),
            )))
        },
    );
    Box::pin(stream::once(async { Ok(first) }).chain(chunks))
}

impl WorkerRpcService {
    pub(super) async fn read_media_patch_result(
        &self,
        request: Request<ReadMediaPatchResultRequest>,
    ) -> Result<Response<ResultStream>, Status> {
        self.media_caller(&request)?;
        let input = request.into_inner();
        let fence = input.original_fence.as_ref().ok_or_else(invalid)?;
        if input.schema_version != 1
            || input.operation_id != fence.action_id
            || fence.execution_epoch != 1
            || input.operation_id.len() > 128
            || !input.operation_id.starts_with("media-patch-")
        {
            return Err(invalid());
        }
        let claim = self
            .agent_execution_control
            .as_ref()
            .ok_or_else(invalid)?
            .media_claim(&input.operation_id, fence)
            .await?;
        if claim.state != "APPLIED" || claim.fence.as_ref() != Some(fence) {
            return Err(invalid());
        }
        let journal = self
            .media_journal
            .as_ref()
            .ok_or_else(invalid)?
            .lock()
            .await;
        let (manifest, file) = journal.patched_record(&claim)?;
        Ok(Response::new(record_stream(file, manifest)))
    }
}
