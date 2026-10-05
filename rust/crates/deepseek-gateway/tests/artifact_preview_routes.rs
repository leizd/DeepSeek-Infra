//! `GET /api/workspace/artifacts/{artifact_id}/preview` through the production router.
//!
//! The body is `preview_artifact`. A missing store is not created.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "artifact-preview-route-token";

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

    async fn request(
        &self,
        method: &str,
        uri: &str,
        auth: bool,
    ) -> (StatusCode, Value, Vec<u8>, Option<String>) {
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
        let allow = response
            .headers()
            .get(header::ALLOW)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, body, bytes.to_vec(), allow)
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf()
}

fn tree(root: &Path) -> Vec<String> {
    let mut found = Vec::new();
    walk(root, root, &mut found);
    found.sort();
    found
}

fn walk(root: &Path, dir: &Path, found: &mut Vec<String>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let relative = path
            .strip_prefix(root)
            .unwrap_or(&path)
            .to_string_lossy()
            .replace('\\', "/");
        found.push(relative);
        if path.is_dir() {
            walk(root, &path, found);
        }
    }
}

fn write_json(path: &Path, value: &Value) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(path, serde_json::to_vec_pretty(value).unwrap()).unwrap();
}

fn oracle(root: &Path, cases: &Value) -> Value {
    let script = r#"
import json, sys
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace.artifacts import preview_artifact
cases = json.loads(sys.stdin.read())
out = []
for case in cases:
    try:
        body = preview_artifact(case["artifactId"], project_id=case.get("projectId") or "")
        out.append({"status": 200, "body": {"ok": True, **body}})
    except AppError as exc:
        out.append({"status": exc.status, "body": {"error": str(exc), "code": exc.code.value}})
    except Exception:
        out.append({"status": 500, "body": {"error": "Server error", "code": "internal"}})
json.dump(out, sys.stdout)
"#;
    let mut child = Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("python");
    let stdin = child.stdin.take().expect("stdin");
    serde_json::to_writer(stdin, cases).expect("write plan");
    let output = child.wait_with_output().expect("python");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("oracle json")
}

fn install_fixture(root: &Path) {
    let projects = root.join(".projects");
    write_json(
        &projects.join("proj-new/project.json"),
        &json!({"id": "proj-new", "name": "New", "createdAt": 10, "updatedAt": 200}),
    );
    write_json(
        &projects.join("proj-old/project.json"),
        &json!({"id": "proj-old", "name": "Old", "createdAt": 10, "updatedAt": 100}),
    );
    write_json(
        &projects.join("proj-new/artifacts.json"),
        &json!({"artifacts": [
            "skip",
            {"artifactId": "", "type": "txt", "path": "notes/note.md"},
            {
                "artifactId": "art-note",
                "type": "",
                "title": "  Hello   note  ",
                "path": "notes/note.md",
                "source": {"conversationId": "conv-1", "note": "hello"},
                "createdAtMs": 1_700_000_000_000i64,
                "updatedAtMs": 1_700_000_000_000i64,
                "version": 1
            },
            {"artifactId": "art-same", "type": "txt", "title": "Same", "path": "notes/same-new.txt"},
            {"artifactId": "art-long", "type": "txt", "title": "Long", "path": "notes/long.txt"},
            {"artifactId": "art-float", "type": "txt", "path": "notes/clock.txt", "createdAtMs": 1500.5, "version": 2.9}
        ]}),
    );
    write_json(
        &projects.join("proj-old/artifacts.json"),
        &json!({"artifacts": [
            {"artifactId": "art-same", "type": "txt", "title": "Same", "path": "notes/same-old.txt"},
            {"artifactId": "art-pdf", "type": "pdf", "title": "Doc", "path": "notes/file.pdf"},
            {"artifactId": "art-gone", "type": "txt", "title": "Gone", "path": "notes/missing.txt"}
        ]}),
    );
    let mut tail = Vec::new();
    for index in 0..=500 {
        tail.push(json!({
            "artifactId": format!("art-{index:04}"),
            "type": "txt",
            "title": "Tail",
            "path": "notes/keep.txt"
        }));
    }
    write_json(
        &projects.join("proj-tail/artifacts.json"),
        &json!({"artifacts": tail}),
    );
    write_json(
        &projects.join("proj-type/artifacts.json"),
        &json!({"artifacts": [{"artifactId": "art-type", "type": "exe", "path": "notes/note.md"}]}),
    );
    write_json(
        &projects.join("proj-path/artifacts.json"),
        &json!({"artifacts": [{"artifactId": "art-path", "type": "txt"}]}),
    );
    write_json(
        &projects.join("proj-clok/artifacts.json"),
        &json!({"artifacts": [{
            "artifactId": "art-clock",
            "type": "txt",
            "title": "Clock",
            "path": "notes/clock.txt",
            "createdAtMs": 1500.5,
            "updatedAtMs": "20",
            "version": "nope"
        }]}),
    );
    write_json(
        &projects.join("proj-hide/artifacts.json"),
        &json!({"artifacts": [{"artifactId": "art-hidden", "type": "txt", "title": "Hidden", "path": "notes/hidden.txt"}]}),
    );
    write_json(
        &projects.join("proj-repl/artifacts.json"),
        &json!({"artifacts": [{"artifactId": "art-repl", "type": "txt", "title": "Repl", "path": "notes/bad-utf8.txt"}]}),
    );
    std::fs::create_dir_all(projects.join("proj-dirx")).unwrap();
    std::fs::create_dir(projects.join("proj-dirx/artifacts.json")).unwrap();
    write_json(
        &projects.join("proj-dirx/artifacts.json/ignored.json"),
        &json!({"artifacts": [{"artifactId": "art-dirx", "type": "txt", "path": "notes/note.md"}]}),
    );
    std::fs::create_dir_all(projects.join("proj-json")).unwrap();
    std::fs::write(projects.join("proj-json/artifacts.json"), b"not-json").unwrap();
    std::fs::create_dir_all(projects.join("proj-utf8")).unwrap();
    std::fs::write(
        projects.join("proj-utf8/artifacts.json"),
        [0xff, 0xfe, b'{', b'}'],
    )
    .unwrap();

    std::fs::create_dir_all(root.join("notes")).unwrap();
    std::fs::write(
        root.join("notes/note.md"),
        "东区\napi_key=supersecret\nAuthorization: Bearer abcdefghijklmnop\n",
    )
    .unwrap();
    std::fs::write(root.join("notes/same-new.txt"), "from-new").unwrap();
    std::fs::write(root.join("notes/same-old.txt"), "from-old").unwrap();
    std::fs::write(root.join("notes/long.txt"), "a".repeat(100_001)).unwrap();
    std::fs::write(root.join("notes/clock.txt"), "clock").unwrap();
    std::fs::write(root.join("notes/hidden.txt"), "hidden").unwrap();
    std::fs::write(root.join("notes/keep.txt"), "keep").unwrap();
    std::fs::write(root.join("notes/file.pdf"), b"%PDF-1.4\n").unwrap();
    std::fs::write(root.join("notes/bad-utf8.txt"), [b'o', b'k', 0xff]).unwrap();
}

