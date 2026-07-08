use serde::{Deserialize, Serialize};
use std::net::IpAddr;

use crate::PolicyDecision;

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
        return PolicyDecision::Deny {
            reason: "missing URL scheme".to_string(),
        };
    };

    if !policy
        .allowed_schemes
        .iter()
        .any(|s| s.eq_ignore_ascii_case(scheme))
    {
        return PolicyDecision::Deny {
            reason: format!("scheme not allowed: {scheme}"),
        };
    }

    let host = extract_host(rest);

    if host.eq_ignore_ascii_case("localhost") {
        return PolicyDecision::Deny {
            reason: "localhost is blocked".to_string(),
        };
    }

    if let Ok(ip) = host.parse::<IpAddr>() {
        if is_blocked_ip(&ip) {
            return PolicyDecision::Deny {
                reason: "private or local IP address".to_string(),
            };
        }
    }

    PolicyDecision::Allow
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

fn is_blocked_ip(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => v4.is_loopback() || v4.is_private() || v4.is_link_local(),
        IpAddr::V6(v6) => {
            v6.is_loopback()
                || v6.is_unique_local()
                || v6.is_unicast_link_local()
                || v6.is_unspecified()
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
    }

    #[test]
    fn url_guard_denies_localhost() {
        let policy = UrlPolicy::default();
        let decision = validate_url_access("http://localhost:8080/", &policy);
        assert!(!decision.is_allowed());
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
