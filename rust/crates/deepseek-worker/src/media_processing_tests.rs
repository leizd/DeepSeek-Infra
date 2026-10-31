use super::*;
use deepseek_protocol::generated::deepseek::media::v1::{MediaValue, media_value::Kind};

#[test]
fn media_processing_create_publishes_six_kinds_with_real_segments_index_and_retained_result() {
    let payloads: Vec<Value> = serde_json::from_str(include_str!(
        "../../../../tests/native/media_processing_reference_v1.json"
    ))
    .unwrap();
    assert_eq!(payloads.len(), 6);
    for payload in payloads {
        let (root, key, mut journal, claim) = fixture();
        let claim = create_claim(&journal, claim, payload);
        let before = read_library(root.path()).unwrap();
        let prepared = journal.prepare(&claim).unwrap();
        assert_eq!(read_library(root.path()).unwrap(), before);
        assert!(!root.path().join(".media/segments").exists());
        assert!(!root.path().join(".local-rag").exists());
        let outcome = journal.publish(&claim, &prepared.digest, &key).unwrap();
        assert_eq!(outcome.http_status, 200);
        assert_eq!(outcome.state, MediaPatchEffectState::Applied as i32);
        let retained =
            crate::media_create::retained_preparation(journal.creation_staging(), &claim, &outcome)
                .unwrap();
        let effect = retained.processing.as_ref().unwrap();
        let record = crate::media_values::to_json(&MediaValue {
            kind: Some(Kind::ObjectValue(retained.record.as_ref().unwrap().clone())),
        })
        .unwrap();
        assert_eq!(record["status"], "ready");
        let id = &claim.intent.as_ref().unwrap().media_id;
        let segments = root
            .path()
            .join(".media/segments")
            .join(format!("{id}.json"));
        let bytes = fs::read(&segments).unwrap();
        let document: Value = serde_json::from_slice(&bytes).unwrap();
        assert!(!document["segments"].as_array().unwrap().is_empty());
        assert_eq!(
            document["segments"].as_array().unwrap().len(),
            record["metadata"]["segmentCount"].as_u64().unwrap() as usize
        );
        if effect.index_enabled {
            let connection = Connection::open_with_flags(
                deepseek_policy::memory_index::local_rag_db(root.path()),
                OpenFlags::SQLITE_OPEN_READ_ONLY,
            )
            .unwrap();
            let count: i64 = connection
                .query_row(
                    "SELECT COUNT(*) FROM rag_items WHERE collection='media' AND source_id=?1",
                    [id],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(count as usize, effect.index_items.len());
            assert!(count > 0);
            assert_eq!(
                count as u64,
                record["metadata"]["indexedChunkCount"].as_u64().unwrap()
            );
        }
        let after = read_library(root.path()).unwrap();
        let modified = fs::metadata(&segments).unwrap().modified().unwrap();
        assert_eq!(
            journal
                .query(
                    &outcome.operation_id,
                    claim.fence.as_ref().unwrap(),
                    true,
                    &key
                )
                .unwrap(),
            Some(outcome.clone())
        );
        assert_eq!(fs::read(&segments).unwrap(), bytes);
        assert_eq!(
            fs::metadata(&segments).unwrap().modified().unwrap(),
            modified
        );
        assert_eq!(read_library(root.path()).unwrap(), after);
        assert!(journal.active().unwrap().is_none());
    }
}

#[test]
fn media_processing_failed_create_retains_failed_record_and_source_as_applied_effect() {
    let (root, key, mut journal, claim) = fixture();
    let claim = create_claim(
        &journal,
        claim,
        json!({"type":"pdf","text":"not a PDF","process":true}),
    );
    let prepared = journal.prepare(&claim).unwrap();
    let outcome = journal.publish(&claim, &prepared.digest, &key).unwrap();
    assert_eq!(outcome.state, MediaPatchEffectState::Applied as i32);
    assert_eq!(outcome.http_status, 422);
    assert_eq!(outcome.error_code, "invalid_payload");
    assert_eq!(
        outcome.error_message,
        "Could not extract text from this PDF"
    );
    assert!(outcome.created.is_some());
    assert_eq!(outcome.signature.len(), 64);
    let mut unknown = outcome.clone();
    unknown.signature.clear();
    unknown.http_status = 503;
    unknown.error_code = "EFFECT_UNKNOWN".into();
    assert!(key.sign_media_patch_outcome(&unknown).is_err());
    let record = crate::media_values::to_json(&MediaValue {
        kind: Some(Kind::ObjectValue(journal.created_record(&claim).unwrap())),
    })
    .unwrap();
    assert_eq!(record["status"], "failed");
    assert_eq!(record["metadata"]["error"], outcome.error_message);
    let source = root
        .path()
        .join(".media/objects/media_0123456789abcdef/source.txt");
    assert_eq!(fs::read(&source).unwrap(), b"not a PDF");
    assert!(
        !root
            .path()
            .join(".media/segments/media_0123456789abcdef.json")
            .exists()
    );
    assert!(!root.path().join(".local-rag").exists());
    assert!(journal.active().unwrap().is_none());
    assert_eq!(
        journal
            .query(
                &outcome.operation_id,
                claim.fence.as_ref().unwrap(),
                true,
                &key
            )
            .unwrap(),
        Some(outcome)
    );
}

#[test]
fn media_processing_source_conflict_keeps_processing_until_original_reconciliation() {
    let (root, key, mut journal, claim) = fixture();
    let claim = create_claim(
        &journal,
        claim,
        json!({"type":"webpage","html":"<p>neptune original</p>","process":true}),
    );
    let prepared = journal.prepare(&claim).unwrap();
    let source = root
        .path()
        .join(".media/objects/media_0123456789abcdef/source.html");
    fs::create_dir_all(&source).unwrap();
    assert!(journal.publish(&claim, &prepared.digest, &key).is_err());
    let document: Value = serde_json::from_slice(&read_library(root.path()).unwrap()).unwrap();
    assert_eq!(
        document["media"].as_array().unwrap().last().unwrap()["status"],
        "processing"
    );
    let row = journal
        .retained(&claim.intent.as_ref().unwrap().operation_id)
        .unwrap()
        .unwrap();
    assert_eq!(row.state, "APPLYING");
    assert!(row.outcome.is_none());
    assert!(!root.path().join(".local-rag").exists());
    fs::remove_dir(&source).unwrap();
    let outcome = journal
        .query(
            &claim.intent.as_ref().unwrap().operation_id,
            claim.fence.as_ref().unwrap(),
            true,
            &key,
        )
        .unwrap()
        .unwrap();
    assert_eq!(outcome.original_fence, claim.fence);
    assert_eq!(outcome.request_sha256, claim.request_sha256);
    assert_eq!(fs::read(source).unwrap(), b"<p>neptune original</p>");
    assert_eq!(outcome.http_status, 200);
    assert!(journal.active().unwrap().is_none());
}

#[test]
fn media_processing_lost_terminal_commit_confirms_original_files_and_index_without_republication() {
    let (root, key, mut journal, claim) = fixture();
    let claim = create_claim(
        &journal,
        claim,
        json!({"type":"webpage","html":"<p>neptune original</p>","process":true}),
    );
    let prepared = journal.prepare(&claim).unwrap();
    journal.connection.execute_batch("CREATE TRIGGER fail_processing_terminal BEFORE UPDATE ON media_operations WHEN NEW.state='APPLIED' BEGIN SELECT RAISE(ABORT,'controlled receipt loss'); END").unwrap();
    assert!(journal.publish(&claim, &prepared.digest, &key).is_err());
    let after = read_library(root.path()).unwrap();
    let source = root
        .path()
        .join(".media/objects/media_0123456789abcdef/source.html");
    let segments = root
        .path()
        .join(".media/segments/media_0123456789abcdef.json");
    let modified = fs::metadata(&source).unwrap().modified().unwrap();
    let segment_modified = fs::metadata(&segments).unwrap().modified().unwrap();
    let segment_bytes = fs::read(&segments).unwrap();
    journal
        .connection
        .execute_batch("DROP TRIGGER fail_processing_terminal")
        .unwrap();
    drop(journal);
    let mut journal = MediaJournal::open(
        root.path(),
        &root.path().join("state"),
        &key.public_key(),
        "fleet-a",
        "test",
    )
    .unwrap();
    let operation = &claim.intent.as_ref().unwrap().operation_id;
    let outcome = journal
        .query(operation, claim.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(outcome.original_fence, claim.fence);
    assert_eq!(outcome.request_sha256, claim.request_sha256);
    assert_eq!(read_library(root.path()).unwrap(), after);
    assert_eq!(fs::metadata(source).unwrap().modified().unwrap(), modified);
    assert_eq!(
        fs::metadata(&segments).unwrap().modified().unwrap(),
        segment_modified
    );
    assert_eq!(fs::read(&segments).unwrap(), segment_bytes);
    assert!(journal.active().unwrap().is_none());
}
