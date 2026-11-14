use super::*;
use deepseek_protocol::ActionFence;
use std::sync::Arc;

#[test]
fn target_location_observes_files_directories_and_missing_without_touching_target_bytes() {
    let (root, policy, target) = directories();
    let marker_path = target.join(MARKER_NAME);
    fs::write(&marker_path, marker()).unwrap();
    let data = root.path().join("registered-file");
    fs::write(&data, b"retained-bytes").unwrap();
    for path in [&target, &data, &root.path().join("forbidden")] {
        assert_eq!(
            probe_location(path.to_str().unwrap()).unwrap(),
            BackupTargetLocationState::Exists
        );
    }
    for path in [root.path().join("missing"), data.join("not-a-directory")] {
        assert_eq!(
            probe_location(path.to_str().unwrap()).unwrap(),
            BackupTargetLocationState::Missing
        );
    }
    assert!(
        policy
            .inspect(root.path().join("forbidden").to_str().unwrap())
            .is_err()
    );
    assert_eq!(fs::read(marker_path).unwrap(), marker());
    assert_eq!(fs::read(data).unwrap(), b"retained-bytes");
    assert!(!root.path().join("missing").exists());
}

#[cfg(unix)]
#[test]
fn target_location_matches_retained_exists_for_live_broken_and_looping_links() {
    use std::os::unix::fs::symlink;
    let (root, _, target) = directories();
    let linked = root.path().join("linked");
    let broken = root.path().join("broken");
    let looping = root.path().join("looping");
    symlink(&target, &linked).unwrap();
    symlink(root.path().join("missing"), &broken).unwrap();
    symlink(&looping, &looping).unwrap();
    assert_eq!(
        probe_location(linked.to_str().unwrap()).unwrap(),
        BackupTargetLocationState::Exists
    );
    for path in [&broken, &looping] {
        assert_eq!(
            probe_location(path.to_str().unwrap()).unwrap(),
            BackupTargetLocationState::Missing
        );
        assert!(fs::symlink_metadata(path).unwrap().file_type().is_symlink());
    }
}

#[test]
fn target_location_never_converts_permission_or_io_failure_into_missing() {
    for kind in [
        std::io::ErrorKind::PermissionDenied,
        std::io::ErrorKind::Other,
    ] {
        let result = location_state(Err(std::io::Error::new(
            kind,
            "isolated unresolved metadata",
        )));
        assert_eq!(result.unwrap_err().code(), tonic::Code::Unavailable);
    }
    assert_eq!(
        probe_location("relative-path").unwrap_err().code(),
        tonic::Code::InvalidArgument
    );
    let path = std::env::temp_dir().join("invalid\0path");
    assert_eq!(
        probe_location(path.to_str().unwrap()).unwrap_err().code(),
        tonic::Code::InvalidArgument
    );
}

fn location_request(path: &Path) -> ProbeFilesystemBackupTargetLocationRequest {
    ProbeFilesystemBackupTargetLocationRequest {
        schema_version: 1,
        fence: Some(ActionFence {
            action_id: "target-inspect-location".into(),
            execution_epoch: 3,
        }),
        operation_id: "target-inspect-location".into(),
        path: path.to_str().unwrap().into(),
        writer_fencing_token: 7,
        resolve_path: false,
    }
}

fn authenticated_location_request(
    path: &Path,
) -> Request<ProbeFilesystemBackupTargetLocationRequest> {
    let mut request = Request::new(location_request(path));
    request.metadata_mut().insert(
        "authorization",
        "Bearer target-inspection-fixture-bearer".parse().unwrap(),
    );
    request
}

