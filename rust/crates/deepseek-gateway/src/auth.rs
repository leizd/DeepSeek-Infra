use axum::{
    extract::Request,
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    middleware::Next,
    response::{IntoResponse, Json, Response},
};
use serde_json::json;

fn query_value(query: Option<&str>, name: &str) -> String {
    let decode = |value: &str| {
        percent_encoding::percent_decode_str(&value.replace('+', " "))
            .decode_utf8_lossy()
            .into_owned()
    };
    query
        .unwrap_or_default()
        .split('&')
        .filter_map(|pair| {
            let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
            (decode(key) == name).then(|| decode(value))
        })
        .last()
        .unwrap_or_default()
}

fn legal_cookie_char(value: char) -> bool {
    value.is_ascii_alphanumeric() || "!#$%&'*+-.^_`|~:".contains(value)
}
fn auth_cookie_header(token: &str) -> String {
    let value = if !token.is_empty() && token.chars().all(legal_cookie_char) {
        token.to_owned()
    } else {
        let mut value = String::from("\"");
        for ch in token.chars() {
            match ch {
                '\\' => value.push_str("\\\\"),
                '"' => value.push_str("\\\""),
                ',' => value.push_str("\\054"),
                ';' => value.push_str("\\073"),
                ' '..='~' => value.push(ch),
                ch if (ch as u32) <= 255 => value.push_str(&format!("\\{:03o}", ch as u32)),
                ch => value.push(ch),
            }
        }
        value.push('"');
        value
    };
    format!("auth_token={value}; HttpOnly; Max-Age=2592000; Path=/; SameSite=Strict")
}
fn cookie_value(raw: &str) -> String {
    let Some(quoted) = raw.strip_prefix('"').and_then(|s| s.strip_suffix('"')) else {
        return raw.to_string();
    };
    let mut chars = quoted.chars().peekable();
    let mut result = String::new();
    while let Some(ch) = chars.next() {
        if ch != '\\' {
            result.push(ch);
            continue;
        }
        let Some(next) = chars.next() else {
            result.push('\\');
            break;
        };
        if matches!(next, '0'..='3') {
            let mut lookahead = chars.clone();
            if let (Some(second @ '0'..='7'), Some(third @ '0'..='7')) =
                (lookahead.next(), lookahead.next())
            {
                let code = (next as u32 - '0' as u32) * 64
                    + (second as u32 - '0' as u32) * 8
                    + third as u32
                    - '0' as u32;
                result.push(char::from_u32(code).unwrap());
                chars = lookahead;
                continue;
            }
        }
        result.push(next);
    }
    result
}
fn cookie_token(raw: &str) -> String {
    let mut quoted = false;
    let mut escaped = false;
    let mut start = 0;
    let mut parts = Vec::new();
    for (offset, ch) in raw.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        match ch {
            '\\' if quoted => escaped = true,
            '"' => quoted = !quoted,
            ';' if !quoted => {
                parts.push(&raw[start..offset]);
                start = offset + 1;
            }
            _ => (),
        }
    }
    if quoted {
        return String::new();
    }
    parts.push(&raw[start..]);
    let mut token = String::new();
    for part in parts {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        let Some((name, value)) = part.split_once('=') else {
            return String::new();
        };
        if !name.trim().chars().all(legal_cookie_char) {
            return String::new();
        }
        if name.trim() == "auth_token" {
            token = cookie_value(value.trim());
        }
    }
    token
}

