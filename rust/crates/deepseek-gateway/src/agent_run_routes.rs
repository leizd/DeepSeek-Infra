//! Public Agent creation and reads combine Go authority with Rust body custody.

use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::{OriginalUri, Path, State},
    http::{StatusCode, Uri, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use deepseek_policy::agent_run_data::{
    ArtifactReference, ArtifactStore, ContentProjection, DataError,
};
use deepseek_protocol::generated::deepseek::{
    agent::v1::{agent_run_control_client::AgentRunControlClient, *},
    common::v1::ActionFence,
};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tonic::{
    metadata::{Ascii, MetadataValue},
    transport::Channel,
};

mod recovery;
mod runtime;
use runtime::{operator_event_with_runtime, prepare_runtime_update, request_credential};

#[derive(Clone)]
struct ReadConfig {
    origin: String,
    bearer: String,
    artifacts: PathBuf,
    credentials: Option<crate::agent_credential_client::WorkerConfig>,
}

#[derive(Clone, Copy, Debug)]
struct ReadError {
    status: StatusCode,
    code: &'static str,
    message: &'static str,
}

impl IntoResponse for ReadError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({"error":self.message,"code":self.code})),
        )
            .into_response()
    }
}

fn control_unavailable() -> ReadError {
    ReadError {
        status: StatusCode::SERVICE_UNAVAILABLE,
        code: "NATIVE_AGENT_CONTROL_UNAVAILABLE",
        message: "Agent control is unavailable",
    }
}

fn corrupt() -> ReadError {
    ReadError {
        status: StatusCode::SERVICE_UNAVAILABLE,
        code: "NATIVE_AGENT_DATA_CORRUPT",
        message: "Agent run data is corrupted",
    }
}

fn not_found() -> ReadError {
    ReadError {
        status: StatusCode::NOT_FOUND,
        code: "NOT_FOUND",
        message: "Agent run not found",
    }
}

fn data_error(error: DataError) -> ReadError {
    match error {
        DataError::Corrupt | DataError::Invalid => corrupt(),
        _ => ReadError {
            status: StatusCode::SERVICE_UNAVAILABLE,
            code: "NATIVE_AGENT_DATA_UNAVAILABLE",
            message: "Agent run bodies are unavailable",
        },
    }
}

fn rpc_error(error: tonic::Status) -> ReadError {
    match error.code() {
        tonic::Code::NotFound => not_found(),
        tonic::Code::DataLoss => corrupt(),
        _ => control_unavailable(),
    }
}

fn valid_origin(origin: &str) -> bool {
    reqwest::Url::parse(origin.trim()).is_ok_and(|url| {
        let loopback = url.host_str().is_some_and(|host| {
            host == "localhost"
                || host
                    .trim_matches(['[', ']'])
                    .parse::<std::net::IpAddr>()
                    .is_ok_and(|ip| ip.is_loopback())
        });
        loopback
            && matches!(url.scheme(), "http" | "https")
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none()
            && url.path() == "/"
    })
}

fn run_id(raw: &str) -> Result<String, ReadError> {
    let id = deepseek_policy::multi_agent::strip(raw);
    let tail = id.strip_prefix("run_").ok_or_else(not_found)?;
    if !(8..=80).contains(&tail.len())
        || !tail
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    {
        return Err(not_found());
    }
    Ok(id.into())
}

fn cursor(uri: &Uri) -> i64 {
    let origin = format!("http://127.0.0.1{uri}");
    let raw = reqwest::Url::parse(&origin)
        .ok()
        .and_then(|url| {
            url.query_pairs()
                .filter(|(key, _)| key == "after")
                .map(|(_, value)| value.into_owned())
                .last()
        })
        .unwrap_or_else(|| "-1".into());
    if let Some(value) = deepseek_policy::core_utils::python_int_opt(Some(&json!(raw))) {
        return value.max(-1);
    }
    // Positive Python integers beyond the wire range are already past EOF.
    let stripped = deepseek_policy::multi_agent::strip(&raw).trim_start_matches('+');
    if !stripped.is_empty() && stripped.bytes().all(|byte| byte.is_ascii_digit()) {
        i64::MAX
    } else {
        -1
    }
}

