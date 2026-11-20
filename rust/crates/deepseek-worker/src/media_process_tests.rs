//! These local fault states exercise the real journal/files/SQLite/vec0 code.
//! They complement, and do not replace, actual process-kill/HTTP qualification.
use super::*;
#[path = "media_process_scope_tests.rs"]
mod scope;
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaProcessOptions, MediaValue, media_value::Kind,
};

fn process_claim(mut claim: MediaPatchClaim, force: bool, nonce: char) -> MediaPatchClaim {
    let intent = claim.intent.as_mut().unwrap();
    intent.kind = 3;
    intent.patch = None;
    intent.create = None;
    intent.operation_id = format!("media-process-{}", nonce.to_string().repeat(32));
    intent.process = Some(MediaProcessOptions {
        force,
        ocr_enabled: Some(false),
        correlation_id: "unit.original.request".into(),
    });
    claim.fence.as_mut().unwrap().action_id = intent.operation_id.clone();
    claim.request_sha256 = sha(&intent.encode_to_vec());
    claim
}

fn ready(
    payload: Value,
) -> (
    tempfile::TempDir,
    ControlSigningKey,
    MediaJournal,
    MediaPatchClaim,
) {
    let (root, key, mut journal, claim) = fixture();
    let claim = create_claim(&journal, claim, payload);
    let p = journal.prepare(&claim).unwrap();
    let result = journal.publish(&claim, &p.digest, &key).unwrap();
    assert_eq!(result.http_status, 200);
    (root, key, journal, claim)
}

fn segments_path(root: &Path, id: &str) -> PathBuf {
    root.join(".media/segments").join(format!("{id}.json"))
}
fn record(object: &deepseek_protocol::generated::deepseek::media::v1::MediaObject) -> Value {
    crate::media_values::to_json(&MediaValue {
        kind: Some(Kind::ObjectValue(object.clone())),
    })
    .unwrap()
}
fn segment_value(array: &deepseek_protocol::generated::deepseek::media::v1::MediaArray) -> Value {
    crate::media_values::to_json(&MediaValue {
        kind: Some(Kind::ArrayValue(array.clone())),
    })
    .unwrap()
}
fn count(root: &Path, id: &str) -> i64 {
    Connection::open(deepseek_policy::memory_index::local_rag_db(root))
        .unwrap()
        .query_row(
            "SELECT COUNT(*) FROM rag_items WHERE collection='media' AND source_id=?1",
            [id],
            |r| r.get(0),
        )
        .unwrap()
}

