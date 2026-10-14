//! Runtime request preparation, private Rust credential rebinding and Go CAS.
use super::*;

pub(super) fn request_credential(
    payload: &Value,
    name: &str,
) -> Result<Option<zeroize::Zeroizing<String>>, ReadError> {
    match payload.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value))
            if value.len() <= 4096 && !value.chars().any(char::is_control) =>
        {
            Ok((!value.trim().is_empty())
                .then(|| zeroize::Zeroizing::new(value.trim().to_string())))
        }
        _ => Err(invalid_request()),
    }
}

pub(super) async fn operator_event_with_runtime(
    session: &mut ReadSession,
    mut event: AgentRunEventMetadata,
    body: Value,
    runtime_update: Option<AgentRunRuntimeUpdate>,
) -> Result<(), ReadError> {
    let root = session.config.artifacts.clone();
    let reference = tokio::task::spawn_blocking(move || {
        ArtifactStore::open_native(&root, "python_disabled")
            .map_err(data_error)?
            .put(&body)
            .map_err(data_error)
    })
    .await
    .map_err(|_| data_error(DataError::Unavailable))??;
    let mut operation_input = format!(
        "{}:{}:{}:{}:{}",
        session.snapshot.run_id,
        session.epoch,
        session.snapshot.next_index,
        event.r#type,
        reference.sha256
    );
    if let Some(update) = runtime_update.as_ref() {
        let previous = update.expected_request.as_ref().ok_or_else(corrupt)?;
        let next = update.request.as_ref().ok_or_else(corrupt)?;
        operation_input.push_str(&format!(
            "\nruntime:{}:{}:{}:{}:{}",
            update.expected_runtime_revision,
            previous.sha256,
            previous.length,
            next.sha256,
            next.length
        ));
    }
    let operation = Sha256::digest(operation_input);
    event.body = Some(AgentArtifactReference {
        sha256: reference.sha256,
        length: reference.length,
    });
    let request = session.request(AppendAgentRunEventRequest {
        schema_version: 1,
        operation_id: format!("agent-web-{operation:x}"),
        actor: "operator:agent-web".into(),
        run_id: session.snapshot.run_id.clone(),
        expected_index: session.snapshot.next_index,
        create: None,
        event: Some(event),
        fence: Some(ActionFence {
            action_id: session.snapshot.run_id.clone(),
            execution_epoch: session.epoch,
        }),
        runtime_update: runtime_update.clone(),
    });
    let response = session
        .client
        .append_event(request)
        .await
        .map_err(rpc_error)?
        .into_inner();
    if let Some(update) = runtime_update {
        let acknowledged = response.run.ok_or_else(corrupt)?;
        if acknowledged.runtime_revision != update.expected_runtime_revision + 1
            || acknowledged.request != update.request
        {
            return Err(corrupt());
        }
    }
    session.refresh().await
}

