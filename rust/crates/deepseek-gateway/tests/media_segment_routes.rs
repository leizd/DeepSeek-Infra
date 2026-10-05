//! `GET /api/media/{media_id}/segments` through the production router.
//!
//! The body is `library.list_segments` after `library.get_media`. A missing
//! `.media` directory stays missing. Other methods on this path are 405.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "media-segment-route-token";

static ENV_BUSY: AtomicBool = AtomicBool::new(false);

struct EnvLock;
impl EnvLock {
    fn acquire() -> Self {
        while ENV_BUSY
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            std::thread::yield_now();
        }
        Self
    }
}
impl Drop for EnvLock {
    fn drop(&mut self) {
        ENV_BUSY.store(false, Ordering::Release);
    }
}

struct EnvGuard {
    saved: Vec<(&'static str, Option<String>)>,
}
impl EnvGuard {
    fn set(pairs: &[(&'static str, String)]) -> Self {
        let saved = pairs
            .iter()
            .map(|(name, value)| {
                let previous = std::env::var(name).ok();
                unsafe { std::env::set_var(name, value) }
                (*name, previous)
            })
            .collect();
        Self { saved }
    }
}
impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (name, value) in &self.saved {
            match value {
                Some(value) => unsafe { std::env::set_var(name, value) },
                None => unsafe { std::env::remove_var(name) },
            }
        }
    }
}

struct Fixture {
    root: tempfile::TempDir,
    _static_root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().expect("workspace");
        let static_root = tempfile::tempdir().expect("static");
        std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
        std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
        let env = EnvGuard::set(&[
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_str().expect("utf-8").to_string(),
            ),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
            ("DEEPSEEK_RUNTIME_MODE", String::new()),
            ("GO_CONTROL_ADDR", String::new()),
            ("DEEPSEEK_GO_CONTROL_URL", String::new()),
        ]);
        let app = create_production_app(static_root.path()).expect("app");
        Self {
            root,
            _static_root: static_root,
            _env: env,
            app,
        }
    }

    fn media_dir(&self) -> PathBuf {
        self.root.path().join(".media")
    }

    fn library_path(&self) -> PathBuf {
        self.media_dir().join("library.json")
    }

    fn segments_path(&self, media_id: &str) -> PathBuf {
        self.media_dir()
            .join("segments")
            .join(format!("{media_id}.json"))
    }

    fn write_library(&self, media: &Value) {
        std::fs::create_dir_all(self.media_dir()).unwrap();
        std::fs::write(
            self.library_path(),
            serde_json::to_vec(&json!({"media": media})).unwrap(),
        )
        .unwrap();
    }

    fn write_segments(&self, media_id: &str, segments: &Value) {
        let path = self.segments_path(media_id);
        std::fs::create_dir_all(path.parent().expect("parent")).unwrap();
        std::fs::write(
            path,
            serde_json::to_vec(&json!({"segments": segments})).unwrap(),
        )
        .unwrap();
    }

    async fn request(&self, method: &str, uri: &str, auth: bool) -> (StatusCode, Value, Vec<u8>) {
        let mut request = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "127.0.0.1");
        if auth {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::empty()).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, body, bytes.to_vec())
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf()
}

fn oracle(root: &Path, cases: &Value) -> Vec<Value> {
    let script = r#"
import json, sys
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.media.library import get_media, list_segments
plan = json.loads(sys.stdin.read())
results = []
for case in plan:
    try:
        get_media(case["mediaId"])
        results.append({"status": 200, "body": {"ok": True, "segments": list_segments(case["mediaId"])}})
    except AppError as exc:
        results.append({"status": exc.status, "body": {"error": str(exc), "code": exc.code.value}})
    except (UnicodeDecodeError, ValueError, TypeError, OverflowError):
        results.append({"status": 500, "body": {"error": "Server error", "code": "internal"}})
print(json.dumps(results))
"#;
    let mut child = std::process::Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("python oracle");
    {
        use std::io::Write;
        child
            .stdin
            .as_mut()
            .expect("stdin")
            .write_all(&serde_json::to_vec(cases).unwrap())
            .unwrap();
    }
    let output = child.wait_with_output().expect("oracle wait");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("oracle json")
}

fn snapshot(path: &Path) -> Option<Vec<u8>> {
    std::fs::read(path).ok()
}