#[test]
fn media_process_replaces_six_kinds_without_recreating_source_and_retains_historical_results() {
    let payloads: Vec<Value> = serde_json::from_str(include_str!(
        "../../../../tests/native/media_processing_reference_v1.json"
    ))
    .unwrap();
    assert_eq!(payloads.len(), 6);
    for payload in payloads {
        let (root, key, mut journal, created) = ready(payload);
        let original = journal.created_record(&created).unwrap();
        let public = record(&original);
        let id = public["mediaId"].as_str().unwrap();
        let source = root
            .path()
            .join(".media")
            .join(public["path"].as_str().unwrap());
        let before_source = source.is_file().then(|| {
            (
                fs::read(&source).unwrap(),
                fs::metadata(&source).unwrap().modified().unwrap(),
                fs::metadata(&source).unwrap().permissions(),
            )
        });
        if let Some((_, _, permissions)) = &before_source {
            let mut read_only = permissions.clone();
            read_only.set_readonly(true);
            fs::set_permissions(&source, read_only).unwrap();
        }
        for (force, nonce) in [(false, 'a'), (true, 'b')] {
            let claim = process_claim(created.clone(), force, nonce);
            let operation = &claim.intent.as_ref().unwrap().operation_id;
            let old = fs::read(segments_path(root.path(), id)).unwrap();
            let before = read_library(root.path()).unwrap();
            let old_count = count(root.path(), id);
            let prepared = journal.prepare(&claim).unwrap();
            assert_eq!(read_library(root.path()).unwrap(), before);
            assert_eq!(fs::read(segments_path(root.path(), id)).unwrap(), old);
            assert_eq!(count(root.path(), id), old_count);
            let planned = journal.retained(operation).unwrap().unwrap().planned;
            assert!(planned.created.is_none());
            assert!(planned.processed.is_some());
            assert!(planned.encoded_len() < 1024);
            let bound =
                crate::media_process::retained(journal.creation_staging(), &claim, &planned)
                    .unwrap();
            assert_eq!(bound.before_segments, old);
            assert!(bound.before_segments_present);
            assert!(!bound.before_index_items.is_empty());
            assert!(bound.effects.as_ref().unwrap().source_bytes.is_empty());
            let result = journal.publish(&claim, &prepared.digest, &key).unwrap();
            assert_eq!(result.http_status, 200);
            assert_eq!(result.signature.len(), 64);
            assert!(result.created.is_none());
            assert_eq!(result.original_fence, claim.fence);
            let response = journal.processed_result(&claim).unwrap();
            let media = record(response.media.as_ref().unwrap());
            let segments = segment_value(response.segments.as_ref().unwrap());
            assert_eq!(media["mediaId"], public["mediaId"]);
            assert_eq!(media["path"], public["path"]);
            assert_eq!(media["status"], "ready");
            assert_eq!(media["createdAt"], public["createdAt"]);
            assert!(!segments.as_array().unwrap().is_empty());
            assert_eq!(count(root.path(), id), response.indexed as i64);
            assert_eq!(
                segments.as_array().unwrap().len(),
                media["metadata"]["segmentCount"].as_u64().unwrap() as usize
            );
            assert_ne!(
                fs::read(segments_path(root.path(), id)).unwrap(),
                old,
                "new processing must generate fresh segment identities"
            );
            assert_eq!(
                journal.created_record(&created).unwrap(),
                original,
                "historical CREATE result was overwritten by PROCESS"
            );
            let final_bytes = read_library(root.path()).unwrap();
            let segment_bytes = fs::read(segments_path(root.path(), id)).unwrap();
            let modified = fs::metadata(segments_path(root.path(), id))
                .unwrap()
                .modified()
                .unwrap();
            drop(journal);
            journal = MediaJournal::open(
                root.path(),
                &root.path().join("state"),
                &key.public_key(),
                "fleet-a",
                "test",
            )
            .unwrap();
            assert_eq!(
                journal
                    .query(operation, claim.fence.as_ref().unwrap(), true, &key)
                    .unwrap(),
                Some(result.clone())
            );
            assert_eq!(journal.processed_result(&claim).unwrap(), response);
            assert_eq!(read_library(root.path()).unwrap(), final_bytes);
            assert_eq!(
                fs::read(segments_path(root.path(), id)).unwrap(),
                segment_bytes
            );
            assert_eq!(
                fs::metadata(segments_path(root.path(), id))
                    .unwrap()
                    .modified()
                    .unwrap(),
                modified
            );
            assert!(journal.active().unwrap().is_none());
            let mut unknown = result.clone();
            unknown.signature.clear();
            unknown.http_status = 503;
            unknown.error_code = "EFFECT_UNKNOWN".into();
            unknown.error_message = "uncertain".into();
            assert!(key.sign_media_patch_outcome(&unknown).is_err());
            let mut mixed = result;
            mixed.signature.clear();
            mixed.created = Some(Default::default());
            assert!(key.sign_media_patch_outcome(&mixed).is_err());
        }
        if let Some((bytes, modified, permissions)) = before_source {
            assert_eq!(fs::read(&source).unwrap(), bytes);
            assert_eq!(fs::metadata(&source).unwrap().modified().unwrap(), modified);
            fs::set_permissions(&source, permissions).unwrap();
        }
    }
}

