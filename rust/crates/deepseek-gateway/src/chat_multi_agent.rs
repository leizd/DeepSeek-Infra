//! The interactive multi-agent NDJSON producer. Go admits each execution batch;
//! Rust owns provider I/O, tool policy, framing and response-owned cancellation.
//! Durable /api/agent-runs storage is a separate, fenced control-plane domain.

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use axum::body::{Body, Bytes};
use axum::http::header;
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::AppError;
use deepseek_policy::model_router::ModelRouterSettings;
use deepseek_policy::multi_agent as policy;
use deepseek_policy::request_messages::validate_deepseek_payload;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentChatOutcome, AgentChatPhase, AgentChatScheduleInput, AgentChatScheduleOutput,
    AgentChatTask, agent_chat_scheduler_client::AgentChatSchedulerClient,
};
use futures_util::{Stream, StreamExt, stream::FuturesUnordered};
use serde_json::{Value, json};
use tokio::sync::mpsc;
use tonic::transport::Channel;

use crate::chat_execution::UpstreamConfig;
use crate::chat_ndjson::{SearchChatTurn, stream_prepared_turn};
use crate::chat_tool_loop::ToolRoundExecutor;

const CONTROL_UNAVAILABLE: &str = "NATIVE_AGENT_CONTROL_UNAVAILABLE";
const EXECUTION_FAILED: &str = "NATIVE_AGENT_EXECUTION_FAILED";

fn planner_candidates(parsed: &Value) -> Vec<AgentChatTask> {
    let string = |value: Option<&Value>| {
        value
            .filter(|v| deepseek_policy::core_utils::python_truthy(v))
            .map(deepseek_policy::python_json::value_str)
            .unwrap_or_default()
    };
    let mut candidates = Vec::new();
    let mut seen = BTreeMap::new();
    for candidate in parsed
        .get("agents")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|v| v.is_object())
    {
        let role = string(candidate.get("id"));
        let normalized = policy::strip(&role);
        if policy::profile(normalized).is_none() || seen.contains_key(normalized) {
            continue;
        }
        seen.insert(normalized.to_owned(), ());
        candidates.push(AgentChatTask {
            role,
            task: string(candidate.get("task")),
            depends_on: candidate
                .get("depends_on")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(|value| string(Some(value)))
                .collect(),
            attempt: 0,
        });
        // Invalid and repeated entries do not consume the transport bound.
        // Go still validates the plan, dependencies and execution batches.
        if candidates.len() == 4 {
            break;
        }
    }
    candidates
}

fn control_error() -> AppError {
    AppError {
        message: "Multi-agent chat requires the authenticated authoritative Go scheduler.".into(),
        code: CONTROL_UNAVAILABLE,
        status: 503,
    }
}

struct Coordinator {
    client: AgentChatSchedulerClient<Channel>,
    authorization: tonic::metadata::MetadataValue<tonic::metadata::Ascii>,
}

