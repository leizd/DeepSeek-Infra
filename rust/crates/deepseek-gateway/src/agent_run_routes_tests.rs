use super::*;
use agent_run_control_server::{AgentRunControl, AgentRunControlServer};
use axum::{
    body::{Body, to_bytes},
    http::Request,
};
use deepseek_policy::agent_run_data::ArtifactStore;
use deepseek_protocol::generated::deepseek::common::v1::ActionFence;
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use tonic::{Response, Status};
use tower::ServiceExt;

#[path = "agent_credential_peer_fixture.rs"]
mod credential_peer;

const RUN: &str = "run_native_data_fixture";
const BEARER: &str = "agent-data-rpc-fixture-bearer-0000000000";

#[derive(Clone)]
struct Fixture {
    snapshot: Arc<Mutex<AgentRunSnapshot>>,
    events: Arc<Mutex<Vec<AgentRunEventMetadata>>>,
    authoritative: bool,
    mutations: Arc<AtomicBool>,
}

fn authenticate<T>(request: &tonic::Request<T>) -> Result<(), Status> {
    if request
        .metadata()
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        != Some(&format!("Bearer {BEARER}"))
    {
        return Err(Status::unauthenticated("AUTH_REQUIRED"));
    }
    Ok(())
}

fn validate_fence(fence: Option<&ActionFence>, id: &str) -> Result<(), Status> {
    if !fence.is_some_and(|value| value.action_id == id && value.execution_epoch == 2) {
        return Err(Status::failed_precondition("FENCE_MISMATCH"));
    }
    Ok(())
}

