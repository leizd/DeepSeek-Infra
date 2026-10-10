use deepseek_federation::create_control_signer_bundle;
use deepseek_protocol::ActionFence;
use deepseek_protocol::generated::deepseek::action::v1::{
    AdmitStatus, ControlSigningPurpose, InstallAuthoritativeEpochRequest, SignControlRequest,
    StorageConditionType, StorageMutationRequest, StoragePrecondition,
    control_signer_server::ControlSigner as SignerRpc, worker_server::Worker as WorkerRpc,
};
use deepseek_worker::{
    CallerIdentity, StaticTokenAuthenticator, Worker, WorkerAuthorityConfig, WorkerRpcService,
    load_control_signer_from_env,
};
use std::{collections::HashMap, fs, path::Path, sync::Arc};
use tonic::{Code, Request};
const TOKEN: &str = "isolated-control-service-token-32bytes";

fn setup() -> (
    tempfile::TempDir,
    WorkerAuthorityConfig,
    HashMap<String, String>,
) {
    let root = tempfile::tempdir().unwrap();
    let password = b"isolated-control-password-32-bytes";
    let bundle = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    let public = bundle["binding"]["signerPublicKey"]
        .as_str()
        .unwrap()
        .into();
    let bundle_path = root.path().join("key.encrypted.json");
    let password_path = root.path().join("credential");
    fs::write(&bundle_path, serde_json::to_vec(&bundle).unwrap()).unwrap();
    fs::write(&password_path, password).unwrap();
    let env = HashMap::from([
        (
            "DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE".into(),
            bundle_path.to_str().unwrap().into(),
        ),
        (
            "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE".into(),
            password_path.to_str().unwrap().into(),
        ),
    ]);
    (
        root,
        WorkerAuthorityConfig {
            signer_public_key: public,
            fleet_id: "fleet-a".into(),
            environment: "test".into(),
            fencing_token: 1,
            now: None,
        },
        env,
    )
}
fn service(
    root: &Path,
    config: &WorkerAuthorityConfig,
    env: &HashMap<String, String>,
) -> WorkerRpcService {
    let worker = Worker::open_with_authority(config.clone(), root).unwrap();
    let signer = load_control_signer_from_env(
        |n| env.get(n).cloned().ok_or(std::env::VarError::NotPresent),
        Some(config),
        Some(root),
        true,
    )
    .unwrap()
    .unwrap();
    WorkerRpcService::new_with_authenticator(
        worker,
        Arc::new(StaticTokenAuthenticator::new(
            TOKEN,
            CallerIdentity {
                service_name: "go-control-plane".into(),
                role: "controller".into(),
            },
        )),
    )
    .with_control_signer(signer)
}
fn epoch() -> SignControlRequest {
    SignControlRequest {
        purpose: ControlSigningPurpose::InstallEpoch as i32,
        fence: Some(ActionFence {
            action_id: "native-action".into(),
            execution_epoch: 1,
        }),
        request_id: "a".repeat(64),
        nonce: "b".repeat(64),
        revision: 2,
        fencing_token: 1,
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
        ..Default::default()
    }
}
fn auth<T>(value: T) -> Request<T> {
    let mut r = Request::new(value);
    r.metadata_mut()
        .insert("authorization", format!("Bearer {TOKEN}").parse().unwrap());
    r
}

#[tokio::test]
async fn agent_execution_signing_requires_a_live_go_authority() {
    let (root, config, env) = setup();
    let service = service(root.path(), &config, &env);
    let mut request = epoch();
    // Additive Agent purpose. It must consult Go rather than accept a caller's
    // assertion that its lease is live or fall through to storage issuance.
    request.purpose = 3;
    let error = service.sign_control(auth(request)).await.unwrap_err();
    assert_eq!(error.code(), Code::Unavailable);
    assert_eq!(error.message(), "AGENT_EXECUTION_CONTROL_UNAVAILABLE");
}
fn grant() -> SignControlRequest {
    let mut request = epoch();
    request.purpose = ControlSigningPurpose::StoragePut as i32;
    request.request_id = "c".repeat(64);
    request.nonce = "d".repeat(64);
    request.storage_intent = Some(StorageMutationRequest {
        fence: request.fence.clone(),
        operation_id: "e".repeat(64),
        request_id: request.request_id.clone(),
        nonce: request.nonce.clone(),
        mutation_type: "PUT_CHUNK".into(),
        provider: "s3".into(),
        target_identity: "f".repeat(64),
        bucket: "native-fixture".into(),
        prefix: "native".into(),
        object_key: "chunk".into(),
        payload_digest: "a".repeat(64),
        expected_length: 1,
        precondition: Some(StoragePrecondition {
            condition_type: StorageConditionType::CreateOnly as i32,
            expected_etag: String::new(),
        }),
        schema_version: 1,
        ..Default::default()
    });
    request
}

