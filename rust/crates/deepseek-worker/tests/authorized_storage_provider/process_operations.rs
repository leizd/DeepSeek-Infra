//! Actual default worker binary, TLS authentication, real MinIO, and forced restart.
//! The test signer is isolated; this does not attest Go control-plane cutover.
use super::{canonical, endpoints, minio_configured, sign_grant, sign_request, store, test_signer};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_protocol::generated::deepseek::{
    action::v1::{
        AdmitStatus, InstallAuthoritativeEpochRequest, QueryStorageEffectRequest,
        StorageConditionType, StorageMutationRequest, StorageMutationStatus, StoragePrecondition,
        worker_client::WorkerClient,
    },
    common::v1::{ActionFence, EffectState},
};
use ed25519_dalek::{Signer as _, SigningKey};
use sha2::{Digest, Sha256};
use std::{
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::Duration,
};
use tonic::{
    Request,
    transport::{Certificate, Channel, ClientTlsConfig},
};

const SERVER_NAME: &str = "deepseek-storage-worker.test";

struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

struct Identity {
    cert: PathBuf,
    key: PathBuf,
    ca: String,
    bearer: String,
}

fn identity(root: &Path) -> Identity {
    let mut ca = rcgen::CertificateParams::default();
    ca.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    ca.key_usages = vec![
        rcgen::KeyUsagePurpose::KeyCertSign,
        rcgen::KeyUsagePurpose::CrlSign,
    ];
    let ca_key = rcgen::KeyPair::generate().unwrap();
    let ca_cert = ca.self_signed(&ca_key).unwrap();
    let mut leaf = rcgen::CertificateParams::new(vec![SERVER_NAME.into()]).unwrap();
    leaf.key_usages = vec![rcgen::KeyUsagePurpose::DigitalSignature];
    leaf.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ServerAuth];
    let key = rcgen::KeyPair::generate().unwrap();
    let certificate = leaf.signed_by(&key, &ca_cert, &ca_key).unwrap();
    let cert = root.join("worker.pem");
    let key_path = root.join("worker.key");
    std::fs::write(&cert, certificate.pem()).unwrap();
    std::fs::write(&key_path, key.serialize_pem()).unwrap();
    Identity {
        cert,
        key: key_path,
        ca: ca_cert.pem(),
        bearer: format!("{:x}", Sha256::digest(ca_key.serialize_pem().as_bytes())),
    }
}

fn timestamp(time: time::OffsetDateTime) -> String {
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        time.year(),
        u8::from(time.month()),
        time.day(),
        time.hour(),
        time.minute(),
        time.second()
    )
}

fn current_signed(key: &SigningKey, encoded: &[u8], domain: &[u8]) -> Vec<u8> {
    let mut value: serde_json::Value = serde_json::from_slice(encoded).unwrap();
    let object = value.as_object_mut().unwrap();
    object.remove("signature");
    object.remove("digest");
    let now = time::OffsetDateTime::now_utc();
    value["issuedAt"] = timestamp(now - time::Duration::seconds(1)).into();
    value["expiresAt"] = timestamp(now + time::Duration::minutes(4)).into();
    value["digest"] = format!("sha256:{:x}", Sha256::digest(canonical(&value))).into();
    let mut message = domain.to_vec();
    message.extend(canonical(&value));
    value["signature"] = URL_SAFE_NO_PAD.encode(key.sign(&message).to_bytes()).into();
    canonical(&value)
}