#[test]
fn media_process_failed_pdf_preserves_or_force_clears_prior_effects_without_false_refusal() {
    let (root, key, mut journal, created) = ready(
        json!({"type":"pdf","text":"not a PDF","metadata":{"pageTexts":[{"page":1,"text":"previous valid imported page"}]},"process":true}),
    );
    let id = created.intent.as_ref().unwrap().media_id.clone();
    let path = segments_path(root.path(), &id);
    let original = fs::read(&path).unwrap();
    let original_count = count(root.path(), &id);
    assert!(original_count > 0);
    let mut library: Value = serde_json::from_slice(&read_library(root.path()).unwrap()).unwrap();
    let media = library["media"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|m| m["mediaId"] == id)
        .unwrap();
    media["metadata"]
        .as_object_mut()
        .unwrap()
        .remove("pageTexts");
    fs::write(
        media_library::library_path(root.path()),
        serde_json::to_vec_pretty(&library).unwrap(),
    )
    .unwrap();
    for (force, nonce) in [(false, 'a'), (true, 'b')] {
        let claim = process_claim(created.clone(), force, nonce);
        let p = journal.prepare(&claim).unwrap();
        let result = journal.publish(&claim, &p.digest, &key).unwrap();
        assert_eq!(result.state, MediaPatchEffectState::Applied as i32);
        assert_eq!(result.http_status, 422);
        assert_eq!(result.error_code, "invalid_payload");
        assert!(result.processed.is_some());
        assert!(result.created.is_none());
        assert_eq!(result.signature.len(), 64);
        let response = journal.processed_result(&claim).unwrap();
        let failed = record(response.media.as_ref().unwrap());
        assert_eq!(failed["status"], "failed");
        assert_eq!(failed["metadata"]["error"], result.error_message);
        assert_eq!(
            failed["metadata"]["segmentCount"], 1,
            "the original failure keeps prior count metadata, even after force clears its files"
        );
        if force {
            let document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            assert_eq!(document["segments"], json!([]));
            assert_eq!(count(root.path(), &id), 0);
        } else {
            assert_eq!(fs::read(&path).unwrap(), original);
            assert_eq!(count(root.path(), &id), original_count);
        }
        assert_eq!(
            fs::read(
                root.path()
                    .join(".media/objects")
                    .join(&id)
                    .join("source.txt")
            )
            .unwrap(),
            b"not a PDF"
        );
        assert_eq!(
            journal
                .query(
                    &result.operation_id,
                    claim.fence.as_ref().unwrap(),
                    true,
                    &key
                )
                .unwrap(),
            Some(result)
        );
        assert!(journal.active().unwrap().is_none());
    }
    let mut missing = process_claim(created, false, 'c');
    missing.intent.as_mut().unwrap().media_id = "media_missing".into();
    missing.request_sha256 = sha(&missing.intent.as_ref().unwrap().encode_to_vec());
    let before = read_library(root.path()).unwrap();
    let p = journal.prepare(&missing).unwrap();
    let refused = journal.publish(&missing, &p.digest, &key).unwrap();
    assert_eq!(refused.http_status, 404);
    assert_eq!(refused.state, MediaPatchEffectState::Refused as i32);
    assert!(refused.processed.is_none());
    assert_eq!(read_library(root.path()).unwrap(), before);
}

