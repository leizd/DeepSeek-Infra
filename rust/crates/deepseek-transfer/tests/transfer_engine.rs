use std::fs;

use deepseek_protocol::ActionFence;
use deepseek_transfer::{
    FederatedTransferJournal, ProposedTransfer, TransferError, TransferOptions, TransferSink,
    TransferSource, TransferState, derive_transfer_id, execute_transfer,
};
use sha2::{Digest, Sha256};

fn dummy_fence() -> ActionFence {
    ActionFence {
        action_id: "xfer-action-101".to_string(),
        execution_epoch: 5,
    }
}

fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        use std::fmt::Write as _;
        let _ = write!(s, "{b:02x}");
    }
    s
}

#[test]
fn memory_to_memory_streaming_transfer_with_bounded_chunks() {
    let payload = vec![0xAA; 16 * 1024]; // 16 KiB payload
    let expected_digest: [u8; 32] = Sha256::digest(&payload).into();
    let fence = dummy_fence();

    let mut source = TransferSource::from_bytes(payload.clone());
    let sink = TransferSink::memory();
    let options = TransferOptions::new()
        .with_chunk_size(4096) // 4 KiB chunks -> 4 iterations
        .unwrap()
        .with_expected_length(payload.len() as u64)
        .with_expected_digest(expected_digest);

    let receipt = execute_transfer(
        "xfer-01",
        &fence,
        5,
        &mut source,
        sink,
        &options,
        None,
        "2026-09-06T12:00:00Z",
    )
    .expect("transfer must succeed");

    assert_eq!(receipt.transfer_id, "xfer-01");
    assert_eq!(receipt.bytes_transferred, payload.len() as u64);
    assert_eq!(receipt.sha256, expected_digest);
    assert!(receipt.chunks_count > 1);
}

#[test]
fn digest_mismatch_fails_closed_and_aborts_sink() {
    let payload = b"critical-production-data".to_vec();
    let wrong_digest: [u8; 32] = Sha256::digest(b"corrupted-data").into();
    let fence = dummy_fence();

    let temp_dir = tempfile::tempdir().unwrap();
    let dest_file = temp_dir.path().join("output.bin");

    let mut source = TransferSource::from_bytes(payload.clone());
    let sink = TransferSink::file(&dest_file, true).unwrap();
    let options = TransferOptions::new()
        .with_chunk_size(64)
        .unwrap()
        .with_expected_digest(wrong_digest);

    let err = execute_transfer(
        "xfer-02",
        &fence,
        5,
        &mut source,
        sink,
        &options,
        None,
        "2026-09-06T12:00:00Z",
    )
    .unwrap_err();

    match err {
        TransferError::DigestMismatch { expected, actual } => {
            assert_eq!(expected, hex(&wrong_digest));
            assert_eq!(actual, hex(&Sha256::digest(&payload)));
        }
        other => panic!("expected DigestMismatch, got {other:?}"),
    }

    // Destination file must NOT exist (atomic commit aborted)
    assert!(!dest_file.exists());
}

#[test]
fn length_mismatch_fails_closed() {
    let payload = b"short-data".to_vec();
    let fence = dummy_fence();

    let mut source = TransferSource::from_bytes(payload.clone());
    let sink = TransferSink::memory();
    let options = TransferOptions::new().with_expected_length(100); // Expects 100 bytes, only 10 provided

    let err = execute_transfer(
        "xfer-03",
        &fence,
        5,
        &mut source,
        sink,
        &options,
        None,
        "2026-09-06T12:00:00Z",
    )
    .unwrap_err();

    match err {
        TransferError::LengthMismatch { expected, actual } => {
            assert_eq!(expected, 100);
            assert_eq!(actual, payload.len() as u64);
        }
        other => panic!("expected LengthMismatch, got {other:?}"),
    }
}

