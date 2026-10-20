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
    atomic::{AtomicU8, AtomicUsize, Ordering},
};
use tonic::Response;

const TOKEN: &str = "isolated-recovery-rpc-bearer-32-bytes";

fn private_control_transport_fixture() -> (
    tempfile::TempDir,
    std::collections::BTreeMap<String, String>,
) {
    let directory = tempfile::tempdir().unwrap();
    let mut ca = rcgen::CertificateParams::default();
    ca.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    ca.key_usages = vec![
        rcgen::KeyUsagePurpose::KeyCertSign,
        rcgen::KeyUsagePurpose::CrlSign,
    ];
    let ca_key = rcgen::KeyPair::generate().unwrap();
    let ca_cert = ca.self_signed(&ca_key).unwrap();
    let mut client = rcgen::CertificateParams::new(vec!["rust-control-client".into()]).unwrap();
    client.key_usages = vec![rcgen::KeyUsagePurpose::DigitalSignature];
    client.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ClientAuth];
    let client_key = rcgen::KeyPair::generate().unwrap();
    let client_cert = client.signed_by(&client_key, &ca_cert, &ca_key).unwrap();
    let mut server = rcgen::CertificateParams::new(vec!["control.native".into()]).unwrap();
    server.key_usages = vec![rcgen::KeyUsagePurpose::DigitalSignature];
    server.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ServerAuth];
    let server_key = rcgen::KeyPair::generate().unwrap();
    let server_cert = server.signed_by(&server_key, &ca_cert, &ca_key).unwrap();
    let ca_path = directory.path().join("root.pem");
    let cert_path = directory.path().join("client.pem");
    let key_path = directory.path().join("client.key");
    std::fs::write(&ca_path, ca_cert.pem()).unwrap();
    std::fs::write(&cert_path, client_cert.pem()).unwrap();
    std::fs::write(&key_path, client_key.serialize_pem()).unwrap();
    std::fs::write(directory.path().join("server.pem"), server_cert.pem()).unwrap();
    std::fs::write(
        directory.path().join("server.key"),
        server_key.serialize_pem(),
    )
    .unwrap();
    let values = [
        ("GO_CONTROL_ADDR", "http://127.0.0.1:8090".to_string()),
        (
            "DEEPSEEK_GO_CONTROL_RPC_URL",
            "https://127.0.0.1:8091".to_string(),
        ),
        (
            "DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE",
            ca_path.to_string_lossy().into_owned(),
        ),
        (
            "DEEPSEEK_GO_CONTROL_RPC_TLS_CERT_FILE",
            cert_path.to_string_lossy().into_owned(),
        ),
        (
            "DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE",
            key_path.to_string_lossy().into_owned(),
        ),
        (
            "DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME",
            "control.native".to_string(),
        ),
        ("DEEPSEEK_INTERNAL_BEARER", TOKEN.to_string()),
    ];
    (
        directory,
        values
            .into_iter()
            .map(|(name, value)| (name.to_string(), value))
            .collect(),
    )
}

