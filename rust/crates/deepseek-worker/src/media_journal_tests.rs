use super::*;
use deepseek_federation::{create_control_signer_bundle, load_control_signer};
use deepseek_protocol::generated::deepseek::media::v1::{MediaPatch, MediaPatchIntent};
use serde_json::{Value, json};
use std::sync::OnceLock;

fn delete_claim(mut claim: MediaPatchClaim, operation: &str, media: &str) -> MediaPatchClaim {
    let intent = claim.intent.as_mut().unwrap();
    intent.kind = 1;
    intent.operation_id = operation.into();
    intent.media_id = media.into();
    intent.patch = None;
    claim.fence.as_mut().unwrap().action_id = operation.into();
    claim.request_sha256 = sha(&intent.encode_to_vec());
    claim
}

#[test]
fn media_signer_binds_patch_and_delete_result_shapes_to_the_original_operation() {
    let (root, key, _journal, claim) = fixture();
    let before = read_library(root.path()).unwrap();
    let (patch, _) = media_library::plan(root.path(), &claim, &before).unwrap();
    assert!(key.sign_media_patch_outcome(&patch).is_ok());
    let deletion = delete_claim(claim, "media-delete-signing", "media_one");
    let (deleted, _) = media_library::plan(root.path(), &deletion, &before).unwrap();
    assert_eq!(deleted.deleted_count, 1);
    assert!(key.sign_media_patch_outcome(&deleted).is_ok());
    let mut missing = deleted.clone();
    missing.deleted_count = 0;
    missing.after_sha256 = missing.before_sha256.clone();
    assert!(key.sign_media_patch_outcome(&missing).is_ok());

    let mut too_many = deleted.clone();
    too_many.deleted_count = 2;
    let mut mixed_delete = deleted.clone();
    mixed_delete.media = patch.media.clone();
    let mut mixed_patch = patch.clone();
    mixed_patch.deleted_count = 1;
    let mut mixed_process = patch.clone();
    mixed_process.processed = Some(Default::default());
    let mut empty_patch = patch;
    empty_patch.media = None;
    let mut refused = media_library::refusal(deleted.clone(), 409, "conflict", "Conflict");
    refused.deleted_count = 1;
    let mut foreign_operation = deleted;
    foreign_operation.operation_id = "foreign-operation".into();
    foreign_operation.original_fence.as_mut().unwrap().action_id = "foreign-operation".into();
    for invalid in [
        too_many,
        mixed_delete,
        mixed_patch,
        mixed_process,
        empty_patch,
        refused,
        foreign_operation,
    ] {
        assert!(key.sign_media_patch_outcome(&invalid).is_err());
    }
    assert_eq!(read_library(root.path()).unwrap(), before);
}

fn seed_delete_data(root: &Path, vectors: bool) {
    let media = root.join(".media");
    fs::create_dir_all(media.join("segments")).unwrap();
    for id in ["media_one", "media_other"] {
        fs::create_dir_all(media.join("objects").join(id)).unwrap();
        fs::write(
            media.join("objects").join(id).join("source.pdf"),
            id.as_bytes(),
        )
        .unwrap();
        fs::write(
            media.join("segments").join(format!("{id}.json")),
            id.as_bytes(),
        )
        .unwrap();
    }
    fs::create_dir(root.join(".local-rag")).unwrap();
    deepseek_policy::sqlite_vec_runtime::register().unwrap();
    let connection = Connection::open(root.join(".local-rag/rag.sqlite3")).unwrap();
    connection.execute_batch("CREATE TABLE rag_items(item_id TEXT PRIMARY KEY,vector_id INTEGER NOT NULL UNIQUE,collection TEXT NOT NULL,source_id TEXT NOT NULL,project_id TEXT NOT NULL); INSERT INTO rag_items VALUES('target-a',101,'media','media_one','project-a'),('target-b',102,'media','media_one','project-b'),('other-media',103,'media','media_other','project-a'),('same-id-files',104,'files','media_one','project-a');").unwrap();
    if vectors {
        connection.execute_batch("CREATE VIRTUAL TABLE rag_vec USING vec0(item_id TEXT,collection TEXT,source_id TEXT,project_id TEXT,scope TEXT,embedding float[2]); INSERT INTO rag_vec(rowid,item_id,collection,source_id,project_id,scope,embedding) VALUES(101,'target-a','media','media_one','project-a','','[1,0]'),(102,'target-b','media','media_one','project-b','','[0,1]'),(103,'other-media','media','media_other','project-a','','[-1,0]'),(104,'same-id-files','files','media_one','project-a','','[0,-1]');").unwrap();
    }
}

