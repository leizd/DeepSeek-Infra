//! The CDP engine: a headless Chromium this process spawns, driven over its own
//! DevTools socket.
//!
//! Scope is the declared boundary, not Playwright's: `open_url`, `read_page`,
//! `extract_links`. The remaining actions refuse by name until their stage. What is
//! here mirrors the oracle's Playwright calls one for one — `goto(wait_until=
//! "domcontentloaded", 30 s)`, `inner_text("body", 2 s)`, `page.content()`,
//! `page.title()` — and where CDP cannot reproduce a Playwright semantic exactly, the
//! difference is left to the parity probe to measure rather than papered over here.

use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use deepseek_protocol::generated::deepseek::browser::v1::{Link, Page};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpStream;
use tokio::process::{Child, Command};
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

use crate::{AppError, codes};

/// The oracle's `goto` deadline.
const NAVIGATE_TIMEOUT: Duration = Duration::from_secs(30);
/// The oracle's `inner_text` deadline.
const EVALUATE_TIMEOUT: Duration = Duration::from_secs(2);
/// How long to wait for Chromium to print its DevTools socket.
const LAUNCH_TIMEOUT: Duration = Duration::from_secs(30);
/// How many of Chromium's stderr lines to keep for a launch failure.
///
/// A browser that cannot start says why on stderr — "Running as root without
/// --no-sandbox is not supported", a missing library, a locked profile. Consuming those
/// lines and then reporting only "no DevTools socket" produces a failure nobody can act
/// on, which is what the first CI run of the browser engine lane got.
const LAUNCH_DIAGNOSTIC_LINES: usize = 8;
/// How long to wait for the browser to close itself, and then for its process.
const BROWSER_CLOSE_TIMEOUT: Duration = Duration::from_secs(2);
/// Attempts to remove a session's profile once the browser is gone.
const PROFILE_REMOVE_ATTEMPTS: usize = 20;
/// How long to wait between profile-removal attempts.
const PROFILE_REMOVE_POLL: Duration = Duration::from_millis(100);
/// The oracle's `expect_download` deadline.
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(15);
/// How long to let a click's navigation settle before reading the URL.
const URL_SETTLE_TIMEOUT: Duration = Duration::from_millis(1_000);
/// How often to re-read the URL while it settles.
const URL_SETTLE_POLL: Duration = Duration::from_millis(50);
/// How long to let an animated wheel settle before reporting the scroll done.
const SCROLL_SETTLE_TIMEOUT: Duration = Duration::from_millis(1_000);
/// How often to re-read `window.scrollY` while a wheel settles.
const SCROLL_SETTLE_POLL: Duration = Duration::from_millis(50);

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

/// What the engine needs to start. Absent settings mean no engine: the gateway keeps
/// its own switch off and the static controller keeps answering.
#[derive(Debug, Clone, Default)]
pub struct EngineSettings {
    pub chromium: Option<PathBuf>,
    pub no_sandbox: bool,
}

impl EngineSettings {
    pub fn from_env(get: impl Fn(&str) -> Result<String, std::env::VarError>) -> Self {
        let chromium = get("DEEPSEEK_BROWSER_CHROMIUM")
            .ok()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .map(PathBuf::from);
        let no_sandbox = get("DEEPSEEK_BROWSER_NO_SANDBOX")
            .map(|value| {
                let value = value.trim().to_ascii_lowercase();
                value == "1" || value == "true"
            })
            .unwrap_or(false);
        Self {
            chromium,
            no_sandbox,
        }
    }

    /// The engine is available when it was given a browser that is actually there.
    pub fn availability(&self) -> Result<PathBuf, String> {
        match self.chromium.as_deref() {
            None => Err("DEEPSEEK_BROWSER_CHROMIUM is not set".to_string()),
            Some(path) if !path.is_file() => Err(format!(
                "DEEPSEEK_BROWSER_CHROMIUM is not a file: {}",
                path.display()
            )),
            Some(path) => Ok(path.to_path_buf()),
        }
    }
}

/// A launched browser and the attached page session.
pub struct Engine {
    child: Child,
    socket: Socket,
    profile_dir: PathBuf,
    session_id: String,
    events: VecDeque<Value>,
    next_id: u64,
    current_url: String,
}

