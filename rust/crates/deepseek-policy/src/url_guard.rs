//! URL guard for the `/policy/url` route.
//!
//! **This delegates to [`crate::tool_policy::evaluate_url_safety`]**, the
//! oracle-parity guard, rather than carrying its own rules. The earlier
//! standalone implementation was strictly weaker than the Python it stands in
//! for:
//!
//! - it never checked the `.local` / `.localhost` / `.internal` suffixes, nor
//!   stripped a trailing dot, so `http://printer.local/` and
//!   `http://localhost./` passed;
//! - it *stripped* URL credentials and allowed the request, where the oracle
//!   denies `user:pass@host` outright;
//! - it rejected only loopback / link-local / private / unspecified addresses,
//!   so multicast, reserved, CGNAT (`100.64.0.0/10`) and every other
//!   non-global IPv4 range — plus the whole IPv6 reserved set — passed.
//!
//! That matters because `deepseek_infra.infra.rust_core.policy_client` can
//! delegate the tool gate to these routes (`DEEPSEEK_RUST_POLICY`). Delegating to
//! a weaker guard would have *lowered* the security posture on the flip of that
//! flag. The response envelope is unchanged; only the verdict is now the
//! oracle's.
//!
//! The guard can only be *tightened* by [`UrlPolicy`], never loosened: the oracle
//! accepts http(s) only, so `allowed_schemes` cannot reintroduce a scheme the
//! oracle rejects.

use serde::{Deserialize, Serialize};
use std::net::IpAddr;

use crate::capability::{Capability, RiskLevel};
use crate::tool_policy::evaluate_url_safety;
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

/// Validate a target URL the way the oracle's tool gate does.
///
/// The returned `code` is derived from the oracle's denial reason. The bridge
/// treats the code as opaque — `policy_client._parse_response` only requires a
/// non-empty string — so mapping the oracle's messages onto codes leaves the wire
/// contract intact. It does mean the oracle's single "private or local ip"
/// verdict now covers loopback, link-local, reserved and multicast alike, exactly
/// as the oracle reports them.
pub fn validate_url_access(url: &str, policy: &UrlPolicy) -> PolicyDecision {
    let (safe, reason) = evaluate_url_safety(url);
    if !safe {
        return deny(code_for_reason(&reason), &reason);
    }

    // The oracle already restricted the scheme to http(s); a caller policy may
    // still be stricter, which is the only direction this knob may move.
    match scheme_lower(url) {
        Some(scheme)
            if policy
                .allowed_schemes
                .iter()
                .any(|allowed| allowed.eq_ignore_ascii_case(&scheme)) =>
        {
            PolicyDecision::allow(Capability::NetworkFetch, RiskLevel::High)
        }
        _ => deny(codes::UNSUPPORTED_SCHEME, "URL scheme is not allowed"),
    }
}

/// Map an oracle denial reason onto the crate's decision codes.
fn code_for_reason(reason: &str) -> &'static str {
    if reason.starts_with("scheme not allowed") {
        codes::UNSUPPORTED_SCHEME
    } else if reason == "url credentials are not allowed" {
        codes::URL_CREDENTIALS_BLOCKED
    } else if reason == "local host is not allowed" {
        codes::LOCALHOST_BLOCKED
    } else if let Some(literal) = reason.strip_prefix("private or local ip is not allowed: ") {
        match literal.parse::<IpAddr>() {
            Ok(IpAddr::V4(ip)) if ip.is_loopback() => codes::LOCALHOST_BLOCKED,
            Ok(IpAddr::V4(ip)) if ip.is_link_local() => codes::LINK_LOCAL_BLOCKED,
            Ok(IpAddr::V6(ip)) if ip.is_loopback() => codes::LOCALHOST_BLOCKED,
            Ok(IpAddr::V6(ip)) if ip.is_unicast_link_local() => codes::LINK_LOCAL_BLOCKED,
            Ok(IpAddr::V6(ip))
                if ip
                    .to_ipv4_mapped()
                    .is_some_and(|mapped| mapped.is_loopback()) =>
            {
                codes::LOCALHOST_BLOCKED
            }
            Ok(IpAddr::V6(ip))
                if ip
                    .to_ipv4_mapped()
                    .is_some_and(|mapped| mapped.is_link_local()) =>
            {
                codes::LINK_LOCAL_BLOCKED
            }
            _ => codes::PRIVATE_NETWORK_BLOCKED,
        }
    } else {
        // "empty url", "invalid url", "missing host" — none of these is a valid
        // request target.
        codes::INVALID_POLICY_REQUEST
    }
}

/// The lowercased scheme, mirroring `urlsplit`'s scheme detection.
fn scheme_lower(url: &str) -> Option<String> {
    let colon = url.find(':')?;
    let prefix = &url[..colon];
    let mut chars = prefix.chars();
    if !chars.next()?.is_ascii_alphabetic() {
        return None;
    }
    if !chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.')) {
        return None;
    }
    Some(prefix.to_ascii_lowercase())
}

