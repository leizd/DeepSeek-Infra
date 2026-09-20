//! fetch_url parity probe, Rust side.
//!
//! Replays the same scripted corpus as
//! `tasks/native-runtime/fetch_url_parity_probe.py` through
//! `deepseek_policy::fetch_url` and prints canonical JSON.
//!
//! Usage::
//!
//!     python tasks/native-runtime/fetch_url_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example fetch_url_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use std::cell::RefCell;
use std::fs;
use std::path::PathBuf;

use deepseek_policy::app_error::AppError;
use deepseek_policy::fetch_url::{
    FetchContext, HttpResponse, MAX_FETCH_BYTES, PublicUrlTarget, ensure_public_address,
    extract_html_text, extract_readable_text, fetch_url, normalize_text, resolve_public_url,
};
use serde_json::{Map, Value, json};

const PUBLIC_IP: &str = "93.184.216.34";
const NOW: f64 = 1_700_000_000.0;
const HTML: &[u8] = b"<html><head><title>Doc</title><script>secret()</script></head><body><main><h1>Hello</h1><p>Readable page text.</p></main></body></html>";

fn public_dns(host: &str, _port: Option<u16>) -> Result<Vec<String>, AppError> {
    if host == "example.com" {
        return Ok(vec![PUBLIC_IP.to_string()]);
    }
    // Names the probe never asks this stub to resolve. A literal IP is handled
    // inside `resolve_public_url` before DNS is consulted.
    Err(AppError {
        message: "URL host could not be resolved".to_string(),
        code: deepseek_policy::app_error::codes::UPSTREAM_FAILURE,
        status: 502,
    })
}

fn error_view(error: &AppError) -> Value {
    json!({"error": error.message, "code": error.code, "status": error.status})
}

fn target_view(target: &PublicUrlTarget) -> Value {
    json!({
        "url": target.url,
        "scheme": target.scheme,
        "host": target.host,
        "port": target.port,
        "hostHeader": target.host_header,
        "requestTarget": target.request_target,
        "address": target.address,
    })
}

fn resolve_result(url: &str) -> Value {
    match resolve_public_url(url, &public_dns) {
        Ok(target) => target_view(&target),
        Err(error) => error_view(&error),
    }
}

fn address_result(address: &str) -> Value {
    match ensure_public_address(address) {
        Ok(()) => json!("ok"),
        Err(error) => error_view(&error),
    }
}

fn fetch_result(
    url: &str,
    http: &deepseek_policy::fetch_url::HttpGet<'_>,
    root: &std::path::Path,
) -> Value {
    let context = FetchContext {
        root,
        now_epoch: NOW,
        timeout_seconds: 45,
        dns: &public_dns,
        http,
    };
    match fetch_url(url, Some(&context)) {
        Ok(value) => value,
        Err(error) => error_view(&error),
    }
}

fn main() {
    let mut out = Map::new();
    let resolve_cases = [
        (
            "https-path-query-fragment",
            "https://example.com/path?x=1#fragment",
        ),
        ("explicit-default-port", "https://example.com:443/x"),
        ("http-default-port", "http://example.com/x"),
        ("explicit-http-port", "http://example.com:8080/x"),
        ("loopback", "http://127.0.0.1:8000/"),
        ("localhost", "http://localhost:8000"),
        ("local-suffix", "http://printer.local/"),
        ("ftp", "ftp://example.com"),
        ("credentials", "https://user:pass@example.com"),
        ("empty", ""),
        ("blank", "   "),
        ("no-host", "https://"),
        ("bad-port", "https://example.com:abc/"),
        ("metadata-ip", "http://169.254.169.254/"),
    ];
    for (label, url) in resolve_cases {
        out.insert(format!("resolve::{label}"), resolve_result(url));
    }

    out.insert("address::public".to_string(), address_result(PUBLIC_IP));
    out.insert("address::loopback".to_string(), address_result("127.0.0.1"));
    out.insert(
        "address::metadata".to_string(),
        address_result("169.254.169.254"),
    );
    out.insert("address::cgnat".to_string(), address_result("100.64.0.1"));
    out.insert(
        "address::mapped-loopback".to_string(),
        address_result("::ffff:127.0.0.1"),
    );

    out.insert("extract::html".to_string(), json!(extract_html_text(HTML)));
    out.insert(
        "extract::plain".to_string(),
        json!(extract_readable_text(b"just text\n\n\nmore", "text/plain")),
    );
    out.insert(
        "normalize".to_string(),
        json!(normalize_text("  a   b\n\n\n\nc  ")),
    );

    let root: PathBuf =
        std::env::temp_dir().join(format!("fetch-url-parity-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).expect("scratch");

    let hits = RefCell::new(0_u32);
    let last_target: RefCell<Option<PublicUrlTarget>> = RefCell::new(None);
    let ok_http = |target: &PublicUrlTarget, _timeout: u64| {
        *hits.borrow_mut() += 1;
        last_target.replace(Some(target.clone()));
        Ok(HttpResponse {
            status: 200,
            location: String::new(),
            content_type: "text/html; charset=utf-8".to_string(),
            body: HTML.to_vec(),
        })
    };
    out.insert(
        "fetch::first".to_string(),
        fetch_result("https://example.com/path?x=1#fragment", &ok_http, &root),
    );
    out.insert(
        "fetch::second".to_string(),
        fetch_result("https://example.com/path?x=1#fragment", &ok_http, &root),
    );
    out.insert("fetch::http-hits".to_string(), json!(*hits.borrow()));
    let pinned = last_target.borrow();
    out.insert(
        "fetch::pinned-address".to_string(),
        json!(pinned.as_ref().map(|target| target.address.clone())),
    );
    out.insert(
        "fetch::host-header".to_string(),
        json!(pinned.as_ref().map(|target| target.host_header.clone())),
    );

    let redirect_http = |_target: &PublicUrlTarget, _timeout: u64| {
        Ok(HttpResponse {
            status: 302,
            location: "http://127.0.0.1/admin".to_string(),
            content_type: String::new(),
            body: Vec::new(),
        })
    };
    out.insert(
        "fetch::redirect".to_string(),
        fetch_result("https://example.com/path", &redirect_http, &root),
    );

    let huge = vec![b'x'; MAX_FETCH_BYTES + 10];
    let huge_http = |_target: &PublicUrlTarget, _timeout: u64| {
        Ok(HttpResponse {
            status: 200,
            location: String::new(),
            content_type: "text/html".to_string(),
            body: huge.clone(),
        })
    };
    out.insert(
        "fetch::too-large".to_string(),
        fetch_result("https://example.com/huge", &huge_http, &root),
    );

    let down_http = |_target: &PublicUrlTarget, _timeout: u64| {
        Ok(HttpResponse {
            status: 503,
            location: String::new(),
            content_type: String::new(),
            body: Vec::new(),
        })
    };
    out.insert(
        "fetch::http-503".to_string(),
        fetch_result("https://example.com/down", &down_http, &root),
    );

    let _ = fs::remove_dir_all(&root);
    let mut encoded = serde_json::to_string_pretty(&Value::Object(out)).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
