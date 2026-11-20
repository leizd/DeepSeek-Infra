//! Merge the retained public payload; plaintext credentials travel only over TLS.
use super::*;

pub(super) struct Draft {
    pub(super) reference: ArtifactReference,
    pub(super) api_key: Option<zeroize::Zeroizing<String>>,
    pub(super) tavily_api_key: Option<zeroize::Zeroizing<String>>,
    pub(super) update: AgentCredentialRuntimeUpdate,
    pub(super) expect_credential_object: bool,
}

pub(super) async fn prepare(session: &ReadSession, input: &Value) -> Result<Draft, ReadError> {
    let previous = reference(session.snapshot.request.as_ref())?;
    let mut body = read_body(session.config.artifacts.clone(), previous).await?;
    let object = body.as_object_mut().ok_or_else(corrupt)?;
    let inherited = object
        .remove(crate::agent_credential_client::PRIVATE_FIELD)
        .is_some();
    object.remove("_nativeRecoveryContext");
    object.remove(crate::agent_credential_client::PREPARATION_FIELD);
    if body.get("payload").is_none() {
        body = json!({"payload":body,"agentPreset":session.snapshot.preset,"confirmPlan":session.snapshot.confirm_plan,
            "conversationId":session.snapshot.conversation_id,"messageId":session.snapshot.message_id});
    }
    let payload = input.get("payload").cloned().unwrap_or_else(|| json!({}));
    let legacy_api = if inherited {
        None
    } else {
        request_credential(&body["payload"], "apiKey")?
    };
    let legacy_tavily = if inherited {
        None
    } else {
        request_credential(&body["payload"], "tavilyApiKey")?
    };
    let replace_api_key = payload.get("apiKey").is_some() || legacy_api.is_some();
    let replace_tavily_api_key = payload.get("tavilyApiKey").is_some() || legacy_tavily.is_some();
    let api_key = if payload.get("apiKey").is_some() {
        request_credential(&payload, "apiKey")?
    } else {
        legacy_api
    };
    let tavily_api_key = if payload.get("tavilyApiKey").is_some() {
        request_credential(&payload, "tavilyApiKey")?
    } else {
        legacy_tavily
    };
    body = deepseek_policy::agent_run_data::sanitize(&body);
    let selected = body
        .get_mut("payload")
        .and_then(Value::as_object_mut)
        .ok_or_else(corrupt)?;
    let safe = deepseek_policy::agent_run_data::sanitize(&payload);
    selected.extend(safe.as_object().ok_or_else(invalid_request)?.clone());
    selected.insert("agentMode".into(), Value::Bool(true));
    creation_metadata(&body)?;
    body.as_object_mut().ok_or_else(corrupt)?.insert(
        crate::agent_credential_client::PREPARATION_FIELD.into(),
        json!(new_run_id()?),
    );
    let stored = body.clone();
    let root = session.config.artifacts.clone();
    let reference = tokio::task::spawn_blocking(move || {
        ArtifactStore::open_native(&root, "python_disabled")
            .and_then(|mut store| store.put(&stored))
            .map_err(data_error)
    })
    .await
    .map_err(|_| data_error(DataError::Unavailable))??;
    let expect_credential_object = api_key.is_some()
        || tavily_api_key.is_some()
        || inherited && !replace_api_key && !replace_tavily_api_key;
    Ok(Draft {
        reference,
        api_key,
        tavily_api_key,
        expect_credential_object,
        update: AgentCredentialRuntimeUpdate {
            previous_request: session.snapshot.request.clone(),
            previous_runtime_revision: session.snapshot.runtime_revision,
            replace_api_key,
            replace_tavily_api_key,
        },
    })
}
