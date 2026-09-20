//! Browser tool family: safety gate + in-memory sessions + static HTML controller.
//!
//! Mirrors `execute_browser_action` with the oracle's **StaticController**
//! fallback (used when Playwright is absent). Playwright itself is not ported.
//! Media/RAG snapshot writes stay Python-owned; read_page still returns the
//! page text.
//!
//! The session records **which controller answered**, exactly as the oracle's
//! `controller_kind` does: `unstarted` until the first dispatch creates a
//! controller, then that controller's kind, or `failed:<message>` once an action
//! raised. A result carries the live controller kind; the session dict carries
//! the sticky recorded one — so the two disagree after a failure, which is what
//! the oracle does too (`controller_for` returns the cached controller without
//! touching the session again).

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use regex::Regex;
use serde_json::{Value, json};

use crate::app_error::{AppError, codes};
use crate::browser_safety::{self, BrowserSettings, evaluate_action, file_url_path};
use crate::core_utils::{python_truthy, utc_now_iso};
use crate::entropy::Entropy;
use crate::python_json::value_str;

/// The engine a session requests (`BrowserSession.engine`), and the controller
/// kind the oracle reports when that engine is available. One string, two roles —
/// `_create_controller` compares the engine to the controller kind.
pub const ENGINE_PLAYWRIGHT: &str = "playwright";
/// `BrowserSession.controller_kind` before any controller exists
/// (`infra/browser/session.py`).
pub const CONTROLLER_UNSTARTED: &str = "unstarted";
/// `StaticController.kind` — the oracle's controller when Playwright is absent.
pub const CONTROLLER_STATIC_FALLBACK: &str = "static_fallback";
/// `mark_failed` prefixes the recorded kind with this.
const FAILED_KIND_PREFIX: &str = "failed:";
/// `mark_failed` truncates the message to this many characters.
const FAILED_KIND_MESSAGE_CHARS: usize = 120;

/// Mirrors `controller.playwright_available()`.
///
/// The oracle answers this by importing `playwright.sync_api`; the native
/// runtime has no Playwright engine — no driver, no Chromium control — so the
/// only truthful answer is `false` and the static fallback is the only
/// controller. This is deliberately a constant rather than a probe that could
/// discover an engine: a deployment must not be able to make native report a
/// controller it cannot actually run. Porting the engine is what changes this,
/// and `controller_kind_for` is the single place that would follow.
pub fn playwright_available() -> bool {
    false
}

/// Mirrors `controller._create_controller`.
fn controller_kind_for(session: &BrowserSession) -> &'static str {
    if session.engine == ENGINE_PLAYWRIGHT && playwright_available() {
        ENGINE_PLAYWRIGHT
    } else {
        CONTROLLER_STATIC_FALLBACK
    }
}

#[derive(Debug, Clone)]
struct BrowserSession {
    browser_session_id: String,
    project_id: String,
    status: String,
    current_url: String,
    created_at: String,
    updated_at: String,
    headless: bool,
    /// The engine the session asked for; `_create_controller` compares this to
    /// the controller it can actually build.
    engine: String,
    /// Sticky: `unstarted`, then the controller kind, then possibly
    /// `failed:<message>`. Distinct from the kind a result reports.
    controller_kind: String,
    html: String,
    title: String,
    links: Vec<Value>,
}

impl BrowserSession {
    fn to_json(&self) -> Value {
        json!({
            "browserSessionId": self.browser_session_id,
            "projectId": self.project_id,
            "status": self.status,
            "currentUrl": self.current_url,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "headless": self.headless,
            "engine": self.engine,
            "controller": self.controller_kind,
        })
    }
}

fn sessions() -> &'static Mutex<HashMap<String, BrowserSession>> {
    static SESSIONS: OnceLock<Mutex<HashMap<String, BrowserSession>>> = OnceLock::new();
    SESSIONS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn now_iso() -> String {
    let epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0);
    utc_now_iso(epoch).replace("+00:00", "Z")
}

fn python_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => String::new(),
    }
}