#[tokio::test]
async fn dedicated_control_transport_selects_private_tls_without_replacing_public_http() {
    let (directory, mut values) = private_control_transport_fixture();
    let public_socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let public_address = public_socket.local_addr().unwrap();
    let socket = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let address = socket.local_addr().unwrap();
    drop(socket);
    drop(public_socket);
    let public_origin = format!("http://{public_address}");
    values.insert("GO_CONTROL_ADDR".into(), public_origin.clone());
    values.insert(
        "DEEPSEEK_GO_CONTROL_RPC_URL".into(),
        format!("https://{address}"),
    );
    let selected = load_agent_execution_control_from_env(|name| {
        values.get(name).cloned().ok_or(VarError::NotPresent)
    })
    .unwrap()
    .expect("configured private RPC was ignored");
    assert_eq!(selected.endpoint.uri().scheme_str(), Some("https"));
    assert_eq!(
        selected.endpoint.uri().authority().unwrap().as_str(),
        address.to_string()
    );
    assert_eq!(values.get("GO_CONTROL_ADDR").unwrap(), &public_origin);
    let context = AgentRunRecoveryContext {
        schema_version: 1,
        run_id: "run-private-recovery".into(),
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
    let private_calls = Arc::new(AtomicUsize::new(0));
    let public_calls = Arc::new(AtomicUsize::new(0));
    let service = Preview {
        context: context.clone(),
        mode: Arc::new(AtomicU8::new(0)),
        calls: Arc::clone(&private_calls),
    };
    let public_service = Preview {
        context: context.clone(),
        mode: Arc::new(AtomicU8::new(0)),
        calls: Arc::clone(&public_calls),
    };
    let tls = tonic::transport::ServerTlsConfig::new()
        .identity(tonic::transport::Identity::from_pem(
            std::fs::read(directory.path().join("server.pem")).unwrap(),
            std::fs::read(directory.path().join("server.key")).unwrap(),
        ))
        .client_ca_root(tonic::transport::Certificate::from_pem(
            std::fs::read(directory.path().join("root.pem")).unwrap(),
        ));
    let (stop, done) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .tls_config(tls)
            .unwrap()
            .add_service(AgentRunRecoveryControlServer::new(service))
            .serve_with_shutdown(address, async {
                let _ = done.await;
            })
            .await
            .unwrap();
    });
    let (stop_public, done_public) = tokio::sync::oneshot::channel();
    let public_server = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(AgentRunRecoveryControlServer::new(public_service))
            .serve_with_shutdown(public_address, async {
                let _ = done_public.await;
            })
            .await
            .unwrap();
    });
    let mut ready = false;
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(address).await.is_ok()
            && tokio::net::TcpStream::connect(public_address).await.is_ok()
        {
            ready = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(ready, "private fixture never bound its listener");
    // Calibrate the successful public decoy before observing private traffic.
    AgentExecutionControl::new(&public_origin, TOKEN)
        .unwrap()
        .validate_recovery_context(&context)
        .await
        .unwrap();
    assert_eq!(public_calls.load(Ordering::SeqCst), 1);
    assert_eq!(private_calls.load(Ordering::SeqCst), 0);
    public_calls.store(0, Ordering::SeqCst);
    // Exercise the production worker recovery RPC, including its bearer and
    // original metadata fence, through a mutually authenticated TLS fixture.
    selected.validate_recovery_context(&context).await.unwrap();
    assert_eq!(private_calls.load(Ordering::SeqCst), 1);
    assert_eq!(public_calls.load(Ordering::SeqCst), 0);
    for fault in [
        "wrong-root",
        "wrong-name",
        "untrusted-client",
        "wrong-bearer",
    ] {
        let (_other, other) = private_control_transport_fixture();
        let mut rejected = values.clone();
        match fault {
            "wrong-root" => {
                rejected.insert(
                    "DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE".into(),
                    other["DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE"].clone(),
                );
            }
            "wrong-name" => {
                rejected.insert(
                    "DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME".into(),
                    "other.native".into(),
                );
            }
            "untrusted-client" => {
                for key in [
                    "DEEPSEEK_GO_CONTROL_RPC_TLS_CERT_FILE",
                    "DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE",
                ] {
                    rejected.insert(key.into(), other[key].clone());
                }
            }
            "wrong-bearer" => {
                rejected.insert("DEEPSEEK_INTERNAL_BEARER".into(), "q".repeat(48));
            }
            _ => unreachable!(),
        }
        let client = load_agent_execution_control_from_env(|name| {
            rejected.get(name).cloned().ok_or(VarError::NotPresent)
        })
        .unwrap()
        .unwrap();
        let before = private_calls.load(Ordering::SeqCst);
        let error = client
            .validate_recovery_context(&context)
            .await
            .unwrap_err();
        if fault == "untrusted-client" {
            // TLS 1.3 client authentication can fail after channel creation.
            // The public wrapper maps that later RPC refusal to custody denial.
            assert!(matches!(
                error.code(),
                tonic::Code::Unavailable | tonic::Code::FailedPrecondition
            ));
        } else {
            assert_eq!(
                error.code(),
                if fault == "wrong-bearer" {
                    tonic::Code::FailedPrecondition
                } else {
                    tonic::Code::Unavailable
                },
                "{fault}"
            );
        }
        let expected = before + if fault == "wrong-bearer" { 1 } else { 0 };
        assert_eq!(
            private_calls.load(Ordering::SeqCst),
            expected,
            "rejected transport reached the handler: {fault}"
        );
        assert_eq!(
            public_calls.load(Ordering::SeqCst),
            0,
            "private failure selected the public decoy: {fault}"
        );
        selected.validate_recovery_context(&context).await.unwrap();
        assert_eq!(private_calls.load(Ordering::SeqCst), expected + 1);
    }
    let plaintext = AgentExecutionControl::new(&format!("http://{address}"), TOKEN).unwrap();
    let before = private_calls.load(Ordering::SeqCst);
    let error = plaintext
        .validate_recovery_context(&context)
        .await
        .unwrap_err();
    assert!(matches!(
        error.code(),
        tonic::Code::Unavailable | tonic::Code::FailedPrecondition
    ));
    assert_eq!(private_calls.load(Ordering::SeqCst), before);
    assert_eq!(public_calls.load(Ordering::SeqCst), 0);
    selected.validate_recovery_context(&context).await.unwrap();
    assert_eq!(private_calls.load(Ordering::SeqCst), before + 1);
    stop.send(()).unwrap();
    stop_public.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(5), server)
        .await
        .unwrap()
        .unwrap();
    tokio::time::timeout(Duration::from_secs(5), public_server)
        .await
        .unwrap()
        .unwrap();
}

