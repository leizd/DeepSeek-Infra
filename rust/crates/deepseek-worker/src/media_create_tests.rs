use super::*;

const ID: &str = "media_0123456789abcdef";

#[test]
fn media_create_upload_preserves_five_kinds_and_opaque_bytes_without_processing() {
    for (filename, mime, kind, suffix) in [
        ("image.PNG", "image/png", "image", ".png"),
        ("scan.pdf", "application/pdf", "pdf", ".pdf"),
        ("audio.mp3", "audio/mpeg", "audio", ".mp3"),
        ("video.mp4", "video/mp4", "video", ".mp4"),
        ("page.HTML", "text/html", "webpage", ".html"),
    ] {
        let payload = MediaUploadPayload {
            filename: filename.into(),
            content_type: mime.into(),
            project_id: "project-upload".into(),
            title: String::new(),
            data: b"  \0opaque\xff\n  ".to_vec(),
        };
        let prepared = upload(&payload, ID, 1_700_000_000).unwrap();
        let record = public(&prepared);
        assert_eq!(record["type"], kind);
        assert_eq!(record["title"], filename);
        assert_eq!(record["source"]["kind"], "upload");
        assert_eq!(record["source"]["refId"], filename);
        assert_eq!(record["projectId"], "project-upload");
        assert_eq!(record["status"], "pending");
        assert_eq!(record["metadata"], json!({}));
        assert_eq!(record["path"], format!("objects/{ID}/source{suffix}"));
        assert_eq!(prepared.source_suffix, suffix);
        assert_eq!(prepared.source_bytes, payload.data);
    }
}

#[test]
fn media_create_upload_keeps_original_suffix_title_and_mime_rules() {
    for (filename, suffix) in [
        (".hidden", ".bin"),
        ("trailing.", ".bin"),
        ("archive.TAR.GZ", ".gz"),
        ("name.Ä", ".ä"),
    ] {
        let payload = MediaUploadPayload {
            filename: filename.into(),
            content_type: " IMAGE/PNG ; charset=binary ".into(),
            title: "  explicit title  ".into(),
            data: b"bytes".to_vec(),
            ..Default::default()
        };
        let prepared = upload(&payload, ID, 1_700_000_000).unwrap();
        assert_eq!(prepared.source_suffix, suffix);
        assert_eq!(public(&prepared)["title"], "explicit title");
        assert_eq!(public(&prepared)["mimeType"], "image/png");
    }
    let payload = MediaUploadPayload {
        filename: "scan.pdf".into(),
        data: b"opaque".to_vec(),
        ..Default::default()
    };
    assert_eq!(
        public(&upload(&payload, ID, 1_700_000_000).unwrap())["mimeType"],
        "application/pdf"
    );
}

#[test]
fn media_create_upload_enforces_media_size_mime_and_normalized_identity() {
    let mut payload = MediaUploadPayload {
        filename: "image.png".into(),
        content_type: "image/png".into(),
        ..Default::default()
    };
    let error = upload(&payload, ID, 1_700_000_000).unwrap_err();
    assert_eq!(
        (error.status, error.message.as_str()),
        (400, "Media source is empty")
    );
    payload.data = vec![1; 50 * 1024 * 1024];
    assert_eq!(
        upload(&payload, ID, 1_700_000_000)
            .unwrap()
            .source_bytes
            .len(),
        50 * 1024 * 1024
    );
    payload.data.push(1);
    let error = upload(&payload, ID, 1_700_000_000).unwrap_err();
    assert_eq!((error.status, error.code), (413, "upload_too_large"));
    payload.data = b"bytes".to_vec();
    payload.content_type = "application/octet-stream".into();
    assert_eq!(
        upload(&payload, ID, 1_700_000_000).unwrap_err().message,
        "Unsupported media MIME type"
    );
    payload.content_type = "image/png".into();
    for filename in ["", "../outside.png", "dir\\image.png", "nul\0.png"] {
        payload.filename = filename.into();
        assert!(upload(&payload, ID, 1_700_000_000).is_err());
    }
    payload.filename = "x".repeat(181);
    assert!(upload(&payload, ID, 1_700_000_000).is_err());
    payload.filename = "image.png".into();
    payload.project_id = "../foreign".into();
    assert!(upload(&payload, ID, 1_700_000_000).is_err());
}

