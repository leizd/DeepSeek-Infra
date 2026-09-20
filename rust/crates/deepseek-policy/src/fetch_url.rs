//! `fetch_url` and the DNS-time SSRF guard, mirroring `tools.fetch_url` /
//! `resolve_public_url` / `fetch_public_url` in
//! `deepseek_infra/infra/tool_runtime/tools.py`.
//!
//! # Two layers of SSRF, not one
//!
//! [`crate::tool_policy::evaluate_url_safety`] is the static pre-check the tool
//! gate runs: scheme, credentials, localhost suffixes, literal private IPs. This
//! module is the DNS-time half the gate cannot do: every address `getaddrinfo`
//! returns must pass [`ensure_public_address`], and the HTTP client must connect
//! to that pinned address rather than resolving the name again (which is how a
//! TTL-0 rebinding attack would otherwise sneak a private IP past the gate).
//!
//! Redirects go back through [`resolve_public_url`], so a `302` to
//! `http://127.0.0.1/admin` is refused even when the original URL was public.
//!
//! # What is injected
//!
//! DNS and HTTP arrive as [`FetchContext`] callbacks, the same way the search
//! transport is injected. The policy crate stays free of TLS; the gateway owns
//! the locked connection (connect to the resolved IP, SNI/Host of the original
//! name). Tests and the parity probe drive both callbacks, so the path is
//! offline-complete except for the gateway's own TCP test.
//!
//! # Trafilatura is not reproduced
//!
//! The oracle uses `trafilatura.extract` when that package is importable, then
//! falls back to [`extract_html_text`]. `trafilatura` is not a production
//! dependency (`requirements.txt` / `pyproject.toml` do not list it), so every
//! shipped deployment and every CI leg takes the HTML-parser fallback. This
//! module ports that fallback, not the optional extra.

use std::net::IpAddr;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::app_error::{AppError, codes};
use crate::core_utils::encode_lower_hex;
use crate::python_json::OrderedJson;
use crate::search::{SEARCH_CACHE_MAX_AGE_SECONDS, search_cache_dir};
use crate::tool_policy::ip_address_is_blocked;

/// `MAX_FETCH_BYTES`.
pub const MAX_FETCH_BYTES: usize = 2_000_000;
/// `MAX_FETCH_REDIRECTS`.
pub const MAX_FETCH_REDIRECTS: usize = 5;
/// `URL_FETCH_CACHE_PREFIX`.
pub const URL_FETCH_CACHE_PREFIX: &str = "fetch-url-";
/// `FETCH_URL_REDIRECT_STATUSES`.
pub const FETCH_URL_REDIRECT_STATUSES: [u16; 5] = [301, 302, 303, 307, 308];
/// `User-Agent` the oracle sends.
pub const FETCH_URL_USER_AGENT: &str = "DeepSeekMobile/0.7 local fetch-url";
/// `Accept` the oracle sends.
pub const FETCH_URL_ACCEPT: &str = "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5";
/// How many characters of extracted text the result keeps.
pub const FETCH_URL_TEXT_LIMIT: usize = 20_000;

/// The resolved, SSRF-checked target `LockedHTTPConnection` connects to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublicUrlTarget {
    pub url: String,
    pub scheme: String,
    pub host: String,
    pub port: u16,
    pub host_header: String,
    pub request_target: String,
    pub address: String,
}

/// One HTTP response, after the client has read at most [`MAX_FETCH_BYTES`] `+ 1`
/// bytes. Redirect handling stays in [`fetch_public_url`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpResponse {
    pub status: u16,
    pub location: String,
    pub content_type: String,
    pub body: Vec<u8>,
}

/// `socket.getaddrinfo` for one host. `port` is `None` for `ensure_public_host`.
pub type DnsLookup<'a> = dyn Fn(&str, Option<u16>) -> Result<Vec<String>, AppError> + 'a;
/// One GET against an already-resolved target. The timeout is seconds.
pub type HttpGet<'a> = dyn Fn(&PublicUrlTarget, u64) -> Result<HttpResponse, AppError> + 'a;

/// Per-request dependencies `fetch_url` needs.
pub struct FetchContext<'a> {
    /// `config.ROOT` — the search-cache directory lives under it.
    pub root: &'a Path,
    /// `time.time()` — cache age is compared against this.
    pub now_epoch: f64,
    /// `TAVILY_TIMEOUT_SECONDS`.
    pub timeout_seconds: u64,
    pub dns: &'a DnsLookup<'a>,
    pub http: &'a HttpGet<'a>,
}

// --- URL parsing (urllib.parse.urlsplit) -----------------------------------------