#[tonic::async_trait]
impl AgentRunControl for Fixture {
    async fn refresh_authorization(
        &self,
        _request: tonic::Request<RefreshAgentRunAuthorizationRequest>,
    ) -> Result<Response<AuthorizeAgentRunExecutionResponse>, Status> {
        Err(Status::unimplemented("READ_FIXTURE_HAS_NO_EXECUTION"))
    }
    async fn authorize_execution(
        &self,
        _request: tonic::Request<ClaimAgentRunExecutionRequest>,
    ) -> Result<Response<AuthorizeAgentRunExecutionResponse>, Status> {
        Err(Status::unimplemented("READ_FIXTURE_HAS_NO_EXECUTION"))
    }
    async fn claim_execution(
        &self,
        _request: tonic::Request<ClaimAgentRunExecutionRequest>,
    ) -> Result<Response<ClaimAgentRunExecutionResponse>, Status> {
        Err(Status::unimplemented("READ_FIXTURE_HAS_NO_EXECUTION"))
    }
    async fn renew_execution(
        &self,
        _request: tonic::Request<RenewAgentRunExecutionRequest>,
    ) -> Result<Response<RenewAgentRunExecutionResponse>, Status> {
        Err(Status::unimplemented("READ_FIXTURE_HAS_NO_EXECUTION"))
    }
    async fn get_authority(
        &self,
        request: tonic::Request<AgentRunAuthorityInput>,
    ) -> Result<Response<AgentRunAuthorityOutput>, Status> {
        authenticate(&request)?;
        Ok(Response::new(AgentRunAuthorityOutput {
            authoritative: self.authoritative,
            metadata_epoch: 2,
        }))
    }
    async fn append_event(
        &self,
        request: tonic::Request<AppendAgentRunEventRequest>,
    ) -> Result<Response<AppendAgentRunEventResponse>, Status> {
        authenticate(&request)?;
        if !self.mutations.load(Ordering::SeqCst) {
            return Err(Status::unimplemented("READ_FIXTURE_HAS_NO_MUTATIONS"));
        }
        let input = request.into_inner();
        validate_fence(input.fence.as_ref(), &input.run_id)?;
        if input.create.is_none()
            && input.schema_version == 1
            && input.actor == "operator:agent-web"
            && input.operation_id.starts_with("agent-web-")
        {
            let mut snapshot = self.snapshot.lock().unwrap();
            if snapshot.run_id != input.run_id || snapshot.next_index != input.expected_index {
                return Err(Status::aborted("REVISION_CONFLICT"));
            }
            let mut event = input
                .event
                .ok_or_else(|| Status::invalid_argument("EVENT_MISSING"))?;
            if event.body.is_none() {
                return Err(Status::invalid_argument("BODY_MISSING"));
            }
            if let Some(update) = input.runtime_update {
                if snapshot.status != "awaiting_plan"
                    || event.r#type != "run_status"
                    || event.status != "running"
                    || update.expected_runtime_revision != snapshot.runtime_revision
                    || update.expected_request != snapshot.request
                    || update.request.is_none()
                {
                    return Err(Status::aborted("RUNTIME_REVISION_CONFLICT"));
                }
                snapshot.request = update.request;
                snapshot.runtime_revision += 1;
            }
            match event.r#type.as_str() {
                "run_status" if event.status == "running" || event.status == "cancelled" => {
                    snapshot.status = event.status.clone();
                }
                "agent_plan" => snapshot.plan = event.plan.clone(),
                _ => return Err(Status::invalid_argument("OPERATOR_EVENT_INVALID")),
            }
            event.run_id = input.run_id;
            event.index = snapshot.next_index;
            event.created_at = "2026-10-13T00:00:01Z".into();
            event.fence = input.fence;
            snapshot.next_index += 1;
            snapshot.last_event = Some(event.clone());
            self.events.lock().unwrap().push(event.clone());
            return Ok(Response::new(AppendAgentRunEventResponse {
                already_applied: false,
                event: Some(event),
                run: Some(snapshot.clone()),
            }));
        }
        if input.schema_version != 1
            || input.expected_index != 0
            || input.actor != "operator:agent-web"
            || input.operation_id != format!("{}_create", input.run_id)
        {
            return Err(Status::invalid_argument("CREATE_INVALID"));
        }
        let metadata = input
            .create
            .ok_or_else(|| Status::invalid_argument("CREATE_MISSING"))?;
        let mut event = input
            .event
            .ok_or_else(|| Status::invalid_argument("EVENT_MISSING"))?;
        if event.r#type != "run_status"
            || event.status != "created"
            || metadata.request.is_none()
            || event.body.is_none()
        {
            return Err(Status::invalid_argument("CREATE_INVALID"));
        }
        event.index = 0;
        event.run_id = input.run_id.clone();
        event.created_at = "2026-10-13T00:00:00Z".into();
        event.fence = input.fence;
        let snapshot = AgentRunSnapshot {
            run_id: input.run_id,
            status: "created".into(),
            next_index: 1,
            preset: metadata.preset,
            confirm_plan: metadata.confirm_plan,
            conversation_id: metadata.conversation_id,
            message_id: metadata.message_id,
            request: metadata.request,
            final_after: -1,
            last_event: Some(event.clone()),
            runtime_revision: 1,
            ..Default::default()
        };
        *self.snapshot.lock().unwrap() = snapshot.clone();
        *self.events.lock().unwrap() = vec![event.clone()];
        Ok(Response::new(AppendAgentRunEventResponse {
            already_applied: false,
            event: Some(event),
            run: Some(snapshot),
        }))
    }
    async fn get_run(
        &self,
        request: tonic::Request<GetAgentRunRequest>,
    ) -> Result<Response<GetAgentRunResponse>, Status> {
        authenticate(&request)?;
        let snapshot = self.snapshot.lock().unwrap().clone();
        if request.get_ref().run_id != snapshot.run_id {
            return Err(Status::not_found("AGENT_RUN_NOT_FOUND"));
        }
        validate_fence(request.get_ref().fence.as_ref(), &snapshot.run_id)?;
        Ok(Response::new(GetAgentRunResponse {
            authoritative: self.authoritative,
            run: Some(snapshot),
        }))
    }
    async fn events_after(
        &self,
        request: tonic::Request<AgentRunEventsRequest>,
    ) -> Result<Response<AgentRunEventsResponse>, Status> {
        authenticate(&request)?;
        validate_fence(
            request.get_ref().fence.as_ref(),
            &self.snapshot.lock().unwrap().run_id,
        )?;
        let input = request.into_inner();
        Ok(Response::new(AgentRunEventsResponse {
            authoritative: self.authoritative,
            events: self
                .events
                .lock()
                .unwrap()
                .iter()
                .filter(|event| event.index > input.after)
                .take(input.limit as usize)
                .cloned()
                .collect(),
        }))
    }
}

struct RunningFixture {
    directory: tempfile::TempDir,
    config: ReadConfig,
    fixture: Fixture,
    task: tokio::task::JoinHandle<()>,
}

impl Drop for RunningFixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn running_fixture() -> RunningFixture {
    running_fixture_with_authority(true).await
}

