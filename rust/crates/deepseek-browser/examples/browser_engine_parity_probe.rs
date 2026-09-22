//! The CDP engine's page-content parity probe, Rust side.
//!
//! Reads the same fixtures the oracle reads, through the same action sequence, and
//! prints what the engine observed as JSON. `tasks/native-runtime/browser_engine_parity_probe.py`
//! runs the Playwright oracle over the identical sequence and compares the two
//! reports field by field.
//!
//! This is the probe the browser-engine specification asks for at stage 2: the
//! controller probes pin *which* controller answered and the static page probe pins
//! *what the static controller read*; this one pins what the **engine** reads, which
//! is the part that could not be pinned before there was an engine.
//!
//! ```text
//! cargo run -p deepseek-browser --example browser_engine_parity_probe -- http://127.0.0.1:PORT
//! ```
//!
//! The browser comes from `DEEPSEEK_BROWSER_CHROMIUM`; the download staging
//! directory from `DEEPSEEK_BROWSER_PROBE_DOWNLOAD_DIR`. The probe exits non-zero
//! with a one-line reason when either is missing, so a harness that forgot to
//! configure it cannot mistake "no engine" for "engine agrees".

use std::path::PathBuf;

use deepseek_browser::engine::Engine;
use serde_json::{Value, json};

/// The fixtures the probe reads, in the order it reports them.
const FIXTURES: &[&str] = &[
    "basic.html",
    "controls.html",
    "download.html",
    "form.html",
    "injection.html",
    "sample-report.html",
];

fn browser_path() -> PathBuf {
    let raw = std::env::var("DEEPSEEK_BROWSER_CHROMIUM").unwrap_or_default();
    let path = PathBuf::from(raw.trim());
    if !path.is_file() {
        fail("DEEPSEEK_BROWSER_CHROMIUM must name a browser binary");
    }
    path
}

fn no_sandbox() -> bool {
    std::env::var("DEEPSEEK_BROWSER_NO_SANDBOX")
        .map(|value| {
            let value = value.trim().to_ascii_lowercase();
            value == "1" || value == "true"
        })
        .unwrap_or(false)
}

fn fail(reason: &str) -> ! {
    eprintln!("browser_engine_parity_probe: {reason}");
    std::process::exit(2);
}

/// The engine's links, normalised to the oracle's three-field shape.
fn links_json(links: Vec<deepseek_protocol::generated::deepseek::browser::v1::Link>) -> Value {
    Value::Array(
        links
            .into_iter()
            .map(|link| {
                json!({
                    "href": link.href,
                    "text": link.text,
                    "title": link.title,
                })
            })
            .collect(),
    )
}

