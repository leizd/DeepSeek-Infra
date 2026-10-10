//! Workspace skill routes through the production router.
//!
//! Bindings and skill runs are rows on `project.json`. Analytics reads the skills
//! run journal. Writes stay refused until `DEEPSEEK_RUNTIME_MODE=python_disabled`,
//! and a refused write leaves the file bytes unchanged.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "workspace-skill-route-token";
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

    fn seed(&self) {
        let directory = self.root.path().join(".projects").join(PROJECT_ID);
        std::fs::create_dir_all(&directory).unwrap();
        let project = json!({
            "id": PROJECT_ID,
            "name": "Demo",
            "documents": [],
            "skills": {
                "enabledPacks": ["keep-pack"],
                "enabledPackVersions": [{
                    "packId": "keep-pack",
                    "version": "1",
                    "installedAt": "2026-01-01T00:00:00Z"
                }],
                "enabledSkills": ["kept-skill"],
                "defaultSkill": "kept-skill",
                "recentSkills": ["kept-skill"]
            },
            "skillRuns": [
                {"skillRunId": "run-first", "skillId": "kept-skill", "status": "completed", "projectId": PROJECT_ID},
                {"skillRunId": "run-second", "skillId": "kept-skill", "status": "completed", "projectId": PROJECT_ID}
            ],
            "savedItems": [],
            "artifacts": [],
            "createdAt": 1,
            "updatedAt": 1
        });
        std::fs::write(
            directory.join("project.json"),
            serde_json::to_vec_pretty(&project).unwrap(),
        )
        .unwrap();

        let skill_dir = self.root.path().join("skills").join("builtin");
        std::fs::create_dir_all(&skill_dir).unwrap();
        std::fs::write(
            skill_dir.join("note-skill.json"),
            serde_json::to_vec_pretty(&json!({
                "skillId": "note-skill",
                "name": "Notes",
                "description": "Take a note",
                "version": "1.0",
                "systemPrompt": "Write the note",
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
                "allowedTools": [],
                "memoryPolicy": {"scope": "none"},
                "artifactPolicy": {"types": []},
                "projectBinding": {"enabled": false}
            }))
            .unwrap(),
        )
        .unwrap();
        let pack_dir = self.root.path().join("skills").join("packs");
        std::fs::create_dir_all(&pack_dir).unwrap();
        std::fs::write(
            pack_dir.join("pack-demo.json"),
            serde_json::to_vec_pretty(&json!({
                "packId": "pack-demo",
                "name": "Demo pack",
                "description": "A pack",
                "version": "1.2.3",
                "skills": ["note-skill"]
            }))
            .unwrap(),
        )
        .unwrap();

        let runs = self.root.path().join(".skills").join("runs");
        std::fs::create_dir_all(&runs).unwrap();
        std::fs::write(
            runs.join("runs.jsonl"),
            "{\"skillRunId\":\"run-analytics-1\",\"skillId\":\"note-skill\",\"projectId\":\"proj-demo\",\"status\":\"completed\",\"latencyMs\":12,\"startedAt\":\"2026-10-03T00:00:00+00:00\",\"completedAt\":\"2026-10-03T00:00:01+00:00\"}\n",
        )
        .unwrap();
    }

    fn project_bytes(&self) -> Vec<u8> {
        std::fs::read(
            self.root
                .path()
                .join(".projects")
                .join(PROJECT_ID)
                .join("project.json"),
        )
        .unwrap_or_default()
    }

    async fn request(
        &self,
        method: &str,
        uri: &str,
        body: Option<&[u8]>,
        auth: bool,
    ) -> (StatusCode, Value) {
        let bytes = body.unwrap_or(b"");
        let mut request = Request::builder().method(method).uri(uri);
        if body.is_some() {
            request = request.header(header::CONTENT_TYPE, "application/json");
            request = request.header(header::CONTENT_LENGTH, bytes.len().to_string());
        }
        if auth {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::from(bytes.to_vec())).unwrap())
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

