//! Private journal/recovery tests, not promotion or public HTTP evidence.
use super::{tests::fixture, *};
use serde_json::json;

fn next_claim(mut claim: MediaPatchClaim, operation: &str) -> MediaPatchClaim {
    claim.intent.as_mut().unwrap().operation_id = operation.into();
    claim.fence.as_mut().unwrap().action_id = operation.into();
    claim.request_sha256 = sha(&claim.intent.as_ref().unwrap().encode_to_vec());
    claim
}
fn reopen(root: &Path, key: &ControlSigningKey) -> MediaJournal {
    MediaJournal::open(
        root,
        &root.join("state"),
        &key.public_key(),
        "fleet-a",
        "test",
    )
    .unwrap()
}
fn count(journal: &MediaJournal, query: &str) -> i64 {
    journal
        .connection
        .query_row(query, [], |r| r.get(0))
        .unwrap()
}

#[test]
fn failed_prepared_insert_retires_both_images_without_a_public_effect() {
    let (root, _key, mut journal, claim) = fixture();
    let before = read_library(root.path()).unwrap();
    journal.connection.execute_batch("CREATE TRIGGER injected_prepare_failure BEFORE INSERT ON media_operations BEGIN SELECT RAISE(ABORT,'injected preparation insert failure'); END").unwrap();
    assert!(journal.prepare(&claim).is_err());
    assert_eq!(
        count(&journal, "SELECT count(*) FROM media_image_manifests"),
        0
    );
    assert_eq!(count(&journal, "SELECT count(*) FROM media_operations"), 0);
    assert_eq!(fs::read_dir(&journal.image_root).unwrap().count(), 0);
    assert_eq!(read_library(root.path()).unwrap(), before);
    assert!(!root.path().join(".workspace-generation").exists());
    journal
        .connection
        .execute_batch("DROP TRIGGER injected_prepare_failure")
        .unwrap();
    assert!(journal.prepare(&claim).is_ok());
}

#[test]
fn restart_retires_a_durably_owned_partial_copy_and_sealed_images_are_untouched() {
    let (root, key, journal, claim) = fixture();
    let before = read_snapshot(root.path()).unwrap();
    let (_, after, _) = media_library::plan(root.path(), &claim, &before).unwrap();
    media_image_custody::reserve(&journal.connection, &claim, &before, after.as_ref()).unwrap();
    let directory = media_image_custody::directory(&journal.image_root, &claim).unwrap();
    before.retain(&directory.join("before.json")).unwrap();
    // Crash after only the first durable image: the reservation survives.
    drop(journal);
    let mut journal = reopen(root.path(), &key);
    let p = journal.prepare(&claim).unwrap();
    assert_eq!(
        count(
            &journal,
            "SELECT count(*) FROM media_image_manifests WHERE phase='SEALED'"
        ),
        1
    );
    let before_hash = sha(&fs::read(directory.join("before.json")).unwrap());
    journal.collect_unsealed_images("media-patch-one").unwrap();
    assert_eq!(
        sha(&fs::read(directory.join("before.json")).unwrap()),
        before_hash
    );
    assert!(directory.join("after.json").is_file());
    assert_eq!(
        journal
            .publish(&claim, &p.digest, &key)
            .unwrap()
            .http_status,
        200
    );
    journal.sweep_images_best_effort();
    assert!(!directory.exists());
}

#[test]
fn bounded_preparation_sweep_keeps_fair_progress_across_repeated_restarts() {
    let (root, key, journal, claim) = fixture();
    let image = Image::Legacy(b"private preparation".to_vec());
    for n in 0..9 {
        let c = next_claim(claim.clone(), &format!("media-patch-orphan-{n:02}"));
        media_image_custody::reserve(&journal.connection, &c, &image, None).unwrap();
        media_image_custody::retain(&journal.image_root, &c, &image, None).unwrap();
        if n < 4 {
            fs::write(
                media_image_custody::directory(&journal.image_root, &c)
                    .unwrap()
                    .join("unexpected"),
                b"retain unknown files",
            )
            .unwrap();
        }
    }
    drop(journal);
    for expected in [9, 5, 4] {
        let journal = reopen(root.path(), &key);
        journal.sweep_images_best_effort();
        assert_eq!(
            count(&journal, "SELECT count(*) FROM media_image_manifests"),
            expected
        );
        // Each process performs exactly one bounded sweep, then exits.
    }
    let journal = reopen(root.path(), &key);
    for n in 0..4 {
        let c = next_claim(claim.clone(), &format!("media-patch-orphan-{n:02}"));
        let path = media_image_custody::directory(&journal.image_root, &c).unwrap();
        assert_eq!(
            fs::read(path.join("unexpected")).unwrap(),
            b"retain unknown files"
        );
        fs::remove_file(path.join("unexpected")).unwrap();
    }
    journal.sweep_images_best_effort();
    assert_eq!(
        count(&journal, "SELECT count(*) FROM media_image_manifests"),
        0
    );
    assert_eq!(count(&journal, "SELECT count(*) FROM media_operations"), 0);
}

