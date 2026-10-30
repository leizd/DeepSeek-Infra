use super::*;

const ID: &str = "media_0123456789abcdef";

fn public(prepared: &MediaCreatePreparation) -> Value {
    media_values::to_json(
        &deepseek_protocol::generated::deepseek::media::v1::MediaValue {
            kind: Some(Kind::ObjectValue(prepared.record.clone().unwrap())),
        },
    )
    .unwrap()
}

#[test]
fn media_create_registration_preserves_all_six_media_kinds_without_a_source() {
    for (kind, mime) in [
        ("image", "image/png"),
        ("pdf", "application/pdf"),
        ("audio", "audio/mpeg"),
        ("video", "video/mp4"),
        ("webpage", "text/html"),
        ("screenshot", "image/png"),
    ] {
        let prepared = registration(
            &json!({"type":kind,"mimeType":mime,"title":" Native "}),
            ID,
            1_700_000_000,
        )
        .unwrap();
        let record = public(&prepared);
        assert_eq!(record["type"], kind);
        assert_eq!(record["mimeType"], mime);
        assert_eq!(record["title"], "Native");
        assert_eq!(record["status"], "pending");
        assert_eq!(record["path"], "");
        assert_eq!(record["mediaId"], ID);
        assert_eq!(record["createdAt"], record["updatedAt"]);
        assert!(prepared.source_bytes.is_empty());
    }
}

#[test]
fn media_create_webpage_preserves_source_bytes_metadata_collision_order_and_typed_zero() {
    let payload:Value=serde_json::from_str(r#"{"type":"webpage","mimeType":"text/html","url":"https://example.invalid","html":"  <h1>原生</h1>  ","text":"  Text  ","metadata":{"a?":"first","a!":"last","zero":0,"disabled":false}}"#).unwrap();
    let prepared = registration(&payload, ID, 1_700_000_000).unwrap();
    let record = public(&prepared);
    assert_eq!(record["path"], format!("objects/{ID}/source.html"));
    assert_eq!(prepared.source_suffix, ".html");
    assert_eq!(prepared.source_bytes, "<h1>原生</h1>".as_bytes());
    assert_eq!(record["metadata"]["a"], "last");
    assert_eq!(record["metadata"]["webpageText"], "Text");
    assert!(record["metadata"]["zero"].is_number());
    assert!(record["metadata"]["disabled"].is_boolean());
    assert_eq!(record["metadata"]["sourceUrl"], "https://example.invalid");
    assert_eq!(record["source"]["kind"], "upload");
}

#[test]
fn media_create_transcript_and_text_sources_keep_original_source_txt_name_and_whitespace() {
    for (kind, field, key) in [
        ("audio", "text", "transcript"),
        ("video", "transcript", "transcript"),
        ("image", "text", "text"),
    ] {
        let mut payload = json!({"type":kind});
        payload[field] = json!("  retained text \n");
        let prepared = registration(&payload, ID, 1_700_000_000).unwrap();
        let record = public(&prepared);
        assert_eq!(record["metadata"][key], "  retained text \n");
        assert_eq!(prepared.source_bytes, b"  retained text \n");
        assert_eq!(prepared.source_suffix, ".txt");
        assert_eq!(record["path"], format!("objects/{ID}/source.txt"));
    }
}

#[test]
fn media_create_registration_refuses_unsupported_types_mimes_and_foreign_paths() {
    for payload in [
        json!({}),
        json!({"type":"exe"}),
        json!({"type":"image","mimeType":"application/octet-stream"}),
        json!({"type":"image","path":"../outside"}),
        json!({"type":"image","path":"objects/other/source.png"}),
        json!({"type":"image","projectId":"../foreign"}),
    ] {
        assert!(
            registration(&payload, ID, 1_700_000_000).is_err(),
            "{payload}"
        );
    }
}

#[test]
fn media_create_metadata_retains_original_two_hundred_entry_and_character_limits() {
    let prepared = registration(
        &json!({"type":"pdf","pageTexts":vec!["page";201],"metadata":{"long":"a".repeat(120_001)}}),
        ID,
        1_700_000_000,
    )
    .unwrap();
    let record = public(&prepared);
    assert_eq!(
        record["metadata"]["pageTexts"].as_array().unwrap().len(),
        200
    );
    assert_eq!(record["metadata"]["long"].as_str().unwrap().len(), 120_000);
}

#[test]
fn media_create_content_custody_refuses_path_aliases_and_conflicting_replay() {
    let root = tempfile::tempdir().unwrap();
    for operation in [
        "media-create-../outside",
        "media-create-/absolute",
        "media-patch-fixture",
        "media-create-ABCDEF",
    ] {
        assert!(operation_dir(root.path(), operation).is_err());
    }
    let path = root.path().join("owned/content.pb");
    persist_bound(&path, b"first").unwrap();
    persist_bound(&path, b"first").unwrap();
    assert!(persist_bound(&path, b"different").is_err());
    assert_eq!(fs::read(path).unwrap(), b"first");
}

// The ordinary test covers a successful retained read. The dedicated cloud
// workload runs this exact compiled test with a qualified syscall fault. These
// variables exist only in this test module, never in a production executable.
#[test]
fn media_create_retained_sync_fault_probe() {
    let Some(root) = std::env::var_os("DEEPSEEK_MEDIA_SYNC_TEST_ROOT") else {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("owned/source.html");
        persist_bound(&path, b"retained-media-sync").unwrap();
        persist_bound(&path, b"retained-media-sync").unwrap();
        assert_eq!(fs::read(path).unwrap(), b"retained-media-sync");
        return;
    };
    assert_eq!(std::env::var("GITHUB_ACTIONS").as_deref(), Ok("true"));
    let root = fs::canonicalize(root).unwrap();
    assert!(root.starts_with(fs::canonicalize(std::env::temp_dir()).unwrap()));
    assert!(
        root.file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("native-media-create-sync-")
    );
    assert_eq!(
        fs::read(root.join("owned-fixture-marker")).unwrap(),
        b"native-media-source-sync-test-v1"
    );
    let path = root.join(".media/objects/media_sync/source.html");
    assert_eq!(fs::read(&path).unwrap(), b"retained-media-sync");
    assert!(
        persist_bound(&path, b"retained-media-sync").is_err(),
        "retained bytes cannot replace the required file/directory synchronization barrier"
    );
    assert_eq!(fs::read(path).unwrap(), b"retained-media-sync");
}
