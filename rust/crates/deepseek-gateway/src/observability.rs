use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Instant;

use axum::body::{Body, HttpBody};
use axum::extract::Request;
use axum::http::{HeaderName, HeaderValue, StatusCode, header::CONTENT_LENGTH};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};

const COMPONENTS: [&str; 7] = [
    "gateway_prepare",
    "mcp_prepare",
    "policy_url",
    "policy_path",
    "policy_capability",
    "rag_vector_rank",
    "rag_document_prepare",
];
const OUTCOMES: [&str; 3] = ["success", "client_error", "server_error"];
const BACKEND_REASONS: [&str; 4] = ["timeout", "unavailable", "malformed_response", "internal"];
const DURATION_BUCKETS: [f64; 10] = [0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0];
const BYTE_BUCKETS: [f64; 9] = [
    256.0,
    1024.0,
    4096.0,
    16_384.0,
    65_536.0,
    262_144.0,
    1_048_576.0,
    4_194_304.0,
    16_777_216.0,
];
const REQUEST_ID_HEADER: HeaderName = HeaderName::from_static("x-deepseek-request-id");
const RUST_PROCESSING_HEADER: HeaderName = HeaderName::from_static("x-deepseek-rust-processing-us");

static METRICS: OnceLock<Mutex<Metrics>> = OnceLock::new();
static REQUEST_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Clone)]
struct Histogram {
    buckets: &'static [f64],
    counts: Vec<u64>,
    count: u64,
    sum: f64,
}

impl Histogram {
    fn new(buckets: &'static [f64]) -> Self {
        Self {
            buckets,
            counts: vec![0; buckets.len()],
            count: 0,
            sum: 0.0,
        }
    }

    fn observe(&mut self, value: f64) {
        self.count += 1;
        self.sum += value;
        for (index, boundary) in self.buckets.iter().enumerate() {
            if value <= *boundary {
                self.counts[index] += 1;
            }
        }
    }
}

struct Metrics {
    requests: BTreeMap<(&'static str, &'static str), u64>,
    duration: BTreeMap<&'static str, Histogram>,
    request_bytes: BTreeMap<&'static str, Histogram>,
    response_bytes: BTreeMap<&'static str, Histogram>,
    backend_errors: BTreeMap<(&'static str, &'static str), u64>,
}

impl Metrics {
    fn new() -> Self {
        let mut metrics = Self {
            requests: BTreeMap::new(),
            duration: BTreeMap::new(),
            request_bytes: BTreeMap::new(),
            response_bytes: BTreeMap::new(),
            backend_errors: BTreeMap::new(),
        };
        for component in COMPONENTS {
            metrics
                .duration
                .insert(component, Histogram::new(&DURATION_BUCKETS));
            metrics
                .request_bytes
                .insert(component, Histogram::new(&BYTE_BUCKETS));
            metrics
                .response_bytes
                .insert(component, Histogram::new(&BYTE_BUCKETS));
            for outcome in OUTCOMES {
                metrics.requests.insert((component, outcome), 0);
            }
            for reason in BACKEND_REASONS {
                metrics.backend_errors.insert((component, reason), 0);
            }
        }
        metrics
    }

    fn record(
        &mut self,
        component: &'static str,
        outcome: &'static str,
        duration_seconds: f64,
        request_bytes: usize,
        response_bytes: usize,
    ) {
        *self.requests.entry((component, outcome)).or_default() += 1;
        if let Some(histogram) = self.duration.get_mut(component) {
            histogram.observe(duration_seconds);
        }
        if let Some(histogram) = self.request_bytes.get_mut(component) {
            histogram.observe(request_bytes as f64);
        }
        if let Some(histogram) = self.response_bytes.get_mut(component) {
            histogram.observe(response_bytes as f64);
        }
        if outcome == "server_error" {
            *self
                .backend_errors
                .entry((component, "internal"))
                .or_default() += 1;
        }
    }
}

fn registry() -> &'static Mutex<Metrics> {
    METRICS.get_or_init(|| Mutex::new(Metrics::new()))
}

fn component_for_path(path: &str) -> Option<&'static str> {
    match path {
        "/gateway/request/prepare" => Some("gateway_prepare"),
        "/mcp" | "/mcp/request/prepare" => Some("mcp_prepare"),
        "/policy/url" => Some("policy_url"),
        "/policy/path" => Some("policy_path"),
        "/policy/capability" => Some("policy_capability"),
        "/rag/vectors/rank" => Some("rag_vector_rank"),
        "/rag/documents/prepare" => Some("rag_document_prepare"),
        _ => None,
    }
}

fn valid_correlation_id(value: &str) -> bool {
    (16..=64).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
}