fn spawn(root: &Path, port: u16, endpoint: &str, identity: &Identity) -> Process {
    let (_, authority) = test_signer();
    let executable = std::env::var_os("DEEPSEEK_TEST_RUST_WORKER_BINARY")
        .unwrap_or_else(|| env!("CARGO_BIN_EXE_deepseek-worker").into());
    let mut child = Command::new(executable);
    child.env_clear();
    // Loader paths and offline coverage output only; no credentials or
    // authority clock are inherited. Keep profiles outside the source tree.
    for name in [
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "LLVM_PROFILE_FILE",
    ] {
        if let Some(value) = std::env::var_os(name) {
            child.env(name, value);
        }
    }
    child
        .env("DEEPSEEK_WORKER_LISTEN", format!("127.0.0.1:{port}"))
        .env("DEEPSEEK_WORKER_STATE_ROOT", root)
        .env(
            "DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY",
            authority.signer_public_key,
        )
        .env("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", authority.fleet_id)
        .env(
            "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT",
            authority.environment,
        )
        .env(
            "DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN",
            authority.fencing_token.to_string(),
        )
        .env("DEEPSEEK_WORKER_TLS_CERT_FILE", &identity.cert)
        .env("DEEPSEEK_WORKER_TLS_KEY_FILE", &identity.key)
        .env("DEEPSEEK_WORKER_SERVICE_BEARER", &identity.bearer)
        .env(
            "DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT",
            timestamp(time::OffsetDateTime::now_utc() + time::Duration::minutes(10)),
        )
        .env("DEEPSEEK_WORKER_SERVICE_NAME", "go-control-plane")
        .env("DEEPSEEK_WORKER_SERVICE_ROLE", "controller")
        .env("DEEPSEEK_WORKER_S3_ENDPOINT", endpoint)
        .env(
            "DEEPSEEK_WORKER_S3_BUCKET",
            std::env::var("DEEPSEEK_NATIVE_S3_BUCKET").unwrap(),
        )
        .env("DEEPSEEK_WORKER_S3_PREFIX", "worker-authorized-e2e")
        .env("DEEPSEEK_WORKER_S3_REGION", "us-east-1")
        .env(
            "DEEPSEEK_WORKER_S3_ACCESS_KEY",
            std::env::var("AWS_ACCESS_KEY_ID").unwrap(),
        )
        .env(
            "DEEPSEEK_WORKER_S3_SECRET_KEY",
            std::env::var("AWS_SECRET_ACCESS_KEY").unwrap(),
        )
        .env("DEEPSEEK_WORKER_S3_ALLOW_HTTP_LOOPBACK", "true")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        child.creation_flags(0x08000000);
    }
    Process(child.spawn().unwrap())
}

async fn connect(process: &mut Process, port: u16, identity: &Identity) -> WorkerClient<Channel> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = process.0.try_wait().unwrap() {
            use std::io::Read as _;
            let mut diagnostics = String::new();
            if let Some(stderr) = process.0.stderr.take() {
                stderr.take(2048).read_to_string(&mut diagnostics).unwrap();
            }
            panic!("worker exited before TLS readiness: {status}; {diagnostics}");
        }
        let tls = ClientTlsConfig::new()
            .domain_name(SERVER_NAME)
            .ca_certificate(Certificate::from_pem(&identity.ca));
        if let Ok(channel) = Channel::from_shared(format!("https://127.0.0.1:{port}"))
            .unwrap()
            .tls_config(tls)
            .unwrap()
            .connect_timeout(Duration::from_millis(250))
            .timeout(Duration::from_secs(5))
            .connect()
            .await
        {
            return WorkerClient::new(channel);
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "worker TLS readiness timed out"
        );
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

fn authenticated<T>(message: T, identity: &Identity) -> Request<T> {
    let mut request = Request::new(message);
    request.metadata_mut().insert(
        "authorization",
        format!("Bearer {}", identity.bearer).parse().unwrap(),
    );
    request
}

