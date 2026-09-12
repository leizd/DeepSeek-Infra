//! Public API forwarding only. Go's `/internal` management plane is not public.
use axum::{
    Json,
    body::{Body, Bytes},
    extract::OriginalUri,
    http::{HeaderMap, Method, StatusCode, Uri, header},
    response::{IntoResponse, Response},
};
use serde_json::json;
use std::time::Duration;

fn proxy_error(status: StatusCode, code: &'static str, message: &'static str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message}})),
    )
        .into_response()
}

fn target_url(base: &str, uri: &Uri) -> Result<reqwest::Url, Response> {
    let invalid_config = || {
        proxy_error(
            StatusCode::SERVICE_UNAVAILABLE,
            "GO_CONTROL_PROXY_INVALID_CONFIG",
            "Go control-plane origin must be configured explicitly",
        )
    };
    let origin = reqwest::Url::parse(base).map_err(|_| invalid_config())?;
    if base.trim() != base
        || !matches!(origin.scheme(), "http" | "https")
        || origin.host_str().is_none()
        || !origin.username().is_empty()
        || origin.password().is_some()
        || origin.path() != "/"
        || origin.query().is_some()
        || origin.fragment().is_some()
    {
        return Err(invalid_config());
    }
    let invalid_path = || {
        proxy_error(
            StatusCode::BAD_REQUEST,
            "GO_CONTROL_PROXY_INVALID_PATH",
            "API request target must retain its original path",
        )
    };
    if !uri.path().starts_with("/api/") {
        return Err(invalid_path());
    }
    // Do not use Axum's decoded Path extractor: decoding followed by URL parsing
    // turns encoded dot segments into an escape from the public /api namespace.
    let raw = uri.path_and_query().ok_or_else(invalid_path)?.as_str();
    let target = origin.join(raw).map_err(|_| invalid_path())?;
    if target.origin() != origin.origin()
        || target.path() != uri.path()
        || target.fragment().is_some()
    {
        return Err(invalid_path());
    }
    Ok(target)
}

fn forwarding_headers(headers: &HeaderMap, request: bool) -> HeaderMap {
    let mut forwarded = headers.clone();
    // RFC 9110 section 7.6.1: all Connection fields can nominate more hop headers.
    for value in headers.get_all(header::CONNECTION) {
        for token in value.as_bytes().split(|byte| *byte == b',') {
            if let Ok(name) = header::HeaderName::from_bytes(token.trim_ascii()) {
                forwarded.remove(name);
            }
        }
    }
    for name in [
        "connection",
        "proxy-connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    ] {
        forwarded.remove(name);
    }
    if request {
        forwarded.remove(header::HOST);
        forwarded.remove(header::CONTENT_LENGTH);
    }
    forwarded
}

