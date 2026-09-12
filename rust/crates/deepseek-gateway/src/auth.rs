use axum::{
    extract::Request,
    http::{HeaderMap, StatusCode, header},
    middleware::Next,
    response::{IntoResponse, Json, Response},
};
use serde_json::json;

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
        for part in cookie.split(';') {
            let part = part.trim();
            if let Some(rest) = part.strip_prefix("auth_token=") {
                return rest.to_string();
            }
        }
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

pub async fn require_production_auth(
    axum::extract::State(auth): axum::extract::State<ProductionAuth>,
    request: Request,
    next: Next,
) -> Response {
    if !auth.enabled {
        return next.run(request).await;
    }
    if !requires_auth(request.uri().path()) {
        return next.run(request).await;
    }
    let expected = auth.token.as_str();
    let provided = token_from_headers(request.headers());
    if !tokens_match(&provided, expected) {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": {"code": "UNAUTHORIZED", "message": "Auth required"}})),
        )
            .into_response();
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
