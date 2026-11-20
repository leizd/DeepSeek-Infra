//! Public recovery uses a live Go plan, Rust custody, and one Go CAS transaction.
use super::*;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_protocol::generated::deepseek::agent::v1::agent_run_recovery_control_client::AgentRunRecoveryControlClient;

mod draft;

#[derive(Clone, Copy)]
enum Mode {
    Resume,
    Rerun,
}

fn selection(mode: Mode, input: &Value) -> Result<AgentRunRecoverySelection, ReadError> {
    if crate::agent_credential_client::has_reserved_field(input) {
        return Err(invalid_request());
    }
    match mode {
        Mode::Resume => Ok(AgentRunRecoverySelection {
            kind: AgentRunRecoveryKind::Resume as i32,
            ..Default::default()
        }),
        Mode::Rerun => {
            let role = input
                .get("agentId")
                .and_then(Value::as_str)
                .map(deepseek_policy::multi_agent::strip)
                .ok_or_else(invalid_request)?;
            if deepseek_policy::multi_agent::profile(role).is_none()
                && !matches!(role, "synth" | "leader" | "synthesizer")
            {
                return Err(invalid_request());
            }
            Ok(AgentRunRecoverySelection {
                kind: AgentRunRecoveryKind::Rerun as i32,
                agent_id: role.into(),
                resynthesize: Some(input.get("resynthesize") != Some(&Value::Bool(false))),
            })
        }
    }
}

fn check_status(mode: Mode, status: &str) -> Result<(), ReadError> {
    if matches!(status, "created" | "planning" | "running")
        || matches!(mode, Mode::Resume) && status == "awaiting_plan"
    {
        return Err(action_conflict());
    }
    Ok(())
}

fn recovery_error(error: tonic::Status) -> ReadError {
    match error.code() {
        tonic::Code::FailedPrecondition | tonic::Code::Aborted | tonic::Code::AlreadyExists => {
            action_conflict()
        }
        tonic::Code::InvalidArgument => invalid_request(),
        _ => rpc_error(error),
    }
}

