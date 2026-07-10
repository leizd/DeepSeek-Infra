use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use deepseek_core::{TraceId, version_info};
use serde::{Deserialize, Serialize};

use crate::capability::{Capability, RiskLevel};

pub mod audit;
pub mod capability;
pub mod path_guard;
pub mod url_guard;

pub fn policy_version() -> &'static str {
    version_info().version
}

pub mod codes {
    pub const ALLOWED: &str = "allowed";
    pub const UNSUPPORTED_SCHEME: &str = "unsupported_scheme";
    pub const LOCALHOST_BLOCKED: &str = "localhost_blocked";
    pub const PRIVATE_NETWORK_BLOCKED: &str = "private_network_blocked";
    pub const LINK_LOCAL_BLOCKED: &str = "link_local_blocked";
    pub const PATH_TRAVERSAL: &str = "path_traversal";
    pub const PROTECTED_PATH: &str = "protected_path";
    pub const MISSING_CAPABILITY: &str = "missing_capability";
    pub const RISK_LIMIT_EXCEEDED: &str = "risk_limit_exceeded";
    pub const INVALID_POLICY_REQUEST: &str = "invalid_policy_request";
    pub const POLICY_BACKEND_UNAVAILABLE: &str = "policy_backend_unavailable";
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PolicyDecision {
    pub allowed: bool,
    pub code: String,
    pub reason: String,
    pub decision_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<TraceId>,
    pub capability: Capability,
    pub risk_level: RiskLevel,
}

impl PolicyDecision {
    pub fn allow(capability: Capability, risk_level: RiskLevel) -> Self {
        Self::new(
            true,
            codes::ALLOWED,
            "policy checks passed",
            capability,
            risk_level,
        )
    }

    pub fn deny(
        code: impl Into<String>,
        reason: impl Into<String>,
        capability: Capability,
        risk_level: RiskLevel,
    ) -> Self {
        Self::new(false, code, reason, capability, risk_level)
    }

    pub fn invalid_request(reason: impl Into<String>) -> Self {
        Self::deny(
            codes::INVALID_POLICY_REQUEST,
            reason,
            Capability::ReadFile,
            RiskLevel::High,
        )
    }

    fn new(
        allowed: bool,
        code: impl Into<String>,
        reason: impl Into<String>,
        capability: Capability,
        risk_level: RiskLevel,
    ) -> Self {
        Self {
            allowed,
            code: code.into(),
            reason: reason.into(),
            decision_id: next_decision_id(),
            trace_id: None,
            capability,
            risk_level,
        }
    }

    pub fn with_trace_id(mut self, trace_id: Option<TraceId>) -> Self {
        self.trace_id = trace_id;
        self
    }

    pub fn with_context(mut self, capability: Capability, risk_level: RiskLevel) -> Self {
        self.capability = capability;
        self.risk_level = risk_level;
        self
    }

    pub fn is_allowed(&self) -> bool {
        self.allowed
    }
}

static DECISION_SEQUENCE: AtomicU64 = AtomicU64::new(1);

fn next_decision_id() -> String {
    let timestamp_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let sequence = DECISION_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    format!("pd_{timestamp_ms:013x}{sequence:06x}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn policy_version_matches_core() {
        assert_eq!(policy_version(), deepseek_core::version_info().version);
    }

    #[test]
    fn policy_decision_roundtrips() {
        let decisions = [
            PolicyDecision::allow(Capability::ReadFile, RiskLevel::Low),
            PolicyDecision::deny(
                codes::PROTECTED_PATH,
                "blocked",
                Capability::WriteFile,
                RiskLevel::High,
            )
            .with_trace_id(Some(TraceId("trace-123".to_string()))),
        ];
        for decision in decisions {
            let json = serde_json::to_string(&decision).unwrap();
            let parsed: PolicyDecision = serde_json::from_str(&json).unwrap();
            assert_eq!(decision, parsed);
        }
    }

    #[test]
    fn policy_decisions_have_unique_identifiers() {
        let first = PolicyDecision::allow(Capability::ReadFile, RiskLevel::Low);
        let second = PolicyDecision::allow(Capability::ReadFile, RiskLevel::Low);
        assert!(first.decision_id.starts_with("pd_"));
        assert_ne!(first.decision_id, second.decision_id);
    }
}