impl Coordinator {
    async fn connect() -> Result<Self, AppError> {
        let origin = crate::backup_mirror_routes::GO_CONTROL_ADDR_ENV
            .iter()
            .find_map(|key| std::env::var(key).ok())
            .ok_or_else(control_error)?;
        let parsed = reqwest::Url::parse(origin.trim()).map_err(|_| control_error())?;
        let host = parsed.host_str().ok_or_else(control_error)?;
        let loopback = host == "localhost"
            || host
                .trim_matches(['[', ']'])
                .parse::<std::net::IpAddr>()
                .is_ok_and(|ip| ip.is_loopback());
        if !loopback
            || !matches!(parsed.scheme(), "http" | "https")
            || !parsed.username().is_empty()
            || parsed.password().is_some()
            || parsed.query().is_some()
            || parsed.fragment().is_some()
            || parsed.path() != "/"
        {
            return Err(control_error());
        }
        let bearer =
            std::env::var(crate::backup_mirror_routes::INTERNAL_BEARER_ENV).unwrap_or_default();
        if bearer.trim().len() < 32 {
            return Err(control_error());
        }
        let authorization = format!("Bearer {}", bearer.trim())
            .parse()
            .map_err(|_| control_error())?;
        let channel = tonic::transport::Endpoint::from_shared(origin.trim().to_string())
            .map_err(|_| control_error())?
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(10))
            .connect()
            .await
            .map_err(|_| control_error())?;
        Ok(Self {
            client: AgentChatSchedulerClient::new(channel).max_decoding_message_size(1 << 20),
            authorization,
        })
    }

    async fn evaluate(
        &mut self,
        input: &AgentChatScheduleInput,
    ) -> Result<AgentChatScheduleOutput, AppError> {
        let mut request = tonic::Request::new(input.clone());
        request
            .metadata_mut()
            .insert("authorization", self.authorization.clone());
        let result = self
            .client
            .evaluate(request)
            .await
            .map_err(|_| control_error())?
            .into_inner();
        if !result.authoritative
            || result.plan.is_empty()
            || result.plan.len() > 4
            || result.execute.len() > 4
            || result
                .plan
                .iter()
                .any(|task| policy::profile(&task.role).is_none())
            || result
                .execute
                .iter()
                .any(|task| !result.plan.iter().any(|planned| planned.role == task.role))
        {
            return Err(control_error());
        }
        Ok(result)
    }
}

fn role_models() -> BTreeMap<String, String> {
    ["planner", "researcher", "coder", "reasoner", "critic"]
        .into_iter()
        .map(|role| {
            let key = format!("AGENT_MODEL_{}", role.to_ascii_uppercase());
            let value = std::env::var(key)
                .unwrap_or_default()
                .trim()
                .to_ascii_lowercase();
            let model = match value.as_str() {
                "flash" | "deepseek-v4-flash" => "deepseek-v4-flash",
                _ => "deepseek-v4-pro",
            };
            (role.into(), model.into())
        })
        .collect()
}

fn stage_turn(
    original: &SearchChatTurn,
    payload: Value,
    router: &ModelRouterSettings,
) -> Result<SearchChatTurn, AppError> {
    let mut stage = original.clone();
    stage.payload = payload;
    stage.payload["stream"] = json!(true);
    stage.validated =
        validate_deepseek_payload(&stage.payload, &original.validated.api_key, router)?;
    stage.streaming = true;
    Ok(stage)
}

fn encode(event: Value) -> Result<Bytes, std::io::Error> {
    let mut bytes = serde_json::to_vec(&event).map_err(std::io::Error::other)?;
    bytes.push(b'\n');
    Ok(Bytes::from(bytes))
}

fn agent_event(role: &str, status: &str, text: String, duration: Option<u64>) -> Value {
    let name = policy::profile(role).map(|p| p.0).unwrap_or("Leader");
    let mut event = json!({"type":"agent","phase":role,"status":status,"name":name,"text":text});
    if let Some(duration) = duration {
        event["durationMs"] = json!(duration);
    }
    event
}

fn elapsed(start: Instant) -> u64 {
    start.elapsed().as_millis().min(u64::MAX as u128) as u64
}

fn local_events(response: Response) -> impl Stream<Item = Result<Value, ()>> {
    async_stream::stream! {
        let mut stream = response.into_body().into_data_stream();
        let mut buffer = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = match chunk { Ok(chunk) => chunk, Err(_) => { yield Err(()); return; } };
            buffer.extend_from_slice(&chunk);
            while let Some(end) = buffer.iter().position(|byte| *byte == b'\n') {
                let line = buffer.drain(..=end).collect::<Vec<_>>();
                if line.iter().all(u8::is_ascii_whitespace) { continue; }
                yield serde_json::from_slice::<Value>(&line).map_err(|_| ());
            }
        }
        if !buffer.is_empty() { yield serde_json::from_slice::<Value>(&buffer).map_err(|_| ()); }
    }
}

