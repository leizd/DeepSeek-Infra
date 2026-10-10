//! The CDP engine against a real browser.
//!
//! Everything else in this crate is testable without a browser: the fence
//! validation, the session registry, the error mapping. The engine itself is not —
//! it is a WebSocket conversation with a Chromium, and a mock socket would only
//! prove the mock. So this test is the one place the crate spawns a browser, and it
//! is gated rather than skipped: with `DEEPSEEK_BROWSER_CHROMIUM` unset the test
//! returns immediately and says so, so the default `cargo test` run stays offline.
//!
//! Run it with a real browser:
//!
//! ```text
//! $env:DEEPSEEK_BROWSER_CHROMIUM = "C:\Program Files\Google\Chrome\Application\chrome.exe"
//! cargo test -p deepseek-browser --test engine_live -- --nocapture
//! ```
//!
//! # What it proves
//!
//! The fixture pages are served over loopback HTTP rather than `file://` on
//! purpose: a `file://` page has no origin, so `fetch`/`Blob` download fixtures
//! behave differently and a navigation test would be measuring the file scheme
//! rather than the engine. Each action is checked by the *observable* effect — the
//! text a page shows after a `select`, the value a field holds after `type_text` —
//! not by the call returning `Ok`.

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use deepseek_browser::engine::Engine;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// The fixture files this test serves, by request path.
const FIXTURES: &[(&str, &str)] = &[
    ("/basic.html", "basic.html"),
    ("/controls.html", "controls.html"),
    ("/form.html", "form.html"),
    ("/download.html", "download.html"),
];

