use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use deepseek_policy::backup_mirror::{MirrorStore, PutRequest, RecipientGroups, sha256_hex};
use deepseek_policy::mirror_handoff::{import_inventory, receipt_path};
use deepseek_policy::python_json::dumps_compact;
use serde_json::{Value, json};

struct Fixture {
    _workspace: tempfile::TempDir,
    source: PathBuf,
    target: PathBuf,
    manifest: PathBuf,
    identity: String,
    recipient: String,
    envelope: Value,
}

impl Fixture {
    fn new() -> Self {
        Self::with_snapshots(0)
    }

    fn with_snapshots(snapshot_count: usize) -> Self {
        let workspace = tempfile::tempdir().unwrap();
        let source = workspace.path().join("source/.backup-mirror");
        let target = workspace.path().join("target/.backup-mirror");
        let manifest = workspace.path().join("export.json");
        let (identity, recipient) = backup_crypto::ephemeral_identity();
        let mut envelope =
            json!({"schemaVersion":1,"conversations":[{"id":"handoff-东区"}],"conflicts":[]});
        envelope["digest"] = json!(sha256_hex(dumps_compact(&envelope).as_bytes()));
        MirrorStore::new(&source)
            .put(request(&recipient, envelope.clone(), 1), false)
            .unwrap();
        let files = MirrorStore::new(&source)
            .files("mirror_main", None)
            .unwrap();
        for index in 0..snapshot_count {
            // Complete immutable copies make the real process copy observable
            // without fabricating crypto or adding fault knobs to the CLI.
            let generation = format!("gen_{:08x}", 0xaa000000 + index);
            let directory = source.join("mirror_main/generations").join(&generation);
            std::fs::create_dir(&directory).unwrap();
            std::fs::copy(
                &files.ciphertext,
                directory.join(files.ciphertext.file_name().unwrap()),
            )
            .unwrap();
            let mut metadata = files.metadata.clone();
            metadata["generationId"] = json!(generation);
            std::fs::write(directory.join("metadata.json"), dumps_compact(&metadata)).unwrap();
        }
        std::fs::create_dir(source.join("mirror_main/previous")).unwrap();
        let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(3)
            .unwrap();
        let python = std::env::var("PYTHON").unwrap_or_else(|_| "python".into());
        let result = Command::new(python)
            .current_dir(repo)
            .env("PYTHONUTF8", "1")
            .args(["-c", "from pathlib import Path; import sys; from scripts.native_mirror_handoff import export_and_fence; export_and_fence(Path(sys.argv[1]), Path(sys.argv[2]), 'native-transfer-1', Path(sys.argv[3]))"])
            .args([&source, &target, &manifest])
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "offline exporter: {}",
            String::from_utf8_lossy(&result.stderr)
        );
        Self {
            _workspace: workspace,
            source,
            target,
            manifest,
            identity,
            recipient,
            envelope,
        }
    }
}

fn request(recipient: &str, envelope: Value, sequence: i64) -> PutRequest {
    PutRequest {
        profile_id: "mirror_main".into(),
        envelope,
        source_epoch: "epoch-handoff".into(),
        recipients: RecipientGroups::Explicit(vec![recipient.into()]),
        acknowledged_at: None,
        client_replica_id: "handoff-test".into(),
        client_sequence: sequence,
        expected_head_generation_id: None,
        now: None,
    }
}

fn revoke(fixture: &Fixture) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_mirror-inventory-import"))
        .arg("--revoke")
        .arg("--manifest")
        .arg(&fixture.manifest)
        .arg("--source")
        .arg(&fixture.source)
        .arg("--target")
        .arg(&fixture.target)
        .output()
        .unwrap()
}

