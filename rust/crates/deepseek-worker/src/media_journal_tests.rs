use super::*;
use deepseek_federation::{create_control_signer_bundle, load_control_signer};
use deepseek_protocol::generated::deepseek::media::v1::{MediaPatch, MediaPatchIntent};
use serde_json::{Value, json};
use std::sync::OnceLock;

fn key() -> ControlSigningKey {
    static BUNDLE: OnceLock<Value> = OnceLock::new();
    let password = b"isolated-media-custody-fixture-password";
    let bundle =
        BUNDLE.get_or_init(|| create_control_signer_bundle(password, "fleet-a", "test").unwrap());
    load_control_signer(
        bundle,
        password,
        "fleet-a",
        "test",
        bundle["binding"]["signerPublicKey"].as_str().unwrap(),
    )
    .unwrap()
}
fn fixture() -> (
    tempfile::TempDir,
    ControlSigningKey,
    MediaJournal,
    MediaPatchClaim,
) {
    let root = tempfile::tempdir().unwrap();
    fs::create_dir(root.path().join(".media")).unwrap();
    fs::write(media_library::library_path(root.path()),serde_json::to_vec(&json!({"schemaVersion":"media-library.v1","media":[{"mediaId":"media_one","projectId":"","type":"pdf","mimeType":"application/pdf","title":"Before","path":"","status":"ready","createdAt":"2026-01-01T00:00:00+00:00","updatedAt":"2026-01-01T00:00:00+00:00","metadata":{"keep":true}}]})).unwrap()).unwrap();
    let key = key();
    let journal = MediaJournal::open(
        root.path(),
        &root.path().join("state"),
        &key.public_key(),
        "fleet-a",
        "test",
    )
    .unwrap();
    let intent = MediaPatchIntent {
        schema_version: 1,
        operation_id: "media-patch-one".into(),
        media_id: "media_one".into(),
        patch: Some(MediaPatch {
            title: Some(crate::media_values::from_json(&json!("After")).unwrap()),
            project_id: None,
            metadata: Some(
                crate::media_values::from_json(&json!({"added":[null,false,2]})).unwrap(),
            ),
        }),
    };
    let claim = MediaPatchClaim {
        schema_version: 1,
        request_sha256: sha(&intent.encode_to_vec()),
        intent: Some(intent.clone()),
        fence: Some(ActionFence {
            action_id: intent.operation_id.clone(),
            execution_epoch: 1,
        }),
        owner: "go-fixture".into(),
        claim_token: "a".repeat(64),
        writer_fencing_token: 1,
        lease_until_unix: i64::MAX,
        state: "CLAIMED".into(),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
    };
    (root, key, journal, claim)
}
#[test]
fn media_publication_and_original_receipt_survive_restart_and_later_updates() {
    let (root, key, mut journal, claim) = fixture();
    let before = read_library(root.path()).unwrap();
    let prepared = journal.prepare(&claim).unwrap();
    assert!(prepared.terminal.is_none());
    assert_eq!(read_library(root.path()).unwrap(), before);
    let outcome = journal.publish(&claim, &prepared.digest, &key).unwrap();
    assert_eq!(outcome.state, MediaPatchEffectState::Applied as i32);
    assert_eq!(outcome.signature.len(), 64);
    let after = read_library(root.path()).unwrap();
    let doc: Value = serde_json::from_slice(&after).unwrap();
    assert_eq!(doc["media"][0]["title"], "After");
    assert_eq!(doc["media"][0]["metadata"]["keep"], true);
    assert_eq!(
        doc["media"][0]["metadata"]["added"],
        json!([null, false, 2])
    );
    assert_eq!(doc["nativePublication"]["operationId"], "media-patch-one");
    drop(journal);
    let mut journal = MediaJournal::open(
        root.path(),
        &root.path().join("state"),
        &key.public_key(),
        "fleet-a",
        "test",
    )
    .unwrap();
    assert_eq!(
        journal
            .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(outcome.clone())
    );
    assert_eq!(read_library(root.path()).unwrap(), after);
    let mut next = claim.clone();
    next.intent.as_mut().unwrap().operation_id = "media-patch-two".into();
    next.intent.as_mut().unwrap().patch.as_mut().unwrap().title =
        Some(crate::media_values::from_json(&json!("Later")).unwrap());
    next.fence.as_mut().unwrap().action_id = "media-patch-two".into();
    next.request_sha256 = sha(&next.intent.as_ref().unwrap().encode_to_vec());
    let p = journal.prepare(&next).unwrap();
    journal.publish(&next, &p.digest, &key).unwrap();
    let later = read_library(root.path()).unwrap();
    assert_ne!(later, after);
    assert_eq!(
        journal
            .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(outcome)
    );
    assert_eq!(read_library(root.path()).unwrap(), later);
}
#[test]
fn media_lost_reply_recovers_exact_published_original_without_another_write() {
    let (root, key, mut journal, claim) = fixture();
    let p = journal.prepare(&claim).unwrap();
    let row = journal.retained("media-patch-one").unwrap().unwrap();
    journal
        .connection
        .execute(
            "UPDATE media_operations SET state='APPLYING' WHERE operation_id='media-patch-one'",
            [],
        )
        .unwrap();
    media_library::publish(root.path(), row.after.as_ref().unwrap(), None).unwrap();
    let after = read_library(root.path()).unwrap();
    drop(journal);
    let mut journal = MediaJournal::open(
        root.path(),
        &root.path().join("state"),
        &key.public_key(),
        "fleet-a",
        "test",
    )
    .unwrap();
    let recovered = journal
        .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(recovered.state, MediaPatchEffectState::Applied as i32);
    assert_eq!(read_library(root.path()).unwrap(), after);
    assert_eq!(journal.publish(&claim, &p.digest, &key).unwrap(), recovered);
    assert_eq!(read_library(root.path()).unwrap(), after);
}
#[test]
fn media_prepared_abort_fences_a_late_old_permit() {
    let (root, key, mut journal, claim) = fixture();
    let before = read_library(root.path()).unwrap();
    let p = journal.prepare(&claim).unwrap();
    let refused = journal
        .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(refused.state, MediaPatchEffectState::Refused as i32);
    assert_eq!(refused.http_status, 409);
    assert_eq!(journal.publish(&claim, &p.digest, &key).unwrap(), refused);
    assert_eq!(read_library(root.path()).unwrap(), before);
}
#[test]
fn media_uncertain_publication_does_not_infer_absence_or_release_the_library() {
    let (root, key, mut journal, claim) = fixture();
    let before = read_library(root.path()).unwrap();
    let p = journal.prepare(&claim).unwrap();
    journal
        .connection
        .execute(
            "UPDATE media_operations SET state='APPLYING' WHERE operation_id='media-patch-one'",
            [],
        )
        .unwrap();
    let unknown = journal
        .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(unknown.state, MediaPatchEffectState::Unknown as i32);
    assert!(unknown.signature.is_empty());
    assert!(journal.publish(&claim, &p.digest, &key).is_err());
    assert_eq!(read_library(root.path()).unwrap(), before);
    let mut next = claim.clone();
    next.intent.as_mut().unwrap().operation_id = "media-patch-two".into();
    next.fence.as_mut().unwrap().action_id = "media-patch-two".into();
    next.request_sha256 = sha(&next.intent.as_ref().unwrap().encode_to_vec());
    assert!(journal.prepare(&next).is_err());
    assert!(
        journal
            .query(
                "missing",
                &ActionFence {
                    action_id: "missing".into(),
                    execution_epoch: 1
                },
                true,
                &key
            )
            .unwrap()
            .is_none()
    );
    let mut wrong = claim.fence.clone().unwrap();
    wrong.execution_epoch = 2;
    assert!(
        journal
            .query("media-patch-one", &wrong, true, &key)
            .is_err()
    );
}
#[test]
fn media_revision_change_before_publication_is_a_conclusive_no_effect_refusal() {
    let (root, key, mut journal, claim) = fixture();
    let p = journal.prepare(&claim).unwrap();
    fs::write(media_library::library_path(root.path()), b"external change").unwrap();
    let outcome = journal.publish(&claim, &p.digest, &key).unwrap();
    assert_eq!(outcome.state, MediaPatchEffectState::Refused as i32);
    assert_eq!(outcome.http_status, 409);
    assert_eq!(read_library(root.path()).unwrap(), b"external change");
}
#[test]
fn media_mutation_keeps_original_not_found_and_metadata_merge_rules() {
    let (root, key, mut journal, mut claim) = fixture();
    let before = read_library(root.path()).unwrap();
    claim.intent.as_mut().unwrap().media_id = "media_absent".into();
    claim.request_sha256 = sha(&claim.intent.as_ref().unwrap().encode_to_vec());
    let p = journal.prepare(&claim).unwrap();
    let refusal = journal.publish(&claim, &p.digest, &key).unwrap();
    assert_eq!(refusal.http_status, 404);
    assert_eq!(refusal.error_message, "Media not found");
    assert_eq!(read_library(root.path()).unwrap(), before);
}
#[test]
fn media_journal_rejects_foreign_identity_and_does_not_modify_foreign_files() {
    let (root, key, journal, _) = fixture();
    drop(journal);
    assert!(
        MediaJournal::open(
            root.path(),
            &root.path().join("state"),
            &key.public_key(),
            "foreign",
            "test"
        )
        .is_err()
    );
    let state = root.path().join("foreign/rust-worker");
    fs::create_dir_all(&state).unwrap();
    let p = state.join("media-effects.sqlite3");
    let bytes = b"not an owned SQLite database";
    fs::write(&p, bytes).unwrap();
    assert!(
        MediaJournal::open(
            root.path(),
            &root.path().join("foreign"),
            &key.public_key(),
            "fleet-a",
            "test"
        )
        .is_err()
    );
    assert_eq!(fs::read(p).unwrap(), bytes);
}
#[cfg(unix)]
#[test]
fn media_library_symlink_cannot_receive_a_publication() {
    use std::os::unix::fs::symlink;
    let (root, _, mut journal, claim) = fixture();
    let target = root.path().join("unrelated");
    fs::write(&target, b"retain me").unwrap();
    fs::remove_file(media_library::library_path(root.path())).unwrap();
    symlink(&target, media_library::library_path(root.path())).unwrap();
    assert!(journal.prepare(&claim).is_err());
    assert_eq!(fs::read(target).unwrap(), b"retain me");
}