#[tokio::test]
async fn target_location_actual_grpc_preserves_signed_missing_existing_and_worker_epoch() {
    use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
    use deepseek_protocol::generated::deepseek::action::v1::{
        backup_target_inspection_client::BackupTargetInspectionClient,
        backup_target_inspection_server::BackupTargetInspectionServer,
    };
    use ed25519_dalek::{Signature, Verifier, VerifyingKey};
    use prost::Message as _;
    let (root, policy, target) = directories();
    fs::write(target.join(MARKER_NAME), marker()).unwrap();
    let file = root.path().join("registered-file");
    fs::write(&file, b"retained-file").unwrap();
    let service = signed_service(root.path(), policy);
    let worker = service.worker.clone();
    let expected_public = worker
        .lock()
        .await
        .agent_node_authority()
        .unwrap()
        .signer_public_key;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let incoming = futures_util::stream::unfold(listener, |listener| async move {
        Some((listener.accept().await.map(|(socket, _)| socket), listener))
    });
    let (stop, stopped) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(BackupTargetInspectionServer::new(service))
            .serve_with_incoming_shutdown(incoming, async {
                let _ = stopped.await;
            })
            .await
            .unwrap();
    });
    let mut client = BackupTargetInspectionClient::connect(format!("http://{address}"))
        .await
        .unwrap();
    assert_eq!(
        client
            .probe_location(location_request(&target))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::Unauthenticated
    );
    for (path, expected) in [
        (target.clone(), BackupTargetLocationState::Exists),
        (file.clone(), BackupTargetLocationState::Exists),
        (
            root.path().join("missing"),
            BackupTargetLocationState::Missing,
        ),
    ] {
        let observed_after = crate::service::unix_now_seconds().unwrap();
        let receipt = client
            .probe_location(authenticated_location_request(&path))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(receipt.schema_version, 1);
        assert_eq!(receipt.signer_public_key, expected_public);
        let observation = receipt.observation.unwrap();
        assert_eq!(observation.state, expected as i32);
        assert_eq!(observation.requested_path, path.to_str().unwrap());
        assert_eq!(observation.fence, location_request(&path).fence);
        assert_eq!(observation.operation_id, "target-inspect-location");
        assert_eq!(observation.writer_fencing_token, 7);
        assert_eq!(
            (&*observation.fleet_id, &*observation.environment),
            ("fleet-a", "test")
        );
        assert!(observation.observed_at_unix >= observed_after);
        assert!(observation.observed_at_unix <= crate::service::unix_now_seconds().unwrap());
        let public: [u8; 32] = URL_SAFE_NO_PAD
            .decode(&receipt.signer_public_key)
            .unwrap()
            .try_into()
            .unwrap();
        let key = VerifyingKey::from_bytes(&public).unwrap();
        let signature = Signature::from_slice(&receipt.signature).unwrap();
        let mut bytes = wire::LOCATION_SIGNATURE_DOMAIN.to_vec();
        bytes.extend(observation.encode_to_vec());
        key.verify(&bytes, &signature).unwrap();
        let mut wrong_purpose = wire::SIGNATURE_DOMAIN.to_vec();
        wrong_purpose.extend(observation.encode_to_vec());
        assert!(key.verify(&wrong_purpose, &signature).is_err());
        let mut changed = observation.clone();
        changed.requested_path.push_str("-different-location");
        let mut changed_message = wire::LOCATION_SIGNATURE_DOMAIN.to_vec();
        changed_message.extend(changed.encode_to_vec());
        assert!(key.verify(&changed_message, &signature).is_err());
    }
    let state = worker.lock().await;
    assert!(state.live_epochs.is_empty());
    assert!(state.effects.is_empty());
    drop(state);
    assert_eq!(fs::read(target.join(MARKER_NAME)).unwrap(), marker());
    assert_eq!(fs::read(file).unwrap(), b"retained-file");
    assert!(!root.path().join("missing").exists());
    stop.send(()).unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn target_location_rejects_stale_fences_wrong_roles_and_shared_reader_exhaustion() {
    let (root, policy, target) = directories();
    let mut service = signed_service(root.path(), policy);
    let mut stale = authenticated_location_request(&target);
    stale.get_mut().writer_fencing_token = 6;
    assert_eq!(
        service.probe_location(stale).await.unwrap_err().code(),
        tonic::Code::FailedPrecondition
    );
    let mut malformed = authenticated_location_request(&target);
    malformed.get_mut().fence.as_mut().unwrap().execution_epoch = 0;
    assert_eq!(
        service.probe_location(malformed).await.unwrap_err().code(),
        tonic::Code::InvalidArgument
    );
    let permit = service
        .target_inspection
        .as_ref()
        .unwrap()
        .readers
        .clone()
        .try_acquire_owned()
        .unwrap();
    assert_eq!(
        service
            .probe_location(authenticated_location_request(&target))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::ResourceExhausted
    );
    drop(permit);
    service.control_signer = None;
    assert_eq!(
        service
            .probe_location(authenticated_location_request(&target))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::Unavailable
    );
    for (name, role) in [
        ("other-service", "controller"),
        ("go-control-plane", "observer"),
    ] {
        let service = WorkerRpcService::new_with_authenticator(
            crate::Worker::new(),
            Arc::new(crate::service::StaticTokenAuthenticator::new(
                "target-inspection-fixture-bearer",
                crate::service::CallerIdentity {
                    service_name: name.into(),
                    role: role.into(),
                },
            )),
        );
        assert_eq!(
            service
                .probe_location(authenticated_location_request(&target))
                .await
                .unwrap_err()
                .code(),
            tonic::Code::PermissionDenied
        );
    }
}