struct SplitUrl {
    scheme: String,
    netloc: String,
    path: String,
    query: String,
}

struct HostInfo {
    username: String,
    password: Option<String>,
    hostname: String,
    /// `Err(())` is Python's `ValueError` on `.port`.
    port: Result<Option<u16>, ()>,
}

/// `None` is Python's `ValueError` from `urlsplit` (bad brackets).
fn split_url(raw: &str) -> Option<SplitUrl> {
    let cleaned: String = raw
        .chars()
        .filter(|c| !matches!(c, '\t' | '\n' | '\r'))
        .collect();
    let cleaned = cleaned.as_str();

    let mut scheme = String::new();
    let mut rest = cleaned;
    if let Some(colon) = cleaned.find(':') {
        let prefix = &cleaned[..colon];
        let valid = !prefix.is_empty()
            && prefix
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_alphabetic())
            && prefix
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'));
        if valid {
            scheme = prefix.to_ascii_lowercase();
            rest = &cleaned[colon + 1..];
        }
    }

    let (netloc, after_netloc) = if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        let (netloc, remainder) = after.split_at(end);
        (netloc.to_string(), remainder)
    } else {
        (String::new(), rest)
    };

    let (path, after_path) = if let Some(stripped) = after_netloc.strip_prefix('?') {
        ("", format!("?{stripped}"))
    } else if let Some(stripped) = after_netloc.strip_prefix('#') {
        ("", format!("#{stripped}"))
    } else {
        let end = after_netloc.find(['?', '#']).unwrap_or(after_netloc.len());
        let (path, remainder) = after_netloc.split_at(end);
        (path, remainder.to_string())
    };

    let query = if let Some(stripped) = after_path.strip_prefix('?') {
        let end = stripped.find('#').unwrap_or(stripped.len());
        stripped[..end].to_string()
    } else {
        String::new()
    };

    Some(SplitUrl {
        scheme,
        netloc,
        path: path.to_string(),
        query,
    })
}

fn split_host(netloc: &str) -> Result<HostInfo, ()> {
    let (userinfo, hostport) = match netloc.rfind('@') {
        Some(at) => (&netloc[..at], &netloc[at + 1..]),
        None => ("", netloc),
    };
    let (username, password) = match userinfo.find(':') {
        Some(colon) => (&userinfo[..colon], Some(&userinfo[colon + 1..])),
        None => (userinfo, None),
    };

    if let Some(after_bracket) = hostport.strip_prefix('[') {
        let end = after_bracket.find(']').ok_or(())?;
        let inner = &after_bracket[..end];
        if inner.parse::<std::net::Ipv6Addr>().is_err() {
            return Err(());
        }
        let after = &after_bracket[end + 1..];
        Ok(HostInfo {
            username: username.to_string(),
            password: password.map(str::to_string),
            hostname: inner.to_ascii_lowercase(),
            port: parse_port_suffix(after),
        })
    } else {
        if hostport.contains(']') {
            return Err(());
        }
        let (hostname, port) = match hostport.find(':') {
            Some(colon) => (&hostport[..colon], parse_port_value(&hostport[colon + 1..])),
            None => (hostport, Ok(None)),
        };
        Ok(HostInfo {
            username: username.to_string(),
            password: password.map(str::to_string),
            hostname: hostname.to_ascii_lowercase(),
            port,
        })
    }
}

fn parse_port_suffix(after: &str) -> Result<Option<u16>, ()> {
    if after.is_empty() {
        return Ok(None);
    }
    let rest = after.strip_prefix(':').ok_or(())?;
    parse_port_value(rest)
}

fn parse_port_value(rest: &str) -> Result<Option<u16>, ()> {
    if rest.is_empty() {
        return Ok(None);
    }
    let parsed: u32 = rest.parse().map_err(|_| ())?;
    if parsed > 65535 {
        return Err(());
    }
    Ok(Some(parsed as u16))
}

/// Mirrors `normalize_url_host`.
pub fn normalize_url_host(host: &str) -> Result<String, AppError> {
    let value = host.trim().trim_end_matches('.').to_ascii_lowercase();
    if value.is_empty() || value.contains('%') {
        return Ok(value);
    }
    idna::domain_to_ascii(&value).map_err(|_| AppError {
        message: "Invalid URL host".to_string(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    })
}

/// Mirrors `format_host_header`.
pub fn format_host_header(host: &str, port: Option<u16>) -> String {
    let host = if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]")
    } else {
        host.to_string()
    };
    match port {
        Some(port) => format!("{host}:{port}"),
        None => host,
    }
}

