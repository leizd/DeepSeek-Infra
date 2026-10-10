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
use crate::browser_engine::{BrowserEngine, ENGINE_KIND_CDP, EngineAction, EngineFence};
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
///
/// The engine is what makes the `playwright` arm reachable: the oracle's condition
/// is `session.engine == "playwright" and playwright_available()`, and the native
/// equivalent of "Playwright is importable" is "an engine answers `Status` with
/// `available: true`". Without one the static fallback is the controller, which is
/// the deployment this crate has always been able to serve.
fn controller_kind_for(
    session: &BrowserSession,
    engine: Option<&dyn BrowserEngine>,
) -> &'static str {
    let Some(engine) = engine else {
        return CONTROLLER_STATIC_FALLBACK;
    };
    let status = engine.status(&EngineFence::for_request(&session.browser_session_id, 1));
    match status {
        Ok(status) if status.available && session.engine == ENGINE_PLAYWRIGHT => ENGINE_KIND_CDP,
        _ => CONTROLLER_STATIC_FALLBACK,
    }
}

/// The engine action for a tool action, or the reason there is none.
fn engine_action_for(action: &str) -> Option<EngineAction> {
    EngineAction::for_tool_action(action)
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

/// Mirrors `_TextAndLinksParser` — a *streaming* scan, not a DOM.
///
/// The oracle appends one whitespace-collapsed part per data node and joins them with
/// `"\n"`, so a port that joins with `" "` reads differently even when every part is
/// identical. It also pushes tags it will never pop, which is why an unclosed
/// `<script>` swallows the rest of the document and why `a<br>b` inside `<title>`
/// keeps `b` out of the title: the innermost open tag is `br`, not `title`. The port
/// reproduces those quirks rather than tidying them away.
fn parse_html(html: &str, base_url: &str) -> ParsedHtml {
    let mut stack: Vec<String> = Vec::new();
    let mut title_parts: Vec<String> = Vec::new();
    let mut text_parts: Vec<String> = Vec::new();
    let mut links: Vec<Value> = Vec::new();
    let mut open_link: Option<Link> = None;
    let mut rest = html;

    while let Some(index) = rest.find('<') {
        push_data(
            &rest[..index],
            &stack,
            &mut title_parts,
            &mut open_link,
            &mut text_parts,
        );
        let candidate = &rest[index..];
        let after = candidate[1..].chars().next();
        // `HTMLParser` treats a `<` that cannot start a tag as text.
        let starts_tag = match after {
            Some('!') | Some('?') | Some('/') => true,
            Some(character) => character.is_ascii_alphabetic(),
            None => false,
        };
        if !starts_tag {
            push_data(
                "<",
                &stack,
                &mut title_parts,
                &mut open_link,
                &mut text_parts,
            );
            rest = &candidate[1..];
            continue;
        }
        let Some(end) = candidate.find('>') else {
            // Unterminated at the end of input: `HTMLParser.close()` drops it.
            rest = "";
            break;
        };
        handle_tag(
            &candidate[1..end],
            &mut stack,
            &mut open_link,
            &mut links,
            base_url,
        );
        rest = &candidate[end + 1..];
    }
    if !rest.is_empty() {
        push_data(
            rest,
            &stack,
            &mut title_parts,
            &mut open_link,
            &mut text_parts,
        );
    }

    ParsedHtml {
        title: title_parts.join(" ").trim().to_string(),
        text: text_parts.join("\n").trim().to_string(),
        links,
    }
}

struct Link {
    href: String,
    title: String,
    parts: Vec<String>,
}

impl Link {
    fn into_json(self) -> Value {
        json!({
            "href": self.href,
            "text": collapse(&self.parts.join(" ")),
            "title": self.title,
        })
    }
}

fn handle_tag(
    raw: &str,
    stack: &mut Vec<String>,
    open_link: &mut Option<Link>,
    links: &mut Vec<Value>,
    base_url: &str,
) {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed.starts_with('!') || trimmed.starts_with('?') {
        return;
    }
    let self_closing = trimmed.ends_with('/');
    let body = trimmed.trim_end_matches('/');
    let (closing, body) = match body.strip_prefix('/') {
        Some(rest) => (true, rest),
        None => (false, body),
    };
    let name = body
        .split(|character: char| character.is_whitespace() || character == '/')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    if name.is_empty() {
        return;
    }
    if closing {
        finish_link(name.as_str(), open_link, links);
        // The oracle pops whenever the stack is non-empty, matched or not.
        stack.pop();
        return;
    }
    let attrs = parse_attrs(body);
    stack.push(name.clone());
    if name == "a" {
        if let Some(href) = attrs.get("href").filter(|value| !value.is_empty()) {
            // A nested `<a>` replaces the open one, as `handle_starttag` does.
            *open_link = Some(Link {
                href: resolve_url(base_url, href),
                title: attrs.get("title").cloned().unwrap_or_default(),
                parts: Vec::new(),
            });
        }
    }
    if self_closing {
        // `handle_startendtag` runs the start handler and then the end handler.
        finish_link(name.as_str(), open_link, links);
        stack.pop();
    }
}

fn finish_link(name: &str, open_link: &mut Option<Link>, links: &mut Vec<Value>) {
    if name == "a" {
        if let Some(link) = open_link.take() {
            links.push(link.into_json());
        }
    }
}

fn push_data(
    data: &str,
    stack: &[String],
    title_parts: &mut Vec<String>,
    open_link: &mut Option<Link>,
    text_parts: &mut Vec<String>,
) {
    if data.is_empty()
        || stack
            .iter()
            .any(|tag| matches!(tag.as_str(), "script" | "style" | "noscript"))
    {
        return;
    }
    let text = collapse(&html_unescape(data));
    if text.is_empty() {
        return;
    }
    if stack.last().map(String::as_str) == Some("title") {
        title_parts.push(text.clone());
    }
    if let Some(link) = open_link.as_mut() {
        link.parts.push(text.clone());
    }
    text_parts.push(text);
}

/// `" ".join(value.split())` — Unicode whitespace, collapsed.
fn collapse(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// `html.unescape` for the entities that appear in documents and fixtures: the named
/// ones from HTML 4 and numeric references. The full HTML5 named table is not ported;
/// an unknown name is left as written, exactly like Python leaves an unparseable one.
fn html_unescape(value: &str) -> String {
    if !value.contains('&') {
        return value.to_string();
    }
    let mut out = String::with_capacity(value.len());
    let mut rest = value;
    while let Some(amp) = rest.find('&') {
        out.push_str(&rest[..amp]);
        let tail = &rest[amp..];
        let (decoded, consumed) = decode_entity(tail);
        match decoded {
            Some(text) => {
                out.push_str(&text);
                rest = &tail[consumed..];
            }
            None => {
                out.push('&');
                rest = &tail[1..];
            }
        }
    }
    out.push_str(rest);
    out
}

fn decode_entity(tail: &str) -> (Option<String>, usize) {
    let Some(semi) = tail.find(';') else {
        return (None, 0);
    };
    if semi > 32 {
        return (None, 0);
    }
    let body = &tail[1..semi];
    let consumed = semi + 1;
    if let Some(digits) = body.strip_prefix("#x").or_else(|| body.strip_prefix("#X")) {
        return (
            u32::from_str_radix(digits, 16)
                .ok()
                .and_then(char::from_u32)
                .map(String::from),
            consumed,
        );
    }
    if let Some(digits) = body.strip_prefix('#') {
        return (
            digits
                .parse::<u32>()
                .ok()
                .and_then(char::from_u32)
                .map(String::from),
            consumed,
        );
    }
    let named = match body {
        "amp" => "&",
        "lt" => "<",
        "gt" => ">",
        "quot" => "\"",
        "apos" => "'",
        "nbsp" => "\u{a0}",
        "copy" => "\u{a9}",
        "reg" => "\u{ae}",
        "trade" => "\u{2122}",
        "hellip" => "\u{2026}",
        "mdash" => "\u{2014}",
        "ndash" => "\u{2013}",
        "lsquo" => "\u{2018}",
        "rsquo" => "\u{2019}",
        "ldquo" => "\u{201c}",
        "rdquo" => "\u{201d}",
        "middot" => "\u{b7}",
        "laquo" => "\u{ab}",
        "raquo" => "\u{bb}",
        "times" => "\u{d7}",
        "divide" => "\u{f7}",
        "deg" => "\u{b0}",
        "plusmn" => "\u{b1}",
        "euro" => "\u{20ac}",
        "pound" => "\u{a3}",
        "yen" => "\u{a5}",
        "cent" => "\u{a2}",
        "sect" => "\u{a7}",
        "para" => "\u{b6}",
        "bull" => "\u{2022}",
        "dagger" => "\u{2020}",
        "permil" => "\u{2030}",
        "prime" => "\u{2032}",
        "Prime" => "\u{2033}",
        "oline" => "\u{203e}",
        "frasl" => "\u{2044}",
        _ => return (None, 0),
    };
    (Some(named.to_string()), consumed)
}

/// `name=value` pairs from a tag body, names lowercased, values unescaped — the shape
/// `handle_starttag` sees through `{key.lower(): str(value or "")}`.
fn parse_attrs(body: &str) -> HashMap<String, String> {
    let mut attrs = HashMap::new();
    let mut rest = match body.find(|character: char| character.is_whitespace()) {
        Some(index) => &body[index..],
        None => return attrs,
    };
    while let Some(start) =
        rest.find(|character: char| !character.is_whitespace() && character != '/')
    {
        rest = &rest[start..];
        let name_end = rest
            .find(|character: char| {
                character.is_whitespace() || character == '=' || character == '/'
            })
            .unwrap_or(rest.len());
        let name = rest[..name_end].to_ascii_lowercase();
        rest = &rest[name_end..];
        let mut value = String::new();
        if let Some(after_eq) = rest.strip_prefix('=') {
            let after_eq = after_eq.trim_start();
            if let Some(quote) = after_eq.chars().next().filter(|c| *c == '"' || *c == '\'') {
                let inner = &after_eq[quote.len_utf8()..];
                let end = inner.find(quote).unwrap_or(inner.len());
                value = inner[..end].to_string();
                let after_quote = (end + quote.len_utf8()).min(inner.len());
                rest = &inner[after_quote..];
            } else {
                let end = after_eq
                    .find(|character: char| character.is_whitespace())
                    .unwrap_or(after_eq.len());
                value = after_eq[..end].to_string();
                rest = &after_eq[end..];
            }
        }
        rest = rest.trim_start_matches(|character: char| character.is_whitespace());
        if !name.is_empty() {
            attrs.entry(name).or_insert_with(|| html_unescape(&value));
        }
    }
    attrs
}

/// `urllib.parse.urljoin` for the shapes a document can carry: an absolute URL, a
/// network-path, a root-relative path, a bare query, a bare fragment, a relative path
/// with `..`/`.` segments, and the empty reference. Dot segments are removed and an
/// **empty fragment is dropped**, both measured against the oracle rather than assumed:
/// `urljoin(base, "#")` answers the base, without a trailing `#`.
fn resolve_url(base: &str, href: &str) -> String {
    let (base_scheme, base_authority, base_path, base_query, _) = split_reference(base);
    let href = href.trim_start_matches(|character: char| character.is_whitespace());
    if href.is_empty() {
        return compose(&base_scheme, &base_authority, &base_path, &base_query, "");
    }
    if let Some(scheme_end) = href.find(':') {
        let scheme = &href[..scheme_end];
        if !scheme.is_empty()
            && scheme
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic())
            && scheme
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
        {
            let (_, authority, path, query, fragment) = split_reference(href);
            return compose(
                scheme,
                &authority,
                &remove_dot_segments(&path),
                &query,
                &fragment,
            );
        }
    }
    if let Some(after) = href.strip_prefix("//") {
        let (authority, path, query, fragment) = split_authority(after);
        return compose(
            &base_scheme,
            &authority,
            &remove_dot_segments(&path),
            &query,
            &fragment,
        );
    }
    if href.starts_with('#') {
        let fragment = href.trim_start_matches('#');
        return compose(
            &base_scheme,
            &base_authority,
            &base_path,
            &base_query,
            fragment,
        );
    }
    let (path, query, fragment) = split_path(href);
    if path.starts_with('/') {
        return compose(
            &base_scheme,
            &base_authority,
            &remove_dot_segments(&path),
            &query,
            &fragment,
        );
    }
    if path.is_empty() {
        let query = if query.is_empty() {
            base_query.clone()
        } else {
            query
        };
        return compose(&base_scheme, &base_authority, &base_path, &query, &fragment);
    }
    let merged = match base_path.rfind('/') {
        Some(index) => format!("{}{}", &base_path[..=index], path),
        None => format!("/{path}"),
    };
    compose(
        &base_scheme,
        &base_authority,
        &remove_dot_segments(&merged),
        &query,
        &fragment,
    )
}

fn split_reference(reference: &str) -> (String, String, String, String, String) {
    let (scheme, rest) = match reference.find(':') {
        Some(index)
            if reference[..index]
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
                && reference[..index]
                    .chars()
                    .next()
                    .is_some_and(|c| c.is_ascii_alphabetic()) =>
        {
            (reference[..index].to_string(), &reference[index + 1..])
        }
        _ => (String::new(), reference),
    };
    if let Some(after) = rest.strip_prefix("//") {
        let (authority, path, query, fragment) = split_authority(after);
        (scheme, authority, path, query, fragment)
    } else {
        let (path, query, fragment) = split_path(rest);
        (scheme, String::new(), path, query, fragment)
    }
}

fn split_authority(after: &str) -> (String, String, String, String) {
    let end = after.find(['/', '?', '#']).unwrap_or(after.len());
    let (path, query, fragment) = split_path(&after[end..]);
    (after[..end].to_string(), path, query, fragment)
}

fn split_path(rest: &str) -> (String, String, String) {
    let (without_fragment, fragment) = match rest.find('#') {
        Some(index) => (&rest[..index], &rest[index + 1..]),
        None => (rest, ""),
    };
    let (path, query) = match without_fragment.find('?') {
        Some(index) => (&without_fragment[..index], &without_fragment[index + 1..]),
        None => (without_fragment, ""),
    };
    (path.to_string(), query.to_string(), fragment.to_string())
}

fn compose(scheme: &str, authority: &str, path: &str, query: &str, fragment: &str) -> String {
    let mut out = String::new();
    if !scheme.is_empty() {
        out.push_str(scheme);
        out.push(':');
    }
    if !authority.is_empty() || (scheme == "file" && path.starts_with('/')) {
        out.push_str("//");
        out.push_str(authority);
    }
    out.push_str(path);
    if !query.is_empty() {
        out.push('?');
        out.push_str(query);
    }
    if !fragment.is_empty() {
        out.push('#');
        out.push_str(fragment);
    }
    out
}

/// RFC 3986 §5.2.4, the same normalisation `urljoin` performs on the merged path.
fn remove_dot_segments(path: &str) -> String {
    let mut out: Vec<&str> = Vec::new();
    let trailing = path.ends_with('/');
    for segment in path.split('/') {
        match segment {
            "" | "." => {}
            ".." => {
                out.pop();
            }
            other => out.push(other),
        }
    }
    let mut rendered = if path.starts_with('/') {
        format!("/{}", out.join("/"))
    } else {
        out.join("/")
    };
    if trailing && !rendered.ends_with('/') {
        rendered.push('/');
    }
    rendered
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
    execute_browser_action_with_engine(payload, settings, entropy, None)
}

/// `execute_browser_action` with an engine behind the controller.
///
/// Everything above the controller is identical: the safety gate, the session
/// registry, the sticky `controller_kind`, the error envelope. Only
/// [`dispatch_action`] changes, and only for the actions the engine serves — which
/// is what ADR-0050 means by "the seam does not move".
pub fn execute_browser_action_with_engine(
    payload: &Value,
    settings: &BrowserSettings,
    entropy: &dyn Entropy,
    engine: Option<&dyn BrowserEngine>,
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
        // The engine's browser context is torn down over the wire, and the registry
        // entry is closed here. Both happen, in that order, and the engine's failure is
        // reported **after** the registry is closed: a session whose browser could not
        // be reached must still leave the registry, or a dead engine would leak a
        // session for the lifetime of the process.
        let engine_result = engine.map(|engine| {
            engine.execute(
                &engine_fence(payload),
                &crate::browser_engine::EngineRequest {
                    action: EngineAction::CloseSession,
                    session_id: session_id.clone(),
                    ..Default::default()
                },
            )
        });
        let closed = close_session(&session_id)?;
        if let Some(Err(error)) = engine_result {
            return Err(AppError {
                message: error.message,
                code: match error.code.as_str() {
                    crate::browser_engine::ENGINE_SESSION_NOT_FOUND => codes::NOT_FOUND,
                    codes::INVALID_PAYLOAD => codes::INVALID_PAYLOAD,
                    codes::UPSTREAM_TIMEOUT => codes::UPSTREAM_TIMEOUT,
                    _ => codes::INTERNAL,
                },
                status: error.status,
            });
        }
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
    let live_kind = controller_kind_for(&session, engine);
    if session.controller_kind == CONTROLLER_UNSTARTED {
        session.controller_kind = live_kind.to_string();
    }
    let result = match dispatch_action(&action, payload, &mut session, live_kind, engine) {
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

/// The fence a browser tool call carries.
///
/// The tool surface has no epoch of its own — the browser owns no durable state — so
/// the request's `requestId` is the ordering id, falling back to the session, and
/// then to the action itself. The last fallback exists because a tool call need not
/// carry either: the fence still has to name *something*, and the sidecar's own
/// admission refuses an empty action id (`EMPTY_ACTION_ID`), which is a fence
/// validation, not a security boundary. What must never happen is a fence that
/// pretends to be a different call, which is why the value is derived from the
/// request rather than minted.
///
/// The epoch is the request's, defaulting to `1`: a worker must never advance an
/// epoch because a caller sent a larger one, and there is no epoch state here to
/// advance.
fn engine_fence(payload: &Value) -> crate::browser_engine::EngineFence {
    let request_id = python_or_empty(payload.get("requestId"));
    let request_id = if request_id.is_empty() {
        python_or_empty(payload.get("sessionId"))
    } else {
        request_id
    };
    let request_id = if request_id.is_empty() {
        let action = python_or_empty(payload.get("action"));
        format!("browser-action:{action}")
    } else {
        request_id
    };
    crate::browser_engine::EngineFence::for_request(&request_id, 1)
}

fn dispatch_action(
    action: &str,
    payload: &Value,
    session: &mut BrowserSession,
    controller_kind: &str,
    engine: Option<&dyn BrowserEngine>,
) -> Result<Value, AppError> {
    let selector = python_or_empty(payload.get("selector"));
    // The engine serves this action, and an engine is configured: the action is
    // executed by a real browser and this crate only owns the session and the
    // envelope. `close_session` never reaches here — it is the registry's.
    if let (Some(engine), Some(engine_action)) = (engine, engine_action_for(action)) {
        if controller_kind == ENGINE_KIND_CDP {
            return dispatch_through_engine(
                engine,
                engine_action,
                payload,
                session,
                controller_kind,
            );
        }
    }
    match action {
        "open_url" => {
            let url = python_or_empty(payload.get("url"));
            let (html, parsed, url) = open_static(&url)?;
            let title = page_title(&parsed, &url);
            let page = json!({
                "url": url,
                "title": title,
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
                "title": page_title(&parsed, &session.current_url),
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

/// Run one action through the engine and shape its answer like the oracle's.
///
/// The result shape is the oracle's per-action shape, not the proto's: the model
/// sees `{"url", "page"}`, `{"url", "links"}`, `{"url", "selector", "chars"}`, and
/// so on, whether the answer came from Playwright or from CDP. Getting this wrong is
/// the kind of divergence a probe cannot catch, because a probe compares *values* —
/// so the shapes below are the oracle's, copied action by action from
/// `deepseek_infra/infra/browser/actions.py:_dispatch`.
fn dispatch_through_engine(
    engine: &dyn BrowserEngine,
    engine_action: EngineAction,
    payload: &Value,
    session: &mut BrowserSession,
    controller_kind: &str,
) -> Result<Value, AppError> {
    let selector = python_or_empty(payload.get("selector"));
    let request = crate::browser_engine::EngineRequest {
        action: engine_action,
        session_id: session.browser_session_id.clone(),
        url: python_or_empty(payload.get("url")),
        selector: selector.clone(),
        text: python_or_empty(payload.get("text")),
        value: python_or_empty(payload.get("value")),
        x: int_or(payload.get("x"), 0) as i32,
        y: int_or(payload.get("y"), 600) as i32,
        download_dir: None,
    };
    let outcome = engine
        .execute(&engine_fence(payload), &request)
        .map_err(|error| {
            // The engine reports the oracle's codes, and the tool envelope carries the
            // code — so an engine failure must not be flattened to `internal`, or the
            // model (and the compatibility corpus) would see a different error for the
            // same condition depending on which controller answered. Only a code the
            // oracle has no equivalent for is reported as `internal`.
            AppError {
                message: error.message,
                code: match error.code.as_str() {
                    crate::browser_engine::ENGINE_SESSION_NOT_FOUND => codes::NOT_FOUND,
                    crate::browser_engine::ENGINE_NOT_CONFIGURED => codes::INTERNAL,
                    codes::INVALID_PAYLOAD => codes::INVALID_PAYLOAD,
                    codes::NOT_FOUND => codes::NOT_FOUND,
                    codes::UPSTREAM_TIMEOUT => codes::UPSTREAM_TIMEOUT,
                    _ => codes::INTERNAL,
                },
                status: error.status,
            }
        })?;
    let url = if outcome.url.is_empty() {
        session.current_url.clone()
    } else {
        outcome.url.clone()
    };
    match engine_action {
        EngineAction::OpenUrl | EngineAction::ReadPage => {
            // The oracle's `read_page`/`save_snapshot` arm returns the snapshot
            // envelope; `open_url` returns the bounded public page. The distinction
            // is the action, not the engine call, which is why it is here.
            session.current_url = url.clone();
            session.title = outcome.title.clone();
            session.html = outcome.html.clone();
            session.links = outcome
                .links
                .iter()
                .map(|link| json!({"href": link.href, "text": link.text, "title": link.title}))
                .collect();
            session.updated_at = now_iso();
            if engine_action == EngineAction::OpenUrl {
                Ok(json!({
                    "url": url,
                    "page": {
                        "url": outcome.url,
                        "title": outcome.title,
                        "text": outcome.text.chars().take(20_000).collect::<String>(),
                        "selector": outcome.selector,
                    },
                    "controller": controller_kind,
                }))
            } else {
                Ok(json!({
                    "url": url,
                    "title": outcome.title,
                    "text": outcome.text,
                    "snapshot": {"type": "webpage", "persisted": false},
                    "segments": [],
                    "indexed": false,
                    "controller": controller_kind,
                }))
            }
        }
        EngineAction::ExtractLinks => Ok(json!({
            "url": url,
            "links": outcome
                .links
                .iter()
                .map(|link| json!({"href": link.href, "text": link.text, "title": link.title}))
                .collect::<Vec<Value>>(),
            "controller": controller_kind,
        })),
        EngineAction::Screenshot => Ok(json!({
            "url": url,
            "screenshot": {
                "type": "screenshot",
                "mimeType": outcome.mime_type,
                "persisted": false,
                "bytes": outcome.screenshot.map(|data| data.len()).unwrap_or(0),
            },
            "controller": controller_kind,
        })),
        EngineAction::Click => Ok(json!({
            "url": url,
            "selector": if outcome.selector.is_empty() { selector } else { outcome.selector },
            "controller": controller_kind,
        })),
        EngineAction::TypeText => Ok(json!({
            "url": url,
            "selector": if outcome.selector.is_empty() { selector.clone() } else { outcome.selector },
            "chars": request.text.chars().count() as i64,
            "controller": controller_kind,
        })),
        EngineAction::Select => Ok(json!({
            "url": url,
            "selector": if outcome.selector.is_empty() { selector } else { outcome.selector },
            "value": request.value,
            "selected": outcome.selected,
            "controller": controller_kind,
        })),
        EngineAction::Scroll => Ok(json!({
            "url": url,
            "x": request.x,
            "y": request.y,
            "controller": controller_kind,
        })),
        EngineAction::Download => match outcome.download {
            Some(download) => Ok(json!({
                "url": url,
                "download": {
                    "filename": download.filename,
                    "bytes": download.data.len(),
                    "sourceUrl": url,
                },
                "controller": controller_kind,
            })),
            None => Err(AppError {
                message: "the engine reported a download with no file".to_string(),
                code: codes::INTERNAL,
                status: 500,
            }),
        },
        EngineAction::Status => Err(AppError {
            message: "Unsupported browser action: status".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        }),
        // `close_session` never reaches `dispatch_action`: it is handled before the
        // session is resolved, because closing must work for a session the registry
        // has already forgotten.
        EngineAction::CloseSession => Ok(json!({"closed": true, "controller": controller_kind})),
    }
}

fn link_href(html: &str, selector: &str, base: &str) -> Option<String> {
    let snippet = html_for_selector(html, selector);
    let re = Regex::new(r#"(?i)<a\b[^>]*\bhref=["']([^"']+)["']"#).ok()?;
    let href = re.captures(&snippet)?.get(1)?.as_str();
    Some(resolve_url(base, href))
}

/// `parsed.title or self.url` — the controller's fallback, which lives outside the
/// parser: `ParsedHTML.parse` answers an empty title and `open_url` replaces it.
fn page_title(parsed: &ParsedHtml, url: &str) -> String {
    if parsed.title.is_empty() {
        url.to_string()
    } else {
        parsed.title.clone()
    }
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

    /// The session registry is process-wide, so two tests that create a session and
    /// then act on it can interleave with a third test's `reset_sessions_for_tests`
    /// and lose the session between the two steps. That is a real race in the *tests*,
    /// not in the code — the registry is per-process by design, exactly as the
    /// oracle's `_sessions` dict is — so the tests serialize on this mutex.
    ///
    /// Measured: adding the engine-backed cases made this fail intermittently at
    /// `execute_browser_action_with_engine(...).unwrap()` with
    /// `Browser session not found`, which is what an interleaved reset looks like.
    fn registry_lock() -> std::sync::MutexGuard<'static, ()> {
        static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        LOCK.lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    #[test]
    fn file_fixture_open_returns_page_text() {
        let _registry = registry_lock();
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
        let _registry = registry_lock();
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
        // With no engine attached the static fallback is the controller, and the
        // session records exactly that. The engine, when one is attached, is what
        // flips this — `a_configured_engine_selects_the_cdp_controller` below.
        assert!(!playwright_available());
        let settings = fixture_settings(&fixture_path());
        let session = fixture_session(&settings);
        assert_eq!(
            controller_kind_for(&session, None),
            CONTROLLER_STATIC_FALLBACK
        );
        assert_eq!(session.controller_kind, CONTROLLER_UNSTARTED);
        assert_eq!(session.engine, ENGINE_PLAYWRIGHT);
    }

    /// An engine that answers `available: true` selects the CDP controller, and one
    /// that answers `false` does not — the oracle's `_create_controller` condition,
    /// with "Playwright is importable" replaced by "an engine answered".
    struct FakeEngine {
        available: bool,
        calls: std::sync::Mutex<Vec<String>>,
    }

    use crate::browser_engine::{EngineError, EngineOutcome, EngineRequest, EngineStatus};

    impl FakeEngine {
        fn new(available: bool) -> Self {
            Self {
                available,
                calls: std::sync::Mutex::new(Vec::new()),
            }
        }

        fn calls(&self) -> Vec<String> {
            self.calls.lock().expect("fake engine lock").clone()
        }
    }

    impl BrowserEngine for FakeEngine {
        fn status(&self, _fence: &EngineFence) -> Result<EngineStatus, EngineError> {
            Ok(EngineStatus {
                available: self.available,
                engine_kind: if self.available {
                    ENGINE_KIND_CDP.to_string()
                } else {
                    String::new()
                },
                chromium_revision: String::new(),
                reason: if self.available {
                    String::new()
                } else {
                    "no browser configured".to_string()
                },
            })
        }

        fn execute(
            &self,
            _fence: &EngineFence,
            request: &EngineRequest,
        ) -> Result<EngineOutcome, EngineError> {
            self.calls
                .lock()
                .expect("fake engine lock")
                .push(format!("{:?}", request.action));
            Ok(EngineOutcome {
                engine_kind: ENGINE_KIND_CDP.to_string(),
                url: request.url.clone(),
                title: "Engine Title".to_string(),
                text: "engine text".to_string(),
                html: "<html></html>".to_string(),
                selector: if request.selector.is_empty() {
                    "body".to_string()
                } else {
                    request.selector.clone()
                },
                links: vec![crate::browser_engine::EngineLink {
                    href: "https://example.com/".to_string(),
                    text: "Example".to_string(),
                    title: String::new(),
                }],
                selected: vec![request.value.clone()],
                ..Default::default()
            })
        }
    }

    #[test]
    fn the_request_fence_is_never_empty() {
        // A tool call need not carry a requestId or a sessionId; the fence still has
        // to name the call, because the sidecar refuses an empty action id.
        assert_eq!(engine_fence(&json!({})).action_id, "browser-action:");
        assert_eq!(
            engine_fence(&json!({"action": "open_url"})).action_id,
            "browser-action:open_url"
        );
        assert_eq!(
            engine_fence(&json!({"sessionId": "browser_1"})).action_id,
            "browser_1"
        );
        assert_eq!(
            engine_fence(&json!({"requestId": "req-1", "sessionId": "browser_1"})).action_id,
            "req-1"
        );
        assert_eq!(engine_fence(&json!({})).execution_epoch, 1);
    }

    #[test]
    fn a_configured_engine_selects_the_cdp_controller() {
        let _registry = registry_lock();
        reset_sessions_for_tests();
        let settings = fixture_settings(&fixture_path());
        let session = fixture_session(&settings);
        let engine = FakeEngine::new(true);
        assert_eq!(
            controller_kind_for(&session, Some(&engine)),
            ENGINE_KIND_CDP
        );
        let absent = FakeEngine::new(false);
        assert_eq!(
            controller_kind_for(&session, Some(&absent)),
            CONTROLLER_STATIC_FALLBACK
        );
    }

    #[test]
    fn an_engine_backed_action_reads_the_engine_and_keeps_the_oracles_shape() {
        let _registry = registry_lock();
        reset_sessions_for_tests();
        let settings = fixture_settings(&fixture_path());
        let engine = FakeEngine::new(true);
        let opened = execute_browser_action_with_engine(
            &json!({"action": "open_url", "url": "https://example.com/page"}),
            &settings,
            &SystemEntropy,
            Some(&engine),
        )
        .unwrap();
        assert_eq!(opened["ok"], true);
        assert_eq!(opened["session"]["controller"], ENGINE_KIND_CDP);
        // The oracle's `open_url` shape: `{url, page:{url,title,text,selector}}`.
        assert_eq!(opened["result"]["url"], "https://example.com/page");
        assert_eq!(opened["result"]["page"]["title"], "Engine Title");
        assert_eq!(opened["result"]["page"]["text"], "engine text");
        assert_eq!(opened["result"]["page"]["selector"], "body");
        assert_eq!(opened["result"]["controller"], ENGINE_KIND_CDP);
        assert_eq!(engine.calls(), vec!["OpenUrl".to_string()]);

        let session_id = opened["session"]["browserSessionId"]
            .as_str()
            .expect("session id")
            .to_string();
        let links = execute_browser_action_with_engine(
            &json!({"action": "extract_links", "sessionId": session_id}),
            &settings,
            &SystemEntropy,
            Some(&engine),
        )
        .unwrap();
        assert_eq!(links["result"]["links"][0]["href"], "https://example.com/");
        assert_eq!(links["result"]["links"][0]["text"], "Example");
        assert_eq!(
            engine.calls(),
            vec!["OpenUrl".to_string(), "ExtractLinks".to_string()]
        );
    }

    #[test]
    fn an_engine_that_reports_no_browser_leaves_the_static_controller_in_place() {
        let _registry = registry_lock();
        reset_sessions_for_tests();
        let settings = fixture_settings(&fixture_path());
        let engine = FakeEngine::new(false);
        let session_id = fixture_session(&settings).browser_session_id;
        let read = execute_browser_action_with_engine(
            &json!({"action": "read_page", "sessionId": session_id}),
            &settings,
            &SystemEntropy,
            Some(&engine),
        )
        .unwrap();
        // The static controller answered, so the engine was never called.
        assert_eq!(read["result"]["controller"], CONTROLLER_STATIC_FALLBACK);
        assert!(engine.calls().is_empty());
    }

    #[test]
    fn a_blocked_action_never_reaches_a_controller() {
        let _registry = registry_lock();
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
        let _registry = registry_lock();
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

    #[test]
    fn url_resolution_matches_the_urljoin_shapes_measured_on_the_oracle() {
        // Every expected string below was read off `urllib.parse.urljoin` with this
        // base, not inferred from the RFC. `#` is the one that caught the port out: an
        // empty fragment is dropped rather than appended.
        let base = "file:///D:/deepseek/tests/fixtures/browser/download.html";
        for (href, expected) in [
            (
                "download.html",
                "file:///D:/deepseek/tests/fixtures/browser/download.html",
            ),
            (
                "#",
                "file:///D:/deepseek/tests/fixtures/browser/download.html",
            ),
            (
                "#frag",
                "file:///D:/deepseek/tests/fixtures/browser/download.html#frag",
            ),
            (
                "?q=1",
                "file:///D:/deepseek/tests/fixtures/browser/download.html?q=1",
            ),
            ("/root.html", "file:///root.html"),
            ("../up.html", "file:///D:/deepseek/tests/fixtures/up.html"),
            (
                "./same.html",
                "file:///D:/deepseek/tests/fixtures/browser/same.html",
            ),
            (
                "",
                "file:///D:/deepseek/tests/fixtures/browser/download.html",
            ),
            (
                "a/../b.html",
                "file:///D:/deepseek/tests/fixtures/browser/b.html",
            ),
            ("//host/x.html", "file://host/x.html"),
            ("https://example.com/r", "https://example.com/r"),
        ] {
            assert_eq!(resolve_url(base, href), expected, "href {href:?}");
        }
    }

    #[test]
    fn the_static_parse_mirrors_the_oracles_streaming_rules() {
        let base = "file:///srv/ws/page.html";
        // One part per data node, newline-joined, entities decoded.
        let parsed = parse_html("<title>T &amp; U</title><p>a  b</p><p>c</p>", base);
        assert_eq!(parsed.title, "T & U");
        assert_eq!(parsed.text, "T & U\na b\nc");
        assert_eq!(parse_html("<p>&#65;&#x42;</p>", base).text, "AB");

        // The link carries its own title, and a bare fragment resolves to the page.
        let parsed = parse_html(r##"<a href="#top" title="Top">go</a>"##, base);
        assert_eq!(parsed.links[0]["href"], "file:///srv/ws/page.html#top");
        assert_eq!(parsed.links[0]["text"], "go");
        assert_eq!(parsed.links[0]["title"], "Top");

        // An unclosed `<script>` keeps the stack inside script, so the rest is not text.
        assert_eq!(
            parse_html("<p>before</p><script>let a = 1;", base).text,
            "before"
        );

        // A tag inside `<title>` becomes the innermost open tag, so `b` is not title.
        let parsed = parse_html("<title>a<br>b</title>", base);
        assert_eq!(parsed.title, "a");
        assert_eq!(parsed.text, "a\nb");
    }

    #[test]
    fn an_empty_title_falls_back_to_the_url_like_open_url() {
        let parsed = parse_html("<p>no title</p>", "file:///srv/ws/page.html");
        assert_eq!(parsed.title, "");
        assert_eq!(
            page_title(&parsed, "file:///srv/ws/page.html"),
            "file:///srv/ws/page.html"
        );
        let titled = parse_html("<title>Kept</title>", "file:///srv/ws/page.html");
        assert_eq!(page_title(&titled, "file:///srv/ws/page.html"), "Kept");
    }
}