fn directories() -> (tempfile::TempDir, LocationPolicy, PathBuf) {
    let root = tempfile::tempdir().unwrap();
    let forbidden = root.path().join("forbidden");
    fs::create_dir(&forbidden).unwrap();
    let target = root.path().join("target");
    fs::create_dir(&target).unwrap();
    let policy = LocationPolicy::new(&[forbidden]).unwrap();
    (root, policy, target)
}

fn marker() -> Vec<u8> {
    br#"{"schemaVersion":3,"targetId":"target_0123456789ab","targetNonce":"unchanged","incarnationId":"inc_original","ownerInstallationId":"inst_original","targetGeneration":9007199254740993,"latestCommitHash":"retained","createdAt":"2026-11-11T00:00:00Z"}"#.to_vec()
}

#[test]
fn target_inspection_reads_existing_identity_without_changing_any_bytes() {
    let (_root, policy, target) = directories();
    let bytes = marker();
    fs::write(target.join(MARKER_NAME), &bytes).unwrap();
    fs::write(target.join("existing-backup.bin"), b"retained ciphertext").unwrap();
    let observed = policy.inspect(target.to_str().unwrap()).unwrap();
    assert_eq!(observed.marker.as_deref(), Some(bytes.as_slice()));
    assert_eq!(observed.path, fs::canonicalize(&target).unwrap());
    assert_eq!(fs::read(target.join(MARKER_NAME)).unwrap(), bytes);
    assert_eq!(
        fs::read(target.join("existing-backup.bin")).unwrap(),
        b"retained ciphertext"
    );
    assert_eq!(fs::read_dir(&target).unwrap().count(), 2);
}

#[test]
fn target_inspection_observes_uninitialized_directory_without_initializing_it() {
    let (_root, policy, target) = directories();
    assert!(
        policy
            .inspect(target.to_str().unwrap())
            .unwrap()
            .marker
            .is_none()
    );
    assert_eq!(fs::read_dir(target).unwrap().count(), 0);
}

#[test]
fn target_inspection_rejects_relative_missing_file_and_forbidden_roots() {
    let (root, policy, target) = directories();
    assert_eq!(
        policy.inspect("relative").unwrap_err().message(),
        "Backup target path must be absolute"
    );
    let missing = root.path().join("missing");
    assert_eq!(
        policy
            .inspect(missing.to_str().unwrap())
            .unwrap_err()
            .message(),
        "Backup target directory does not exist"
    );
    assert!(!missing.exists());
    let file = root.path().join("file");
    fs::write(&file, b"not a directory").unwrap();
    assert!(policy.inspect(file.to_str().unwrap()).is_err());
    let forbidden = root.path().join("forbidden");
    let child = forbidden.join("child");
    fs::create_dir(&child).unwrap();
    for p in [&forbidden, &child] {
        assert!(
            policy
                .inspect(p.to_str().unwrap())
                .unwrap_err()
                .message()
                .starts_with("Unsafe backup target:")
        );
    }
    assert!(LocationPolicy::new(&[]).is_err());
    assert!(LocationPolicy::new(&[PathBuf::from("relative")]).is_err());
    // A component boundary matters: 'forbidden-other' is not under 'forbidden'.
    let sibling = root.path().join("forbidden-other");
    fs::create_dir(&sibling).unwrap();
    assert!(policy.inspect(sibling.to_str().unwrap()).is_ok());
    assert!(policy.inspect(target.to_str().unwrap()).is_ok());
}