#[test]
fn media_process_conflicts_stay_unknown_and_never_recreate_sources_or_overwrite_foreign_effects() {
    for conflict in ["source", "segments", "index", "no-effect-witness"] {
        let (root, key, mut journal, created) = ready(
            json!({"type":"webpage","html":"<h1>Original processing source</h1>","process":true}),
        );
        let id = created.intent.as_ref().unwrap().media_id.clone();
        let claim = process_claim(created, true, 'a');
        let operation = &claim.intent.as_ref().unwrap().operation_id;
        let source = root
            .path()
            .join(".media/objects")
            .join(&id)
            .join("source.html");
        let source_bytes = fs::read(&source).unwrap();
        let segment = segments_path(root.path(), &id);
        let segment_bytes = fs::read(&segment).unwrap();
        let p = journal.prepare(&claim).unwrap();
        let db = deepseek_policy::memory_index::local_rag_db(root.path());
        match conflict {
            "source" => fs::write(&source, b"foreign source").unwrap(),
            "segments" => fs::write(&segment, b"foreign segments").unwrap(),
            "index" => {
                Connection::open(&db).unwrap().execute("UPDATE rag_items SET text='foreign index' WHERE collection='media' AND source_id=?1",[&id]).unwrap();
            }
            _ => {
                journal
                    .connection
                    .execute(
                        "UPDATE media_operations SET state='APPLYING' WHERE operation_id=?1",
                        [operation],
                    )
                    .unwrap();
            }
        }
        if conflict != "no-effect-witness" {
            assert_eq!(
                journal
                    .publish(&claim, &p.digest, &key)
                    .unwrap_err()
                    .message(),
                "EFFECT_UNKNOWN"
            );
        }
        assert_eq!(
            journal
                .query(operation, claim.fence.as_ref().unwrap(), true, &key)
                .unwrap_err()
                .message(),
            "EFFECT_UNKNOWN"
        );
        assert!(journal.active().unwrap().is_some());
        if conflict == "source" {
            assert_eq!(fs::read(&source).unwrap(), b"foreign source");
            fs::write(&source, &source_bytes).unwrap();
        } else {
            assert_eq!(fs::read(&source).unwrap(), source_bytes);
        }
        if conflict == "segments" {
            assert_eq!(fs::read(&segment).unwrap(), b"foreign segments");
            fs::write(&segment, &segment_bytes).unwrap();
        }
        if conflict == "index" {
            let row = journal.retained(operation).unwrap().unwrap();
            let original =
                crate::media_process::retained(journal.creation_staging(), &claim, &row.planned)
                    .unwrap();
            for row in &original.before_index_items {
                let item = row.record.as_ref().unwrap();
                Connection::open(&db)
                    .unwrap()
                    .execute(
                        "UPDATE rag_items SET text=?1 WHERE item_id=?2",
                        params![item.text, item.item_id],
                    )
                    .unwrap();
            }
        }
        if conflict != "no-effect-witness" {
            let result = journal
                .query(operation, claim.fence.as_ref().unwrap(), true, &key)
                .unwrap()
                .unwrap();
            assert_eq!(result.http_status, 200);
            assert_eq!(result.original_fence, claim.fence);
            assert!(journal.active().unwrap().is_none());
        } else {
            assert_eq!(fs::read(&segment).unwrap(), segment_bytes);
            assert!(
                journal.active().unwrap().is_some(),
                "absence must not manufacture another publication"
            );
        }
    }
}

