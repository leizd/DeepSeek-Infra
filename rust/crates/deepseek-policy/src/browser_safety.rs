//! Browser action safety policy, mirroring `infra/browser/safety.py`.
//!
//! This is the gate that runs **before** any page controller. Playwright is not
//! required to refuse a private host or a high-risk click.

use std::net::IpAddr;
use std::path::{Path, PathBuf};

use regex::Regex;
use serde_json::{Value, json};

use crate::core_utils::python_truthy;
use crate::python_json::value_str;

pub const ALLOW: &str = "allow";
pub const DENY: &str = "deny";
pub const NEEDS_CONFIRMATION: &str = "needs_confirmation";

const PRIVATE_HOST_SUFFIXES: [&str; 3] = [".local", ".localhost", ".internal"];
const EXECUTABLE_SUFFIXES: [&str; 8] = [
    ".bat", ".cmd", ".com", ".exe", ".msi", ".ps1", ".scr", ".sh",
];

#[derive(Debug, Clone)]
pub struct BrowserSettings {
    pub enabled: bool,
    pub require_confirm: bool,
    pub allow_private_hosts: bool,
    pub headless: bool,
    pub fixture_roots: Vec<PathBuf>,
}

impl Default for BrowserSettings {
    fn default() -> Self {
        Self::from_env()
    }
}

impl BrowserSettings {
    pub fn from_env() -> Self {
        let mut fixture_roots = Vec::new();
        if let Some(root) = std::env::var_os("DEEPSEEK_REPO_ROOT").map(PathBuf::from) {
            fixture_roots.extend([
                root.join("tests/fixtures/browser"),
                root.join("tests/fixtures/automation"),
                root.join("evals/golden/browser"),
                root.join("evals/golden/automation"),
            ]);
        }
        Self {
            enabled: env_truthy("BROWSER_CONTROL_ENABLED"),
            require_confirm: env_truthy_default("BROWSER_REQUIRE_CONFIRM", true),
            allow_private_hosts: env_truthy("BROWSER_ALLOW_PRIVATE_HOSTS"),
            headless: env_truthy_default("BROWSER_HEADLESS", true),
            fixture_roots,
        }
    }
}

fn env_truthy(name: &str) -> bool {
    matches!(
        std::env::var(name).ok().as_deref().map(str::trim),
        Some("1" | "true" | "TRUE" | "yes" | "YES" | "on" | "ON")
    )
}

fn env_truthy_default(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(value) => matches!(
            value.trim(),
            "1" | "true" | "TRUE" | "yes" | "YES" | "on" | "ON"
        ),
        Err(_) => default,
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BrowserSafetyDecision {
    pub action: String,
    pub verdict: String,
    pub risk: String,
    pub reasons: Vec<String>,
    pub suggestion: String,
}

impl BrowserSafetyDecision {
    pub fn allowed(&self) -> bool {
        self.verdict == ALLOW
    }
    pub fn needs_confirmation(&self) -> bool {
        self.verdict == NEEDS_CONFIRMATION
    }
    pub fn to_json(&self) -> Value {
        json!({
            "action": self.action,
            "verdict": self.verdict,
            "risk": self.risk,
            "reasons": self.reasons,
            "suggestion": self.suggestion,
        })
    }
}

fn decision(
    action: &str,
    verdict: &str,
    risk: &str,
    reasons: &[&str],
    suggestion: &str,
) -> BrowserSafetyDecision {
    BrowserSafetyDecision {
        action: action.to_string(),
        verdict: verdict.to_string(),
        risk: risk.to_string(),
        reasons: reasons.iter().map(|item| (*item).to_string()).collect(),
        suggestion: suggestion.to_string(),
    }
}

/// Mirrors `normalize_action`.
pub fn normalize_action(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found).trim().to_ascii_lowercase(),
        _ => String::new(),
    }
}

fn high_risk_text_re() -> &'static Regex {
    static PATTERN: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"(?i)\b(submit|delete|remove|purchase|buy|pay|checkout|confirm|authorize|transfer|sign\s*in|login)\b")
            .expect("static regex")
    })
}

fn password_re() -> &'static Regex {
    static PATTERN: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r#"(?i)(password|passwd|pwd|type=['"]password['"]|\[type=['"]password['"]\])"#)
            .expect("static regex")
    })
}

const READ_ACTIONS: [&str; 8] = [
    "open_url",
    "read_page",
    "screenshot",
    "scroll",
    "extract_links",
    "extract_dom",
    "save_snapshot",
    "close_session",
];
const WRITE_ACTIONS: [&str; 4] = ["click", "type_text", "select", "download"];

fn supported(action: &str) -> bool {
    READ_ACTIONS.contains(&action) || WRITE_ACTIONS.contains(&action)
}

/// Mirrors `is_executable_filename`.
pub fn is_executable_filename(value: &str) -> bool {
    let suffix = Path::new(value)
        .extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| format!(".{}", ext.to_ascii_lowercase()))
        .unwrap_or_default();
    EXECUTABLE_SUFFIXES.contains(&suffix.as_str())
}