#[tokio::test]
async fn skill_routes_read_and_write_the_project_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new("python_disabled");
    fixture.seed();

    let (status, body) = fixture
        .request(
            "GET",
            &format!("/api/workspace/projects/{PROJECT_ID}/skills"),
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["skills"]["enabledSkills"], json!(["kept-skill"]));
    assert_eq!(body["skills"]["enabledPacks"], json!(["keep-pack"]));
    assert_eq!(body["skills"]["defaultSkill"], "kept-skill");

    let (status, body) = fixture
        .request(
            "GET",
            &format!("/api/workspace/projects/{PROJECT_ID}/skill-runs?limit=1"),
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["skillRuns"].as_array().unwrap().len(), 1);
    assert_eq!(body["skillRuns"][0]["skillRunId"], "run-first");
    assert_eq!(body["skillRuns"][0]["skillId"], "kept-skill");

    let (status, body) = fixture
        .request(
            "GET",
            &format!("/api/workspace/projects/{PROJECT_ID}/skill-runs?limit=0"),
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["skillRuns"].as_array().unwrap().len(), 2);

    let (status, body) = fixture
        .request(
            "GET",
            &format!("/api/workspace/projects/{PROJECT_ID}/skill-analytics?days=7"),
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["summary"]["scope"], "project");
    assert_eq!(body["summary"]["projectId"], PROJECT_ID);
    assert_eq!(body["summary"]["totalRuns"], 1);
    assert_eq!(body["summary"]["successRuns"], 1);
    assert_eq!(body["summary"]["recentTrend"].as_array().unwrap().len(), 7);

    let (status, body) = fixture
        .request(
            "GET",
            &format!("/api/workspace/projects/{PROJECT_ID}/skill-runs?limit=nope"),
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{body}");
    assert_eq!(body, json!({"error": "Server error", "code": "internal"}));

    let (status, _) = fixture
        .request(
            "GET",
            &format!("/api/workspace/projects/{PROJECT_ID}/skills"),
            None,
            false,
        )
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);

    let before = fixture.project_bytes();
    unsafe { std::env::set_var("DEEPSEEK_RUNTIME_MODE", "") };
    let (status, body) = fixture
        .request(
            "PATCH",
            &format!("/api/workspace/projects/{PROJECT_ID}/skills"),
            Some(br#"{"enabledSkills":["note-skill"],"defaultSkill":"note-skill"}"#),
            true,
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["code"], "NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED");
    assert_eq!(fixture.project_bytes(), before);

    unsafe { std::env::set_var("DEEPSEEK_RUNTIME_MODE", "python_disabled") };
    let (status, body) = fixture
        .request(
            "PATCH",
            &format!("/api/workspace/projects/{PROJECT_ID}/skills"),
            Some(br#"{"enabledSkills":["note-skill"],"defaultSkill":"note-skill"}"#),
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["skills"]["enabledSkills"], json!(["note-skill"]));
    assert_eq!(body["skills"]["defaultSkill"], "note-skill");
    assert_eq!(body["skills"]["enabledPacks"], json!(["keep-pack"]));
    assert_eq!(body["skills"]["recentSkills"], json!(["kept-skill"]));

    let (status, body) = fixture
        .request(
            "POST",
            &format!("/api/workspace/projects/{PROJECT_ID}/skill-packs/pack-demo/install"),
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["skills"]["enabledSkills"], json!(["note-skill"]));
    let packs = body["skills"]["enabledPacks"].as_array().unwrap();
    assert!(packs.iter().any(|pack| pack == "keep-pack"));
    assert!(packs.iter().any(|pack| pack == "pack-demo"));
    assert_eq!(
        body["skills"]["enabledPackVersions"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
}

#[tokio::test]
async fn missing_project_skill_binding_is_not_found() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new("python_disabled");
    let (status, body) = fixture
        .request(
            "GET",
            "/api/workspace/projects/missing-proj/skills",
            None,
            true,
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(body["code"], "not_found");
}
