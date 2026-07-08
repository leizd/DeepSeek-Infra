use deepseek_core::version_info;
use serde::{Deserialize, Serialize};

pub mod audit;
pub mod capability;
pub mod path_guard;
pub mod url_guard;

pub fn policy_version() -> &'static str {
    version_info().version
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "decision")]
pub enum PolicyDecision {
    Allow,
    Deny { reason: String },
}

impl PolicyDecision {
    pub fn is_allowed(&self) -> bool {
        matches!(self, PolicyDecision::Allow)
    }
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
            PolicyDecision::Allow,
            PolicyDecision::Deny {
                reason: "blocked".to_string(),
            },
        ];
        for decision in decisions {
            let json = serde_json::to_string(&decision).unwrap();
            let parsed: PolicyDecision = serde_json::from_str(&json).unwrap();
            assert_eq!(decision, parsed);
        }
    }
}