fn download_looks_executable(payload: &Value) -> bool {
    ["filename", "url", "downloadUrl", "selector"]
        .iter()
        .any(|key| is_executable_filename(&python_or_empty(payload.get(key))))
}

fn python_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => String::new(),
    }
}

/// Mirrors `evaluate_url_safety`.
pub fn evaluate_url_safety(url: &str, settings: &BrowserSettings) -> (bool, String) {
    let raw = url.trim();
    if raw.is_empty() {
        return (false, "empty url".to_string());
    }
    let Some((scheme, host, path, user, password)) = split_url(raw) else {
        return (false, "invalid url".to_string());
    };
    if scheme == "file" {
        return evaluate_file_url_safety(&path, settings);
    }
    if scheme != "http" && scheme != "https" {
        let shown = if scheme.is_empty() {
            "(none)"
        } else {
            scheme.as_str()
        };
        return (false, format!("scheme not allowed: {shown}"));
    }
    if !user.is_empty() || !password.is_empty() {
        return (false, "url credentials are not allowed".to_string());
    }
    let host = host.trim().trim_end_matches('.').to_ascii_lowercase();
    if host.is_empty() {
        return (false, "missing host".to_string());
    }
    if settings.allow_private_hosts {
        return (true, String::new());
    }
    if host == "localhost"
        || PRIVATE_HOST_SUFFIXES
            .iter()
            .any(|suffix| host.ends_with(suffix))
    {
        return (false, "private host is not allowed".to_string());
    }
    let literal = host
        .strip_prefix('[')
        .and_then(|inner| inner.strip_suffix(']'))
        .unwrap_or(host.as_str());
    let Ok(ip) = literal.parse::<IpAddr>() else {
        return (true, String::new());
    };
    if ip_is_blocked(&ip) {
        return (false, format!("private or local ip is not allowed: {ip}"));
    }
    (true, String::new())
}

fn ip_is_blocked(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            let o = v4.octets();
            v4.is_loopback()
                || v4.is_private()
                || v4.is_link_local()
                || v4.is_multicast()
                || v4.is_unspecified()
                || v4.is_broadcast()
                || o[0] == 0
                || o[0] >= 240
                || (o[0] == 100 && (64..=127).contains(&o[1]))
        }
        IpAddr::V6(v6) => {
            v6.is_loopback()
                || v6.is_multicast()
                || v6.is_unspecified()
                || v6.segments()[0] & 0xfe00 == 0xfc00
                || v6.segments()[0] & 0xffc0 == 0xfe80
                || v6
                    .to_ipv4_mapped()
                    .is_some_and(|mapped| ip_is_blocked(&IpAddr::V4(mapped)))
        }
    }
}

fn evaluate_file_url_safety(path_value: &str, settings: &BrowserSettings) -> (bool, String) {
    let path = file_url_path(path_value);
    let Ok(path) = path.canonicalize() else {
        return (false, "invalid file url".to_string());
    };
    for allowed in &settings.fixture_roots {
        if let Ok(root) = allowed.canonicalize() {
            if path.starts_with(&root) {
                return (true, String::new());
            }
        }
    }
    (
        false,
        "file url is only allowed for browser fixture directories".to_string(),
    )
}

pub(crate) fn file_url_path(path_value: &str) -> PathBuf {
    let trimmed = path_value.trim_start_matches('/');
    if cfg!(windows) && trimmed.chars().nth(1) == Some(':') {
        return PathBuf::from(trimmed.replace('/', "\\"));
    }
    PathBuf::from(path_value)
}

fn split_url(raw: &str) -> Option<(String, String, String, String, String)> {
    let (scheme, rest) = raw.split_once(':')?;
    let scheme = scheme.to_ascii_lowercase();
    let rest = rest.strip_prefix("//").unwrap_or(rest);
    if scheme == "file" {
        return Some((
            scheme,
            String::new(),
            rest.to_string(),
            String::new(),
            String::new(),
        ));
    }
    let (authority, path) = match rest.split_once('/') {
        Some((auth, path)) => (auth, format!("/{path}")),
        None => (rest, String::new()),
    };
    let (userinfo, hostport) = match authority.rsplit_once('@') {
        Some((userinfo, host)) => (userinfo, host),
        None => ("", authority),
    };
    let (user, password) = if userinfo.is_empty() {
        (String::new(), String::new())
    } else if let Some((u, p)) = userinfo.split_once(':') {
        (u.to_string(), p.to_string())
    } else {
        (userinfo.to_string(), String::new())
    };
    let host = hostport
        .rsplit_once(':')
        .and_then(|(h, port)| {
            if port.chars().all(|c| c.is_ascii_digit()) {
                Some(h)
            } else {
                None
            }
        })
        .unwrap_or(hostport)
        .trim_matches(|c| c == '[' || c == ']')
        .to_string();
    Some((scheme, host, path, user, password))
}