fn correlation_id(request: &Request) -> String {
    request
        .headers()
        .get(&REQUEST_ID_HEADER)
        .and_then(|value| value.to_str().ok())
        .filter(|value| valid_correlation_id(value))
        .map(str::to_string)
        .unwrap_or_else(|| {
            format!(
                "rs-{:016x}",
                REQUEST_SEQUENCE.fetch_add(1, Ordering::Relaxed)
            )
        })
}

fn content_length(request: &Request) -> usize {
    request
        .headers()
        .get(CONTENT_LENGTH)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(0)
}

fn body_size(response: &Response<Body>) -> usize {
    response
        .body()
        .size_hint()
        .exact()
        .and_then(|value| usize::try_from(value).ok())
        .unwrap_or(0)
}

fn outcome(status: StatusCode) -> (&'static str, &'static str) {
    if status.is_server_error() {
        ("server_error", "http_server_error")
    } else if status.is_client_error() {
        ("client_error", "http_client_error")
    } else {
        ("success", "")
    }
}

pub async fn observe_sidecar_request(request: Request, next: Next) -> Response {
    let Some(component) = component_for_path(request.uri().path()) else {
        return next.run(request).await;
    };
    let payload_bytes = content_length(&request);
    let request_id = correlation_id(&request);
    let started = Instant::now();
    let mut response = next.run(request).await;
    let elapsed = started.elapsed();
    let duration_us = elapsed.as_micros().min(u128::from(u64::MAX)) as u64;
    let response_bytes = body_size(&response);
    let (outcome, stable_error_code) = outcome(response.status());
    if let Ok(mut metrics) = registry().lock() {
        metrics.record(
            component,
            outcome,
            elapsed.as_secs_f64(),
            payload_bytes,
            response_bytes,
        );
    }
    if let Ok(value) = HeaderValue::from_str(&duration_us.to_string()) {
        response.headers_mut().insert(RUST_PROCESSING_HEADER, value);
    }
    if let Ok(value) = HeaderValue::from_str(&request_id) {
        response.headers_mut().insert(REQUEST_ID_HEADER, value);
    }
    tracing::info!(
        component,
        payload_bytes,
        response_bytes,
        duration_us,
        outcome,
        stable_error_code,
        correlation_id = request_id,
        "rust sidecar request"
    );
    response
}

fn render_histogram(output: &mut String, name: &str, component: &str, histogram: &Histogram) {
    for (boundary, count) in histogram.buckets.iter().zip(&histogram.counts) {
        output.push_str(&format!(
            "{name}_bucket{{component=\"{component}\",le=\"{boundary}\"}} {count}\n"
        ));
    }
    output.push_str(&format!(
        "{name}_bucket{{component=\"{component}\",le=\"+Inf\"}} {}\n",
        histogram.count
    ));
    output.push_str(&format!(
        "{name}_sum{{component=\"{component}\"}} {}\n",
        histogram.sum
    ));
    output.push_str(&format!(
        "{name}_count{{component=\"{component}\"}} {}\n",
        histogram.count
    ));
}

pub async fn metrics() -> impl IntoResponse {
    let mut output = String::new();
    output.push_str("# TYPE requests_total counter\n");
    output.push_str("# TYPE request_duration_seconds histogram\n");
    output.push_str("# TYPE request_payload_bytes histogram\n");
    output.push_str("# TYPE response_payload_bytes histogram\n");
    output.push_str("# TYPE backend_errors_total counter\n");
    if let Ok(metrics) = registry().lock() {
        for ((component, outcome), count) in &metrics.requests {
            output.push_str(&format!(
                "requests_total{{component=\"{component}\",outcome=\"{outcome}\"}} {count}\n"
            ));
        }
        for (component, histogram) in &metrics.duration {
            render_histogram(
                &mut output,
                "request_duration_seconds",
                component,
                histogram,
            );
        }
        for (component, histogram) in &metrics.request_bytes {
            render_histogram(&mut output, "request_payload_bytes", component, histogram);
        }
        for (component, histogram) in &metrics.response_bytes {
            render_histogram(&mut output, "response_payload_bytes", component, histogram);
        }
        for ((component, reason), count) in &metrics.backend_errors {
            output.push_str(&format!(
                "backend_errors_total{{component=\"{component}\",reason=\"{reason}\"}} {count}\n"
            ));
        }
    }
    (
        [("content-type", "text/plain; version=0.0.4; charset=utf-8")],
        output,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn component_and_label_values_are_bounded() {
        assert_eq!(component_for_path("/policy/url"), Some("policy_url"));
        assert_eq!(component_for_path("/policy/url/example.com"), None);
        assert_eq!(OUTCOMES, ["success", "client_error", "server_error"]);
        assert_eq!(BACKEND_REASONS.len(), 4);
    }

    #[test]
    fn arbitrary_correlation_ids_are_rejected() {
        assert!(valid_correlation_id("0123456789abcdef"));
        assert!(!valid_correlation_id("user input with spaces"));
        assert!(!valid_correlation_id("../../secret-token"));
    }
}