#[test]
fn media_delete_noop_settlement_preserves_legal_edges_and_rolls_back_atomically() {
    let (root, key, mut journal, claim) = fixture();
    let claim = delete_claim(claim, "media-delete-atomic-noop", "media_missing");
    let before = read_library(root.path()).unwrap();
    let prepared = journal.prepare(&claim).unwrap();
    journal.connection.execute_batch("CREATE TRIGGER fail_noop BEFORE UPDATE ON media_operations WHEN NEW.state='APPLIED' BEGIN SELECT RAISE(ABORT,'controlled terminal failure'); END;").unwrap();
    assert!(journal.publish(&claim, &prepared.digest, &key).is_err());
    let row = journal
        .retained("media-delete-atomic-noop")
        .unwrap()
        .unwrap();
    assert_eq!(row.state, "PREPARED");
    assert!(row.outcome.is_none());
    assert_eq!(read_library(root.path()).unwrap(), before);
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        0
    );
    journal
        .connection
        .execute_batch("DROP TRIGGER fail_noop;")
        .unwrap();
    let result = journal.publish(&claim, &prepared.digest, &key).unwrap();
    assert_eq!(result.deleted_count, 0);
    assert_eq!(result.signature.len(), 64);
    assert_eq!(result.state, MediaPatchEffectState::Applied as i32);
    assert!(journal.active().unwrap().is_none());
    assert_eq!(read_library(root.path()).unwrap(), before);
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        0
    );
    drop(journal);
    let retained = MediaJournal::open(
        root.path(),
        &root.path().join("state"),
        &key.public_key(),
        "fleet-a",
        "test",
    )
    .unwrap()
    .query(
        "media-delete-atomic-noop",
        claim.fence.as_ref().unwrap(),
        true,
        &key,
    )
    .unwrap()
    .unwrap();
    assert_eq!(retained, result);
}

