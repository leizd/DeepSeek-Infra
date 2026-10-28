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
fn media_retained_publication_sync_never_creates_or_replaces_bytes() {
    let (root, _key, mut journal, claim) = fixture();
    journal.prepare(&claim).unwrap();
    let after = journal
        .retained("media-patch-one")
        .unwrap()
        .unwrap()
        .after
        .unwrap();
    media_library::publish(root.path(), &after, None).unwrap();
    let path = media_library::library_path(root.path());
    let modified = fs::metadata(&path).unwrap().modified().unwrap();
    media_library::confirm_publication(root.path(), &after).unwrap();
    assert_eq!(read_library(root.path()).unwrap(), after);
    assert_eq!(fs::metadata(&path).unwrap().modified().unwrap(), modified);
    assert!(media_library::confirm_publication(root.path(), b"different publication").is_err());
    assert_eq!(read_library(root.path()).unwrap(), after);
    let retained = path.with_file_name("retained-library.json");
    fs::rename(&path, &retained).unwrap();
    assert!(media_library::confirm_publication(root.path(), &after).is_err());
    assert!(!path.exists());
    assert_eq!(fs::read(&retained).unwrap(), after);
    fs::rename(&retained, &path).unwrap();
    media_library::confirm_publication(root.path(), &after).unwrap();
    assert_eq!(fs::metadata(&path).unwrap().modified().unwrap(), modified);
}

#[cfg(windows)]
#[test]
fn media_windows_readonly_recovery_keeps_original_unknown_until_sync_is_possible() {
    let (root, key, mut journal, claim) = fixture();
    journal.prepare(&claim).unwrap();
    let after = journal
        .retained("media-patch-one")
        .unwrap()
        .unwrap()
        .after
        .unwrap();
    journal
        .connection
        .execute(
            "UPDATE media_operations SET state='APPLYING' WHERE operation_id='media-patch-one'",
            [],
        )
        .unwrap();
    media_library::publish(root.path(), &after, None).unwrap();
    let path = media_library::library_path(root.path());
    let original_permissions = fs::metadata(&path).unwrap().permissions();
    let mut readonly = original_permissions.clone();
    readonly.set_readonly(true);
    fs::set_permissions(&path, readonly).unwrap();
    let attempt = journal.query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key);
    fs::set_permissions(&path, original_permissions).unwrap();
    assert_eq!(attempt.unwrap_err().message(), "EFFECT_UNKNOWN");
    let unresolved = journal.retained("media-patch-one").unwrap().unwrap();
    assert_eq!(unresolved.state, "APPLYING");
    assert!(journal.terminal(&unresolved).unwrap().is_none());
    assert_eq!(read_library(root.path()).unwrap(), after);
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        0
    );
    let result = journal
        .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(result.state, MediaPatchEffectState::Applied as i32);
    assert_eq!(read_library(root.path()).unwrap(), after);
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        2
    );
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

#[test]
fn media_publication_fences_crossing_backups_without_bumping_on_receipt_replay() {
    let (root, key, mut journal, claim) = fixture();
    let generation = || deepseek_policy::mutation_gate::read_generation(root.path());
    assert_eq!(generation(), 0);
    let prepared = journal.prepare(&claim).unwrap();
    assert_eq!(
        generation(),
        0,
        "preparation cannot mutate the public store"
    );
    let outcome = journal.publish(&claim, &prepared.digest, &key).unwrap();
    assert_eq!(outcome.state, MediaPatchEffectState::Applied as i32);
    assert_eq!(
        generation(),
        2,
        "publication must use the existing before/after generation protocol"
    );
    let published = read_library(root.path()).unwrap();
    assert_eq!(
        journal.publish(&claim, &prepared.digest, &key).unwrap(),
        outcome
    );
    assert_eq!(
        journal
            .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(outcome)
    );
    assert_eq!(read_library(root.path()).unwrap(), published);
    assert_eq!(
        generation(),
        2,
        "receipt replay is not another public mutation"
    );
}

#[test]
fn media_generation_counter_failure_refuses_publication_before_the_effect_phase() {
    let (root, key, mut journal, claim) = fixture();
    let before = read_library(root.path()).unwrap();
    let prepared = journal.prepare(&claim).unwrap();
    let counter = deepseek_policy::mutation_gate::generation_path(root.path());
    fs::create_dir(&counter).unwrap();
    assert!(
        journal.publish(&claim, &prepared.digest, &key).is_err(),
        "an unavailable backup generation must fail closed"
    );
    assert_eq!(read_library(root.path()).unwrap(), before);
    assert_eq!(
        journal.retained("media-patch-one").unwrap().unwrap().state,
        "PREPARED"
    );
    fs::remove_dir(&counter).unwrap();
    let result = journal.publish(&claim, &prepared.digest, &key).unwrap();
    assert_eq!(result.state, MediaPatchEffectState::Applied as i32);
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        2
    );
}

#[test]
fn media_original_publication_reconciliation_invalidates_crossing_backups_once() {
    let (root, key, mut journal, claim) = fixture();
    journal.prepare(&claim).unwrap();
    let row = journal.retained("media-patch-one").unwrap().unwrap();
    journal
        .connection
        .execute(
            "UPDATE media_operations SET state='APPLYING' WHERE operation_id='media-patch-one'",
            [],
        )
        .unwrap();
    // Explicit old-writer/lost-reply fixture: retain the exact publication and
    // original operation but no closing generation/receipt. No RPC is faked.
    media_library::publish(root.path(), row.after.as_ref().unwrap(), None).unwrap();
    let published = read_library(root.path()).unwrap();
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        0
    );
    let result = journal
        .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(result.state, MediaPatchEffectState::Applied as i32);
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        2
    );
    assert_eq!(read_library(root.path()).unwrap(), published);
    assert_eq!(
        journal
            .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(result)
    );
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        2
    );
}

#[test]
fn media_generation_failure_cannot_settle_an_unconfirmed_original_publication() {
    let (root, key, mut journal, claim) = fixture();
    journal.prepare(&claim).unwrap();
    let row = journal.retained("media-patch-one").unwrap().unwrap();
    journal
        .connection
        .execute(
            "UPDATE media_operations SET state='APPLYING' WHERE operation_id='media-patch-one'",
            [],
        )
        .unwrap();
    media_library::publish(root.path(), row.after.as_ref().unwrap(), None).unwrap();
    let published = read_library(root.path()).unwrap();
    let counter = deepseek_policy::mutation_gate::generation_path(root.path());
    fs::create_dir(&counter).unwrap();
    assert!(
        journal
            .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
            .is_err()
    );
    assert_eq!(
        journal.retained("media-patch-one").unwrap().unwrap().state,
        "APPLYING"
    );
    assert_eq!(read_library(root.path()).unwrap(), published);
    fs::remove_dir(&counter).unwrap();
    let result = journal
        .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(result.state, MediaPatchEffectState::Applied as i32);
    assert_eq!(read_library(root.path()).unwrap(), published);
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        2
    );
}