fn plan_value(plan: &[AgentRunPlanNode]) -> Value {
    json!(
        plan.iter()
            .map(|node| {
                let mut value = json!({"id":node.id,"task":node.task});
                if !node.depends_on.is_empty() {
                    value["depends_on"] = json!(node.depends_on);
                }
                value
            })
            .collect::<Vec<_>>()
    )
}

fn reference(value: Option<&AgentArtifactReference>) -> Result<ArtifactReference, ReadError> {
    let value = value.ok_or_else(corrupt)?;
    Ok(ArtifactReference {
        sha256: value.sha256.clone(),
        length: value.length,
    })
}

fn hydrate(store: &ArtifactStore, metadata: &AgentRunEventMetadata) -> Result<Value, ReadError> {
    let mut value = store
        .get(&reference(metadata.body.as_ref())?)
        .map_err(data_error)?;
    if value.get("type").and_then(Value::as_str) != Some(metadata.r#type.as_str()) {
        return Err(corrupt());
    }
    for (key, expected) in [
        ("phase", &metadata.phase),
        ("status", &metadata.status),
        ("scope", &metadata.scope),
    ] {
        if let Some(field) = value.get(key) {
            let text = if deepseek_policy::core_utils::python_truthy(field) {
                deepseek_policy::python_json::value_str(field)
            } else {
                String::new()
            };
            if text != *expected {
                return Err(corrupt());
            }
        }
    }
    value["runId"] = json!(metadata.run_id);
    value["index"] = json!(metadata.index);
    value["createdAt"] = json!(metadata.created_at);
    Ok(value)
}

struct ReadSession {
    config: ReadConfig,
    channel: Channel,
    client: AgentRunControlClient<Channel>,
    authorization: MetadataValue<Ascii>,
    epoch: u64,
    snapshot: AgentRunSnapshot,
}

impl ReadSession {
    fn request<T>(&self, value: T) -> tonic::Request<T> {
        let mut request = tonic::Request::new(value);
        request
            .metadata_mut()
            .insert("authorization", self.authorization.clone());
        request
    }

    async fn open(config: Option<ReadConfig>, raw: &str) -> Result<Self, ReadError> {
        let mut session = Self::connect(config, raw).await?;
        session.refresh().await?;
        Ok(session)
    }

