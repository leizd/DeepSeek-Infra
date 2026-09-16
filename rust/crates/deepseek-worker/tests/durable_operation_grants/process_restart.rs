//! TLS + actual Rust child death. No S3 transport or provider-effect claim.
use super::{Fixture, canonical, fixture, signed};
use deepseek_protocol::generated::deepseek::{
    action::v1::{
        AdmitStatus, InstallAuthoritativeEpochRequest, StorageConditionType,
        StorageMutationRequest, StorageMutationStatus, StoragePrecondition,
        worker_client::WorkerClient,
    },
    common::v1::{ActionFence, EffectState},
};
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

const SERVER_NAME: &str = "deepseek-grant-worker.test";

struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

struct TlsMaterial {
    cert: PathBuf,
    key: PathBuf,
    ca: String,
    bearer: String,
}

fn tls_material(root: &Path) -> TlsMaterial {
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
    let cert = root.join("grant-worker.pem");
    let key_path = root.join("grant-worker.key");
    std::fs::write(&cert, certificate.pem()).unwrap();
    std::fs::write(&key_path, key.serialize_pem()).unwrap();
    use sha2::{Digest as _, Sha256};
    // Ephemeral qualification credential; never loaded from the user's env.
    let bearer = format!("{:x}", Sha256::digest(ca_key.serialize_pem().as_bytes()));
    TlsMaterial {
        cert,
        key: key_path,
        ca: ca_cert.pem(),
        bearer,
    }
}

fn spawn(root: &Path, port: u16, tls: &TlsMaterial, f: &Fixture) -> Process {
    use deepseek_worker::{
        WORKER_SERVICE_BEARER, WORKER_SERVICE_BEARER_EXPIRES_AT, WORKER_SERVICE_NAME,
        WORKER_SERVICE_ROLE, WORKER_TLS_CERT_FILE, WORKER_TLS_KEY_FILE,
    };
    let expiry = time::OffsetDateTime::now_utc() + time::Duration::minutes(10);
    let expiry = format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        expiry.year(),
        u8::from(expiry.month()),
        expiry.day(),
        expiry.hour(),
        expiry.minute(),
        expiry.second()
    );
    let mut process = Command::new(env!("CARGO_BIN_EXE_deepseek-worker"));
    process
        .env("DEEPSEEK_WORKER_LISTEN", format!("127.0.0.1:{port}"))
        .env("DEEPSEEK_WORKER_STATE_ROOT", root)
        .env(
            "DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY",
            &f.config.signer_public_key,
        )
        .env("DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", &f.config.fleet_id)
        .env(
            "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT",
            &f.config.environment,
        )
        .env(
            "DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN",
            f.config.fencing_token.to_string(),
        )
        .env(
            "DEEPSEEK_WORKER_AUTHORITY_NOW",
            f.config.now.as_ref().unwrap(),
        )
        .env(WORKER_TLS_CERT_FILE, &tls.cert)
        .env(WORKER_TLS_KEY_FILE, &tls.key)
        .env(WORKER_SERVICE_BEARER, &tls.bearer)
        .env(WORKER_SERVICE_BEARER_EXPIRES_AT, expiry)
        .env(WORKER_SERVICE_NAME, "go-control-plane")
        .env(WORKER_SERVICE_ROLE, "controller")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        process.creation_flags(0x08000000);
    }
    Process(process.spawn().unwrap())
}