#[test]
fn missing_or_corrupt_active_images_never_settle_an_applying_effect() {
    for corrupt in [false, true] {
        let (root, key, mut journal, claim) = fixture();
        let before = read_library(root.path()).unwrap();
        let p = journal.prepare(&claim).unwrap();
        let path = media_image_custody::directory(&journal.image_root, &claim)
            .unwrap()
            .join("before.json");
        if corrupt {
            let mut bytes = fs::read(&path).unwrap();
            bytes[0] ^= 1;
            fs::write(&path, bytes).unwrap();
        } else {
            fs::remove_file(&path).unwrap();
        }
        assert!(journal.publish(&claim, &p.digest, &key).is_err());
        assert_eq!(read_library(root.path()).unwrap(), before);
        assert!(!root.path().join(".workspace-generation").exists());
        assert!(
            journal
                .query(
                    "media-patch-one",
                    claim.fence.as_ref().unwrap(),
                    false,
                    &key
                )
                .is_err()
        );
        journal
            .connection
            .execute(
                "UPDATE media_operations SET state='APPLYING' WHERE operation_id=?1",
                ["media-patch-one"],
            )
            .unwrap();
        drop(journal);
        let mut journal = reopen(root.path(), &key);
        assert!(
            journal
                .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
                .is_err()
        );
        assert!(journal.active().unwrap().is_some());
        assert_eq!(
            count(
                &journal,
                "SELECT count(*) FROM media_operations WHERE state='APPLYING' AND outcome IS NULL"
            ),
            1
        );
        assert!(
            journal
                .prepare(&next_claim(claim, "media-patch-next"))
                .is_err()
        );
        assert_eq!(read_library(root.path()).unwrap(), before);
    }
}

#[test]
fn positive_prepared_abort_and_terminal_replay_do_not_require_raw_images() {
    let (root, key, mut journal, claim) = fixture();
    let before = read_library(root.path()).unwrap();
    let p = journal.prepare(&claim).unwrap();
    media_image_custody::collect(&journal.image_root, &claim).unwrap();
    let result = journal
        .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(result.state, MediaPatchEffectState::Refused as i32);
    assert_eq!(result.error_code, "execution_interrupted");
    assert_eq!(result.signature.len(), 64);
    journal.sweep_images_best_effort();
    assert_eq!(
        count(
            &journal,
            "SELECT count(*) FROM media_image_manifests WHERE phase='COLLECTED'"
        ),
        1
    );
    drop(journal);
    let mut journal = reopen(root.path(), &key);
    assert_eq!(journal.publish(&claim, &p.digest, &key).unwrap(), result);
    assert_eq!(read_library(root.path()).unwrap(), before);
}

#[test]
fn terminal_gc_failure_cannot_hide_the_receipt_and_is_retried_on_later_preparation() {
    let (root, key, mut journal, claim) = fixture();
    let p = journal.prepare(&claim).unwrap();
    let directory = media_image_custody::directory(&journal.image_root, &claim).unwrap();
    fs::write(directory.join("unexpected"), b"do not recursively delete").unwrap();
    let outcome = journal.publish(&claim, &p.digest, &key).unwrap();
    assert_eq!(outcome.http_status, 200);
    assert_eq!(
        count(
            &journal,
            "SELECT count(*) FROM media_image_manifests WHERE phase='SEALED'"
        ),
        1
    );
    assert!(directory.join("before.json").exists());
    journal.sweep_images_best_effort();
    assert!(!directory.join("before.json").exists());
    assert!(!directory.join("after.json").exists());
    let after = read_library(root.path()).unwrap();
    drop(journal);
    let mut journal = reopen(root.path(), &key);
    assert_eq!(
        journal
            .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(outcome)
    );
    assert_eq!(
        fs::read(directory.join("unexpected")).unwrap(),
        b"do not recursively delete"
    );
    fs::remove_file(directory.join("unexpected")).unwrap();
    journal
        .prepare(&next_claim(claim, "media-patch-later"))
        .unwrap();
    assert_eq!(
        count(
            &journal,
            "SELECT count(*) FROM media_image_manifests WHERE phase='COLLECTED'"
        ),
        1
    );
    assert!(!directory.exists());
    assert_eq!(read_library(root.path()).unwrap(), after);
}