fn validate_session_id(value: &str) -> Result<String, AppError> {
    let safe = value.trim();
    let valid = (4..=80).contains(&safe.chars().count())
        && safe
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-'));
    if !valid {
        return Err(AppError {
            message: "Invalid browser session id".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    Ok(safe.to_string())
}

fn create_session(
    project_id: &str,
    headless: Option<bool>,
    settings: &BrowserSettings,
    entropy: &dyn Entropy,
) -> Result<BrowserSession, AppError> {
    let session_id = format!("browser_{}", entropy.new_id()?);
    let now = now_iso();
    let session = BrowserSession {
        browser_session_id: session_id.clone(),
        project_id: project_id.trim().to_string(),
        status: "idle".to_string(),
        current_url: String::new(),
        created_at: now.clone(),
        updated_at: now,
        headless: headless.unwrap_or(settings.headless),
        engine: ENGINE_PLAYWRIGHT.to_string(),
        controller_kind: CONTROLLER_UNSTARTED.to_string(),
        html: String::new(),
        title: String::new(),
        links: Vec::new(),
    };
    sessions()
        .lock()
        .expect("browser session lock")
        .insert(session_id, session.clone());
    Ok(session)
}

fn get_session(session_id: &str) -> Result<BrowserSession, AppError> {
    let safe = validate_session_id(session_id)?;
    let map = sessions().lock().expect("browser session lock");
    match map.get(&safe) {
        Some(session) if session.status != "closed" => Ok(session.clone()),
        _ => Err(AppError {
            message: "Browser session not found".to_string(),
            code: codes::NOT_FOUND,
            status: 404,
        }),
    }
}

fn put_session(session: BrowserSession) {
    sessions()
        .lock()
        .expect("browser session lock")
        .insert(session.browser_session_id.clone(), session);
}

fn close_session(session_id: &str) -> Result<BrowserSession, AppError> {
    let safe = validate_session_id(session_id)?;
    let mut map = sessions().lock().expect("browser session lock");
    if let Some(session) = map.get_mut(&safe) {
        session.status = "closed".to_string();
        session.updated_at = now_iso();
        return Ok(session.clone());
    }
    Ok(BrowserSession {
        browser_session_id: safe,
        project_id: String::new(),
        status: "closed".to_string(),
        current_url: String::new(),
        created_at: now_iso(),
        updated_at: now_iso(),
        headless: true,
        engine: ENGINE_PLAYWRIGHT.to_string(),
        controller_kind: CONTROLLER_UNSTARTED.to_string(),
        html: String::new(),
        title: String::new(),
        links: Vec::new(),
    })
}

/// Mirrors `session.mark_failed`.
fn mark_failed(session: &mut BrowserSession, message: &str) {
    session.status = "failed".to_string();
    session.updated_at = now_iso();
    if !message.is_empty() {
        let truncated: String = message.chars().take(FAILED_KIND_MESSAGE_CHARS).collect();
        session.controller_kind = format!("{FAILED_KIND_PREFIX}{truncated}");
    }
}

#[derive(Debug, Clone)]
struct ParsedHtml {
    title: String,
    text: String,
    links: Vec<Value>,
}

fn parse_html(html: &str, base_url: &str) -> ParsedHtml {
    let title = capture(r"(?is)<title[^>]*>(.*?)</title>", html)
        .map(|text| strip_tags(&text))
        .unwrap_or_default();
    let without_script =
        ["script", "style", "noscript"]
            .iter()
            .fold(html.to_string(), |acc, tag| {
                Regex::new(&format!(r"(?is)<{tag}\b[^>]*>.*?</{tag}>"))
                    .expect("static regex")
                    .replace_all(&acc, "")
                    .into_owned()
            });
    let text = strip_tags(&without_script);
    let href_re = Regex::new(r#"(?is)<a\b[^>]*\bhref=["']([^"']+)["'][^>]*>(.*?)</a>"#)
        .expect("static regex");
    let mut links = Vec::new();
    for caps in href_re.captures_iter(html) {
        let href = join_url(base_url, caps.get(1).map(|m| m.as_str()).unwrap_or(""));
        let text = strip_tags(caps.get(2).map(|m| m.as_str()).unwrap_or(""));
        links.push(json!({"href": href, "text": text, "title": ""}));
    }
    ParsedHtml { title, text, links }
}

fn capture(pattern: &str, html: &str) -> Option<String> {
    Regex::new(pattern)
        .ok()?
        .captures(html)
        .and_then(|caps| caps.get(1).map(|m| m.as_str().to_string()))
}

fn strip_tags(value: &str) -> String {
    Regex::new(r"(?is)<[^>]+>")
        .expect("static regex")
        .replace_all(value, " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn join_url(base: &str, href: &str) -> String {
    if href.contains("://") {
        return href.to_string();
    }
    if let Some(idx) = base.rfind('/') {
        format!("{}{href}", &base[..=idx])
    } else {
        href.to_string()
    }
}

fn html_for_selector(html: &str, selector: &str) -> String {
    let selector = selector.trim();
    if selector.is_empty() {
        return html.to_string();
    }
    if let Some(id) = selector.strip_prefix('#') {
        return tag_by_attr(html, "id", id);
    }
    if let Some(class) = selector.strip_prefix('.') {
        return tag_by_class(html, class);
    }
    tag_by_name(html, selector)
}

fn tag_by_attr(html: &str, attr: &str, expected: &str) -> String {
    let attr = regex::escape(attr);
    let expected = regex::escape(expected);
    let pattern = format!(
        r#"(?is)<[A-Za-z][\w:-]*\b(?=[^>]*\b{attr}=["']{expected}["'])[^>]*>.*?</[A-Za-z][\w:-]*>"#
    );
    first_regex(html, &pattern)
}

fn tag_by_class(html: &str, class_name: &str) -> String {
    let class_name = regex::escape(class_name);
    let pattern = format!(
        r#"(?is)<[A-Za-z][\w:-]*\b(?=[^>]*\bclass=["'][^"']*\b{class_name}\b[^"']*["'])[^>]*>.*?</[A-Za-z][\w:-]*>"#
    );
    first_regex(html, &pattern)
}

fn tag_by_name(html: &str, tag: &str) -> String {
    let safe = regex::escape(tag.split_whitespace().next().unwrap_or("body"));
    first_regex(html, &format!(r"(?is)<{safe}\b[^>]*>.*?</{safe}>"))
}

fn first_regex(html: &str, pattern: &str) -> String {
    Regex::new(pattern)
        .ok()
        .and_then(|re| re.find(html).map(|m| m.as_str().to_string()))
        .unwrap_or_default()
}

fn open_static(url: &str) -> Result<(String, ParsedHtml, String), AppError> {
    if !url.starts_with("file:") {
        return Err(AppError {
            message: "static browser controller only reads approved file:// fixtures".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let rest = url.split_once(':').map(|(_, rest)| rest).unwrap_or(url);
    let disk = file_url_path(rest);
    let html = std::fs::read_to_string(&disk).map_err(|error| AppError {
        message: format!("cannot read fixture: {error}"),
        code: codes::NOT_FOUND,
        status: 404,
    })?;
    let parsed = parse_html(&html, url);
    Ok((html, parsed, url.to_string()))
}

fn apply_page(session: &mut BrowserSession, url: &str, html: String, parsed: ParsedHtml) {
    session.current_url = url.to_string();
    session.html = html;
    session.title = parsed.title;
    session.links = parsed.links;
    session.status = "idle".to_string();
    session.updated_at = now_iso();
}

/// Mirrors `execute_browser_action`.
pub fn execute_browser_action(
    payload: &Value,
    settings: &BrowserSettings,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let Some(object) = payload.as_object() else {
        return Err(AppError {
            message: "Browser action payload must be an object".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    let action = browser_safety::normalize_action(payload.get("action"));
    let session_id = python_or_empty(payload.get("sessionId")).trim().to_string();
    if action == "close_session" {
        let closed = close_session(&session_id)?;
        return Ok(json!({
            "ok": true,
            "session": closed.to_json(),
            "result": {"closed": true},
        }));
    }
    let mut session = if action == "open_url" && session_id.is_empty() {
        create_session(
            &python_or_empty(payload.get("projectId")),
            payload.get("headless").map(python_truthy),
            settings,
            entropy,
        )?
    } else if !action.is_empty() {
        get_session(&session_id)?
    } else {
        create_session(
            &python_or_empty(payload.get("projectId")),
            payload.get("headless").map(python_truthy),
            settings,
            entropy,
        )?
    };
    let mut request = object.clone();
    request.insert("action".to_string(), json!(action));
    request.insert("sessionId".to_string(), json!(session.browser_session_id));
    request.insert(
        "projectId".to_string(),
        json!(if session.project_id.is_empty() {
            python_or_empty(payload.get("projectId"))
        } else {
            session.project_id.clone()
        }),
    );
    request.insert("currentUrl".to_string(), json!(session.current_url));
    let request_val = Value::Object(request);
    let decision = evaluate_action(&request_val, settings);
    if !decision.allowed() {
        return Ok(json!({
            "ok": false,
            "code": if decision.needs_confirmation() { "requires_confirmation" } else { "forbidden" },
            "error": if decision.needs_confirmation() {
                "Browser action requires confirmation"
            } else {
                "Browser action blocked by safety policy"
            },
            "session": session.to_json(),
            "safety": decision.to_json(),
        }));
    }
    session.status = "running".to_string();
    // Mirrors `controller_for`, the first thing `_dispatch` does: the controller
    // is created once per session, so the recorded kind is sticky — if a later
    // action fails, `mark_failed` overwrites it and a subsequent success does
    // not restore it, because the oracle hands back the cached controller
    // without touching the session again.
    let live_kind = controller_kind_for(&session);
    if session.controller_kind == CONTROLLER_UNSTARTED {
        session.controller_kind = live_kind.to_string();
    }
    let result = match dispatch_action(&action, payload, &mut session, live_kind) {
        Ok(result) => result,
        Err(error) => {
            mark_failed(&mut session, &error.message);
            put_session(session);
            return Err(error);
        }
    };
    session.status = "idle".to_string();
    if let Some(url) = result.get("url").and_then(Value::as_str) {
        session.current_url = url.to_string();
    }
    session.updated_at = now_iso();
    put_session(session.clone());
    Ok(json!({
        "ok": true,
        "session": session.to_json(),
        "safety": decision.to_json(),
        "result": result,
    }))
}

fn dispatch_action(
    action: &str,
    payload: &Value,
    session: &mut BrowserSession,
    controller_kind: &str,
) -> Result<Value, AppError> {
    let selector = python_or_empty(payload.get("selector"));
    match action {
        "open_url" => {
            let url = python_or_empty(payload.get("url"));
            let (html, parsed, url) = open_static(&url)?;
            let page = json!({
                "url": url,
                "title": parsed.title,
                "text": parsed.text.chars().take(20_000).collect::<String>(),
                "selector": "",
            });
            apply_page(session, &url, html, parsed);
            Ok(json!({"url": url, "page": page, "controller": controller_kind}))
        }
        "read_page" | "save_snapshot" => {
            let snippet = html_for_selector(&session.html, &selector);
            let parsed = parse_html(
                if snippet.is_empty() {
                    &session.html
                } else {
                    &snippet
                },
                &session.current_url,
            );
            Ok(json!({
                "url": session.current_url,
                "title": parsed.title,
                "text": parsed.text,
                "snapshot": {"type": "webpage", "persisted": false},
                "segments": [],
                "indexed": false,
                "controller": controller_kind,
            }))
        }
        "screenshot" => Ok(json!({
            "url": session.current_url,
            "screenshot": {"type": "screenshot", "mimeType": "image/png", "persisted": false},
            "controller": controller_kind,
        })),
        "click" => {
            if let Some(href) = link_href(&session.html, &selector, &session.current_url) {
                if let Ok((html, parsed, url)) = open_static(&href) {
                    apply_page(session, &url, html, parsed);
                    return Ok(json!({"url": url, "controller": controller_kind}));
                }
            }
            Ok(
                json!({"url": session.current_url, "selector": selector, "static": true, "controller": controller_kind}),
            )
        }
        "type_text" => {
            let text = python_or_empty(payload.get("text"));
            Ok(json!({
                "url": session.current_url,
                "selector": selector,
                "chars": text.chars().count() as i64,
                "static": true,
                "controller": controller_kind,
            }))
        }
        "select" => Ok(json!({
            "url": session.current_url,
            "selector": selector,
            "value": python_or_empty(payload.get("value")),
            "static": true,
            "controller": controller_kind,
        })),
        "scroll" => Ok(json!({
            "url": session.current_url,
            "x": int_or(payload.get("x"), 0),
            "y": int_or(payload.get("y"), 600),
            "static": true,
            "controller": controller_kind,
        })),
        "extract_links" => Ok(json!({
            "url": session.current_url,
            "links": session.links,
            "controller": controller_kind,
        })),
        "extract_dom" => Ok(json!({
            "url": session.current_url,
            "selector": if selector.is_empty() { "document".to_string() } else { selector.clone() },
            "html": if selector.is_empty() { session.html.clone() } else { html_for_selector(&session.html, &selector) },
            "controller": controller_kind,
        })),
        "download" => Err(AppError {
            message: "static browser downloads are not persisted by the native controller"
                .to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        }),
        "close_session" => Ok(json!({"url": session.current_url, "closed": true})),
        other => Err(AppError {
            message: format!("Unsupported browser action: {other}"),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        }),
    }
}

fn link_href(html: &str, selector: &str, base: &str) -> Option<String> {
    let snippet = html_for_selector(html, selector);
    let re = Regex::new(r#"(?i)<a\b[^>]*\bhref=["']([^"']+)["']"#).ok()?;
    let href = re.captures(&snippet)?.get(1)?.as_str();
    Some(join_url(base, href))
}

fn int_or(value: Option<&Value>, default: i64) -> i64 {
    match value {
        Some(Value::Number(number)) => number.as_i64().unwrap_or(default),
        Some(Value::String(text)) => text.parse().unwrap_or(default),
        _ => default,
    }
}

/// Reset the in-memory registry (tests).
pub fn reset_sessions_for_tests() {
    sessions().lock().expect("browser session lock").clear();
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::entropy::SystemEntropy;
    use std::path::{Path, PathBuf};

    fn fixture_path() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/fixtures/browser/basic.html")
    }

    fn fixture_uri(fixture: &Path) -> String {
        assert!(fixture.exists(), "missing {}", fixture.display());
        let display = fixture
            .canonicalize()
            .unwrap_or_else(|_| fixture.to_path_buf())
            .to_string_lossy()
            .replacen(r"\\?\", "", 1);
        format!("file:///{}", display.replace('\\', "/"))
    }

    fn fixture_settings(fixture: &Path) -> BrowserSettings {
        BrowserSettings {
            enabled: true,
            require_confirm: true,
            allow_private_hosts: false,
            headless: true,
            fixture_roots: vec![fixture.parent().expect("fixture dir").to_path_buf()],
        }
    }

    fn fixture_session(settings: &BrowserSettings) -> BrowserSession {
        create_session("", None, settings, &SystemEntropy).expect("browser session")
    }

    #[test]
    fn file_fixture_open_returns_page_text() {
        reset_sessions_for_tests();
        let fixture = fixture_path();
        let settings = fixture_settings(&fixture);
        let result = execute_browser_action(
            &json!({"action": "open_url", "url": fixture_uri(&fixture)}),
            &settings,
            &SystemEntropy,
        )
        .unwrap();
        assert_eq!(result["ok"], true, "{result}");
        let text = result["result"]["page"]["text"].as_str().unwrap();
        assert!(text.contains("Browser Control Runtime"), "{text}");
        assert_eq!(result["result"]["controller"], CONTROLLER_STATIC_FALLBACK);
        assert_eq!(result["session"]["controller"], CONTROLLER_STATIC_FALLBACK);
    }

    #[test]
    fn private_open_is_forbidden_when_enabled() {
        reset_sessions_for_tests();
        let settings = BrowserSettings {
            enabled: true,
            require_confirm: true,
            allow_private_hosts: false,
            headless: true,
            fixture_roots: Vec::new(),
        };
        let result = execute_browser_action(
            &json!({"action": "open_url", "url": "http://127.0.0.1:8000/private"}),
            &settings,
            &SystemEntropy,
        )
        .unwrap();
        assert_eq!(result["ok"], false);
        assert_eq!(result["code"], "forbidden");
        assert_eq!(result["safety"]["risk"], "critical");
    }

    #[test]
    fn the_engine_is_reported_unavailable_rather_than_guessed() {
        // This states the requirement, not the current gap: native runs no
        // Playwright engine, so it must answer `false` and report the fallback it
        // actually uses instead of claiming the oracle's engine. Porting the
        // engine is what flips this, together with `controller_kind_for`.
        assert!(!playwright_available());
        let settings = fixture_settings(&fixture_path());
        let session = fixture_session(&settings);
        assert_eq!(controller_kind_for(&session), CONTROLLER_STATIC_FALLBACK);
        assert_eq!(session.controller_kind, CONTROLLER_UNSTARTED);
        assert_eq!(session.engine, ENGINE_PLAYWRIGHT);
    }

    #[test]
    fn a_blocked_action_never_reaches_a_controller() {
        reset_sessions_for_tests();
        let settings = fixture_settings(&fixture_path());
        let result = execute_browser_action(
            &json!({"action": "open_url", "url": "http://127.0.0.1:8000/private"}),
            &settings,
            &SystemEntropy,
        )
        .unwrap();
        assert_eq!(result["ok"], false);
        // The oracle evaluates the safety policy before `controller_for`, so a
        // refused action leaves the session with no controller recorded at all.
        assert_eq!(result["session"]["controller"], CONTROLLER_UNSTARTED);
        assert_eq!(result["session"]["engine"], ENGINE_PLAYWRIGHT);
        assert_eq!(result["session"]["status"], "idle");
    }

    #[test]
    fn a_failed_action_marks_the_session_and_the_recorded_kind_stays_sticky() {
        reset_sessions_for_tests();
        let fixture = fixture_path();
        let settings = fixture_settings(&fixture);
        let opened = execute_browser_action(
            &json!({"action": "open_url", "url": fixture_uri(&fixture)}),
            &settings,
            &SystemEntropy,
        )
        .unwrap();
        let session_id = opened["session"]["browserSessionId"]
            .as_str()
            .expect("session id")
            .to_string();

        // `download` is not ported: the gate lets it through, the dispatch refuses.
        let failed = execute_browser_action(
            &json!({"action": "download", "sessionId": session_id, "confirmed": true}),
            &settings,
            &SystemEntropy,
        );
        assert!(failed.is_err(), "an unported download must fail closed");

        // A refused action reads the stored session back without touching it, so
        // it is how the failed state is observable through the public API.
        let observed = execute_browser_action(
            &json!({
                "action": "open_url",
                "sessionId": session_id,
                "url": "http://127.0.0.1:8000/private",
            }),
            &settings,
            &SystemEntropy,
        )
        .unwrap();
        assert_eq!(observed["ok"], false);
        assert_eq!(observed["session"]["status"], "failed");
        let recorded = observed["session"]["controller"].as_str().unwrap();
        assert!(recorded.starts_with(FAILED_KIND_PREFIX), "{recorded}");

        // A later success reports the live controller kind, but the sticky
        // recorded one is not restored — the oracle hands back the cached
        // controller without touching the session again.
        let read = execute_browser_action(
            &json!({"action": "read_page", "sessionId": session_id}),
            &settings,
            &SystemEntropy,
        )
        .unwrap();
        assert_eq!(read["result"]["controller"], CONTROLLER_STATIC_FALLBACK);
        assert_eq!(read["session"]["status"], "idle");
        let still_recorded = read["session"]["controller"].as_str().unwrap();
        assert!(
            still_recorded.starts_with(FAILED_KIND_PREFIX),
            "{still_recorded}"
        );
    }

    #[test]
    fn mark_failed_truncates_by_characters_like_python() {
        let settings = fixture_settings(&fixture_path());
        let mut session = fixture_session(&settings);
        // Multi-byte characters on purpose: `str[:120]` slices characters.
        mark_failed(&mut session, &"é".repeat(500));
        assert_eq!(session.status, "failed");
        let recorded = session.controller_kind.clone();
        assert_eq!(
            recorded.chars().count(),
            FAILED_KIND_PREFIX.chars().count() + FAILED_KIND_MESSAGE_CHARS
        );
        assert_eq!(recorded, format!("{FAILED_KIND_PREFIX}{}", "é".repeat(120)));

        // An empty message leaves the recorded kind alone, as `mark_failed` does.
        let mut blank = fixture_session(&settings);
        blank.controller_kind = CONTROLLER_STATIC_FALLBACK.to_string();
        mark_failed(&mut blank, "");
        assert_eq!(blank.status, "failed");
        assert_eq!(blank.controller_kind, CONTROLLER_STATIC_FALLBACK);
    }
}