/// Mirrors `evaluate_action`.
pub fn evaluate_action(payload: &Value, settings: &BrowserSettings) -> BrowserSafetyDecision {
    let action = normalize_action(payload.get("action"));
    if !supported(&action) {
        return decision(
            if action.is_empty() {
                "unknown"
            } else {
                &action
            },
            DENY,
            "high",
            &["unknown_action"],
            "Use a registered browser action.",
        );
    }
    if !settings.enabled {
        return decision(
            &action,
            DENY,
            "high",
            &["browser_control_disabled"],
            "Set BROWSER_CONTROL_ENABLED=1 before exposing browser control.",
        );
    }
    let url = {
        let a = python_or_empty(payload.get("url"));
        let b = python_or_empty(payload.get("downloadUrl"));
        let c = python_or_empty(payload.get("currentUrl"));
        let raw = if !a.trim().is_empty() {
            a
        } else if !b.trim().is_empty() {
            b
        } else {
            c
        };
        raw.trim().to_string()
    };
    if action == "open_url" && url.is_empty() {
        return decision(
            &action,
            DENY,
            "medium",
            &["missing_url"],
            "Provide a URL to open.",
        );
    }
    if !url.is_empty() {
        let (safe, why) = evaluate_url_safety(&url, settings);
        if !safe {
            return decision(
                &action,
                DENY,
                "critical",
                &[&format!("unsafe_url:{why}")],
                "Use a public http(s) URL or an approved fixture file.",
            );
        }
    }
    let selector = python_or_empty(payload.get("selector"));
    let reason = python_or_empty(payload.get("reason"));
    let mut reasons: Vec<String> = Vec::new();
    let mut risk = "low".to_string();
    if WRITE_ACTIONS.contains(&action.as_str()) {
        risk = "medium".to_string();
        if settings.require_confirm {
            reasons.push("write_action_requires_confirmation".to_string());
        }
    }
    let field_type = python_or_empty(payload.get("fieldType")).to_ascii_lowercase();
    if action == "type_text" && (password_re().is_match(&selector) || field_type == "password") {
        risk = "high".to_string();
        reasons.push("password_field_requires_confirmation".to_string());
    }
    if action == "click"
        && (high_risk_text_re().is_match(&selector) || high_risk_text_re().is_match(&reason))
    {
        risk = "high".to_string();
        reasons.push("high_risk_click_requires_confirmation".to_string());
    }
    if action == "download" && download_looks_executable(payload) {
        risk = "high".to_string();
        reasons.push("executable_download_requires_confirmation".to_string());
    }
    if payload
        .get("requiresConfirmation")
        .is_some_and(python_truthy)
    {
        reasons.push("caller_requested_confirmation".to_string());
    }
    let confirmed = payload.get("confirmed").is_some_and(python_truthy);
    if !reasons.is_empty() && !confirmed {
        reasons.sort();
        reasons.dedup();
        return BrowserSafetyDecision {
            action,
            verdict: NEEDS_CONFIRMATION.to_string(),
            risk,
            reasons,
            suggestion: "Ask the user to confirm this browser action.".to_string(),
        };
    }
    if !reasons.is_empty() && confirmed {
        reasons.push("confirmed".to_string());
    }
    reasons.sort();
    reasons.dedup();
    BrowserSafetyDecision {
        action,
        verdict: ALLOW.to_string(),
        risk,
        reasons,
        suggestion: String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn enabled() -> BrowserSettings {
        BrowserSettings {
            enabled: true,
            require_confirm: true,
            allow_private_hosts: false,
            headless: true,
            fixture_roots: Vec::new(),
        }
    }

    #[test]
    fn disabled_control_is_denied() {
        let settings = BrowserSettings {
            enabled: false,
            ..enabled()
        };
        let decision = evaluate_action(
            &json!({"action": "open_url", "url": "https://example.com"}),
            &settings,
        );
        assert_eq!(decision.verdict, DENY);
        assert_eq!(decision.reasons, vec!["browser_control_disabled"]);
    }

    #[test]
    fn private_hosts_are_critical() {
        let settings = enabled();
        let decision = evaluate_action(
            &json!({"action": "open_url", "url": "http://127.0.0.1:8000/private"}),
            &settings,
        );
        assert_eq!(decision.verdict, DENY);
        assert_eq!(decision.risk, "critical");
        assert!(decision.reasons[0].starts_with("unsafe_url:"));
    }

    #[test]
    fn high_risk_click_and_password_need_confirmation() {
        let settings = enabled();
        let submit = evaluate_action(
            &json!({"action": "click", "selector": "button.submit", "reason": "Submit form"}),
            &settings,
        );
        assert_eq!(submit.verdict, NEEDS_CONFIRMATION);
        assert!(
            submit
                .reasons
                .iter()
                .any(|r| r == "high_risk_click_requires_confirmation")
        );
        let password = evaluate_action(
            &json!({"action": "type_text", "selector": "#password", "text": "secret"}),
            &settings,
        );
        assert!(
            password
                .reasons
                .iter()
                .any(|r| r == "password_field_requires_confirmation")
        );
    }
}
