use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs;
use tempfile::tempdir;

use deepseek_storage::backup::{BackupEngine, BackupItem, BackupParams};
use deepseek_storage::receipt::validate_committed_documents;
use deepseek_storage::restore::{RestoreEngine, RestoreError, sanitize_relative_path};

fn sha256_hex(data: &[u8]) -> String {
    let mut s = String::with_capacity(64);
    for b in Sha256::digest(data) {
        use std::fmt::Write as _;
        let _ = write!(s, "{b:02x}");
    }
    s
}

fn sample_backup_params() -> BackupParams {
    BackupParams {
        policy_id: "pol-production-01".to_string(),
        backup_id: "bk-2026-prod-001".to_string(),
        run_id: "run-001".to_string(),
        schedule_slot: "2026-09-06T00:00:00Z".to_string(),
        created_at: "2026-09-06T00:00:05Z".to_string(),
        storage_protocol: "object-set-v1".to_string(),
        target_id: "tgt-s3-prod".to_string(),
        snapshot_kind: "full".to_string(),
        fencing_token: 42,
        target_generation: 1,
        previous_commit_hash: deepseek_storage::receipt::GENESIS_COMMIT_HASH.to_string(),
    }
}

fn sample_items() -> Vec<BackupItem> {
    vec![
        BackupItem {
            name: "etc/config.json".to_string(),
            payload: b"{\"tier\": \"prod\", \"shards\": 4}".to_vec(),
            is_control: false,
        },
        BackupItem {
            name: "bin/weights.bin".to_string(),
            payload: vec![0x42; 1024],
            is_control: false,
        },
        BackupItem {
            name: "var/state.txt".to_string(),
            payload: b"epoch: 42\nstatus: active\n".to_vec(),
            is_control: false,
        },
    ]
}

#[test]
fn backup_and_atomic_restore_e2e() {
    let temp = tempdir().expect("tempdir");
    let target_dir = temp.path().join("restored_target");

    let items = sample_items();
    let params = sample_backup_params();

    // 1. Build backup
    let result = BackupEngine::build_backup(&params, &items).expect("build_backup");
    assert_eq!(result.receipt.objects.len(), 3);
    assert_eq!(result.receipt.size, 29 + 1024 + 25);
    assert_eq!(result.total_bytes, result.receipt.size);

    // Validate committed document invariants
    let (receipt, commit) =
        validate_committed_documents(&result.receipt_bytes, &result.commit_bytes)
            .expect("validate_committed_documents");
    assert_eq!(receipt.backup_id, "bk-2026-prod-001");
    assert_eq!(commit.fencing_token, 42);

    // Build payload store keyed by digest
    let mut store: HashMap<String, Vec<u8>> = HashMap::new();
    for item in &items {
        store.insert(sha256_hex(&item.payload), item.payload.clone());
    }

    // 2. Restore into target directory
    let summary = RestoreEngine::restore_payloads(
        &result.receipt_bytes,
        &result.commit_bytes,
        &target_dir,
        |digest| {
            store
                .get(digest)
                .cloned()
                .ok_or_else(|| RestoreError::NotFound(digest.to_string()))
        },
    )
    .expect("restore_payloads");

    assert_eq!(summary.restored_objects, 3);
    assert_eq!(summary.total_bytes, result.total_bytes);

    // Verify each restored object matches byte-for-byte
    for item in &items {
        let digest = sha256_hex(&item.payload);
        let restored_file = target_dir.join(&digest);
        assert!(restored_file.exists(), "Restored file must exist: {digest}");
        let contents = fs::read(&restored_file).expect("read restored file");
        assert_eq!(contents, item.payload, "Payload mismatch for {digest}");
    }

    // Verify staging directory was purged
    let entries: Vec<_> = fs::read_dir(&target_dir)
        .expect("read_dir")
        .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
        .collect();
    assert_eq!(entries.len(), 3);
    for entry in entries {
        assert!(
            !entry.starts_with(".restore-staging-"),
            "Staging dir should be removed: {entry}"
        );
    }
}

