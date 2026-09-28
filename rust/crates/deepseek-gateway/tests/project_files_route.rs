//! `POST /api/project-files` through the production router.
//!
//! While Python owns `project_metadata_store`, the route refuses and leaves
//! `project.json` byte-identical. Once `DEEPSEEK_RUNTIME_MODE=python_disabled`,
//! the same upload writes `.projects/<id>/files` and `project.json`, and
//! `GET /api/workspace/projects/<id>` reads the document back.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "unit-project-files-token";
const PROJECT_ID: &str = "proj-demo";

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
    fn new(mode: &str) -> Self {
        let root = tempfile::tempdir().expect("workspace");
        let env = EnvGuard::set(&[
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_str().expect("utf-8").to_string(),
            ),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
            ("DEEPSEEK_RUNTIME_MODE", mode.to_string()),
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

    fn seed_project(&self) -> Vec<u8> {
        let directory = self.root.path().join(".projects").join(PROJECT_ID);
        std::fs::create_dir_all(&directory).unwrap();
        let body = format!(
            "{{\n  \"id\": \"{PROJECT_ID}\",\n  \"name\": \"Demo\",\n  \"documents\": [],\n  \"skills\": {{\n    \"enabledPacks\": [],\n    \"enabledPackVersions\": [],\n    \"enabledSkills\": [],\n    \"defaultSkill\": \"\",\n    \"recentSkills\": []\n  }},\n  \"skillRuns\": [],\n  \"savedItems\": [],\n  \"artifacts\": [],\n  \"createdAt\": 1,\n  \"updatedAt\": 1\n}}\n"
        );
        let bytes = body.into_bytes();
        std::fs::write(directory.join("project.json"), &bytes).unwrap();
        bytes
    }

    fn project_bytes(&self) -> Vec<u8> {
        std::fs::read(self.project_path()).unwrap_or_default()
    }

    fn project_path(&self) -> std::path::PathBuf {
        self.root
            .path()
            .join(".projects")
            .join(PROJECT_ID)
            .join("project.json")
    }

    async fn post(
        &self,
        uri: &str,
        content_type: &str,
        body: Vec<u8>,
        auth: bool,
    ) -> (StatusCode, Value) {
        let mut request = Request::builder()
            .method("POST")
            .uri(uri)
            .header(header::CONTENT_TYPE, content_type)
            .header(header::CONTENT_LENGTH, body.len().to_string());
        if auth {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::from(body)).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }

    async fn get(&self, uri: &str) -> (StatusCode, Value) {
        let response = self
            .app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(uri)
                    .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }
}

fn multipart(filename: &str, content_type: &str, data: &[u8]) -> (String, Vec<u8>) {
    let boundary = "DeepseekBoundary";
    let mut body = Vec::new();
    body.extend(
        format!(
            "--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n"
        )
        .into_bytes(),
    );
    body.extend(data);
    body.extend(format!("\r\n--{boundary}--\r\n").into_bytes());
    (format!("multipart/form-data; boundary={boundary}"), body)
}

#[tokio::test]
async fn a_project_file_is_written_and_read_back_once_python_is_disabled() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new("python_disabled");
    fixture.seed_project();
    let payload = b"hello project";
    let (content_type, body) = multipart("notes.txt", "text/plain", payload);
    let uri = format!("/api/project-files?projectId={PROJECT_ID}");
    let (status, json) = fixture.post(&uri, &content_type, body, true).await;
    assert_eq!(status, StatusCode::OK, "{json}");
    assert_eq!(json["ok"], true);
    let document = &json["documents"][0];
    let file_id = document["fileId"].as_str().expect("file id");
    assert_eq!(file_id.len(), 32);
    assert!(file_id.chars().all(|ch| ch.is_ascii_hexdigit()));
    assert_eq!(document["name"], "notes.txt");
    assert_eq!(document["projectId"], PROJECT_ID);
    assert_eq!(document["kind"], "txt");
    assert_eq!(document["preview"], "hello project");
    assert_eq!(document["id"].as_str().unwrap_or("").len(), 16);

    let stored: Value = serde_json::from_slice(&fixture.project_bytes()).unwrap();
    assert_eq!(stored["documents"][0]["fileId"], file_id);
    assert_eq!(stored["documents"][0]["preview"], "hello project");
    assert!(stored["updatedAt"].as_i64().unwrap_or(0) > 1);

    let source = fixture
        .root
        .path()
        .join(".projects")
        .join(PROJECT_ID)
        .join("files")
        .join(format!("{file_id}.source"));
    assert_eq!(std::fs::read(&source).unwrap(), payload);
    assert!(!fixture.root.path().join(".local-rag").exists());

    let (status, project) = fixture
        .get(&format!("/api/workspace/projects/{PROJECT_ID}"))
        .await;
    assert_eq!(status, StatusCode::OK, "{project}");
    assert_eq!(project["project"]["documents"][0]["fileId"], file_id);
    assert_eq!(project["project"]["files"][0]["preview"], "hello project");
    assert_eq!(project["project"]["stats"]["files"], 1);

    let reader = Request::builder()
        .method("POST")
        .uri("/api/file-reader")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::from(
            json!({"fileId": file_id, "projectId": PROJECT_ID}).to_string(),
        ))
        .unwrap();
    let response = fixture.app.clone().oneshot(reader).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let reader_body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(reader_body["chunks"][0]["text"], "hello project");

    let source_response = fixture
        .app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!(
                    "/api/file-source?fileId={file_id}&projectId={PROJECT_ID}"
                ))
                .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(source_response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(source_response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), payload);
}