#[test]
fn real_cli_revocation_is_durable_and_cannot_reenable_the_candidate() {
    let fixture = Fixture::new();
    let imported = import_inventory(&fixture.manifest, &fixture.source, &fixture.target).unwrap();
    let output = revoke(&fixture);
    assert!(
        output.status.success(),
        "native candidate revocation: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let receipt: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(receipt["phase"], "revoked");
    assert_eq!(receipt["targetDigest"], imported.source_digest);
    assert_eq!(revoke(&fixture).stdout, output.stdout);
    let persisted = std::fs::read(receipt_path(&fixture.target).unwrap()).unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&persisted).unwrap(),
        receipt
    );
    assert!(
        import_inventory(&fixture.manifest, &fixture.source, &fixture.target)
            .unwrap_err()
            .contains("revoked")
    );
    for root in [&fixture.source, &fixture.target] {
        assert_eq!(
            MirrorStore::new(root)
                .put(
                    request(&fixture.recipient, fixture.envelope.clone(), 2),
                    false
                )
                .unwrap_err()
                .status,
            423
        );
    }
    assert_eq!(
        std::fs::read(receipt_path(&fixture.target).unwrap()).unwrap(),
        persisted
    );
}

#[test]
fn revocation_refuses_drift_and_preserves_both_fences() {
    for damage_source in [false, true] {
        let fixture = Fixture::new();
        import_inventory(&fixture.manifest, &fixture.source, &fixture.target).unwrap();
        let root = if damage_source {
            &fixture.source
        } else {
            &fixture.target
        };
        let files = MirrorStore::new(root).files("mirror_main", None).unwrap();
        std::fs::write(files.ciphertext, "unexpected external rewrite").unwrap();
        let path = receipt_path(&fixture.target).unwrap();
        let before = std::fs::read(&path).unwrap();
        let output = revoke(&fixture);
        assert!(!output.status.success());
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("inventory"),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(std::fs::read(path).unwrap(), before);
        assert!(
            fixture
                .source
                .with_file_name(".backup-mirror.native-handoff.json")
                .is_file()
        );
    }
}

#[test]
fn real_offline_handback_preserves_age_consumers_and_native_writer_denial() {
    let fixture = Fixture::new();
    import_inventory(&fixture.manifest, &fixture.source, &fixture.target).unwrap();
    assert!(revoke(&fixture).status.success());
    let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .unwrap();
    let python = std::env::var("PYTHON").unwrap_or_else(|_| "python".into());
    let code = "from pathlib import Path; import sys; from scripts.native_mirror_handoff import handback_revoked_candidate; from deepseek_infra.infra.workspace import backup_mirror; source,target,manifest=map(Path,sys.argv[1:]); receipt=handback_revoked_candidate(source,target,manifest); backup_mirror.BACKUP_MIRROR_DIR=source; backup_mirror._guard_mirror_mutation('put_frontend_mirror'); source_files=backup_mirror.mirror_files('mirror_main'); assert source_files[0].is_file(); assert source_files[2]['ciphertextSha256']; assert handback_revoked_candidate(source,target,manifest)==receipt";
    let output = Command::new(python)
        .current_dir(repo)
        .env("PYTHONUTF8", "1")
        .env("DEEPSEEK_RUNTIME_MODE", "python_authoritative")
        .args(["-c", code])
        .args([&fixture.source, &fixture.target, &fixture.manifest])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        !fixture
            .source
            .with_file_name(".backup-mirror.native-handoff.json")
            .exists()
    );
    assert!(
        fixture
            .source
            .with_file_name(".backup-mirror.native-handback.json")
            .is_file()
    );
    for root in [&fixture.source, &fixture.target] {
        let store = MirrorStore::new(root);
        let files = store.files("mirror_main", None).unwrap();
        let mut plaintext = Vec::new();
        backup_crypto::decrypt_identity(
            std::fs::File::open(files.ciphertext).unwrap(),
            &mut plaintext,
            fixture.identity.clone(),
        )
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&plaintext).unwrap(),
            fixture.envelope
        );
        assert_eq!(
            store
                .put(
                    request(&fixture.recipient, fixture.envelope.clone(), 2),
                    false
                )
                .unwrap_err()
                .status,
            423
        );
    }
    assert_eq!(
        serde_json::from_slice::<Value>(&revoke(&fixture).stdout).unwrap()["phase"],
        "revoked"
    );
    assert!(import_inventory(&fixture.manifest, &fixture.source, &fixture.target).is_err());
}