fn generated_ids(body: &Value) -> Vec<String> {
    body.get("segments")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|segment| segment.get("segmentId").and_then(Value::as_str))
        .filter(|id| is_generated_segment_id(id))
        .map(str::to_string)
        .collect()
}

fn is_generated_segment_id(id: &str) -> bool {
    let Some(hex) = id.strip_prefix("seg_") else {
        return false;
    };
    hex.len() == 16 && hex.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn mask_generated_ids(value: &mut Value) {
    let Some(segments) = value.get_mut("segments").and_then(Value::as_array_mut) else {
        return;
    };
    for segment in segments {
        let generated = segment
            .get("segmentId")
            .and_then(Value::as_str)
            .is_some_and(is_generated_segment_id);
        if generated {
            segment
                .as_object_mut()
                .expect("segment")
                .insert("segmentId".to_string(), json!("GENERATED"));
        }
    }
}

fn rich_library() -> Value {
    json!([
        {
            "mediaId": "media_keep",
            "projectId": "proj_ok",
            "type": "pdf",
            "title": "Notes",
            "mimeType": "application/pdf",
            "path": "objects/media_keep/source.pdf",
            "status": "ready"
        },
        {"mediaId": "media_keep", "type": "nope", "title": "Second"},
        {"type": "image", "title": "no id"},
        {
            "mediaId": "media_mime",
            "mimeType": "image/jpeg; charset=binary",
            "title": "shot"
        },
        {
            "mediaId": "media_suffix",
            "title": "clip.mp4",
            "path": "objects\\media_suffix\\source.mp4"
        },
        {"mediaId": "media_page", "title": "page.html"},
        {"mediaId": "media_bad", "type": "nope", "title": "nope"},
        {"mediaId": "media_badproj", "type": "image", "projectId": "x"},
        {"mediaId": "media_badpath", "type": "image", "path": "/abs/a.png"},
        {"mediaId": "media_baddir", "type": "image", "path": "a/../b.png"},
        {"mediaId": "media_badstatus", "type": "image", "status": "nope"},
        {"mediaId": " media_trim ", "type": "audio", "title": "a.mp3"},
        {"mediaId": "media_second", "type": "nope"},
        {"mediaId": "media_second", "type": "Image", "title": "Saved"}
    ])
}

fn rich_segments() -> Value {
    json!([
        "skip-me",
        {
            "segmentId": "keep_text",
            "type": " Page_Text ",
            "text": "Hello password=abcd",
            "confidence": "0.125",
            "index": "1_0",
            "page": " 2 ",
            "timeRange": [1.5, 0.25, 9],
            "framePath": "frames\\a.png",
            "citation": {"kind": "file", "refId": "doc-1", "": "drop"}
        },
        {"type": "nope", "segmentId": "skip_type", "text": "hidden"},
        {"type": "caption", "segmentId": "x", "text": "short id"},
        {"type": "frame", "segmentId": "skip_path", "framePath": "../escape.png"},
        {"type": "frame", "segmentId": "skip_abs", "framePath": "/tmp/a.png"},
        {
            "type": "transcript",
            "text": false,
            "confidence": false,
            "index": false,
            "page": false
        },
        {
            "segmentId": "keep_nums",
            "type": "ocr_text",
            "text": 12,
            "confidence": "",
            "index": 1.9,
            "page": 2.9,
            "timeRange": [0.5, 1.25]
        },
        {"segmentId": "keep_nulls", "type": "webpage_text", "confidence": null, "page": null, "index": null},
        {"type": "caption", "text": "minted"}
    ])
}

async fn expect_oracle(fixture: &Fixture, media_id: &str, query: &str, expected: &Value) {
    let uri = if query.is_empty() {
        format!("/api/media/{media_id}/segments")
    } else {
        format!("/api/media/{media_id}/segments?{query}")
    };
    let (status, mut body, _) = fixture.request("GET", &uri, true).await;
    let expected_status =
        StatusCode::from_u16(expected["status"].as_u64().unwrap() as u16).unwrap();
    assert_eq!(
        status, expected_status,
        "id={media_id} body={body} expected={expected}"
    );
    let mut expected_body = expected["body"].clone();
    if status == StatusCode::OK {
        let http_ids = generated_ids(&body);
        let oracle_ids = generated_ids(&expected_body);
        assert_eq!(http_ids.len(), oracle_ids.len(), "generated id count");
        if !http_ids.is_empty() {
            assert_ne!(
                http_ids, oracle_ids,
                "generated ids must not be copied from the oracle"
            );
        }
        mask_generated_ids(&mut body);
        mask_generated_ids(&mut expected_body);
    }
    assert_eq!(body, expected_body, "id={media_id}");
}

#[tokio::test]
async fn media_segments_match_the_library_without_creating_a_missing_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.root.path();

    let (status, body, _) = fixture
        .request("GET", "/api/media/media_keep/segments", false)
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(
        body,
        json!({"error": {"code": "UNAUTHORIZED", "message": "Auth required"}})
    );
    assert!(!fixture.media_dir().exists());

    let missing = oracle(
        root,
        &json!([{"mediaId": "media_keep"}, {"mediaId": "no"}, {"mediaId": "   "}]),
    );
    expect_oracle(&fixture, "media_keep", "", &missing[0]).await;
    expect_oracle(&fixture, "no", "", &missing[1]).await;
    expect_oracle(&fixture, "%20%20%20", "", &missing[2]).await;
    assert!(!fixture.media_dir().exists());

    let (status, body, _) = fixture
        .request("POST", "/api/media/media_keep/segments", true)
        .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(body, json!({"detail": "Method Not Allowed"}));
    let (status, _, _) = fixture
        .request("DELETE", "/api/media/media_keep/segments", false)
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert!(!fixture.media_dir().exists());

    fixture.write_library(&rich_library());
    let library_bytes = snapshot(&fixture.library_path()).expect("library");
    let bare = oracle(root, &json!([{"mediaId": "media_keep"}]));
    expect_oracle(&fixture, "media_keep", "projectId=other", &bare[0]).await;
    assert!(!fixture.segments_path("media_keep").exists());
    assert_eq!(
        snapshot(&fixture.library_path()).as_deref(),
        Some(library_bytes.as_slice())
    );

    fixture.write_segments("media_keep", &rich_segments());
    fixture.write_segments(
        "media_bad",
        &json!([{"segmentId": "should_not_leak", "type": "caption", "text": "secret"}]),
    );
    let segment_bytes = snapshot(&fixture.segments_path("media_keep")).expect("segments");
    let bad_segment_bytes = snapshot(&fixture.segments_path("media_bad")).expect("bad segments");
    let ids = [
        "media_keep",
        "media_mime",
        "media_suffix",
        "media_page",
        "media_bad",
        "media_badproj",
        "media_badpath",
        "media_baddir",
        "media_badstatus",
        "media_trim",
        "media_second",
        "media_missing",
    ];
    let plan = Value::Array(ids.iter().map(|id| json!({"mediaId": *id})).collect());
    let expected = oracle(root, &plan);
    for (id, expected) in ids.iter().zip(expected.iter()) {
        let encoded = if *id == "media_trim" {
            "%20media_trim%20"
        } else {
            *id
        };
        expect_oracle(&fixture, encoded, "", expected).await;
    }
    assert_eq!(
        snapshot(&fixture.library_path()).as_deref(),
        Some(library_bytes.as_slice())
    );
    assert_eq!(
        snapshot(&fixture.segments_path("media_keep")).as_deref(),
        Some(segment_bytes.as_slice())
    );
    assert_eq!(
        snapshot(&fixture.segments_path("media_bad")).as_deref(),
        Some(bad_segment_bytes.as_slice())
    );

    let (status, bytes_body, raw) = fixture
        .request("HEAD", "/api/media/media_keep/segments", true)
        .await;
    assert_eq!(status, StatusCode::OK);
    assert!(raw.is_empty(), "HEAD body={bytes_body}");

    let (status, body, _) = fixture.request("GET", "/api/media", true).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "GO_CONTROL_PROXY_NOT_READY");
    let (status, body, _) = fixture.request("GET", "/api/media/media_keep", true).await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "GO_CONTROL_PROXY_NOT_READY");
    let (status, body, _) = fixture
        .request("POST", "/api/media/media_keep/segments", true)
        .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(body, json!({"detail": "Method Not Allowed"}));
    assert_eq!(
        snapshot(&fixture.library_path()).as_deref(),
        Some(library_bytes.as_slice())
    );
    assert_eq!(
        snapshot(&fixture.segments_path("media_keep")).as_deref(),
        Some(segment_bytes.as_slice())
    );

    fixture.write_segments(
        "media_keep",
        &json!([{"type": "caption", "segmentId": "keep_bad_index", "index": "abc"}]),
    );
    let fatal = oracle(root, &json!([{"mediaId": "media_keep"}]));
    expect_oracle(&fixture, "media_keep", "", &fatal[0]).await;
    assert_eq!(fatal[0]["status"], 500);

    fixture.write_segments(
        "media_keep",
        &json!([{"type": "caption", "segmentId": "keep_bad_page", "page": "nope"}]),
    );
    let fatal_page = oracle(root, &json!([{"mediaId": "media_keep"}]));
    expect_oracle(&fixture, "media_keep", "", &fatal_page[0]).await;
    assert_eq!(fatal_page[0]["status"], 500);
    assert_eq!(
        snapshot(&fixture.library_path()).as_deref(),
        Some(library_bytes.as_slice())
    );

    let utf8 = [0xff_u8, 0xfe, 0x00];
    std::fs::write(fixture.library_path(), utf8).unwrap();
    let decoded = oracle(root, &json!([{"mediaId": "media_keep"}, {"mediaId": "no"}]));
    expect_oracle(&fixture, "media_keep", "", &decoded[0]).await;
    expect_oracle(&fixture, "no", "", &decoded[1]).await;
    assert_eq!(decoded[0]["status"], 500);
    assert_eq!(decoded[1]["status"], 400);
    assert_eq!(
        snapshot(&fixture.library_path()).as_deref(),
        Some(utf8.as_slice())
    );

    std::fs::remove_file(fixture.library_path()).unwrap();
    std::fs::create_dir(fixture.library_path()).unwrap();
    let directory = oracle(root, &json!([{"mediaId": "media_keep"}]));
    expect_oracle(&fixture, "media_keep", "", &directory[0]).await;
    assert_eq!(directory[0]["status"], 404);
    assert!(fixture.library_path().is_dir());

    std::fs::remove_dir(fixture.library_path()).unwrap();
    std::fs::write(fixture.library_path(), b"{").unwrap();
    let bad_json = oracle(root, &json!([{"mediaId": "media_keep"}]));
    expect_oracle(&fixture, "media_keep", "", &bad_json[0]).await;
    assert_eq!(bad_json[0]["status"], 404);
    assert_eq!(
        snapshot(&fixture.library_path()).as_deref(),
        Some(b"{".as_slice())
    );

    fixture.write_library(&json!([{"mediaId": "media_keep", "type": "image", "title": "ok"}]));
    let library_after = snapshot(&fixture.library_path()).unwrap();
    std::fs::write(fixture.segments_path("media_keep"), utf8).unwrap();
    let segment_utf8 = oracle(root, &json!([{"mediaId": "media_keep"}]));
    expect_oracle(&fixture, "media_keep", "", &segment_utf8[0]).await;
    assert_eq!(segment_utf8[0]["status"], 500);
    assert_eq!(
        snapshot(&fixture.segments_path("media_keep")).as_deref(),
        Some(utf8.as_slice())
    );
    assert_eq!(
        snapshot(&fixture.library_path()).as_deref(),
        Some(library_after.as_slice())
    );

    std::fs::remove_file(fixture.segments_path("media_keep")).unwrap();
    std::fs::create_dir(fixture.segments_path("media_keep")).unwrap();
    let segment_dir = oracle(root, &json!([{"mediaId": "media_keep"}]));
    expect_oracle(&fixture, "media_keep", "", &segment_dir[0]).await;
    assert_eq!(segment_dir[0]["body"]["segments"], json!([]));
    assert!(fixture.segments_path("media_keep").is_dir());

    std::fs::remove_dir(fixture.segments_path("media_keep")).unwrap();
    std::fs::write(fixture.segments_path("media_keep"), b"[]").unwrap();
    let segment_root = oracle(root, &json!([{"mediaId": "media_keep"}]));
    expect_oracle(&fixture, "media_keep", "", &segment_root[0]).await;
    assert_eq!(segment_root[0]["body"]["segments"], json!([]));
    assert_eq!(
        snapshot(&fixture.segments_path("media_keep")).as_deref(),
        Some(b"[]".as_slice())
    );
    assert_eq!(
        snapshot(&fixture.library_path()).as_deref(),
        Some(library_after.as_slice())
    );
}
