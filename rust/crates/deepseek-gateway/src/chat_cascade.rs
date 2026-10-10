//! Complete draft/gate/judge/refine inference, replayed for NDJSON clients.
//! Cloud stages use the same native assembly and tool/search runner as JSON chat.

use std::time::Duration;

use axum::body::{Body, Bytes};
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::AppError;
use deepseek_policy::chat_stream_events::{ChatEvent, STREAM_MEDIA_TYPE, encode_stream_event};
use deepseek_policy::model_router::{self, CascadePlan, ModelRouterSettings};
use deepseek_policy::request_messages::validate_deepseek_payload;
use serde::Serialize;
use serde_json::{Value, json};

use crate::chat_execution::UpstreamConfig;
use crate::chat_ndjson::{SearchChatTurn, complete_nonstream, execution_app_error};
use crate::chat_tool_loop::ToolRoundExecutor;
use crate::request_assembly::build_deepseek_request;

const JUDGE_SYSTEM_PROMPT: &str = "你是答案质量评审。只根据候选回答是否充分、准确、直接解决用户问题来打分，输出一个 0 到 1 之间的小数（1 表示完全充分），不要任何解释或多余文字。";

fn model_turn(
    turn: &SearchChatTurn,
    model: &str,
    router: &ModelRouterSettings,
) -> Result<SearchChatTurn, AppError> {
    let mut stage = turn.clone();
    stage.payload["model"] = json!(model);
    stage.payload["cascade"] = json!(false);
    stage.payload["autoRoute"] = json!(false);
    stage.validated = validate_deepseek_payload(&stage.payload, &turn.validated.api_key, router)?;
    Ok(stage)
}

pub(crate) async fn complete(
    turn: SearchChatTurn,
    executor: &ToolRoundExecutor,
    query: String,
    config: UpstreamConfig,
    prefetch: bool,
    router: &ModelRouterSettings,
) -> Result<Value, AppError> {
    let plan = model_router::cascade_plan(&turn.payload, router);
    if !plan.enabled {
        return complete_nonstream(turn, executor, query, config, prefetch).await;
    }
    let draft = if plan.draft_provider == "ollama" {
        ollama_draft(&turn.payload, &plan).await
    } else {
        complete_nonstream(
            model_turn(&turn, &plan.draft_model, router)?,
            executor,
            query.clone(),
            config.clone(),
            prefetch,
        )
        .await?
    };
    let content = draft
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let gate = model_router::quality_gate(
        content,
        plan.min_chars,
        turn.payload.get("searchEnabled") == Some(&Value::Bool(true)),
    );
    let score = if plan.judge {
        Some(
            judge(&turn, content, &plan, config.clone(), router)
                .await
                .unwrap_or(1.0),
        )
    } else {
        None
    };
    let escalated = !gate.passed || score.is_some_and(|v| v < plan.judge_threshold);
    let mut block = json!({"enabled":true,"escalated":escalated,
        "draftModel":plan.draft_model,"refineModel":plan.refine_model,
        "draftProvider":plan.draft_provider,"judge":plan.judge,"gate":gate.to_value()});
    if let Some(score) = score {
        block["judgeScore"] = json!(format!("{score:.3}").parse::<f64>().unwrap_or(score));
    }
    let mut answer = if escalated {
        block["draftContentChars"] = json!(content.chars().count());
        complete_nonstream(
            model_turn(&turn, &plan.refine_model, router)?,
            executor,
            query,
            config,
            prefetch,
        )
        .await?
    } else {
        draft
    };
    if !answer.get("diagnostics").is_some_and(Value::is_object) {
        answer["diagnostics"] = json!({});
    }
    answer["diagnostics"]["modelCascade"] = block;
    Ok(answer)
}

async fn judge(
    turn: &SearchChatTurn,
    content: &str,
    plan: &CascadePlan,
    mut config: UpstreamConfig,
    router: &ModelRouterSettings,
) -> Result<f64, AppError> {
    let query = deepseek_policy::core_utils::latest_user_query(&turn.payload);
    let payload = json!({"apiKey":turn.payload.get("apiKey"),"tavilyApiKey":turn.payload.get("tavilyApiKey"),
        "model":plan.judge_model,"toolsEnabled":false,"searchEnabled":false,"thinkingEnabled":false,
        "systemPrompt":JUDGE_SYSTEM_PROMPT,"messages":[{"role":"user","content":
            format!("用户问题：\n{query}\n\n候选回答：\n{content}\n\n只输出一个 0 到 1 之间的小数表示该回答的充分性。")}]});
    let validated = validate_deepseek_payload(&payload, &turn.validated.api_key, router)?;
    // Scoring cannot create another memory effect or execute a provider tool call.
    let memory = deepseek_policy::memory::prepare_memory_state_read_only(
        &payload,
        turn.assembly.root(),
        None,
    );
    let (prepared, consulted) = turn.assembly.with_env(|env| {
        build_deepseek_request(&payload, false, Some(&memory), Some(&validated), env)
    });
    if consulted {
        return Err(deepseek_policy::file_store::vector_index_not_ready());
    }
    let prepared = prepared?;
    config.api_key = prepared.api_key;
    let result = crate::chat_tool_loop::execute_chat_turn_with_limit(
        &config,
        &prepared.body,
        &ToolRoundExecutor::new(None, None),
        0,
    )
    .await
    .map_err(execution_app_error)?;
    Ok(model_router::parse_judge_score(&result.turn.content()))
}