#[tokio::test]
async fn rust_custody_issues_installs_and_replays_after_restart() {
    let (root, config, env) = setup();
    let svc = service(root.path(), &config, &env);
    let signed = svc.sign_control(auth(epoch())).await.unwrap().into_inner();
    assert_eq!(signed.signer_public_key, config.signer_public_key);
    // Issuance cannot install authority or authorize a storage grant.
    assert_eq!(
        svc.sign_control(auth(grant())).await.unwrap_err().code(),
        Code::FailedPrecondition
    );
    let installed = svc
        .install_authoritative_epoch(auth(InstallAuthoritativeEpochRequest {
            fence: signed.fence.clone(),
            canonical_request: signed.canonical_document.clone(),
        }))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(installed.status, AdmitStatus::Admitted as i32);
    let signed_grant = svc.sign_control(auth(grant())).await.unwrap().into_inner();
    let mut changed = epoch();
    changed.revision = 3;
    assert_eq!(
        svc.sign_control(auth(changed)).await.unwrap_err().code(),
        Code::AlreadyExists
    );
    let mut reused = epoch();
    reused.request_id = "f".repeat(64);
    assert_eq!(
        svc.sign_control(auth(reused)).await.unwrap_err().message(),
        "CONTROL_SIGNER_NONCE_REUSE"
    );
    drop(svc);
    let restarted = service(root.path(), &config, &env);
    assert_eq!(
        restarted
            .sign_control(auth(epoch()))
            .await
            .unwrap()
            .into_inner(),
        signed
    );
    assert_eq!(
        restarted
            .sign_control(auth(grant()))
            .await
            .unwrap()
            .into_inner(),
        signed_grant
    );
    let mut takeover = epoch();
    takeover.fence.as_mut().unwrap().execution_epoch = 2;
    takeover.request_id = "1".repeat(64);
    takeover.nonce = "2".repeat(64);
    let next = restarted
        .sign_control(auth(takeover))
        .await
        .unwrap()
        .into_inner();
    restarted
        .install_authoritative_epoch(auth(InstallAuthoritativeEpochRequest {
            fence: next.fence,
            canonical_request: next.canonical_document,
        }))
        .await
        .unwrap();
    assert_eq!(
        restarted
            .sign_control(auth(grant()))
            .await
            .unwrap_err()
            .message(),
        "STALE_EXECUTION_EPOCH"
    );
}