#[derive(Default)]
struct Capture {
    content: String,
    usage: Value,
    search: Value,
    finish: String,
    error: Option<String>,
    risk: bool,
    tool_note: bool,
}

impl Capture {
    fn observe(&mut self, event: &Value) {
        match event
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
        {
            "content" => self.content.push_str(
                event
                    .get("text")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            ),
            "system_note" => self.tool_note = true,
            "search" => self.search = event["search"].clone(),
            "done" => {
                self.usage = event["usage"].clone();
                self.finish = event["finishReason"].as_str().unwrap_or_default().into();
                if event.get("search").is_some_and(Value::is_object) {
                    self.search = event["search"].clone();
                }
            }
            "error" => {
                self.error = Some(
                    event["error"]
                        .as_str()
                        .unwrap_or("Agent upstream request failed")
                        .into(),
                );
                self.risk = event["code"]
                    .as_str()
                    .is_some_and(|code| code.eq_ignore_ascii_case("upstream_content_risk"));
            }
            _ => {}
        }
    }
}

struct WorkerResult {
    output: Value,
    outcome: AgentChatOutcome,
    duration: u64,
}

async fn send(sender: &mpsc::Sender<Value>, event: Value) -> Result<(), ()> {
    sender.send(event).await.map_err(|_| ())
}

struct WorkerJob {
    original: SearchChatTurn,
    executor: ToolRoundExecutor,
    config: UpstreamConfig,
    router: ModelRouterSettings,
    task: AgentChatTask,
    model: String,
    prior: Vec<Value>,
    revision: bool,
    timeout_seconds: u64,
}

async fn worker(job: WorkerJob, sender: mpsc::Sender<Value>) -> WorkerResult {
    let WorkerJob {
        original,
        executor,
        config,
        router,
        task,
        model,
        prior,
        revision,
        timeout_seconds,
    } = job;
    let start = Instant::now();
    let name = policy::profile(&task.role).map(|p| p.0).unwrap_or("Agent");
    let mut capture = Capture::default();
    let operation = async {
        let payload =
            policy::worker_payload(&original.payload, &task.role, &task.task, &prior, &model)
                .ok_or(())?;
        let turn = stage_turn(&original, payload, &router).map_err(|_| ())?;
        let response = stream_prepared_turn(turn, executor.for_agent(&task.role), config, 4)
            .await
            .map_err(|error| {
                capture.error = Some(error.message);
            })?;
        let events = local_events(response);
        tokio::pin!(events);
        while let Some(event) = events.next().await {
            let event = event?;
            capture.observe(&event);
            let kind = event["type"].as_str().unwrap_or_default();
            let relay = match kind {
                "content" => Some(
                    json!({"type":"agent_delta","phase":task.role,"name":name,"text":event["text"]}),
                ),
                "reasoning" => Some(
                    json!({"type":"agent_reasoning","phase":task.role,"name":name,"text":event["text"]}),
                ),
                "system_note" | "error" => Some(
                    json!({"type":"agent_note","phase":task.role,"name":name,"text":if kind=="error" {event["error"].clone()} else {event["text"].clone()}}),
                ),
                "search" => Some(
                    json!({"type":"agent_search","phase":task.role,"name":name,"search":event["search"]}),
                ),
                "done" if event.get("search").is_some_and(Value::is_object) => Some(
                    json!({"type":"agent_search","phase":task.role,"name":name,"search":event["search"]}),
                ),
                _ => None,
            };
            if let Some(event) = relay {
                send(&sender, event).await?;
            }
        }
        Ok::<(), ()>(())
    };
    let result = tokio::time::timeout(
        Duration::from_secs(timeout_seconds.min(31_536_000)),
        operation,
    )
    .await;
    let deadline = result.is_err();
    if deadline {
        capture.error = Some("Agent exceeded its execution timeout.".into());
    } else if !matches!(result, Ok(Ok(()))) && capture.error.is_none() {
        capture.error = Some("Agent stream interrupted.".into());
    }
    let salvage = !deadline
        && !capture.risk
        && capture.error.is_some()
        && policy::strip(&capture.content).chars().count() >= 200;
    let failed = capture.error.is_some() && !salvage;
    let mut output = policy::worker_output(
        &task.role,
        &task.task,
        &capture.content,
        capture.usage.clone(),
        &capture.search,
    );
    if failed {
        output = policy::failed_output(
            &task.role,
            &task.task,
            capture.error.as_deref().unwrap_or("Agent failed"),
        );
    } else if salvage || capture.finish == "length" {
        let note = if salvage {
            "注意：该 Agent 的流式输出中途中断，以上为中断前的部分产出，结论可能不完整。"
        } else {
            "注意：该 Agent 输出达到上游长度上限被截断，结论可能不完整。"
        };
        let old = output["risks"].as_str().unwrap_or_default();
        output["risks"] = json!(format!("{old}\n{note}").trim());
        output["degraded"] = json!(true);
        let _ = send(
            &sender,
            json!({"type":"agent_note","phase":task.role,"name":name,"text":note}),
        )
        .await;
    }
    output["duration_ms"] = json!(elapsed(start));
    let outcome = AgentChatOutcome {
        role: task.role,
        tokens: policy::usage_tokens(&capture.usage),
        failed,
        retryable: failed && !capture.risk && !deadline && !capture.tool_note,
        revision,
    };
    WorkerResult {
        output,
        outcome,
        duration: elapsed(start),
    }
}

