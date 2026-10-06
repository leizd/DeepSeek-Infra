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
