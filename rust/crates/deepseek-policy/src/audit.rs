use deepseek_core::TraceId;
use serde::{Deserialize, Serialize};

use crate::PolicyDecision;
use crate::capability::Capability;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AuditEvent {
    pub trace_id: Option<TraceId>,
    pub capability: Capability,
    pub decision: PolicyDecision,
    pub reason: Option<String>,
}

impl AuditEvent {
    pub fn new(capability: Capability, decision: PolicyDecision) -> Self {
        AuditEvent {
            trace_id: None,
            capability,
            decision,
            reason: None,
        }
    }

    pub fn with_trace_id(mut self, trace_id: TraceId) -> Self {
        self.trace_id = Some(trace_id);
        self
    }

    pub fn with_reason(mut self, reason: impl Into<String>) -> Self {
        self.reason = Some(reason.into());
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audit_event_roundtrips_json() {
        let event = AuditEvent::new(
            Capability::NetworkFetch,
            PolicyDecision::Deny {
                reason: "private IP".to_string(),
            },
        )
        .with_trace_id(TraceId("trace-abc".to_string()))
        .with_reason("blocked by url guard");

        let json = serde_json::to_string(&event).unwrap();
        let parsed: AuditEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(event, parsed);
    }

    #[test]
    fn audit_event_without_optional_fields_roundtrips() {
        let event = AuditEvent::new(Capability::ReadFile, PolicyDecision::Allow);
        let json = serde_json::to_string(&event).unwrap();
        let parsed: AuditEvent = serde_json::from_str(&json).unwrap();
        assert_eq!(event, parsed);
        assert!(parsed.trace_id.is_none());
        assert!(parsed.reason.is_none());
    }
}
