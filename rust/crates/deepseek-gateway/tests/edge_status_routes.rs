//! `GET /api/edge/status` through the production router.
//!
//! The body is `edge_inference_status()` with an empty payload. Query strings
//! are ignored. Nothing under the data root is created.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "edge-status-route-token";

const EDGE_KEYS: &[&str] = &[
    "EDGE_INFERENCE_ENABLED",
    "EDGE_INFERENCE_PROVIDER",
    "EDGE_PROVIDER",
    "EDGE_MODEL_PATH",
    "EDGE_MODEL_NAME",
    "EDGE_ALLOW_MODEL_PATH_OVERRIDE",
    "EDGE_N_CTX",
    "EDGE_N_THREADS",
    "EDGE_N_GPU_LAYERS",
    "EDGE_MAX_TOKENS",
    "EDGE_TEMPERATURE",
    "EDGE_TOP_P",
    "PYTHONPATH",
];

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
        let mut pairs = vec![
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_str().expect("utf-8").to_string(),
            ),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
            ("DEEPSEEK_RUNTIME_MODE", String::new()),
            ("GO_CONTROL_ADDR", String::new()),
            ("DEEPSEEK_GO_CONTROL_URL", String::new()),
        ];
        for key in EDGE_KEYS {
            pairs.push((*key, String::new()));
        }
        let env = EnvGuard::set(&pairs);
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

fn apply_edge(overrides: &[(&str, &str)]) {
    for key in EDGE_KEYS {
        unsafe { std::env::set_var(key, "") }
    }
    for (name, value) in overrides {
        unsafe { std::env::set_var(name, value) }
    }
}