impl Engine {
    /// Spawn Chromium and attach to one page.
    ///
    /// `--remote-debugging-port=0` makes Chromium pick a free port and print it, which
    /// is why the socket URL is read off stderr: a fixed port would collide between
    /// sessions, and the oracle's persistent context gives every session its own
    /// profile for the same reason.
    pub async fn launch(
        chromium: &Path,
        profile_dir: &Path,
        no_sandbox: bool,
    ) -> Result<Self, AppError> {
        tokio::fs::create_dir_all(profile_dir)
            .await
            .map_err(|error| {
                AppError::new(
                    codes::INTERNAL,
                    format!("cannot create browser profile: {error}"),
                )
            })?;
        let mut command = Command::new(chromium);
        command
            .arg("--headless=new")
            .arg("--remote-debugging-port=0")
            .arg(format!("--user-data-dir={}", profile_dir.display()))
            .arg("--no-first-run")
            .arg("--no-default-browser-check")
            .arg("--disable-gpu")
            .arg("--disable-extensions")
            .arg("--disable-background-networking")
            .arg("--disable-sync")
            .arg("--mute-audio")
            .arg("--window-size=1280,720")
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        if no_sandbox {
            command.arg("--no-sandbox");
        }
        let mut child = command.spawn().map_err(|error| {
            AppError::new(
                codes::INTERNAL,
                format!("cannot start the browser engine: {error}"),
            )
        })?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| AppError::new(codes::INTERNAL, "the browser engine has no stderr"))?;
        let mut lines = BufReader::new(stderr).lines();
        let mut socket_url = String::new();
        let mut diagnostics: VecDeque<String> = VecDeque::new();
        let read = async {
            while let Ok(Some(line)) = lines.next_line().await {
                if let Some(index) = line.find("DevTools listening on ") {
                    socket_url = line[index + "DevTools listening on ".len()..]
                        .trim()
                        .to_string();
                    break;
                }
                if !line.trim().is_empty() {
                    diagnostics.push_back(line);
                    if diagnostics.len() > LAUNCH_DIAGNOSTIC_LINES {
                        diagnostics.pop_front();
                    }
                }
            }
        };
        if timeout(LAUNCH_TIMEOUT, read).await.is_err() || socket_url.is_empty() {
            let state = match child.try_wait() {
                Ok(Some(status)) => format!("the browser exited with {status}"),
                _ => "the browser is still running".to_string(),
            };
            let detail = if diagnostics.is_empty() {
                "it printed nothing on stderr".to_string()
            } else {
                diagnostics
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>()
                    .join(" | ")
            };
            return Err(AppError::new(
                codes::INTERNAL,
                format!(
                    "the browser engine did not report a DevTools socket: {state}, and {detail}"
                ),
            ));
        }
        let (socket, _response) = tokio_tungstenite::connect_async(socket_url.as_str())
            .await
            .map_err(|error| {
                AppError::new(
                    codes::INTERNAL,
                    format!("cannot reach the browser engine socket: {error}"),
                )
            })?;
        let mut engine = Self {
            child,
            socket,
            profile_dir: profile_dir.to_path_buf(),
            session_id: String::new(),
            events: VecDeque::new(),
            next_id: 1,
            current_url: String::new(),
        };
        engine.attach_page().await?;
        Ok(engine)
    }

    /// `Target.createTarget` + `Target.attachToTarget` in flat mode: one browser
    /// socket, one page session, `sessionId` on every page-level call.
    async fn attach_page(&mut self) -> Result<(), AppError> {
        let target = self
            .call(None, "Target.createTarget", json!({"url": "about:blank"}))
            .await?;
        let target_id = target
            .get("targetId")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::new(codes::INTERNAL, "the engine created no target"))?
            .to_string();
        let attached = self
            .call(
                None,
                "Target.attachToTarget",
                json!({"targetId": target_id, "flatten": true}),
            )
            .await?;
        self.session_id = attached
            .get("sessionId")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::new(codes::INTERNAL, "the engine attached no session"))?
            .to_string();
        for domain in ["Page", "Runtime"] {
            self.call(
                Some(&self.session_id.clone()),
                &format!("{domain}.enable"),
                json!({}),
            )
            .await?;
        }
        Ok(())
    }

    /// One CDP call. Events that arrive while waiting for the answer are queued, so a
    /// later `wait_for_event` still sees them.
    async fn call(
        &mut self,
        session: Option<&str>,
        method: &str,
        params: Value,
    ) -> Result<Value, AppError> {
        let id = self.next_id;
        self.next_id += 1;
        let mut message = json!({"id": id, "method": method, "params": params});
        if let Some(session) = session {
            message["sessionId"] = json!(session);
        }
        self.socket
            .send(Message::Text(message.to_string().into()))
            .await
            .map_err(|error| {
                AppError::new(codes::INTERNAL, format!("engine write failed: {error}"))
            })?;
        loop {
            let value = self.next_message().await?;
            if value.get("id").and_then(Value::as_u64) == Some(id) {
                if let Some(error) = value.get("error") {
                    return Err(AppError::new(
                        codes::INTERNAL,
                        format!("engine refused {method}: {}", compact(error)),
                    ));
                }
                return Ok(value.get("result").cloned().unwrap_or(Value::Null));
            }
            if value.get("method").is_some() {
                self.events.push_back(value);
            }
        }
    }

    async fn next_message(&mut self) -> Result<Value, AppError> {
        loop {
            let message = self
                .socket
                .next()
                .await
                .ok_or_else(|| AppError::new(codes::INTERNAL, "the engine socket closed"))?;
            match message.map_err(|error| {
                AppError::new(codes::INTERNAL, format!("engine read failed: {error}"))
            })? {
                Message::Text(text) => {
                    return serde_json::from_str(text.as_ref()).map_err(|error| {
                        AppError::new(
                            codes::INTERNAL,
                            format!("engine sent invalid JSON: {error}"),
                        )
                    });
                }
                Message::Binary(bytes) => {
                    return serde_json::from_slice(&bytes).map_err(|error| {
                        AppError::new(
                            codes::INTERNAL,
                            format!("engine sent invalid JSON: {error}"),
                        )
                    });
                }
                Message::Ping(payload) => {
                    self.socket
                        .send(Message::Pong(payload))
                        .await
                        .map_err(|error| {
                            AppError::new(codes::INTERNAL, format!("engine write failed: {error}"))
                        })?;
                }
                _ => {}
            }
        }
    }

    async fn wait_for_event(
        &mut self,
        session: &str,
        method: &str,
        deadline: Duration,
    ) -> Result<Value, AppError> {
        let started = Instant::now();
        loop {
            if let Some(index) = self.events.iter().position(|event| {
                event.get("method").and_then(Value::as_str) == Some(method)
                    && event.get("sessionId").and_then(Value::as_str) == Some(session)
            }) {
                return Ok(self.events.remove(index).unwrap_or(Value::Null));
            }
            let remaining = deadline.checked_sub(started.elapsed()).ok_or_else(|| {
                AppError::new(
                    codes::UPSTREAM_TIMEOUT,
                    format!("engine timed out waiting for {method}"),
                )
            })?;
            let value = timeout(remaining, self.next_message())
                .await
                .map_err(|_| {
                    AppError::new(
                        codes::UPSTREAM_TIMEOUT,
                        format!("engine timed out waiting for {method}"),
                    )
                })??;
            if value.get("method").is_some() {
                self.events.push_back(value);
            }
        }
    }

    /// Mirrors `page.goto(url, wait_until="domcontentloaded", timeout=30_000)`.
    pub async fn open_url(&mut self, url: &str) -> Result<(), AppError> {
        self.call(
            Some(&self.session_id.clone()),
            "Page.navigate",
            json!({"url": url}),
        )
        .await?;
        let session = self.session_id.clone();
        self.wait_for_event(&session, "Page.domContentEventFired", NAVIGATE_TIMEOUT)
            .await?;
        // The oracle's `self._page.url` after the navigation, which may differ from the
        // requested one after a redirect.
        let url = self.value("location.href").await?;
        self.current_url = as_string(url).unwrap_or_default();
        Ok(())
    }

    /// Mirrors `page.evaluate` with `returnByValue`, under the oracle's 2 s deadline.
    pub async fn value(&mut self, expression: &str) -> Result<Value, AppError> {
        let session = self.session_id.clone();
        let result = timeout(
            EVALUATE_TIMEOUT,
            self.call(
                Some(&session),
                "Runtime.evaluate",
                json!({"expression": expression, "returnByValue": true, "awaitPromise": true}),
            ),
        )
        .await
        .map_err(|_| AppError::new(codes::UPSTREAM_TIMEOUT, "engine evaluation timed out"))??;
        if let Some(exception) = result.get("exceptionDetails") {
            return Err(AppError::new(
                codes::INTERNAL,
                format!("engine evaluation failed: {}", compact(exception)),
            ));
        }
        Ok(result
            .get("result")
            .and_then(|value| value.get("value"))
            .cloned()
            .unwrap_or(Value::Null))
    }

    pub fn current_url(&self) -> &str {
        &self.current_url
    }

    /// Mirrors `page.title()`, `page.content()` and `page.inner_text("body", 2_000)`.
    ///
    /// The document read is `documentElement.outerHTML` with the doctype put back:
    /// the DOM property never carries a doctype, and the oracle's `page.content()` is
    /// the *serialized document*, which does. Measured against the oracle on the same
    /// fixture, the two are byte-identical once the doctype is prepended — see
    /// `tasks/native-runtime/browser_engine_parity_probe.py`. A selector read is an
    /// element's own `outerHTML`, which has no doctype on either side.
    pub async fn read_page(&mut self, selector: &str) -> Result<Page, AppError> {
        let (text_expression, html_expression) = if selector.is_empty() {
            (
                "document.body === null ? '' : document.body.innerText".to_string(),
                "(() => { const doctype = document.doctype; \
                  const name = doctype ? doctype.name : 'html'; \
                  return '<!DOCTYPE ' + name + '>' + document.documentElement.outerHTML; })()"
                    .to_string(),
            )
        } else {
            let quoted = serde_json::to_string(selector).unwrap_or_else(|_| "\"\"".to_string());
            (
                format!(
                    "(() => {{ const node = document.querySelector({quoted}); return node === null ? '' : node.innerText; }})()"
                ),
                format!(
                    "(() => {{ const node = document.querySelector({quoted}); return node === null ? '' : node.outerHTML; }})()"
                ),
            )
        };
        let text = self.value(&text_expression).await?;
        let html = self.value(&html_expression).await?;
        let title = self.value("document.title").await?;
        let url = self.value("location.href").await?;
        Ok(Page {
            url: as_string(url).unwrap_or_else(|| self.current_url.clone()),
            title: as_string(title).unwrap_or_default(),
            text: as_string(text).unwrap_or_default(),
            html: as_string(html).unwrap_or_default(),
            selector: if selector.is_empty() {
                "body".to_string()
            } else {
                selector.to_string()
            },
        })
    }

    /// Mirrors the oracle's `extract_links` script, including its `innerText ||
    /// textContent` fallback and the `title` attribute.
    pub async fn extract_links(&mut self, selector: &str) -> Result<Vec<Link>, AppError> {
        let root = if selector.is_empty() {
            "document.body === null ? document.documentElement : document.body".to_string()
        } else {
            let quoted = serde_json::to_string(selector).unwrap_or_else(|_| "\"\"".to_string());
            format!(
                "(() => {{ const node = document.querySelector({quoted}); return node === null ? null : node; }})()"
            )
        };
        let expression = format!(
            "(() => {{ const root = {root}; if (root === null) return []; return Array.from(root.querySelectorAll('a[href]')).map(a => ({{ text: (a.innerText || a.textContent || '').trim(), href: a.href, title: a.title || '' }})); }})()"
        );
        let links = self.value(&expression).await?;
        let mut out = Vec::new();
        if let Some(items) = links.as_array() {
            for item in items {
                out.push(Link {
                    href: as_string(item.get("href").cloned().unwrap_or(Value::Null))
                        .unwrap_or_default(),
                    text: as_string(item.get("text").cloned().unwrap_or(Value::Null))
                        .unwrap_or_default(),
                    title: as_string(item.get("title").cloned().unwrap_or(Value::Null))
                        .unwrap_or_default(),
                });
            }
        }
        Ok(out)
    }

    /// Mirrors `page.screenshot(type="png", full_page=True)` for the page and
    /// `locator.screenshot(type="png")` for a selector.
    ///
    /// The element path is `Page.captureScreenshot` with `clip` at the node's
    /// bounding box: Playwright scrolls the element into view and captures its box,
    /// which is what `DOM.getBoxModel` reports in *page* coordinates — so the clip is
    /// taken with `captureBeyondViewport`, the same way Playwright's full-page path
    /// captures beyond the viewport.
    pub async fn screenshot(&mut self, selector: &str) -> Result<Vec<u8>, AppError> {
        let selector = selector.trim();
        let params = if selector.is_empty() {
            // `Page.getLayoutMetrics` is a *page* domain command: without the
            // `sessionId` the browser answers `-32601 wasn't found`, which is what a
            // flat-mode socket does with a page command addressed to the browser.
            let session = self.session_id.clone();
            let metrics = self
                .call(Some(&session), "Page.getLayoutMetrics", json!({}))
                .await?;
            let width = number_at(&metrics, &["cssContentSize", "width"])
                .or_else(|| number_at(&metrics, &["contentSize", "width"]))
                .unwrap_or(1280.0);
            let height = number_at(&metrics, &["cssContentSize", "height"])
                .or_else(|| number_at(&metrics, &["contentSize", "height"]))
                .unwrap_or(720.0);
            json!({
                "format": "png",
                "captureBeyondViewport": true,
                "clip": {"x": 0.0, "y": 0.0, "width": width, "height": height, "scale": 1.0},
            })
        } else {
            let box_model = self.box_model(selector).await?;
            json!({
                "format": "png",
                "captureBeyondViewport": true,
                "clip": {"x": box_model.0, "y": box_model.1, "width": box_model.2, "height": box_model.3, "scale": 1.0},
            })
        };
        let session = self.session_id.clone();
        let captured = self
            .call(Some(&session), "Page.captureScreenshot", params)
            .await?;
        let encoded = captured
            .get("data")
            .and_then(Value::as_str)
            .ok_or_else(|| AppError::new(codes::INTERNAL, "the engine captured no screenshot"))?;
        decode_base64(encoded)
    }

    /// The element's box in page coordinates, via `DOM.getBoxModel`.
    ///
    /// Playwright auto-waits for the locator; CDP does not, so a selector that is not
    /// in the document is reported as `element_not_found` rather than silently
    /// clicking the origin. That divergence is listed in the specification, not
    /// hidden: the oracle's locator would have retried until its 5 s deadline.
    async fn box_model(&mut self, selector: &str) -> Result<(f64, f64, f64, f64), AppError> {
        let session = self.session_id.clone();
        let document = self
            .call(Some(&session), "DOM.getDocument", json!({"depth": 0}))
            .await?;
        let root = document
            .get("root")
            .and_then(|root| root.get("nodeId"))
            .and_then(Value::as_u64)
            .ok_or_else(|| {
                AppError::new(codes::INTERNAL, "the engine returned no document root")
            })?;
        let found = self
            .call(
                Some(&session),
                "DOM.querySelector",
                json!({"nodeId": root, "selector": selector}),
            )
            .await?;
        let node_id = found
            .get("nodeId")
            .and_then(Value::as_u64)
            .filter(|node| *node != 0)
            .ok_or_else(|| {
                AppError::new(codes::NOT_FOUND, format!("element_not_found: {selector}"))
            })?;
        let model = self
            .call(
                Some(&session),
                "DOM.getBoxModel",
                json!({"nodeId": node_id}),
            )
            .await?;
        let quad = model
            .get("model")
            .and_then(|model| model.get("content"))
            .and_then(Value::as_array)
            .ok_or_else(|| AppError::new(codes::INTERNAL, "the engine returned no box model"))?;
        let numbers: Vec<f64> = quad.iter().filter_map(Value::as_f64).collect();
        if numbers.len() < 8 {
            return Err(AppError::new(
                codes::INTERNAL,
                "the engine returned a malformed box model",
            ));
        }
        let xs = [numbers[0], numbers[2], numbers[4], numbers[6]];
        let ys = [numbers[1], numbers[3], numbers[5], numbers[7]];
        let min_x = xs.iter().copied().fold(f64::INFINITY, f64::min);
        let max_x = xs.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        let min_y = ys.iter().copied().fold(f64::INFINITY, f64::min);
        let max_y = ys.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        Ok((min_x, min_y, max_x - min_x, max_y - min_y))
    }

    /// The viewport centre of a selector, after scrolling it into view.
    ///
    /// The scroll is not decoration: `Input.dispatchMouseEvent` takes **viewport**
    /// coordinates, and a box model is in page coordinates, so an element below the
    /// fold would otherwise be clicked at the wrong place.
    async fn element_centre(&mut self, selector: &str) -> Result<(f64, f64), AppError> {
        let (x, y, width, height) = self.box_model(selector).await?;
        let session = self.session_id.clone();
        self.call(
            Some(&session),
            "Runtime.evaluate",
            json!({
                "expression": format!(
                    "(() => {{ const node = document.querySelector({}); if (node) node.scrollIntoView({{block:'center', inline:'center'}}); }})()",
                    serde_json::to_string(selector).unwrap_or_else(|_| "\"\"".to_string())
                ),
            }),
        )
        .await?;
        let scroll_x = self.value("window.scrollX").await?.as_f64().unwrap_or(0.0);
        let scroll_y = self.value("window.scrollY").await?.as_f64().unwrap_or(0.0);
        Ok((x - scroll_x + width / 2.0, y - scroll_y + height / 2.0))
    }

    /// Refresh `current_url` after an action that may have navigated.
    ///
    /// The cached URL is what the *last* `open_url` read, so a click that follows a
    /// link would otherwise report the page it was clicked on — measured against the
    /// gateway's end-to-end path, where `click` answered the pre-click URL while the
    /// page had already moved. The read is bounded rather than immediate because a
    /// click's navigation is asynchronous: Playwright's `click` waits for the action,
    /// not for the navigation it starts, so the URL is polled briefly and the last
    /// observation wins. A page that never navigates costs one extra evaluate.
    async fn refresh_url(&mut self) {
        let deadline = Instant::now() + URL_SETTLE_TIMEOUT;
        loop {
            if let Ok(value) = self.value("location.href").await {
                if let Some(url) = as_string(value) {
                    if !url.is_empty() {
                        self.current_url = url;
                    }
                }
            }
            if Instant::now() >= deadline {
                return;
            }
            tokio::time::sleep(URL_SETTLE_POLL).await;
        }
    }

    /// Mirrors `locator.click(timeout=5_000)`.
    ///
    /// The click is a real `Input.dispatchMouseEvent` press/release at the element's
    /// viewport centre, so the page's own handlers see a trusted event. What is not
    /// reproduced is Playwright's actionability retry: a selector that never becomes
    /// visible fails immediately instead of after 5 s.
    pub async fn click(&mut self, selector: &str) -> Result<String, AppError> {
        let (x, y) = self.element_centre(selector).await?;
        let session = self.session_id.clone();
        for event_type in ["mouseMoved", "mousePressed", "mouseReleased"] {
            let mut params = json!({
                "type": event_type,
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            });
            if event_type != "mouseMoved" {
                params["buttons"] = json!(1);
            }
            self.call(Some(&session), "Input.dispatchMouseEvent", params)
                .await?;
        }
        self.refresh_url().await;
        Ok(selector.to_string())
    }

    /// Mirrors `locator.fill(text, timeout=5_000)`.
    ///
    /// Playwright's `fill` sets the value and fires `input`/`change`; `Input.insertText`
    /// would fire neither reliably for a non-focused element and would append rather
    /// than replace. So the value is assigned through the DOM and the two events are
    /// dispatched, which is what a page listening for them observes.
    pub async fn type_text(&mut self, selector: &str, text: &str) -> Result<(), AppError> {
        let quoted_selector =
            serde_json::to_string(selector).unwrap_or_else(|_| "\"\"".to_string());
        let quoted_text = serde_json::to_string(text).unwrap_or_else(|_| "\"\"".to_string());
        let expression = format!(
            "(() => {{ const node = document.querySelector({quoted_selector}); \
             if (node === null) return false; \
             node.focus(); node.value = {quoted_text}; \
             node.dispatchEvent(new Event('input', {{bubbles: true}})); \
             node.dispatchEvent(new Event('change', {{bubbles: true}})); return true; }})()"
        );
        let filled = self.value(&expression).await?;
        if filled.as_bool() != Some(true) {
            return Err(AppError::new(
                codes::NOT_FOUND,
                format!("element_not_found: {selector}"),
            ));
        }
        Ok(())
    }

    /// Mirrors `locator.select_option(value, timeout=5_000)`, which returns the list of
    /// values that were actually selected.
    pub async fn select(&mut self, selector: &str, value: &str) -> Result<Vec<String>, AppError> {
        let quoted_selector =
            serde_json::to_string(selector).unwrap_or_else(|_| "\"\"".to_string());
        let quoted_value = serde_json::to_string(value).unwrap_or_else(|_| "\"\"".to_string());
        let expression = format!(
            "(() => {{ const node = document.querySelector({quoted_selector}); \
             if (node === null) return null; \
             node.value = {quoted_value}; \
             node.dispatchEvent(new Event('input', {{bubbles: true}})); \
             node.dispatchEvent(new Event('change', {{bubbles: true}})); \
             return Array.from(node.selectedOptions || []).map(o => o.value); }})()"
        );
        let selected = self.value(&expression).await?;
        if selected.is_null() {
            return Err(AppError::new(
                codes::NOT_FOUND,
                format!("element_not_found: {selector}"),
            ));
        }
        Ok(selected
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default())
    }

    /// Mirrors `page.mouse.wheel(x, y)`, which dispatches at the current mouse
    /// position — the viewport centre, since nothing else moved it.
    /// Mirrors `page.mouse.wheel(x, y)` — *including* the part that is not obvious:
    /// Chromium animates a wheel event, so the scroll has not happened yet when the CDP
    /// call returns. Playwright's `mouse.wheel` gives its caller a page that has already
    /// scrolled; a port that returns immediately hands back `window.scrollY == 0` and
    /// the difference shows up as a scroll that did nothing.
    ///
    /// The wait is a bounded settle on the observed position, not a fixed sleep: a page
    /// that cannot scroll that far settles at once and returns without waiting.
    pub async fn scroll(&mut self, x: i32, y: i32) -> Result<(), AppError> {
        let session = self.session_id.clone();
        self.call(
            Some(&session),
            "Input.dispatchMouseEvent",
            json!({
                "type": "mouseWheel",
                "x": 0,
                "y": 0,
                "deltaX": x,
                "deltaY": y,
            }),
        )
        .await?;
        let deadline = Instant::now() + SCROLL_SETTLE_TIMEOUT;
        let mut previous: Option<i64> = None;
        while Instant::now() < deadline {
            let position = self.value("window.scrollY").await?;
            let position = position.as_i64().unwrap_or(0);
            if previous == Some(position) {
                return Ok(());
            }
            previous = Some(position);
            tokio::time::sleep(SCROLL_SETTLE_POLL).await;
        }
        Ok(())
    }

    /// Mirrors `expect_download(timeout=15_000)` around a click on `selector`.
    ///
    /// `Browser.setDownloadBehavior` with `allowAndName` is what makes the file land at
    /// `<download_dir>/<guid>` and be reported through `Browser.downloadProgress`; the
    /// deprecated `Page.setDownloadBehavior` is not used because it is not guaranteed
    /// to report progress. The returned name is the browser's own, which is the
    /// closest CDP comes to Playwright's `suggested_filename`.
    pub async fn download(
        &mut self,
        selector: &str,
        download_dir: &Path,
    ) -> Result<(String, Vec<u8>), AppError> {
        tokio::fs::create_dir_all(download_dir)
            .await
            .map_err(|error| {
                AppError::new(
                    codes::INTERNAL,
                    format!("cannot create the download directory: {error}"),
                )
            })?;
        self.call(
            None,
            "Browser.setDownloadBehavior",
            json!({
                "behavior": "allowAndName",
                "downloadPath": download_dir.to_string_lossy(),
                "eventsEnabled": true,
            }),
        )
        .await?;
        let (x, y) = self.element_centre(selector).await?;
        let session = self.session_id.clone();
        for event_type in ["mouseMoved", "mousePressed", "mouseReleased"] {
            let mut params = json!({
                "type": event_type,
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            });
            if event_type != "mouseMoved" {
                params["buttons"] = json!(1);
            }
            self.call(Some(&session), "Input.dispatchMouseEvent", params)
                .await?;
        }
        let guid = self.wait_for_download_completion(DOWNLOAD_TIMEOUT).await?;
        let path = download_dir.join(&guid);
        let data = tokio::fs::read(&path).await.map_err(|error| {
            AppError::new(
                codes::INTERNAL,
                format!("the engine reported a download that is not on disk: {error}"),
            )
        })?;
        Ok((guid, data))
    }

    /// Wait for `Browser.downloadProgress` to reach `completed` and answer its guid.
    async fn wait_for_download_completion(
        &mut self,
        deadline: Duration,
    ) -> Result<String, AppError> {
        let started = Instant::now();
        loop {
            let remaining = deadline.checked_sub(started.elapsed()).ok_or_else(|| {
                AppError::new(
                    codes::UPSTREAM_TIMEOUT,
                    "engine timed out waiting for a download",
                )
            })?;
            let value = timeout(remaining, self.next_message())
                .await
                .map_err(|_| {
                    AppError::new(
                        codes::UPSTREAM_TIMEOUT,
                        "engine timed out waiting for a download",
                    )
                })??;
            let method = value.get("method").and_then(Value::as_str).unwrap_or("");
            if method != "Browser.downloadProgress" {
                if value.get("method").is_some() {
                    self.events.push_back(value);
                }
                continue;
            }
            let params = value.get("params").cloned().unwrap_or(Value::Null);
            match params.get("state").and_then(Value::as_str) {
                Some("completed") => {
                    return Ok(params
                        .get("guid")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string());
                }
                Some("canceled") => {
                    return Err(AppError::new(
                        codes::INTERNAL,
                        "the engine canceled the download",
                    ));
                }
                _ => {}
            }
        }
    }

    /// The engine reports its own version, which is what a pinned CDP surface pins.
    pub async fn version(&mut self) -> Result<Value, AppError> {
        self.call(None, "Browser.getVersion", json!({})).await
    }

    pub fn profile_dir(&self) -> &Path {
        &self.profile_dir
    }

    /// Stop the browser and forget the session's profile.
    ///
    /// Takes `&mut self` rather than `self` so the caller can close an engine it holds
    /// behind a guard without moving it out: the session registry already removed the
    /// map entry, so no new call can reach this engine while it is being torn down.
    pub async fn close(&mut self) {
        // Ask the browser to close before taking its process away. Chromium's helper
        // processes outlive a killed parent long enough to keep a handle on
        // `--user-data-dir`, so removing the profile immediately leaves the directory
        // behind and the session leaks a profile per close. Measured by the gateway's
        // end-to-end test: it passed on Windows, where the helpers die with the parent,
        // and failed on Linux, where they do not.
        let _ = timeout(
            BROWSER_CLOSE_TIMEOUT,
            self.call(None, "Browser.close", json!({})),
        )
        .await;
        let _ = self.socket.close(None).await;
        if timeout(BROWSER_CLOSE_TIMEOUT, self.child.wait())
            .await
            .is_err()
        {
            let _ = self.child.kill().await;
        }
        for _ in 0..PROFILE_REMOVE_ATTEMPTS {
            if tokio::fs::remove_dir_all(&self.profile_dir).await.is_ok() {
                return;
            }
            tokio::time::sleep(PROFILE_REMOVE_POLL).await;
        }
    }
}

fn as_string(value: Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text),
        Value::Null => None,
        other => Some(other.to_string()),
    }
}

/// A number nested under `path`, for the layout-metrics shapes that differ between
/// Chromium revisions (`cssContentSize` on newer, `contentSize` on older).
fn number_at(value: &Value, path: &[&str]) -> Option<f64> {
    let mut current = value;
    for key in path {
        current = current.get(*key)?;
    }
    current.as_f64()
}

/// `base64.b64decode`, strict: a CDP payload that is not base64 is a protocol error,
/// not an empty screenshot.
fn decode_base64(value: &str) -> Result<Vec<u8>, AppError> {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD
        .decode(value)
        .map_err(|error| {
            AppError::new(
                codes::INTERNAL,
                format!("the engine returned invalid base64: {error}"),
            )
        })
}

fn compact(value: &Value) -> String {
    let text = value.to_string();
    text.chars().take(400).collect()
}
