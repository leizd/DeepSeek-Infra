//! Real `deepseek-worker` binary with server-authenticated TLS and a service bearer.
use deepseek_protocol::generated::deepseek::{
    action::v1::{StorageMutationRequest, StorageMutationStatus, worker_client::WorkerClient},
    common::v1::ActionFence,
};
use deepseek_worker::{
    WORKER_SERVICE_BEARER, WORKER_SERVICE_BEARER_EXPIRES_AT, WORKER_SERVICE_NAME,
    WORKER_SERVICE_ROLE, WORKER_TLS_CERT_FILE, WORKER_TLS_KEY_FILE,
};
use std::{
    fs::File,
    io::Read,
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::Duration,
};
use tonic::{
    Request,
    metadata::MetadataValue,
    transport::{Certificate, Channel, ClientTlsConfig},
};

const SERVER_NAME: &str = "deepseek-worker.test";
const SECRET: &str = "tls-bearer-secret-value-do-not-log";

fn utc_z_after_minutes(minutes: i64) -> String {
    let when = time::OffsetDateTime::now_utc() + time::Duration::minutes(minutes);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        when.year(),
        u8::from(when.month()),
        when.day(),
        when.hour(),
        when.minute(),
        when.second()
    )
}

struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

struct TlsMaterial {
    cert_path: PathBuf,
    key_path: PathBuf,
    ca_pem: Vec<u8>,
}

fn write_tls_material(dir: &Path, server_name: &str) -> TlsMaterial {
    let mut ca_params = rcgen::CertificateParams::default();
    ca_params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    ca_params.key_usages = vec![
        rcgen::KeyUsagePurpose::KeyCertSign,
        rcgen::KeyUsagePurpose::CrlSign,
    ];
    let ca_key = rcgen::KeyPair::generate().unwrap();
    let ca_cert = ca_params.self_signed(&ca_key).unwrap();

    let mut leaf_params = rcgen::CertificateParams::new(vec![server_name.to_string()]).unwrap();
    leaf_params.key_usages = vec![rcgen::KeyUsagePurpose::DigitalSignature];
    leaf_params.extended_key_usages = vec![rcgen::ExtendedKeyUsagePurpose::ServerAuth];
    let leaf_key = rcgen::KeyPair::generate().unwrap();
    let leaf = leaf_params.signed_by(&leaf_key, &ca_cert, &ca_key).unwrap();

    let cert_path = dir.join("server.pem");
    let key_path = dir.join("server.key");
    std::fs::write(&cert_path, leaf.pem()).unwrap();
    std::fs::write(&key_path, leaf_key.serialize_pem()).unwrap();
    TlsMaterial {
        cert_path,
        key_path,
        ca_pem: ca_cert.pem().into_bytes(),
    }
}

fn reserve_port() -> u16 {
    let reservation = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = reservation.local_addr().unwrap().port();
    drop(reservation);
    port
}

fn base_command(stdout: &Path, stderr: &Path) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_deepseek-worker"));
    for name in [
        WORKER_TLS_CERT_FILE,
        WORKER_TLS_KEY_FILE,
        WORKER_SERVICE_BEARER,
        WORKER_SERVICE_BEARER_EXPIRES_AT,
        WORKER_SERVICE_NAME,
        WORKER_SERVICE_ROLE,
        "DEEPSEEK_WORKER_LISTEN",
        "DEEPSEEK_WORKER_STATE_ROOT",
        "DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY",
        "DEEPSEEK_WORKER_AUTHORITY_FLEET_ID",
        "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT",
        "DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN",
        "DEEPSEEK_WORKER_AUTHORITY_NOW",
    ] {
        command.env_remove(name);
    }
    command
        .stdin(Stdio::null())
        .stdout(File::create(stdout).unwrap())
        .stderr(File::create(stderr).unwrap());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command
}

fn launch_tls(dir: &Path, port: u16, material: &TlsMaterial) -> Process {
    let stdout = dir.join("worker.out");
    let stderr = dir.join("worker.err");
    let mut command = base_command(&stdout, &stderr);
    command
        .env("DEEPSEEK_WORKER_LISTEN", format!("127.0.0.1:{port}"))
        .env(WORKER_TLS_CERT_FILE, &material.cert_path)
        .env(WORKER_TLS_KEY_FILE, &material.key_path)
        .env(WORKER_SERVICE_BEARER, SECRET)
        .env(WORKER_SERVICE_BEARER_EXPIRES_AT, utc_z_after_minutes(10))
        .env(WORKER_SERVICE_NAME, "go-control-plane")
        .env(WORKER_SERVICE_ROLE, "controller");
    Process(command.spawn().unwrap())
}

