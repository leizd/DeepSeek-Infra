use super::*;
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaFlatPatchRecord, MediaObject, media_flat_patch_node::Kind,
};
use serde_json::{Value, json};

fn fixture() -> (
    tempfile::TempDir,
    ControlSigningKey,
    MediaJournal,
    MediaPatchClaim,
) {
    let (root, key, journal, mut claim) = super::tests::fixture();
    let intent = claim.intent.as_mut().unwrap();
    intent.max_outcome_schema_version = 2;
    claim.request_sha256 = sha(&intent.encode_to_vec());
    let path = media_library::library_path(root.path());
    let mut document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    let metadata: serde_json::Map<String, Value> = (0..36)
        .map(|n| (format!("field_{n:02}"), Value::String("m".repeat(120_000))))
        .collect();
    document["media"][0]["metadata"] = Value::Object(metadata);
    fs::write(path, serde_json::to_vec(&document).unwrap()).unwrap();
    (root, key, journal, claim)
}

#[test]
fn flat_patch_preserves_the_deep_original_result_and_codec_binding_after_restart() {
    let (root, key, mut journal, mut claim) = super::tests::fixture();
    let intent = claim.intent.as_mut().unwrap();
    intent.max_outcome_schema_version = patch_wire::FLAT_VERSION;
    claim.request_sha256 = sha(&intent.encode_to_vec());
    let deep = format!(
        "{}{}{}",
        r#"{"next":"#.repeat(160),
        r#"{"last":1}"#,
        "}".repeat(160)
    );
    let before = format!(
        r#"{{"media":[{{"mediaId":"media_one","type":"pdf","metadata":{{"value":{deep}}}}}]}}"#
    );
    fs::write(media_library::library_path(root.path()), &before).unwrap();
    let prepared = journal.prepare(&claim).unwrap();
    let operation = &claim.intent.as_ref().unwrap().operation_id;
    let planned = journal.retained(operation).unwrap().unwrap().planned;
    assert_eq!(planned.schema_version, patch_wire::FLAT_VERSION);
    assert_eq!(prepared.digest, sha(&planned.encode_to_vec()));
    assert_eq!(
        read_library(root.path()).unwrap().as_slice(),
        before.as_bytes()
    );
    let applied = journal.publish(&claim, &prepared.digest, &key).unwrap();
    assert_eq!(applied.state, MediaPatchEffectState::Applied as i32);
    assert_eq!(applied.http_status, 200);
    assert_eq!(applied.signature.len(), 64);
    assert!(applied.media.is_none());
    let after = read_library(root.path()).unwrap();
    assert!(std::str::from_utf8(&after).unwrap().contains(deep.as_str()));
    assert_eq!(applied.after_sha256, sha(&after));
    drop(journal);

    let journal = MediaJournal::open(
        root.path(),
        &root.path().join("state"),
        &key.public_key(),
        "fleet-a",
        "test",
    )
    .unwrap();
    let (manifest, mut file) = journal.patched_record(&claim).unwrap();
    assert_eq!(applied.patched.as_ref(), Some(&manifest));
    assert!(
        crate::media_patch_result::open_record(
            journal.creation_staging(),
            &claim,
            patch_wire::COMPACT_VERSION,
            &manifest,
        )
        .is_err()
    );
    let mut encoded = Vec::new();
    file.read_to_end(&mut encoded).unwrap();
    assert_eq!(manifest.record_bytes, encoded.len() as u64);
    assert_eq!(manifest.record_sha256, sha(&encoded));
    let record = MediaFlatPatchRecord::decode(encoded.as_slice()).unwrap();
    let field = |index: u32, name: &str| {
        let Some(Kind::ObjectValue(object)) = &record.nodes[(index - 1) as usize].kind else {
            panic!("flat result field parent is not an object");
        };
        object
            .members
            .iter()
            .find(|member| member.key == name)
            .unwrap()
            .child
    };
    let metadata = field(record.nodes.len() as u32, "metadata");
    let mut value = field(metadata, "value");
    for _ in 0..160 {
        value = field(value, "next");
    }
    let last = field(value, "last");
    assert_eq!(
        record.nodes[(last - 1) as usize].kind,
        Some(Kind::NumberValue("1".into()))
    );
    assert_eq!(read_library(root.path()).unwrap(), after);
}