async fn ollama_draft(payload: &Value, plan: &CascadePlan) -> Value {
    match call_ollama(payload, plan).await {
        Ok(answer) => answer,
        // The quality gate refines an unavailable local draft. No credentials,
        // upstream bodies or endpoint details enter the selected response.
        Err(()) => json!({"content":"","diagnostics":{}}),
    }
}

async fn call_ollama(payload: &Value, plan: &CascadePlan) -> Result<Value, ()> {
    let enabled = std::env::var("OLLAMA_ENABLED").unwrap_or_default();
    if !matches!(
        enabled.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    ) {
        return Err(());
    }
    let base = std::env::var("OLLAMA_BASE_URL")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .unwrap_or_else(|| "http://127.0.0.1:11434".into());
    let url = reqwest::Url::parse(&format!("{}/api/chat", base.trim().trim_end_matches('/')))
        .map_err(|_| ())?;
    if !matches!(url.scheme(), "http" | "https")
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err(());
    }
    let seconds = std::env::var("OLLAMA_TIMEOUT_SECONDS")
        .ok()
        .and_then(|s| s.trim().parse::<i64>().ok())
        .unwrap_or(120)
        .clamp(5, 1800);
    let messages: Vec<Value> = payload.get("messages").and_then(Value::as_array).into_iter().flatten()
        .filter(|m| m.is_object() && m.get("role").is_some_and(deepseek_policy::core_utils::python_truthy))
        .map(|m| json!({"role":deepseek_policy::python_json::value_str(&m["role"]),
            "content":m.get("content").and_then(Value::as_str).map(str::to_string)
                .unwrap_or_else(|| deepseek_policy::python_json::dumps_default_separators(&m["content"]))})).collect();
    let model = plan
        .draft_model
        .strip_prefix("ollama/")
        .unwrap_or(&plan.draft_model);
    let body =
        json!({"model":model,"messages":messages,"stream":false,"options":{"temperature":0.7}});
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(seconds as u64))
        .build()
        .map_err(|_| ())?;
    let response = client
        .post(url)
        .header(header::CONTENT_TYPE, "application/json")
        .body(serde_json::to_vec(&body).map_err(|_| ())?)
        .send()
        .await
        .map_err(|_| ())?;
    if !response.status().is_success() {
        return Err(());
    }
    let data: Value =
        serde_json::from_slice(&response.bytes().await.map_err(|_| ())?).map_err(|_| ())?;
    let message = data.get("message").and_then(Value::as_object).ok_or(())?;
    let content = deepseek_policy::core_utils::text_or_empty(message.get("content"));
    let prompt =
        deepseek_policy::core_utils::python_int_opt(data.get("prompt_eval_count")).unwrap_or(0);
    let completion =
        deepseek_policy::core_utils::python_int_opt(data.get("eval_count")).unwrap_or(0);
    Ok(
        json!({"model":plan.draft_model,"content":content,"diagnostics":{},
        "usage":{"prompt_tokens":prompt,"completion_tokens":completion,"total_tokens":prompt.saturating_add(completion)}}),
    )
}

// The cascade producer has no finishReason field. Struct serialization preserves
// the retained event field order without using the ordinary SSE accumulator.
#[derive(Serialize)]
struct Done {
    #[serde(rename = "type")]
    kind: &'static str,
    id: Value,
    model: Value,
    content: Value,
    reasoning: Value,
    usage: Value,
    search: Value,
    #[serde(rename = "memorySuggestions")]
    memory_suggestions: Value,
    diagnostics: Value,
}

pub(crate) fn stream_response(
    turn: SearchChatTurn,
    executor: ToolRoundExecutor,
    query: String,
    config: UpstreamConfig,
    prefetch: bool,
    router: ModelRouterSettings,
) -> Response {
    let body = async_stream::stream! {
        // This future is owned by the response body. Disconnecting drops it before
        // a later judge/refine stage can be dispatched.
        match complete(turn, &executor, query, config, prefetch, &router).await {
            Ok(answer) => {
                for key in ["reasoning", "content"] {
                    let text = answer.get(key).and_then(Value::as_str).unwrap_or_default();
                    if !text.is_empty() {
                        let event = if key == "reasoning" { ChatEvent::Reasoning { text:text.into() } }
                            else { ChatEvent::Content { text:text.into() } };
                        yield Ok::<Bytes, std::io::Error>(Bytes::from(encode_stream_event(&event)));
                    }
                }
                let done = Done { kind:"done", id:answer["id"].clone(), model:answer["model"].clone(),
                    content:answer.get("content").cloned().unwrap_or(json!("")),
                    reasoning:answer.get("reasoning").cloned().unwrap_or(json!("")),
                    usage:answer.get("usage").cloned().unwrap_or(json!({})), search:answer["search"].clone(),
                    memory_suggestions:answer.get("memorySuggestions").cloned().unwrap_or(json!([])),
                    diagnostics:answer.get("diagnostics").cloned().unwrap_or(json!({})) };
                let mut bytes = serde_json::to_vec(&done).expect("JSON value serialization");
                bytes.push(b'\n');
                yield Ok(Bytes::from(bytes));
            }
            Err(error) => yield Ok(Bytes::from(encode_stream_event(&ChatEvent::Error {
                error:error.message, code:error.code.into() }))),
        }
    };
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, STREAM_MEDIA_TYPE),
            (header::CACHE_CONTROL, "no-store"),
        ],
        Body::from_stream(body),
    )
        .into_response()
}