#[test]
fn restore_fences_on_either_workspace_block_candidate_import() {
    for target_workspace in [false, true] {
        for raw in ["{}", "null", "[]", "broken restore fence"] {
            let fixture = Fixture::new();
            let root = if target_workspace {
                &fixture.target
            } else {
                &fixture.source
            };
            let parent = root.parent().unwrap();
            std::fs::create_dir_all(parent).unwrap();
            std::fs::write(parent.join(".workspace-restore-fence.json"), raw).unwrap();
            assert!(import_inventory(&fixture.manifest, &fixture.source, &fixture.target).is_err());
            assert!(!fixture.target.exists());
            assert!(!receipt_path(&fixture.target).unwrap().exists());
        }
    }
}

#[test]
fn import_waits_for_the_target_workspace_lock_and_rechecks_restore_fence() {
    let fixture = Fixture::new();
    let parent = fixture.target.parent().unwrap();
    std::fs::create_dir_all(parent).unwrap();
    let lock = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(parent.join(".workspace-mutation.lock"))
        .unwrap();
    deepseek_policy::file_lock::lock_exclusive(&lock).unwrap();
    let (sender, receiver) = std::sync::mpsc::channel();
    let source = fixture.source.clone();
    let target = fixture.target.clone();
    let manifest = fixture.manifest.clone();
    let thread = std::thread::spawn(move || {
        sender
            .send(import_inventory(&manifest, &source, &target))
            .unwrap();
    });
    let early = receiver.recv_timeout(Duration::from_secs(1));
    std::fs::write(parent.join(".workspace-restore-fence.json"), "{}").unwrap();
    deepseek_policy::file_lock::unlock(&lock).unwrap();
    assert!(
        matches!(early, Err(std::sync::mpsc::RecvTimeoutError::Timeout)),
        "{early:?}"
    );
    assert!(
        receiver
            .recv_timeout(Duration::from_secs(15))
            .unwrap()
            .is_err()
    );
    thread.join().unwrap();
    assert!(!fixture.target.exists());
    assert!(!receipt_path(&fixture.target).unwrap().exists());
}

#[test]
fn import_attests_every_file_and_native_consumer_decrypts_real_age() {
    let fixture = Fixture::new();
    let receipt = import_inventory(&fixture.manifest, &fixture.source, &fixture.target).unwrap();
    assert_eq!(receipt.phase, "imported");
    assert_eq!(receipt.target_digest, receipt.source_digest);
    assert!(fixture.target.join("mirror_main/previous").is_dir());
    let files = MirrorStore::new(&fixture.target)
        .files(
            "mirror_main",
            Some(std::slice::from_ref(&fixture.recipient)),
        )
        .unwrap();
    let source_files = MirrorStore::new(&fixture.source)
        .files("mirror_main", None)
        .unwrap();
    assert_eq!(
        std::fs::read(&files.ciphertext).unwrap(),
        std::fs::read(&source_files.ciphertext).unwrap()
    );
    assert_eq!(
        std::fs::read(&files.metadata_path).unwrap(),
        std::fs::read(&source_files.metadata_path).unwrap()
    );
    let mut plaintext = Vec::new();
    backup_crypto::decrypt_identity(
        std::fs::File::open(files.ciphertext).unwrap(),
        &mut plaintext,
        fixture.identity.clone(),
    )
    .unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&plaintext).unwrap(),
        fixture.envelope
    );
    assert_eq!(
        import_inventory(&fixture.manifest, &fixture.source, &fixture.target).unwrap(),
        receipt
    );
}

#[test]
fn source_drift_after_fencing_does_not_create_a_native_store() {
    let fixture = Fixture::new();
    let source_file = MirrorStore::new(&fixture.source)
        .files("mirror_main", None)
        .unwrap();
    std::fs::write(
        source_file.ciphertext,
        b"source changed outside its writer fence",
    )
    .unwrap();
    assert!(import_inventory(&fixture.manifest, &fixture.source, &fixture.target).is_err());
    assert!(!fixture.target.exists());
    assert!(!receipt_path(&fixture.target).unwrap().exists());
}

#[test]
fn existing_native_data_is_preserved_and_never_adopted() {
    let fixture = Fixture::new();
    std::fs::create_dir_all(&fixture.target).unwrap();
    let unrelated = fixture.target.join("unrelated-state");
    std::fs::write(&unrelated, b"preserve this asset").unwrap();
    assert!(import_inventory(&fixture.manifest, &fixture.source, &fixture.target).is_err());
    assert_eq!(std::fs::read(unrelated).unwrap(), b"preserve this asset");
    assert!(!receipt_path(&fixture.target).unwrap().exists());
}