fn env_truthy(name: &str) -> bool {
    matches!(
        std::env::var(name)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProductionAuth {
    pub enabled: bool,
    pub token: String,
}

pub(crate) fn production_auth_enabled() -> bool {
    ProductionAuth::from_env().enabled
}

impl ProductionAuth {
    pub fn from_env() -> Self {
        Self {
            enabled: !env_truthy("AUTH_DISABLED"),
            token: std::env::var("AUTH_TOKEN")
                .unwrap_or_default()
                .trim()
                .to_string(),
        }
    }
}

fn requires_auth(path: &str) -> bool {
    path.starts_with("/api/")
        || path.starts_with("/v1/")
        || path == "/mcp"
        || path.starts_with("/a2a/")
        || path == "/a2a"
}

fn browser_response(path: &str, mut response: Response) -> Response {
    crate::static_files::apply_frontend_headers(path, &mut response);
    response
}

fn token_from_headers(headers: &HeaderMap) -> String {
    if let Some(value) = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
    {
        let value = value.trim();
        if let Some(token) = value.strip_prefix("Bearer ").or_else(|| {
            value
                .get(..7)
                .filter(|prefix| prefix.eq_ignore_ascii_case("bearer "))
                .and_then(|_| value.get(7..))
        }) {
            return token.trim().to_string();
        }
    }
    if let Some(cookie) = headers
        .get(header::COOKIE)
        .and_then(|value| value.to_str().ok())
    {
        return cookie_token(cookie);
    }
    String::new()
}

fn tokens_match(provided: &str, expected: &str) -> bool {
    let left = provided.as_bytes();
    let right = expected.as_bytes();
    let n = left.len().max(right.len());
    let mut diff = left.len() ^ right.len();
    for index in 0..n {
        diff |= (left.get(index).copied().unwrap_or(0) ^ right.get(index).copied().unwrap_or(0))
            as usize;
    }
    !expected.is_empty() && diff == 0
}

fn backup_inventory_host_allowed(headers: &HeaderMap) -> bool {
    let host = headers
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim();
    !host.is_empty()
        && !host.contains('/')
        && !host.contains('\\')
        && crate::openai_facade::allowed_auth_hosts()
            .contains(&crate::openai_facade::host_without_port(host))
}

/// The oracle's `require_api_auth` checks the original `Host` on every inventory route it
/// serves — the collection **and** the item paths — so a foreign host is refused there for
/// `PATCH`, `DELETE` and the per-policy sub-routes too, not only for the collection.
///
/// The edge has to make that decision itself. The proxy rewrites `Host` to the Go listener
/// when it forwards, so a check made only by the Go side would inspect the wrong value and
/// never refuse a foreign host.
fn backup_inventory_route(path: &str) -> bool {
    const PREFIXES: [&str; 3] = [
        "/api/workspace/backup-policies",
        "/api/workspace/backup-targets",
        "/api/workspace/backup-mirrors",
    ];
    PREFIXES
        .iter()
        .any(|prefix| match path.strip_prefix(prefix) {
            Some("") => true,
            Some(rest) => rest.starts_with('/'),
            None => false,
        })
}

pub async fn require_production_auth(
    axum::extract::State(auth): axum::extract::State<ProductionAuth>,
    request: Request,
    next: Next,
) -> Response {
    if !auth.enabled {
        return next.run(request).await;
    }
    // The launch URL is an existing public interface: browser navigation exchanges
    // its token for an HttpOnly cookie; desktop WebViews load the document directly.
    if matches!(*request.method(), Method::GET | Method::HEAD)
        && matches!(request.uri().path(), "/" | "/ui" | "/ui/")
    {
        let token = query_value(request.uri().query(), "token");
        if !token.is_empty() {
            let path = request.uri().path().to_owned();
            if !tokens_match(&token, &auth.token) {
                return browser_response(
                    &path,
                    (
                        StatusCode::UNAUTHORIZED,
                        Json(json!({"error":"Auth required","code":"unauthorized"})),
                    )
                        .into_response(),
                );
            }
            let cookie = match HeaderValue::from_str(&auth_cookie_header(&auth.token)) {
                Ok(value) => value,
                Err(_) => return browser_response(&path, (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(json!({"error":"Invalid authentication configuration","code":"internal"})),
                )
                    .into_response()),
            };
            let desktop = query_value(request.uri().query(), "desktop");
            let mut response = if deepseek_policy::core_utils::web_truthy(Some(&json!(desktop))) {
                next.run(request).await
            } else {
                let location = if request.uri().path().starts_with("/ui") {
                    "/ui/"
                } else {
                    "/"
                };
                let mut response = StatusCode::FOUND.into_response();
                response
                    .headers_mut()
                    .insert(header::LOCATION, HeaderValue::from_static(location));
                response
            };
            response.headers_mut().insert(header::SET_COOKIE, cookie);
            return browser_response(&path, response);
        }
    }
    if !requires_auth(request.uri().path()) {
        return next.run(request).await;
    }
    let expected = auth.token.as_str();
    let provided = token_from_headers(request.headers());
    if backup_inventory_route(request.uri().path()) {
        if !backup_inventory_host_allowed(request.headers()) {
            return (
                StatusCode::FORBIDDEN,
                Json(json!({"error": "Host not allowed", "code": "forbidden"})),
            )
                .into_response();
        }
        if !tokens_match(&provided, expected) {
            return (
                StatusCode::UNAUTHORIZED,
                Json(json!({"error": "Auth required", "code": "unauthorized"})),
            )
                .into_response();
        }
        return next.run(request).await;
    }
    if !tokens_match(&provided, expected) {
        let path = request.uri().path();
        let error = if path == "/api/skills" || path.starts_with("/api/skills/") {
            json!({"error":"Auth required","code":"unauthorized"})
        } else {
            json!({"error": {"code": "UNAUTHORIZED", "message": "Auth required"}})
        };
        return (StatusCode::UNAUTHORIZED, Json(error)).into_response();
    }
    next.run(request).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    #[test]
    fn empty_configured_token_never_matches() {
        assert!(!tokens_match("", ""));
        assert!(!tokens_match("secret", ""));
    }

    #[test]
    fn bearer_and_cookie_tokens_are_extracted() {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::AUTHORIZATION,
            HeaderValue::from_static("Bearer unit-token"),
        );
        assert_eq!(token_from_headers(&headers), "unit-token");

        let mut cookie_headers = HeaderMap::new();
        cookie_headers.insert(
            header::COOKIE,
            HeaderValue::from_static("theme=dark; auth_token=cookie-token"),
        );
        assert_eq!(token_from_headers(&cookie_headers), "cookie-token");
    }

    #[test]
    fn tokens_match_rejects_length_mismatch() {
        assert!(!tokens_match("ab", "abc"));
        assert!(tokens_match("same", "same"));
    }
}