    async fn connect(config: Option<ReadConfig>, raw: &str) -> Result<Self, ReadError> {
        let id = run_id(raw)?;
        let config = config.ok_or_else(control_unavailable)?;
        if !valid_origin(&config.origin) || config.bearer.trim().len() < 32 {
            return Err(control_unavailable());
        }
        let authorization = format!("Bearer {}", config.bearer.trim())
            .parse()
            .map_err(|_| control_unavailable())?;
        let channel = tonic::transport::Endpoint::from_shared(config.origin.trim().to_string())
            .map_err(|_| control_unavailable())?
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(10))
            .connect()
            .await
            .map_err(|_| control_unavailable())?;
        let mut session = Self {
            config,
            client: AgentRunControlClient::new(channel.clone()).max_decoding_message_size(1 << 20),
            channel,
            authorization,
            epoch: 0,
            snapshot: AgentRunSnapshot {
                run_id: id,
                ..Default::default()
            },
        };
        let request = session.request(AgentRunAuthorityInput {});
        let authority = session
            .client
            .get_authority(request)
            .await
            .map_err(rpc_error)?
            .into_inner();
        if !authority.authoritative || authority.metadata_epoch == 0 {
            return Err(control_unavailable());
        }
        session.epoch = authority.metadata_epoch;
        Ok(session)
    }

    async fn refresh(&mut self) -> Result<(), ReadError> {
        let request = self.request(GetAgentRunRequest {
            run_id: self.snapshot.run_id.clone(),
            fence: Some(ActionFence {
                action_id: self.snapshot.run_id.clone(),
                execution_epoch: self.epoch,
            }),
        });
        let response = self
            .client
            .get_run(request)
            .await
            .map_err(rpc_error)?
            .into_inner();
        if !response.authoritative {
            return Err(control_unavailable());
        }
        let snapshot = response.run.ok_or_else(corrupt)?;
        if snapshot.run_id != self.snapshot.run_id
            || snapshot.next_index < 1
            || !matches!(
                snapshot.status.as_str(),
                "created"
                    | "planning"
                    | "awaiting_plan"
                    | "running"
                    | "done"
                    | "failed"
                    | "cancelled"
                    | "orphaned"
            )
        {
            return Err(corrupt());
        }
        self.snapshot = snapshot;
        Ok(())
    }

    async fn events(&mut self, after: i64) -> Result<Vec<AgentRunEventMetadata>, ReadError> {
        let mut cursor = after.max(-1);
        let mut limit = 128;
        let mut result = Vec::new();
        while cursor < self.snapshot.next_index - 1 {
            let request = self.request(AgentRunEventsRequest {
                run_id: self.snapshot.run_id.clone(),
                after: cursor,
                limit,
                fence: Some(ActionFence {
                    action_id: self.snapshot.run_id.clone(),
                    execution_epoch: self.epoch,
                }),
            });
            let response = match self.client.events_after(request).await {
                Ok(response) => response.into_inner(),
                Err(error) if error.code() == tonic::Code::ResourceExhausted && limit > 1 => {
                    limit /= 2;
                    continue;
                }
                Err(error) => return Err(rpc_error(error)),
            };
            if !response.authoritative {
                return Err(control_unavailable());
            }
            if response.events.is_empty() || response.events.len() > limit as usize {
                return Err(corrupt());
            }
            let before = cursor;
            for event in response.events {
                if event.index >= self.snapshot.next_index {
                    break;
                }
                if event.run_id != self.snapshot.run_id
                    || event.index != cursor + 1
                    || event.created_at.is_empty()
                    || !event.fence.as_ref().is_some_and(|fence| {
                        fence.action_id == event.run_id
                            && fence.execution_epoch > 0
                            && fence.execution_epoch <= self.epoch
                    })
                {
                    return Err(corrupt());
                }
                cursor = event.index;
                result.push(event);
            }
            if cursor == before {
                return Err(corrupt());
            }
        }
        Ok(result)
    }

    async fn bodies(&self, events: Vec<AgentRunEventMetadata>) -> Result<Vec<Value>, ReadError> {
        if events.is_empty() {
            return Ok(Vec::new());
        }
        let root = self.config.artifacts.clone();
        tokio::task::spawn_blocking(move || {
            let store = ArtifactStore::open_readonly(&root).map_err(data_error)?;
            events.iter().map(|event| hydrate(&store, event)).collect()
        })
        .await
        .map_err(|_| data_error(DataError::Unavailable))?
    }

    async fn public_run(&mut self) -> Result<Value, ReadError> {
        let events = self.events(-1).await?;
        if events.last() != self.snapshot.last_event.as_ref() {
            return Err(corrupt());
        }
        let bodies = self.bodies(events.clone()).await?;
        let root = self.config.artifacts.clone();
        let request_reference = reference(self.snapshot.request.as_ref())?;
        let request = tokio::task::spawn_blocking(move || {
            ArtifactStore::open_readonly(&root).and_then(|store| store.get(&request_reference))
        })
        .await
        .map_err(|_| data_error(DataError::Unavailable))?
        .map_err(data_error)?;
        let mut projection = ContentProjection::default();
        let mut plan = json!([]);
        let mut final_after = -1;
        for (metadata, body) in events.iter().zip(&bodies) {
            if metadata.r#type == "agent_plan" {
                plan = plan_value(&metadata.plan);
            }
            if metadata.r#type == "final_reset" && metadata.scope == "final_answer" {
                final_after = metadata.index;
            }
            projection.apply(body, &plan);
        }
        if plan != plan_value(&self.snapshot.plan) || final_after != self.snapshot.final_after {
            return Err(corrupt());
        }
        let mut nodes = Map::new();
        for node in &self.snapshot.nodes {
            if nodes.contains_key(&node.id) || node.id.is_empty() {
                return Err(corrupt());
            }
            nodes.insert(node.id.clone(),json!({"id":node.id,"state":node.state,"attempts":node.attempts,"latencyMs":node.latency_ms,
                "promptTokens":node.prompt_tokens,"completionTokens":node.completion_tokens,"failed":node.failed}));
        }
        let model = request
            .get("payload")
            .unwrap_or(&request)
            .get("model")
            .filter(|value| deepseek_policy::core_utils::python_truthy(value))
            .map(deepseek_policy::python_json::value_str)
            .unwrap_or_else(|| {
                deepseek_policy::model_router::ModelRouterSettings::from_env().default_model
            });
        let mut run = projection.public_value();
        let fields = json!({"runId":self.snapshot.run_id,"status":self.snapshot.status,"nextIndex":self.snapshot.next_index,
            "createdAt":events.first().map(|event|event.created_at.as_str()).unwrap_or_default(),
            "updatedAt":events.last().map(|event|event.created_at.as_str()).unwrap_or_default(),"plan":plan,"nodes":nodes,"events":bodies,
            "requestMeta":{"conversationId":self.snapshot.conversation_id,"messageId":self.snapshot.message_id,"model":model,
                "agentPreset":self.snapshot.preset,"confirmPlan":self.snapshot.confirm_plan}});
        run.as_object_mut()
            .unwrap()
            .extend(fields.as_object().unwrap().clone());
        Ok(run)
    }
}