#[test]
fn imported_candidate_requires_ownership_admission_even_after_restart() {
    let fixture = Fixture::new();
    import_inventory(&fixture.manifest, &fixture.source, &fixture.target).unwrap();
    let head = fixture.target.join("mirror_main/HEAD.json");
    let before = std::fs::read(&head).unwrap();
    let restarted = MirrorStore::new(&fixture.target);
    let error = restarted
        .put(
            request(&fixture.recipient, fixture.envelope.clone(), 2),
            false,
        )
        .unwrap_err();
    assert_eq!(error.status, 423);
    assert!(error.message.contains("ownership admission"));
    assert_eq!(std::fs::read(head).unwrap(), before);
}

#[test]
fn target_drift_invalidates_attestation_without_rewriting_receipts() {
    let fixture = Fixture::new();
    import_inventory(&fixture.manifest, &fixture.source, &fixture.target).unwrap();
    let receipt = receipt_path(&fixture.target).unwrap();
    let before = std::fs::read(&receipt).unwrap();
    let files = MirrorStore::new(&fixture.target)
        .files("mirror_main", None)
        .unwrap();
    std::fs::write(files.ciphertext, b"damaged native copy").unwrap();
    assert!(import_inventory(&fixture.manifest, &fixture.source, &fixture.target).is_err());
    assert_eq!(std::fs::read(receipt).unwrap(), before);
}

#[test]
fn the_fenced_source_also_denies_a_restarted_native_writer() {
    let fixture = Fixture::new();
    let head = fixture.source.join("mirror_main/HEAD.json");
    let before = std::fs::read(&head).unwrap();
    let error = MirrorStore::new(&fixture.source)
        .put(
            request(&fixture.recipient, fixture.envelope.clone(), 2),
            false,
        )
        .unwrap_err();
    assert_eq!(error.status, 423);
    assert!(error.message.contains("persistently fenced"));
    assert_eq!(std::fs::read(head).unwrap(), before);
}

#[test]
fn a_killed_import_process_recovers_from_its_durable_reservation() {
    let fixture = Fixture::with_snapshots(128);
    let stderr_path = fixture._workspace.path().join("import-child.stderr");
    let mut child = Command::new(env!("CARGO_BIN_EXE_mirror-inventory-import"))
        .args([
            "--manifest",
            fixture.manifest.to_str().unwrap(),
            "--source",
            fixture.source.to_str().unwrap(),
            "--target",
            fixture.target.to_str().unwrap(),
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(std::fs::File::create(&stderr_path).unwrap())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    let receipt = receipt_path(&fixture.target).unwrap();
    loop {
        if let Ok(bytes) = std::fs::read(&receipt) {
            let reservation = serde_json::from_slice::<Value>(&bytes).unwrap();
            let stage = Path::new(reservation["stagingRoot"].as_str().unwrap());
            if reservation["phase"] == "copying" && stage.join("mirror_main/HEAD.json").is_file() {
                child
                    .kill()
                    .expect("kill the actual importer while its reservation is held");
                assert!(!child.wait().unwrap().success());
                break;
            }
        }
        if let Some(status) = child.try_wait().unwrap() {
            panic!(
                "importer exited before fault injection: {status}: {}",
                std::fs::read_to_string(&stderr_path).unwrap()
            );
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("importer never persisted its copy reservation");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let recovered = import_inventory(&fixture.manifest, &fixture.source, &fixture.target).unwrap();
    assert_eq!(recovered.phase, "imported");
    assert_eq!(recovered.source_digest, recovered.target_digest);
    let candidate = MirrorStore::new(&fixture.target)
        .files("mirror_main", None)
        .unwrap();
    let mut plaintext = Vec::new();
    backup_crypto::decrypt_identity(
        std::fs::File::open(candidate.ciphertext).unwrap(),
        &mut plaintext,
        fixture.identity,
    )
    .unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&plaintext).unwrap(),
        fixture.envelope
    );
}
