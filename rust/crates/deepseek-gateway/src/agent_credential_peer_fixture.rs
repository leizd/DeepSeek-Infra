//! Controlled TLS peer for public-route transport tests, not encryption proof.
use std::{
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};

use deepseek_protocol::generated::deepseek::agent::v1::{
    agent_node_worker_server::{AgentNodeWorker, AgentNodeWorkerServer},
    *,
};
use tonic::{
    Request, Response, Status,
    transport::{Identity, Server, ServerTlsConfig},
};

use crate::agent_credential_client::WorkerConfig;

const BEARER: &str = "isolated-credential-peer-transport-32-bytes";

#[derive(Clone)]
struct CustodyFixture {
    accepted: Arc<AtomicUsize>,
}

#[tonic::async_trait]
impl AgentNodeWorker for CustodyFixture {
    async fn prepare_recovery_progress(
        &self,
        _: Request<PrepareAgentRecoveryProgressRequest>,
    ) -> Result<Response<AgentRecoveryProgressReceipt>, Status> {
        Err(Status::unimplemented("NOT_A_RECOVERY_FIXTURE"))
    }

    async fn prepare_credentials(
        &self,
        request: Request<PrepareAgentCredentialsRequest>,
    ) -> Result<Response<PrepareAgentCredentialsResponse>, Status> {
        if request
            .metadata()
            .get("authorization")
            .and_then(|value| value.to_str().ok())
            != Some(format!("Bearer {BEARER}").as_str())
        {
            return Err(Status::unauthenticated("FIXTURE_CALLER_DENIED"));
        }
        let request = request.into_inner();
        let fence = request
            .fence
            .ok_or_else(|| Status::invalid_argument("FIXTURE_SCOPE_MISSING"))?;
        let reference = request
            .request
            .ok_or_else(|| Status::invalid_argument("FIXTURE_BODY_MISSING"))?;
        if request.schema_version != 1
            || request.runtime_revision != 1
            || request.run_id != fence.action_id
            || fence.execution_epoch == 0
            || request.api_key != "request-only-credential"
        {
            return Err(Status::invalid_argument("FIXTURE_SCOPE_INVALID"));
        }
        self.accepted.fetch_add(1, Ordering::SeqCst);
        Ok(Response::new(PrepareAgentCredentialsResponse {
            scope: Some(AgentCredentialScope {
                fleet_id: "fixture-fleet".into(),
                environment: "test".into(),
                run_id: request.run_id,
                metadata_epoch: fence.execution_epoch,
                runtime_revision: 1,
                request_sha256: reference.sha256,
                request_length: reference.length,
            }),
            object: Some(AgentCredentialObjectReference {
                id: "a".repeat(64),
                sha256: "b".repeat(64),
                length: 300,
            }),
            ..Default::default()
        }))
    }

    async fn execute_plan(
        &self,
        _: Request<ExecuteAgentPlanRequest>,
    ) -> Result<Response<AgentNodeEffectMetadata>, Status> {
        Err(Status::unimplemented("NOT_A_PROVIDER_FIXTURE"))
    }
    async fn execute_task(
        &self,
        _: Request<ExecuteAgentTaskRequest>,
    ) -> Result<Response<AgentNodeEffectMetadata>, Status> {
        Err(Status::unimplemented("NOT_A_PROVIDER_FIXTURE"))
    }
    async fn execute_synthesis(
        &self,
        _: Request<ExecuteAgentTaskRequest>,
    ) -> Result<Response<AgentNodeEffectMetadata>, Status> {
        Err(Status::unimplemented("NOT_A_PROVIDER_FIXTURE"))
    }
    async fn query_effect(
        &self,
        _: Request<QueryAgentNodeEffectRequest>,
    ) -> Result<Response<QueryAgentNodeEffectResponse>, Status> {
        Err(Status::unimplemented("NOT_A_PROVIDER_FIXTURE"))
    }
    async fn project_plan(
        &self,
        _: Request<ProjectAgentPlanRequest>,
    ) -> Result<Response<AgentPlanProjectionReceipt>, Status> {
        Err(Status::unimplemented("NOT_A_PROVIDER_FIXTURE"))
    }
    async fn project_synthesis(
        &self,
        _: Request<ProjectAgentSynthesisRequest>,
    ) -> Result<Response<AgentSynthesisProjectionReceipt>, Status> {
        Err(Status::unimplemented("NOT_A_PROVIDER_FIXTURE"))
    }
}

pub(super) struct Peer {
    pub(super) config: WorkerConfig,
    pub(super) accepted: Arc<AtomicUsize>,
    task: tokio::task::JoinHandle<()>,
}

impl Drop for Peer {
    fn drop(&mut self) {
        self.task.abort();
    }
}

impl Peer {
    pub(super) async fn start(root: &Path) -> Self {
        let rcgen::CertifiedKey { cert, key_pair } =
            rcgen::generate_simple_self_signed(vec!["localhost".into()]).unwrap();
        let pem = cert.pem();
        let ca_file = root.join("credential-peer-ca.pem");
        std::fs::write(&ca_file, &pem).unwrap();
        let identity = Identity::from_pem(pem, key_pair.serialize_pem());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let origin = format!("https://{}", listener.local_addr().unwrap());
        let incoming = futures_util::stream::unfold(listener, |listener| async move {
            Some((listener.accept().await.map(|(stream, _)| stream), listener))
        });
        let accepted = Arc::new(AtomicUsize::new(0));
        let fixture = CustodyFixture {
            accepted: Arc::clone(&accepted),
        };
        let task = tokio::spawn(async move {
            Server::builder()
                .tls_config(ServerTlsConfig::new().identity(identity))
                .unwrap()
                .add_service(AgentNodeWorkerServer::new(fixture))
                .serve_with_incoming(incoming)
                .await
                .unwrap();
        });
        Self {
            config: WorkerConfig {
                origin,
                ca_file,
                server_name: "localhost".into(),
                bearer: zeroize::Zeroizing::new(BEARER.into()),
            },
            accepted,
            task,
        }
    }
}

#[tokio::test]
async fn credential_transport_refuses_foreign_hostname_and_bearer() {
    let root = tempfile::tempdir().unwrap();
    let peer = Peer::start(root.path()).await;
    let reference = deepseek_policy::agent_run_data::ArtifactReference {
        sha256: "a".repeat(64),
        length: 20,
    };
    for bad_hostname in [false, true] {
        let mut config = peer.config.clone();
        if bad_hostname {
            config.server_name = "foreign.invalid".into();
        } else {
            config.bearer =
                zeroize::Zeroizing::new("foreign-credential-peer-transport-32-bytes".into());
        }
        assert!(
            crate::agent_credential_client::prepare(
                &config,
                crate::agent_credential_client::Preparation {
                    run_id: "run-fixture",
                    epoch: 1,
                    reference: &reference,
                    runtime_revision: 1,
                    api_key: Some("request-only-credential"),
                    tavily_api_key: None,
                    runtime_update: None,
                },
            )
            .await
            .is_err()
        );
    }
    assert_eq!(peer.accepted.load(Ordering::SeqCst), 0);
}
