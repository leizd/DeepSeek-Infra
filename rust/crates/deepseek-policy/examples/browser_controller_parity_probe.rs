//! browser controller/engine-state parity probe, Rust side.
//!
//! Pins `session.controller_kind` and the controller a *result* reports across
//! the states the oracle distinguishes: a session nothing has answered yet, a
//! refused action, a dispatched action, and a failed one. Also pins the
//! engine-availability answer that selects the controller.
//!
//! Run against `tasks/native-runtime/browser_controller_parity_probe.py`; the
//! two outputs must be byte-identical.
//!
//! Usage:
//!
//! ```text
//! python tasks/native-runtime/browser_controller_parity_probe.py > python.json
//! cd rust && cargo run -p deepseek-policy --example browser_controller_parity_probe > ../rust.json
//! ```
//!
//! Keys are emitted from a `BTreeMap` so the order is sorted regardless of
//! whether `serde_json` is built with `preserve_order` (which the workspace
//! turns on for some crates). The Python side uses `sort_keys=True` for the same
//! bytes.

use std::collections::BTreeMap;
use std::path::PathBuf;

use deepseek_policy::browser::{
    ENGINE_PLAYWRIGHT, execute_browser_action, playwright_available, reset_sessions_for_tests,
};
use deepseek_policy::browser_safety::BrowserSettings;
use deepseek_policy::entropy::SystemEntropy;
use serde_json::{Value, json};

const PRIVATE_URL: &str = "http://127.0.0.1:8000/private";

fn fixture() -> (String, BrowserSettings) {
    let fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/fixtures/browser/basic.html");
    assert!(fixture.exists(), "missing {}", fixture.display());
    let display = fixture
        .canonicalize()
        .unwrap_or(fixture.clone())
        .to_string_lossy()
        .replacen(r"\\?\", "", 1);
    let uri = format!("file:///{}", display.replace('\\', "/"));
    let settings = BrowserSettings {
        enabled: true,
        require_confirm: true,
        allow_private_hosts: false,
        headless: true,
        fixture_roots: vec![fixture.parent().expect("fixture dir").to_path_buf()],
    };
    (uri, settings)
}

fn text(value: &Value, key: &str) -> Value {
    json!(value.get(key).and_then(Value::as_str).unwrap_or(""))
}

fn prefixed(value: &Value) -> bool {
    value
        .as_str()
        .is_some_and(|found| found.starts_with("failed:"))
}

fn main() {
    reset_sessions_for_tests();
    let (uri, settings) = fixture();
    let entropy = SystemEntropy;
    let browser = |payload: Value| execute_browser_action(&payload, &settings, &entropy);
    let mut out: BTreeMap<String, Value> = BTreeMap::new();

    // 1. A session exists but nothing has answered: no controller is recorded yet.
    let fresh = browser(json!({"action": ""})).expect("empty action returns a payload");

    // 2. Refused on the URL, which the oracle does *before* it builds a controller.
    let blocked = browser(json!({"action": "open_url", "url": PRIVATE_URL}))
        .expect("refused action returns a payload");

    // 3. Dispatched: a controller answered, so the session records its kind.
    let opened = browser(json!({"action": "open_url", "url": uri})).expect("fixture opens");
    let session_id = opened["session"]["browserSessionId"]
        .as_str()
        .expect("session id")
        .to_string();

    // 4. Failed: `download` is not ported, so the dispatch refuses after the gate.
    let failed = browser(json!({"action": "download", "sessionId": session_id, "confirmed": true}));
    let raised = failed.is_err();

    // A refused action reads the stored session back without touching it, so it is
    // how the failed state is observable through the public API.
    let observed = browser(json!({
        "action": "open_url",
        "sessionId": session_id,
        "url": PRIVATE_URL,
    }))
    .expect("refused action returns a payload");

    // 5. A later success reports the live controller, but the recorded kind is
    //    sticky — the oracle hands back its cached controller without touching
    //    the session again.
    let read = browser(json!({"action": "read_page", "sessionId": session_id}))
        .expect("read_page on a live session");

    out.insert("blocked-code".to_string(), text(&blocked, "code"));
    out.insert(
        "blocked-controller".to_string(),
        text(&blocked["session"], "controller"),
    );
    out.insert("blocked-risk".to_string(), text(&blocked["safety"], "risk"));
    out.insert(
        "dispatched-result-controller".to_string(),
        text(&opened["result"], "controller"),
    );
    out.insert(
        "dispatched-session-controller".to_string(),
        text(&opened["session"], "controller"),
    );
    out.insert(
        "dispatched-session-status".to_string(),
        text(&opened["session"], "status"),
    );
    out.insert("engine".to_string(), json!(ENGINE_PLAYWRIGHT));
    out.insert(
        "engine-available".to_string(),
        json!(playwright_available()),
    );
    out.insert("failed-action-raised".to_string(), json!(raised));
    out.insert(
        "failed-controller-is-prefixed".to_string(),
        json!(prefixed(&observed["session"]["controller"])),
    );
    out.insert(
        "failed-status".to_string(),
        text(&observed["session"], "status"),
    );
    out.insert(
        "fresh-controller".to_string(),
        text(&fresh["session"], "controller"),
    );
    out.insert(
        "fresh-status".to_string(),
        text(&fresh["session"], "status"),
    );
    out.insert(
        "live-controller-after-failure".to_string(),
        text(&read["result"], "controller"),
    );
    out.insert(
        "sticky-controller-is-prefixed".to_string(),
        json!(prefixed(&read["session"]["controller"])),
    );

    let mut encoded = serde_json::to_string_pretty(&out).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
