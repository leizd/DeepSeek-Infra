//! The Skill side of the Tool Policy Engine, mirroring
//! `deepseek_infra/infra/skills/permissions.py`.
//!
//! `skill_allowed_tools` already lives in [`super::schema`] (the probe compares it as the `tools`
//! op), so this module holds the binding Python keeps beside it: a Skill's own `allowedTools`,
//! evaluated through the engine.
//!
//! One divergence, in a side effect rather than in the answer: the oracle's `ToolPolicy` defaults
//! to `audit=True` and writes an audit entry on every evaluation, so its `evaluate_skill_tool`
//! leaves a line in the tool-audit directory. This port evaluates with the crate's default audit
//! sink, which is a no-op, so nothing is written.
use super::{Result, schema, text};
use crate::tool_policy::{ToolPolicy, ToolPolicyConfig, ToolPolicyDecision};
use serde_json::{Value, json};

/// `build_skill_tool_policy`'s scope: `project:<id>` when a project is bound, else
/// `skill:<skillId or unknown>`.
fn scope_for(skill: &Value, project_id: &str) -> String {
    if !project_id.is_empty() {
        return format!("project:{project_id}");
    }
    let skill_id = text(skill, "skillId");
    if skill_id.is_empty() {
        "skill:unknown".to_string()
    } else {
        format!("skill:{skill_id}")
    }
}

/// `permissions.evaluate_skill_tool`: the Skill's own tool grant, gated by the engine with an empty
/// argument object — which is how `_tool_policy_pass` asks its `deniedTools` question.
pub fn evaluate_skill_tool(skill: &Value, tool_name: &str) -> Result<ToolPolicyDecision> {
    evaluate_skill_tool_with(skill, tool_name, &json!({}), "")
}

/// As [`evaluate_skill_tool`], with the arguments and project the caller has.
pub fn evaluate_skill_tool_with(
    skill: &Value,
    tool_name: &str,
    arguments: &Value,
    project_id: &str,
) -> Result<ToolPolicyDecision> {
    let mut policy = ToolPolicy::new(ToolPolicyConfig {
        allowed_tools: Some(schema::skill_allowed_tools(skill)?),
        enforce_schema: false,
        scope: scope_for(skill, project_id),
        ..ToolPolicyConfig::default()
    });
    Ok(policy.evaluate(tool_name, Some(arguments), None))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn skill(allowed: Value) -> Value {
        json!({"skillId": "demo", "allowedTools": allowed})
    }

    #[test]
    fn a_granted_tool_is_allowed_and_an_ungranted_one_is_denied() {
        let skill = skill(json!(["search_files"]));
        assert!(
            evaluate_skill_tool(&skill, "search_files")
                .unwrap()
                .allowed()
        );
        let denied = evaluate_skill_tool(&skill, "python_eval").unwrap();
        assert!(!denied.allowed());
        assert_eq!(denied.capability, "full");
    }

    #[test]
    fn an_unknown_tool_is_denied_before_the_grant_is_consulted() {
        let decision =
            evaluate_skill_tool(&skill(json!(["search_files"])), "no_such_tool").unwrap();
        assert!(!decision.allowed());
        assert_eq!(decision.reasons, ["unknown_tool"]);
        assert!(decision.reason.contains("Unknown tool"));
    }

    #[test]
    fn the_scope_follows_the_project_then_the_skill() {
        assert_eq!(scope_for(&skill(json!([])), ""), "skill:demo");
        assert_eq!(scope_for(&json!({}), ""), "skill:unknown");
        assert_eq!(scope_for(&skill(json!([])), "proj-1"), "project:proj-1");
    }
}