async fn read_body(root: PathBuf, reference: ArtifactReference) -> Result<Value, ReadError> {
    tokio::task::spawn_blocking(move || {
        ArtifactStore::open_readonly(&root)
            .and_then(|store| store.get(&reference))
            .map_err(data_error)
    })
    .await
    .map_err(|_| data_error(DataError::Unavailable))?
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

async fn verify_prepared(
    root: &std::path::Path,
    draft_reference: &ArtifactReference,
    response: &PrepareAgentCredentialsResponse,
    expected_digest: &str,
) -> Result<(), ReadError> {
    let scope = response.scope.as_ref().ok_or_else(corrupt)?;
    let context_ref = reference(response.recovery_context.as_ref())?;
    let context = read_body(root.to_path_buf(), context_ref).await?;
    let encoded = context
        .get("metadataProto")
        .and_then(Value::as_str)
        .ok_or_else(corrupt)?;
    let bytes = URL_SAFE_NO_PAD.decode(encoded).map_err(|_| corrupt())?;
    if context.get("schema").and_then(Value::as_str) != Some("native-agent-recovery-context-v1")
        || URL_SAFE_NO_PAD.encode(&bytes) != encoded
        || format!("{:x}", Sha256::digest(&bytes)) != expected_digest
        || context.as_object().is_none_or(|v| v.len() != 2)
    {
        return Err(corrupt());
    }
    let base = read_body(
        root.to_path_buf(),
        ArtifactReference {
            sha256: scope.request_sha256.clone(),
            length: scope.request_length,
        },
    )
    .await?;
    // Compare the immutable artifact named in the custody request. Both sides
    // are read through the same hash-checked Rust persistence boundary.
    let mut expected = read_body(root.to_path_buf(), draft_reference.clone()).await?;
    expected
        .as_object_mut()
        .ok_or_else(corrupt)?
        .insert("_nativeRecoveryContext".into(), context);
    if base != expected {
        return Err(corrupt());
    }
    let mut final_body = read_body(
        root.to_path_buf(),
        reference(response.prepared_request.as_ref())?,
    )
    .await?;
    let private = final_body
        .as_object_mut()
        .ok_or_else(corrupt)?
        .remove(crate::agent_credential_client::PRIVATE_FIELD);
    let expected_private = response.object.as_ref().map(|object| {
        json!({
        "scope":{"fleetId":scope.fleet_id,"environment":scope.environment,"runId":scope.run_id,
            "metadataEpoch":scope.metadata_epoch,"runtimeRevision":scope.runtime_revision,
            "requestSha256":scope.request_sha256,"requestLength":scope.request_length},
        "object":{"id":object.id,"sha256":object.sha256,"length":object.length}})
    });
    if final_body != base || private != expected_private {
        return Err(corrupt());
    }
    Ok(())
}

async fn recover(
    config: Option<ReadConfig>,
    id: String,
    body: Bytes,
    mode: Mode,
) -> Result<Json<Value>, ReadError> {
    let input = operator_input(&body)?;
    let selected = selection(mode, &input)?;
    let mut session = ReadSession::open(config, &id).await?;
    check_status(mode, &session.snapshot.status)?;
    let credentials = session
        .config
        .credentials
        .as_ref()
        .ok_or_else(control_unavailable)?;
    let revision = session.snapshot.runtime_revision;
    if revision == 0 || revision >= i64::MAX as u64 {
        return Err(control_unavailable());
    }
    let mut control = AgentRunRecoveryControlClient::new(session.channel.clone())
        .max_decoding_message_size(1 << 20);
    let preview = control
        .preview(session.request(PreviewAgentRunRecoveryRequest {
            schema_version: 1,
            run_id: session.snapshot.run_id.clone(),
            metadata_fence: Some(ActionFence {
                action_id: session.snapshot.run_id.clone(),
                execution_epoch: session.epoch,
            }),
            expected_index: session.snapshot.next_index,
            expected_runtime_revision: revision,
            expected_request: session.snapshot.request.clone(),
            selection: Some(selected.clone()),
        }))
        .await
        .map_err(recovery_error)?
        .into_inner();
    let context = preview.context.ok_or_else(corrupt)?;
    if !preview.authoritative
        || !valid_digest(&preview.context_sha256)
        || context.schema_version != 1
        || context.run_id != session.snapshot.run_id
        || context.metadata_epoch != session.epoch
        || context.expected_index != session.snapshot.next_index
        || context.expected_runtime_revision != revision
        || context.expected_request != session.snapshot.request
        || context.selection.as_ref() != Some(&selected)
        || context.previous_scope != session.snapshot.recovery_scope
    {
        return Err(control_unavailable());
    }
    // No public body or Go state changes until the complete recovery preview is valid.
    let draft = draft::prepare(&session, &input).await?;
    let prepared = crate::agent_credential_client::prepare_recovery(
        credentials,
        crate::agent_credential_client::RecoveryPreparation {
            input: crate::agent_credential_client::Preparation {
                run_id: &session.snapshot.run_id,
                epoch: session.epoch,
                reference: &draft.reference,
                runtime_revision: revision + 1,
                api_key: draft.api_key.as_ref().map(|v| v.as_str()),
                tavily_api_key: draft.tavily_api_key.as_ref().map(|v| v.as_str()),
                runtime_update: Some(draft.update.clone()),
            },
            context: context.clone(),
            context_sha256: &preview.context_sha256,
            expect_credential_object: draft.expect_credential_object,
        },
    )
    .await
    .map_err(recovery_error)?;
    verify_prepared(
        &session.config.artifacts,
        &draft.reference,
        &prepared,
        &preview.context_sha256,
    )
    .await?;
    let request_ref = prepared.prepared_request.ok_or_else(corrupt)?;
    let operation = Sha256::digest(format!(
        "{}:{}:{}:{}:{}",
        session.snapshot.run_id,
        session.epoch,
        session.snapshot.next_index,
        preview.context_sha256,
        request_ref.sha256
    ));
    let committed = control
        .commit(session.request(CommitAgentRunRecoveryRequest {
            schema_version: 1,
            operation_id: format!("agent-web-recovery-{operation:x}"),
            actor: "operator:agent-web".into(),
            run_id: session.snapshot.run_id.clone(),
            fence: Some(ActionFence {
                action_id: session.snapshot.run_id.clone(),
                execution_epoch: session.epoch,
            }),
            expected_index: session.snapshot.next_index,
            expected_runtime_revision: revision,
            expected_request: session.snapshot.request.clone(),
            selection: Some(selected),
            context_sha256: preview.context_sha256,
            context: prepared.recovery_context,
            request: Some(request_ref.clone()),
            event_bodies: prepared.recovery_event_bodies,
        }))
        .await
        .map_err(recovery_error)?
        .into_inner();
    let acknowledged = committed.run.ok_or_else(corrupt)?;
    if acknowledged.run_id != session.snapshot.run_id
        || acknowledged.runtime_revision != revision + 1
        || acknowledged.request.as_ref() != Some(&request_ref)
    {
        return Err(corrupt());
    }
    session.refresh().await?;
    Ok(Json(
        json!({"ok":true,"started":true,"run":session.public_run().await?}),
    ))
}

pub(super) async fn resume_run(
    State(config): State<Option<ReadConfig>>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<Value>, ReadError> {
    recover(config, id, body, Mode::Resume).await
}

pub(super) async fn rerun_run(
    State(config): State<Option<ReadConfig>>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<Value>, ReadError> {
    recover(config, id, body, Mode::Rerun).await
}

#[cfg(test)]
#[path = "recovery_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "recovery_body_tests.rs"]
mod body_tests;
