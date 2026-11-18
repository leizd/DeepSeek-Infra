use super::*;
use deepseek_protocol::ActionFence;
use std::fs;

#[test]
fn target_marker_preparation_reopens_with_original_bytes_and_only_confirmed_marker_is_reconciled() {
    let temporary = tempfile::tempdir().unwrap();
    let root = temporary.path().canonicalize().unwrap();
    let state = root.join("state");
    let workspace = root.join("workspace");
    let target = root.join("target");
    for folder in [&state, &workspace, &target] {
        fs::create_dir(folder).unwrap();
    }
    let legacy_path = workspace.join(".backup-targets");
    fs::create_dir(&legacy_path).unwrap();
    fs::write(
        legacy_path.join("installation.id"),
        b"inst_0123456789abcdef\n",
    )
    .unwrap();
    let policy = LocationPolicy::new(&[workspace.clone(), state.clone()]).unwrap();
    let directory = MarkerDirectory::open(&target).unwrap();
    let operation = "target-inspect-public-v1-marker-preparation";
    let claim = TargetMarkerClaim {
        schema_version: 1,
        fence: Some(ActionFence {
            action_id: operation.into(),
            execution_epoch: 3,
        }),
        operation_id: operation.into(),
        writer_fencing_token: 7,
        cutover_revision: 4,
        issued_at_unix: 1794963600,
        lease_until_unix: 1794963630,
        requested_path: target.to_str().unwrap().into(),
        canonical_path: target.to_str().unwrap().into(),
        target_id: "target_030303030303".into(),
        request_sha256: vec![1; 32],
        nonce: vec![3; 32],
        fleet_id: "fleet-a".into(),
        environment: "isolated".into(),
        directory_identity: directory.identity().unwrap().to_vec(),
    };
    assert!(wire::valid_marker_claim(&claim));
    let first = MarkerJournal::new(&state, &workspace).unwrap();
    let prepared = first.prepare(&claim).unwrap();
    let hash = preparation_digest(&prepared);
    let claim_hash = Sha256::digest(claim.encode_to_vec());
    let genesis: serde_json::Value = serde_json::from_slice(&prepared.marker_json).unwrap();
    assert_eq!(genesis["ownerInstallationId"], "inst_0123456789abcdef");
    assert_eq!(genesis["targetGeneration"], 0);
    assert_eq!(genesis["latestCommitHash"], "0".repeat(64));
    assert_eq!(
        fs::read(legacy_path.join("installation.id")).unwrap(),
        b"inst_0123456789abcdef\n"
    );
    assert!(first.dispatched(operation, 3, &claim_hash).is_err());
    assert!(read_effect(&policy, &prepared).is_err());
    let permit = TargetMarkerDispatchPermit {
        schema_version: 1,
        preparation_sha256: hash.to_vec(),
        lease_until_unix: claim.lease_until_unix,
    };
    first.accept(&claim, &permit).unwrap();
    let reopened = MarkerJournal::new(&state, &workspace).unwrap();
    assert_eq!(reopened.prepare(&claim).unwrap(), prepared);
    assert_eq!(
        reopened.dispatched(operation, 3, &claim_hash).unwrap(),
        prepared
    );
    assert!(reopened.dispatched(operation, 2, &claim_hash).is_err());
    assert!(reopened.dispatched(operation, 3, &[5; 32]).is_err());

    // A stale authorization check runs after staging but before the marker is
    // published. A failed check leaves the externally visible path absent.
    assert!(
        directory
            .create_marker(&prepared.marker_json, &[0; 32], || Err(io::Error::other(
                "fixture expired"
            )))
            .is_err()
    );
    assert!(
        directory
            .marker(wire::MARKER_INITIALIZATION_MAX_JSON)
            .unwrap()
            .is_none()
    );
    directory
        .create_marker(&prepared.marker_json, &hash, || Ok(()))
        .unwrap();
    let effect = read_effect(&policy, &prepared).unwrap();
    assert_eq!(effect.marker_json, prepared.marker_json);
    assert_eq!(effect.preparation_sha256, hash);
    drop(directory);

    fs::rename(&target, root.join("original")).unwrap();
    fs::create_dir(&target).unwrap();
    fs::write(
        target.join(crate::target_marker_files::MARKER_NAME),
        &prepared.marker_json,
    )
    .unwrap();
    assert!(read_effect(&policy, &prepared).is_err());
}