#[tokio::test]
async fn project_files_are_refused_while_python_owns_the_store() {
    let _lock = EnvLock::acquire();
    for mode in ["", "python_authoritative", "go_authoritative"] {
        let fixture = Fixture::new(mode);
        let before = fixture.seed_project();
        let (content_type, body) = multipart("notes.txt", "text/plain", b"secret");
        let (status, json) = fixture
            .post(
                &format!("/api/project-files?projectId={PROJECT_ID}"),
                &content_type,
                body,
                true,
            )
            .await;
        assert_eq!(status, StatusCode::CONFLICT, "{mode}: {json}");
        assert_eq!(json["code"], "NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED");
        assert_eq!(fixture.project_bytes(), before, "{mode}");
        assert!(
            !fixture
                .root
                .path()
                .join(".projects")
                .join(PROJECT_ID)
                .join("files")
                .exists()
        );
        assert!(!fixture.root.path().join(".local-rag").exists());
    }
}

#[tokio::test]
async fn a_missing_token_and_a_bad_upload_do_not_write_the_project() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new("python_disabled");
    let before = fixture.seed_project();
    let (content_type, body) = multipart("notes.txt", "text/plain", b"secret");
    let (status, _) = fixture
        .post(
            &format!("/api/project-files?projectId={PROJECT_ID}"),
            &content_type,
            body,
            false,
        )
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(fixture.project_bytes(), before);

    let (content_type, body) = multipart("blob.bin", "application/octet-stream", b"a\0b");
    let (status, json) = fixture
        .post(
            &format!("/api/project-files?projectId={PROJECT_ID}"),
            &content_type,
            body,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::UNSUPPORTED_MEDIA_TYPE, "{json}");
    assert_eq!(json["code"], "unsupported_file");
    assert_eq!(fixture.project_bytes(), before);
    assert!(
        !fixture
            .root
            .path()
            .join(".projects")
            .join(PROJECT_ID)
            .join("files")
            .exists()
    );

    let (content_type, body) = multipart("notes.txt", "text/plain", b"later");
    let (status, json) = fixture
        .post(
            "/api/project-files?projectId=missing-proj",
            &content_type,
            body,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{json}");
    assert_eq!(json["error"], "Project not found");
    assert!(
        !fixture
            .root
            .path()
            .join(".projects")
            .join("missing-proj")
            .exists()
    );
    assert_eq!(fixture.project_bytes(), before);
}
