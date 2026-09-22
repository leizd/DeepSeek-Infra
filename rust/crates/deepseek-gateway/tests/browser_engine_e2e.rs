//! The whole browser path, across a real process boundary.
//!
//! The pieces are each tested where they live: the safety gate and the session
//! registry in `deepseek-policy`, the CDP engine in `deepseek-browser`, the gRPC
//! client in this crate. This test is the one that proves they are *connected* —
//! gateway client → versioned gRPC → sidecar process → CDP → real Chromium → HTTP
//! fixture — because a seam that is only tested from one side is a seam that can be
//! wired to nothing.
//!
//! Gated on `DEEPSEEK_BROWSER_CHROMIUM`, like the engine's own live test: without a
//! browser there is nothing to drive, and the test says so rather than passing
//! quietly. Run it with:
//!
//! ```text
//! $env:DEEPSEEK_BROWSER_CHROMIUM = "C:\Program Files\Google\Chrome\Application\chrome.exe"
//! cargo test -p deepseek-gateway --test browser_engine_e2e -- --nocapture --test-threads=1
//! ```

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use deepseek_gateway::browser_engine_client::GrpcBrowserEngine;
use deepseek_policy::browser::execute_browser_action_with_engine;
use deepseek_policy::browser_engine::BrowserEngine;
use deepseek_policy::browser_safety::BrowserSettings;
use deepseek_policy::entropy::SystemEntropy;
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const FIXTURES: &[(&str, &str)] = &[
    ("/basic.html", "basic.html"),
    ("/controls.html", "controls.html"),
    ("/download.html", "download.html"),
    ("/form.html", "form.html"),
];

fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("the crate is three levels below the repository root")
        .to_path_buf()
}

fn browser_path() -> Option<PathBuf> {
    let raw = std::env::var("DEEPSEEK_BROWSER_CHROMIUM").ok()?;
    let path = PathBuf::from(raw.trim());
    path.is_file().then_some(path)
}

/// The sidecar binary, beside this test's own executable.
fn sidecar_binary() -> PathBuf {
    if let Ok(path) = std::env::var("DEEPSEEK_BROWSER_SIDECAR") {
        return PathBuf::from(path);
    }
    let mut directory = std::env::current_exe().expect("the test executable has a path");
    directory.pop();
    if directory.ends_with("deps") {
        directory.pop();
    }
    let name = if cfg!(windows) {
        "deepseek-browser.exe"
    } else {
        "deepseek-browser"
    };
    directory.join(name)
}

async fn serve_fixtures() -> (SocketAddr, tokio::task::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind loopback");
    let address = listener.local_addr().expect("local address");
    let root = repository_root()
        .join("tests")
        .join("fixtures")
        .join("browser");
    let handle = tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                return;
            };
            let root = root.clone();
            tokio::spawn(async move {
                let mut buffer = vec![0u8; 4096];
                let Ok(read) = stream.read(&mut buffer).await else {
                    return;
                };
                let request = String::from_utf8_lossy(&buffer[..read]).to_string();
                let path = request
                    .split_whitespace()
                    .nth(1)
                    .unwrap_or("/")
                    .split('?')
                    .next()
                    .unwrap_or("/")
                    .to_string();
                let name = FIXTURES
                    .iter()
                    .find(|(route, _)| *route == path)
                    .map(|(_, name)| *name);
                let response = match name {
                    Some(name) => match std::fs::read(root.join(name)) {
                        Ok(body) => format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            body.len()
                        )
                        .into_bytes()
                        .into_iter()
                        .chain(body)
                        .collect::<Vec<u8>>(),
                        Err(_) => b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec(),
                    },
                    None => b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec(),
                };
                let _ = stream.write_all(&response).await;
                let _ = stream.flush().await;
            });
        }
    });
    (address, handle)
}

/// A free loopback port, released before the sidecar binds it.
fn free_port() -> u16 {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind loopback");
    let port = listener.local_addr().expect("local address").port();
    drop(listener);
    port
}

/// The sidecar, killed when the test ends.
struct Sidecar {
    child: Child,
    profile_root: PathBuf,
}