#[test]
fn media_process_recovers_each_original_local_phase_and_lost_terminal_commit_without_reprocessing()
{
    for phase in [
        "processing",
        "cleared",
        "segments",
        "indexed",
        "terminal-commit",
    ] {
        let (root, key, mut journal, created) =
            ready(json!({"type":"webpage","html":"<h1>phase recovery</h1>","process":true}));
        let id = created.intent.as_ref().unwrap().media_id.clone();
        let claim = process_claim(created, true, 'a');
        let operation = &claim.intent.as_ref().unwrap().operation_id;
        let p = journal.prepare(&claim).unwrap();
        let row = journal.retained(operation).unwrap().unwrap();
        let prepared =
            crate::media_process::retained(journal.creation_staging(), &claim, &row.planned)
                .unwrap();
        let effect = prepared
            .effects
            .as_ref()
            .unwrap()
            .processing
            .as_ref()
            .unwrap();
        let expected_segments = serde_json::to_vec_pretty(
            &json!({"segments":segment_value(effect.segments.as_ref().unwrap())}),
        )
        .unwrap();
        if phase == "terminal-commit" {
            journal.connection.execute_batch("CREATE TRIGGER process_terminal_failure BEFORE UPDATE ON media_operations WHEN NEW.state='APPLIED' BEGIN SELECT RAISE(ABORT,'injected lost terminal commit'); END;").unwrap();
            assert!(journal.publish(&claim, &p.digest, &key).is_err());
            assert_eq!(
                read_library(root.path()).unwrap(),
                *row.after.as_ref().unwrap()
            );
            journal
                .connection
                .execute_batch("DROP TRIGGER process_terminal_failure;")
                .unwrap();
        } else {
            journal
                .connection
                .execute(
                    "UPDATE media_operations SET state='APPLYING' WHERE operation_id=?1",
                    [operation],
                )
                .unwrap();
            let processing =
                crate::media_processing::intermediate(row.after.as_ref().unwrap(), effect, &id)
                    .unwrap();
            assert!(media_library::publish(root.path(), &processing, None).unwrap());
            if phase != "processing" {
                crate::media_process_index::finish(
                    root.path(),
                    &claim,
                    &row.planned,
                    &prepared,
                    crate::media_process_index::Phase::Clear,
                )
                .unwrap();
                fs::write(segments_path(root.path(), &id), b"{\n  \"segments\": []\n}").unwrap();
            }
            if phase == "segments" || phase == "indexed" {
                fs::write(segments_path(root.path(), &id), &expected_segments).unwrap();
            }
            if phase == "indexed" {
                crate::media_process_index::finish(
                    root.path(),
                    &claim,
                    &row.planned,
                    &prepared,
                    crate::media_process_index::Phase::Final,
                )
                .unwrap();
            }
        }
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
            .query(operation, claim.fence.as_ref().unwrap(), true, &key)
            .unwrap()
            .unwrap();
        assert_eq!(result.http_status, 200);
        assert_eq!(result.original_fence, claim.fence);
        assert_eq!(result.processed, row.planned.processed);
        assert_eq!(
            fs::read(segments_path(root.path(), &id)).unwrap(),
            expected_segments
        );
        assert_eq!(count(root.path(), &id), effect.index_items.len() as i64);
        assert!(journal.active().unwrap().is_none());
        let after = read_library(root.path()).unwrap();
        let modified = fs::metadata(segments_path(root.path(), &id))
            .unwrap()
            .modified()
            .unwrap();
        assert_eq!(
            journal
                .query(operation, claim.fence.as_ref().unwrap(), true, &key)
                .unwrap(),
            Some(result)
        );
        assert_eq!(read_library(root.path()).unwrap(), after);
        assert_eq!(
            fs::metadata(segments_path(root.path(), &id))
                .unwrap()
                .modified()
                .unwrap(),
            modified
        );
    }
}

#[test]
fn media_process_starts_from_pending_registration_and_binds_present_empty_segment_result() {
    for (payload, empty) in [
        (
            json!({"type":"webpage","html":"<h1>first processing</h1>","process":false}),
            false,
        ),
        (json!({"type":"audio","metadata":{},"process":false}), true),
    ] {
        let (root, key, mut journal, claim) = fixture();
        let created = create_claim(&journal, claim, payload);
        let p = journal.prepare(&created).unwrap();
        let creation = journal.publish(&created, &p.digest, &key).unwrap();
        assert_eq!(creation.http_status, 200);
        let pending = record(&journal.created_record(&created).unwrap());
        assert_eq!(pending["status"], "pending");
        let id = created.intent.as_ref().unwrap().media_id.clone();
        assert!(!segments_path(root.path(), &id).exists());
        assert!(!deepseek_policy::memory_index::local_rag_db(root.path()).exists());
        let claim = process_claim(created, false, 'a');
        let prepared = journal.prepare(&claim).unwrap();
        let result = journal.publish(&claim, &prepared.digest, &key).unwrap();
        assert_eq!(result.http_status, 200);
        let response = journal.processed_result(&claim).unwrap();
        assert_eq!(record(response.media.as_ref().unwrap())["status"], "ready");
        let segments = response
            .segments
            .as_ref()
            .expect("even an empty result must remain present");
        if empty {
            assert!(segments.values.is_empty());
            let bound = result.processed.as_ref().unwrap();
            assert_eq!(bound.segments_bytes, 0);
            assert_eq!(bound.segments_sha256, sha(&[]));
            assert_eq!(bound.indexed, 0);
            assert_eq!(count(root.path(), &id), 0);
        } else {
            assert_eq!(segments.values.len(), 1);
            assert_eq!(response.indexed, 1);
            assert_eq!(count(root.path(), &id), 1);
        }
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
                .query(
                    &result.operation_id,
                    claim.fence.as_ref().unwrap(),
                    true,
                    &key
                )
                .unwrap(),
            Some(result)
        );
        assert_eq!(journal.processed_result(&claim).unwrap(), response);
    }
}