#[cfg(unix)]
#[test]
fn terminal_replay_survives_a_symlinked_image_path_without_following_or_deleting_it() {
    let (root, key, mut journal, claim) = fixture();
    let p = journal.prepare(&claim).unwrap();
    let directory = media_image_custody::directory(&journal.image_root, &claim).unwrap();
    fs::write(directory.join("blocker"), b"keep GC pending").unwrap();
    let outcome = journal.publish(&claim, &p.digest, &key).unwrap();
    journal.sweep_images_best_effort();
    fs::remove_file(directory.join("blocker")).unwrap();
    fs::remove_dir(&directory).unwrap();
    let external = tempfile::tempdir().unwrap();
    fs::write(external.path().join("before.json"), b"external data").unwrap();
    std::os::unix::fs::symlink(external.path(), &directory).unwrap();
    drop(journal);
    let mut journal = reopen(root.path(), &key);
    assert_eq!(
        journal
            .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(outcome.clone())
    );
    assert_eq!(journal.publish(&claim, &p.digest, &key).unwrap(), outcome);
    journal.sweep_images_best_effort();
    assert_eq!(
        fs::read(external.path().join("before.json")).unwrap(),
        b"external data"
    );
    assert_eq!(
        count(
            &journal,
            "SELECT count(*) FROM media_image_manifests WHERE phase='SEALED'"
        ),
        1
    );
    fs::remove_file(&directory).unwrap();
    journal.sweep_images_best_effort();
    assert!(
        journal
            .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
            .is_ok()
    );
    assert_eq!(
        count(
            &journal,
            "SELECT count(*) FROM media_image_manifests WHERE phase='COLLECTED'"
        ),
        1
    );
}

#[test]
fn sealed_image_identity_and_collection_receipts_are_mechanically_immutable() {
    let (_root, key, mut journal, claim) = fixture();
    let p = journal.prepare(&claim).unwrap();
    for sql in [
        "UPDATE media_image_manifests SET before_sha256=zeroblob(32)",
        "UPDATE media_image_manifests SET claim=X'00'",
        "UPDATE media_image_manifests SET phase='RETAINING'",
        "UPDATE media_image_manifests SET phase='COLLECTED'",
        "DELETE FROM media_image_manifests",
        "INSERT OR REPLACE INTO media_image_manifests SELECT * FROM media_image_manifests",
    ] {
        assert!(journal.connection.execute(sql, []).is_err(), "{sql}");
    }
    let outcome = journal.publish(&claim, &p.digest, &key).unwrap();
    journal.sweep_images_best_effort();
    let manifest = media_image_custody::manifest(&journal.connection, "media-patch-one")
        .unwrap()
        .unwrap();
    assert_eq!(manifest.phase, "COLLECTED");
    assert_eq!(
        manifest.collected_outcome_sha256,
        Some(sha(&outcome.encode_to_vec()))
    );
    for sql in [
        "UPDATE media_image_manifests SET phase='SEALED',collected_outcome_sha256=NULL",
        "UPDATE media_image_manifests SET collected_outcome_sha256=zeroblob(32)",
        "DELETE FROM media_image_manifests",
    ] {
        assert!(journal.connection.execute(sql, []).is_err(), "{sql}");
    }
}

#[test]
fn terminal_calls_return_while_private_maintenance_holds_the_workspace_gate() {
    let (root, key, mut journal, claim) = fixture();
    let p = journal.prepare(&claim).unwrap();
    let outcome = journal.publish(&claim, &p.digest, &key).unwrap();
    let (ready_tx, ready_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let gate_root = root.path().to_path_buf();
    let guard = std::thread::spawn(move || {
        let _gate = exclusive_gate(&gate_root).unwrap();
        ready_tx.send(()).unwrap();
        let _ = release_rx.recv_timeout(Duration::from_secs(3));
    });
    ready_rx.recv_timeout(Duration::from_secs(3)).unwrap();
    let start = std::time::Instant::now();
    let queried = journal.query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key);
    let replayed = journal.publish(&claim, &p.digest, &key);
    let prepared = journal.prepare(&claim);
    let elapsed = start.elapsed();
    let _ = release_tx.send(());
    guard.join().unwrap();
    assert_eq!(queried.unwrap(), Some(outcome.clone()));
    assert_eq!(replayed.unwrap(), outcome);
    assert!(prepared.unwrap().terminal.is_some());
    assert!(
        elapsed < Duration::from_secs(1),
        "terminal calls waited for workspace maintenance: {elapsed:?}"
    );
    // No call retired images or accessed the private cleanup path.
    assert_eq!(
        count(
            &journal,
            "SELECT count(*) FROM media_image_manifests WHERE phase='SEALED'"
        ),
        1
    );
    let directory = media_image_custody::directory(&journal.image_root, &claim).unwrap();
    assert!(directory.join("before.json").is_file());
    assert!(directory.join("after.json").is_file());
}