#[test]
fn compact_patch_keeps_original_record_outside_retention_and_after_restart() {
    let (root, key, mut journal, claim) = fixture();
    let path = media_library::library_path(root.path());
    let mut document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    for n in 0..1000 {
        document["media"].as_array_mut().unwrap().push(json!({
            "mediaId":format!("media_tail_{n:04}"),"type":"image","metadata":{},
            "createdAt":"2026-01-01T00:00:00+00:00","updatedAt":"2026-01-01T00:00:00+00:00"
        }));
    }
    fs::write(&path, serde_json::to_vec(&document).unwrap()).unwrap();
    let prepared = journal.prepare(&claim).unwrap();
    let operation = &claim.intent.as_ref().unwrap().operation_id;
    let row = journal.retained(operation).unwrap().unwrap();
    assert_eq!(row.planned.schema_version, 2);
    assert!(row.planned.encoded_len() < 1024);
    let manifest = row.planned.patched.as_ref().unwrap();
    assert!(manifest.record_bytes > 4 * 1024 * 1024);
    assert_ne!(manifest.preparation_sha256, prepared.digest);
    assert_eq!(prepared.digest, sha(&row.planned.encode_to_vec()));
    let applied = journal.publish(&claim, &prepared.digest, &key).unwrap();
    assert_eq!(applied.schema_version, 2);
    assert_eq!(applied.signature.len(), 64);
    assert!(applied.media.is_none());
    let after = read_library(root.path()).unwrap();
    let retained: Value = serde_json::from_slice(&after).unwrap();
    assert_eq!(retained["media"].as_array().unwrap().len(), 1000);
    assert!(
        retained["media"]
            .as_array()
            .unwrap()
            .iter()
            .all(|r| r["mediaId"] != "media_one")
    );
    // A later small PATCH retains the legacy v1 receipt in the same journal.
    let mut second = claim.clone();
    let intent = second.intent.as_mut().unwrap();
    intent.operation_id = "media-patch-second".into();
    intent.media_id = "media_tail_0999".into();
    second.fence.as_mut().unwrap().action_id = intent.operation_id.clone();
    second.request_sha256 = sha(&intent.encode_to_vec());
    let next = journal.prepare(&second).unwrap();
    let small = journal.publish(&second, &next.digest, &key).unwrap();
    assert_eq!(small.schema_version, 1);
    assert!(small.patched.is_none());
    assert_eq!(
        journal
            .query(
                &claim.intent.as_ref().unwrap().operation_id,
                claim.fence.as_ref().unwrap(),
                true,
                &key
            )
            .unwrap(),
        Some(applied.clone())
    );
    let after = read_library(root.path()).unwrap();
    drop(journal);
    let journal = MediaJournal::open(
        root.path(),
        &root.path().join("state"),
        &key.public_key(),
        "fleet-a",
        "test",
    )
    .unwrap();
    let (receipt, mut file) = journal.patched_record(&claim).unwrap();
    assert_eq!(applied.patched.as_ref(), Some(&receipt));
    let mut encoded = Vec::new();
    file.read_to_end(&mut encoded).unwrap();
    let record = MediaObject::decode(encoded.as_slice()).unwrap();
    let public = crate::media_values::to_json(
        &deepseek_protocol::generated::deepseek::media::v1::MediaValue {
            kind: Some(
                deepseek_protocol::generated::deepseek::media::v1::media_value::Kind::ObjectValue(
                    record,
                ),
            ),
        },
    )
    .unwrap();
    assert_eq!(public["mediaId"], "media_one");
    assert_eq!(public["title"], "After");
    let large = public["metadata"]["field_35"].as_str().unwrap();
    assert_eq!(large.len(), 120_000);
    assert!(large.bytes().all(|b| b == b'm'));
    assert_eq!(sha(&read_library(root.path()).unwrap()), sha(&after));
}