#[tokio::test]
async fn signer_rejects_untrusted_scope_and_unavailable_custody() {
    let (root, config, env) = setup();
    let svc = service(root.path(), &config, &env);
    assert_eq!(
        svc.sign_control(Request::new(epoch()))
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    for change in 0..10 {
        let mut request = epoch();
        match change {
            0 => request.purpose = 0,
            1 => request.fence = None,
            2 => request.fencing_token = 2,
            3 => request.fleet_id = "fleet-b".into(),
            4 => request.environment = "prod".into(),
            5 => request.request_id = "bad".into(),
            6 => request.revision = 0,
            7 => request.expires_at = "2020-01-01T00:00:00Z".into(),
            8 => request.storage_intent = grant().storage_intent,
            9 => request.fence.as_mut().unwrap().execution_epoch = u64::MAX,
            _ => unreachable!(),
        };
        assert!(
            svc.sign_control(auth(request)).await.is_err(),
            "case {change}"
        );
    }
    let unavailable = WorkerRpcService::new_with_authenticator(
        Worker::new(),
        Arc::new(StaticTokenAuthenticator::new(
            TOKEN,
            CallerIdentity {
                service_name: "go-control-plane".into(),
                role: "controller".into(),
            },
        )),
    );
    assert_eq!(
        unavailable
            .sign_control(auth(epoch()))
            .await
            .unwrap_err()
            .code(),
        Code::Unavailable
    );
    let wrong_role = WorkerRpcService::new_with_authenticator(
        Worker::new(),
        Arc::new(StaticTokenAuthenticator::new(
            TOKEN,
            CallerIdentity {
                service_name: "other".into(),
                role: "worker".into(),
            },
        )),
    );
    assert_eq!(
        wrong_role
            .sign_control(auth(epoch()))
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
}

#[test]
fn invalid_key_or_journal_never_falls_back_to_memory() {
    let (root, config, env) = setup();
    drop(service(root.path(), &config, &env));
    let load = |values: &HashMap<String, String>, tls| {
        load_control_signer_from_env(
            |n| values.get(n).cloned().ok_or(std::env::VarError::NotPresent),
            Some(&config),
            Some(root.path()),
            tls,
        )
    };
    assert!(load(&env, false).is_err());
    let mut partial = env.clone();
    partial.remove("DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE");
    assert!(load(&partial, true).is_err());
    partial.insert(
        "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE".into(),
        "".into(),
    );
    assert!(load(&partial, true).is_err());
    assert!(load(&HashMap::new(), true).unwrap().is_none());
    fs::write(
        root.path().join("rust-worker/control-signatures.sqlite3"),
        b"foreign state",
    )
    .unwrap();
    assert!(load(&env, true).is_err());
    assert!(!format!("{:?}", load(&env, true).err().unwrap()).contains("password"));
}

#[test]
fn native_initializer_only_exports_public_metadata_and_refuses_overwrite() {
    let root = tempfile::tempdir().unwrap();
    let bundle = root.path().join("key.encrypted.json");
    let credential = root.path().join("credential");
    fs::write(&credential, b"isolated-control-password-32-bytes").unwrap();
    let run = || {
        let mut command =
            std::process::Command::new(env!("CARGO_BIN_EXE_deepseek-control-signer-init"));
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        command
            .env_remove("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY")
            .env("DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE", &bundle)
            .env(
                "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE",
                &credential,
            )
            .env("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", "fleet-a")
            .env("DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT", "test")
            .output()
            .unwrap()
    };
    let first = run();
    assert!(first.status.success());
    let public: serde_json::Value = serde_json::from_slice(&first.stdout).unwrap();
    assert!(public.get("signerPublicKey").is_some());
    assert!(public.get("privateKeyEnvelope").is_none());
    let bytes = fs::read(&bundle).unwrap();
    let second = run();
    assert!(!second.status.success());
    assert!(second.stdout.is_empty());
    assert_eq!(bytes, fs::read(&bundle).unwrap());
    assert!(!String::from_utf8_lossy(&first.stdout).contains("password"));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&bundle).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
}

#[cfg(unix)]
#[test]
fn signature_journal_refuses_an_auxiliary_symlink_without_touching_its_target() {
    let (root, config, env) = setup();
    drop(service(root.path(), &config, &env));
    let foreign = root.path().join("foreign-data");
    fs::write(&foreign, b"retained foreign bytes").unwrap();
    std::os::unix::fs::symlink(
        &foreign,
        root.path()
            .join("rust-worker/control-signatures.sqlite3-journal"),
    )
    .unwrap();
    let loaded = load_control_signer_from_env(
        |n| env.get(n).cloned().ok_or(std::env::VarError::NotPresent),
        Some(&config),
        Some(root.path()),
        true,
    );
    assert!(loaded.is_err());
    assert_eq!(fs::read(&foreign).unwrap(), b"retained foreign bytes");
}

#[derive(Clone)]
struct AgentLeaseFixture {
    current: Arc<
        tokio::sync::RwLock<
            deepseek_protocol::generated::deepseek::agent::v1::AgentRunExecutionClaim,
        >,
    >,
}
#[tonic::async_trait]
impl deepseek_protocol::generated::deepseek::agent::v1::agent_run_control_server::AgentRunControl
    for AgentLeaseFixture
{
    async fn get_authority(
        &self,
        _: Request<deepseek_protocol::generated::deepseek::agent::v1::AgentRunAuthorityInput>,
    ) -> Result<
        tonic::Response<deepseek_protocol::generated::deepseek::agent::v1::AgentRunAuthorityOutput>,
        tonic::Status,
    > {
        Err(tonic::Status::unimplemented("fixture"))
    }
    async fn append_event(
        &self,
        _: Request<deepseek_protocol::generated::deepseek::agent::v1::AppendAgentRunEventRequest>,
    ) -> Result<
        tonic::Response<
            deepseek_protocol::generated::deepseek::agent::v1::AppendAgentRunEventResponse,
        >,
        tonic::Status,
    > {
        Err(tonic::Status::unimplemented("fixture"))
    }
    async fn get_run(
        &self,
        _: Request<deepseek_protocol::generated::deepseek::agent::v1::GetAgentRunRequest>,
    ) -> Result<
        tonic::Response<deepseek_protocol::generated::deepseek::agent::v1::GetAgentRunResponse>,
        tonic::Status,
    > {
        Err(tonic::Status::unimplemented("fixture"))
    }
    async fn events_after(
        &self,
        _: Request<deepseek_protocol::generated::deepseek::agent::v1::AgentRunEventsRequest>,
    ) -> Result<
        tonic::Response<deepseek_protocol::generated::deepseek::agent::v1::AgentRunEventsResponse>,
        tonic::Status,
    > {
        Err(tonic::Status::unimplemented("fixture"))
    }
    async fn claim_execution(
        &self,
        _: Request<
            deepseek_protocol::generated::deepseek::agent::v1::ClaimAgentRunExecutionRequest,
        >,
    ) -> Result<
        tonic::Response<
            deepseek_protocol::generated::deepseek::agent::v1::ClaimAgentRunExecutionResponse,
        >,
        tonic::Status,
    > {
        Err(tonic::Status::unimplemented("fixture"))
    }
    async fn renew_execution(
        &self,
        request: Request<
            deepseek_protocol::generated::deepseek::agent::v1::RenewAgentRunExecutionRequest,
        >,
    ) -> Result<
        tonic::Response<
            deepseek_protocol::generated::deepseek::agent::v1::RenewAgentRunExecutionResponse,
        >,
        tonic::Status,
    > {
        use deepseek_protocol::generated::deepseek::agent::v1::RenewAgentRunExecutionResponse;
        assert_eq!(
            request
                .metadata()
                .get("authorization")
                .unwrap()
                .to_str()
                .unwrap(),
            format!("Bearer {TOKEN}")
        );
        let current = self.current.read().await.clone();
        let input = request.into_inner();
        if input.fence != current.fence
            || input.run_id != current.run_id
            || input.owner != current.owner
            || input.claim_token != current.claim_token
        {
            return Err(tonic::Status::failed_precondition("lease denied"));
        }
        assert_eq!(input.lease_seconds, 60);
        Ok(tonic::Response::new(RenewAgentRunExecutionResponse {
            claim: Some(current),
        }))
    }
}

fn agent_claim() -> deepseek_protocol::generated::deepseek::agent::v1::AgentRunExecutionClaim {
    use deepseek_protocol::generated::deepseek::agent::v1::{
        AgentArtifactReference, AgentRunExecutionClaim, AgentRunExecutionPhase,
    };
    use sha2::{Digest, Sha256};
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    AgentRunExecutionClaim {
        fence: Some(ActionFence {
            action_id: format!(
                "agent-exec-{:x}",
                Sha256::digest(b"agent-run-execution-v1\nagent-run\nplan\n2\n")
            ),
            execution_epoch: 1,
        }),
        run_id: "agent-run".into(),
        phase: AgentRunExecutionPhase::Plan as i32,
        request: Some(AgentArtifactReference {
            sha256: "c".repeat(64),
            length: 24,
        }),
        plan_digest: "d".repeat(64),
        metadata_index: 2,
        metadata_epoch: 3,
        owner: "rust-agent-worker".into(),
        claim_token: "private-claim-token".into(),
        lease_until: now + 60,
        claim_revision: 2,
        writer_fencing_token: 1,
        writer_lease_until: now + 120,
        state: "CLAIMED".into(),
        reconciliation_required: false,
    }
}
fn agent_sign_request(
    claim: &deepseek_protocol::generated::deepseek::agent::v1::AgentRunExecutionClaim,
) -> SignControlRequest {
    SignControlRequest {
        purpose: ControlSigningPurpose::AgentExecution as i32,
        fence: claim.fence.clone(),
        request_id: "e".repeat(64),
        nonce: "f".repeat(64),
        revision: claim.claim_revision,
        fencing_token: claim.writer_fencing_token as u64,
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
        agent_intent: Some(claim.clone()),
        ..Default::default()
    }
}

#[tokio::test]
async fn agent_epoch_installation_rejects_unchecked_claims() {
    let (root, config, env) = setup();
    let service = service(root.path(), &config, &env);
    let claim = agent_claim();
    let mut request = epoch();
    request.fence = claim.fence;
    let error = service.sign_control(auth(request)).await.unwrap_err();
    assert_eq!(error.code(), Code::FailedPrecondition);
    assert_eq!(error.message(), "AGENT_EPOCH_AUTHORITY_REQUIRED");
}

#[tokio::test]
async fn agent_epoch_installation_needs_its_live_go_lease() {
    let (root, config, env) = setup();
    let service = service(root.path(), &config, &env);
    let mut request = agent_sign_request(&agent_claim());
    request.purpose = 4; // Additive typed Agent epoch-installation purpose.
    let error = service.sign_control(auth(request)).await.unwrap_err();
    assert_eq!(error.code(), Code::Unavailable);
    assert_eq!(error.message(), "AGENT_EXECUTION_CONTROL_UNAVAILABLE");
}

#[tokio::test]
async fn agent_execution_grant_binds_current_lease_and_preserves_retries() {
    use deepseek_protocol::generated::deepseek::agent::v1::agent_run_control_server::AgentRunControlServer;
    use deepseek_worker::{
        AgentExecutionControl, AgentExecutionGrantContext, verify_agent_execution_grant,
    };
    let claim = agent_claim();
    let current = Arc::new(tokio::sync::RwLock::new(claim.clone()));
    let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = socket.local_addr().unwrap();
    drop(socket);
    let (stop, done) = tokio::sync::oneshot::channel();
    let fixture = AgentLeaseFixture {
        current: current.clone(),
    };
    let server = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(AgentRunControlServer::new(fixture))
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
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    assert!(ready);
    let (root, config, env) = setup();
    let service = service(root.path(), &config, &env).with_agent_execution_control(
        AgentExecutionControl::new(&format!("http://{address}"), TOKEN).unwrap(),
    );
    assert!(
        service
            .sign_control(auth(agent_sign_request(&claim)))
            .await
            .is_err()
    );
    let mut installation = agent_sign_request(&claim);
    installation.purpose = ControlSigningPurpose::AgentInstallEpoch as i32;
    installation.request_id = "a".repeat(64);
    installation.nonce = "b".repeat(64);
    let signed = service
        .sign_control(auth(installation.clone()))
        .await
        .unwrap()
        .into_inner();
    let epoch_document: serde_json::Value =
        serde_json::from_slice(&signed.canonical_document).unwrap();
    assert_eq!(
        epoch_document["schema"],
        deepseek_worker::AUTHORITY_REQUEST_SCHEMA
    );
    assert_eq!(epoch_document["payload"], serde_json::json!({}));
    assert_eq!(
        epoch_document["expiresAt"],
        time::OffsetDateTime::from_unix_timestamp(claim.lease_until)
            .unwrap()
            .format(&time::format_description::well_known::Rfc3339)
            .unwrap()
    );
    let repeated_epoch = service
        .sign_control(auth(installation))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(repeated_epoch.canonical_document, signed.canonical_document);
    service
        .install_authoritative_epoch(auth(InstallAuthoritativeEpochRequest {
            fence: claim.fence.clone(),
            canonical_request: signed.canonical_document,
        }))
        .await
        .unwrap();
    let request = agent_sign_request(&claim);
    let signed = service
        .sign_control(auth(request.clone()))
        .await
        .unwrap()
        .into_inner();
    let document: serde_json::Value = serde_json::from_slice(&signed.canonical_document).unwrap();
    assert_eq!(
        document["schema"],
        deepseek_worker::AGENT_EXECUTION_GRANT_SCHEMA
    );
    assert_eq!(document["payload"]["metadataEpoch"], 3);
    assert_eq!(document["payload"]["requestSha256"], "c".repeat(64));
    assert!(
        !String::from_utf8(signed.canonical_document.clone())
            .unwrap()
            .contains(&claim.claim_token)
    );
    let now = time::OffsetDateTime::now_utc()
        .replace_nanosecond(0)
        .unwrap()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap();
    let context = AgentExecutionGrantContext {
        now: &now,
        authority: &config,
        claim: &claim,
    };
    verify_agent_execution_grant(&signed.canonical_document, &context).unwrap();
    current.write().await.lease_until += 20;
    let replay = service
        .sign_control(auth(request.clone()))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(replay.canonical_document, signed.canonical_document);
    let mut other = request.clone();
    other.agent_intent.as_mut().unwrap().plan_digest = "9".repeat(64);
    assert!(service.sign_control(auth(other)).await.is_err());
    current.write().await.reconciliation_required = true;
    assert!(service.sign_control(auth(request.clone())).await.is_err());
    current.write().await.reconciliation_required = false;
    current.write().await.state = "EFFECT_UNKNOWN".into();
    assert!(service.sign_control(auth(request.clone())).await.is_err());
    current.write().await.state = "CLAIMED".into();
    current.write().await.writer_lease_until = 1;
    assert!(service.sign_control(auth(request.clone())).await.is_err());
    current.write().await.writer_lease_until = claim.writer_lease_until;
    let mut bad = request.clone();
    bad.request_id = "1".repeat(64);
    bad.nonce = "2".repeat(64);
    bad.issued_at = document["issuedAt"].as_str().unwrap().into();
    bad.expires_at = time::OffsetDateTime::from_unix_timestamp(claim.writer_lease_until + 1)
        .unwrap()
        .format(&time::format_description::well_known::Rfc3339)
        .unwrap();
    assert!(service.sign_control(auth(bad)).await.is_err());
    current.write().await.lease_until = 1;
    assert!(service.sign_control(auth(request)).await.is_err());
    // A Go-authorized takeover installs a larger fence for reconciliation;
    // it cannot authorize another fresh phase effect.
    let mut takeover = claim.clone();
    takeover.fence.as_mut().unwrap().execution_epoch = 2;
    takeover.claim_revision += 1;
    takeover.owner = "rust-reconciler".into();
    takeover.claim_token = "takeover-claim-token".into();
    takeover.state = "RECONCILING".into();
    takeover.reconciliation_required = true;
    *current.write().await = takeover.clone();
    let mut takeover_epoch = agent_sign_request(&takeover);
    takeover_epoch.purpose = ControlSigningPurpose::AgentInstallEpoch as i32;
    takeover_epoch.request_id = "4".repeat(64);
    takeover_epoch.nonce = "5".repeat(64);
    let signed_epoch = service
        .sign_control(auth(takeover_epoch))
        .await
        .unwrap()
        .into_inner();
    service
        .install_authoritative_epoch(auth(InstallAuthoritativeEpochRequest {
            fence: takeover.fence.clone(),
            canonical_request: signed_epoch.canonical_document,
        }))
        .await
        .unwrap();
    assert!(
        service
            .sign_control(auth(agent_sign_request(&takeover)))
            .await
            .is_err()
    );
    assert!(
        service
            .sign_control(auth(agent_sign_request(&claim)))
            .await
            .is_err()
    );
    stop.send(()).unwrap();
    server.await.unwrap();
}

#[test]
fn agent_execution_control_rejects_external_or_ambiguous_configuration() {
    use deepseek_worker::{AgentExecutionControl, load_agent_execution_control_from_env};
    for origin in [
        "http://example.com:8090",
        "http://0.0.0.0:8090",
        "http://127.0.0.1:0",
        "http://127.0.0.1:8090/path",
        "https://127.0.0.1:8090",
    ] {
        assert!(
            AgentExecutionControl::new(origin, TOKEN).is_err(),
            "{origin}"
        );
    }
    assert!(AgentExecutionControl::new("http://127.0.0.1:8090", "short").is_err());
    let env = HashMap::from([
        ("GO_CONTROL_ADDR", "http://127.0.0.1:8090"),
        ("DEEPSEEK_GO_CONTROL_URL", "http://127.0.0.1:8091"),
        ("DEEPSEEK_INTERNAL_BEARER", TOKEN),
    ]);
    assert!(
        load_agent_execution_control_from_env(|key| env
            .get(key)
            .map(|value| value.to_string())
            .ok_or(std::env::VarError::NotPresent))
        .is_err()
    );
}