#[test]
fn compact_result_custody_is_verified_before_any_terminal_raw_image_is_collected() {
    let (root, key, mut journal, mut claim) = fixture();
    claim.intent.as_mut().unwrap().max_outcome_schema_version = 2;
    claim.request_sha256 = sha(&claim.intent.as_ref().unwrap().encode_to_vec());
    let path = media_library::library_path(root.path());
    let mut doc: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    doc["media"][0]["metadata"] = serde_json::Value::Object(
        (0..18)
            .map(|n| (format!("field_{n:02}"), json!("m".repeat(120_000))))
            .collect(),
    );
    fs::write(path, serde_json::to_vec(&doc).unwrap()).unwrap();
    let p = journal.prepare(&claim).unwrap();
    let result = journal.publish(&claim, &p.digest, &key).unwrap();
    assert_eq!(result.schema_version, 2);
    let record =
        crate::media_patch_result::record_path(&journal.creation_staging, "media-patch-one")
            .unwrap();
    let retained_record = record.with_extension("held");
    fs::rename(&record, &retained_record).unwrap();
    let directory = media_image_custody::directory(&journal.image_root, &claim).unwrap();
    journal.sweep_images_best_effort();
    assert!(directory.join("before.json").is_file());
    assert!(directory.join("after.json").is_file());
    assert_eq!(
        count(
            &journal,
            "SELECT count(*) FROM media_image_manifests WHERE phase='SEALED'"
        ),
        1
    );
    assert_eq!(
        journal
            .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(result)
    );
    fs::rename(&retained_record, &record).unwrap();
    journal.sweep_images_best_effort();
    assert!(!directory.exists());
    assert!(record.is_file());
}