#[test]
fn explicit_private_control_transport_never_falls_back_after_configuration_failure() {
    for fault in [
        "missing-url",
        "missing-ca",
        "missing-cert",
        "missing-key",
        "missing-name",
        "cleartext",
        "public-address",
        "malformed-ca",
        "missing-ca-file",
        "empty-ca-file",
        "oversized-ca-file",
        "malformed-identity",
        "invalid-server-name",
        "url-userinfo",
        "url-query",
        "url-path",
        "zero-port",
    ] {
        let (_directory, mut values) = private_control_transport_fixture();
        match fault {
            "missing-url" => {
                values.remove("DEEPSEEK_GO_CONTROL_RPC_URL");
            }
            "missing-ca" => {
                values.remove("DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE");
            }
            "missing-cert" => {
                values.remove("DEEPSEEK_GO_CONTROL_RPC_TLS_CERT_FILE");
            }
            "missing-key" => {
                values.remove("DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE");
            }
            "missing-name" => {
                values.remove("DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME");
            }
            "cleartext" => {
                values.insert(
                    "DEEPSEEK_GO_CONTROL_RPC_URL".to_string(),
                    "http://127.0.0.1:8091".to_string(),
                );
            }
            "public-address" => {
                values.insert(
                    "DEEPSEEK_GO_CONTROL_RPC_URL".to_string(),
                    "https://198.51.100.9:8091".to_string(),
                );
            }
            "malformed-ca" => {
                std::fs::write(
                    values.get("DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE").unwrap(),
                    "not a certificate",
                )
                .unwrap();
            }
            "missing-ca-file" => {
                values.insert(
                    "DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE".into(),
                    "/missing-native-control-ca.pem".into(),
                );
            }
            "empty-ca-file" | "oversized-ca-file" => {
                let bytes = if fault == "empty-ca-file" {
                    Vec::new()
                } else {
                    vec![b'a'; 1024 * 1024 + 1]
                };
                std::fs::write(
                    values.get("DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE").unwrap(),
                    bytes,
                )
                .unwrap();
            }
            "malformed-identity" => {
                std::fs::write(
                    values.get("DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE").unwrap(),
                    "not a private key",
                )
                .unwrap();
            }
            "invalid-server-name" => {
                values.insert(
                    "DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME".into(),
                    "invalid name".into(),
                );
            }
            "url-userinfo" | "url-query" | "url-path" | "zero-port" => {
                let url = match fault {
                    "url-userinfo" => "https://caller@127.0.0.1:8091",
                    "url-query" => "https://127.0.0.1:8091?other=peer",
                    "url-path" => "https://127.0.0.1:8091/other",
                    _ => "https://127.0.0.1:0",
                };
                values.insert("DEEPSEEK_GO_CONTROL_RPC_URL".into(), url.into());
            }
            _ => unreachable!(),
        }
        let result = load_agent_execution_control_from_env(|name| {
            values.get(name).cloned().ok_or(VarError::NotPresent)
        });
        assert!(
            result.is_err(),
            "invalid private transport selected the legacy HTTP peer: {fault}"
        );
    }
}

#[derive(Clone)]
struct Preview {
    context: AgentRunRecoveryContext,
    mode: Arc<AtomicU8>,
    calls: Arc<AtomicUsize>,
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
        self.calls.fetch_add(1, Ordering::SeqCst);
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
        calls: Arc::new(AtomicUsize::new(0)),
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