#[test]
fn compact_patch_missing_or_corrupt_preparation_never_publishes_or_reconstructs() {
    let (root, key, mut journal, claim) = fixture();
    let before = read_library(root.path()).unwrap();
    let prepared = journal.prepare(&claim).unwrap();
    let operation = &claim.intent.as_ref().unwrap().operation_id;
    let path =
        crate::media_patch_result::record_path(journal.creation_staging(), operation).unwrap();
    let record = fs::read(&path).unwrap();
    fs::write(&path, b"partial").unwrap();
    assert!(journal.publish(&claim, &prepared.digest, &key).is_err());
    assert_eq!(sha(&read_library(root.path()).unwrap()), sha(&before));
    assert_eq!(
        journal.retained(operation).unwrap().unwrap().state,
        "PREPARED"
    );
    fs::write(&path, &record).unwrap();
    let applied = journal.publish(&claim, &prepared.digest, &key).unwrap();
    let after = read_library(root.path()).unwrap();
    fs::remove_file(&path).unwrap();
    assert!(journal.patched_record(&claim).is_err());
    assert_eq!(
        journal
            .query(operation, claim.fence.as_ref().unwrap(), true, &key)
            .unwrap(),
        Some(applied)
    );
    assert_eq!(sha(&read_library(root.path()).unwrap()), sha(&after));
    // Restoring the exact already-bound receipt bytes enables a read, never a
    // replacement mutation or reconstruction from a later library document.
    fs::write(&path, &record).unwrap();
    assert!(journal.patched_record(&claim).is_ok());
}

#[test]
fn compact_patch_refusals_and_unknown_keep_the_frozen_v1_shape() {
    for case in ["lease", "revision", "prepared"] {
        let (root, key, mut journal, mut claim) = fixture();
        if case == "lease" {
            claim.lease_until_unix = 1;
        }
        let before = read_library(root.path()).unwrap();
        let prepared = journal.prepare(&claim).unwrap();
        let operation = &claim.intent.as_ref().unwrap().operation_id;
        let refused = match case {
            "revision" => {
                fs::write(media_library::library_path(root.path()), b"{}").unwrap();
                journal.publish(&claim, &prepared.digest, &key).unwrap()
            }
            "lease" => journal.publish(&claim, &prepared.digest, &key).unwrap(),
            _ => {
                let unknown = journal
                    .query(operation, claim.fence.as_ref().unwrap(), false, &key)
                    .unwrap()
                    .unwrap();
                assert_eq!(unknown.schema_version, 1);
                assert_eq!(unknown.state, MediaPatchEffectState::Unknown as i32);
                assert!(unknown.patched.is_none());
                journal
                    .query(operation, claim.fence.as_ref().unwrap(), true, &key)
                    .unwrap()
                    .unwrap()
            }
        };
        assert_eq!(refused.schema_version, 1);
        assert_eq!(refused.state, MediaPatchEffectState::Refused as i32);
        assert!(refused.patched.is_none());
        assert_eq!(refused.signature.len(), 64);
        assert_eq!(
            journal
                .query(operation, claim.fence.as_ref().unwrap(), true, &key)
                .unwrap(),
            Some(refused)
        );
        if case != "revision" {
            assert_eq!(sha(&read_library(root.path()).unwrap()), sha(&before));
        }
    }
}

#[test]
fn compact_patch_original_applied_recovery_does_not_require_a_live_lease() {
    let (root, key, mut journal, mut claim) = fixture();
    claim.lease_until_unix = 1;
    journal.prepare(&claim).unwrap();
    let operation = &claim.intent.as_ref().unwrap().operation_id;
    let row = journal.retained(operation).unwrap().unwrap();
    // Actual before/after bytes plus an interrupted original APPLYING state;
    // this core journal fixture is not public Go admission qualification.
    journal
        .connection
        .execute(
            "UPDATE media_operations SET state='APPLYING' WHERE operation_id=?1",
            [operation],
        )
        .unwrap();
    media_library::publish(root.path(), row.after.as_ref().unwrap(), None).unwrap();
    let applied = journal
        .query(operation, claim.fence.as_ref().unwrap(), true, &key)
        .unwrap()
        .unwrap();
    assert_eq!(applied.schema_version, 2);
    assert_eq!(applied.state, MediaPatchEffectState::Applied as i32);
    assert!(journal.patched_record(&claim).is_ok());
}
