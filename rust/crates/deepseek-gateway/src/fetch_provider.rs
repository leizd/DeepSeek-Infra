//! The locked HTTP client `fetch_url` uses: connect to the DNS-pinned address,
//! speak HTTP with the original `Host` / SNI.
//!
//! This is the wiring layer. The SSRF check, redirect revalidation, cache and
//! HTML extraction live in `deepseek_policy::fetch_url` and are probe-verified;
//! what is here is the connection that those tests inject a fake for.
//!
//! Mirrors `LockedHTTPConnection` / `LockedHTTPSConnection` /
//! `public_http_connection` in `infra/tool_runtime/tools.py`.
//!
//! # Why `reqwest::blocking` plus `resolve`
//!
//! `FetchContext.http` is a synchronous callback, and the tool loop calls it from
//! `tokio::task::spawn_blocking` — a blocking client is the right shape. Binding
//! the hostname to the already-checked [`PublicUrlTarget::address`] with
//! [`reqwest::ClientBuilder::resolve`] is what keeps the client from doing a
//! second DNS lookup (the rebinding window the oracle closes by connecting to
//! `info[4][0]`). Redirects are disabled: the policy crate re-resolves `Location`
//! itself.
//!
//! Official source: <https://docs.rs/reqwest/0.12.28/reqwest/struct.ClientBuilder.html#method.resolve>

use std::io::Read;
use std::net::{IpAddr, SocketAddr};
use std::time::Duration;

use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::fetch_url::{
    FETCH_URL_ACCEPT, FETCH_URL_USER_AGENT, HttpResponse, MAX_FETCH_BYTES, PublicUrlTarget,
};
use deepseek_policy::search::TAVILY_TIMEOUT_SECONDS;

/// `TAVILY_TIMEOUT_SECONDS`, overridable the same way the search provider is.
pub fn timeout_from_env() -> u64 {
    std::env::var("TAVILY_TIMEOUT_SECONDS")
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|seconds| *seconds > 0)
        .unwrap_or(TAVILY_TIMEOUT_SECONDS)
}

/// One GET against a resolved target. Connects to `target.address`, sends
/// `Host: target.host_header`, and for HTTPS uses `target.host` as SNI.
pub fn locked_http_get(
    target: &PublicUrlTarget,
    timeout_seconds: u64,
) -> Result<HttpResponse, AppError> {
    let ip: IpAddr = target.address.parse().map_err(|_| AppError {
        message: "URL host resolved to an invalid address".to_string(),
        code: codes::UPSTREAM_FAILURE,
        status: 502,
    })?;
    let addr = SocketAddr::new(ip, target.port);
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(timeout_seconds))
        .connect_timeout(Duration::from_secs(timeout_seconds))
        .redirect(reqwest::redirect::Policy::none())
        // The oracle connects to the pinned address directly. Ambient
        // HTTP(S)_PROXY would send the request somewhere else and re-introduce
        // the DNS lookup the lock exists to skip.
        .no_proxy()
        .resolve(&target.host, addr)
        .build()
        .map_err(|error| transport_failure(&error))?;
    let url = format!(
        "{}://{}{}",
        target.scheme, target.host_header, target.request_target
    );
    let response = client
        .get(&url)
        .header(reqwest::header::USER_AGENT, FETCH_URL_USER_AGENT)
        .header(reqwest::header::ACCEPT, FETCH_URL_ACCEPT)
        .header(reqwest::header::HOST, &target.host_header)
        .send()
        .map_err(|error| transport_failure(&error))?;
    let status = response.status().as_u16();
    let location = response
        .headers()
        .get(reqwest::header::LOCATION)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string();
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_string();
    let mut body = Vec::new();
    response
        .take(MAX_FETCH_BYTES as u64 + 1)
        .read_to_end(&mut body)
        .map_err(|error| AppError {
            message: format!("Cannot fetch URL: {error}"),
            code: codes::UPSTREAM_FAILURE,
            status: 502,
        })?;
    Ok(HttpResponse {
        status,
        location,
        content_type,
        body,
    })
}

fn transport_failure(error: &reqwest::Error) -> AppError {
    let reason = error.to_string();
    let timed_out = error.is_timeout() || reason.to_ascii_lowercase().contains("timed out");
    AppError {
        message: format!("Cannot fetch URL: {reason}"),
        code: if timed_out {
            codes::UPSTREAM_TIMEOUT
        } else {
            codes::UPSTREAM_FAILURE
        },
        status: 502,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn locked_http_get_connects_to_the_pinned_address_and_sends_the_oracle_headers() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let addr = listener.local_addr().expect("addr");
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept");
            let mut buf = [0_u8; 1024];
            let n = stream.read(&mut buf).expect("read");
            let request = String::from_utf8_lossy(&buf[..n]).into_owned();
            let body = b"<html><body><p>from the pinned socket</p></body></html>";
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            stream.write_all(response.as_bytes()).expect("headers");
            stream.write_all(body).expect("body");
            request
        });

        let target = PublicUrlTarget {
            url: "http://example.com/from-pin".to_string(),
            scheme: "http".to_string(),
            host: "example.com".to_string(),
            port: addr.port(),
            host_header: "example.com".to_string(),
            request_target: "/from-pin".to_string(),
            address: "127.0.0.1".to_string(),
        };
        let response = locked_http_get(&target, 5).expect("fetch");
        let request = server.join().expect("server thread");
        assert_eq!(response.status, 200);
        assert!(
            String::from_utf8_lossy(&response.body).contains("from the pinned socket"),
            "body: {:?}",
            String::from_utf8_lossy(&response.body)
        );
        let lowered = request.to_ascii_lowercase();
        assert!(lowered.contains("host: example.com"), "request: {request}");
        assert!(request.contains(FETCH_URL_USER_AGENT), "request: {request}");
        assert!(request.contains("GET /from-pin"), "request: {request}");
    }

    #[test]
    fn locked_http_get_surfaces_a_4xx_without_following_it() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let addr = listener.local_addr().expect("addr");
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept");
            let mut buf = [0_u8; 512];
            let _ = stream.read(&mut buf);
            let _ = stream.write_all(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/admin\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            );
        });
        let target = PublicUrlTarget {
            url: "http://example.com/redirect".to_string(),
            scheme: "http".to_string(),
            host: "example.com".to_string(),
            port: addr.port(),
            host_header: "example.com".to_string(),
            request_target: "/redirect".to_string(),
            address: "127.0.0.1".to_string(),
        };
        let response = locked_http_get(&target, 5).expect("fetch");
        assert_eq!(response.status, 302);
        assert_eq!(response.location, "http://127.0.0.1/admin");
    }
}