fn repository_root() -> PathBuf {
    // `rust/crates/deepseek-browser` -> repository root.
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

fn no_sandbox() -> bool {
    std::env::var("DEEPSEEK_BROWSER_NO_SANDBOX")
        .map(|value| {
            let value = value.trim().to_ascii_lowercase();
            value == "1" || value == "true"
        })
        .unwrap_or(false)
}

/// A loopback HTTP server for the fixture directory.
///
/// Deliberately hand-rolled: the alternative is a dev-dependency on a web
/// framework to serve four static files, and a test that pulls in a server crate
/// is a test whose failures can come from the server crate.
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

/// A profile directory that is removed when the test ends, pass or fail.
struct TempProfile(PathBuf);

impl TempProfile {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!(
            "deepseek-browser-engine-live-{label}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&path);
        Self(path)
    }
}

impl Drop for TempProfile {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

async fn launch(browser: &Path, profile: &TempProfile) -> Engine {
    Engine::launch(browser, &profile.0, no_sandbox())
        .await
        .expect("the engine must drive a real browser")
}

#[tokio::test]
async fn the_engine_drives_a_real_browser_through_the_declared_actions() {
    let Some(browser) = browser_path() else {
        eprintln!(
            "engine_live: DEEPSEEK_BROWSER_CHROMIUM is unset; \
             this run did not exercise a browser"
        );
        return;
    };
    let (address, server) = serve_fixtures().await;
    let base = format!("http://{address}");
    let profile = TempProfile::new("actions");
    let mut engine = launch(&browser, &profile).await;

    // open_url + read_page: the fixture's own title and body text, read back
    // through `innerText` the way the oracle reads it. The expected text is the
    // oracle's measured output for this fixture (`page.inner_text("body")`), blank
    // lines and all — `innerText` and Playwright's normaliser agree here, and a
    // tidied-up expectation would have hidden that they agree.
    engine
        .open_url(&format!("{base}/basic.html"))
        .await
        .expect("navigation must succeed");
    assert_eq!(engine.current_url(), format!("{base}/basic.html"));
    let page = engine.read_page("").await.expect("read_page");
    assert_eq!(page.title, "Browser Fixture Basic");
    assert_eq!(
        page.text,
        "Browser Control Runtime\n\n\
         Browser snapshots become Media Library objects and Local RAG chunks.\n\n\
         Download fixture Reference"
    );
    assert_eq!(page.selector, "body");
    // The document read carries the doctype the DOM property drops, which is what
    // makes it comparable to the oracle's `page.content()`. Byte equality with the
    // oracle is pinned by `browser_engine_parity_probe.py`; here the shape is checked
    // so a regression names itself.
    assert!(page.html.starts_with("<!DOCTYPE html><html lang=\"en\">"));
    assert!(page.html.ends_with("</html>"));
    assert!(page.html.contains("<main id=\"content\">"));

    // extract_links: absolute hrefs resolved against the page, with the anchor text
    // trimmed and the `title` attribute carried.
    let links = engine.extract_links("").await.expect("extract_links");
    assert_eq!(links.len(), 2);
    assert_eq!(links[0].text, "Download fixture");
    assert_eq!(links[0].href, format!("{base}/download.html"));
    assert_eq!(links[0].title, "");
    assert_eq!(links[1].text, "Reference");
    assert_eq!(links[1].href, "https://example.com/reference");

    // A selector read returns the element's own outerHTML and innerText, not the
    // document's.
    let scoped = engine.read_page("#content").await.expect("scoped read");
    assert_eq!(scoped.selector, "#content");
    assert!(scoped.html.starts_with("<main id=\"content\">"));
    assert!(scoped.text.starts_with("Browser Control Runtime"));

    // select: the fixture's own change handler writes the chosen value into
    // `#chosen`, so the effect is observable in the page rather than in the call.
    engine
        .open_url(&format!("{base}/controls.html"))
        .await
        .expect("navigation must succeed");
    let selected = engine
        .select("#colour", "green")
        .await
        .expect("select must reach the element");
    assert_eq!(selected, vec!["green".to_string()]);
    let chosen = engine
        .value("document.querySelector('#chosen').textContent")
        .await;
    assert_eq!(chosen.expect("evaluate").as_str(), Some("green"));

    // type_text: the value is set and `input`/`change` are dispatched, which is what
    // a page listening for them observes.
    engine
        .open_url(&format!("{base}/form.html"))
        .await
        .expect("navigation must succeed");
    engine
        .type_text("#email", "user@example.com")
        .await
        .expect("type_text must reach the element");
    let typed = engine.value("document.querySelector('#email').value").await;
    assert_eq!(typed.expect("evaluate").as_str(), Some("user@example.com"));

    // click: the fixture's link is followed, so the URL is the evidence.
    engine
        .open_url(&format!("{base}/basic.html"))
        .await
        .expect("navigation must succeed");
    engine.click("#docs-link").await.expect("click");
    let landed = engine.value("location.href").await;
    assert_eq!(
        landed.expect("evaluate").as_str(),
        Some(format!("{base}/download.html").as_str())
    );

    // A selector that is not in the document is `element_not_found`, not a click at
    // the origin.
    let missing = engine.click("#no-such-element").await;
    assert_eq!(
        missing.expect_err("a missing element must fail").code,
        deepseek_browser::codes::NOT_FOUND
    );

    // scroll: `page.mouse.wheel(x, y)` dispatches at the *current mouse position*,
    // which Playwright leaves at the origin. Chromium animates the wheel, so a read
    // straight afterwards measures the race rather than the scroll — the oracle passes
    // through that same window, which is how this test first recorded `0` on both
    // sides. Settled on both sides, the oracle lands at 900 on this fixture, and the
    // engine answers only once the position has stopped moving.
    engine
        .open_url(&format!("{base}/controls.html"))
        .await
        .expect("navigation must succeed");
    engine.scroll(0, 900).await.expect("scroll");
    let scrolled = engine.value("window.scrollY").await;
    assert_eq!(
        scrolled.expect("evaluate").as_f64(),
        Some(900.0),
        "an animated wheel must have landed before scroll returns"
    );

    // screenshot: a real PNG, not a placeholder. The signature is checked rather
    // than the bytes, which no two Chromium builds agree on.
    let page_png = engine.screenshot("").await.expect("full-page screenshot");
    assert!(page_png.starts_with(b"\x89PNG\r\n\x1a\n"));
    assert!(page_png.len() > 1000, "a 1280x3000 page is not 1KB");
    let element_png = engine
        .screenshot("#top-link")
        .await
        .expect("element screenshot");
    assert!(element_png.starts_with(b"\x89PNG\r\n\x1a\n"));
    assert!(element_png.len() < page_png.len());

    // download: the fixture builds a Blob and the anchor downloads it. The engine
    // reports the browser's own file name and the bytes that landed on disk.
    let downloads = TempProfile::new("downloads");
    std::fs::create_dir_all(&downloads.0).expect("download dir");
    engine
        .open_url(&format!("{base}/download.html"))
        .await
        .expect("navigation must succeed");
    let (filename, data) = engine
        .download("#download-report", &downloads.0)
        .await
        .expect("download");
    // The browser's own name for the file, which under `allowAndName` is a GUID: the
    // oracle's `suggested_filename` is the link's `download` attribute instead. That
    // divergence is in the specification's non-equal list, so the name is checked for
    // shape and the *bytes* are checked for content.
    assert!(
        uuid_shaped(&filename),
        "expected the browser's own file name, got {filename:?}"
    );
    let text = String::from_utf8_lossy(&data);
    assert!(
        text.contains("Sample report"),
        "unexpected download body: {text}"
    );

    engine.close().await;
    server.abort();
}

#[tokio::test]
async fn a_browser_that_cannot_be_started_is_reported_not_panicked() {
    let profile = TempProfile::new("bad-binary");
    let missing = profile.0.join("no-such-browser");
    let error = match Engine::launch(&missing, &profile.0, false).await {
        Ok(_) => panic!("a missing binary must be an error"),
        Err(error) => error,
    };
    assert_eq!(error.code, deepseek_browser::codes::INTERNAL);
    assert!(error.message.contains("cannot start the browser engine"));
}

/// The engine and its `Arc`-shared service are `Send`: the gateway holds the service
/// across await points on a multi-threaded runtime.
#[allow(dead_code)]
fn the_service_is_shareable() {
    fn assert_send<T: Send + Sync>() {}
    assert_send::<Arc<Engine>>();
}

/// `Browser.setDownloadBehavior("allowAndName")` names each file with a GUID.
fn uuid_shaped(value: &str) -> bool {
    let parts: Vec<&str> = value.split('-').collect();
    parts.len() == 5
        && [8, 4, 4, 4, 12]
            .iter()
            .zip(parts.iter())
            .all(|(width, part)| {
                part.len() == *width && part.chars().all(|c| c.is_ascii_hexdigit())
            })
}
