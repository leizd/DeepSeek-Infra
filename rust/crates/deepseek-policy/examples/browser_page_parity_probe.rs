//! browser page-content parity probe, Rust side.
//!
//! Pins what the static controller extracts from a fixture: `title`, `text` and the
//! `links` list, for every browser fixture. The controller probe pins *which*
//! controller answered; this one pins *what it read*.
//!
//! Run against `tasks/native-runtime/browser_page_parity_probe.py`; the two outputs
//! must be byte-identical.
//!
//! ```text
//! python tasks/native-runtime/browser_page_parity_probe.py > python.json
//! cd rust && cargo run -p deepseek-policy --example browser_page_parity_probe > ../rust.json
//! ```
//!
//! Keys come from a `BTreeMap` so the order is sorted whatever `serde_json` was built
//! with; the Python side uses `sort_keys=True` for the same bytes.

use std::collections::BTreeMap;
use std::path::PathBuf;

use deepseek_policy::browser::{execute_browser_action, reset_sessions_for_tests};
use deepseek_policy::browser_safety::BrowserSettings;
use deepseek_policy::entropy::SystemEntropy;
use serde_json::{Value, json};

const FIXTURES: [&str; 5] = [
    "basic.html",
    "download.html",
    "form.html",
    "injection.html",
    "sample-report.html",
];

fn fixture_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/fixtures/browser")
}

fn fixture_uri(fixture: &std::path::Path) -> String {
    assert!(fixture.exists(), "missing {}", fixture.display());
    let display = fixture
        .canonicalize()
        .unwrap_or_else(|_| fixture.to_path_buf())
        .to_string_lossy()
        .replacen(r"\\?\", "", 1);
    format!("file:///{}", display.replace('\\', "/"))
}

fn text(value: &Value, key: &str) -> Value {
    json!(value.get(key).and_then(Value::as_str).unwrap_or(""))
}

fn main() {
    reset_sessions_for_tests();
    let root = fixture_root();
    let settings = BrowserSettings {
        enabled: true,
        require_confirm: true,
        allow_private_hosts: false,
        headless: true,
        fixture_roots: vec![root.clone()],
    };
    let entropy = SystemEntropy;

    let mut out: BTreeMap<String, Value> = BTreeMap::new();
    for name in FIXTURES {
        let uri = fixture_uri(&root.join(name));
        let opened = execute_browser_action(
            &json!({"action": "open_url", "url": uri}),
            &settings,
            &entropy,
        )
        .expect("fixture opens");
        let session_id = opened["session"]["browserSessionId"]
            .as_str()
            .unwrap_or_default()
            .to_string();
        let links = execute_browser_action(
            &json!({"action": "extract_links", "sessionId": session_id}),
            &settings,
            &entropy,
        )
        .expect("links extract")
        .get("result")
        .cloned()
        .unwrap_or(Value::Null);

        let normalized: Vec<Value> = links
            .get("links")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .map(|link| {
                        json!({
                            "href": text(link, "href"),
                            "text": text(link, "text"),
                            "title": text(link, "title"),
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();

        out.insert(
            name.to_string(),
            json!({
                "title": text(&opened["result"]["page"], "title"),
                "text": text(&opened["result"]["page"], "text"),
                "links": normalized,
            }),
        );
    }

    let mut encoded = serde_json::to_string_pretty(&out).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