fn upload_plan_fixture(payload: &MediaCreatePayload, staging: &Path) -> MediaPatchClaim {
    use deepseek_protocol::generated::deepseek::{
        common::v1::ActionFence,
        media::v1::{MediaCreateManifest, MediaPatchIntent},
    };
    let bytes = payload.encode_to_vec();
    let mut claim = MediaPatchClaim {
        schema_version: 1,
        intent: Some(MediaPatchIntent {
            schema_version: 1,
            operation_id: "media-create-0123456789abcdef0123456789abcdef".into(),
            media_id: ID.into(),
            kind: MediaMutationKind::Create as i32,
            create: Some(MediaCreateManifest {
                payload_sha256: media_library::sha(&bytes),
                payload_bytes: bytes.len() as u64,
            }),
            ..Default::default()
        }),
        fence: Some(ActionFence {
            action_id: "media-create-0123456789abcdef0123456789abcdef".into(),
            execution_epoch: 7,
        }),
        owner: "go-fixture".into(),
        claim_token: "a".repeat(64),
        writer_fencing_token: 1,
        lease_until_unix: i64::MAX,
        state: "CLAIMED".into(),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
        ..Default::default()
    };
    claim.request_sha256 = media_library::sha(&claim.intent.as_ref().unwrap().encode_to_vec());
    let directory = operation_dir(staging, &claim.intent.as_ref().unwrap().operation_id).unwrap();
    persist_bound(&directory.join("payload.pb"), &bytes).unwrap();
    claim
}

#[test]
fn media_create_upload_preparation_remains_bound_to_original_content_through_retention() {
    let root = tempfile::tempdir().unwrap();
    let staging = tempfile::tempdir().unwrap();
    let payload = MediaCreatePayload {
        registration: None,
        upload: Some(MediaUploadPayload {
            filename: "scan.PDF".into(),
            content_type: "application/pdf".into(),
            data: b"opaque PDF".to_vec(),
            ..Default::default()
        }),
    };
    let claim = upload_plan_fixture(&payload, staging.path());
    let (outcome, publication) =
        plan(root.path(), staging.path(), &claim, b"{\"media\":[]}").unwrap();
    assert_eq!(outcome.state, MediaPatchEffectState::Applied as i32);
    assert!(publication.is_some());
    assert_eq!(
        retained_preparation(staging.path(), &claim, &outcome)
            .unwrap()
            .source_bytes,
        b"opaque PDF"
    );
    finish(root.path(), staging.path(), &claim, &outcome).unwrap();
    let path = root.path().join(format!(".media/objects/{ID}/source.pdf"));
    assert_eq!(fs::read(&path).unwrap(), b"opaque PDF");
    let modified = fs::metadata(&path).unwrap().modified().unwrap();
    finish(root.path(), staging.path(), &claim, &outcome).unwrap();
    assert_eq!(fs::metadata(&path).unwrap().modified().unwrap(), modified);
    let directory =
        operation_dir(staging.path(), &claim.intent.as_ref().unwrap().operation_id).unwrap();
    fs::write(directory.join("payload.pb"), b"changed original payload").unwrap();
    assert!(retained_preparation(staging.path(), &claim, &outcome).is_err());
}

#[test]
fn media_create_upload_rejects_ambiguous_or_absent_payload_variants() {
    for both in [false, true] {
        let root = tempfile::tempdir().unwrap();
        let staging = tempfile::tempdir().unwrap();
        let mut payload = MediaCreatePayload::default();
        if both {
            payload.registration = registration(
                &json!({"type":"image","mimeType":"image/png"}),
                ID,
                1_700_000_000,
            )
            .unwrap()
            .record;
            payload.upload = Some(MediaUploadPayload {
                filename: "image.png".into(),
                content_type: "image/png".into(),
                data: b"opaque".to_vec(),
                ..Default::default()
            });
        }
        let claim = upload_plan_fixture(&payload, staging.path());
        assert!(plan(root.path(), staging.path(), &claim, b"{\"media\":[]}").is_err());
        assert!(!root.path().join(".media").exists());
    }
}

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