async fn connect(process: &mut Process, port: u16, tls: &TlsMaterial) -> WorkerClient<Channel> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    loop {
        assert!(
            process.0.try_wait().unwrap().is_none(),
            "worker exited before TLS readiness"
        );
        let config = ClientTlsConfig::new()
            .domain_name(SERVER_NAME)
            .ca_certificate(Certificate::from_pem(&tls.ca));
        let channel = Channel::from_shared(format!("https://127.0.0.1:{port}"))
            .unwrap()
            .tls_config(config)
            .unwrap()
            .connect_timeout(Duration::from_millis(250))
            .timeout(Duration::from_secs(3))
            .connect()
            .await;
        if let Ok(channel) = channel {
            return WorkerClient::new(channel);
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "worker TLS readiness timed out"
        );
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

fn authenticated<T>(value: T, tls: &TlsMaterial) -> Request<T> {
    let mut request = Request::new(value);
    request.metadata_mut().insert(
        "authorization",
        format!("Bearer {}", tls.bearer).parse().unwrap(),
    );
    request
}

fn mutation(document: &serde_json::Value) -> StorageMutationRequest {
    let c = super::command(document);
    StorageMutationRequest {
        fence: Some(ActionFence {
            action_id: c.action_id.into(),
            execution_epoch: c.execution_epoch,
        }),
        operation_id: c.operation_id.into(),
        mutation_type: c.mutation_type.into(),
        provider: c.provider.into(),
        target_identity: c.target_identity.into(),
        bucket: c.bucket.into(),
        prefix: c.prefix.into(),
        object_key: c.object_key.into(),
        payload_digest: c.object_digest.into(),
        expected_length: c.expected_length,
        payload: vec![1, 2, 3],
        precondition: Some(StoragePrecondition {
            condition_type: StorageConditionType::CreateOnly as i32,
            expected_etag: String::new(),
        }),
        canonical_authorization: signed(document, true),
        ..Default::default()
    }
}

#[tokio::test]
async fn tls_admitted_grant_survives_forced_worker_termination() {
    let root = tempfile::tempdir().unwrap();
    let f = fixture();
    let tls = tls_material(root.path());
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let mut process = spawn(root.path(), port, &tls, &f);
    let pid = process.0.id();
    let mut client = connect(&mut process, port, &tls).await;
    let installed = client
        .install_authoritative_epoch(authenticated(
            InstallAuthoritativeEpochRequest {
                fence: Some(super::fence()),
                canonical_request: signed(&f.epoch, false),
            },
            &tls,
        ))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(installed.status(), AdmitStatus::Admitted);
    let admitted = client
        .execute_storage_mutation(authenticated(mutation(&f.grant), &tls))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        admitted.status(),
        if cfg!(feature = "s3") {
            StorageMutationStatus::Failed
        } else {
            StorageMutationStatus::Rejected
        }
    );
    assert_eq!(admitted.state(), EffectState::Unknown);
    let expected = if cfg!(feature = "s3") {
        "STORAGE_TRANSPORT_UNAVAILABLE"
    } else {
        "STORAGE_FEATURE_DISABLED"
    };
    assert_eq!(admitted.error.as_ref().unwrap().code, expected);
    // Observe the worker's own committed row, never seed or fabricate a journal.
    let database = rusqlite::Connection::open_with_flags(
        root.path().join("rust-worker/authority.sqlite3"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let stored: Vec<u8> = database
        .query_row("SELECT request FROM storage_operation_grants", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(stored, canonical(&f.grant));
    drop(database);
    process.0.kill().unwrap();
    assert!(!process.0.wait().unwrap().success());
    drop(client);
    let mut restarted = spawn(root.path(), port, &tls, &f);
    assert_ne!(pid, restarted.0.id());
    let mut client = connect(&mut restarted, port, &tls).await;
    let retry = client
        .execute_storage_mutation(authenticated(mutation(&f.grant), &tls))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(retry, admitted);
    for nonce_reuse in [false, true] {
        let mut replacement = f.grant.clone();
        replacement["payload"]["objectKey"] = "objects/substituted".into();
        if nonce_reuse {
            replacement["requestId"] = "3".repeat(64).into();
        }
        let refused = client
            .execute_storage_mutation(authenticated(mutation(&replacement), &tls))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(refused.status(), StorageMutationStatus::Rejected);
        assert_eq!(refused.state(), EffectState::Unknown);
        assert!(refused.effect_id.is_empty());
        assert_eq!(
            refused.error.unwrap().code,
            if nonce_reuse {
                "STORAGE_OPERATION_GRANT_NONCE_REUSE"
            } else {
                "STORAGE_OPERATION_GRANT_REPLAY"
            }
        );
    }
}
