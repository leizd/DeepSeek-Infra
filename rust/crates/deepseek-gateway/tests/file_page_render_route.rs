//! `GET /api/file-page-image` and `GET /api/file-page-layout` on the production router.
//!
//! A legal PDF page comes back as a PNG with the oracle's page headers, and the
//! word boxes match the MuPDF Helvetica geometry. A non-PDF, a bad page and a bad
//! scale leave the cache directory unchanged.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "unit-file-page-token";
const FILE_ID: &str = "abababababababababababababababab";

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
    _env: EnvGuard,
    app: axum::Router,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().expect("workspace");
        let env = EnvGuard::set(&[
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_str().expect("utf-8").to_string(),
            ),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ]);
        let static_root = tempfile::tempdir().expect("static");
        std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
        std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
        let app = create_production_app(static_root.path()).expect("app");
        Self {
            root,
            _env: env,
            app,
        }
    }

    fn seed(&self, name: &str, kind: &str, media: &str, source: &[u8]) {
        let directory = self.root.path().join(".file-cache");
        std::fs::create_dir_all(&directory).unwrap();
        let body = json!({
            "id": FILE_ID,
            "name": name,
            "type": media,
            "size": source.len(),
            "kind": kind,
            "pageCount": 1,
            "charCount": 1,
            "chunkCount": 1,
            "chunks": [{"index": 0, "text": "Hello pdf", "lineStart": 1, "lineEnd": 1}],
            "sourceAvailable": true,
        });
        std::fs::write(directory.join(format!("{FILE_ID}.json")), body.to_string()).unwrap();
        std::fs::write(directory.join(format!("{FILE_ID}.source")), source).unwrap();
    }

    fn names(&self) -> Vec<String> {
        let directory = self.root.path().join(".file-cache");
        let mut names: Vec<String> = std::fs::read_dir(&directory)
            .unwrap()
            .flatten()
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }

    async fn get(&self, uri: &str, auth: bool) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
        let mut request = Request::builder().uri(uri);
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
        let headers = response.headers().clone();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap()
            .to_vec();
        (status, headers, bytes)
    }
}

fn simple_pdf() -> Vec<u8> {
    let stream = b"BT\n/F1 12 Tf\n72 720 Td\n(Hello pdf) Tj\n0 -16 Td\n(World) Tj\nET\n";
    let mut objects = vec![
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_vec(),
        Vec::new(),
        b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
    ];
    let mut content = format!("<< /Length {} >>\nstream\n", stream.len()).into_bytes();
    content.extend_from_slice(stream);
    content.extend_from_slice(b"endstream");
    objects.push(content);
    objects.push(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 1 0 R >> >> >>".to_vec(),
    );
    objects[1] = b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>".to_vec();
    let mut parts = vec![b"%PDF-1.4\n".to_vec()];
    let mut offsets = vec![0usize];
    for (index, object) in objects.iter().enumerate() {
        offsets.push(parts.iter().map(Vec::len).sum());
        let mut encoded = format!("{} 0 obj\n", index + 1).into_bytes();
        encoded.extend_from_slice(object);
        encoded.extend_from_slice(b"\nendobj\n");
        parts.push(encoded);
    }
    let xref = parts.iter().map(Vec::len).sum::<usize>();
    let mut trailer = b"xref\n0 6\n0000000000 65535 f \n".to_vec();
    for offset in offsets.iter().skip(1) {
        trailer.extend(format!("{offset:010} 00000 n \n").into_bytes());
    }
    trailer.extend(
        format!("trailer\n<< /Size 6 /Root 3 0 R >>\nstartxref\n{xref}\n%%EOF\n").into_bytes(),
    );
    parts.push(trailer);
    parts.into_iter().flatten().collect()
}

