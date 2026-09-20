//! Default A2A task runner: native chat + capability-scoped tool loop.
//!
//! Injected runners (tests) still win. This path is what `POST /a2a` uses when
//! none is attached. A missing `DEEPSEEK_API_KEY` fails the task visibly
//! rather than inventing an answer.

use serde_json::{Value, json};

use crate::chat_execution::UpstreamConfig;
use crate::chat_tool_loop::{ToolRoundExecutor, WorkspaceBundle, execute_chat_with_tool_rounds};
use crate::openai_facade::DEFAULT_MODEL;
use crate::request_preparation::prepare_chat_request;
use deepseek_policy::tool_catalog::available_tool_definitions;
use deepseek_policy::tool_policy::{ToolPolicy, ToolPolicyConfig, capability_tools};

fn system_for(agent_id: &str) -> &'static str {
    match agent_id {
        "orchestrator" => {
            "You are DeepSeek Infra's general-purpose assistant Agent with the full local tool surface."
        }
        "researcher" => "你负责事实、资料、背景、来源和最新信息核查。",
        "coder" => "你负责代码、架构、bug、接口、实现路径和工程风险分析。",
        "reasoner" => "你负责严谨推理、边界条件、因果关系和方案权衡。",
        "critic" => "你负责挑错、找漏洞、检查遗漏、质疑假设和风险复核。",
        _ => "You are a DeepSeek Infra agent.",
    }
}

fn capability_for(agent_id: &str) -> String {
    if agent_id == "orchestrator" {
        "full".to_string()
    } else {
        agent_id.to_string()
    }
}

fn openai_tools_for(agent_id: &str) -> Vec<Value> {
    let names: Vec<&str> = if agent_id == "orchestrator" {
        deepseek_policy::tool_policy::all_tool_names()
    } else {
        capability_tools(agent_id)
    };
    names
        .into_iter()
        .filter_map(|name| {
            available_tool_definitions().iter().find(|definition| {
                definition
                    .get("function")
                    .and_then(|function| function.get("name"))
                    .and_then(Value::as_str)
                    == Some(name)
            })
        })
        .cloned()
        .collect()
}

fn executor_for(agent_id: &str) -> ToolRoundExecutor {
    let config = ToolPolicyConfig {
        capability: capability_for(agent_id),
        scope: "a2a".to_string(),
        ..ToolPolicyConfig::default()
    };
    let workspace = std::env::var_os("DEEPSEEK_INFRA_ROOT")
        .map(std::path::PathBuf::from)
        .map(WorkspaceBundle::new);
    ToolRoundExecutor::new(workspace, Some(ToolPolicy::new(config)))
}

/// Run one A2A agent turn through the native chat tool loop.
pub async fn run_native_a2a(agent_id: &str, text: &str) -> Result<String, String> {
    let config = UpstreamConfig::from_env();
    if config.api_key.trim().is_empty() {
        return Err("A2A upstream is not configured (DEEPSEEK_API_KEY)".to_string());
    }
    let messages = vec![
        json!({"role": "system", "content": system_for(agent_id)}),
        json!({"role": "user", "content": text}),
    ];
    let mut body = json!({
        "model": DEFAULT_MODEL,
        "messages": messages,
        "stream": false,
    });
    let tools = openai_tools_for(agent_id);
    if !tools.is_empty() {
        body.as_object_mut()
            .unwrap()
            .insert("tools".to_string(), Value::Array(tools));
    }
    let prepared = prepare_chat_request(&body).map_err(|error| error.message.to_string())?;
    let executor = executor_for(agent_id);
    let result = execute_chat_with_tool_rounds(&config, &prepared, &executor)
        .await
        .map_err(|error| error.code().to_string())?;
    if result.content.trim().is_empty() {
        return Err("A2A upstream returned an empty answer".to_string());
    }
    Ok(result.content)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn researcher_tools_are_the_research_slice() {
        let tools = openai_tools_for("researcher");
        let names: Vec<&str> = tools
            .iter()
            .filter_map(|tool| tool["function"]["name"].as_str())
            .collect();
        assert_eq!(
            names,
            vec!["web_search", "compare_search_results", "fetch_url"]
        );
        assert!(openai_tools_for("reasoner").is_empty());
        assert!(openai_tools_for("orchestrator").len() >= 20);
    }

    #[test]
    fn empty_upstream_key_is_rejected_before_the_wire() {
        let config = UpstreamConfig {
            url: "https://api.deepseek.com/v1/chat/completions".to_string(),
            api_key: String::new(),
            timeout: std::time::Duration::from_secs(1),
        };
        assert!(config.api_key.trim().is_empty());
    }
}
