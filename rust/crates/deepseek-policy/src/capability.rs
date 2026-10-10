use serde::{Deserialize, Serialize};

use crate::{PolicyDecision, codes};

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
    let required = capability_baseline_risk(&requested);
    if !granted.contains(&requested) {
        return PolicyDecision::deny(
            codes::MISSING_CAPABILITY,
            "required capability was not granted",
            requested,
            required,
        );
    }

    if required > max_risk {
        return PolicyDecision::deny(
            codes::RISK_LIMIT_EXCEEDED,
            "capability risk exceeds the configured limit",
            requested,
            required,
        );
    }

    PolicyDecision::allow(requested, required)
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
        assert_eq!(decision.code, codes::MISSING_CAPABILITY);
    }

    #[test]
    fn capability_denies_risk_too_high() {
        let decision = is_capability_allowed(
            Capability::ShellExec,
            &[Capability::ShellExec],
            RiskLevel::High,
        );
        assert!(!decision.is_allowed());
        assert_eq!(decision.code, codes::RISK_LIMIT_EXCEEDED);
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