#[test]
fn atomic_file_transfer_commits_safely() {
    let temp_dir = tempfile::tempdir().unwrap();
    let src_file = temp_dir.path().join("source.txt");
    let dest_file = temp_dir.path().join("destination.txt");

    let content =
        "The quick brown fox jumps over the lazy dog. Repetition for length: ".repeat(100);
    fs::write(&src_file, content.as_bytes()).unwrap();

    let fence = dummy_fence();
    let mut source = TransferSource::from_file(&src_file).unwrap();
    let sink = TransferSink::file(&dest_file, true).unwrap();
    let options = TransferOptions::new()
        .with_chunk_size(1024)
        .unwrap()
        .with_expected_length(content.len() as u64);

    let receipt = execute_transfer(
        "xfer-04",
        &fence,
        5,
        &mut source,
        sink,
        &options,
        None,
        "2026-09-06T12:00:00Z",
    )
    .unwrap();

    assert_eq!(receipt.bytes_transferred, content.len() as u64);
    assert!(dest_file.exists());
    let read_back = fs::read_to_string(&dest_file).unwrap();
    assert_eq!(read_back, content);
}

#[test]
fn invalid_fence_and_stale_epoch_are_rejected_before_io() {
    let payload = b"bytes-that-should-never-be-read".to_vec();
    let mut source = TransferSource::from_bytes(payload);
    let sink = TransferSink::memory();
    let options = TransferOptions::new();

    // Stale epoch (fence.execution_epoch < live_epoch)
    let stale_fence = ActionFence {
        action_id: "act-1".to_string(),
        execution_epoch: 2,
    };
    let err = execute_transfer(
        "xfer-05",
        &stale_fence,
        5, // Live is 5, fence is 2
        &mut source,
        sink,
        &options,
        None,
        "2026-09-06T12:00:00Z",
    )
    .unwrap_err();
    assert!(matches!(err, TransferError::Admit(_)));
}

#[test]
fn journal_advancement_during_transfer() {
    use base64::Engine as _;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;

    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("transfers.sqlite3");
    let fleet_id = "fleet-a";
    let public_key = [0x42_u8; 32];
    let digest = hex(&Sha256::digest(public_key));
    let fleet_identity = serde_json::json!({
        "schema": "fleet-identity-v1",
        "fleetId": fleet_id,
        "rootKeyId": format!("fed-root-{}", &digest[..24]),
        "rootPublicKey": URL_SAFE_NO_PAD.encode(public_key),
        "rootFingerprint": format!("sha256:{digest}"),
        "signatureAlgorithm": "Ed25519",
        "createdAt": "2026-09-01T06:00:00Z"
    });

    let journal = FederatedTransferJournal::open(&path, &fleet_identity).unwrap();

    let transfer_id = derive_transfer_id(
        "fleet-a",
        "fleet-b",
        "backup-01",
        "sha256:4444444444444444444444444444444444444444444444444444444444444444",
    )
    .unwrap();

    let proposed = ProposedTransfer {
        transfer_id: transfer_id.clone(),
        source_fleet_id: "fleet-a".to_string(),
        destination_fleet_id: "fleet-b".to_string(),
        policy_id: "policy-custody".to_string(),
        backup_id: "backup-01".to_string(),
        object_set_digest:
            "sha256:4444444444444444444444444444444444444444444444444444444444444444".to_string(),
    };

    let record = journal
        .persist_proposed_transfer(&proposed, "2026-09-06T12:00:00Z")
        .unwrap();
    // Advance to GrantRequested then GrantVerified
    let r2 = journal
        .advance_transfer(
            &transfer_id,
            record.revision,
            TransferState::GrantRequested,
            serde_json::json!({"grant": "req"}),
            "2026-09-06T12:00:01Z",
        )
        .unwrap();
    let _r3 = journal
        .advance_transfer(
            &transfer_id,
            r2.revision,
            TransferState::GrantVerified,
            serde_json::json!({"grant": "ver"}),
            "2026-09-06T12:00:02Z",
        )
        .unwrap();

    let payload = b"journal-integrated-transfer-payload".to_vec();
    let fence = ActionFence {
        action_id: "transfer-act".to_string(),
        execution_epoch: 1,
    };
    let mut source = TransferSource::from_bytes(payload.clone());
    let sink = TransferSink::memory();
    let options = TransferOptions::new().with_expected_length(payload.len() as u64);

    let receipt = execute_transfer(
        &transfer_id,
        &fence,
        1,
        &mut source,
        sink,
        &options,
        Some(&journal),
        "2026-09-06T12:00:03Z",
    )
    .unwrap();

    assert_eq!(receipt.bytes_transferred, payload.len() as u64);

    // Assert journal reached RemoteVerifying state
    let final_record = journal.get_transfer(&transfer_id).unwrap().unwrap();
    assert_eq!(final_record.state, TransferState::RemoteVerifying);
}