fn read_router(enabled: bool, config: Option<ReadConfig>) -> Router {
    if !enabled {
        return Router::new();
    }
    Router::new()
        .route("/api/agent-runs", post(create))
        .route("/api/agent-runs/:run_id", get(detail))
        .route("/api/agent-runs/:run_id/plan", post(confirm_plan))
        .route("/api/agent-runs/:run_id/cancel", post(cancel_run))
        .route("/api/agent-runs/:run_id/resume", post(recovery::resume_run))
        .route("/api/agent-runs/:run_id/rerun", post(recovery::rerun_run))
        .route("/api/agent-runs/:run_id/events", get(events))
        .route("/api/agent-runs/:run_id/stream", get(stream))
        .with_state(config)
}

pub fn router() -> Router {
    let origin = crate::backup_mirror_routes::GO_CONTROL_ADDR_ENV
        .iter()
        .find_map(|key| std::env::var(key).ok());
    let config = origin.map(|origin| ReadConfig {
        origin,
        bearer: std::env::var(crate::backup_mirror_routes::INTERNAL_BEARER_ENV).unwrap_or_default(),
        credentials: crate::agent_credential_client::WorkerConfig::from_env(),
        artifacts: std::env::var_os("DEEPSEEK_AGENT_ARTIFACT_DIR")
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                crate::data_routes::workspace_root().join(".native-agent-artifacts")
            }),
    });
    read_router(crate::python_is_de_authorised(), config)
}

fn invalid_request() -> ReadError {
    ReadError {
        status: StatusCode::BAD_REQUEST,
        code: "AGENT_RUN_REQUEST_INVALID",
        message: "任务请求无效",
    }
}

// Run IDs are record identifiers, never credentials. The authority transaction
// rejects collisions; the process/time/counter tuple avoids accidental reuse.
fn new_run_id() -> Result<String, ReadError> {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| control_unavailable())?
        .as_nanos();
    let sequence = COUNTER.fetch_add(1, Ordering::Relaxed);
    let digest = Sha256::digest(format!(
        "agent-run-v1:{now}:{}:{sequence}",
        std::process::id()
    ));
    Ok(format!("run_{digest:x}"))
}

fn creation_metadata(value: &Value) -> Result<AgentRunCreateMetadata, ReadError> {
    let object = value.as_object().ok_or_else(invalid_request)?;
    let payload = object
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(invalid_request)?;
    if !payload.get("messages").is_some_and(|messages| {
        messages
            .as_array()
            .is_some_and(|entries| !entries.is_empty() && entries.len() <= 256)
    }) {
        return Err(invalid_request());
    }
    let preset = object
        .get("agentPreset")
        .map_or(Ok("full"), |v| v.as_str().ok_or_else(invalid_request))?;
    if !matches!(
        preset,
        "full" | "auto" | "plan" | "code" | "research" | "reason" | "critic" | "leader"
    ) {
        return Err(invalid_request());
    }
    let confirm = object
        .get("confirmPlan")
        .map_or(Ok(false), |v| v.as_bool().ok_or_else(invalid_request))?;
    let identity = |key: &str| -> Result<String, ReadError> {
        let text = object
            .get(key)
            .map_or(Ok(""), |v| v.as_str().ok_or_else(invalid_request))?;
        if text.len() > 512 || text.chars().any(char::is_control) {
            return Err(invalid_request());
        }
        Ok(text.to_string())
    };
    Ok(AgentRunCreateMetadata {
        preset: preset.to_string(),
        confirm_plan: confirm
            || matches!(preset, "plan" | "auto")
            || deepseek_policy::core_utils::latest_user_query(&Value::Object(payload.clone()))
                .chars()
                .count()
                > 1800,
        conversation_id: identity("conversationId")?,
        message_id: identity("messageId")?,
        request: None,
    })
}