/// Mirrors `ensure_public_address`.
pub fn ensure_public_address(address: &str) -> Result<(), AppError> {
    let literal = address
        .split_once('%')
        .map(|(head, _)| head)
        .unwrap_or(address);
    let ip: IpAddr = literal.parse().map_err(|_| AppError {
        message: "URL host resolved to an invalid address".to_string(),
        code: codes::UPSTREAM_FAILURE,
        status: 502,
    })?;
    if ip_address_is_blocked(ip) {
        return Err(AppError {
            message: "Private or local URL targets are not allowed".to_string(),
            code: codes::FORBIDDEN,
            status: 403,
        });
    }
    Ok(())
}

/// `std::net` DNS lookup, matching `resolve_public_host`'s `getaddrinfo` call.
pub fn system_dns(host: &str, port: Option<u16>) -> Result<Vec<String>, AppError> {
    use std::net::ToSocketAddrs;
    let lookup_port = port.unwrap_or(0);
    let infos = (host, lookup_port)
        .to_socket_addrs()
        .map_err(|_| AppError {
            message: "URL host could not be resolved".to_string(),
            code: codes::UPSTREAM_FAILURE,
            status: 502,
        })?;
    let mut addresses: Vec<String> = Vec::new();
    for info in infos {
        let address = info.ip().to_string();
        ensure_public_address(&address)?;
        if !addresses.contains(&address) {
            addresses.push(address);
        }
    }
    if addresses.is_empty() {
        return Err(AppError {
            message: "URL host could not be resolved".to_string(),
            code: codes::UPSTREAM_FAILURE,
            status: 502,
        });
    }
    Ok(addresses)
}

/// Wall clock as `time.time()`.
pub fn system_now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0)
}