impl Sidecar {
    fn start(browser: &Path, port: u16) -> Self {
        let binary = sidecar_binary();
        assert!(
            binary.is_file(),
            "missing sidecar binary {}; run `cargo build -p deepseek-browser` first",
            binary.display()
        );
        let profile_root =
            std::env::temp_dir().join(format!("deepseek-browser-e2e-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&profile_root);
        let child = Command::new(&binary)
            .env(
                "DEEPSEEK_BROWSER_ENGINE_LISTEN",
                format!("127.0.0.1:{port}"),
            )
            .env("DEEPSEEK_BROWSER_CHROMIUM", browser)
            .env(
                "DEEPSEEK_BROWSER_PROFILE_ROOT",
                profile_root.join("profiles"),
            )
            .env(
                "DEEPSEEK_BROWSER_DOWNLOAD_ROOT",
                profile_root.join("downloads"),
            )
            .env("DEEPSEEK_BROWSER_NO_SANDBOX", "1")
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("spawn the sidecar");
        Self {
            child,
            profile_root,
        }
    }

    fn wait_for_listen(&self, port: u16) {
        let deadline = Instant::now() + Duration::from_secs(20);
        while Instant::now() < deadline {
            if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        panic!("the sidecar never listened on 127.0.0.1:{port}");
    }
}

impl Drop for Sidecar {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.profile_root);
    }
}

fn settings(allow_private_hosts: bool, require_confirm: bool) -> BrowserSettings {
    BrowserSettings {
        enabled: true,
        require_confirm,
        allow_private_hosts,
        headless: true,
        fixture_roots: vec![repository_root().join("tests/fixtures/browser")],
    }
}

fn session_id(result: &Value) -> String {
    result["session"]["browserSessionId"]
        .as_str()
        .expect("the envelope carries a session id")
        .to_string()
}

/// The engine is constructed on a **plain** thread, not inside the test's runtime.
///
/// That is not a test artefact: `GrpcBrowserEngine::connect` builds its own
/// single-threaded runtime and blocks on it, which is exactly what the server does at
/// startup, and `Runtime::block_on` panics inside a runtime. Building it here also
/// proves the guard in `connect` is about the call site and not about the client.
fn connect_engine(port: u16) -> Option<std::sync::Arc<GrpcBrowserEngine>> {
    std::thread::spawn(move || GrpcBrowserEngine::connect(&format!("http://127.0.0.1:{port}")))
        .join()
        .expect("the connecting thread must not panic")
}

#[tokio::test(flavor = "multi_thread")]
async fn the_gateway_reaches_a_real_browser_through_the_sidecar() {
    let Some(browser) = browser_path() else {
        eprintln!(
            "browser_engine_e2e: DEEPSEEK_BROWSER_CHROMIUM is unset; \
             this run did not cross the process boundary"
        );
        return;
    };
    let (address, server) = serve_fixtures().await;
    let base = format!("http://{address}");
    let port = free_port();
    let sidecar = Sidecar::start(&browser, port);
    sidecar.wait_for_listen(port);
    let engine = connect_engine(port).expect("the sidecar must answer Status");
    assert!(engine.status(&fence()).expect("status").available);
    assert_eq!(
        engine.status(&fence()).expect("status").engine_kind,
        "cdp_chromium"
    );

    // The whole browser path runs on the blocking pool, because that is where the
    // gateway runs it: `ToolRoundExecutor::run_round` wraps `dispatch` in
    // `spawn_blocking`, and the client blocks. Running it on the runtime's own thread
    // would test a call site the server never uses — and would panic, which is how
    // this was measured rather than assumed.
    let profile_root = sidecar.profile_root.clone();
    let outcome = tokio::task::spawn_blocking(move || {
        let engine = engine;
        let base = base;
        let mut checks: Vec<String> = Vec::new();
        let permissive = settings(true, false);

        // 1. The gate still runs first, and a private host is refused before the
        //    engine is reached. `allow_private_hosts: false` is the shipped default.
        deepseek_policy::browser::reset_sessions_for_tests();
        let blocked = execute_browser_action_with_engine(
            &json!({"action": "open_url", "url": format!("{base}/basic.html")}),
            &settings(false, true),
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("a refused action is an envelope, not an error");
        assert_eq!(blocked["ok"], false);
        assert_eq!(blocked["code"], "forbidden");
        checks.push("gate refuses a private host before the engine".to_string());

        // 2. With the fixture origin allowed, the whole path runs.
        deepseek_policy::browser::reset_sessions_for_tests();
        let opened = execute_browser_action_with_engine(
            &json!({"action": "open_url", "url": format!("{base}/basic.html")}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("open_url");
        assert_eq!(opened["ok"], true);
        assert_eq!(opened["session"]["controller"], "cdp_chromium");
        assert_eq!(opened["result"]["page"]["title"], "Browser Fixture Basic");
        assert!(
            opened["result"]["page"]["text"]
                .as_str()
                .unwrap_or_default()
                .contains("Browser Control Runtime"),
            "unexpected page text: {}",
            opened["result"]["page"]["text"]
        );
        let id = session_id(&opened);
        checks.push("open_url through gRPC -> CDP -> Chromium".to_string());

        // 3. `extract_links` is served by the engine and keeps the oracle's shape.
        let links = execute_browser_action_with_engine(
            &json!({"action": "extract_links", "sessionId": id}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("extract_links");
        assert_eq!(links["result"]["links"][0]["text"], "Download fixture");
        assert_eq!(
            links["result"]["links"][0]["href"],
            format!("{base}/download.html")
        );
        checks.push("extract_links".to_string());

        // 4. `select` reaches the real page: the fixture's own change handler writes
        //    the chosen value into `#chosen`, so the effect is observable rather than
        //    assumed.
        let controls = execute_browser_action_with_engine(
            &json!({"action": "open_url", "sessionId": id, "url": format!("{base}/controls.html")}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("open_url controls");
        assert_eq!(controls["result"]["url"], format!("{base}/controls.html"));
        let selected = execute_browser_action_with_engine(
            &json!({"action": "select", "sessionId": id, "selector": "#colour", "value": "green"}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("select");
        assert_eq!(selected["result"]["selected"][0], "green");
        assert_eq!(selected["result"]["value"], "green");
        checks.push("select".to_string());

        // 5. `type_text` into the form fixture.
        execute_browser_action_with_engine(
            &json!({"action": "open_url", "sessionId": id, "url": format!("{base}/form.html")}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("open_url form");
        let typed = execute_browser_action_with_engine(
            &json!({"action": "type_text", "sessionId": id, "selector": "#email", "text": "user@example.com"}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("type_text");
        assert_eq!(typed["result"]["chars"], 16);
        checks.push("type_text".to_string());

        // 6. A selector that is not in the document is the oracle's `not_found`,
        //    carried across the gRPC boundary through the response's error detail.
        let missing = execute_browser_action_with_engine(
            &json!({"action": "click", "sessionId": id, "selector": "#no-such-element"}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect_err("a missing element must fail the action");
        assert_eq!(missing.code, deepseek_policy::app_error::codes::NOT_FOUND);
        assert_eq!(missing.status, 404);
        checks.push("a missing element is not_found across the boundary".to_string());

        // 7. `click` follows the fixture's link.
        execute_browser_action_with_engine(
            &json!({"action": "open_url", "sessionId": id, "url": format!("{base}/basic.html")}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("open_url basic");
        let clicked = execute_browser_action_with_engine(
            &json!({"action": "click", "sessionId": id, "selector": "#docs-link"}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("click");
        assert_eq!(clicked["result"]["url"], format!("{base}/download.html"));
        checks.push("click".to_string());

        // 8. `screenshot` returns real PNG bytes, and the envelope reports how many.
        let shot = execute_browser_action_with_engine(
            &json!({"action": "screenshot", "sessionId": id}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("screenshot");
        assert_eq!(shot["result"]["screenshot"]["mimeType"], "image/png");
        assert!(
            shot["result"]["screenshot"]["bytes"].as_u64().unwrap_or(0) > 1000,
            "a full-page PNG is not 1KB: {shot}"
        );
        checks.push("screenshot".to_string());

        // 9. A session the engine does not know is `not_found` — an implicit create
        //    would let one client drive another's browser.
        let unknown = execute_browser_action_with_engine(
            &json!({"action": "read_page", "sessionId": "browser_deadbeefdeadbeef"}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect_err("an unknown session must not create a browser");
        assert_eq!(unknown.code, deepseek_policy::app_error::codes::NOT_FOUND);
        checks.push("an unknown session does not create a browser".to_string());

        // 10. Closing the session tears the browser down over the wire: the registry
        //     closes, the engine's context is gone (a later call is `not_found` rather
        //     than a new browser), and the profile directory is removed.
        let profile = profile_root.join("profiles").join(&id);
        assert!(
            profile.is_dir(),
            "the engine did not create a profile at {}",
            profile.display()
        );
        let closed = execute_browser_action_with_engine(
            &json!({"action": "close_session", "sessionId": id}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect("close_session");
        assert_eq!(closed["result"]["closed"], true);
        assert_eq!(closed["session"]["status"], "closed");
        assert!(
            !profile.exists(),
            "the engine left the closed session's profile behind: {}",
            profile.display()
        );
        let after_close = execute_browser_action_with_engine(
            &json!({"action": "read_page", "sessionId": id}),
            &permissive,
            &SystemEntropy,
            Some(engine.as_ref()),
        )
        .expect_err("a closed session must not resolve to a browser");
        assert_eq!(
            after_close.code,
            deepseek_policy::app_error::codes::NOT_FOUND
        );
        checks.push("close_session".to_string());

        checks
    })
    .await
    .expect("the browser path must not panic");

    for check in &outcome {
        eprintln!("browser_engine_e2e: PASS {check}");
    }
    assert_eq!(outcome.len(), 10);

    server.abort();
    drop(sidecar);
}

fn fence() -> deepseek_policy::browser_engine::EngineFence {
    deepseek_policy::browser_engine::EngineFence::for_request("e2e", 1)
}