fn deny(code: &str, reason: &str) -> PolicyDecision {
    PolicyDecision::deny(code, reason, Capability::NetworkFetch, RiskLevel::High)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn blocked(url: &str) -> PolicyDecision {
        let decision = validate_url_access(url, &UrlPolicy::default());
        assert!(!decision.is_allowed(), "{url} should be blocked");
        decision
    }

    #[test]
    fn url_guard_allows_https_public_host() {
        let policy = UrlPolicy::default();
        assert!(validate_url_access("https://example.com/path", &policy).is_allowed());
        assert!(validate_url_access("http://example.com:8080/", &policy).is_allowed());
    }

    #[test]
    fn url_guard_denies_file_scheme() {
        assert_eq!(
            blocked("file:///etc/passwd").code,
            codes::UNSUPPORTED_SCHEME
        );
        assert_eq!(
            blocked("ftp://example.com/").code,
            codes::UNSUPPORTED_SCHEME
        );
        // "No scheme" is reported by the oracle as `scheme not allowed: (none)`,
        // so it maps to the scheme code rather than to an invalid request.
        assert_eq!(blocked("example.com/path").code, codes::UNSUPPORTED_SCHEME);
        assert_eq!(blocked("//example.com/").code, codes::UNSUPPORTED_SCHEME);
        assert_eq!(blocked("").code, codes::INVALID_POLICY_REQUEST);
    }

    #[test]
    fn url_guard_denies_localhost() {
        assert_eq!(
            blocked("http://localhost:8080/").code,
            codes::LOCALHOST_BLOCKED
        );
        assert_eq!(blocked("http://LOCALHOST/").code, codes::LOCALHOST_BLOCKED);
    }

    /// These all passed before the guard delegated to the oracle. Each is a real
    /// hole the standalone implementation left open.
    #[test]
    fn url_guard_denies_local_host_suffixes_and_a_trailing_dot() {
        for url in [
            "http://localhost./",
            "http://printer.local/",
            "http://svc.internal/",
            "http://x.localhost/",
        ] {
            assert_eq!(blocked(url).code, codes::LOCALHOST_BLOCKED, "{url}");
        }
        // A name that merely contains "local" is not a suffix match.
        assert!(validate_url_access("http://notlocal/", &UrlPolicy::default()).is_allowed());
    }

    /// Previously the guard split userinfo off at `@` and allowed the request.
    #[test]
    fn url_guard_denies_url_credentials() {
        assert_eq!(
            blocked("http://user:pass@example.com/").code,
            codes::URL_CREDENTIALS_BLOCKED
        );
        assert_eq!(
            blocked("http://user@example.com/").code,
            codes::URL_CREDENTIALS_BLOCKED
        );
    }

    #[test]
    fn url_guard_denies_ipv4_private_and_non_global_ranges() {
        assert_eq!(blocked("http://127.0.0.1/").code, codes::LOCALHOST_BLOCKED);
        assert_eq!(
            blocked("http://169.254.169.254/").code,
            codes::LINK_LOCAL_BLOCKED
        );
        for url in [
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            // Previously all allowed:
            "http://224.0.0.1/",
            "http://240.0.0.1/",
            "http://255.255.255.255/",
            "http://100.64.0.1/",
            "http://0.1.2.3/",
            "http://192.0.2.1/",
            "http://198.51.100.1/",
            "http://203.0.113.1/",
            "http://198.18.0.1/",
        ] {
            assert_eq!(blocked(url).code, codes::PRIVATE_NETWORK_BLOCKED, "{url}");
        }
        // A short IPv4 form is a hostname to the oracle, so it stays allowed.
        assert!(validate_url_access("http://127.1/", &UrlPolicy::default()).is_allowed());
    }

    #[test]
    fn url_guard_denies_ipv6_loopback_unique_local_and_link_local() {
        assert_eq!(blocked("http://[::1]/").code, codes::LOCALHOST_BLOCKED);
        assert_eq!(blocked("http://[fe80::1]/").code, codes::LINK_LOCAL_BLOCKED);
        for url in [
            "http://[::]/",
            "http://[fc00::1]/",
            // Previously all allowed:
            "http://[2002::1]/",
            "http://[64:ff9b::1]/",
            "http://[100::1]/",
            "http://[2001:db8::1]/",
            "http://[ff00::1]/",
            "http://[4000::1]/",
        ] {
            assert_eq!(blocked(url).code, codes::PRIVATE_NETWORK_BLOCKED, "{url}");
        }
        // `fec0::/10` is allowed by the oracle, so it is allowed here too.
        assert!(validate_url_access("http://[fec0::1]/", &UrlPolicy::default()).is_allowed());
    }

    #[test]
    fn url_guard_allows_public_ipv4_and_ipv6() {
        let policy = UrlPolicy::default();
        assert!(validate_url_access("http://8.8.8.8/", &policy).is_allowed());
        assert!(validate_url_access("http://[2001:4860:4860::8888]/", &policy).is_allowed());
    }

    /// A caller policy can be stricter than the oracle, but never looser.
    #[test]
    fn a_custom_policy_can_only_tighten_the_scheme_set() {
        let https_only = UrlPolicy {
            allowed_schemes: vec!["https".to_string()],
        };
        assert_eq!(
            validate_url_access("http://example.com/", &https_only).code,
            codes::UNSUPPORTED_SCHEME
        );
        assert!(validate_url_access("https://example.com/", &https_only).is_allowed());

        // Listing a scheme the oracle rejects does not reintroduce it.
        let permissive = UrlPolicy {
            allowed_schemes: vec!["http".to_string(), "https".to_string(), "ftp".to_string()],
        };
        assert_eq!(
            validate_url_access("ftp://example.com/", &permissive).code,
            codes::UNSUPPORTED_SCHEME
        );
    }
}