#[tokio::test]
async fn configured_binary_writes_real_objects_and_replays_after_forced_restart() {
    if !minio_configured() {
        return;
    }
    for (index, endpoint) in endpoints().iter().enumerate() {
        let root = tempfile::tempdir().unwrap();
        let identity = identity(root.path());
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let transport = store(endpoint);
        let (key, _) = test_signer();
        let fence = ActionFence {
            action_id: format!("process-storage-{index}"),
            execution_epoch: 21,
        };
        let mut target_identity = String::with_capacity(64);
        for byte in transport.target_identity() {
            use std::fmt::Write as _;
            write!(target_identity, "{byte:02x}").unwrap();
        }
        let payload = b"production worker entrypoint reaches a real provider";
        let mut mutation = StorageMutationRequest {
            fence: Some(fence.clone()),
            operation_id: format!(" process-operation-{index} "),
            mutation_type: "PUT_CHUNK".into(),
            provider: "s3".into(),
            target_identity,
            bucket: std::env::var("DEEPSEEK_NATIVE_S3_BUCKET").unwrap(),
            prefix: "worker-authorized-e2e".into(),
            object_key: format!("process-operation-{index}"),
            payload_digest: format!("{:x}", Sha256::digest(payload)),
            expected_length: payload.len() as u64,
            payload: payload.to_vec(),
            precondition: Some(StoragePrecondition {
                condition_type: StorageConditionType::CreateOnly as i32,
                expected_etag: String::new(),
            }),
            ..Default::default()
        };
        mutation.canonical_authorization = current_signed(
            &key,
            &sign_grant(&key, &mutation),
            b"deepseek-infra:control-storage-operation-grant-v1\0",
        );
        let epoch = current_signed(
            &key,
            &sign_request(&key, &fence.action_id, 21, 4, "c", "d"),
            b"deepseek-infra:control-authority-request-v1\0",
        );
        let mut process = spawn(root.path(), port, endpoint, &identity);
        let pid = process.0.id();
        let mut client = connect(&mut process, port, &identity).await;
        let unauthenticated = client
            .execute_storage_mutation(mutation.clone())
            .await
            .unwrap()
            .into_inner();
        assert_eq!(unauthenticated.status(), StorageMutationStatus::Rejected);
        assert!(
            transport
                .stat(&mutation.object_key)
                .await
                .unwrap()
                .is_none()
        );
        let installed = client
            .install_authoritative_epoch(authenticated(
                InstallAuthoritativeEpochRequest {
                    fence: Some(fence.clone()),
                    canonical_request: epoch,
                },
                &identity,
            ))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(installed.status(), AdmitStatus::Admitted);
        let mut unsigned = mutation.clone();
        unsigned.canonical_authorization.clear();
        let rejected = client
            .execute_storage_mutation(authenticated(unsigned, &identity))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(rejected.status(), StorageMutationStatus::Rejected);
        assert!(
            transport
                .stat(&mutation.object_key)
                .await
                .unwrap()
                .is_none()
        );
        let confirmed = client
            .execute_storage_mutation(authenticated(mutation.clone(), &identity))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(
            confirmed.status(),
            StorageMutationStatus::Confirmed,
            "{confirmed:?}"
        );
        assert_eq!(confirmed.state(), EffectState::Applied);
        assert_eq!(confirmed.fence.as_ref(), Some(&fence));
        assert_eq!(confirmed.operation_id, mutation.operation_id);
        assert!(!confirmed.etag.is_empty());
        let provider_metadata: serde_json::Value =
            serde_json::from_str(&confirmed.provider_metadata).unwrap();
        if std::env::var("DEEPSEEK_TEST_VERSIONED_PROVIDER_INDEX")
            .ok()
            .as_deref()
            == Some(index.to_string().as_str())
        {
            assert!(
                provider_metadata["version"]
                    .as_str()
                    .is_some_and(|v| !v.is_empty())
            );
        }
        transport
            .download_verified(
                &mutation.object_key,
                payload.len() as u64,
                Sha256::digest(payload).into(),
                &mut tokio::io::sink(),
            )
            .await
            .unwrap();
        process.0.kill().unwrap();
        assert!(!process.0.wait().unwrap().success());
        drop(client);
        let mut restarted = spawn(root.path(), port, endpoint, &identity);
        assert_ne!(pid, restarted.0.id());
        let mut client = connect(&mut restarted, port, &identity).await;
        let query = client
            .query_storage_effect(authenticated(
                QueryStorageEffectRequest {
                    fence: Some(fence),
                    operation_id: mutation.operation_id.clone(),
                },
                &identity,
            ))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(query, confirmed);
        let retry = client
            .execute_storage_mutation(authenticated(mutation.clone(), &identity))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(retry, confirmed);
        let head = transport.stat(&mutation.object_key).await.unwrap().unwrap();
        assert_eq!(head.etag, confirmed.etag);
        assert_eq!(
            head.version.as_deref(),
            provider_metadata["version"].as_str()
        );
        transport
            .download_verified(
                &mutation.object_key,
                payload.len() as u64,
                Sha256::digest(payload).into(),
                &mut tokio::io::sink(),
            )
            .await
            .unwrap();
        eprintln!(
            "real worker process/provider {index}: TLS auth, signed PUT, forced restart, unchanged ETag"
        );
    }
}