#[test]
fn schema1_identity_or_shape_rejection_does_not_partially_migrate_the_journal() {
    for extra_schema in [false, true] {
        let (root, key, _original, _claim) = fixture();
        let state = root.path().join("untrusted-legacy");
        fs::create_dir_all(state.join("rust-worker")).unwrap();
        let db_path = state.join("rust-worker/media-effects.sqlite3");
        let old = Connection::open(&db_path).unwrap();
        for statement in SCHEMA {
            old.execute(statement, []).unwrap();
        }
        old.pragma_update(None, "application_id", APPLICATION_ID)
            .unwrap();
        old.pragma_update(None, "user_version", 1).unwrap();
        let signer = if extra_schema {
            key.public_key()
        } else {
            "foreign-signer".into()
        };
        old.execute(
            "INSERT INTO media_identity VALUES(1,?1,?2,'fleet-a','test')",
            params![
                fs::canonicalize(root.path()).unwrap().to_str().unwrap(),
                signer
            ],
        )
        .unwrap();
        if extra_schema {
            old.execute("CREATE TABLE unrecognized(value TEXT)", [])
                .unwrap();
        }
        drop(old);
        assert!(
            MediaJournal::open(root.path(), &state, &key.public_key(), "fleet-a", "test").is_err()
        );
        let old = Connection::open(&db_path).unwrap();
        assert_eq!(
            old.pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert_eq!(
            old.query_row(
                "SELECT count(*) FROM sqlite_schema WHERE name LIKE 'media_image_%'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            0
        );
    }
}

#[test]
fn one_hundred_updates_keep_only_current_effect_images_and_small_historical_descriptors() {
    let (root, key, mut journal, claim) = fixture();
    let mut doc: serde_json::Value =
        serde_json::from_slice(&read_library(root.path()).unwrap()).unwrap();
    let mut other = doc["media"][0].clone();
    other["mediaId"] = json!("media_other");
    other["metadata"] = json!({"largeUnmodifiedFieldA":"x".repeat(120_000),"largeUnmodifiedFieldB":"y".repeat(120_000)});
    let original_metadata = other["metadata"].clone();
    doc["media"].as_array_mut().unwrap().push(other);
    fs::write(
        media_library::library_path(root.path()),
        serde_json::to_vec(&doc).unwrap(),
    )
    .unwrap();
    for n in 0..100 {
        let c = next_claim(claim.clone(), &format!("media-patch-storage-{n:03}"));
        let p = journal.prepare(&c).unwrap();
        assert_eq!(
            count(
                &journal,
                "SELECT count(*) FROM media_image_manifests WHERE phase='SEALED'"
            ),
            1
        );
        assert_eq!(
            journal.publish(&c, &p.digest, &key).unwrap().http_status,
            200
        );
        assert_eq!(fs::read_dir(&journal.image_root).unwrap().count(), 1);
    }
    journal.sweep_images_best_effort();
    assert_eq!(fs::read_dir(&journal.image_root).unwrap().count(), 0);
    assert_eq!(
        count(
            &journal,
            "SELECT count(*) FROM media_image_manifests WHERE phase='COLLECTED'"
        ),
        100
    );
    assert_eq!(
        count(&journal, "SELECT count(*) FROM media_operations"),
        100
    );
    assert!(
        count(
            &journal,
            "SELECT sum(length(before_bytes)+length(after_bytes)) FROM media_operations"
        ) < 10_000
    );
    assert!(
        fs::metadata(root.path().join("state/rust-worker/media-effects.sqlite3"))
            .unwrap()
            .len()
            < 2 * 1024 * 1024
    );
    let current: serde_json::Value =
        serde_json::from_slice(&read_library(root.path()).unwrap()).unwrap();
    assert_eq!(current["media"][1]["metadata"], original_metadata);
}

#[test]
fn exact_legacy_schema_rows_migrate_without_rewriting_and_recover_each_durable_phase() {
    for phase in ["PREPARED", "APPLYING", "APPLIED"] {
        let (root, key, mut original, claim) = fixture();
        let p = original.prepare(&claim).unwrap();
        let row = original.retained("media-patch-one").unwrap().unwrap();
        let before = row.before.bytes_for_test();
        let after = row.after.as_ref().unwrap().bytes_for_test();
        let outcome = if phase == "APPLIED" {
            Some(
                original
                    .publish(&claim, &p.digest, &key)
                    .unwrap()
                    .encode_to_vec(),
            )
        } else {
            if phase == "APPLYING" {
                media_library::publish(root.path(), &after, None).unwrap();
            }
            None
        };
        let state = root.path().join("legacy-state");
        fs::create_dir_all(state.join("rust-worker")).unwrap();
        let old = Connection::open(state.join("rust-worker/media-effects.sqlite3")).unwrap();
        for statement in SCHEMA {
            old.execute(statement, []).unwrap();
        }
        old.pragma_update(None, "application_id", APPLICATION_ID)
            .unwrap();
        old.pragma_update(None, "user_version", 1).unwrap();
        old.execute(
            "INSERT INTO media_identity VALUES(1,?1,?2,'fleet-a','test')",
            params![
                fs::canonicalize(root.path()).unwrap().to_str().unwrap(),
                key.public_key()
            ],
        )
        .unwrap();
        old.execute(
            "INSERT INTO media_operations VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
            params![
                "media-patch-one",
                row.claim,
                row.digest,
                before,
                after,
                row.planned.encode_to_vec(),
                phase,
                outcome
            ],
        )
        .unwrap();
        drop(old);
        let mut journal =
            MediaJournal::open(root.path(), &state, &key.public_key(), "fleet-a", "test").unwrap();
        assert_eq!(count(&journal, "PRAGMA user_version"), 2);
        assert_eq!(
            count(&journal, "SELECT count(*) FROM media_image_manifests"),
            0
        );
        let retained = journal.retained("media-patch-one").unwrap().unwrap();
        assert!(!retained.external_images);
        assert_eq!(retained.claim, row.claim);
        assert_eq!(retained.digest, row.digest);
        assert_eq!(retained.before.bytes_for_test(), before);
        assert_eq!(retained.after.as_ref().unwrap().bytes_for_test(), after);
        let result = journal
            .query("media-patch-one", claim.fence.as_ref().unwrap(), true, &key)
            .unwrap()
            .unwrap();
        assert_eq!(
            result.state,
            if phase == "PREPARED" {
                MediaPatchEffectState::Refused as i32
            } else {
                MediaPatchEffectState::Applied as i32
            }
        );
        assert_eq!(journal.publish(&claim, &p.digest, &key).unwrap(), result);
        assert_eq!(
            read_library(root.path()).unwrap(),
            if phase == "PREPARED" { before } else { after }
        );
    }
}
