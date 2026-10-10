use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use deepseek_core::{TraceId, UnixTimestampMillis};
use serde::{Deserialize, Serialize};

use crate::PolicyDecision;
use crate::capability::{Capability, RiskLevel};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditEvent {
    pub event: String,
    pub decision_id: String,
    pub trace_id: Option<TraceId>,
    pub capability: Capability,
    pub risk_level: RiskLevel,
    pub allowed: bool,
    pub code: String,
    pub reason: String,
    pub target: Option<String>,
    pub timestamp_ms: UnixTimestampMillis,
}

impl AuditEvent {
    pub fn from_decision(decision: &PolicyDecision, target: Option<String>) -> Self {
        Self {
            event: "tool_policy_decision".to_string(),
            decision_id: decision.decision_id.clone(),
            trace_id: decision.trace_id.clone(),
            capability: decision.capability,
            risk_level: decision.risk_level,
            allowed: decision.allowed,
            code: decision.code.clone(),
            reason: decision.reason.clone(),
            target,
            timestamp_ms: current_timestamp_ms(),
        }
    }
}

fn current_timestamp_ms() -> UnixTimestampMillis {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    UnixTimestampMillis(u64::try_from(millis).unwrap_or(u64::MAX))
}

pub fn redact_url_target(url: &str) -> Option<String> {
    let (scheme, rest) = url.split_once("://")?;
    if scheme.is_empty() || rest.is_empty() {
        return None;
    }

    let without_fragment = rest.split('#').next().unwrap_or(rest);
    let without_query = without_fragment
        .split('?')
        .next()
        .unwrap_or(without_fragment);
    let (authority, path) = without_query
        .split_once('/')
        .map_or((without_query, ""), |(authority, path)| (authority, path));
    let host = authority.rsplit('@').next().unwrap_or(authority);
    if host.is_empty() {
        return None;
    }

    let suffix = if path.is_empty() {
        String::new()
    } else {
        format!("/{path}")
    };
    Some(format!(
        "{}://{}{}",
        scheme.to_ascii_lowercase(),
        host,
        suffix
    ))
}

pub fn redact_path_target(requested: &str) -> Option<String> {
    let file_name = Path::new(requested).file_name()?.to_string_lossy();
    if file_name.is_empty() {
        None
    } else {
        Some(format!("<workspace>/{file_name}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_event_roundtrips_json() {
        let decision = PolicyDecision::deny(
            crate::codes::PRIVATE_NETWORK_BLOCKED,
            "private networks are blocked",
            Capability::NetworkFetch,
            RiskLevel::High,
        )
        .with_trace_id(Some(TraceId("trace-abc".to_string())));
        let event = AuditEvent::from_decision(
            &decision,
            redact_url_target("https://user:secret@example.com/path?token=abc"),
        );

        let json = serde_json::to_string(&event).unwrap();
        let parsed: AuditEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(event, parsed);
        assert_eq!(parsed.decision_id, decision.decision_id);
        assert_eq!(parsed.trace_id, decision.trace_id);
    }

    #[test]
    fn audit_event_without_optional_fields_roundtrips() {
        let decision = PolicyDecision::allow(Capability::ReadFile, RiskLevel::Low);
        let event = AuditEvent::from_decision(&decision, None);
        let json = serde_json::to_string(&event).unwrap();
        let parsed: AuditEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(event, parsed);
        assert!(parsed.trace_id.is_none());
        assert!(parsed.target.is_none());
    }

    #[test]
    fn url_audit_target_redacts_credentials_and_query_values() {
        let target = redact_url_target(
            "https://admin:secret@example.com/private/report?authorization=Bearer-secret#fragment",
        )
        .unwrap();
        assert_eq!(target, "https://example.com/private/report");
        assert!(!target.contains("secret"));
        assert!(!target.contains("authorization"));
    }

    #[test]
    fn path_audit_target_does_not_leak_workspace_root() {
        let target = redact_path_target("/srv/private/workspace/reports/result.txt").unwrap();
        assert_eq!(target, "<workspace>/result.txt");
        assert!(!target.contains("/srv/private"));
    }
}