pub(crate) async fn proxy_api_to_go(
    method: Method,
    headers: HeaderMap,
    OriginalUri(uri): OriginalUri,
    body: Bytes,
) -> Response {
    // HTTP CONNECT uses authority-form and becomes a tunnel in Hyper. It cannot
    // retain a public /api request target and must never reach the Go listener.
    if method == Method::CONNECT {
        return proxy_error(
            StatusCode::METHOD_NOT_ALLOWED,
            "GO_CONTROL_TUNNEL_UNSUPPORTED",
            "Go control-plane tunneling is not supported",
        );
    }
    let go_url = std::env::var("GO_CONTROL_ADDR")
        .or_else(|_| std::env::var("DEEPSEEK_GO_CONTROL_URL"))
        .ok()
        .filter(|value| !value.trim().is_empty());
    let Some(base) = go_url else {
        return super::unavailable(
            "GO_CONTROL_PROXY_NOT_READY",
            "Go control-plane proxy is not wired",
        )
        .into_response();
    };
    let target = match target_url(&base, &uri) {
        Ok(target) => target,
        Err(response) => return response,
    };
    // Explicit operator-configured private origins may use HTTP. Never consult
    // ambient proxies or follow a redirect into a different API/management path.
    let client = match reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .http1_only()
        .pool_max_idle_per_host(0)
        .no_gzip()
        .no_brotli()
        .no_zstd()
        .no_deflate()
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(10))
        .build()
    {
        Ok(client) => client,
        Err(_) => {
            return proxy_error(
                StatusCode::SERVICE_UNAVAILABLE,
                "GO_CONTROL_PROXY_NOT_READY",
                "Go control-plane transport is unavailable",
            );
        }
    };
    let upstream = match client
        .request(method, target)
        .headers(forwarding_headers(&headers, true))
        .body(body)
        .send()
        .await
    {
        Ok(upstream) => upstream,
        Err(_) => {
            return proxy_error(
                StatusCode::SERVICE_UNAVAILABLE,
                "GO_CONTROL_UNREACHABLE",
                "Go control-plane response unavailable; mutation outcome may be unknown",
            );
        }
    };
    if upstream.status() == StatusCode::SWITCHING_PROTOCOLS {
        return proxy_error(
            StatusCode::BAD_GATEWAY,
            "GO_CONTROL_UPGRADE_UNSUPPORTED",
            "Go control-plane protocol upgrades are not supported",
        );
    }
    let status = upstream.status();
    let response_headers = forwarding_headers(upstream.headers(), false);
    // Preserve backpressure and body errors. An interrupted upstream body must
    // not be changed into an empty successful response.
    let mut response = Response::new(Body::from_stream(upstream.bytes_stream()));
    *response.status_mut() = status;
    *response.headers_mut() = response_headers;
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origin_configuration_never_supplies_a_path_or_credentials() {
        let uri = "/api/policies?limit=2".parse().unwrap();
        for base in [
            "file:///tmp/control",
            "http://user:password@localhost",
            "http://localhost/internal",
            "http://localhost?route=/internal",
            "http://localhost#fragment",
            " http://localhost",
        ] {
            assert_eq!(
                target_url(base, &uri).unwrap_err().status(),
                StatusCode::SERVICE_UNAVAILABLE
            );
        }
        for base in [
            "http://127.0.0.1:8081",
            "http://deepseekd:8081/",
            "https://control.example/",
        ] {
            let target = target_url(base, &uri).unwrap();
            assert_eq!(target.path(), "/api/policies");
            assert_eq!(target.query(), Some("limit=2"));
        }
    }

    #[test]
    fn normalized_paths_are_rejected_without_reencoding_existing_escapes() {
        for path in [
            "/api/../internal/status",
            "/api/%2E%2e/internal/status",
            "/api/a/./b",
            "/internal/status",
        ] {
            let uri = path.parse().unwrap();
            assert_eq!(
                target_url("http://localhost", &uri).unwrap_err().status(),
                StatusCode::BAD_REQUEST
            );
        }
        for path in [
            "/api/%252e%252e/items",
            "/api/%E4%B8%AD%E6%96%87",
            "/api/a%20b",
        ] {
            assert_eq!(
                target_url("http://localhost", &path.parse().unwrap())
                    .unwrap()
                    .path(),
                path
            );
        }
    }

    #[test]
    fn all_connection_fields_nominate_hop_headers() {
        let mut headers = HeaderMap::new();
        headers.append(header::CONNECTION, "X-One, keep-alive".parse().unwrap());
        headers.append(header::CONNECTION, "X-Two".parse().unwrap());
        for name in [
            "x-one",
            "x-two",
            "keep-alive",
            "proxy-authorization",
            "upgrade",
            "transfer-encoding",
            "x-end",
        ] {
            headers.insert(
                header::HeaderName::from_static(name),
                "value".parse().unwrap(),
            );
        }
        let forwarded = forwarding_headers(&headers, false);
        assert_eq!(forwarded.len(), 1);
        assert_eq!(forwarded["x-end"], "value");
    }
}