async fn running_fixture_with_authority(authoritative: bool) -> RunningFixture {
    let directory = tempfile::tempdir().unwrap();
    let mut store = ArtifactStore::open_native(directory.path(), "python_disabled").unwrap();
    let request = store
        .put(&json!({"model":"deepseek-v4-pro","apiKey":"fixture-private","messages":[]})) // pragma: allowlist secret
        .unwrap();
    let plan = AgentRunPlanNode {
        id: "coder".into(),
        task: "test task".into(),
        depends_on: vec![],
    };
    let bodies = vec![
        json!({"type":"run_status","status":"created","runId":"forged","index":999,"createdAt":"forged"}),
        json!({"type":"agent_plan","plan":[{"id":"coder","task":"test task"}]}),
        json!({"type":"agent_output","phase":"coder","output":{"id":"coder","content":"retained output"}}),
        json!({"type":"content","text":"old answer"}),
        json!({"type":"final_reset","scope":"final_answer"}),
        json!({"type":"content","text":"new answer"}),
        json!({"type":"done","diagnostics":{"usage":9}}),
    ];
    let events = bodies
        .iter()
        .enumerate()
        .map(|(index, body)| {
            let reference = store.put(body).unwrap();
            AgentRunEventMetadata {
                fence: Some(ActionFence {
                    action_id: RUN.into(),
                    execution_epoch: 2,
                }),
                run_id: RUN.into(),
                index: index as i64,
                created_at: format!("2026-10-09T00:00:0{index}Z"),
                r#type: body["type"].as_str().unwrap().into(),
                phase: body["phase"].as_str().unwrap_or_default().into(),
                status: body["status"].as_str().unwrap_or_default().into(),
                scope: body["scope"].as_str().unwrap_or_default().into(),
                has_plan: index == 1,
                plan: if index == 1 {
                    vec![plan.clone()]
                } else {
                    vec![]
                },
                body: Some(AgentArtifactReference {
                    sha256: reference.sha256,
                    length: reference.length,
                }),
                ..Default::default()
            }
        })
        .collect::<Vec<_>>();
    let snapshot = AgentRunSnapshot {
        run_id: RUN.into(),
        status: "done".into(),
        next_index: events.len() as i64,
        preset: "full".into(),
        runtime_revision: 1,
        request: Some(AgentArtifactReference {
            sha256: request.sha256,
            length: request.length,
        }),
        plan: vec![plan],
        final_after: 4,
        last_event: events.last().cloned(),
        nodes: vec![AgentRunNodeMetadata {
            id: "coder".into(),
            state: "succeeded".into(),
            attempts: 2,
            latency_ms: Some(8),
            prompt_tokens: 11,
            completion_tokens: 7,
            failed: false,
        }],
        ..Default::default()
    };
    drop(store);
    let fixture = Fixture {
        snapshot: Arc::new(Mutex::new(snapshot)),
        events: Arc::new(Mutex::new(events)),
        authoritative,
        mutations: Arc::new(AtomicBool::new(false)),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let incoming = futures_util::stream::unfold(listener, |listener| async move {
        Some((listener.accept().await.map(|(stream, _)| stream), listener))
    });
    let server = fixture.clone();
    let task = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(AgentRunControlServer::new(server))
            .serve_with_incoming(incoming)
            .await
            .unwrap();
    });
    let config = ReadConfig {
        origin,
        bearer: BEARER.into(),
        artifacts: directory.path().to_path_buf(),
        credentials: None,
    };
    RunningFixture {
        directory,
        config,
        fixture,
        task,
    }
}