fn read_logs(dir: &Path) -> String {
    let mut out = String::new();
    let _ = File::open(dir.join("worker.out")).and_then(|mut file| file.read_to_string(&mut out));
    let mut err = String::new();
    let _ = File::open(dir.join("worker.err")).and_then(|mut file| file.read_to_string(&mut err));
    out.push_str(&err);
    out
}

fn assert_logs_hide_secret(dir: &Path) {
    let logs = read_logs(dir);
    assert!(
        !logs.contains(SECRET),
        "worker logs leaked the service credential"
    );
}

async fn connect_tls(
    process: &mut Process,
    port: u16,
    ca_pem: &[u8],
    server_name: &str,
) -> WorkerClient<Channel> {
    for _ in 0..100 {
        if let Some(status) = process.0.try_wait().unwrap() {
            panic!("worker exited before listening: {status}");
        }
        let origin = format!("https://127.0.0.1:{port}");
        let tls = ClientTlsConfig::new()
            .domain_name(server_name)
            .ca_certificate(Certificate::from_pem(ca_pem));
        if let Ok(channel) = Channel::from_shared(origin)
            .unwrap()
            .tls_config(tls)
            .unwrap()
            .connect()
            .await
        {
            return WorkerClient::new(channel);
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("worker did not listen within 5 seconds");
}

fn mutation(token: Option<&str>) -> Request<StorageMutationRequest> {
    let mut request = Request::new(StorageMutationRequest {
        fence: Some(ActionFence {
            action_id: "tls-act-1".into(),
            execution_epoch: 1,
        }),
        operation_id: "tls-op-1".into(),
        ..Default::default()
    });
    if let Some(token) = token {
        request
            .metadata_mut()
            .insert("authorization", format!("Bearer {token}").parse().unwrap());
    }
    request
}

fn assert_auth_passed(code: &str) {
    assert!(
        code == "STORAGE_FEATURE_DISABLED"
            || code == "WORKER_WITHOUT_AUTHORITY"
            || code == "OPERATION_INVALID"
            || code == "STORAGE_OPERATION_GRANT_MISSING"
            || code == "STORAGE_OPERATION_GRANT_AUTHORITY_MISSING",
        "authenticated call should fail after auth, got {code}"
    );
    assert_ne!(code, "SERVICE_AUTHENTICATION_UNAVAILABLE");
    assert_ne!(code, "AUTHENTICATION_MISSING");
    assert_ne!(code, "AUTHENTICATION_INVALID");
}

#[tokio::test]
async fn spawned_tls_worker_accepts_complete_bearer_and_rejects_bad_credentials() {
    let dir = tempfile::tempdir().unwrap();
    let material = write_tls_material(dir.path(), SERVER_NAME);
    let port = reserve_port();
    let mut process = launch_tls(dir.path(), port, &material);
    let mut client = connect_tls(&mut process, port, &material.ca_pem, SERVER_NAME).await;

    let ok = client
        .execute_storage_mutation(mutation(Some(SECRET)))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(ok.status(), StorageMutationStatus::Rejected);
    assert_auth_passed(&ok.error.unwrap().code);

    let missing = client
        .execute_storage_mutation(mutation(None))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(missing.error.unwrap().code, "AUTHENTICATION_MISSING");

    let wrong = client
        .execute_storage_mutation(mutation(Some("wrong-token")))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(wrong.error.as_ref().unwrap().code, "AUTHENTICATION_INVALID");
    assert!(!format!("{wrong:?}").contains(SECRET));

    let mut duplicate = mutation(None);
    duplicate.metadata_mut().append(
        "authorization",
        MetadataValue::from_static("Bearer tls-bearer-secret-value-do-not-log"),
    );
    duplicate.metadata_mut().append(
        "authorization",
        MetadataValue::from_static("Bearer tls-bearer-secret-value-do-not-log"),
    );
    let duplicated = client
        .execute_storage_mutation(duplicate)
        .await
        .unwrap()
        .into_inner();
    assert_eq!(duplicated.error.unwrap().code, "AUTHENTICATION_INVALID");
    assert_logs_hide_secret(dir.path());
}

#[tokio::test]
async fn spawned_tls_worker_rejects_wrong_server_name() {
    let dir = tempfile::tempdir().unwrap();
    let material = write_tls_material(dir.path(), SERVER_NAME);
    let port = reserve_port();
    let mut process = launch_tls(dir.path(), port, &material);
    let origin = format!("https://127.0.0.1:{port}");
    let mut last_err = None;
    for _ in 0..100 {
        if let Some(status) = process.0.try_wait().unwrap() {
            panic!("worker exited before listening: {status}");
        }
        let tls = ClientTlsConfig::new()
            .domain_name("wrong.example")
            .ca_certificate(Certificate::from_pem(&material.ca_pem));
        match Channel::from_shared(origin.clone())
            .unwrap()
            .tls_config(tls)
            .unwrap()
            .connect()
            .await
        {
            Ok(_) => panic!("wrong server identity must fail TLS"),
            Err(error) => {
                let text = format!("{error:?}");
                assert!(!text.contains(SECRET));
                last_err = Some(error);
                if TcpListener::bind(format!("127.0.0.1:{port}")).is_err() {
                    break;
                }
            }
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(last_err.is_some());
    assert_logs_hide_secret(dir.path());
}

#[test]
fn spawned_worker_fails_closed_on_partial_tls_env() {
    let dir = tempfile::tempdir().unwrap();
    let material = write_tls_material(dir.path(), SERVER_NAME);
    let port = reserve_port();
    let stdout = dir.path().join("worker.out");
    let stderr = dir.path().join("worker.err");
    let mut command = base_command(&stdout, &stderr);
    command
        .env("DEEPSEEK_WORKER_LISTEN", format!("127.0.0.1:{port}"))
        .env(WORKER_TLS_CERT_FILE, &material.cert_path)
        .env(WORKER_SERVICE_BEARER, SECRET);
    let mut child = command.spawn().unwrap();
    let status = child.wait().unwrap();
    assert!(!status.success());
    assert_logs_hide_secret(dir.path());
}

#[test]
fn spawned_worker_fails_closed_on_expired_credential() {
    let dir = tempfile::tempdir().unwrap();
    let material = write_tls_material(dir.path(), SERVER_NAME);
    let port = reserve_port();
    let stdout = dir.path().join("worker.out");
    let stderr = dir.path().join("worker.err");
    let mut command = base_command(&stdout, &stderr);
    command
        .env("DEEPSEEK_WORKER_LISTEN", format!("127.0.0.1:{port}"))
        .env(WORKER_TLS_CERT_FILE, &material.cert_path)
        .env(WORKER_TLS_KEY_FILE, &material.key_path)
        .env(WORKER_SERVICE_BEARER, SECRET)
        .env(WORKER_SERVICE_BEARER_EXPIRES_AT, "2000-01-01T00:00:00Z")
        .env(WORKER_SERVICE_NAME, "go-control-plane")
        .env(WORKER_SERVICE_ROLE, "controller");
    let mut child = command.spawn().unwrap();
    let status = child.wait().unwrap();
    assert!(!status.success());
    assert_logs_hide_secret(dir.path());
}

#[tokio::test]
async fn spawned_plaintext_worker_stays_fail_closed_without_tls() {
    let dir = tempfile::tempdir().unwrap();
    let port = reserve_port();
    let stdout = dir.path().join("worker.out");
    let stderr = dir.path().join("worker.err");
    let mut command = base_command(&stdout, &stderr);
    command.env("DEEPSEEK_WORKER_LISTEN", format!("127.0.0.1:{port}"));
    let mut process = Process(command.spawn().unwrap());
    let mut client = None;
    for _ in 0..100 {
        if process.0.try_wait().unwrap().is_some() {
            panic!("plaintext worker exited before listening");
        }
        if let Ok(connected) = WorkerClient::connect(format!("http://127.0.0.1:{port}")).await {
            client = Some(connected);
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let mut client = client.expect("plaintext worker did not listen");
    let response = client
        .execute_storage_mutation(mutation(None))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        response.error.unwrap().code,
        "SERVICE_AUTHENTICATION_UNAVAILABLE"
    );
}