async fn create(
    State(config): State<Option<ReadConfig>>,
    body: Bytes,
) -> Result<Json<Value>, ReadError> {
    if body.is_empty() || body.len() > 1024 * 1024 {
        return Err(invalid_request());
    }
    let input: Value = serde_json::from_slice(&body).map_err(|_| invalid_request())?;
    let mut metadata = creation_metadata(&input)?;
    if crate::agent_credential_client::has_reserved_field(&input) {
        return Err(invalid_request());
    }
    let api_key = request_credential(&input["payload"], "apiKey")?;
    let tavily_api_key = request_credential(&input["payload"], "tavilyApiKey")?;
    let mut sanitized = deepseek_policy::agent_run_data::sanitize(&input);
    let id = new_run_id()?;
    // Check the configured private control peer and actual domain authority before
    // opening writable body custody. Go alone commits the initial run metadata.
    let mut session = ReadSession::connect(config, &id).await?;
    let root = session.config.artifacts.clone();
    let base = sanitized.clone();
    let (request, status) = tokio::task::spawn_blocking(move || {
        let mut store = ArtifactStore::open_native(&root, "python_disabled").map_err(data_error)?;
        let request = store.put(&base).map_err(data_error)?;
        let status = store
            .put(&json!({"type":"run_status", "status":"created"}))
            .map_err(data_error)?;
        Ok::<_, ReadError>((request, status))
    })
    .await
    .map_err(|_| data_error(DataError::Unavailable))??;
    let request = if api_key.is_some() || tavily_api_key.is_some() {
        let private = crate::agent_credential_client::prepare(
            session
                .config
                .credentials
                .as_ref()
                .ok_or_else(control_unavailable)?,
            crate::agent_credential_client::Preparation {
                run_id: &id,
                epoch: session.epoch,
                reference: &request,
                runtime_revision: 1,
                api_key: api_key.as_ref().map(|value| value.as_str()),
                tavily_api_key: tavily_api_key.as_ref().map(|value| value.as_str()),
                runtime_update: None,
            },
        )
        .await
        .map_err(|_| control_unavailable())?
        .ok_or_else(control_unavailable)?;
        sanitized
            .as_object_mut()
            .ok_or_else(invalid_request)?
            .insert(
                crate::agent_credential_client::PRIVATE_FIELD.into(),
                private,
            );
        let root = session.config.artifacts.clone();
        tokio::task::spawn_blocking(move || {
            ArtifactStore::open_native(&root, "python_disabled")
                .and_then(|mut store| store.put(&sanitized))
                .map_err(data_error)
        })
        .await
        .map_err(|_| data_error(DataError::Unavailable))??
    } else {
        request
    };
    metadata.request = Some(AgentArtifactReference {
        sha256: request.sha256,
        length: request.length,
    });
    let request = session.request(AppendAgentRunEventRequest {
        schema_version: 1,
        operation_id: format!("{id}_create"),
        actor: "operator:agent-web".to_string(),
        run_id: id.clone(),
        expected_index: 0,
        create: Some(metadata),
        event: Some(AgentRunEventMetadata {
            r#type: "run_status".to_string(),
            status: "created".to_string(),
            body: Some(AgentArtifactReference {
                sha256: status.sha256,
                length: status.length,
            }),
            ..Default::default()
        }),
        fence: Some(ActionFence {
            action_id: id.clone(),
            execution_epoch: session.epoch,
        }),
        runtime_update: None,
    });
    session
        .client
        .append_event(request)
        .await
        .map_err(rpc_error)?;
    session.refresh().await?;
    Ok(Json(json!({"runId":id, "run":session.public_run().await?})))
}