#[test]
fn target_inspection_rejects_invalid_and_oversized_markers_without_rewriting_them() {
    let (_root, policy, target) = directories();
    let marker_path = target.join(MARKER_NAME);
    for bytes in [
        b"{".to_vec(),
        b"[]".to_vec(),
        b"{}".to_vec(),
        br#"{"targetId":""}"#.to_vec(),
        vec![b' '; MAX_MARKER_BYTES + 1],
    ] {
        fs::write(&marker_path, &bytes).unwrap();
        assert!(policy.inspect(target.to_str().unwrap()).is_err());
        assert_eq!(fs::read(&marker_path).unwrap(), bytes);
    }
    fs::remove_file(&marker_path).unwrap();
    fs::create_dir(&marker_path).unwrap();
    assert!(policy.inspect(target.to_str().unwrap()).is_err());
    assert!(marker_path.is_dir());
}

#[cfg(unix)]
#[test]
fn target_inspection_rejects_symlinked_directories_and_marker_files() {
    use std::os::unix::fs::symlink;
    let (root, policy, target) = directories();
    let alias = root.path().join("alias");
    symlink(&target, &alias).unwrap();
    assert!(policy.inspect(alias.to_str().unwrap()).is_err());
    let outside = root.path().join("outside-marker");
    fs::write(&outside, marker()).unwrap();
    symlink(&outside, target.join(MARKER_NAME)).unwrap();
    assert!(policy.inspect(target.to_str().unwrap()).is_err());
    assert_eq!(fs::read(outside).unwrap(), marker());
}

fn signed_service(root: &Path, policy: LocationPolicy) -> WorkerRpcService {
    use crate::control_signer::{BUNDLE_FILE, PASSPHRASE_FILE, load_control_signer_from_env};
    use crate::service::{CallerIdentity, StaticTokenAuthenticator};
    let password = b"isolated-target-inspection-fixture-password";
    let bundle =
        deepseek_federation::create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    let config = crate::WorkerAuthorityConfig {
        signer_public_key: bundle["binding"]["signerPublicKey"]
            .as_str()
            .unwrap()
            .into(),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
        fencing_token: 7,
        now: None,
    };
    let bundle_path = root.join("bundle.json");
    let password_path = root.join("password");
    fs::write(&bundle_path, serde_json::to_vec(&bundle).unwrap()).unwrap();
    fs::write(&password_path, password).unwrap();
    let state = root.join("state");
    fs::create_dir(&state).unwrap();
    let worker = crate::Worker::open_with_authority(config.clone(), &state).unwrap();
    let signer = load_control_signer_from_env(
        |name| match name {
            BUNDLE_FILE => Ok(bundle_path.to_str().unwrap().into()),
            PASSPHRASE_FILE => Ok(password_path.to_str().unwrap().into()),
            _ => Err(std::env::VarError::NotPresent),
        },
        Some(&config),
        Some(&state),
        true,
    )
    .unwrap()
    .unwrap();
    let mut service = WorkerRpcService::new_with_authenticator(
        worker,
        Arc::new(StaticTokenAuthenticator::new(
            "target-inspection-fixture-bearer",
            CallerIdentity {
                service_name: "go-control-plane".into(),
                role: "controller".into(),
            },
        )),
    )
    .with_control_signer(signer);
    service.target_inspection = Some(Arc::new(policy));
    service
}