fn assert_delete_data(root: &Path, vectors: bool) {
    assert!(!root.join(".media/segments/media_one.json").exists());
    assert!(!root.join(".media/objects/media_one").exists());
    assert_eq!(
        fs::read(root.join(".media/objects/media_other/source.pdf")).unwrap(),
        b"media_other"
    );
    assert_eq!(
        fs::read(root.join(".media/segments/media_other.json")).unwrap(),
        b"media_other"
    );
    let connection = Connection::open(root.join(".local-rag/rag.sqlite3")).unwrap();
    let mut statement = connection
        .prepare("SELECT vector_id FROM rag_items ORDER BY vector_id")
        .unwrap();
    assert_eq!(
        statement
            .query_map([], |row| row.get::<_, i64>(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap(),
        vec![103, 104]
    );
    if vectors {
        let mut statement = connection
            .prepare("SELECT rowid FROM rag_vec ORDER BY rowid")
            .unwrap();
        assert_eq!(
            statement
                .query_map([], |row| row.get::<_, i64>(0))
                .unwrap()
                .collect::<Result<Vec<_>, _>>()
                .unwrap(),
            vec![103, 104]
        );
    }
}

#[test]
fn media_delete_cleans_target_files_and_cross_project_sqlite_and_vector_rows() {
    for vectors in [false, true] {
        let (root, key, mut journal, claim) = fixture();
        seed_delete_data(root.path(), vectors);
        let claim = delete_claim(claim, "media-delete-one", "media_one");
        let prepared = journal.prepare(&claim).unwrap();
        assert!(
            root.path()
                .join(".media/objects/media_one/source.pdf")
                .exists()
        );
        let result = journal.publish(&claim, &prepared.digest, &key).unwrap();
        assert_eq!(result.state, MediaPatchEffectState::Applied as i32);
        assert_eq!(result.deleted_count, 1);
        assert!(result.media.is_none());
        assert_delete_data(root.path(), vectors);
        let document: Value = serde_json::from_slice(&read_library(root.path()).unwrap()).unwrap();
        assert!(document["media"].as_array().unwrap().is_empty());
        // A late query returns the original signed result and cannot delete
        // later bytes that happen to use the same former media id.
        fs::create_dir(root.path().join(".media/objects/media_one")).unwrap();
        fs::write(root.path().join(".media/objects/media_one/later"), b"later").unwrap();
        let replay = journal
            .query(
                "media-delete-one",
                claim.fence.as_ref().unwrap(),
                true,
                &key,
            )
            .unwrap()
            .unwrap();
        assert_eq!(replay, result);
        assert_eq!(
            fs::read(root.path().join(".media/objects/media_one/later")).unwrap(),
            b"later"
        );
    }
}

#[test]
fn media_delete_missing_record_preserves_store_generation_and_orphan_bytes() {
    let (root, key, mut journal, claim) = fixture();
    let orphan = root.path().join(".media/objects/media_missing");
    fs::create_dir_all(&orphan).unwrap();
    fs::write(orphan.join("retained"), b"orphan retained").unwrap();
    let before = read_library(root.path()).unwrap();
    let claim = delete_claim(claim, "media-delete-missing", "media_missing");
    let prepared = journal.prepare(&claim).unwrap();
    let result = journal.publish(&claim, &prepared.digest, &key).unwrap();
    assert_eq!(result.deleted_count, 0);
    assert_eq!(result.state, MediaPatchEffectState::Applied as i32);
    assert_eq!(read_library(root.path()).unwrap(), before);
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        0
    );
    assert_eq!(
        fs::read(orphan.join("retained")).unwrap(),
        b"orphan retained"
    );
    assert_eq!(
        journal
            .query(
                "media-delete-missing",
                claim.fence.as_ref().unwrap(),
                true,
                &key
            )
            .unwrap()
            .unwrap(),
        result
    );
}

#[test]
fn media_delete_restart_finishes_original_cleanup_after_known_library_publication() {
    let (root, key, mut journal, claim) = fixture();
    seed_delete_data(root.path(), true);
    let claim = delete_claim(claim, "media-delete-recovery", "media_one");
    journal.prepare(&claim).unwrap();
    let row = journal.retained("media-delete-recovery").unwrap().unwrap();
    journal.connection.execute("UPDATE media_operations SET state='APPLYING' WHERE operation_id='media-delete-recovery'", []).unwrap();
    deepseek_policy::mutation_gate::bump_generation(root.path()).unwrap();
    assert!(media_library::publish(root.path(), row.after.as_ref().unwrap(), None).unwrap());
    let publication = read_library(root.path()).unwrap();
    drop(journal);
    let mut journal = MediaJournal::open(
        root.path(),
        &root.path().join("state"),
        &key.public_key(),
        "fleet-a",
        "test",
    )
    .unwrap();
    let result = journal
        .query(
            "media-delete-recovery",
            claim.fence.as_ref().unwrap(),
            true,
            &key,
        )
        .unwrap()
        .unwrap();
    assert_eq!(result.deleted_count, 1);
    assert_eq!(result.original_fence, claim.fence);
    assert_eq!(result.request_sha256, claim.request_sha256);
    assert_eq!(read_library(root.path()).unwrap(), publication);
    assert_eq!(
        deepseek_policy::mutation_gate::read_generation(root.path()),
        3
    );
    assert_delete_data(root.path(), true);
}

#[test]
fn media_delete_cleanup_failure_keeps_applying_without_a_signed_success() {
    let (root, key, mut journal, claim) = fixture();
    seed_delete_data(root.path(), false);
    let claim = delete_claim(claim, "media-delete-cleanup-failure", "media_one");
    let prepared = journal.prepare(&claim).unwrap();
    let connection = Connection::open(root.path().join(".local-rag/rag.sqlite3")).unwrap();
    connection.execute_batch("CREATE TRIGGER scope_guard BEFORE DELETE ON rag_items BEGIN SELECT RAISE(ABORT,'blocked'); END;").unwrap();
    assert_eq!(
        journal
            .publish(&claim, &prepared.digest, &key)
            .unwrap_err()
            .message(),
        "EFFECT_UNKNOWN"
    );
    let row = journal
        .retained("media-delete-cleanup-failure")
        .unwrap()
        .unwrap();
    assert_eq!(row.state, "APPLYING");
    assert!(row.outcome.is_none());
    let publication = read_library(root.path()).unwrap();
    assert!(
        root.path()
            .join(".media/objects/media_one/source.pdf")
            .exists()
    );
    connection
        .execute_batch("DROP TRIGGER scope_guard;")
        .unwrap();
    let result = journal
        .query(
            "media-delete-cleanup-failure",
            claim.fence.as_ref().unwrap(),
            true,
            &key,
        )
        .unwrap()
        .unwrap();
    assert_eq!(result.deleted_count, 1);
    assert_eq!(read_library(root.path()).unwrap(), publication);
    assert_delete_data(root.path(), false);
}

#[test]
fn media_patch_and_delete_share_one_unresolved_publication_barrier() {
    let (root, key, mut journal, patch) = fixture();
    journal.prepare(&patch).unwrap();
    let deletion = delete_claim(patch.clone(), "media-delete-barrier", "media_one");
    let before = read_library(root.path()).unwrap();
    assert!(journal.prepare(&deletion).is_err());
    assert_eq!(read_library(root.path()).unwrap(), before);
    let stopped = journal
        .query("media-patch-one", patch.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(stopped.state, MediaPatchEffectState::Refused as i32);
    let prepared = journal.prepare(&deletion).unwrap();
    assert_eq!(
        journal
            .publish(&deletion, &prepared.digest, &key)
            .unwrap()
            .deleted_count,
        1
    );
}

#[test]
fn media_original_query_refuses_a_different_go_claim_with_the_same_fence() {
    let (_root, _key, mut journal, claim) = fixture();
    journal.prepare(&claim).unwrap();
    let mut other = claim.clone();
    other.intent.as_mut().unwrap().media_id = "media_other".into();
    other.request_sha256 = sha(&other.intent.as_ref().unwrap().encode_to_vec());
    assert!(journal.verify_original_claim(&other).is_err());
    journal.verify_original_claim(&claim).unwrap();
}

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
        kind: 0,
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
        create: None,
        process: None,
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

fn create_claim(
    journal: &MediaJournal,
    mut claim: MediaPatchClaim,
    payload: Value,
) -> MediaPatchClaim {
    use deepseek_protocol::generated::deepseek::media::v1::{
        MediaCreateManifest, MediaCreatePayload,
    };
    let object = crate::media_values::from_json(&payload).unwrap();
    let Some(deepseek_protocol::generated::deepseek::media::v1::media_value::Kind::ObjectValue(
        object,
    )) = object.kind
    else {
        panic!()
    };
    let bytes = MediaCreatePayload {
        registration: Some(object),
        upload: None,
    }
    .encode_to_vec();
    let intent = claim.intent.as_mut().unwrap();
    intent.kind = 2;
    intent.patch = None;
    intent.media_id = "media_0123456789abcdef".into();
    intent.operation_id = "media-create-0123456789abcdef0123456789abcdef".into();
    intent.create = Some(MediaCreateManifest {
        payload_sha256: sha(&bytes),
        payload_bytes: bytes.len() as u64,
    });
    claim.fence.as_mut().unwrap().action_id = intent.operation_id.clone();
    claim.request_sha256 = sha(&intent.encode_to_vec());
    let path = crate::media_create::operation_dir(journal.creation_staging(), &intent.operation_id)
        .unwrap()
        .join("payload.pb");
    crate::media_create::persist_bound(&path, &bytes).unwrap();
    claim
}

#[path = "media_processing_tests.rs"]
mod processing;
#[path = "media_process_tests.rs"]
mod standalone_process;

#[test]
fn media_create_original_result_and_source_survive_close_and_replay_without_republishing() {
    let (root, key, mut journal, claim) = fixture();
    let claim = create_claim(
        &journal,
        claim,
        json!({"type":"webpage","html":"<h1>retained</h1>"}),
    );
    let operation = claim.intent.as_ref().unwrap().operation_id.clone();
    let before = read_library(root.path()).unwrap();
    let prepared = journal.prepare(&claim).unwrap();
    assert_eq!(read_library(root.path()).unwrap(), before);
    assert!(
        !root
            .path()
            .join(".media/objects/media_0123456789abcdef")
            .exists()
    );
    let outcome = journal.publish(&claim, &prepared.digest, &key).unwrap();
    assert!(outcome.created.is_some());
    assert!(outcome.media.is_none());
    assert_eq!(outcome.signature.len(), 64);
    let published = read_library(root.path()).unwrap();
    let source = root
        .path()
        .join(".media/objects/media_0123456789abcdef/source.html");
    assert_eq!(fs::read(&source).unwrap(), b"<h1>retained</h1>");
    let record = journal.created_record(&claim).unwrap();
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
            .query(&operation, claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(outcome.clone())
    );
    assert_eq!(journal.created_record(&claim).unwrap(), record);
    assert_eq!(read_library(root.path()).unwrap(), published);
    // Terminal replay is evidence only: never overwrite a later id reuse.
    fs::write(&source, b"later retained identity").unwrap();
    assert_eq!(
        journal
            .query(&operation, claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(outcome)
    );
    assert_eq!(fs::read(source).unwrap(), b"later retained identity");
}

#[test]
fn media_create_lost_receipt_finishes_only_original_bound_source_after_library_publication() {
    let (root, key, mut journal, claim) = fixture();
    let claim = create_claim(
        &journal,
        claim,
        json!({"type":"webpage","html":"original source"}),
    );
    let operation = claim.intent.as_ref().unwrap().operation_id.clone();
    journal.prepare(&claim).unwrap();
    let row = journal.retained(&operation).unwrap().unwrap();
    journal
        .connection
        .execute(
            "UPDATE media_operations SET state='APPLYING' WHERE operation_id=?1",
            [&operation],
        )
        .unwrap();
    media_library::publish(root.path(), row.after.as_ref().unwrap(), None).unwrap();
    let after = read_library(root.path()).unwrap();
    let source = root
        .path()
        .join(".media/objects/media_0123456789abcdef/source.html");
    fs::create_dir_all(&source).unwrap();
    assert!(
        journal
            .query(&operation, claim.fence.as_ref().unwrap(), true, &key)
            .is_err()
    );
    assert_eq!(
        journal.retained(&operation).unwrap().unwrap().state,
        "APPLYING"
    );
    assert_eq!(read_library(root.path()).unwrap(), after);
    fs::remove_dir(&source).unwrap();
    let result = journal
        .query(&operation, claim.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(result.original_fence, claim.fence);
    assert_eq!(result.request_sha256, claim.request_sha256);
    assert_eq!(fs::read(&source).unwrap(), b"original source");
    assert_eq!(read_library(root.path()).unwrap(), after);
    assert_eq!(
        journal
            .query(&operation, claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(result)
    );
}

#[test]
fn media_create_large_original_json_record_keeps_control_receipt_compact() {
    let (root, key, mut journal, claim) = fixture();
    let metadata: serde_json::Map<String, Value> = (0..500)
        .map(|i| (format!("field{i:03}"), json!("z".repeat(9000))))
        .collect();
    let payload = json!({"type":"pdf","metadata":metadata});
    assert!(serde_json::to_vec(&payload).unwrap().len() < 16_000_000);
    let claim = create_claim(&journal, claim, payload);
    assert!(claim.encoded_len() < 1024);
    let prepared = journal.prepare(&claim).unwrap();
    let outcome = journal.publish(&claim, &prepared.digest, &key).unwrap();
    assert!(outcome.encoded_len() < 1024);
    assert!(outcome.created.as_ref().unwrap().record_bytes > 4 * 1024 * 1024);
    assert_eq!(
        journal.created_record(&claim).unwrap().encoded_len() as u64,
        outcome.created.unwrap().record_bytes
    );
    assert!(read_library(root.path()).unwrap().len() > 4 * 1024 * 1024);
}