fn forward(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn oracle(root: &Path) -> Value {
    let script = r#"
import json
from deepseek_infra.infra.gateway.edge_inference import edge_inference_status
print(json.dumps({"status": 200, "body": {"ok": True, "edgeInference": edge_inference_status()}}))
"#;
    let output = Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env("AUTH_TOKEN", TEST_TOKEN)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .expect("python");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("oracle json")
}

fn suggestions(body: &Value) -> Vec<String> {
    body["edgeInference"]["suggestions"]
        .as_array()
        .expect("suggestions")
        .iter()
        .map(|item| item.as_str().expect("suggestion text").to_string())
        .collect()
}

async fn assert_oracle(fixture: &Fixture, label: &str) -> Value {
    let (status, body, _, _) = fixture.request("GET", "/api/edge/status", true).await;
    let expected = oracle(fixture.root.path());
    assert_eq!(
        status.as_u16(),
        expected["status"].as_u64().unwrap() as u16,
        "{label}"
    );
    assert_eq!(&body, &expected["body"], "{label}");
    assert_eq!(body["ok"], json!(true), "{label}");
    assert_eq!(body["edgeInference"]["loaded"], json!(false), "{label}");
    assert!(
        tree(fixture.root.path()).is_empty(),
        "{label} created {}",
        tree(fixture.root.path()).join(", ")
    );
    body
}

#[tokio::test]
async fn edge_status_matches_the_oracle_without_creating_a_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.root.path();
    let models = tempfile::tempdir().expect("models");
    std::fs::create_dir_all(models.path()).unwrap();
    let gguf = models.path().join("local-Q4_K_M.gguf");
    std::fs::write(&gguf, b"gguf-bytes").unwrap();
    let bin = models.path().join("weights.bin");
    std::fs::write(&bin, b"bin-bytes").unwrap();
    let gguf_dir = models.path().join("dir-Q3_K.gguf");
    std::fs::create_dir(&gguf_dir).unwrap();
    let missing = models.path().join("missing-Q5_0.gguf");
    let modules = tempfile::tempdir().expect("modules");
    std::fs::create_dir(modules.path().join("llama_cpp")).unwrap();

    let (status, unauthenticated, _, _) = fixture.request("GET", "/api/edge/status", false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert_eq!(unauthenticated["error"]["message"], "Auth required");
    assert!(
        tree(root).is_empty(),
        "unauthenticated request created a store"
    );

    apply_edge(&[]);
    let defaults = assert_oracle(&fixture, "defaults").await;
    assert_eq!(defaults["edgeInference"]["enabled"], json!(false));
    assert_eq!(defaults["edgeInference"]["provider"], "llama_cpp");
    assert_eq!(defaults["edgeInference"]["providerSupported"], json!(true));
    assert_eq!(defaults["edgeInference"]["available"], json!(false));
    assert_eq!(
        defaults["edgeInference"]["modelPathConfigured"],
        json!(false)
    );
    assert_eq!(defaults["edgeInference"]["modelPath"], "");
    assert_eq!(
        defaults["edgeInference"]["modelName"],
        "deepseek-r1-distill-local"
    );
    assert_eq!(defaults["edgeInference"]["nCtx"], json!(4096));
    assert_eq!(defaults["edgeInference"]["nThreads"], json!(0));
    assert_eq!(defaults["edgeInference"]["maxTokens"], json!(1024));
    assert_eq!(
        defaults["edgeInference"]["allowModelPathOverride"],
        json!(false)
    );
    assert_eq!(
        suggestions(&defaults),
        vec!["Set EDGE_INFERENCE_ENABLED=1 to enable local model routing.".to_string()]
    );
    let (query_status, query_body, _, _) = fixture
        .request("GET", "/api/edge/status?edgeProvider=fake", true)
        .await;
    assert_eq!(query_status, StatusCode::OK);
    assert_eq!(query_body, defaults, "query string changed the body");

    let (status, denied, _, allow) = fixture.request("POST", "/api/edge/status", true).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{denied}");
    assert_eq!(denied, json!({"detail": "Method Not Allowed"}));
    assert_eq!(allow.as_deref(), Some("GET, HEAD"));
    let (status, head, head_bytes, _) = fixture.request("HEAD", "/api/edge/status", true).await;
    assert_eq!(status, StatusCode::OK);
    assert!(head_bytes.is_empty(), "{head}");
    assert!(tree(root).is_empty(), "method probes created a store");

    apply_edge(&[("EDGE_INFERENCE_ENABLED", "1")]);
    let enabled = assert_oracle(&fixture, "enabled without a model").await;
    assert_eq!(enabled["edgeInference"]["enabled"], json!(true));
    assert_eq!(enabled["edgeInference"]["provider"], "llama_cpp");
    assert_eq!(
        enabled["edgeInference"]["dependencyAvailable"],
        json!(false)
    );
    assert_eq!(
        enabled["edgeInference"]["modelPathConfigured"],
        json!(false)
    );
    assert_eq!(enabled["edgeInference"]["available"], json!(false));
    assert_eq!(
        suggestions(&enabled),
        vec![
            "Install llama-cpp-python or choose another edge provider.".to_string(),
            "Set EDGE_MODEL_PATH to a local model file.".to_string(),
        ]
    );

    let gguf_bytes = std::fs::read(&gguf).unwrap();
    apply_edge(&[
        ("EDGE_INFERENCE_ENABLED", "1"),
        ("EDGE_MODEL_PATH", &forward(&gguf)),
        ("EDGE_ALLOW_MODEL_PATH_OVERRIDE", "true"),
    ]);
    let present = assert_oracle(&fixture, "existing gguf").await;
    assert_eq!(present["edgeInference"]["modelPathExists"], json!(true));
    assert_eq!(
        present["edgeInference"]["modelPathSuffixSupported"],
        json!(true)
    );
    assert_eq!(present["edgeInference"]["modelPathConfigured"], json!(true));
    assert_eq!(present["edgeInference"]["quantization"], "Q4_K_M");
    assert_eq!(
        present["edgeInference"]["allowModelPathOverride"],
        json!(true)
    );
    assert_eq!(
        present["edgeInference"]["dependencyAvailable"],
        json!(false)
    );
    assert_eq!(present["edgeInference"]["available"], json!(false));
    assert!(
        present["edgeInference"]["modelPath"]
            .as_str()
            .unwrap()
            .contains("local-Q4_K_M.gguf"),
        "{}",
        present["edgeInference"]["modelPath"]
    );
    assert_eq!(
        std::fs::read(&gguf).unwrap(),
        gguf_bytes,
        "status rewrote the model"
    );
    assert!(
        suggestions(&present)
            .iter()
            .any(|line| line.contains("llama-cpp-python")),
        "{present}"
    );

    apply_edge(&[
        ("EDGE_INFERENCE_ENABLED", "1"),
        ("EDGE_MODEL_PATH", &forward(&bin)),
    ]);
    let binary = assert_oracle(&fixture, "existing bin").await;
    assert_eq!(
        binary["edgeInference"]["modelPathSuffixSupported"],
        json!(false)
    );
    assert_eq!(binary["edgeInference"]["modelPathExists"], json!(false));
    assert_eq!(binary["edgeInference"]["modelPathConfigured"], json!(true));
    let binary_lines = suggestions(&binary);
    assert!(
        binary_lines.iter().any(|line| line.contains(".gguf")),
        "{binary}"
    );
    assert!(
        binary_lines
            .iter()
            .any(|line| line.contains("exists and is readable")),
        "{binary}"
    );
    assert_eq!(std::fs::read(&bin).unwrap(), b"bin-bytes");

    apply_edge(&[
        ("EDGE_INFERENCE_ENABLED", "1"),
        ("EDGE_MODEL_PATH", &forward(&missing)),
    ]);
    let absent = assert_oracle(&fixture, "missing gguf").await;
    assert_eq!(
        absent["edgeInference"]["modelPathSuffixSupported"],
        json!(true)
    );
    assert_eq!(absent["edgeInference"]["modelPathExists"], json!(false));
    assert_eq!(absent["edgeInference"]["quantization"], "Q5_0");
    assert!(
        suggestions(&absent)
            .iter()
            .any(|line| line.contains("exists and is readable")),
        "{absent}"
    );
    assert!(
        !suggestions(&absent)
            .iter()
            .any(|line| line.contains(".gguf")),
        "{absent}"
    );
    assert!(!missing.exists(), "status created the missing model");

    apply_edge(&[
        ("EDGE_INFERENCE_ENABLED", "1"),
        ("EDGE_MODEL_PATH", &forward(&gguf_dir)),
    ]);
    let directory = assert_oracle(&fixture, "gguf directory").await;
    assert_eq!(directory["edgeInference"]["modelPathExists"], json!(false));
    assert_eq!(
        directory["edgeInference"]["modelPathSuffixSupported"],
        json!(true)
    );
    assert_eq!(directory["edgeInference"]["quantization"], "Q3_K");

    apply_edge(&[("EDGE_INFERENCE_ENABLED", "1"), ("EDGE_PROVIDER", "fake")]);
    let fake = assert_oracle(&fixture, "provider fallback fake").await;
    assert_eq!(fake["edgeInference"]["provider"], "fake");
    assert_eq!(fake["edgeInference"]["dependencyAvailable"], json!(true));
    assert_eq!(fake["edgeInference"]["modelPathConfigured"], json!(true));
    assert_eq!(fake["edgeInference"]["modelPathExists"], json!(true));
    assert_eq!(fake["edgeInference"]["available"], json!(true));
    assert_eq!(fake["edgeInference"]["modelPath"], "");
    assert!(suggestions(&fake).is_empty(), "{fake}");

    apply_edge(&[
        ("EDGE_INFERENCE_ENABLED", "1"),
        ("EDGE_INFERENCE_PROVIDER", "mlc"),
        ("EDGE_PROVIDER", "fake"),
        ("EDGE_MODEL_PATH", "not-a-file"),
    ]);
    let mlc = assert_oracle(&fixture, "mlc path").await;
    assert_eq!(mlc["edgeInference"]["provider"], "mlc");
    assert_eq!(mlc["edgeInference"]["modelPath"], "not-a-file");
    assert_eq!(mlc["edgeInference"]["modelPathExists"], json!(true));
    assert_eq!(mlc["edgeInference"]["dependencyAvailable"], json!(false));
    assert_eq!(mlc["edgeInference"]["available"], json!(false));
    assert!(
        suggestions(&mlc)
            .iter()
            .any(|line| line.contains("mlc-llm")),
        "{mlc}"
    );

    apply_edge(&[
        ("EDGE_INFERENCE_ENABLED", "yes"),
        ("EDGE_INFERENCE_PROVIDER", "dry_run"),
        ("EDGE_PROVIDER", "fake"),
        ("EDGE_N_CTX", "1"),
        ("EDGE_N_THREADS", "-4"),
        ("EDGE_N_GPU_LAYERS", "abc"),
        ("EDGE_MAX_TOKENS", "999999"),
        ("EDGE_TEMPERATURE", "9"),
        ("EDGE_TOP_P", "0"),
        ("EDGE_MODEL_NAME", "   "),
    ]);
    let clamped = assert_oracle(&fixture, "clamps and dry_run").await;
    assert_eq!(clamped["edgeInference"]["provider"], "llama_cpp");
    assert_eq!(clamped["edgeInference"]["nCtx"], json!(512));
    assert_eq!(clamped["edgeInference"]["nThreads"], json!(0));
    assert_eq!(clamped["edgeInference"]["nGpuLayers"], json!(0));
    assert_eq!(clamped["edgeInference"]["maxTokens"], json!(16384));
    assert_eq!(clamped["edgeInference"]["temperature"], json!(2.0));
    assert_eq!(clamped["edgeInference"]["topP"], json!(0.05));
    assert_eq!(
        clamped["edgeInference"]["modelName"],
        "deepseek-r1-distill-local"
    );

    apply_edge(&[
        ("EDGE_INFERENCE_ENABLED", "1"),
        ("EDGE_INFERENCE_PROVIDER", "FAKE"),
        ("EDGE_PROVIDER", "llama_cpp"),
        ("EDGE_N_CTX", "999999999"),
        ("EDGE_TOP_P", "0"),
        ("EDGE_TEMPERATURE", "9"),
    ]);
    let fake_upper = assert_oracle(&fixture, "FAKE choice").await;
    assert_eq!(fake_upper["edgeInference"]["provider"], "fake");
    assert_eq!(fake_upper["edgeInference"]["nCtx"], json!(262144));
    assert_eq!(fake_upper["edgeInference"]["available"], json!(true));

    apply_edge(&[
        ("EDGE_INFERENCE_ENABLED", "1"),
        ("EDGE_INFERENCE_PROVIDER", "llama_cpp"),
        ("EDGE_MODEL_PATH", &forward(&gguf)),
        ("PYTHONPATH", &forward(modules.path())),
    ]);
    let planted = assert_oracle(&fixture, "pythonpath llama_cpp").await;
    assert_eq!(planted["edgeInference"]["dependencyAvailable"], json!(true));
    assert_eq!(planted["edgeInference"]["modelPathExists"], json!(true));
    assert_eq!(planted["edgeInference"]["available"], json!(true));
    assert!(
        !suggestions(&planted)
            .iter()
            .any(|line| line.contains("llama-cpp-python")),
        "{planted}"
    );
    assert!(suggestions(&planted).is_empty(), "{planted}");

    apply_edge(&[
        ("EDGE_INFERENCE_ENABLED", "1"),
        ("EDGE_INFERENCE_PROVIDER", "llama_cpp"),
        ("EDGE_MODEL_PATH", &forward(&gguf)),
    ]);
    let cleared = assert_oracle(&fixture, "pythonpath cleared").await;
    assert_eq!(
        cleared["edgeInference"]["dependencyAvailable"],
        json!(false)
    );
    assert_eq!(cleared["edgeInference"]["available"], json!(false));
    assert_eq!(cleared["edgeInference"]["modelPathExists"], json!(true));
    assert!(
        suggestions(&cleared)
            .iter()
            .any(|line| line.contains("llama-cpp-python")),
        "{cleared}"
    );

    let (status, head, head_bytes, _) = fixture.request("HEAD", "/api/edge/status", true).await;
    assert_eq!(status, StatusCode::OK);
    assert!(head_bytes.is_empty(), "{head}");
    assert_eq!(std::fs::read(&gguf).unwrap(), gguf_bytes);
    assert!(
        tree(root).is_empty(),
        "edge status created a store: {}",
        tree(root).join(", ")
    );
}