#[tokio::test]
async fn a_pdf_page_renders_and_a_refusal_writes_nothing() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    fixture.seed("notes.pdf", "pdf", "application/pdf", &simple_pdf());

    let (status, headers, png) = fixture
        .get(
            &format!("/api/file-page-image?fileId={FILE_ID}&page=1&scale=1.6"),
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{}", String::from_utf8_lossy(&png));
    assert!(png.starts_with(b"\x89PNG"), "png magic");
    assert_eq!(headers.get("x-file-page").unwrap(), "1");
    assert_eq!(headers.get("x-file-page-count").unwrap(), "1");
    assert_eq!(headers.get(header::CONTENT_TYPE).unwrap(), "image/png");
    assert!(
        headers
            .get(header::CONTENT_DISPOSITION)
            .unwrap()
            .to_str()
            .unwrap()
            .contains("notes-page-1.png")
    );
    let cache_name = format!("{FILE_ID}.page-1-160.png");
    let cached = std::fs::read(fixture.root.path().join(".file-cache").join(&cache_name)).unwrap();
    assert_eq!(cached, png);
    let index_before = std::fs::read(
        fixture
            .root
            .path()
            .join(".file-cache")
            .join(format!("{FILE_ID}.json")),
    )
    .unwrap();

    let (again_status, _, again) = fixture
        .get(
            &format!("/api/file-page-image?fileId={FILE_ID}&page=1&scale=1.6"),
            true,
        )
        .await;
    assert_eq!(again_status, StatusCode::OK);
    assert_eq!(again, png);

    let (status, _, body) = fixture
        .get(
            &format!("/api/file-page-layout?fileId={FILE_ID}&page=1"),
            true,
        )
        .await;
    let layout: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(status, StatusCode::OK, "{layout}");
    assert_eq!(layout["page"]["text"], "Hello pdf\nWorld");
    assert_eq!(layout["page"]["words"][0]["text"], "Hello");
    assert_eq!(layout["page"]["words"][1]["text"], "pdf");
    assert_eq!(layout["page"]["words"][2]["text"], "World");
    assert_eq!(layout["page"]["width"], 612.0);
    assert_eq!(layout["page"]["height"], 792.0);
    assert_eq!(layout["file"]["fileId"], FILE_ID);

    let after_layout = fixture.names();
    assert!(after_layout.contains(&cache_name));
    assert_eq!(
        std::fs::read(
            fixture
                .root
                .path()
                .join(".file-cache")
                .join(format!("{FILE_ID}.json"))
        )
        .unwrap(),
        index_before
    );

    let text = fixture;
    drop(text);
    let fixture = Fixture::new();
    fixture.seed("notes.txt", "txt", "text/plain", b"hello");
    let before = fixture.names();
    let (status, _, body) = fixture
        .get(
            &format!("/api/file-page-image?fileId={FILE_ID}&page=1"),
            true,
        )
        .await;
    let json: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    assert_eq!(status, StatusCode::UNSUPPORTED_MEDIA_TYPE, "{json}");
    assert_eq!(json["code"], "unsupported_file");
    let (status, _, body) = fixture
        .get(
            &format!("/api/file-page-layout?fileId={FILE_ID}&page=x"),
            true,
        )
        .await;
    let json: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    assert_eq!(status, StatusCode::UNSUPPORTED_MEDIA_TYPE, "{json}");
    assert_eq!(
        json["error"],
        "Page text layout is only available for PDF files"
    );
    assert_eq!(fixture.names(), before);

    let fixture = Fixture::new();
    fixture.seed("notes.pdf", "pdf", "application/pdf", &simple_pdf());
    let before = fixture.names();
    for uri in [
        format!("/api/file-page-image?fileId={FILE_ID}&page=x"),
        format!("/api/file-page-image?fileId={FILE_ID}&scale=0"),
        format!("/api/file-page-image?fileId={FILE_ID}&scale=nope"),
        format!("/api/file-page-layout?fileId={FILE_ID}&page=1.5"),
    ] {
        let (status, _, body) = fixture.get(&uri, true).await;
        let json: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
        assert_eq!(status, StatusCode::BAD_REQUEST, "{uri}: {json}");
        assert_eq!(json["code"], "invalid_payload", "{uri}");
        assert_eq!(fixture.names(), before, "{uri}");
    }
    let (status, _, _) = fixture
        .get(
            &format!("/api/file-page-image?fileId={FILE_ID}&page=1"),
            false,
        )
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(fixture.names(), before);
}
