//! Immutable recovery protobufs stay in Rust custody and in the request hash.
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_policy::agent_run_data::{ArtifactReference, ArtifactStore, sanitize};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentRunRecoveryContext, AgentRunRecoveryKind, AgentRunSnapshot,
};
use prost::Message;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::Path;
use tonic::Status;

pub(crate) const PRIVATE_FIELD: &str = "_nativeRecoveryContext";
const SCHEMA: &str = "native-agent-recovery-context-v1";
const MAX_BYTES: usize = 1024 * 1024;

pub(crate) fn denied() -> Status {
    Status::failed_precondition("AGENT_RECOVERY_CUSTODY_DENIED")
}

pub(crate) struct PreparedRecovery {
    pub(crate) context: AgentRunRecoveryContext,
    pub(crate) reference: ArtifactReference,
    pub(crate) digest: String,
}

pub(crate) fn digest(context: &AgentRunRecoveryContext) -> Result<String, Status> {
    let bytes = context.encode_to_vec();
    if bytes.is_empty() || bytes.len() > MAX_BYTES {
        return Err(denied());
    }
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

/// Called only after authoritative Go preview and the independent Rust journal
/// audit. Immutable orphan objects cannot authorize effects if Go's CAS loses.
pub(crate) fn prepare(
    root: &Path,
    body: &mut Value,
    context: &AgentRunRecoveryContext,
) -> Result<(PreparedRecovery, ArtifactReference), Status> {
    if !body.is_object()
        || body.get(PRIVATE_FIELD).is_some()
        || body.get("_nativeProviderCredentials").is_some()
    {
        return Err(denied());
    }
    let context_digest = digest(context)?;
    let envelope =
        json!({"metadataProto":URL_SAFE_NO_PAD.encode(context.encode_to_vec()),"schema":SCHEMA});
    let canonical: Value = serde_json::from_slice(
        &crate::authority_request::canonical_json_bytes(&envelope).map_err(|_| denied())?,
    )
    .map_err(|_| denied())?;
    let mut store = ArtifactStore::open_native(root, "python_disabled").map_err(|_| denied())?;
    let reference = store.put(&canonical).map_err(|_| denied())?;
    body[PRIVATE_FIELD] = canonical;
    let request = store.put(body).map_err(|_| denied())?;
    let prepared = read(root, body)?.ok_or_else(denied)?;
    if prepared.context != *context
        || prepared.digest != context_digest
        || prepared.reference != reference
    {
        return Err(denied());
    }
    Ok((prepared, request))
}

pub(crate) fn initial_event_bodies(
    store: &mut ArtifactStore,
    context: &AgentRunRecoveryContext,
) -> Result<Vec<AgentArtifactReference>, Status> {
    let mut bodies = vec![json!({"type":"run_status","status":"running"})];
    for id in &context.reset_ids {
        bodies.push(json!({"type":"agent_reset","phase":id}));
    }
    if context.reset_final
        && (context
            .selection
            .as_ref()
            .is_some_and(|s| s.kind == AgentRunRecoveryKind::Resume as i32)
            || context.execute.is_empty())
    {
        bodies.push(json!({"type":"final_reset","scope":"final_answer"}));
    }
    if context.complete_immediately {
        bodies.push(json!({"type":"run_status","status":"done"}));
    }
    bodies
        .iter()
        .map(|body| {
            store
                .put(body)
                .map(|r| AgentArtifactReference {
                    sha256: r.sha256,
                    length: r.length,
                })
                .map_err(|_| denied())
        })
        .collect()
}

/// The envelope is itself a hash-checked Rust artifact. A missing object, an
/// unknown protobuf field, or a noncanonical encoding cannot become custody.
pub(crate) fn read(root: &Path, body: &Value) -> Result<Option<PreparedRecovery>, Status> {
    let Some(envelope) = body.get(PRIVATE_FIELD) else {
        return Ok(None);
    };
    let object = envelope.as_object().ok_or_else(denied)?;
    if object.len() != 2 || envelope["schema"] != SCHEMA {
        return Err(denied());
    }
    let encoded = envelope["metadataProto"].as_str().ok_or_else(denied)?;
    if encoded.len() > MAX_BYTES.div_ceil(3) * 4 {
        return Err(denied());
    }
    let bytes = URL_SAFE_NO_PAD.decode(encoded).map_err(|_| denied())?;
    if bytes.is_empty() || bytes.len() > MAX_BYTES || URL_SAFE_NO_PAD.encode(&bytes) != encoded {
        return Err(denied());
    }
    let context = AgentRunRecoveryContext::decode(bytes.as_slice()).map_err(|_| denied())?;
    if context.encode_to_vec() != bytes
        || context.schema_version != 1
        || context.run_id.is_empty()
        || context.generation == 0
        || context.generation > i64::MAX as u64
        || context.metadata_epoch == 0
        || context.expected_runtime_revision == 0
        || context.expected_runtime_revision >= i64::MAX as u64
        || context.expected_index < 1
    {
        return Err(denied());
    }
    let sanitized = sanitize(envelope);
    if sanitized != *envelope {
        return Err(denied());
    }
    let stored = crate::authority_request::canonical_json_bytes(envelope).map_err(|_| denied())?;
    let reference = ArtifactReference {
        sha256: format!("{:x}", Sha256::digest(&stored)),
        length: stored.len() as u64,
    };
    if ArtifactStore::open_readonly(root)
        .and_then(|store| store.get(&reference))
        .map_err(|_| denied())?
        != *envelope
    {
        return Err(denied());
    }
    Ok(Some(PreparedRecovery {
        context,
        reference,
        digest: format!("{:x}", Sha256::digest(bytes)),
    }))
}

/// Current Go metadata must bind precisely the independently retained context.
/// It remains metadata only; the original signed grant still admits each effect.
pub(crate) fn validate_binding(
    prepared: Option<&PreparedRecovery>,
    run: &AgentRunSnapshot,
) -> Result<(), Status> {
    match (prepared, run.recovery_scope.as_ref()) {
        (None, None) => Ok(()),
        (Some(prepared), Some(scope)) => {
            let context = &prepared.context;
            let reference = scope.context.as_ref().ok_or_else(denied)?;
            if context.run_id != run.run_id
                || context.generation != scope.generation
                || context.expected_index != scope.start_index
                || scope.start_index >= run.next_index
                || context.expected_runtime_revision.checked_add(1) != Some(run.runtime_revision)
                || scope.context_sha256 != prepared.digest
                || reference.sha256 != prepared.reference.sha256
                || reference.length != prepared.reference.length
                || context.expected_request.as_ref() == run.request.as_ref()
            {
                return Err(denied());
            }
            Ok(())
        }
        _ => Err(denied()),
    }
}

#[cfg(test)]
mod tests;
