use serde::{Deserialize, Serialize};

use crate::PolicyDecision;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Capability {
    ReadFile,
    WriteFile,
    NetworkFetch,
    ShellExec,
    BrowserControl,
    McpToolCall,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, PartialOrd, Ord)]
pub enum RiskLevel {
    Low,
    Medium,
    High,
    Critical,
}

pub fn capability_baseline_risk(cap: &Capability) -> RiskLevel {
    match cap {
        Capability::ReadFile => RiskLevel::Low,
        Capability::NetworkFetch => RiskLevel::Medium,
        Capability::McpToolCall => RiskLevel::Medium,
        Capability::WriteFile => RiskLevel::High,
        Capability::BrowserControl => RiskLevel::High,
        Capability::ShellExec => RiskLevel::Critical,
    }
}

pub fn is_capability_allowed(
    requested: Capability,
    granted: &[Capability],
    max_risk: RiskLevel,
) -> PolicyDecision {
    if !granted.contains(&requested) {
        return PolicyDecision::Deny {
            reason: "capability not granted".to_string(),
        };
    }

    let required = capability_baseline_risk(&requested);
    if required > max_risk {
        return PolicyDecision::Deny {
            reason: format!("risk level {required:?} exceeds max allowed {max_risk:?}"),
        };
    }

    PolicyDecision::Allow
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capability_allows_granted_low_risk() {
        let decision = is_capability_allowed(
            Capability::ReadFile,
            &[Capability::ReadFile],
            RiskLevel::Low,
        );
        assert!(decision.is_allowed());
    }

    #[test]
    fn capability_denies_missing_capability() {
        let decision = is_capability_allowed(
            Capability::ShellExec,
            &[Capability::ReadFile, Capability::NetworkFetch],
            RiskLevel::Critical,
        );
        assert!(!decision.is_allowed());
    }

    #[test]
    fn capability_denies_risk_too_high() {
        let decision = is_capability_allowed(
            Capability::ShellExec,
            &[Capability::ShellExec],
            RiskLevel::High,
        );
        assert!(!decision.is_allowed());
    }

    #[test]
    fn capability_allows_at_exact_risk() {
        let decision = is_capability_allowed(
            Capability::WriteFile,
            &[Capability::WriteFile],
            RiskLevel::High,
        );
        assert!(decision.is_allowed());
    }

    #[test]
    fn risk_level_ordering_is_correct() {
        assert!(RiskLevel::Low < RiskLevel::Medium);
        assert!(RiskLevel::Medium < RiskLevel::High);
        assert!(RiskLevel::High < RiskLevel::Critical);
    }
}