/// Mirrors `resolve_public_url`.
pub fn resolve_public_url(url: &str, dns: &DnsLookup<'_>) -> Result<PublicUrlTarget, AppError> {
    let raw = url.trim();
    let parsed = split_url(raw).ok_or_else(|| AppError {
        message: "Invalid URL".to_string(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    })?;
    if parsed.scheme != "http" && parsed.scheme != "https" || parsed.netloc.is_empty() {
        return Err(AppError {
            message: "Only public http(s) URLs are supported".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let host_info = split_host(&parsed.netloc).map_err(|_| AppError {
        message: "Invalid URL".to_string(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    })?;
    if !host_info.username.is_empty()
        || host_info
            .password
            .as_ref()
            .is_some_and(|password| !password.is_empty())
    {
        return Err(AppError {
            message: "URL credentials are not allowed".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let host = normalize_url_host(&host_info.hostname)?;
    if host.is_empty() || host == "localhost" || host.ends_with(".local") {
        return Err(AppError {
            message: "Local URLs are not allowed".to_string(),
            code: codes::FORBIDDEN,
            status: 403,
        });
    }
    let parsed_port = host_info.port.map_err(|_| AppError {
        message: "Invalid URL port".to_string(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    })?;
    let port = parsed_port.unwrap_or(if parsed.scheme == "https" { 443 } else { 80 });
    let host_header = format_host_header(&host, parsed_port);
    // A hostname that is already an IP is the address. Python still calls
    // `getaddrinfo` and then `ensure_public_address`; for a literal the two are
    // the same address, and checking here keeps a stub DNS from laundering
    // `127.0.0.1` into a public stand-in (the redirect test relies on this).
    let addresses = if host.parse::<IpAddr>().is_ok() {
        ensure_public_address(&host)?;
        vec![host.clone()]
    } else {
        dns(&host, Some(port))?
    };
    let path = if parsed.path.is_empty() {
        "/"
    } else {
        parsed.path.as_str()
    };
    let request_target = if parsed.query.is_empty() {
        path.to_string()
    } else {
        format!("{}?{}", path, parsed.query)
    };
    let safe_url = format!("{}://{}{}", parsed.scheme, host_header, request_target);
    Ok(PublicUrlTarget {
        url: safe_url,
        scheme: parsed.scheme,
        host,
        port,
        host_header,
        request_target,
        address: addresses.first().cloned().ok_or_else(|| AppError {
            message: "URL host could not be resolved".to_string(),
            code: codes::UPSTREAM_FAILURE,
            status: 502,
        })?,
    })
}

/// Mirrors `validate_public_url`.
pub fn validate_public_url(url: &str, dns: &DnsLookup<'_>) -> Result<String, AppError> {
    Ok(resolve_public_url(url, dns)?.url)
}

/// Mirrors `urllib.parse.urljoin` for the http(s) cases `fetch_public_url` hits.
pub fn join_url(base: &str, location: &str) -> String {
    let location = location.trim();
    if location.is_empty() {
        return match split_url(base) {
            Some(parsed) => reconstruct(&parsed),
            None => base.to_string(),
        };
    }
    if let Some(parsed) = split_url(location) {
        if parsed.scheme == "http" || parsed.scheme == "https" {
            return reconstruct(&parsed);
        }
        if location.starts_with("//") {
            if let Some(base_parsed) = split_url(base) {
                let scheme = if base_parsed.scheme.is_empty() {
                    "https"
                } else {
                    base_parsed.scheme.as_str()
                };
                return reconstruct(&SplitUrl {
                    scheme: scheme.to_string(),
                    netloc: parsed.netloc,
                    path: parsed.path,
                    query: parsed.query,
                });
            }
        }
    }
    let Some(base_parsed) = split_url(base) else {
        return location.to_string();
    };
    if location.starts_with('/') {
        return reconstruct(&SplitUrl {
            scheme: base_parsed.scheme,
            netloc: base_parsed.netloc,
            path: location
                .split(['?', '#'])
                .next()
                .unwrap_or(location)
                .to_string(),
            query: if let Some((_, query)) = location.split_once('?') {
                query.split('#').next().unwrap_or(query).to_string()
            } else {
                String::new()
            },
        });
    }
    let base_path = if base_parsed.path.is_empty() {
        "/"
    } else {
        base_parsed.path.as_str()
    };
    let directory = match base_path.rsplit_once('/') {
        Some((dir, _)) => format!("{dir}/"),
        None => "/".to_string(),
    };
    let (rel_path, rel_query) = match location.split_once('?') {
        Some((path, query)) => (
            path.split('#').next().unwrap_or(path),
            query.split('#').next().unwrap_or(query),
        ),
        None => (location.split('#').next().unwrap_or(location), ""),
    };
    let combined = format!("{directory}{rel_path}");
    reconstruct(&SplitUrl {
        scheme: base_parsed.scheme,
        netloc: base_parsed.netloc,
        path: collapse_dot_segments(&combined),
        query: rel_query.to_string(),
    })
}

fn reconstruct(parsed: &SplitUrl) -> String {
    let path = if parsed.path.is_empty() {
        "/"
    } else {
        parsed.path.as_str()
    };
    let mut url = format!("{}://{}{}", parsed.scheme, parsed.netloc, path);
    if !parsed.query.is_empty() {
        url.push('?');
        url.push_str(&parsed.query);
    }
    url
}

fn collapse_dot_segments(path: &str) -> String {
    let mut segments: Vec<&str> = Vec::new();
    let absolute = path.starts_with('/');
    for segment in path.split('/') {
        match segment {
            "" | "." => {}
            ".." => {
                segments.pop();
            }
            other => segments.push(other),
        }
    }
    let mut out = String::new();
    if absolute {
        out.push('/');
    }
    out.push_str(&segments.join("/"));
    if path.ends_with('/') && !out.ends_with('/') {
        out.push('/');
    }
    if out.is_empty() { "/".to_string() } else { out }
}

/// Mirrors `fetch_public_url`.
pub fn fetch_public_url(
    target: PublicUrlTarget,
    dns: &DnsLookup<'_>,
    http: &HttpGet<'_>,
    timeout_seconds: u64,
) -> Result<(Vec<u8>, String, String), AppError> {
    let mut current = target;
    for redirect_count in 0..=MAX_FETCH_REDIRECTS {
        let response = http(&current, timeout_seconds)?;
        if FETCH_URL_REDIRECT_STATUSES.contains(&response.status) && !response.location.is_empty() {
            if redirect_count >= MAX_FETCH_REDIRECTS {
                return Err(AppError {
                    message: "Too many URL redirects".to_string(),
                    code: codes::UPSTREAM_FAILURE,
                    status: 502,
                });
            }
            current = resolve_public_url(&join_url(&current.url, &response.location), dns)?;
            continue;
        }
        if response.status >= 400 {
            return Err(AppError {
                message: format!("URL fetch failed: HTTP {}", response.status),
                code: codes::UPSTREAM_FAILURE,
                status: response.status.min(502),
            });
        }
        return Ok((response.body, response.content_type, current.url));
    }
    Err(AppError {
        message: "Too many URL redirects".to_string(),
        code: codes::UPSTREAM_FAILURE,
        status: 502,
    })
}

// --- cache -----------------------------------------------------------------------

/// Mirrors `fetch_url_cache_path`.
pub fn fetch_url_cache_path(root: &Path, url: &str) -> PathBuf {
    let digest = Sha256::digest(url.as_bytes());
    search_cache_dir(root).join(format!(
        "{URL_FETCH_CACHE_PREFIX}{}.json",
        encode_lower_hex(&digest)
    ))
}

/// Mirrors `load_fetch_url_cache`.
pub fn load_fetch_url_cache(root: &Path, url: &str, now_epoch: f64) -> Option<Map<String, Value>> {
    let path = fetch_url_cache_path(root, url);
    let raw = std::fs::read_to_string(&path).ok()?;
    let data: Value = serde_json::from_str(&raw).ok()?;
    let object = data.as_object()?;
    let fetched_at = match object.get("fetchedAt") {
        Some(Value::Number(number)) => number.as_f64().unwrap_or(0.0),
        Some(Value::Bool(true)) => 1.0,
        Some(Value::String(text)) => text.parse().unwrap_or(0.0),
        _ => 0.0,
    };
    if now_epoch - fetched_at > SEARCH_CACHE_MAX_AGE_SECONDS as f64 {
        return None;
    }
    object.get("result")?.as_object().cloned()
}

/// Mirrors `save_fetch_url_cache`. Direct write, no temp file — the oracle
/// does not go through the mutation gate for this cache.
pub fn save_fetch_url_cache(
    root: &Path,
    url: &str,
    result: &Value,
    now_epoch: f64,
) -> std::io::Result<()> {
    let directory = search_cache_dir(root);
    std::fs::create_dir_all(&directory)?;
    let path = fetch_url_cache_path(root, url);
    let payload = json!({
        "url": url,
        "fetchedAt": now_epoch,
        "result": result,
    });
    let rendered = OrderedJson::from_value_with_order(&payload, &["url", "fetchedAt", "result"])
        .render_default_separators();
    std::fs::write(path, rendered)
}

// --- extraction ------------------------------------------------------------------

/// Mirrors `normalize_text`.
pub fn normalize_text(value: &str) -> String {
    let collapsed_spaces = regex::Regex::new(r"[ \t]+")
        .expect("static regex")
        .replace_all(value, " ");
    regex::Regex::new(r"\n{3,}")
        .expect("static regex")
        .replace_all(&collapsed_spaces, "\n\n")
        .trim()
        .to_string()
}

/// Mirrors `decode_text_file`.
pub fn decode_text_file(data: &[u8]) -> String {
    let without_bom = data.strip_prefix(&[0xEF, 0xBB, 0xBF]).unwrap_or(data);
    if let Ok(text) = std::str::from_utf8(without_bom) {
        return text.to_string();
    }
    if std::str::from_utf8(data).is_ok() {
        return String::from_utf8(data.to_vec()).expect("checked utf-8");
    }
    let (cow, _, had_errors) = encoding_rs::GB18030.decode(data);
    if !had_errors {
        return cow.into_owned();
    }
    data.iter().map(|&byte| char::from(byte)).collect()
}

/// Mirrors `html.unescape` for the named and numeric entities the extractor
/// actually meets. Unknown entities are left intact.
fn html_unescape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find('&') {
        out.push_str(&rest[..start]);
        rest = &rest[start..];
        if let Some(end) = rest.find(';') {
            let entity = &rest[1..end];
            if let Some(decoded) = decode_entity(entity) {
                out.push_str(&decoded);
                rest = &rest[end + 1..];
                continue;
            }
        }
        out.push('&');
        rest = &rest[1..];
    }
    out.push_str(rest);
    out
}

fn decode_entity(entity: &str) -> Option<String> {
    if let Some(hex) = entity
        .strip_prefix("#x")
        .or_else(|| entity.strip_prefix("#X"))
    {
        let code = u32::from_str_radix(hex, 16).ok()?;
        return char::from_u32(code).map(String::from);
    }
    if let Some(digits) = entity.strip_prefix('#') {
        let code = digits.parse::<u32>().ok()?;
        return char::from_u32(code).map(String::from);
    }
    let named = match entity {
        "amp" => "&",
        "lt" => "<",
        "gt" => ">",
        "quot" => "\"",
        "apos" => "'",
        "nbsp" => "\u{00a0}",
        _ => return None,
    };
    Some(named.to_string())
}

/// Mirrors `HTMLTextExtractor` + `extract_html_text`.
pub fn extract_html_text(data: &[u8]) -> String {
    let decoded = decode_text_file(data);
    let mut extractor = HtmlTextExtractor::default();
    extractor.feed(&decoded);
    extractor.text()
}

#[derive(Default)]
struct HtmlTextExtractor {
    parts: Vec<String>,
    skip_depth: usize,
}

impl HtmlTextExtractor {
    fn handle_starttag(&mut self, tag: &str) {
        if matches!(tag, "script" | "style" | "noscript" | "svg") {
            self.skip_depth += 1;
            return;
        }
        if matches!(
            tag,
            "p" | "div"
                | "section"
                | "article"
                | "header"
                | "footer"
                | "li"
                | "tr"
                | "h1"
                | "h2"
                | "h3"
                | "h4"
                | "h5"
                | "h6"
        ) {
            self.parts.push("\n".to_string());
        }
        if tag == "br" {
            self.parts.push("\n".to_string());
        }
    }

    fn handle_endtag(&mut self, tag: &str) {
        if matches!(tag, "script" | "style" | "noscript" | "svg") && self.skip_depth > 0 {
            self.skip_depth -= 1;
            return;
        }
        if matches!(
            tag,
            "p" | "div"
                | "section"
                | "article"
                | "li"
                | "tr"
                | "h1"
                | "h2"
                | "h3"
                | "h4"
                | "h5"
                | "h6"
        ) {
            self.parts.push("\n".to_string());
        }
    }

    fn handle_data(&mut self, data: &str) {
        if self.skip_depth > 0 {
            return;
        }
        let text = html_unescape(data).trim().to_string();
        if !text.is_empty() {
            self.parts.push(text);
            self.parts.push(" ".to_string());
        }
    }

    fn feed(&mut self, html: &str) {
        let mut rest = html;
        while !rest.is_empty() {
            let Some(start) = rest.find('<') else {
                self.handle_data(rest);
                break;
            };
            if start > 0 {
                self.handle_data(&rest[..start]);
            }
            rest = &rest[start..];
            if rest.starts_with("<!--") {
                match rest.find("-->") {
                    Some(end) => rest = &rest[end + 3..],
                    None => break,
                }
                continue;
            }
            if let Some(after) = rest.strip_prefix("</") {
                match after.find('>') {
                    Some(end) => {
                        let tag = after[..end]
                            .split_whitespace()
                            .next()
                            .unwrap_or("")
                            .to_ascii_lowercase();
                        self.handle_endtag(&tag);
                        rest = &after[end + 1..];
                    }
                    None => break,
                }
                continue;
            }
            let after = &rest[1..];
            if after.starts_with('!') || after.starts_with('?') {
                match after.find('>') {
                    Some(end) => rest = &after[end + 1..],
                    None => break,
                }
                continue;
            }
            match after.find('>') {
                Some(end) => {
                    let inner = &after[..end];
                    let trimmed = inner.trim().trim_end_matches('/');
                    let tag = trimmed
                        .split_whitespace()
                        .next()
                        .unwrap_or("")
                        .to_ascii_lowercase();
                    self.handle_starttag(&tag);
                    rest = &after[end + 1..];
                }
                None => break,
            }
        }
    }

    fn text(&self) -> String {
        normalize_text(&self.parts.concat())
    }
}

/// Mirrors `extract_readable_text` without the optional trafilatura arm.
pub fn extract_readable_text(raw: &[u8], content_type: &str) -> String {
    let htmlish = content_type.to_ascii_lowercase().contains("html")
        || raw
            .get(..1000.min(raw.len()))
            .is_some_and(|prefix| contains_ignore_ascii_case(prefix, b"<html"));
    if htmlish {
        return normalize_text(&extract_html_text(raw));
    }
    normalize_text(&String::from_utf8_lossy(raw))
}

fn contains_ignore_ascii_case(haystack: &[u8], needle: &[u8]) -> bool {
    haystack
        .windows(needle.len())
        .any(|window| window.eq_ignore_ascii_case(needle))
}

// --- the branch ------------------------------------------------------------------

/// `str(value or "")` for the `url` argument.
pub fn python_url_arg(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::Bool(false)) => String::new(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Number(number)) => {
            if number.as_f64() == Some(0.0) {
                String::new()
            } else {
                number.to_string()
            }
        }
        Some(Value::String(text)) => text.clone(),
        Some(Value::Array(items)) if items.is_empty() => String::new(),
        Some(Value::Object(fields)) if fields.is_empty() => String::new(),
        Some(other) => other.to_string(),
    }
}

/// Mirrors `fetch_url`.
pub fn fetch_url(url: &str, context: Option<&FetchContext<'_>>) -> Result<Value, AppError> {
    let Some(context) = context else {
        return Err(AppError {
            message: "fetch_url is not enabled for this request".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    let target = resolve_public_url(url, context.dns)?;
    let cache_url = target.url.clone();
    if let Some(mut cached) = load_fetch_url_cache(context.root, &cache_url, context.now_epoch) {
        cached.insert("cached".to_string(), Value::Bool(true));
        return Ok(Value::Object(cached));
    }
    let (raw, content_type, final_url) =
        fetch_public_url(target, context.dns, context.http, context.timeout_seconds)?;
    if raw.len() > MAX_FETCH_BYTES {
        return Err(AppError {
            message: "Fetched page is too large".to_string(),
            code: codes::UPLOAD_TOO_LARGE,
            status: 413,
        });
    }
    let text = extract_readable_text(&raw, &content_type);
    let truncated: String = text.chars().take(FETCH_URL_TEXT_LIMIT).collect();
    let result = json!({
        "url": final_url,
        "contentType": content_type,
        "text": truncated,
        "charCount": text.chars().count() as i64,
    });
    let _ = save_fetch_url_cache(context.root, &cache_url, &result, context.now_epoch);
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    fn public_dns(_host: &str, _port: Option<u16>) -> Result<Vec<String>, AppError> {
        Ok(vec!["93.184.216.34".to_string()])
    }

    fn html_page() -> Vec<u8> {
        b"<html><head><title>Doc</title></head><body><main><h1>Hello</h1><p>Readable page text.</p></main></body></html>".to_vec()
    }

    fn ok_http(_target: &PublicUrlTarget, _timeout: u64) -> Result<HttpResponse, AppError> {
        Ok(HttpResponse {
            status: 200,
            location: String::new(),
            content_type: "text/html; charset=utf-8".to_string(),
            body: html_page(),
        })
    }

    #[test]
    fn resolve_public_url_strips_the_fragment_and_pins_the_resolved_address() {
        let target = resolve_public_url("https://example.com/path?x=1#fragment", &public_dns)
            .expect("public");
        assert_eq!(target.url, "https://example.com/path?x=1");
        assert_eq!(target.host, "example.com");
        assert_eq!(target.port, 443);
        assert_eq!(target.host_header, "example.com");
        assert_eq!(target.request_target, "/path?x=1");
        assert_eq!(target.address, "93.184.216.34");
        assert_eq!(target.scheme, "https");
    }

    #[test]
    fn resolve_public_url_keeps_an_explicit_default_port_in_the_host_header() {
        let target = resolve_public_url("https://example.com:443/x", &public_dns).expect("public");
        assert_eq!(target.host_header, "example.com:443");
        assert_eq!(target.port, 443);
        assert_eq!(target.url, "https://example.com:443/x");
    }

    #[test]
    fn resolve_public_url_rejects_local_credentials_and_non_http_schemes() {
        let local = resolve_public_url("http://127.0.0.1:8000/", &public_dns).unwrap_err();
        assert_eq!(local.code, codes::FORBIDDEN);
        assert_eq!(local.status, 403);

        let localhost = resolve_public_url("http://localhost:8000", &public_dns).unwrap_err();
        assert_eq!(localhost.code, codes::FORBIDDEN);

        let local_suffix = resolve_public_url("http://printer.local/", &public_dns).unwrap_err();
        assert_eq!(local_suffix.code, codes::FORBIDDEN);

        let ftp = resolve_public_url("ftp://example.com", &public_dns).unwrap_err();
        assert_eq!(ftp.code, codes::INVALID_PAYLOAD);
        assert_eq!(ftp.message, "Only public http(s) URLs are supported");

        let creds = resolve_public_url("https://user:pass@example.com", &public_dns).unwrap_err();
        assert_eq!(creds.message, "URL credentials are not allowed");

        let bad_port = resolve_public_url("https://example.com:abc/", &public_dns).unwrap_err();
        assert_eq!(bad_port.message, "Invalid URL port");
    }

    #[test]
    fn ensure_public_address_uses_the_same_block_set_as_the_static_guard() {
        ensure_public_address("93.184.216.34").expect("public v4");
        let loopback = ensure_public_address("127.0.0.1").unwrap_err();
        assert_eq!(loopback.code, codes::FORBIDDEN);
        let metadata = ensure_public_address("169.254.169.254").unwrap_err();
        assert_eq!(metadata.code, codes::FORBIDDEN);
        let cgnat = ensure_public_address("100.64.0.1").unwrap_err();
        assert_eq!(cgnat.code, codes::FORBIDDEN);
        let mapped = ensure_public_address("::ffff:127.0.0.1").unwrap_err();
        assert_eq!(mapped.code, codes::FORBIDDEN);
    }

    #[test]
    fn extract_html_text_drops_script_and_keeps_readable_text() {
        let html = b"<html><head><script>secret()</script><style>x{color:red}</style></head><body><h1>Hello</h1><p>Readable page text.</p></body></html>";
        let text = extract_html_text(html);
        assert!(text.contains("Hello"));
        assert!(text.contains("Readable page text"));
        assert!(!text.contains("secret"));
        assert!(!text.contains("color:red"));
    }

    #[test]
    fn fetch_url_extracts_and_caches_a_public_page() {
        let root = std::env::temp_dir().join(format!("fetch-url-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let hits = RefCell::new(0_u32);
        let http = |target: &PublicUrlTarget, _timeout: u64| {
            *hits.borrow_mut() += 1;
            assert_eq!(target.address, "93.184.216.34");
            assert_eq!(target.host_header, "example.com");
            ok_http(target, _timeout)
        };
        let dns_hits = RefCell::new(0_u32);
        let dns = |host: &str, port: Option<u16>| {
            *dns_hits.borrow_mut() += 1;
            public_dns(host, port)
        };
        let context = FetchContext {
            root: &root,
            now_epoch: 1_700_000_000.0,
            timeout_seconds: 45,
            dns: &dns,
            http: &http,
        };
        let first = fetch_url("https://example.com/path?x=1#fragment", Some(&context)).unwrap();
        let second = fetch_url("https://example.com/path?x=1#fragment", Some(&context)).unwrap();
        assert_eq!(*dns_hits.borrow(), 2);
        assert_eq!(*hits.borrow(), 1);
        assert_eq!(first["url"], "https://example.com/path?x=1");
        assert!(
            first["text"]
                .as_str()
                .unwrap()
                .contains("Readable page text")
        );
        assert_eq!(second["cached"], true);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn fetch_url_revalidates_redirect_targets() {
        let http = |target: &PublicUrlTarget, _timeout: u64| {
            assert_eq!(target.host, "example.com");
            Ok(HttpResponse {
                status: 302,
                location: "http://127.0.0.1/admin".to_string(),
                content_type: String::new(),
                body: Vec::new(),
            })
        };
        let err = fetch_public_url(
            resolve_public_url("https://example.com/path", &public_dns).unwrap(),
            &public_dns,
            &http,
            45,
        )
        .unwrap_err();
        assert_eq!(err.code, codes::FORBIDDEN);
    }

    #[test]
    fn fetch_url_rejects_an_oversize_body() {
        let huge = vec![b'x'; MAX_FETCH_BYTES + 10];
        let http = |_target: &PublicUrlTarget, _timeout: u64| {
            Ok(HttpResponse {
                status: 200,
                location: String::new(),
                content_type: "text/html".to_string(),
                body: huge.clone(),
            })
        };
        let root = std::env::temp_dir().join(format!("fetch-url-huge-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&root);
        let context = FetchContext {
            root: &root,
            now_epoch: 1.0,
            timeout_seconds: 45,
            dns: &public_dns,
            http: &http,
        };
        let err = fetch_url("https://example.com/huge", Some(&context)).unwrap_err();
        assert_eq!(err.code, codes::UPLOAD_TOO_LARGE);
        assert_eq!(err.status, 413);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn fetch_url_without_a_context_is_the_disabled_path() {
        let err = fetch_url("https://example.com/", None).unwrap_err();
        assert_eq!(err.code, codes::INVALID_PAYLOAD);
        assert!(err.message.contains("not enabled"));
    }

    #[test]
    fn python_url_arg_matches_str_of_or_empty() {
        assert_eq!(python_url_arg(None), "");
        assert_eq!(python_url_arg(Some(&Value::Null)), "");
        assert_eq!(python_url_arg(Some(&json!(0))), "");
        assert_eq!(python_url_arg(Some(&json!(false))), "");
        assert_eq!(python_url_arg(Some(&json!(""))), "");
        assert_eq!(python_url_arg(Some(&json!([]))), "");
        assert_eq!(python_url_arg(Some(&json!({}))), "");
        assert_eq!(python_url_arg(Some(&json!(true))), "True");
        assert_eq!(python_url_arg(Some(&json!("https://x"))), "https://x");
    }

    #[test]
    fn http_errors_cap_their_status_at_502() {
        let http = |_target: &PublicUrlTarget, _timeout: u64| {
            Ok(HttpResponse {
                status: 503,
                location: String::new(),
                content_type: String::new(),
                body: Vec::new(),
            })
        };
        let err = fetch_public_url(
            resolve_public_url("https://example.com/", &public_dns).unwrap(),
            &public_dns,
            &http,
            45,
        )
        .unwrap_err();
        assert_eq!(err.status, 502);
        assert_eq!(err.message, "URL fetch failed: HTTP 503");
    }
}