pub(super) async fn prepare_runtime_update(
    session: &ReadSession,
    input: &Value,
) -> Result<Option<AgentRunRuntimeUpdate>, ReadError> {
    let Some(payload) = input
        .get("payload")
        .filter(|value| value.as_object().is_some_and(|fields| !fields.is_empty()))
    else {
        return Ok(None);
    };
    if crate::agent_credential_client::has_reserved_field(input) {
        return Err(invalid_request());
    }
    let previous_revision = session.snapshot.runtime_revision;
    if previous_revision == 0 || previous_revision >= i64::MAX as u64 {
        // Older peers cannot promise the atomic request/event swap. Refuse
        // before sending a confirmation that such a peer could silently apply.
        return Err(control_unavailable());
    }
    let previous = reference(session.snapshot.request.as_ref())?;
    let root = session.config.artifacts.clone();
    let mut assembled = tokio::task::spawn_blocking({
        let previous = previous.clone();
        move || {
            ArtifactStore::open_readonly(&root)
                .and_then(|store| store.get(&previous))
                .map_err(data_error)
        }
    })
    .await
    .map_err(|_| data_error(DataError::Unavailable))??;
    let inherited = assembled
        .as_object_mut()
        .ok_or_else(corrupt)?
        .remove(crate::agent_credential_client::PRIVATE_FIELD)
        .is_some();
    if assembled.get("payload").is_none() {
        assembled = json!({"payload":assembled, "agentPreset":session.snapshot.preset,
            "confirmPlan":session.snapshot.confirm_plan, "conversationId":session.snapshot.conversation_id,
            "messageId":session.snapshot.message_id});
    }
    let legacy_api_key = if inherited {
        None
    } else {
        request_credential(&assembled["payload"], "apiKey")?
    };
    let legacy_tavily_key = if inherited {
        None
    } else {
        request_credential(&assembled["payload"], "tavilyApiKey")?
    };
    let replace_api_key = payload.get("apiKey").is_some() || legacy_api_key.is_some();
    let replace_tavily_api_key =
        payload.get("tavilyApiKey").is_some() || legacy_tavily_key.is_some();
    let api_key = if payload.get("apiKey").is_some() {
        request_credential(payload, "apiKey")?
    } else {
        legacy_api_key
    };
    let tavily_api_key = if payload.get("tavilyApiKey").is_some() {
        request_credential(payload, "tavilyApiKey")?
    } else {
        legacy_tavily_key
    };
    assembled = deepseek_policy::agent_run_data::sanitize(&assembled);
    let selected = assembled
        .get_mut("payload")
        .and_then(Value::as_object_mut)
        .ok_or_else(corrupt)?;
    let safe_payload = deepseek_policy::agent_run_data::sanitize(payload);
    selected.extend(
        safe_payload
            .as_object()
            .ok_or_else(invalid_request)?
            .clone(),
    );
    creation_metadata(&assembled)?;
    // A preparation may seal successfully and then lose the Go cursor CAS.
    // Distinct Rust-selected proposals keep a later credential correction from
    // colliding with that orphan's immutable vault object at the same revision.
    assembled.as_object_mut().ok_or_else(corrupt)?.insert(
        crate::agent_credential_client::PREPARATION_FIELD.into(),
        json!(new_run_id()?),
    );
    let base = assembled.clone();
    let root = session.config.artifacts.clone();
    let base_reference = tokio::task::spawn_blocking(move || {
        ArtifactStore::open_native(&root, "python_disabled")
            .and_then(|mut store| store.put(&base))
            .map_err(data_error)
    })
    .await
    .map_err(|_| data_error(DataError::Unavailable))??;
    let request = if inherited || api_key.is_some() || tavily_api_key.is_some() {
        let private = crate::agent_credential_client::prepare(
            session
                .config
                .credentials
                .as_ref()
                .ok_or_else(control_unavailable)?,
            crate::agent_credential_client::Preparation {
                run_id: &session.snapshot.run_id,
                epoch: session.epoch,
                reference: &base_reference,
                runtime_revision: previous_revision + 1,
                api_key: api_key.as_ref().map(|value| value.as_str()),
                tavily_api_key: tavily_api_key.as_ref().map(|value| value.as_str()),
                runtime_update: Some(AgentCredentialRuntimeUpdate {
                    previous_request: session.snapshot.request.clone(),
                    previous_runtime_revision: previous_revision,
                    replace_api_key,
                    replace_tavily_api_key,
                }),
            },
        )
        .await
        .map_err(|_| control_unavailable())?;
        if let Some(private) = private {
            assembled.as_object_mut().ok_or_else(corrupt)?.insert(
                crate::agent_credential_client::PRIVATE_FIELD.into(),
                private,
            );
            let root = session.config.artifacts.clone();
            tokio::task::spawn_blocking(move || {
                ArtifactStore::open_native(&root, "python_disabled")
                    .and_then(|mut store| store.put(&assembled))
                    .map_err(data_error)
            })
            .await
            .map_err(|_| data_error(DataError::Unavailable))??
        } else {
            base_reference
        }
    } else {
        base_reference
    };
    Ok(Some(AgentRunRuntimeUpdate {
        expected_runtime_revision: previous_revision,
        expected_request: session.snapshot.request.clone(),
        request: Some(AgentArtifactReference {
            sha256: request.sha256,
            length: request.length,
        }),
    }))
}