fn action_conflict() -> ReadError {
    ReadError {
        status: StatusCode::CONFLICT,
        code: "NATIVE_AGENT_ACTION_CONFLICT",
        message: "Agent run is not ready for this action",
    }
}

fn operator_input(body: &Bytes) -> Result<Value, ReadError> {
    if body.len() > 1024 * 1024 {
        return Err(invalid_request());
    }
    let value = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(body).map_err(|_| invalid_request())?
    };
    if !value.is_object()
        || value
            .get("payload")
            .is_some_and(|payload| !payload.is_object())
    {
        return Err(invalid_request());
    }
    Ok(value)
}

fn confirmation_plan(value: &Value) -> Result<Option<Vec<AgentRunPlanNode>>, ReadError> {
    let Some(raw) = value.get("plan") else {
        return Ok(None);
    };
    let entries = raw.as_array().ok_or_else(invalid_request)?;
    if entries.is_empty() || entries.len() > 4 {
        return Err(invalid_request());
    }
    let mut plan = Vec::new();
    let mut identifiers = std::collections::BTreeSet::new();
    for entry in entries {
        let id = entry
            .get("id")
            .and_then(Value::as_str)
            .ok_or_else(invalid_request)?;
        if !matches!(id, "researcher" | "coder" | "reasoner" | "critic")
            || !identifiers.insert(id.to_string())
        {
            return Err(invalid_request());
        }
        let task = entry
            .get("task")
            .and_then(Value::as_str)
            .ok_or_else(invalid_request)?;
        if task.trim().is_empty() || task.len() > 32 * 1024 {
            return Err(invalid_request());
        }
        let dependencies = entry
            .get("depends_on")
            .or_else(|| entry.get("dependsOn"))
            .map_or(Ok(Vec::new()), |value| {
                let values = value.as_array().ok_or_else(invalid_request)?;
                if values.len() > 4 {
                    return Err(invalid_request());
                }
                values
                    .iter()
                    .map(|v| v.as_str().map(str::to_string).ok_or_else(invalid_request))
                    .collect::<Result<Vec<_>, _>>()
            })?;
        plan.push(AgentRunPlanNode {
            id: id.into(),
            task: task.into(),
            depends_on: dependencies,
        });
    }
    let mut ready = std::collections::BTreeSet::new();
    for _ in 0..plan.len() {
        let prior = ready.len();
        for node in &plan {
            if node.depends_on.iter().all(|id| ready.contains(id)) {
                ready.insert(node.id.clone());
            }
        }
        if ready.len() == plan.len() {
            return Ok(Some(plan));
        }
        if ready.len() == prior {
            break;
        }
    }
    Err(invalid_request())
}

async fn operator_event(
    session: &mut ReadSession,
    event: AgentRunEventMetadata,
    body: Value,
) -> Result<(), ReadError> {
    operator_event_with_runtime(session, event, body, None).await
}