#[tokio::test]
async fn target_inspection_real_grpc_binds_signature_and_preserves_worker_epoch_and_files() {
    use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
    use deepseek_protocol::generated::deepseek::action::v1::{
        backup_target_inspection_client::BackupTargetInspectionClient,
        backup_target_inspection_server::BackupTargetInspectionServer,
    };
    use ed25519_dalek::{Signature, Verifier, VerifyingKey};
    use prost::Message as _;
    let (root, policy, target) = directories();
    fs::write(target.join(MARKER_NAME), marker()).unwrap();
    let service = signed_service(root.path(), policy);
    let worker = service.worker.clone();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let incoming = futures_util::stream::unfold(listener, |listener| async move {
        Some((listener.accept().await.map(|(socket, _)| socket), listener))
    });
    let (stop, stopped) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(BackupTargetInspectionServer::new(service))
            .serve_with_incoming_shutdown(incoming, async {
                let _ = stopped.await;
            })
            .await
            .unwrap();
    });
    let mut client = BackupTargetInspectionClient::connect(format!("http://{address}"))
        .await
        .unwrap();
    let input = InspectFilesystemBackupTargetRequest {
        schema_version: 1,
        fence: Some(ActionFence {
            action_id: "target-inspect-fixture".into(),
            execution_epoch: 3,
        }),
        operation_id: "target-inspect-fixture".into(),
        path: target.to_str().unwrap().into(),
        writer_fencing_token: 7,
    };
    assert_eq!(
        client.inspect(input.clone()).await.unwrap_err().code(),
        tonic::Code::Unauthenticated
    );
    let mut request = Request::new(input.clone());
    request.metadata_mut().insert(
        "authorization",
        "Bearer target-inspection-fixture-bearer".parse().unwrap(),
    );
    let receipt = client.inspect(request).await.unwrap().into_inner();
    let observation = receipt.observation.as_ref().unwrap();
    assert_eq!(observation.fence, input.fence);
    assert_eq!(observation.writer_fencing_token, 7);
    assert_eq!(observation.marker_json, marker());
    assert_eq!(observation.marker_sha256, Sha256::digest(marker()).to_vec());
    let key: [u8; 32] = URL_SAFE_NO_PAD
        .decode(&receipt.signer_public_key)
        .unwrap()
        .try_into()
        .unwrap();
    let mut message = deepseek_protocol::backup_target_inspection::SIGNATURE_DOMAIN.to_vec();
    message.extend(observation.encode_to_vec());
    VerifyingKey::from_bytes(&key)
        .unwrap()
        .verify(
            &message,
            &Signature::from_slice(&receipt.signature).unwrap(),
        )
        .unwrap();
    let state = worker.lock().await;
    assert!(state.live_epochs.is_empty());
    assert!(state.effects.is_empty());
    drop(state);
    assert_eq!(fs::read(target.join(MARKER_NAME)).unwrap(), marker());
    let mut stale = Request::new(InspectFilesystemBackupTargetRequest {
        writer_fencing_token: 6,
        ..input
    });
    stale.metadata_mut().insert(
        "authorization",
        "Bearer target-inspection-fixture-bearer".parse().unwrap(),
    );
    assert_eq!(
        client.inspect(stale).await.unwrap_err().code(),
        tonic::Code::FailedPrecondition
    );
    stop.send(()).unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn target_inspection_rpc_refuses_missing_configuration_and_wrong_roles() {
    let input = || Request::new(InspectFilesystemBackupTargetRequest::default());
    let service = WorkerRpcService::new(crate::Worker::new());
    assert_eq!(
        service.inspect(input()).await.unwrap_err().code(),
        tonic::Code::Unauthenticated
    );
    let auth = Arc::new(crate::service::StaticTokenAuthenticator::new(
        "test",
        crate::service::CallerIdentity {
            service_name: "other".into(),
            role: "controller".into(),
        },
    ));
    let service = WorkerRpcService::new_with_authenticator(crate::Worker::new(), auth);
    let mut request = input();
    request
        .metadata_mut()
        .insert("authorization", "Bearer test".parse().unwrap());
    assert_eq!(
        service.inspect(request).await.unwrap_err().code(),
        tonic::Code::PermissionDenied
    );
    let auth = Arc::new(crate::service::StaticTokenAuthenticator::new(
        "test",
        crate::service::CallerIdentity {
            service_name: "go-control-plane".into(),
            role: "controller".into(),
        },
    ));
    let service = WorkerRpcService::new_with_authenticator(crate::Worker::new(), auth);
    let mut request = Request::new(InspectFilesystemBackupTargetRequest {
        schema_version: 1,
        operation_id: "target-inspect-missing-config".into(),
        fence: Some(ActionFence {
            action_id: "target-inspect-missing-config".into(),
            execution_epoch: 1,
        }),
        writer_fencing_token: 1,
        path: "a path which must never be read".into(),
    });
    request
        .metadata_mut()
        .insert("authorization", "Bearer test".parse().unwrap());
    assert_eq!(
        service.inspect(request).await.unwrap_err().code(),
        tonic::Code::Unavailable
    );
}
