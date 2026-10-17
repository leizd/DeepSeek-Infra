use super::*;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentRunRecoveryContext, AgentRunRecoverySelection,
    CommitAgentRunRecoveryRequest, CommitAgentRunRecoveryResponse, PreviewAgentRunRecoveryRequest,
    PreviewAgentRunRecoveryResponse,
    agent_run_recovery_control_server::{AgentRunRecoveryControl, AgentRunRecoveryControlServer},
};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentRecoveryProgressContext, AgentRunRecoveryEffectSource, CommitAgentRecoveryProgressRequest,
    PreviewAgentRecoveryProgressRequest, PreviewAgentRecoveryProgressResponse,
};
use std::sync::{
    Arc,
    atomic::{AtomicU8, Ordering},
};
use tonic::Response;

const TOKEN: &str = "isolated-recovery-rpc-bearer-32-bytes";
#[derive(Clone)]
struct Preview {
    context: AgentRunRecoveryContext,
    mode: Arc<AtomicU8>,
}

impl Preview {
    fn progress(&self) -> AgentRecoveryProgressContext {
        AgentRecoveryProgressContext {
            schema_version: 1,
            run_id: self.context.run_id.clone(),
            generation: self.context.generation,
            metadata_epoch: self.context.metadata_epoch,
            expected_index: self.context.expected_index + 3,
            runtime_revision: self.context.expected_runtime_revision + 1,
            request: self.context.expected_request.clone(),
            recovery_context_sha256: crate::agent_recovery_context::digest(&self.context).unwrap(),
            task_action_id: "original-task".into(),
            task_action_revision: 7,
            task_metadata_index: self.context.expected_index + 3,
            role: "critic".into(),
            synthesize: true,
            outputs: vec![AgentRunRecoveryEffectSource::default()],
        }
    }
}

#[tonic::async_trait]
impl AgentRunRecoveryControl for Preview {
    async fn preview_progress(
        &self,
        request: Request<PreviewAgentRecoveryProgressRequest>,
    ) -> Result<Response<PreviewAgentRecoveryProgressResponse>, Status> {
        if request
            .metadata()
            .get("authorization")
            .and_then(|m| m.to_str().ok())
            != Some(format!("Bearer {TOKEN}").as_str())
        {
            return Err(Status::unauthenticated("fixture auth"));
        }
        let input = request.into_inner();
        let context = self.progress();
        assert_eq!(input.schema_version, 1);
        assert_eq!(input.run_id, context.run_id);
        assert_eq!(
            input.metadata_fence.unwrap().execution_epoch,
            context.metadata_epoch
        );
        assert_eq!(input.expected_index, context.expected_index);
        assert_eq!(input.runtime_revision, context.runtime_revision);
        assert_eq!(input.request, context.request);
        let mode = self.mode.load(Ordering::SeqCst);
        if mode == 5 {
            return Err(Status::failed_precondition("fixture stale progress"));
        }
        if mode == 6 {
            return Err(Status::unavailable("fixture unavailable"));
        }
        let mut response = PreviewAgentRecoveryProgressResponse {
            authoritative: mode != 1,
            context: Some(context),
        };
        if mode == 2 {
            response.context.as_mut().unwrap().task_action_revision += 1;
        }
        if mode == 3 {
            response.context.as_mut().unwrap().outputs.clear();
        }
        if mode == 4 {
            response.context = None;
        }
        Ok(Response::new(response))
    }

    async fn commit_progress(
        &self,
        _: Request<CommitAgentRecoveryProgressRequest>,
    ) -> Result<Response<CommitAgentRunRecoveryResponse>, Status> {
        panic!("Rust preview cannot mutate the Go progress journal")
    }