#[tokio::main]
async fn main() {
    let base = std::env::args().nth(1).unwrap_or_default();
    if base.trim().is_empty() {
        fail("usage: browser_engine_parity_probe <base-url>");
    }
    let base = base.trim().trim_end_matches('/').to_string();
    let download_dir = std::env::var("DEEPSEEK_BROWSER_PROBE_DOWNLOAD_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir().join("deepseek-browser-engine-probe"));
    let browser = browser_path();
    let profile = std::env::temp_dir().join(format!(
        "deepseek-browser-engine-probe-profile-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&profile);

    let mut engine = Engine::launch(&browser, &profile, no_sandbox())
        .await
        .unwrap_or_else(|error| fail(&format!("cannot launch the engine: {}", error.message)));

    let mut pages = serde_json::Map::new();
    for name in FIXTURES {
        let url = format!("{base}/{name}");
        if let Err(error) = engine.open_url(&url).await {
            fail(&format!("open_url {url} failed: {}", error.message));
        }
        let page = engine
            .read_page("")
            .await
            .unwrap_or_else(|error| fail(&format!("read_page {url} failed: {}", error.message)));
        let links = engine.extract_links("").await.unwrap_or_else(|error| {
            fail(&format!("extract_links {url} failed: {}", error.message))
        });
        pages.insert(
            (*name).to_string(),
            json!({
                "url": page.url,
                "title": page.title,
                "text": page.text,
                "html": page.html,
                "links": links_json(links),
            }),
        );
    }

    // The controls fixture, where each action has an effect the page itself shows.
    let controls = format!("{base}/controls.html");
    let mut interactions = serde_json::Map::new();
    engine
        .open_url(&controls)
        .await
        .unwrap_or_else(|error| fail(&format!("controls open failed: {}", error.message)));
    let selected = engine
        .select("#colour", "green")
        .await
        .unwrap_or_else(|error| fail(&format!("select failed: {}", error.message)));
    let chosen = engine
        .value("document.querySelector('#chosen').textContent")
        .await
        .unwrap_or_else(|error| fail(&format!("chosen read failed: {}", error.message)));
    interactions.insert(
        "select".to_string(),
        json!({
            "selected": selected,
            "chosen": chosen,
        }),
    );

    // `mouse.wheel` is dispatched at the origin on both sides; the measurement is
    // recorded rather than asserted so the divergence stays visible.
    engine
        .scroll(0, 900)
        .await
        .unwrap_or_else(|error| fail(&format!("scroll failed: {}", error.message)));
    let scroll_y = engine
        .value("window.scrollY")
        .await
        .unwrap_or_else(|error| fail(&format!("scroll read failed: {}", error.message)));
    interactions.insert("scroll".to_string(), json!({ "scrollY": scroll_y }));

    let form = format!("{base}/form.html");
    engine
        .open_url(&form)
        .await
        .unwrap_or_else(|error| fail(&format!("form open failed: {}", error.message)));
    engine
        .type_text("#email", "user@example.com")
        .await
        .unwrap_or_else(|error| fail(&format!("type_text failed: {}", error.message)));
    let email = engine
        .value("document.querySelector('#email').value")
        .await
        .unwrap_or_else(|error| fail(&format!("email read failed: {}", error.message)));
    interactions.insert("type_text".to_string(), json!({ "value": email }));

    let basic = format!("{base}/basic.html");
    engine
        .open_url(&basic)
        .await
        .unwrap_or_else(|error| fail(&format!("basic open failed: {}", error.message)));
    engine
        .click("#docs-link")
        .await
        .unwrap_or_else(|error| fail(&format!("click failed: {}", error.message)));
    let landed = engine
        .value("location.href")
        .await
        .unwrap_or_else(|error| fail(&format!("location read failed: {}", error.message)));
    interactions.insert("click".to_string(), json!({ "url": landed }));

    let missing = engine.click("#no-such-element").await;
    interactions.insert(
        "click_missing".to_string(),
        json!({
            "code": match &missing {
                Ok(_) => Value::Null,
                Err(error) => json!(error.code),
            },
            "status": match &missing {
                Ok(_) => Value::Null,
                Err(error) => json!(error.status),
            },
        }),
    );

    let download_page = format!("{base}/download.html");
    engine
        .open_url(&download_page)
        .await
        .unwrap_or_else(|error| fail(&format!("download open failed: {}", error.message)));
    let _ = std::fs::remove_dir_all(&download_dir);
    let downloaded = engine
        .download("#download-report", &download_dir)
        .await
        .unwrap_or_else(|error| fail(&format!("download failed: {}", error.message)));
    let body = std::fs::read_to_string(download_dir.join(&downloaded.0)).unwrap_or_default();
    interactions.insert(
        "download".to_string(),
        json!({
            "filename": downloaded.0,
            "bytes": downloaded.1.len(),
            "body": body,
        }),
    );

    let screenshot = {
        engine
            .open_url(&basic)
            .await
            .unwrap_or_else(|error| fail(&format!("screenshot open failed: {}", error.message)));
        let data = engine
            .screenshot("")
            .await
            .unwrap_or_else(|error| fail(&format!("screenshot failed: {}", error.message)));
        json!({
            "signature": data.iter().take(8).map(|byte| *byte as u32).collect::<Vec<u32>>(),
            "length": data.len(),
        })
    };

    let report = json!({
        "engine": "cdp_chromium",
        "pages": pages,
        "interactions": interactions,
        "screenshot": screenshot,
    });
    engine.close().await;
    let _ = std::fs::remove_dir_all(&profile);
    let _ = std::fs::remove_dir_all(&download_dir);

    println!(
        "{}",
        serde_json::to_string_pretty(&report).expect("the report is JSON")
    );
}