#[tokio::test]
async fn artifact_preview_matches_the_oracle_without_creating_a_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.root.path();

    let (status, unauthenticated, _, _) = fixture
        .request("GET", "/api/workspace/artifacts/art-note/preview", false)
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert_eq!(unauthenticated["error"]["message"], "Auth required");

    let (status, invalid_id, _, _) = fixture
        .request("GET", "/api/workspace/artifacts/no/preview", true)
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{invalid_id}");
    assert_eq!(invalid_id["error"], "Invalid artifact id");
    assert_eq!(invalid_id["code"], "invalid_payload");

    let (status, missing, _, _) = fixture
        .request(
            "GET",
            "/api/workspace/artifacts/art-note/preview?projectId=proj-none",
            true,
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{missing}");
    assert_eq!(missing["error"], "Artifact not found");
    assert!(
        tree(root).is_empty(),
        "empty preview created {}",
        tree(root).join(", ")
    );

    let (status, denied, _, _) = fixture
        .request("POST", "/api/workspace/artifacts/art-note/preview", true)
        .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{denied}");
    assert_eq!(denied, json!({"detail": "Method Not Allowed"}));

    let (status, download, _, _) = fixture
        .request("GET", "/api/workspace/artifacts/art-note/download", true)
        .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{download}");
    assert_eq!(download["error"]["code"], "GO_CONTROL_PROXY_NOT_READY");
    assert!(
        tree(root).is_empty(),
        "proxy created {}",
        tree(root).join(", ")
    );

    install_fixture(root);
    let before = tree(root);
    let utf8_before = std::fs::read(root.join(".projects/proj-utf8/artifacts.json")).unwrap();

    let cases = json!([
        {"artifactId": "art-note", "projectId": ""},
        {"artifactId": "art-same", "projectId": ""},
        {"artifactId": "art-same", "projectId": "proj-old"},
        {"artifactId": "art-pdf", "projectId": "proj-old"},
        {"artifactId": "art-gone", "projectId": "proj-old"},
        {"artifactId": "art-long", "projectId": "proj-new"},
        {"artifactId": "art-float", "projectId": "proj-new"},
        {"artifactId": "art-clock", "projectId": "proj-clok"},
        {"artifactId": "art-0500", "projectId": "proj-tail"},
        {"artifactId": "art-0000", "projectId": "proj-tail"},
        {"artifactId": "art-hidden", "projectId": ""},
        {"artifactId": "art-hidden", "projectId": "proj-hide"},
        {"artifactId": "art-repl", "projectId": "proj-repl"},
        {"artifactId": "art-type", "projectId": "proj-type"},
        {"artifactId": "art-path", "projectId": "proj-path"},
        {"artifactId": "art-utf8", "projectId": "proj-utf8"},
        {"artifactId": "art-dirx", "projectId": "proj-dirx"},
        {"artifactId": "art-json", "projectId": "proj-json"},
        {"artifactId": "   ", "projectId": "proj-new"},
        {"artifactId": "art-note", "projectId": "0"},
        {"artifactId": "art-note", "projectId": "   "},
        {"artifactId": "art-note", "projectId": " proj-new "}
    ]);
    let uris = [
        "/api/workspace/artifacts/art-note/preview",
        "/api/workspace/artifacts/art-same/preview",
        "/api/workspace/artifacts/art-same/preview?projectId=proj-old&projectId=proj-new",
        "/api/workspace/artifacts/art-pdf/preview?projectId=proj-old",
        "/api/workspace/artifacts/art-gone/preview?projectId=proj-old",
        "/api/workspace/artifacts/art-long/preview?projectId=proj-new",
        "/api/workspace/artifacts/art-float/preview?projectId=proj-new",
        "/api/workspace/artifacts/art-clock/preview?projectId=proj-clok",
        "/api/workspace/artifacts/art-0500/preview?projectId=proj-tail",
        "/api/workspace/artifacts/art-0000/preview?projectId=proj-tail",
        "/api/workspace/artifacts/art-hidden/preview?projectid=proj-hide",
        "/api/workspace/artifacts/art-hidden/preview?projectId=proj-hide",
        "/api/workspace/artifacts/art-repl/preview?projectId=proj-repl",
        "/api/workspace/artifacts/art-type/preview?projectId=proj-type",
        "/api/workspace/artifacts/art-path/preview?projectId=proj-path",
        "/api/workspace/artifacts/art-utf8/preview?projectId=proj-utf8",
        "/api/workspace/artifacts/art-dirx/preview?projectId=proj-dirx",
        "/api/workspace/artifacts/art-json/preview?projectId=proj-json",
        "/api/workspace/artifacts/%20%20%20/preview?projectId=proj-new",
        "/api/workspace/artifacts/art-note/preview?projectId=0",
        "/api/workspace/artifacts/art-note/preview?projectId=%20%20%20",
        "/api/workspace/artifacts/art-note/preview?projectId=%20proj-new%20",
    ];
    let mut http = Vec::new();
    for uri in uris {
        http.push(fixture.request("GET", uri, true).await);
    }
    assert_eq!(tree(root), before, "preview wrote the store");
    assert_eq!(
        std::fs::read(root.join(".projects/proj-utf8/artifacts.json")).unwrap(),
        utf8_before
    );

    let (status, head, head_bytes, _) = fixture
        .request("HEAD", "/api/workspace/artifacts/art-note/preview", true)
        .await;
    assert_eq!(status, StatusCode::OK);
    assert!(head_bytes.is_empty(), "{head}");
    let (status, head_missing, head_bytes, _) = fixture
        .request(
            "HEAD",
            "/api/workspace/artifacts/art-gone/preview?projectId=proj-old",
            true,
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(head_bytes.is_empty(), "{head_missing}");
    let (status, posted, _, allow) = fixture
        .request("POST", "/api/workspace/artifacts/art-note/preview", true)
        .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(posted, json!({"detail": "Method Not Allowed"}));
    assert_eq!(allow.as_deref(), Some("GET, HEAD"));
    assert_eq!(tree(root), before, "head or post wrote the store");

    let expected = oracle(root, &cases);
    let expected = expected.as_array().expect("oracle list");
    assert_eq!(expected.len(), http.len());
    for (index, (status, body, _, _)) in http.iter().enumerate() {
        assert_eq!(
            status.as_u16(),
            expected[index]["status"].as_u64().unwrap() as u16,
            "case {index}: {body}"
        );
        assert_eq!(body, &expected[index]["body"], "case {index}");
    }

    std::fs::create_dir_all(root.join(".projects/proj-badc")).unwrap();
    std::fs::write(
        root.join(".projects/proj-badc/project.json"),
        br#"{"createdAt": 1, "updatedAt": "nope"}"#,
    )
    .unwrap();
    let (status, bad_clock, _, _) = fixture
        .request("GET", "/api/workspace/artifacts/art-note/preview", true)
        .await;
    let clock_oracle = oracle(root, &json!([{"artifactId": "art-note", "projectId": ""}]));
    assert_eq!(
        status.as_u16(),
        clock_oracle[0]["status"].as_u64().unwrap() as u16,
        "{bad_clock}"
    );
    assert_eq!(bad_clock, clock_oracle[0]["body"]);
    std::fs::remove_dir_all(root.join(".projects/proj-badc")).unwrap();

    std::fs::create_dir(root.join(".projects/bad")).unwrap();
    let (status, bad_dir, _, _) = fixture
        .request("GET", "/api/workspace/artifacts/art-note/preview", true)
        .await;
    let bad_oracle = oracle(root, &json!([{"artifactId": "art-note", "projectId": ""}]));
    assert_eq!(
        status.as_u16(),
        bad_oracle[0]["status"].as_u64().unwrap() as u16,
        "{bad_dir}"
    );
    assert_eq!(bad_dir, bad_oracle[0]["body"]);
}