async fn response(router: Router, path: &str) -> (StatusCode, Value) {
    let response = router
        .oneshot(Request::builder().uri(path).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 16 * 1024 * 1024)
        .await
        .unwrap();
    let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

#[tokio::test]
async fn detail_reopens_verified_bodies_and_uses_go_metadata() {
    let fixture = running_fixture().await;
    let (status, body) = response(
        read_router(true, Some(fixture.config.clone())),
        &format!("/api/agent-runs/{RUN}"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["run"]["finalAnswer"], "new answer");
    assert_eq!(
        body["run"]["agentOutputs"]["coder"]["content"],
        "retained output"
    );
    assert_eq!(body["run"]["status"], "done");
    assert_eq!(body["run"]["nextIndex"], 7);
    assert_eq!(body["run"]["nodes"]["coder"]["attempts"], 2);
    assert_eq!(body["run"]["nodes"]["coder"]["promptTokens"], 11);
    assert_eq!(body["run"]["createdAt"], "2026-10-09T00:00:00Z");
    assert_eq!(body["run"]["events"][0]["runId"], RUN);
    assert_eq!(body["run"]["events"][0]["index"], 0);
    assert!(body["run"].get("requestPayload").is_none());
    assert!(!body.to_string().contains("fixture-private"));
}

#[tokio::test]
async fn cursor_reads_and_terminal_stream_preserve_ndjson_order() {
    let fixture = running_fixture().await;
    let app = read_router(true, Some(fixture.config.clone()));
    let (status, body) = response(
        app.clone(),
        &format!("/api/agent-runs/{RUN}/events?after=4"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["events"].as_array().unwrap().len(), 2);
    assert_eq!(body["events"][0]["index"], 5);
    let (status, body) = response(
        app.clone(),
        &format!("/api/agent-runs/{RUN}/events?after=invalid"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["events"].as_array().unwrap().len(), 7);
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/api/agent-runs/{RUN}/stream?after=4"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert!(
        response.headers()["content-type"]
            .to_str()
            .unwrap()
            .starts_with("application/x-ndjson")
    );
    let bytes = tokio::time::timeout(
        Duration::from_secs(2),
        to_bytes(response.into_body(), 1024 * 1024),
    )
    .await
    .unwrap()
    .unwrap();
    let events = bytes
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice::<Value>(line).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["index"], 5);
    assert_eq!(events[1]["type"], "done");
}

#[tokio::test]
async fn unreadable_or_tampered_bodies_never_become_empty_successes() {
    let fixture = running_fixture().await;
    let mut missing = fixture.config.clone();
    missing.artifacts = fixture.directory.path().join("missing");
    let (status, _) = response(
        read_router(true, Some(missing)),
        &format!("/api/agent-runs/{RUN}"),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert!(!fixture.directory.path().join("missing").exists());
    fixture.fixture.events.lock().unwrap()[5]
        .body
        .as_mut()
        .unwrap()
        .length += 1;
    let (status, body) = response(
        read_router(true, Some(fixture.config.clone())),
        &format!("/api/agent-runs/{RUN}/events?after=4"),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["code"], "NATIVE_AGENT_DATA_CORRUPT");
}

#[tokio::test]
async fn missing_auth_shadow_mode_and_legacy_routing_fail_closed() {
    let fixture = running_fixture().await;
    let mut wrong = fixture.config.clone();
    wrong.bearer = "wrong-bearer-0000000000000000000000".into();
    let (status, _) = response(
        read_router(true, Some(wrong)),
        &format!("/api/agent-runs/{RUN}"),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    let shadow = running_fixture_with_authority(false).await;
    let (status, _) = response(
        read_router(true, Some(shadow.config.clone())),
        &format!("/api/agent-runs/{RUN}"),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    let (status, _) = response(
        read_router(false, Some(fixture.config.clone())),
        &format!("/api/agent-runs/{RUN}"),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, _) = response(read_router(true, None), "/api/agent-runs/bad").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, _) = response(read_router(true, None), &format!("/api/agent-runs/{RUN}")).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
}

#[test]
fn remote_origins_and_invalid_ids_are_rejected_before_connecting() {
    for origin in [
        "http://example.com",
        "file:///tmp/control",
        "http://127.0.0.1/path",
        "http://user@127.0.0.1",
        "http://127.0.0.1?secret=fixture",
        "http://127.0.0.1#fragment",
    ] {
        assert!(!valid_origin(origin), "{origin}");
    }
    for origin in [
        "http://127.0.0.1:8090",
        "http://localhost:8090",
        "https://[::1]:8090",
    ] {
        assert!(valid_origin(origin), "{origin}");
    }
    assert!(run_id("run_short").is_err());
    assert!(run_id("run_../../outside").is_err());
    assert_eq!(
        run_id("  run_native_fixture  ").unwrap(),
        "run_native_fixture"
    );
}

#[test]
fn python_cursor_defaults_and_past_eof_values_are_preserved() {
    for (query, expected) in [
        ("", -1),
        ("?after=invalid", -1),
        ("?after=-200", -1),
        ("?after=3", 3),
        ("?after=1&after=5", 5),
        ("?after=922337203685477580800", i64::MAX),
    ] {
        let uri = format!("/api/agent-runs/{RUN}/events{query}")
            .parse()
            .unwrap();
        assert_eq!(cursor(&uri), expected, "{query}");
    }
}

#[tokio::test]
async fn index_gaps_future_epochs_and_body_type_mismatch_are_not_replayed() {
    for kind in 0..4 {
        let fixture = running_fixture().await;
        {
            let mut events = fixture.fixture.events.lock().unwrap();
            match kind {
                0 => events[5].index = 6,
                1 => events[5].fence.as_mut().unwrap().execution_epoch = 3,
                2 => events[5].r#type = "error".into(),
                _ => events[5].index = 8,
            }
        }
        let (status, body) = response(
            read_router(true, Some(fixture.config.clone())),
            &format!("/api/agent-runs/{RUN}/events?after=4"),
        )
        .await;
        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body["code"], "NATIVE_AGENT_DATA_CORRUPT");
    }
}

#[tokio::test]
async fn running_stream_reads_events_committed_after_connection() {
    let fixture = running_fixture().await;
    let later = fixture.fixture.events.lock().unwrap()[5..].to_vec();
    {
        fixture.fixture.events.lock().unwrap().truncate(5);
        let mut snapshot = fixture.fixture.snapshot.lock().unwrap();
        snapshot.status = "running".into();
        snapshot.next_index = 5;
        snapshot.last_event = fixture.fixture.events.lock().unwrap().last().cloned();
    }
    let app = read_router(true, Some(fixture.config.clone()));
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("/api/agent-runs/{RUN}/stream?after=4"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let consumer =
        tokio::spawn(async move { to_bytes(response.into_body(), 1024 * 1024).await.unwrap() });
    tokio::time::sleep(Duration::from_millis(50)).await;
    {
        fixture.fixture.events.lock().unwrap().extend(later);
        let mut snapshot = fixture.fixture.snapshot.lock().unwrap();
        snapshot.status = "done".into();
        snapshot.next_index = 7;
        snapshot.last_event = fixture.fixture.events.lock().unwrap().last().cloned();
    }
    let bytes = tokio::time::timeout(Duration::from_secs(3), consumer)
        .await
        .unwrap()
        .unwrap();
    let events = bytes
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice::<Value>(line).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["index"], 5);
    assert_eq!(events[1]["type"], "done");
}

async fn creation_response(app: Router, input: &Value) -> (StatusCode, Value) {
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/agent-runs")
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(input).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 2 * 1024 * 1024)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

#[tokio::test]
async fn public_creation_commits_go_metadata_and_rust_sanitized_body() {
    let mut running = running_fixture().await;
    let custody = credential_peer::Peer::start(running.directory.path()).await;
    running.config.credentials = Some(custody.config.clone());
    running.fixture.mutations.store(true, Ordering::SeqCst);
    let input = json!({"payload":{"messages":[{"role":"user","content":"review a function"}],"model":"deepseek-chat", "apiKey":"request-only-credential"},
        "agentPreset":"plan", "conversationId":"conversation_1", "messageId":"message_1"});
    let (status, body) =
        creation_response(read_router(true, Some(running.config.clone())), &input).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let id = body["runId"].as_str().unwrap();
    assert!(id.starts_with("run_") && id.len() == 68);
    assert_eq!(body["run"]["runId"], id);
    assert_eq!(body["run"]["status"], "created");
    assert_eq!(body["run"]["nextIndex"], 1);
    assert_eq!(body["run"]["requestMeta"]["confirmPlan"], true);
    assert_eq!(body["run"]["requestMeta"]["model"], "deepseek-chat");
    assert_eq!(body["run"]["events"].as_array().unwrap().len(), 1);
    let snapshot = running.fixture.snapshot.lock().unwrap().clone();
    assert_eq!(snapshot.conversation_id, "conversation_1");
    assert_eq!(snapshot.message_id, "message_1");
    let store = ArtifactStore::open_readonly(&running.config.artifacts).unwrap();
    let body = store
        .get(&reference(snapshot.request.as_ref()).unwrap())
        .unwrap();
    assert_eq!(body["payload"]["messages"], input["payload"]["messages"]);
    assert!(body["payload"].get("apiKey").is_none());
    assert!(!body.to_string().contains("request-only-credential"));
    assert!(
        body.get(crate::agent_credential_client::PRIVATE_FIELD)
            .is_some()
    );
    assert_eq!(custody.accepted.load(Ordering::SeqCst), 1);
    assert_eq!(snapshot.plan.len(), 0);
    assert_eq!(snapshot.nodes.len(), 0);
}

#[tokio::test]
async fn public_creation_with_credentials_requires_private_peer_before_go_metadata_write() {
    let running = running_fixture().await;
    running.fixture.mutations.store(true, Ordering::SeqCst);
    let before = running.fixture.snapshot.lock().unwrap().clone();
    let input = json!({"payload":{"messages":[{"role":"user","content":"inspect"}],"apiKey":"request-only-credential"}});
    let (status, _) =
        creation_response(read_router(true, Some(running.config.clone())), &input).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(*running.fixture.snapshot.lock().unwrap(), before);
}

#[tokio::test]
async fn public_creation_refuses_invalid_input_without_body_or_go_writes() {
    let running = running_fixture().await;
    running.fixture.mutations.store(true, Ordering::SeqCst);
    let valid = json!({"payload":{"messages":[{"role":"user","content":"hello"}]}});
    for input in [
        Value::Null,
        json!({}),
        json!({"payload":[]}),
        json!({"payload":{"messages":[]}}),
        json!({"payload":{"messages":"bad"}}),
        json!({"payload":{"messages":[{}]},"confirmPlan":"yes"}),
        json!({"payload":{"messages":[{}]},"agentPreset":"unknown"}),
        json!({"payload":{"messages":[{}]},"conversationId":1}),
        json!({"payload":{"messages":[{}]},"messageId":"line\nbreak"}),
        json!({"payload":{"messages":[{}]},"messageId":"x".repeat(513)}),
    ] {
        let (status, body) =
            creation_response(read_router(true, Some(running.config.clone())), &input).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{input}: {body}");
        assert_eq!(body["code"], "AGENT_RUN_REQUEST_INVALID");
        assert_eq!(running.fixture.snapshot.lock().unwrap().run_id, RUN);
    }
    let (status, body) = creation_response(read_router(true, None), &valid).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    let response = read_router(false, Some(running.config.clone()))
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/agent-runs")
                .body(Body::from(valid.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn public_creation_checks_control_authority_before_opening_writable_custody() {
    let running = running_fixture_with_authority(false).await;
    let empty = tempfile::tempdir().unwrap();
    let mut config = running.config.clone();
    config.artifacts = empty.path().join("unopened");
    let input = json!({"payload":{"messages":[{"role":"user","content":"hello"}]}});
    let (status, body) = creation_response(read_router(true, Some(config)), &input).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert!(!empty.path().join("unopened").exists());
    assert_eq!(running.fixture.snapshot.lock().unwrap().run_id, RUN);
}

async fn operator_response(router: Router, path: &str, input: &Value) -> (StatusCode, Value) {
    let response = router
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(path)
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(input).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 2 * 1024 * 1024)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

#[test]
fn public_creation_preserves_auto_and_long_query_confirmation() {
    for (preset, query, explicit, expected) in [
        ("auto", "short".into(), false, true),
        ("plan", "short".into(), false, true),
        ("full", "短".repeat(1800), false, false),
        ("code", "短".repeat(1801), false, true),
        ("critic", "short".into(), true, true),
        ("research", "short".into(), false, false),
    ] {
        let input = json!({"payload":{"messages":[{"role":"user","content":query}]},"agentPreset":preset,"confirmPlan":explicit});
        assert_eq!(
            creation_metadata(&input).unwrap().confirm_plan,
            expected,
            "{preset}"
        );
    }
}

#[test]
fn confirmation_plan_refuses_invalid_or_cyclic_edits() {
    for plan in [
        json!([]),
        json!([{"id":"leader","task":"x"}]),
        json!([{"id":"coder","task":""}]),
        json!([{"id":"coder","task":"x","dependsOn":["critic"]},{"id":"critic","task":"y","dependsOn":["coder"]}]),
        json!([{"id":"coder","task":"x","dependsOn":["unknown"]}]),
        json!([{"id":"coder","task":"x"},{"id":"coder","task":"y"}]),
        json!([{"id":"critic","task":"x","dependsOn":"coder"}]),
        json!([{"id":"coder","task":"x","dependsOn":[2]}]),
    ] {
        assert!(confirmation_plan(&json!({"plan":plan})).is_err());
    }
    assert!(confirmation_plan(&json!({})).unwrap().is_none());
    let plan=confirmation_plan(&json!({"plan":[{"id":"critic","task":"review","dependsOn":["coder"]},{"id":"coder","task":"write"}]})).unwrap().unwrap();
    assert_eq!(plan.len(), 2);
}

#[tokio::test]
async fn public_plan_confirmation_uses_go_events_and_rust_custody() {
    let running = running_fixture().await;
    running.fixture.mutations.store(true, Ordering::SeqCst);
    running.fixture.snapshot.lock().unwrap().status = "awaiting_plan".into();
    let original = running.fixture.snapshot.lock().unwrap().next_index;
    let input = json!({"payload":{},"plan":[{"id":"coder","task":"updated task","dependsOn":[]}]});
    let (status, body) = operator_response(
        read_router(true, Some(running.config.clone())),
        &format!("/api/agent-runs/{RUN}/plan"),
        &input,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["started"], true);
    assert_eq!(body["run"]["status"], "running");
    assert_eq!(body["run"]["nextIndex"], original + 2);
    assert_eq!(body["run"]["plan"][0]["task"], "updated task");
    let events = running.fixture.events.lock().unwrap().clone();
    let store = ArtifactStore::open_readonly(&running.config.artifacts).unwrap();
    for event in &events[events.len() - 2..] {
        let reference = reference(event.body.as_ref()).unwrap();
        let body = store.get(&reference).unwrap();
        assert_eq!(body["type"], event.r#type);
    }
    let (status, body) = operator_response(
        read_router(true, Some(running.config.clone())),
        &format!("/api/agent-runs/{RUN}/plan"),
        &json!({"payload":{}}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["started"], false);
    assert_eq!(body["run"]["nextIndex"], original + 2);
}

#[tokio::test]
async fn public_plan_confirmation_merges_runtime_values_and_commits_the_new_reference() {
    let running = running_fixture().await;
    running.fixture.mutations.store(true, Ordering::SeqCst);
    let original_body = json!({"payload":{"messages":[{"role":"user","content":"retained query"}],
        "model":"deepseek-chat","temperature":0.2},"agentPreset":"full","confirmPlan":true});
    let original_request = ArtifactStore::open_native(&running.config.artifacts, "python_disabled")
        .unwrap()
        .put(&original_body)
        .unwrap();
    {
        let mut snapshot = running.fixture.snapshot.lock().unwrap();
        snapshot.status = "awaiting_plan".into();
        snapshot.request = Some(AgentArtifactReference {
            sha256: original_request.sha256.clone(),
            length: original_request.length,
        });
    }
    let original_index = running.fixture.snapshot.lock().unwrap().next_index;
    let (status, body) = operator_response(
        read_router(true, Some(running.config.clone())),
        &format!("/api/agent-runs/{RUN}/plan"),
        &json!({"payload":{"model":"deepseek-reasoner","temperature":0.7}}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["started"], true);
    let snapshot = running.fixture.snapshot.lock().unwrap().clone();
    assert_eq!(snapshot.runtime_revision, 2);
    assert_eq!(snapshot.next_index, original_index + 1);
    let current = reference(snapshot.request.as_ref()).unwrap();
    assert_ne!(current, original_request);
    let store = ArtifactStore::open_readonly(&running.config.artifacts).unwrap();
    let request = store.get(&current).unwrap();
    assert_eq!(request["payload"]["model"], "deepseek-reasoner");
    assert_eq!(request["payload"]["temperature"], 0.7);
    assert_eq!(
        request["payload"]["messages"],
        original_body["payload"]["messages"]
    );
    assert_eq!(store.get(&original_request).unwrap(), original_body);
    let (status, body) = operator_response(
        read_router(true, Some(running.config.clone())),
        &format!("/api/agent-runs/{RUN}/plan"),
        &json!({"payload":{"model":"different-model"}}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["started"], false);
    assert_eq!(
        running
            .fixture
            .snapshot
            .lock()
            .unwrap()
            .request
            .as_ref()
            .unwrap()
            .sha256,
        current.sha256
    );
}

#[tokio::test]
async fn runtime_updates_retain_legacy_flat_request_contents() {
    let running = running_fixture().await;
    let original = json!({"messages":[{"role":"user","content":"retained legacy query"}],"model":"deepseek-chat"});
    let previous = ArtifactStore::open_native(&running.config.artifacts, "python_disabled")
        .unwrap()
        .put(&original)
        .unwrap();
    {
        let mut snapshot = running.fixture.snapshot.lock().unwrap();
        snapshot.status = "awaiting_plan".into();
        snapshot.request = Some(AgentArtifactReference {
            sha256: previous.sha256.clone(),
            length: previous.length,
        });
    }
    let session = ReadSession::open(Some(running.config.clone()), RUN)
        .await
        .unwrap();
    let update =
        prepare_runtime_update(&session, &json!({"payload":{"model":"deepseek-reasoner"}}))
            .await
            .unwrap()
            .unwrap();
    let store = ArtifactStore::open_readonly(&running.config.artifacts).unwrap();
    let current = store
        .get(&reference(update.request.as_ref()).unwrap())
        .unwrap();
    assert_eq!(current["payload"]["messages"], original["messages"]);
    assert_eq!(current["payload"]["model"], "deepseek-reasoner");
    assert_eq!(store.get(&previous).unwrap(), original);
}

#[test]
fn runtime_credentials_keep_the_original_input_bounds_and_clear_semantics() {
    for value in [
        json!(1),
        json!([]),
        json!("invalid\nfixture"),
        json!("x".repeat(4097)),
    ] {
        assert!(request_credential(&json!({"apiKey":value}), "apiKey").is_err());
    }
    for value in [Value::Null, json!(""), json!("   ")] {
        assert!(
            request_credential(&json!({"apiKey":value}), "apiKey")
                .unwrap()
                .is_none()
        );
    }
    assert_eq!(
        request_credential(&json!({"apiKey":"  bounded-fixture  "}), "apiKey")
            .unwrap()
            .unwrap()
            .as_str(),
        "bounded-fixture"
    );
}

#[tokio::test]
async fn separate_preparations_do_not_rebind_an_orphaned_credential_scope() {
    let running = running_fixture().await;
    let original = json!({"payload":{"messages":[{"role":"user","content":"retained query"}]},"agentPreset":"full"});
    let reference = ArtifactStore::open_native(&running.config.artifacts, "python_disabled")
        .unwrap()
        .put(&original)
        .unwrap();
    {
        let mut snapshot = running.fixture.snapshot.lock().unwrap();
        snapshot.status = "awaiting_plan".into();
        snapshot.request = Some(AgentArtifactReference {
            sha256: reference.sha256,
            length: reference.length,
        });
    }
    let before = running.fixture.snapshot.lock().unwrap().clone();
    let session = ReadSession::open(Some(running.config.clone()), RUN)
        .await
        .unwrap();
    let input = json!({"payload":{"model":"deepseek-chat"}});
    let first = prepare_runtime_update(&session, &input)
        .await
        .unwrap()
        .unwrap();
    let second = prepare_runtime_update(&session, &input)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(first.expected_request, second.expected_request);
    assert_eq!(
        first.expected_runtime_revision,
        second.expected_runtime_revision
    );
    assert_ne!(first.request, second.request);
    assert_eq!(*running.fixture.snapshot.lock().unwrap(), before);
}

#[tokio::test]
async fn runtime_confirmation_refuses_old_peers_and_reserved_input_before_go_writes() {
    let running = running_fixture().await;
    running.fixture.mutations.store(true, Ordering::SeqCst);
    {
        let mut snapshot = running.fixture.snapshot.lock().unwrap();
        snapshot.status = "awaiting_plan".into();
        snapshot.runtime_revision = 0;
    }
    let before = running.fixture.snapshot.lock().unwrap().clone();
    let (status, _) = operator_response(
        read_router(true, Some(running.config.clone())),
        &format!("/api/agent-runs/{RUN}/plan"),
        &json!({"payload":{"model":"deepseek-chat"}}),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(*running.fixture.snapshot.lock().unwrap(), before);
    running.fixture.snapshot.lock().unwrap().runtime_revision = 1;
    let before = running.fixture.snapshot.lock().unwrap().clone();
    let (status, _) = operator_response(
        read_router(true, Some(running.config.clone())),
        &format!("/api/agent-runs/{RUN}/plan"),
        &json!({"payload":{"nested":{"_nativeProviderCredentials":{}}}}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(*running.fixture.snapshot.lock().unwrap(), before);
}

#[tokio::test]
async fn public_confirmation_and_cancellation_refuse_wrong_states() {
    let running = running_fixture().await;
    running.fixture.mutations.store(true, Ordering::SeqCst);
    for state in [
        "created",
        "planning",
        "done",
        "failed",
        "cancelled",
        "orphaned",
    ] {
        running.fixture.snapshot.lock().unwrap().status = state.into();
        let (status, _) = operator_response(
            read_router(true, Some(running.config.clone())),
            &format!("/api/agent-runs/{RUN}/plan"),
            &json!({"payload":{}}),
        )
        .await;
        assert_eq!(status, StatusCode::CONFLICT, "{state}");
    }
    running.fixture.snapshot.lock().unwrap().status = "awaiting_plan".into();
    let before = running.fixture.snapshot.lock().unwrap().next_index;
    let (status, body) = operator_response(
        read_router(true, Some(running.config.clone())),
        &format!("/api/agent-runs/{RUN}/cancel"),
        &json!({}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["run"]["status"], "cancelled");
    assert_eq!(body["run"]["nextIndex"], before + 1);
    let (status, body) = operator_response(
        read_router(true, Some(running.config.clone())),
        &format!("/api/agent-runs/{RUN}/cancel"),
        &json!({}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["cancelled"], false);
    for input in [json!([]), json!({"payload":[]}), json!({"plan":[]})] {
        let (status, _) = operator_response(
            read_router(true, Some(running.config.clone())),
            &format!("/api/agent-runs/{RUN}/plan"),
            &input,
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }
}

#[test]
fn public_creation_retains_all_explicit_agent_presets_and_confirmation_rules() {
    for preset in [
        "full", "code", "research", "reason", "critic", "leader", "auto", "plan",
    ] {
        let metadata = creation_metadata(&json!({"agentPreset":preset,"payload":{"messages":[{"role":"user","content":"inspect"}]}})).unwrap();
        assert_eq!(metadata.preset, preset);
        assert_eq!(metadata.confirm_plan, matches!(preset, "auto" | "plan"));
    }
}