#[test]
fn restore_fails_closed_on_corrupt_payload_and_purges_staging() {
    let temp = tempdir().expect("tempdir");
    let target_dir = temp.path().join("restored_corrupt");

    let items = sample_items();
    let params = sample_backup_params();
    let result = BackupEngine::build_backup(&params, &items).expect("build_backup");

    let mut store: HashMap<String, Vec<u8>> = HashMap::new();
    for (i, item) in items.iter().enumerate() {
        let digest = sha256_hex(&item.payload);
        let mut data = item.payload.clone();
        if i == 1 {
            // Corrupt one byte of the second item
            data[0] ^= 0xff;
        }
        store.insert(digest, data);
    }

    let err = RestoreEngine::restore_payloads(
        &result.receipt_bytes,
        &result.commit_bytes,
        &target_dir,
        |digest| {
            store
                .get(digest)
                .cloned()
                .ok_or_else(|| RestoreError::NotFound(digest.to_string()))
        },
    )
    .expect_err("restore should fail on corrupted payload");

    match err {
        RestoreError::IntegrityMismatch { expected, actual } => {
            assert_ne!(expected, actual);
        }
        other => panic!("Expected IntegrityMismatch, got {other:?}"),
    }

    // Target dir should NOT contain any promoted files
    if target_dir.exists() {
        let entries: Vec<_> = fs::read_dir(&target_dir)
            .expect("read_dir")
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        assert!(
            entries.is_empty(),
            "Target directory must be empty after fail-closed error: {entries:?}"
        );
    }
}

#[test]
fn restore_fails_closed_on_size_mismatch() {
    let temp = tempdir().expect("tempdir");
    let target_dir = temp.path().join("restored_truncated");

    let items = sample_items();
    let params = sample_backup_params();
    let result = BackupEngine::build_backup(&params, &items).expect("build_backup");

    let mut store: HashMap<String, Vec<u8>> = HashMap::new();
    for (i, item) in items.iter().enumerate() {
        let digest = sha256_hex(&item.payload);
        let mut data = item.payload.clone();
        if i == 0 {
            // Truncate first item
            data.truncate(5);
        }
        store.insert(digest, data);
    }

    let err = RestoreEngine::restore_payloads(
        &result.receipt_bytes,
        &result.commit_bytes,
        &target_dir,
        |digest| {
            store
                .get(digest)
                .cloned()
                .ok_or_else(|| RestoreError::NotFound(digest.to_string()))
        },
    )
    .expect_err("restore should fail on truncated payload");

    match err {
        RestoreError::SizeMismatch { expected, actual } => {
            assert_eq!(expected, 29);
            assert_eq!(actual, 5);
        }
        other => panic!("Expected SizeMismatch, got {other:?}"),
    }

    // Target dir should NOT contain any promoted files
    if target_dir.exists() {
        let entries: Vec<_> = fs::read_dir(&target_dir)
            .expect("read_dir")
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();
        assert!(
            entries.is_empty(),
            "Target dir must be empty on size mismatch fail-closed"
        );
    }
}

#[test]
fn restore_fails_closed_on_tampered_commit() {
    let temp = tempdir().expect("tempdir");
    let target_dir = temp.path().join("restored_tampered_doc");

    let items = sample_items();
    let params = sample_backup_params();
    let result = BackupEngine::build_backup(&params, &items).expect("build_backup");

    let mut tampered_commit = result.commit_bytes.clone();
    tampered_commit[10] ^= 0x01; // flip a byte in JSON

    let mut store: HashMap<String, Vec<u8>> = HashMap::new();
    for item in &items {
        store.insert(sha256_hex(&item.payload), item.payload.clone());
    }

    let err = RestoreEngine::restore_payloads(
        &result.receipt_bytes,
        &tampered_commit,
        &target_dir,
        |digest| {
            store
                .get(digest)
                .cloned()
                .ok_or_else(|| RestoreError::NotFound(digest.to_string()))
        },
    )
    .expect_err("restore should fail on tampered commit bytes");

    assert!(matches!(err, RestoreError::Document(_)));
}

#[test]
fn sanitize_relative_path_traversal_prevention() {
    assert!(sanitize_relative_path("safe_file.bin").is_ok());
    assert!(sanitize_relative_path("sub/dir/safe_file.bin").is_ok());
    assert!(
        sanitize_relative_path("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
            .is_ok()
    );

    // Forbidden path traversals
    assert!(matches!(
        sanitize_relative_path("../escape.bin"),
        Err(RestoreError::PathTraversal(_))
    ));
    assert!(matches!(
        sanitize_relative_path("a/../../escape.bin"),
        Err(RestoreError::PathTraversal(_))
    ));
    assert!(matches!(
        sanitize_relative_path("/etc/passwd"),
        Err(RestoreError::PathTraversal(_))
    ));
}