async fn confirm_plan(
    State(config): State<Option<ReadConfig>>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<Value>, ReadError> {
    let input = operator_input(&body)?;
    let plan = confirmation_plan(&input)?;
    let mut session = ReadSession::open(config, &id).await?;
    let cancelled = session
        .events(-1)
        .await?
        .iter()
        .any(|event| event.r#type == "run_status" && event.status == "cancelled");
    if cancelled {
        return Err(action_conflict());
    }
    if session.snapshot.status == "running" && plan.is_none() {
        return Ok(Json(
            json!({"started":false,"run":session.public_run().await?}),
        ));
    }
    if session.snapshot.status != "awaiting_plan" {
        return Err(action_conflict());
    }
    let runtime_update = prepare_runtime_update(&session, &input).await?;
    if let Some(plan) = plan {
        let body = json!({"type":"agent_plan","plan":plan_value(&plan)});
        operator_event(
            &mut session,
            AgentRunEventMetadata {
                r#type: "agent_plan".into(),
                has_plan: true,
                plan,
                ..Default::default()
            },
            body,
        )
        .await?;
    }
    if session.snapshot.status != "awaiting_plan" || session.snapshot.plan.is_empty() {
        return Err(action_conflict());
    }
    operator_event_with_runtime(
        &mut session,
        AgentRunEventMetadata {
            r#type: "run_status".into(),
            status: "running".into(),
            ..Default::default()
        },
        json!({"type":"run_status","status":"running"}),
        runtime_update,
    )
    .await?;
    Ok(Json(
        json!({"started":true,"run":session.public_run().await?}),
    ))
}

async fn cancel_run(
    State(config): State<Option<ReadConfig>>,
    Path(id): Path<String>,
    body: Bytes,
) -> Result<Json<Value>, ReadError> {
    operator_input(&body)?;
    let mut session = ReadSession::open(config, &id).await?;
    if session.snapshot.status == "cancelled" {
        return Ok(Json(
            json!({"cancelled":false,"run":session.public_run().await?}),
        ));
    }
    if matches!(session.snapshot.status.as_str(), "done" | "failed") {
        return Err(action_conflict());
    }
    operator_event(
        &mut session,
        AgentRunEventMetadata {
            r#type: "run_status".into(),
            status: "cancelled".into(),
            ..Default::default()
        },
        json!({"type":"run_status","status":"cancelled"}),
    )
    .await?;
    Ok(Json(
        json!({"cancelled":true,"run":session.public_run().await?}),
    ))
}

async fn detail(
    State(config): State<Option<ReadConfig>>,
    Path(id): Path<String>,
) -> Result<Json<Value>, ReadError> {
    let mut session = ReadSession::open(config, &id).await?;
    Ok(Json(json!({"ok":true,"run":session.public_run().await?})))
}

async fn events(
    State(config): State<Option<ReadConfig>>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
) -> Result<Json<Value>, ReadError> {
    let mut session = ReadSession::open(config, &id).await?;
    let metadata = session.events(cursor(&uri)).await?;
    Ok(Json(
        json!({"ok":true,"events":session.bodies(metadata).await?}),
    ))
}

async fn stream(
    State(config): State<Option<ReadConfig>>,
    Path(id): Path<String>,
    OriginalUri(uri): OriginalUri,
) -> Result<Response, ReadError> {
    let mut session = ReadSession::open(config, &id).await?;
    let mut cursor = cursor(&uri);
    let metadata = session.events(cursor).await?;
    // Initial missing/corrupt bodies produce an HTTP error before stream headers.
    let mut pending = session.bodies(metadata).await?;
    let stream = async_stream::try_stream! {
        loop {
            for body in pending.drain(..) {
                cursor = body["index"].as_i64().unwrap_or(cursor);
                let mut bytes = serde_json::to_vec(&body).map_err(std::io::Error::other)?;
                bytes.push(b'\n');
                yield Bytes::from(bytes);
            }
            if matches!(session.snapshot.status.as_str(),"done"|"failed"|"cancelled"|"orphaned"|"awaiting_plan") && cursor>=session.snapshot.next_index-1 { break; }
            tokio::time::sleep(Duration::from_secs(1)).await;
            let next = async {
                session.refresh().await?;
                let metadata = session.events(cursor).await?;
                session.bodies(metadata).await
            }.await;
            match next {
                Ok(bodies) => pending=bodies,
                Err(error) => {
                    let body = json!({"type":"error","error":error.message,"code":error.code});
                    let mut bytes = serde_json::to_vec(&body).map_err(std::io::Error::other)?;
                    bytes.push(b'\n');
                    yield Bytes::from(bytes);
                    break;
                }
            }
        }
    };
    let stream: std::pin::Pin<
        Box<dyn futures_core::Stream<Item = Result<Bytes, std::io::Error>> + Send>,
    > = Box::pin(stream);
    Ok((
        [
            (header::CONTENT_TYPE, "application/x-ndjson; charset=utf-8"),
            (header::HeaderName::from_static("x-accel-buffering"), "no"),
        ],
        Body::from_stream(stream),
    )
        .into_response())
}

#[cfg(test)]
#[path = "agent_run_routes_tests.rs"]
mod tests;
