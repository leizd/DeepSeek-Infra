//! browser safety parity probe, Rust side.

use deepseek_policy::browser_safety::{BrowserSettings, evaluate_action};
use serde_json::{Map, Value, json};

fn settings(enabled: bool, confirm: bool, private: bool) -> BrowserSettings {
    BrowserSettings {
        enabled,
        require_confirm: confirm,
        allow_private_hosts: private,
        headless: true,
        fixture_roots: Vec::new(),
    }
}

fn view(payload: Value, enabled: bool) -> Value {
    evaluate_action(&payload, &settings(enabled, true, false)).to_json()
}

fn main() {
    let mut out = Map::new();
    out.insert(
        "disabled".to_string(),
        view(
            json!({"action": "open_url", "url": "https://example.com/"}),
            false,
        ),
    );
    out.insert(
        "missing-url".to_string(),
        view(json!({"action": "open_url"}), true),
    );
    out.insert(
        "private-ip".to_string(),
        view(
            json!({"action": "open_url", "url": "http://127.0.0.1:8000/private"}),
            true,
        ),
    );
    out.insert(
        "localhost".to_string(),
        view(
            json!({"action": "open_url", "url": "http://localhost/admin"}),
            true,
        ),
    );
    out.insert(
        "credentials".to_string(),
        view(
            json!({"action": "open_url", "url": "https://user:pass@example.com/"}),
            true,
        ),
    );
    out.insert(
        "public".to_string(),
        view(
            json!({"action": "open_url", "url": "https://example.com/docs"}),
            true,
        ),
    );
    out.insert(
        "click-submit".to_string(),
        view(
            json!({"action": "click", "selector": "button.submit", "reason": "Submit form"}),
            true,
        ),
    );
    out.insert(
        "type-password".to_string(),
        view(
            json!({"action": "type_text", "selector": "#password", "text": "secret"}),
            true,
        ),
    );
    out.insert(
        "click-confirmed".to_string(),
        view(
            json!({"action": "click", "selector": "ok", "confirmed": true}),
            true,
        ),
    );
    out.insert(
        "unknown".to_string(),
        view(json!({"action": "explode"}), true),
    );
    let mut encoded = serde_json::to_string_pretty(&Value::Object(out)).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
