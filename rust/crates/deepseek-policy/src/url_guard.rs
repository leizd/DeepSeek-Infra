use serde::{Deserialize, Serialize};
use std::net::IpAddr;

use crate::capability::{Capability, RiskLevel};
use crate::{PolicyDecision, codes};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UrlPolicy {
    pub allowed_schemes: Vec<String>,
}

impl Default for UrlPolicy {
    fn default() -> Self {
        UrlPolicy {
            allowed_schemes: vec!["http".to_string(), "https".to_string()],
        }
    }
}

pub fn validate_url_access(url: &str, policy: &UrlPolicy) -> PolicyDecision {
    let Some((scheme, rest)) = url.split_once("://") else {
        return deny(
            codes::UNSUPPORTED_SCHEME,
            "URL scheme is missing or unsupported",
        );
    };

    if !policy
        .allowed_schemes
        .iter()
        .any(|s| s.eq_ignore_ascii_case(scheme))
    {
        return deny(codes::UNSUPPORTED_SCHEME, "URL scheme is not allowed");
    }

    let host = extract_host(rest);
    if host.is_empty() {
        return deny(codes::INVALID_POLICY_REQUEST, "URL host is required");
    }

    if host.eq_ignore_ascii_case("localhost") {
        return deny(
            codes::LOCALHOST_BLOCKED,
            "localhost addresses are not allowed",
        );
    }

    if let Ok(ip) = host.parse::<IpAddr>() {
        if let Some((code, reason)) = blocked_ip_reason(&ip) {
            return deny(code, reason);
        }
    }

    PolicyDecision::allow(Capability::NetworkFetch, RiskLevel::High)
}

fn deny(code: &str, reason: &str) -> PolicyDecision {
    PolicyDecision::deny(code, reason, Capability::NetworkFetch, RiskLevel::High)
}

fn extract_host(rest: &str) -> String {
    let without_path = rest.split('/').next().unwrap_or(rest);
    let without_credentials = without_path.split('@').next_back().unwrap_or(without_path);

    if let (Some(start), Some(end)) = (without_credentials.find('['), without_credentials.find(']'))
    {
        if start < end {
            return without_credentials[start + 1..end].to_string();
        }
    }

    let without_port = without_credentials
        .split(':')
        .next()
        .unwrap_or(without_credentials);
    without_port.to_string()
}

fn blocked_ip_reason(ip: &IpAddr) -> Option<(&'static str, &'static str)> {
    match ip {
        IpAddr::V4(v4) if v4.is_loopback() => Some((
            codes::LOCALHOST_BLOCKED,
            "loopback addresses are not allowed",
        )),
        IpAddr::V4(v4) if v4.is_link_local() => Some((
            codes::LINK_LOCAL_BLOCKED,
            "link-local addresses are not allowed",
        )),
        IpAddr::V4(v4) if v4.is_private() || v4.is_unspecified() => Some((
            codes::PRIVATE_NETWORK_BLOCKED,
            "private network addresses are not allowed",
        )),
        IpAddr::V4(_) => None,
        IpAddr::V6(v6) if v6.is_loopback() => Some((
            codes::LOCALHOST_BLOCKED,
            "loopback addresses are not allowed",
        )),
        IpAddr::V6(v6) if v6.is_unicast_link_local() => Some((
            codes::LINK_LOCAL_BLOCKED,
            "link-local addresses are not allowed",
        )),
        IpAddr::V6(v6) if v6.is_unique_local() || v6.is_unspecified() => Some((
            codes::PRIVATE_NETWORK_BLOCKED,
            "private network addresses are not allowed",
        )),
        IpAddr::V6(v6) => {
            let _ = v6;
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_guard_allows_https_public_host() {
        let policy = UrlPolicy::default();
        let decision = validate_url_access("https://example.com/path", &policy);
        assert!(decision.is_allowed());
    }

    #[test]
    fn url_guard_denies_file_scheme() {
        let policy = UrlPolicy::default();
        let decision = validate_url_access("file:///etc/passwd", &policy);
        assert!(!decision.is_allowed());
        assert_eq!(decision.code, codes::UNSUPPORTED_SCHEME);
    }

    #[test]
    fn url_guard_denies_localhost() {
        let policy = UrlPolicy::default();
        let decision = validate_url_access("http://localhost:8080/", &policy);
        assert!(!decision.is_allowed());
        assert_eq!(decision.code, codes::LOCALHOST_BLOCKED);
    }

    #[test]
    fn url_guard_denies_ipv4_private_ranges() {
        let policy = UrlPolicy::default();
        let blocked = [
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            "http://169.254.0.1/",
        ];
        for url in blocked {
            let decision = validate_url_access(url, &policy);
            assert!(!decision.is_allowed(), "{url} should be blocked");
        }
    }

    #[test]
    fn url_guard_denies_ipv6_loopback() {
        let policy = UrlPolicy::default();
        let decision = validate_url_access("http://[::1]/", &policy);
        assert!(!decision.is_allowed());
    }

    #[test]
    fn url_guard_denies_ipv6_unique_local() {
        let policy = UrlPolicy::default();
        let decision = validate_url_access("http://[fc00::1]/", &policy);
        assert!(!decision.is_allowed());
    }

    #[test]
    fn url_guard_denies_ipv6_link_local() {
        let policy = UrlPolicy::default();
        let decision = validate_url_access("http://[fe80::1]/", &policy);
        assert!(!decision.is_allowed());
        assert_eq!(decision.code, codes::LINK_LOCAL_BLOCKED);
    }

    #[test]
    fn url_guard_allows_public_ipv4() {
        let policy = UrlPolicy::default();
        let decision = validate_url_access("http://8.8.8.8/", &policy);
        assert!(decision.is_allowed());
    }

    #[test]
    fn url_guard_allows_public_ipv6() {
        let policy = UrlPolicy::default();
        let decision = validate_url_access("http://[2001:4860:4860::8888]/", &policy);
        assert!(decision.is_allowed());
    }
}
