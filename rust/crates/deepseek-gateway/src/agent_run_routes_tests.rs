use super::*;
use agent_run_control_server::{AgentRunControl, AgentRunControlServer};
use axum::{
    body::{Body, to_bytes},
    http::Request,
};
use deepseek_policy::agent_run_data::ArtifactStore;
use deepseek_protocol::generated::deepseek::common::v1::ActionFence;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tonic::{Response, Status};
use tower::ServiceExt;

const RUN: &str = "run_native_data_fixture";
const BEARER: &str = "agent-data-rpc-fixture-bearer-0000000000";

#[derive(Clone)]
struct Fixture {
    snapshot: Arc<Mutex<AgentRunSnapshot>>,
    events: Arc<Mutex<Vec<AgentRunEventMetadata>>>,
    authoritative: bool,
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

fn validate_fence(fence: Option<&ActionFence>) -> Result<(), Status> {
    if !fence.is_some_and(|value| value.action_id == RUN && value.execution_epoch == 2) {
        return Err(Status::failed_precondition("FENCE_MISMATCH"));
    }
    Ok(())
}

#[tonic::async_trait]
impl AgentRunControl for Fixture {
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
        _request: tonic::Request<AppendAgentRunEventRequest>,
    ) -> Result<Response<AppendAgentRunEventResponse>, Status> {
        Err(Status::unimplemented("READ_FIXTURE_HAS_NO_MUTATIONS"))
    }
    async fn get_run(
        &self,
        request: tonic::Request<GetAgentRunRequest>,
    ) -> Result<Response<GetAgentRunResponse>, Status> {
        authenticate(&request)?;
        if request.get_ref().run_id != RUN {
            return Err(Status::not_found("AGENT_RUN_NOT_FOUND"));
        }
        validate_fence(request.get_ref().fence.as_ref())?;
        Ok(Response::new(GetAgentRunResponse {
            authoritative: self.authoritative,
            run: Some(self.snapshot.lock().unwrap().clone()),
        }))
    }
    async fn events_after(
        &self,
        request: tonic::Request<AgentRunEventsRequest>,
    ) -> Result<Response<AgentRunEventsResponse>, Status> {
        authenticate(&request)?;
        validate_fence(request.get_ref().fence.as_ref())?;
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
        .put(&json!({"model":"deepseek-v4-pro","apiKey":"fixture-private","messages":[]}))
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