    async fn preview(
        &self,
        request: Request<PreviewAgentRunRecoveryRequest>,
    ) -> Result<Response<PreviewAgentRunRecoveryResponse>, Status> {
        if request
            .metadata()
            .get("authorization")
            .and_then(|m| m.to_str().ok())
            != Some(format!("Bearer {TOKEN}").as_str())
        {
            return Err(Status::unauthenticated("fixture auth"));
        }
        let input = request.into_inner();
        let context = &self.context;
        assert_eq!(input.schema_version, 1);
        assert_eq!(input.run_id, context.run_id);
        let fence = input.metadata_fence.unwrap();
        assert_eq!(fence.action_id, context.run_id);
        assert_eq!(fence.execution_epoch, context.metadata_epoch);
        assert_eq!(input.expected_index, context.expected_index);
        assert_eq!(
            input.expected_runtime_revision,
            context.expected_runtime_revision
        );
        assert_eq!(input.expected_request, context.expected_request);
        assert_eq!(input.selection, context.selection);
        let mode = self.mode.load(Ordering::SeqCst);
        if mode == 5 {
            return Err(Status::failed_precondition("fixture stale fence"));
        }
        if mode == 6 {
            return Err(Status::unavailable("fixture unavailable"));
        }
        let mut response = PreviewAgentRunRecoveryResponse {
            authoritative: mode != 1,
            context: Some(context.clone()),
            context_sha256: crate::agent_recovery_context::digest(context).unwrap(),
        };
        if mode == 2 {
            response.context_sha256 = "e".repeat(64);
        }
        if mode == 3 {
            response.context.as_mut().unwrap().generation += 1;
        }
        if mode == 4 {
            response.context = None;
        }
        Ok(Response::new(response))
    }

    async fn commit(
        &self,
        _: Request<CommitAgentRunRecoveryRequest>,
    ) -> Result<Response<CommitAgentRunRecoveryResponse>, Status> {
        panic!("Credential preparation must never commit Go recovery metadata")
    }
}

#[tokio::test]
async fn recovery_context_is_rechecked_through_authenticated_authoritative_preview() {
    let context = AgentRunRecoveryContext {
        schema_version: 1,
        run_id: "run-rpc-recovery".into(),
        generation: 1,
        metadata_epoch: 3,
        expected_index: 12,
        expected_runtime_revision: 2,
        expected_request: Some(AgentArtifactReference {
            sha256: "a".repeat(64),
            length: 30,
        }),
        selection: Some(AgentRunRecoverySelection {
            kind: 1,
            agent_id: String::new(),
            resynthesize: None,
        }),
        ..Default::default()
    };
    let mode = Arc::new(AtomicU8::new(0));
    let fixture = Preview {
        context: context.clone(),
        mode: Arc::clone(&mode),
    };
    let progress = fixture.progress();
    let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = socket.local_addr().unwrap();
    drop(socket);
    let (stop, done) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(AgentRunRecoveryControlServer::new(fixture))
            .serve_with_shutdown(address, async {
                let _ = done.await;
            })
            .await
            .unwrap();
    });
    let mut ready = false;
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(address).await.is_ok() {
            ready = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(ready);
    let origin = format!("http://{address}");
    let client = AgentExecutionControl::new(&origin, TOKEN).unwrap();
    client.validate_recovery_context(&context).await.unwrap();
    client.validate_recovery_progress(&progress).await.unwrap();
    for value in 1..=6 {
        mode.store(value, Ordering::SeqCst);
        let error = client
            .validate_recovery_context(&context)
            .await
            .unwrap_err();
        assert_eq!(
            error.code(),
            if value == 6 {
                tonic::Code::Unavailable
            } else {
                tonic::Code::FailedPrecondition
            }
        );
        let error = client
            .validate_recovery_progress(&progress)
            .await
            .unwrap_err();
        assert_eq!(
            error.code(),
            if value == 6 {
                tonic::Code::Unavailable
            } else {
                tonic::Code::FailedPrecondition
            }
        );
    }
    mode.store(0, Ordering::SeqCst);
    assert!(
        AgentExecutionControl::new(&origin, "wrong-recovery-bearer-32-characters")
            .unwrap()
            .validate_recovery_context(&context)
            .await
            .is_err()
    );
    client.validate_recovery_context(&context).await.unwrap();
    assert!(
        AgentExecutionControl::new(&origin, "wrong-recovery-bearer-32-characters")
            .unwrap()
            .validate_recovery_progress(&progress)
            .await
            .is_err()
    );
    client.validate_recovery_progress(&progress).await.unwrap();
    stop.send(()).unwrap();
    server.await.unwrap();
}