pub(crate) async fn response(
    turn: SearchChatTurn,
    executor: ToolRoundExecutor,
    config: UpstreamConfig,
    query: String,
    router: ModelRouterSettings,
) -> Response {
    let mut coordinator = match Coordinator::connect().await {
        Ok(client) => client,
        Err(error) => {
            return (
                axum::http::StatusCode::SERVICE_UNAVAILABLE,
                axum::Json(json!({"error":error.message,"code":error.code})),
            )
                .into_response();
        }
    };
    let mut input = AgentChatScheduleInput::default();
    if coordinator.evaluate(&input).await.is_err() {
        let error = control_error();
        return (
            axum::http::StatusCode::SERVICE_UNAVAILABLE,
            axum::Json(json!({"error":error.message,"code":error.code})),
        )
            .into_response();
    }
    let models = role_models();
    let selected_model = turn.validated.model.clone();
    let budget_settings = turn.assembly.with_env(|env| env.budget.clone()).0;
    let stream = async_stream::stream! {
        let started = Instant::now();
        yield encode(agent_event("leader", "running", "正在拆解问题并分配 Agent...".into(), None));
        let planner = policy::planner_payload(&turn.payload, &models["planner"]);
        let mut planning = Capture::default();
        if let Ok(stage) = stage_turn(&turn, planner, &router) {
            if let Ok(response) = stream_prepared_turn(stage, executor.for_agent("planner"), config.clone(), 0).await {
                let events = local_events(response);
                tokio::pin!(events);
                while let Some(event) = events.next().await {
                    match event {
                        Ok(event) => { planning.observe(&event); if event["type"] == "reasoning" { yield encode(event); } },
                        Err(_) => break,
                    }
                }
            }
        }
        let parsed = policy::parse_plan_response(&planning.content);
        input.candidates = planner_candidates(&parsed);
        let mut outputs = BTreeMap::<String, Value>::new();
        let mut announced = BTreeMap::<String, ()>::new();
        let mut durations = BTreeMap::<String, u64>::new();
        let mut leader_done = false;
        let final_schedule = loop {
            input.revision_target = outputs.get("critic").and_then(policy::critic_target).unwrap_or_default().into();
            let schedule = match coordinator.evaluate(&input).await {
                Ok(value) => value,
                Err(_) => { yield encode(json!({"type":"error","error":control_error().message,"code":CONTROL_UNAVAILABLE})); return; },
            };
            if !leader_done {
                input.candidates = schedule.plan.clone();
                let items = schedule.plan.iter().map(|task| format!("- {}：{}", policy::profile(&task.role).map(|p|p.0).unwrap_or("Agent"), task.task)).collect::<Vec<_>>().join("\n");
                yield encode(agent_event("leader", "done", format!("已完成任务拆解：\n{items}"), Some(elapsed(started))));
                leader_done = true;
            }
            for role in &schedule.completed_roles {
                if !announced.contains_key(role) {
                    if let Some(output) = outputs.get(role) {
                        let failed = output.get("failed") == Some(&Value::Bool(true));
                        yield encode(agent_event(role, if failed { "error" } else { "done" }, if failed { "Agent 执行失败，综合将明确说明。".into() } else { "已完成分析".into() }, durations.get(role).copied()));
                        yield encode(json!({"type":"agent_output","phase":role,"output":output}));
                        announced.insert(role.clone(), ());
                    }
                }
            }
            if schedule.execute.is_empty() {
                if schedule.budget_exhausted { yield encode(json!({"type":"agent_note","phase":"leader","name":"Leader","text":"已达 token 预算上限，停止启动新的 Agent，直接进入综合。"})); }
                break schedule;
            }
            let revision = schedule.phase == AgentChatPhase::Revision as i32;
            let (sender, mut receiver) = mpsc::channel(8);
            let mut jobs = FuturesUnordered::new();
            for task in &schedule.execute {
                let mut task = task.clone();
                let mut prior: Vec<Value> = schedule.plan.iter().filter_map(|t| outputs.get(&t.role).cloned()).collect();
                if revision {
                    yield encode(json!({"type":"agent_reset","phase":task.role,"reason":"critic_revision"}));
                    let critique = outputs.get("critic").map(|o| ["summary","risks","evidence"].into_iter().filter_map(|k|o[k].as_str()).collect::<Vec<_>>().join("\n\n")).unwrap_or_default();
                    task.task = format!("{}\n\n反驳审查 Agent 复核后认为你上一轮的结论需要修订，反馈如下；请针对性地重新分析并修正，给出改进后的公开摘要：\n{critique}", task.task);
                    prior.retain(|o| o["id"] != task.role && o["id"] != "critic");
                } else if task.attempt > 1 {
                    yield encode(json!({"type":"agent_reset","phase":task.role,"reason":"stream_retry"}));
                }
                yield encode(agent_event(&task.role, "running", task.task.clone(), None));
                jobs.push(worker(WorkerJob { original:turn.clone(), executor:executor.clone(), config:config.clone(), router:router.clone(), task:task.clone(), model:models.get(&task.role).cloned().unwrap_or_else(|| "deepseek-v4-pro".into()), prior, revision, timeout_seconds:schedule.timeout_seconds }, sender.clone()));
            }
            drop(sender);
            while !jobs.is_empty() {
                tokio::select! {
                    Some(event) = receiver.recv() => { yield encode(event); },
                    Some(result) = jobs.next() => {
                        let role = result.outcome.role.clone();
                        input.outcomes.push(result.outcome);
                        if !revision || result.output.get("failed") != Some(&Value::Bool(true)) {
                            let mut output = result.output;
                            if revision {
                                if let Some(original) = schedule.plan.iter().find(|t|t.role == role) { output["task"] = json!(original.task); }
                                announced.remove(&role);
                            }
                            outputs.insert(role.clone(), output);
                            durations.insert(role, result.duration);
                        } else {
                            yield encode(agent_event(&role, "error", "修订重跑失败，保留原结论。".into(), Some(result.duration)));
                        }
                    }
                }
            }
            while let Ok(event) = receiver.try_recv() { yield encode(event); }
        };
        let ordered: Vec<Value> = final_schedule.plan.iter().filter_map(|task|outputs.get(&task.role).cloned()).collect();
        yield encode(agent_event("leader", "running", "正在综合多个 Agent 的结论...".into(), None));
        let synthesis_start = Instant::now();
        let payload = policy::synthesis_payload(&turn.payload, &selected_model, &query, &ordered);
        let stage = match stage_turn(&turn, payload, &router) { Ok(stage) => stage, Err(_) => { yield encode(json!({"type":"error","error":"Agent synthesis could not be prepared","code":EXECUTION_FAILED})); return; } };
        let response = match stream_prepared_turn(stage, executor.for_agent("synthesizer"), config, 0).await { Ok(response) => response, Err(error) => { yield encode(json!({"type":"error","error":error.message,"code":error.code})); return; } };
        let events = local_events(response);
        tokio::pin!(events);
        let mut synthesis = Capture::default();
        while let Some(event) = events.next().await {
            let event = match event { Ok(event) => event, Err(_) => { yield encode(json!({"type":"error","error":"Agent synthesis stream interrupted","code":EXECUTION_FAILED})); return; } };
            synthesis.observe(&event);
            match event["type"].as_str().unwrap_or_default() {
                "content" | "reasoning" | "system_note" | "memory_suggestion" | "search" => { yield encode(event); },
                "error" => { yield encode(event); return; },
                _ => {},
            }
        }
        if synthesis.content.is_empty() { yield encode(json!({"type":"content","text":policy::EMPTY_SYNTHESIS_FALLBACK})); }
        yield encode(agent_event("leader", "done", "已完成综合。".into(), Some(elapsed(synthesis_start))));
        let mut token_by_agent = serde_json::Map::<String, Value>::new();
        for outcome in &input.outcomes {
            let used = token_by_agent.get(&outcome.role).and_then(Value::as_u64).unwrap_or(0);
            token_by_agent.insert(outcome.role.clone(), json!(used.saturating_add(outcome.tokens)));
        }
        let synthesis_tokens = policy::usage_tokens(&synthesis.usage);
        token_by_agent.insert("synthesizer".into(), json!(synthesis_tokens));
        let cost = ordered.iter().map(|output| {
            let model = output["id"].as_str().and_then(|role|models.get(role)).map(String::as_str).unwrap_or("deepseek-v4-pro");
            deepseek_policy::budget_manager::cost_from_usage(&output["usage"], Some(model), &budget_settings)
        }).sum::<f64>() + deepseek_policy::budget_manager::cost_from_usage(&synthesis.usage, Some(&selected_model), &budget_settings);
        let search_used = executor.search_provider().map(|provider|provider.for_agent().budget_used()).unwrap_or(0);
        yield encode(json!({"type":"done","model":selected_model,"usage":{},"diagnostics":{
            "agentMode":true,"agentCount":ordered.len(),"agents":ordered.iter().map(|output|output["id"].clone()).collect::<Vec<_>>(),
            "agentOutputs":ordered,"agentDurations":durations,"agentCache":policy::agent_cache(&ordered,&synthesis.usage),
            "agentSearchBudgetUsed":search_used,"agentSearchBudgetLimit":36,
            "agentTokenBudgetUsed":final_schedule.used_tokens.saturating_add(synthesis_tokens),"agentTokenBudgetLimit":final_schedule.token_limit,
            "agentTokenByAgent":token_by_agent,"agentCostUsd":deepseek_policy::core_utils::round_six(cost)
        }}));
    };
    (
        [
            (
                header::CONTENT_TYPE,
                deepseek_policy::chat_stream_events::STREAM_MEDIA_TYPE,
            ),
            (header::CACHE_CONTROL, "no-cache"),
            (header::HeaderName::from_static("x-accel-buffering"), "no"),
        ],
        Body::from_stream(stream),
    )
        .into_response()
}
