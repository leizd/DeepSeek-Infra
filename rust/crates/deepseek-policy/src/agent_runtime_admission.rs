//! Admission for retained Agent runtime updates, resume, rerun, and tool continuation.
//!
//! Creation stays at runtime revision 1 on its own path. This decision does not
//! allocate a provider effect. `EFFECT_UNKNOWN` refuses every operation, including
//! a same-effect tool continuation. A worker must not treat a larger epoch as
//! authority: callers pass `mismatch` for that case.

use std::sync::OnceLock;

use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeNode {
    pub id: String,
    pub state: String,
    pub failed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeAdmission<'a> {
    pub operation: &'a str,
    pub status: &'a str,
    pub identity_valid: bool,
    pub lease: &'a str,
    pub epoch: &'a str,
    pub effect: &'a str,
    pub runtime_revision: u64,
    pub requested_revision: u64,
    pub agent_id: &'a str,
    pub resynthesize: bool,
    pub plan: &'a [String],
    pub nodes: &'a [RuntimeNode],
    pub has_final_answer: bool,
    pub same_effect: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeDecision {
    pub admitted: bool,
    pub http_status: u16,
    pub code: &'static str,
    pub message: &'static str,
    pub dispatch: &'static str,
    pub started: bool,
    pub bumps_revision: bool,
    pub requested_revision: u64,
    pub events: Vec<Value>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Contract {
    operations: Vec<String>,
    resumable_statuses: Vec<String>,
    active_statuses: Vec<String>,
    known_agents: Vec<String>,
    synthesizer_aliases: Vec<String>,
}

fn contract() -> &'static Contract {
    static CONTRACT: OnceLock<Contract> = OnceLock::new();
    CONTRACT.get_or_init(|| {
        serde_json::from_str(include_str!("agent_runtime_admission_v1.json"))
            .expect("agent runtime admission contract")
    })
}

fn refuse(status: u16, code: &'static str, message: &'static str) -> RuntimeDecision {
    RuntimeDecision {
        admitted: false,
        http_status: status,
        code,
        message,
        dispatch: "none",
        started: false,
        bumps_revision: false,
        requested_revision: 0,
        events: Vec::new(),
    }
}

fn allow(
    dispatch: &'static str,
    started: bool,
    requested_revision: u64,
    events: Vec<Value>,
) -> RuntimeDecision {
    RuntimeDecision {
        admitted: true,
        http_status: 200,
        code: "",
        message: "",
        dispatch,
        started,
        bumps_revision: true,
        requested_revision,
        events,
    }
}

/// The first sealed object may land on the metadata revision. A later object
/// may replay that revision or advance it by exactly one. A gap is refused.
pub fn vault_revision_allowed(sealed_max: Option<u64>, requested: u64) -> bool {
    if requested == 0 {
        return false;
    }
    match sealed_max {
        None => true,
        Some(current) => requested == current || current < u64::MAX && requested == current + 1,
    }
}

/// Event-log revision. Missing history is creation revision 1. A gap or a
/// non-increment is corrupt and must not be treated as the next legal revision.
#[allow(clippy::result_unit_err)]
pub fn current_runtime_revision(events: &[Value]) -> Result<u64, ()> {
    let mut current = 1u64;
    for event in events {
        if event.get("type").and_then(Value::as_str) != Some("runtime_payload") {
            continue;
        }
        let revision = event
            .get("runtimeRevision")
            .and_then(Value::as_u64)
            .ok_or(())?;
        if revision != current + 1 {
            return Err(());
        }
        current = revision;
    }
    Ok(current)
}

fn contains(values: &[String], candidate: &str) -> bool {
    values.iter().any(|value| value == candidate)
}

fn incomplete(plan: &[String], nodes: &[RuntimeNode]) -> Vec<String> {
    plan.iter()
        .filter(|id| {
            nodes
                .iter()
                .find(|node| node.id == **id)
                .is_none_or(|node| node.failed || node.state != "succeeded")
        })
        .cloned()
        .collect()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RetainedEffect {
    pub effect: &'static str,
    pub same_effect: bool,
}

fn remember(nodes: &mut Vec<(String, &'static str)>, node: &str, kind: &'static str) {
    if let Some(found) = nodes.iter_mut().find(|(id, _)| id == node) {
        if found.1 == "sticky" && kind != "done" {
            return;
        }
        found.1 = kind;
        return;
    }
    nodes.push((node.to_string(), kind));
}

fn effect_node_id(event: &Value) -> String {
    if let Some(phase) = event.get("phase").and_then(Value::as_str) {
        if !phase.is_empty() {
            return phase.to_string();
        }
    }
    if let Some(node) = event.get("nodeId").and_then(Value::as_str) {
        if !node.is_empty() {
            return node.to_string();
        }
    }
    if let Some(identity) = event
        .get("output")
        .and_then(|value| value.get("id"))
        .and_then(Value::as_str)
    {
        if !identity.is_empty() {
            return identity.to_string();
        }
    }
    String::new()
}

fn apply_effect_state(nodes: &mut Vec<(String, &'static str)>, node: &str, state: &Value) {
    let Some(text) = state.as_str() else {
        remember(nodes, node, "sticky");
        return;
    };
    match text {
        "AWAITING_TOOLS" | "awaiting_tools" => remember(nodes, node, "wait"),
        "CONFIRMED" | "SETTLED_FAILURE" => remember(nodes, node, "done"),
        "NOT_APPLIED" | "not_applied" | "EFFECT_UNKNOWN" | "effect_unknown" | "DISPATCHING"
        | "dispatching" | "" => {
            remember(nodes, node, "sticky");
        }
        "NONE" | "none" => {
            let current = nodes
                .iter()
                .find(|(id, _)| id == node)
                .map(|(_, kind)| *kind);
            if !matches!(current, Some("sticky" | "open" | "wait")) {
                remember(nodes, node, "done");
            }
        }
        _ => remember(nodes, node, "sticky"),
    }
}

fn tool_waiting(event: &Value) -> &'static str {
    if let Some(continuation) = event.get("toolContinuation").and_then(Value::as_object) {
        if continuation.contains_key("toolCalls") {
            let Some(calls) = continuation.get("toolCalls").and_then(Value::as_array) else {
                return "sticky";
            };
            if calls
                .iter()
                .any(|item| item.as_object().is_some_and(|fields| !fields.is_empty()))
            {
                return "wait";
            }
        }
    }
    match event.get("toolCount") {
        Some(Value::Number(value)) => match value.as_u64() {
            Some(count) if count > 0 => "wait",
            _ => "sticky",
        },
        _ => "sticky",
    }
}

/// Classifies the retained public stream before resume, rerun, or a payload update.
///
/// A complete stream with no outstanding effect is `none`. Unknown, unconfirmed,
/// missing, timed-out, and absent records are `effect_unknown`, never a fresh
/// dispatch. An open tool request is `awaiting_tools` and keeps `same_effect`.
pub fn retained_effect(events: &[Value]) -> RetainedEffect {
    let mut nodes = Vec::<(String, &'static str)>::new();
    for event in events {
        let Some(object) = event.as_object() else {
            remember(&mut nodes, "", "sticky");
            continue;
        };
        let kind = object.get("type").and_then(Value::as_str).unwrap_or("");
        let node = effect_node_id(event);
        match kind {
            "node_effect" | "agent_node_effect" => {
                if object.contains_key("effect") {
                    apply_effect_state(&mut nodes, &node, &object["effect"]);
                } else if object.contains_key("state") {
                    apply_effect_state(&mut nodes, &node, &object["state"]);
                }
                if object.contains_key("observation") {
                    remember(&mut nodes, &node, "sticky");
                }
            }
            "agent_tool_request" => {
                if request_published_answer(event).is_some() {
                    remember(&mut nodes, &node, "done");
                } else {
                    remember(&mut nodes, &node, tool_waiting(event));
                }
            }
            "agent_output" => remember(&mut nodes, &node, "done"),
            "agent" => match object.get("status").and_then(Value::as_str) {
                Some("done" | "error") => remember(&mut nodes, &node, "done"),
                Some("running") => remember(&mut nodes, &node, "open"),
                _ => {}
            },
            "content" => {
                let settled = object
                    .get("text")
                    .and_then(Value::as_str)
                    .is_some_and(|text| !text.trim().is_empty());
                if settled {
                    remember(&mut nodes, "synthesizer", "done");
                }
            }
            _ => {}
        }
    }
    let mut effect = "none";
    for (_, kind) in &nodes {
        if *kind == "sticky" || *kind == "open" {
            effect = "effect_unknown";
            break;
        }
        if *kind == "wait" {
            effect = "awaiting_tools";
        }
    }
    RetainedEffect {
        effect,
        same_effect: effect == "awaiting_tools",
    }
}

const JOURNAL_STATE_DOCUMENT: &str =
    "{\"type\":\"node_effect\",\"phase\":\"%s\",\"status\":\"%s\",\"state\":\"%s\"}";
const JOURNAL_OBSERVATION_DOCUMENT: &str =
    "{\"type\":\"node_effect\",\"phase\":\"%s\",\"scope\":\"%s\",\"observation\":\"%s\"}";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JournalObservation {
    pub kind: &'static str,
    pub append: bool,
    pub phase: String,
    pub status: String,
    pub scope: String,
    pub event: Value,
    pub sha256: String,
    pub length: u64,
}

fn safe_journal_token(value: &str) -> bool {
    value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b':' | b'-'))
}

fn journal_observation_scope(scope: &str) -> bool {
    matches!(
        scope,
        "missing" | "timeout" | "absent_log" | "unavailable" | "not_applied"
    )
}

fn apply_journal_template(template: &str, values: &[&str]) -> String {
    let mut text = template.to_string();
    for value in values {
        if let Some(found) = text.find("%s") {
            text.replace_range(found..found + 2, value);
        }
    }
    text
}

/// Canonical `node_effect` bytes shared with the Go reference and the Python oracle.
pub fn node_effect_bytes(phase: &str, status: &str, scope: &str) -> Option<Vec<u8>> {
    if !safe_journal_token(phase) || !safe_journal_token(status) || !safe_journal_token(scope) {
        return None;
    }
    let text = if journal_observation_scope(scope) {
        apply_journal_template(JOURNAL_OBSERVATION_DOCUMENT, &[phase, scope, scope])
    } else {
        apply_journal_template(JOURNAL_STATE_DOCUMENT, &[phase, status, status])
    };
    Some(text.into_bytes())
}

/// Parses the canonical document and refuses a serializer that would change its bytes.
/// `ArtifactStore::put` hashes `serde_json::to_vec`, so a drift here must not be stored.
#[allow(clippy::result_unit_err)]
pub fn node_effect_value(phase: &str, status: &str, scope: &str) -> Result<Value, ()> {
    let bytes = node_effect_bytes(phase, status, scope).ok_or(())?;
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| ())?;
    let encoded = serde_json::to_vec(&value).map_err(|_| ())?;
    if encoded != bytes {
        return Err(());
    }
    Ok(value)
}

#[allow(clippy::result_unit_err)]
pub fn node_effect_digest(phase: &str, status: &str, scope: &str) -> Result<(String, u64), ()> {
    let bytes = node_effect_bytes(phase, status, scope).ok_or(())?;
    Ok((hex_encode(&Sha256::digest(&bytes)), bytes.len() as u64))
}

fn journal_document(
    phase: &str,
    status: &str,
    scope: &str,
    append: bool,
    kind: &'static str,
) -> JournalObservation {
    let Some(raw) = node_effect_bytes(phase, status, scope) else {
        if !(phase.is_empty() && status.is_empty() && scope == "unavailable") {
            return journal_document("", "", "unavailable", true, "unreadable");
        }
        return JournalObservation {
            kind: "unreadable",
            append: true,
            phase: String::new(),
            status: String::new(),
            scope: String::new(),
            event: Value::Null,
            sha256: String::new(),
            length: 0,
        };
    };
    let mut event = serde_json::Map::new();
    event.insert("type".to_string(), json!("node_effect"));
    event.insert("phase".to_string(), json!(phase));
    let (public_status, public_scope) = if journal_observation_scope(scope) {
        event.insert("scope".to_string(), json!(scope));
        event.insert("observation".to_string(), json!(scope));
        (String::new(), scope.to_string())
    } else {
        event.insert("status".to_string(), json!(status));
        event.insert("state".to_string(), json!(status));
        (status.to_string(), String::new())
    };
    JournalObservation {
        kind,
        append,
        phase: phase.to_string(),
        status: public_status,
        scope: public_scope,
        event: Value::Object(event),
        sha256: hex_encode(&Sha256::digest(&raw)),
        length: raw.len() as u64,
    }
}

fn canonical_node_id(node_id: &str) -> bool {
    !node_id.is_empty() && safe_journal_token(node_id)
}

fn journal_query_observation(query_error: &str) -> &'static str {
    let lowered = query_error.to_ascii_lowercase();
    if query_error == "timeout"
        || lowered.contains("deadline exceeded")
        || lowered.contains("deadlineexceeded")
        || lowered.contains("context canceled")
        || lowered.contains("context cancelled")
        || lowered.contains("timeout")
    {
        return "timeout";
    }
    if query_error.contains("ABSENT_LOG") || lowered.contains("absent_log") {
        return "absent_log";
    }
    "unavailable"
}

fn journal_settled_state(state: &str) -> Option<&'static str> {
    match state {
        "CONFIRMED" | "confirmed" => Some("CONFIRMED"),
        "SETTLED_FAILURE" | "settled_failure" => Some("SETTLED_FAILURE"),
        "NONE" | "none" => Some("NONE"),
        _ => None,
    }
}

/// Projects one journal read onto the public stream `retained_effect` classifies.
///
/// Unknown and waiting reads are appended. A settled read is not. A missing row,
/// timeout, or absent log is an observation and is never state `NOT_APPLIED`.
pub fn project_journal_observation(
    node_id: &str,
    found: bool,
    state: Option<&str>,
    query_error: Option<&str>,
) -> JournalObservation {
    if !canonical_node_id(node_id) {
        return journal_document("", "", "unavailable", true, "unreadable");
    }
    if let Some(error) = query_error {
        let observation = journal_query_observation(error);
        return journal_document(node_id, "", observation, true, "unreadable");
    }
    if !found {
        return journal_document(node_id, "", "missing", true, "unreadable");
    }
    let Some(state) = state else {
        return journal_document(node_id, "", "unavailable", true, "unreadable");
    };
    if matches!(state, "NOT_APPLIED" | "not_applied") {
        return journal_document(node_id, "", "not_applied", true, "unreadable");
    }
    if matches!(state, "AWAITING_TOOLS" | "awaiting_tools") {
        return journal_document(node_id, "AWAITING_TOOLS", "", true, "awaiting");
    }
    if let Some(settled) = journal_settled_state(state) {
        return journal_document(node_id, settled, "", false, "settled");
    }
    let sticky = match state {
        "EFFECT_UNKNOWN" | "effect_unknown" => Some("EFFECT_UNKNOWN"),
        "DISPATCHING" | "dispatching" => Some("DISPATCHING"),
        "" => Some(""),
        _ => None,
    };
    if let Some(sticky) = sticky {
        return journal_document(node_id, sticky, "", true, "unknown");
    }
    if safe_journal_token(state) {
        return journal_document(node_id, state, "", true, "unknown");
    }
    journal_document(node_id, "", "unavailable", true, "unreadable")
}

fn status_event(status: &str, reason: &str) -> Value {
    json!({"type":"run_status","status":status,"reason":reason})
}

fn reset_event(phase: &str, reason: &str) -> Value {
    json!({"type":"agent_reset","phase":phase,"reason":reason})
}

fn final_reset(reason: &str) -> Value {
    json!({"type":"final_reset","scope":"final_answer","reason":reason})
}

pub fn admit(input: &RuntimeAdmission<'_>) -> RuntimeDecision {
    let contract = contract();
    if !contains(&contract.operations, input.operation) {
        return refuse(400, "AGENT_RUN_REQUEST_INVALID", "任务请求无效");
    }
    if !input.identity_valid {
        return refuse(403, "INVALID_IDENTITY", "Agent runtime identity is invalid");
    }
    if input.lease != "live" {
        return refuse(409, "LEASE_NOT_HELD", "Agent runtime lease is not live");
    }
    if input.epoch == "stale" {
        return refuse(409, "STALE_EXECUTION_EPOCH", "Agent runtime epoch is stale");
    }
    if input.epoch != "match" {
        return refuse(409, "FENCE_MISMATCH", "Agent runtime fence does not match");
    }
    if input.effect == "effect_unknown" {
        return refuse(
            409,
            "EFFECT_UNKNOWN",
            "Agent runtime effect is unknown and was not retried",
        );
    }
    let continuation = input.operation == "tool_continuation";
    let revision_ok = if continuation {
        input.requested_revision == input.runtime_revision && input.runtime_revision >= 1
    } else {
        input.runtime_revision >= 1 && input.requested_revision == input.runtime_revision + 1
    };
    if !revision_ok {
        return refuse(
            409,
            "AGENT_RUNTIME_REVISION_DENIED",
            "Agent runtime revision is not the next admitted revision",
        );
    }
    if continuation {
        if input.effect != "awaiting_tools" || !input.same_effect || input.status != "running" {
            return refuse(
                409,
                "AGENT_TOOL_CONTINUATION_REQUIRED",
                "Tool continuation requires the original awaiting effect",
            );
        }
        let agent = input.agent_id;
        if !contains(&contract.known_agents, agent) || !input.plan.iter().any(|id| id == agent) {
            return refuse(400, "AGENT_RUN_REQUEST_INVALID", "Unknown Agent");
        }
        return RuntimeDecision {
            admitted: true,
            http_status: 200,
            code: "",
            message: "",
            dispatch: "same-effect",
            started: false,
            bumps_revision: false,
            requested_revision: input.runtime_revision,
            events: Vec::new(),
        };
    }
    if input.effect == "awaiting_tools" {
        return refuse(
            409,
            "AGENT_TOOL_CONTINUATION_REQUIRED",
            "Confirm tool continuation before changing this run",
        );
    }
    if contains(&contract.active_statuses, input.status) {
        return refuse(
            409,
            "NATIVE_AGENT_ACTION_CONFLICT",
            "Agent run is still running",
        );
    }
    if input.status == "awaiting_plan" {
        return refuse(
            409,
            "NATIVE_AGENT_ACTION_CONFLICT",
            "Confirm the plan before resuming",
        );
    }
    if !contains(&contract.resumable_statuses, input.status) {
        return refuse(
            409,
            "NATIVE_AGENT_ACTION_CONFLICT",
            "Agent run is not ready for this action",
        );
    }
    if input.operation == "runtime_update" {
        return allow("none", false, input.requested_revision, Vec::new());
    }
    if input.operation == "rerun" {
        let alias = contains(&contract.synthesizer_aliases, input.agent_id);
        let agent = if alias { "synthesizer" } else { input.agent_id };
        if agent.is_empty() {
            return refuse(400, "AGENT_RUN_REQUEST_INVALID", "agentId is required");
        }
        let mut events = vec![status_event("running", "rerun")];
        if agent == "synthesizer" {
            events.push(final_reset("rerun_synthesizer"));
            return allow("synthesis-only", true, input.requested_revision, events);
        }
        if !contains(&contract.known_agents, agent) {
            return refuse(400, "AGENT_RUN_REQUEST_INVALID", "Unknown Agent");
        }
        events.push(reset_event(agent, "rerun_agent"));
        if input.resynthesize {
            events.push(final_reset("rerun_agent"));
        }
        return allow("rerun-node", true, input.requested_revision, events);
    }
    let pending = incomplete(input.plan, input.nodes);
    if pending.is_empty() && input.has_final_answer {
        return allow(
            "none",
            false,
            input.requested_revision,
            vec![
                status_event("running", "resume"),
                status_event("done", "resume"),
            ],
        );
    }
    let mut events = vec![status_event("running", "resume")];
    if pending.is_empty() {
        events.push(final_reset("resume"));
        return allow("synthesis-only", true, input.requested_revision, events);
    }
    for id in &pending {
        events.push(reset_event(id, "resume"));
    }
    events.push(final_reset("resume"));
    allow("reset-incomplete", true, input.requested_revision, events)
}

fn result_identity(event: &Value) -> (String, String) {
    let phase = event
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let call_id = event
        .get("toolCallId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    (phase, call_id)
}

fn stored_tool_results(events: &[Value]) -> (bool, Vec<Value>) {
    let mut seen = Vec::<(String, String)>::new();
    for event in events {
        if event.get("type").and_then(Value::as_str) == Some("agent_tool_result") {
            let key = result_identity(event);
            if !seen.contains(&key) {
                seen.push(key);
            }
        }
    }
    let mut fresh = Vec::new();
    let mut had = false;
    for event in events {
        if event.get("type").and_then(Value::as_str) != Some("agent_tool_request") {
            continue;
        }
        let Some(results) = event.get("toolResults").and_then(Value::as_array) else {
            continue;
        };
        for item in results {
            if item.get("type").and_then(Value::as_str) != Some("agent_tool_result") {
                continue;
            }
            had = true;
            let key = result_identity(item);
            if seen.contains(&key) {
                continue;
            }
            seen.push(key);
            fresh.push(item.clone());
        }
    }
    (had, fresh)
}

/// Resume or rerun returns tool results already on the public event.
///
/// Other operations use `admit` with the retained effect. An
/// `agent_tool_request` that already holds `agent_tool_result` objects is
/// copied. This does not call the tool and does not append a confirmed effect.
/// A request with no stored result stays on the awaiting-tools refusal. A
/// result already present as its own event is not copied again.
pub fn resume_stored_tools(input: &RuntimeAdmission<'_>, events: &[Value]) -> RuntimeDecision {
    let retained = retained_effect(events);
    let probed = RuntimeAdmission {
        operation: input.operation,
        status: input.status,
        identity_valid: input.identity_valid,
        lease: input.lease,
        epoch: input.epoch,
        effect: retained.effect,
        runtime_revision: input.runtime_revision,
        requested_revision: input.requested_revision,
        agent_id: input.agent_id,
        resynthesize: input.resynthesize,
        plan: input.plan,
        nodes: input.nodes,
        has_final_answer: input.has_final_answer,
        same_effect: retained.same_effect,
    };
    if input.operation != "resume" && input.operation != "rerun" {
        return admit(&probed);
    }
    if retained.effect != "awaiting_tools" {
        return admit(&probed);
    }
    let (had, fresh) = stored_tool_results(events);
    if !had {
        return admit(&probed);
    }
    let probe = admit(&probed);
    if probe.code != "AGENT_TOOL_CONTINUATION_REQUIRED" {
        return probe;
    }
    RuntimeDecision {
        admitted: true,
        http_status: 200,
        code: "",
        message: "",
        dispatch: "none",
        started: false,
        bumps_revision: false,
        requested_revision: input.requested_revision,
        events: fresh,
    }
}

fn retained_tool_calls(events: &[Value]) -> Vec<Value> {
    let mut calls = Vec::new();
    for event in events {
        let Some(object) = event.as_object() else {
            continue;
        };
        if object.get("type").and_then(Value::as_str) != Some("agent_tool_request") {
            continue;
        }
        let Some(continuation) = object.get("toolContinuation").and_then(Value::as_object) else {
            continue;
        };
        let Some(raw) = continuation.get("toolCalls").and_then(Value::as_array) else {
            continue;
        };
        for item in raw {
            if item.as_object().is_some_and(|fields| !fields.is_empty()) {
                calls.push(item.clone());
            }
        }
    }
    calls
}

/// Run retained tool calls after `admit` accepts the same effect.
///
/// The calls are the private `toolContinuation` already on the stream. An
/// unknown stream, including a missing row, returns no result and does not
/// emit state `NOT_APPLIED`. The result does not append `CONFIRMED`.
/// `context` is the chat tool loop's workspace. `deny_store_write` refuses a
/// store Python still owns before `dispatch` can write it.
pub fn continue_retained_tools(
    input: &RuntimeAdmission<'_>,
    events: &[Value],
    context: &crate::tool_search::ExecutorContext<'_>,
    deny_store_write: &dyn Fn(&str) -> Option<Value>,
) -> RuntimeDecision {
    let retained = retained_effect(events);
    if retained.effect == "effect_unknown" {
        return refuse(
            409,
            "EFFECT_UNKNOWN",
            "Agent runtime effect is unknown and was not retried",
        );
    }
    let mut decision = admit(input);
    if !decision.admitted || decision.dispatch != "same-effect" {
        return decision;
    }
    if retained.effect != "awaiting_tools" || !retained.same_effect {
        return refuse(
            409,
            "AGENT_TOOL_CONTINUATION_REQUIRED",
            "Tool continuation requires the original awaiting effect",
        );
    }
    let calls = retained_tool_calls(events);
    if calls.is_empty() {
        return decision;
    }
    let mut results = Vec::new();
    for call in calls
        .into_iter()
        .take(crate::tool_dispatch::MAX_TOOL_CALLS_PER_RESPONSE)
    {
        let name = crate::tool_dispatch::tool_call_name(&call);
        let arguments = call
            .get("function")
            .and_then(|function| function.get("arguments"))
            .cloned()
            .unwrap_or(Value::Null);
        let output = if let Some(denied) = deny_store_write(&name) {
            denied
        } else {
            let outcome = crate::tool_dispatch::dispatch(&name, &arguments, None, None, context);
            match outcome.to_output() {
                Some(value) => value.clone(),
                None => json!({
                    "ok": false,
                    "tool": name,
                    "error": "Tool did not run",
                    "code": "internal",
                }),
            }
        };
        let tool_call_id = call.get("id").and_then(Value::as_str).unwrap_or("");
        results.push(json!({
            "type": "agent_tool_result",
            "phase": input.agent_id,
            "toolCallId": tool_call_id,
            "output": output,
        }));
    }
    decision.events = results;
    decision.started = false;
    decision.bumps_revision = false;
    decision
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

fn canonical_token(value: &str, limit: usize) -> bool {
    !value.is_empty()
        && value.len() <= limit
        && !value.chars().any(|mark| mark == '\n' || mark == '\r')
}

/// SHA-256 of the shared successor material. Go and the Python oracle use this line.
pub fn successor_operation_id(
    operation_id: &str,
    action_id: &str,
    epoch: u64,
    node_id: &str,
    reset_index: u64,
) -> String {
    let material = format!(
        "agent-node-successor-v1\n{operation_id}\n{action_id}\n{epoch}\n{node_id}\n{reset_index}"
    );
    let digest = Sha256::digest(material.as_bytes());
    format!("agent-successor-{}", hex_encode(&digest))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SuccessorInput<'a> {
    pub identity_valid: bool,
    pub lease: &'a str,
    pub epoch: &'a str,
    pub effect: &'a str,
    pub reset_admitted: bool,
    pub reset_fence_matches: bool,
    pub operation_id: &'a str,
    pub action_id: &'a str,
    pub execution_epoch: u64,
    pub node_id: &'a str,
    pub reset_index: u64,
    pub existing_successor: &'a str,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SuccessorDecision {
    pub admitted: bool,
    pub code: &'static str,
    pub message: &'static str,
    pub retained_operation_id: String,
    pub successor_operation_id: String,
    pub dispatch: &'static str,
    pub replayed: bool,
}

fn successor_refuse(
    retained: &str,
    code: &'static str,
    message: &'static str,
) -> SuccessorDecision {
    SuccessorDecision {
        admitted: false,
        code,
        message,
        retained_operation_id: retained.to_string(),
        successor_operation_id: String::new(),
        dispatch: "none",
        replayed: false,
    }
}

/// Mints a successor operation id only for a settled prior effect whose reset
/// event shares this fence. The original id is retained. `EFFECT_UNKNOWN` does
/// not receive a new id and must not become a provider call.
pub fn successor_generation(input: &SuccessorInput<'_>) -> SuccessorDecision {
    let retained = input.operation_id;
    if !input.identity_valid {
        return successor_refuse(
            retained,
            "INVALID_IDENTITY",
            "Agent runtime identity is invalid",
        );
    }
    if input.lease != "live" {
        return successor_refuse(
            retained,
            "LEASE_NOT_HELD",
            "Agent runtime lease is not live",
        );
    }
    if input.epoch == "stale" {
        return successor_refuse(
            retained,
            "STALE_EXECUTION_EPOCH",
            "Agent runtime epoch is stale",
        );
    }
    if input.epoch != "match" {
        return successor_refuse(
            retained,
            "FENCE_MISMATCH",
            "Agent runtime fence does not match",
        );
    }
    if input.effect == "AWAITING_TOOLS" {
        return successor_refuse(
            retained,
            "AGENT_DRIVER_TOOLS_PENDING",
            "Tool continuation stays on the original effect",
        );
    }
    if input.effect != "CONFIRMED" && input.effect != "SETTLED_FAILURE" {
        return successor_refuse(
            retained,
            "EFFECT_UNKNOWN",
            "Agent runtime effect is unknown and was not retried",
        );
    }
    if !input.reset_admitted || !input.reset_fence_matches {
        return successor_refuse(
            retained,
            "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
            "Agent reset requires a settled effect on the same fence",
        );
    }
    if !canonical_token(input.operation_id, 256)
        || !canonical_token(input.action_id, 256)
        || !canonical_token(input.node_id, 64)
        || input.execution_epoch == 0
    {
        return successor_refuse(
            retained,
            "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
            "Agent reset requires a settled effect on the same fence",
        );
    }
    let successor = successor_operation_id(
        input.operation_id,
        input.action_id,
        input.execution_epoch,
        input.node_id,
        input.reset_index,
    );
    if successor == input.operation_id {
        return successor_refuse(
            retained,
            "AGENT_RUNTIME_GENERATION_CONFLICT",
            "Agent successor generation does not replace a different operation",
        );
    }
    if !input.existing_successor.is_empty() && input.existing_successor != successor {
        return successor_refuse(
            retained,
            "AGENT_RUNTIME_GENERATION_CONFLICT",
            "Agent successor generation does not replace a different operation",
        );
    }
    SuccessorDecision {
        admitted: true,
        code: "",
        message: "",
        retained_operation_id: input.operation_id.to_string(),
        successor_operation_id: successor,
        dispatch: "successor",
        replayed: !input.existing_successor.is_empty(),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SuccessorBindingDecision {
    pub persisted: bool,
    pub replaced_operation_id: bool,
    pub operation_id: String,
    pub successor_operation_id: String,
    pub reset_index: u64,
    pub code: &'static str,
    pub replayed: bool,
}

/// Records the minted id beside the original operation. A refusal writes nothing.
pub fn successor_binding(input: &SuccessorInput<'_>) -> SuccessorBindingDecision {
    let decision = successor_generation(input);
    if !decision.admitted {
        return SuccessorBindingDecision {
            persisted: false,
            replaced_operation_id: false,
            operation_id: decision.retained_operation_id,
            successor_operation_id: input.existing_successor.to_string(),
            reset_index: 0,
            code: decision.code,
            replayed: false,
        };
    }
    SuccessorBindingDecision {
        persisted: true,
        replaced_operation_id: false,
        operation_id: decision.retained_operation_id,
        successor_operation_id: decision.successor_operation_id,
        reset_index: input.reset_index,
        code: "",
        replayed: decision.replayed,
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SuccessorExecutionInput<'a> {
    pub identity_valid: bool,
    pub lease: &'a str,
    pub epoch: &'a str,
    pub effect: &'a str,
    pub reset_admitted: bool,
    pub reset_fence_matches: bool,
    pub operation_id: &'a str,
    pub action_id: &'a str,
    pub execution_epoch: u64,
    pub node_id: &'a str,
    pub reset_index: u64,
    pub stored_successor: &'a str,
    pub stored_operation_id: &'a str,
    pub authority_issued: bool,
    pub worker_promoted: bool,
    pub authority_epoch: u64,
    pub presented_epoch: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SuccessorExecutionDecision {
    pub admitted: bool,
    pub execute: bool,
    pub code: &'static str,
    pub message: &'static str,
    pub retained_operation_id: String,
    pub retained_epoch: u64,
    pub operation_id: String,
    pub execution_epoch: u64,
    pub promoted: bool,
}

fn execution_refuse(
    retained: &str,
    retained_epoch: u64,
    code: &'static str,
    message: &'static str,
) -> SuccessorExecutionDecision {
    SuccessorExecutionDecision {
        admitted: false,
        execute: false,
        code,
        message,
        retained_operation_id: retained.to_string(),
        retained_epoch,
        operation_id: String::new(),
        execution_epoch: 0,
        promoted: false,
    }
}

/// Executes a persisted successor only when claim or takeover already issued a
/// newer epoch. A worker-promoted epoch is not installed.
pub fn successor_execution(input: &SuccessorExecutionInput<'_>) -> SuccessorExecutionDecision {
    let retained = input.operation_id;
    let retained_epoch = input.execution_epoch;
    if input.worker_promoted {
        return execution_refuse(
            retained,
            retained_epoch,
            "FENCE_MISMATCH",
            "Worker cannot promote its own execution epoch",
        );
    }
    if !input.identity_valid {
        return execution_refuse(
            retained,
            retained_epoch,
            "INVALID_IDENTITY",
            "Agent runtime identity is invalid",
        );
    }
    if input.lease != "live" {
        return execution_refuse(
            retained,
            retained_epoch,
            "LEASE_NOT_HELD",
            "Agent runtime lease is not live",
        );
    }
    if input.epoch == "stale"
        || (input.authority_epoch > 0 && input.presented_epoch < input.authority_epoch)
    {
        return execution_refuse(
            retained,
            retained_epoch,
            "STALE_EXECUTION_EPOCH",
            "Agent runtime epoch is stale",
        );
    }
    if input.epoch != "match"
        || input.authority_epoch == 0
        || input.presented_epoch == 0
        || input.presented_epoch != input.authority_epoch
    {
        return execution_refuse(
            retained,
            retained_epoch,
            "FENCE_MISMATCH",
            "Agent runtime fence does not match",
        );
    }
    if input.effect == "AWAITING_TOOLS" {
        return execution_refuse(
            retained,
            retained_epoch,
            "AGENT_DRIVER_TOOLS_PENDING",
            "Tool continuation stays on the original effect",
        );
    }
    if input.effect != "CONFIRMED" && input.effect != "SETTLED_FAILURE" {
        return execution_refuse(
            retained,
            retained_epoch,
            "EFFECT_UNKNOWN",
            "Agent runtime effect is unknown and was not retried",
        );
    }
    if !input.reset_admitted || !input.reset_fence_matches {
        return execution_refuse(
            retained,
            retained_epoch,
            "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
            "Agent reset requires a settled effect on the same fence",
        );
    }
    if !input.authority_issued {
        return execution_refuse(
            retained,
            retained_epoch,
            "FENCE_MISMATCH",
            "Only claim or takeover can issue the next execution epoch",
        );
    }
    if !canonical_token(input.operation_id, 256)
        || !canonical_token(input.action_id, 256)
        || !canonical_token(input.node_id, 64)
        || input.execution_epoch == 0
    {
        return execution_refuse(
            retained,
            retained_epoch,
            "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
            "Agent reset requires a settled effect on the same fence",
        );
    }
    if input.stored_operation_id != input.operation_id {
        return execution_refuse(
            retained,
            retained_epoch,
            "AGENT_RUNTIME_GENERATION_CONFLICT",
            "Agent successor generation does not replace a different operation",
        );
    }
    if input.authority_epoch <= input.execution_epoch {
        return execution_refuse(
            retained,
            retained_epoch,
            "AGENT_RUNTIME_SUCCESSOR_HELD",
            "Successor generation stays on the original fence until takeover",
        );
    }
    let successor = successor_operation_id(
        input.operation_id,
        input.action_id,
        input.execution_epoch,
        input.node_id,
        input.reset_index,
    );
    if input.stored_successor.is_empty()
        || input.stored_successor != successor
        || input.stored_successor == input.operation_id
    {
        return execution_refuse(
            retained,
            retained_epoch,
            "AGENT_RUNTIME_SUCCESSOR_HELD",
            "Successor generation is not persisted on the original intent",
        );
    }
    SuccessorExecutionDecision {
        admitted: true,
        execute: true,
        code: "",
        message: "",
        retained_operation_id: input.operation_id.to_string(),
        retained_epoch: input.execution_epoch,
        operation_id: input.stored_successor.to_string(),
        execution_epoch: input.authority_epoch,
        promoted: false,
    }
}

/// Classifies a retained effect before any new provider call.
/// `AWAITING_TOOLS` stays on the original effect. Unknown, stale, mismatched,
/// and dead-lease inputs do not become a fresh dispatch.
pub fn dispatch_allowed(lease: &str, epoch: &str, effect: &str) -> (&'static str, bool) {
    if lease != "live" {
        return ("LEASE_NOT_HELD", false);
    }
    if epoch == "stale" {
        return ("STALE_EXECUTION_EPOCH", false);
    }
    if epoch != "match" {
        return ("FENCE_MISMATCH", false);
    }
    match effect {
        "EFFECT_UNKNOWN" | "" => ("EFFECT_UNKNOWN", false),
        "AWAITING_TOOLS" => ("AGENT_DRIVER_TOOLS_PENDING", false),
        "CONFIRMED" | "SETTLED_FAILURE" | "NONE" => ("", true),
        _ => ("EFFECT_UNKNOWN", false),
    }
}

pub const RETAINED_TOOL_RESULT_SCOPE: &str = "retained_tool_results";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DriverToolDecision {
    pub returned: bool,
    pub code: String,
    pub events: Vec<Value>,
}

fn driver_pending() -> DriverToolDecision {
    DriverToolDecision {
        returned: false,
        code: "AGENT_DRIVER_TOOLS_PENDING".to_string(),
        events: Vec::new(),
    }
}

fn event_body_ref(event: &Value) -> Option<(String, u64)> {
    let reference = event.get("bodyRef").or_else(|| event.get("body"))?;
    let sha = reference.get("sha256").and_then(Value::as_str)?;
    let length = reference.get("length").and_then(Value::as_u64)?;
    let lowercase_hex = sha.len() == 64
        && sha
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase());
    if !lowercase_hex || length == 0 {
        return None;
    }
    Some((sha.to_string(), length))
}

fn projected_driver_results(events: &[Value], public: Option<&Value>) -> Vec<Value> {
    let Some(public) = public else {
        return Vec::new();
    };
    let Some(want) = event_body_ref(&json!({"bodyRef": public})) else {
        return Vec::new();
    };
    let mut phase: Option<String> = None;
    for event in events {
        if event.get("type").and_then(Value::as_str) != Some("agent_tool_request") {
            continue;
        }
        if event.get("scope").and_then(Value::as_str) != Some(RETAINED_TOOL_RESULT_SCOPE) {
            continue;
        }
        if event_body_ref(event).as_ref() != Some(&want) {
            continue;
        }
        phase = Some(
            event
                .get("phase")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        );
    }
    let Some(phase) = phase.filter(|value| !value.is_empty()) else {
        return Vec::new();
    };
    let mut fresh = Vec::new();
    let mut seen = Vec::<(String, String, String)>::new();
    for event in events {
        if event.get("type").and_then(Value::as_str) != Some("agent_tool_result") {
            continue;
        }
        if event.get("phase").and_then(Value::as_str) != Some(phase.as_str()) {
            continue;
        }
        let Some(body) = event_body_ref(event) else {
            continue;
        };
        let call = event
            .get("toolCallId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .or_else(|| event.get("scope").and_then(Value::as_str))
            .unwrap_or("")
            .to_string();
        let key = (phase.clone(), call, body.0);
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        fresh.push(event.clone());
    }
    fresh
}

fn stored_result_copies(events: &[Value]) -> Vec<Value> {
    let mut wanted = Vec::<(String, String)>::new();
    for event in events {
        if event.get("type").and_then(Value::as_str) != Some("agent_tool_request") {
            continue;
        }
        let Some(results) = event.get("toolResults").and_then(Value::as_array) else {
            continue;
        };
        for item in results {
            if item.get("type").and_then(Value::as_str) == Some("agent_tool_result") {
                let key = result_identity(item);
                if !wanted.contains(&key) {
                    wanted.push(key);
                }
            }
        }
    }
    let mut copies = Vec::new();
    let mut seen = Vec::<(String, String)>::new();
    for event in events {
        if event.get("type").and_then(Value::as_str) != Some("agent_tool_result") {
            continue;
        }
        let key = result_identity(event);
        if !wanted.contains(&key) || seen.contains(&key) {
            continue;
        }
        seen.push(key);
        copies.push(event.clone());
    }
    copies
}

/// Return stored tool results for one waiting driver observation.
///
/// A public request that already holds result objects is copied. Header copies
/// are returned only when a request marker names the same public artifact.
/// This does not call the tool and does not settle the node. A waiting effect
/// with no stored result stays pending.
pub fn retained_driver_tool_results(
    state: &str,
    events: &[Value],
    public: Option<&Value>,
) -> DriverToolDecision {
    if state != "AWAITING_TOOLS" && state != "awaiting_tools" {
        return DriverToolDecision {
            returned: false,
            code: String::new(),
            events: Vec::new(),
        };
    }
    let (had, fresh) = stored_tool_results(events);
    if had {
        let copies = if fresh.is_empty() {
            stored_result_copies(events)
        } else {
            fresh
        };
        if !copies.is_empty() {
            return DriverToolDecision {
                returned: true,
                code: String::new(),
                events: copies,
            };
        }
        return driver_pending();
    }
    let projected = projected_driver_results(events, public);
    if !projected.is_empty() {
        return DriverToolDecision {
            returned: true,
            code: String::new(),
            events: projected,
        };
    }
    driver_pending()
}

#[derive(Debug, Clone, PartialEq)]
pub struct ModelTurnDecision {
    pub started: bool,
    pub code: String,
    pub body: Option<Value>,
    pub events: Vec<Value>,
}

fn model_turn_idle(code: &str) -> ModelTurnDecision {
    ModelTurnDecision {
        started: false,
        code: code.to_string(),
        body: None,
        events: Vec::new(),
    }
}

fn stored_outputs(events: &[Value]) -> Vec<(String, Value)> {
    let mut found = Vec::new();
    for event in events {
        if event.get("type").and_then(Value::as_str) != Some("agent_tool_request") {
            continue;
        }
        let Some(results) = event.get("toolResults").and_then(Value::as_array) else {
            continue;
        };
        for item in results {
            if item.get("type").and_then(Value::as_str) != Some("agent_tool_result") {
                continue;
            }
            let Some(call_id) = item.get("toolCallId").and_then(Value::as_str) else {
                continue;
            };
            if call_id.is_empty() {
                continue;
            }
            let Some(output) = item.get("output").filter(|value| value.is_object()) else {
                continue;
            };
            if found.iter().any(|(key, _)| key == call_id) {
                continue;
            }
            found.push((call_id.to_string(), output.clone()));
        }
    }
    found
}

fn continuation_turn(events: &[Value]) -> (Value, Vec<Value>) {
    let mut assistant = Value::Null;
    let mut calls = Vec::new();
    for event in events {
        if event.get("type").and_then(Value::as_str) != Some("agent_tool_request") {
            continue;
        }
        let Some(continuation) = event
            .get("toolContinuation")
            .filter(|value| value.is_object())
        else {
            continue;
        };
        if let Some(raw) = continuation.get("toolCalls").and_then(Value::as_array) {
            calls = raw
                .iter()
                .filter(|item| item.as_object().is_some_and(|object| !object.is_empty()))
                .cloned()
                .collect();
        }
        assistant = continuation
            .get("assistant")
            .cloned()
            .unwrap_or(Value::Null);
    }
    (assistant, calls)
}

/// Build the next provider body from tool results already stored.
///
/// The assistant turn and each tool message come from the retained request.
/// This does not call the tool. A waiting request with no stored result does
/// not start the turn. The decision appends no effect.
pub fn next_model_turn(body: &Value, events: &[Value]) -> ModelTurnDecision {
    let retained = retained_effect(events);
    if retained.effect == "effect_unknown" {
        return model_turn_idle("EFFECT_UNKNOWN");
    }
    if retained.effect != "awaiting_tools" {
        return model_turn_idle("");
    }
    let Some(root) = body.as_object() else {
        return model_turn_idle("AGENT_DRIVER_TOOLS_PENDING");
    };
    let (assistant, calls) = continuation_turn(events);
    let outputs = stored_outputs(events);
    let selected: Vec<Value> = calls
        .into_iter()
        .take(crate::tool_dispatch::MAX_TOOL_CALLS_PER_RESPONSE)
        .collect();
    if selected.is_empty() {
        return model_turn_idle("AGENT_DRIVER_TOOLS_PENDING");
    }
    let mut tool_messages = Vec::with_capacity(selected.len());
    for call in &selected {
        let Some(call_id) = call.get("id").and_then(Value::as_str) else {
            return model_turn_idle("AGENT_DRIVER_TOOLS_PENDING");
        };
        let Some((_, output)) = outputs.iter().find(|(key, _)| key == call_id) else {
            return model_turn_idle("AGENT_DRIVER_TOOLS_PENDING");
        };
        tool_messages.push(crate::tool_batch::tool_result_message(call, output));
    }
    let content = match assistant.get("content") {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    };
    let mut payload = json!({
        "role": "assistant",
        "content": content,
        "tool_calls": selected,
    });
    let reasoning = assistant
        .get("reasoning_content")
        .or_else(|| assistant.get("reasoning"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if !reasoning.is_empty() {
        payload["reasoning_content"] = json!(reasoning);
    }
    let mut messages = root
        .get("messages")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    messages.push(payload);
    messages.extend(tool_messages);
    let mut next = root.clone();
    next.insert("messages".to_string(), Value::Array(messages));
    if next.get("tool_choice").is_some_and(Value::is_object) {
        next.insert("tool_choice".to_string(), Value::String("auto".to_string()));
    }
    ModelTurnDecision {
        started: true,
        code: String::new(),
        body: Some(Value::Object(next)),
        events: Vec::new(),
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ModelAnswerDecision {
    pub published: bool,
    pub code: String,
    pub event: Option<Value>,
}

fn answer_idle(code: &str) -> ModelAnswerDecision {
    ModelAnswerDecision {
        published: false,
        code: code.to_string(),
        event: None,
    }
}

fn finished_public_answer(event: &Value) -> Option<Value> {
    let object = event.as_object()?;
    if object.get("type").and_then(Value::as_str) != Some("agent_output") {
        return None;
    }
    let phase = object
        .get("phase")
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())?;
    let output = object.get("output").and_then(Value::as_object)?;
    if let Some(identity) = output.get("id").filter(|value| !value.is_null()) {
        if identity.as_str() != Some(phase) {
            return None;
        }
    }
    let content = output.get("content").and_then(Value::as_str)?;
    if content.trim().is_empty() {
        return None;
    }
    let calls = object.get("toolCalls").and_then(Value::as_array);
    let unfinished = calls.is_some_and(|items| {
        items
            .iter()
            .any(|item| item.as_object().is_some_and(|fields| !fields.is_empty()))
    });
    if unfinished {
        return None;
    }
    Some(json!({
        "type": "agent_output",
        "phase": phase,
        "output": Value::Object(output.clone()),
    }))
}

fn request_published_answer(event: &Value) -> Option<Value> {
    let answer = finished_public_answer(event.get("publishedAnswer")?)?;
    let phase = event.get("phase").and_then(Value::as_str)?;
    if answer.get("phase").and_then(Value::as_str) != Some(phase) {
        return None;
    }
    Some(answer)
}

fn envelope_has_calls(saved: &Value) -> bool {
    let Some(continuation) = saved.get("toolContinuation").and_then(Value::as_object) else {
        return false;
    };
    let Some(calls) = continuation.get("toolCalls").and_then(Value::as_array) else {
        return false;
    };
    calls
        .iter()
        .any(|item| item.as_object().is_some_and(|fields| !fields.is_empty()))
}

fn waiting_phase(events: &[Value]) -> &str {
    let mut phase = "";
    for event in events {
        if event.get("type").and_then(Value::as_str) != Some("agent_tool_request") {
            continue;
        }
        if let Some(raw) = event.get("phase").and_then(Value::as_str) {
            phase = raw;
        }
    }
    phase
}

/// Publish a saved follow-up that has no further tool calls.
///
/// A missing file does not publish and is not a new effect state.
pub fn published_model_answer(events: &[Value], saved: &Value) -> ModelAnswerDecision {
    let retained = retained_effect(events);
    if retained.effect == "effect_unknown" {
        return answer_idle("EFFECT_UNKNOWN");
    }
    if retained.effect != "awaiting_tools" {
        return answer_idle("");
    }
    if !saved.is_object() || envelope_has_calls(saved) {
        return answer_idle("");
    }
    let nested = saved.get("publicEvent").filter(|value| value.is_object());
    let candidate = nested.unwrap_or(saved);
    let Some(answer) = finished_public_answer(candidate) else {
        return answer_idle("");
    };
    if answer.get("phase").and_then(Value::as_str) != Some(waiting_phase(events)) {
        return answer_idle("");
    }
    ModelAnswerDecision {
        published: true,
        code: String::new(),
        event: Some(answer),
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct PublishedAnswerSettlement {
    pub settled: bool,
    pub code: String,
    pub event: Option<Value>,
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    pub latency_ms: i64,
    pub failed: bool,
    pub task: String,
}

fn settlement_idle(code: &str) -> PublishedAnswerSettlement {
    PublishedAnswerSettlement {
        settled: false,
        code: code.to_string(),
        event: None,
        prompt_tokens: 0,
        completion_tokens: 0,
        latency_ms: 0,
        failed: false,
        task: String::new(),
    }
}

fn stored_epoch(value: u64) -> Option<u64> {
    if value == 0 { None } else { Some(value) }
}

fn settlement_counter(value: Option<&Value>) -> Option<u64> {
    match value {
        None => Some(0),
        Some(number) => number.as_u64().filter(|count| *count <= i64::MAX as u64),
    }
}

fn strip_published_answer(events: &[Value]) -> (Vec<Value>, Option<Value>) {
    let mut stripped = Vec::with_capacity(events.len());
    let mut attached = None;
    for event in events {
        let Some(object) = event.as_object() else {
            stripped.push(event.clone());
            continue;
        };
        if object.get("type").and_then(Value::as_str) == Some("agent_tool_request")
            && object.contains_key("publishedAnswer")
        {
            attached = object.get("publishedAnswer").cloned();
            let mut clone = object.clone();
            clone.remove("publishedAnswer");
            stripped.push(Value::Object(clone));
        } else {
            stripped.push(event.clone());
        }
    }
    (stripped, attached)
}

/// Admit settlement of an answer already published on the tool request.
///
/// The presented epoch must already be the installed epoch. A larger value
/// is a fence mismatch and is not installed here. A missing answer does not
/// settle and does not invent a receipt.
pub fn settle_published_answer(
    events: &[Value],
    saved: &Value,
    presented_epoch: u64,
    installed_epoch: u64,
    intent_task: &str,
) -> PublishedAnswerSettlement {
    let Some(presented) = stored_epoch(presented_epoch) else {
        return settlement_idle("FENCE_MISMATCH");
    };
    let Some(installed) = stored_epoch(installed_epoch) else {
        return settlement_idle("FENCE_MISMATCH");
    };
    if presented > installed {
        return settlement_idle("FENCE_MISMATCH");
    }
    if presented < installed {
        return settlement_idle("STALE_EXECUTION_EPOCH");
    }
    let (stripped, attached) = strip_published_answer(events);
    let Some(attached) = attached.filter(|value| value.is_object()) else {
        return settlement_idle("");
    };
    let decision = published_model_answer(&stripped, saved);
    if !decision.published {
        return settlement_idle(&decision.code);
    }
    let Some(event) = decision.event else {
        return settlement_idle("");
    };
    if event != attached {
        return settlement_idle("");
    }
    let Some(output) = event.get("output").and_then(Value::as_object) else {
        return settlement_idle("");
    };
    if output.get("id").and_then(Value::as_str) != event.get("phase").and_then(Value::as_str) {
        return settlement_idle("");
    }
    let task = output.get("task").and_then(Value::as_str).unwrap_or("");
    if task.is_empty() || task != intent_task {
        return settlement_idle("");
    }
    let latency = output.get("latencyMs").and_then(Value::as_i64);
    let Some(latency) = latency.filter(|ms| *ms >= 0) else {
        return settlement_idle("");
    };
    let usage = output.get("usage").cloned().unwrap_or_else(|| json!({}));
    if !usage.is_object() {
        return settlement_idle("");
    }
    let Some(prompt) = settlement_counter(usage.get("prompt_tokens")) else {
        return settlement_idle("");
    };
    let Some(completion) = settlement_counter(usage.get("completion_tokens")) else {
        return settlement_idle("");
    };
    let failed = match output.get("failed") {
        None => false,
        Some(Value::Bool(value)) => *value,
        Some(_) => return settlement_idle(""),
    };
    // `task` borrows from `event`; own it before `event` moves into the result.
    let task = task.to_string();
    PublishedAnswerSettlement {
        settled: true,
        code: String::new(),
        event: Some(event),
        prompt_tokens: prompt,
        completion_tokens: completion,
        latency_ms: latency,
        failed,
        task,
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PublishedDependencyOutput {
    pub ready: bool,
    pub code: String,
    pub output: Option<Value>,
}

fn dependency_idle(code: &str) -> PublishedDependencyOutput {
    PublishedDependencyOutput {
        ready: false,
        code: code.to_string(),
        output: None,
    }
}

/// Return a dependency output already stored on the row or the saved answer.
///
/// A direct answer is copied from the row. A tool request contributes the
/// saved follow-up only when that follow-up publishes. A missing follow-up
/// does not invent an output and does not call the tool.
pub fn published_dependency_output(
    envelope: &Value,
    saved: Option<&Value>,
    dependency: &str,
    task: &str,
    run_id: &str,
) -> PublishedDependencyOutput {
    let Some(root) = envelope.as_object() else {
        return dependency_idle("");
    };
    if root.get("runId").and_then(Value::as_str) != Some(run_id)
        || dependency.is_empty()
        || task.is_empty()
    {
        return dependency_idle("");
    }
    let Some(event) = root.get("publicEvent").and_then(Value::as_object) else {
        return dependency_idle("");
    };
    let kind = event.get("type").and_then(Value::as_str).unwrap_or("");
    if kind == "agent_output" {
        if event.get("phase").and_then(Value::as_str) != Some(dependency) {
            return dependency_idle("");
        }
        let Some(output) = event.get("output").and_then(Value::as_object) else {
            return dependency_idle("");
        };
        if output.get("id").and_then(Value::as_str) != Some(dependency)
            || output.get("task").and_then(Value::as_str) != Some(task)
        {
            return dependency_idle("");
        }
        return PublishedDependencyOutput {
            ready: true,
            code: String::new(),
            output: Some(Value::Object(output.clone())),
        };
    }
    if kind != "agent_tool_request"
        || event.get("phase").and_then(Value::as_str) != Some(dependency)
    {
        return dependency_idle("");
    }
    let event_value = Value::Object(event.clone());
    let missing = Value::Null;
    let decision = published_model_answer(
        std::slice::from_ref(&event_value),
        saved.unwrap_or(&missing),
    );
    if !decision.published {
        return dependency_idle(&decision.code);
    }
    let Some(answer) = decision.event.as_ref().and_then(Value::as_object) else {
        return dependency_idle("");
    };
    let Some(output) = answer.get("output").and_then(Value::as_object) else {
        return dependency_idle("");
    };
    if output.get("id").and_then(Value::as_str) != Some(dependency)
        || output.get("task").and_then(Value::as_str) != Some(task)
    {
        return dependency_idle("");
    }
    PublishedDependencyOutput {
        ready: true,
        code: String::new(),
        output: Some(Value::Object(output.clone())),
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct DependencyProviderBody {
    pub built: bool,
    pub code: String,
    pub worker: Option<Value>,
    pub synthesis: Option<Value>,
}

fn provider_body_idle(code: &str) -> DependencyProviderBody {
    DependencyProviderBody {
        built: false,
        code: code.to_string(),
        worker: None,
        synthesis: None,
    }
}

/// Build one provider body from outputs the caller already resolved.
///
/// An empty output list does not call the payload builders.
pub fn provider_body_from_outputs(
    outputs: &[Value],
    payload: &Value,
    role: &str,
    task: &str,
    model: &str,
    query: &str,
    synthesis: bool,
) -> Option<Value> {
    if outputs.is_empty() {
        return None;
    }
    if synthesis {
        return Some(crate::multi_agent::synthesis_payload(
            payload, model, query, outputs,
        ));
    }
    crate::multi_agent::worker_payload(payload, role, task, outputs, model)
}

/// Place one published dependency output into the worker and synthesis bodies.
///
/// A missing answer does not build either body. An unknown observation keeps
/// its code and does not build either body.
#[allow(clippy::too_many_arguments)]
pub fn dependency_provider_body(
    envelope: &Value,
    saved: Option<&Value>,
    dependency: &str,
    task: &str,
    run_id: &str,
    payload: &Value,
    role: &str,
    assignment: &str,
    model: &str,
    query: &str,
) -> DependencyProviderBody {
    let decision = published_dependency_output(envelope, saved, dependency, task, run_id);
    let ready = decision.ready;
    let code = if decision.code.is_empty() {
        "AGENT_DEPENDENCY_UNPUBLISHED".to_string()
    } else {
        decision.code.clone()
    };
    let Some(output) = decision.output.filter(|_| ready) else {
        return provider_body_idle(&code);
    };
    if !payload.is_object()
        || crate::multi_agent::profile(role).is_none()
        || assignment.is_empty()
        || model.is_empty()
        || query.trim().is_empty()
    {
        return provider_body_idle("");
    }
    let outputs = [output];
    let Some(worker) =
        provider_body_from_outputs(&outputs, payload, role, assignment, model, query, false)
    else {
        return provider_body_idle("");
    };
    let Some(synthesis) =
        provider_body_from_outputs(&outputs, payload, role, assignment, model, query, true)
    else {
        return provider_body_idle("");
    };
    DependencyProviderBody {
        built: true,
        code: String::new(),
        worker: Some(worker),
        synthesis: Some(synthesis),
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExecutionSendLease {
    pub allowed: bool,
    pub code: &'static str,
    pub mark_unknown: bool,
    pub provider_effect: bool,
}

/// Admit a provider send only while the execution lease is still ahead.
///
/// A deadline that has arrived does not start the provider call and does not
/// store an effect. The caller marks the admitted node unknown.
pub fn execution_send_lease(deadline: i64, now: i64) -> ExecutionSendLease {
    if deadline <= 0 || now <= 0 || deadline <= now {
        return ExecutionSendLease {
            allowed: false,
            code: "AGENT_EXECUTION_LEASE_DENIED",
            mark_unknown: true,
            provider_effect: false,
        };
    }
    ExecutionSendLease {
        allowed: true,
        code: "",
        mark_unknown: false,
        provider_effect: false,
    }
}

/// Forward the shaped body only for a clean live execution lease.
///
/// Any other lease result denies the send and stores no effect.
pub fn admitted_provider_send(lease: ExecutionSendLease) -> ExecutionSendLease {
    if !lease.allowed || !lease.code.is_empty() || lease.mark_unknown || lease.provider_effect {
        return ExecutionSendLease {
            allowed: false,
            code: "AGENT_EXECUTION_LEASE_DENIED",
            mark_unknown: true,
            provider_effect: false,
        };
    }
    ExecutionSendLease {
        allowed: true,
        code: "",
        mark_unknown: false,
        provider_effect: false,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ProviderExchangeUnknown {
    pub code: &'static str,
    pub mark_unknown: bool,
    pub provider_effect: bool,
}

/// Classify an admitted send before any provider effect is stored.
///
/// A timeout, a missing object, and an absent log are the same refusal. The
/// caller marks the admitted node unknown. This decision does not store an effect.
pub fn provider_exchange_unknown(receipt_present: bool) -> ProviderExchangeUnknown {
    if receipt_present {
        return ProviderExchangeUnknown {
            code: "",
            mark_unknown: false,
            provider_effect: false,
        };
    }
    ProviderExchangeUnknown {
        code: "AGENT_PROVIDER_RESULT_UNKNOWN",
        mark_unknown: true,
        provider_effect: false,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FollowUpExchange {
    pub store: bool,
    pub code: &'static str,
    pub mark_unknown: bool,
    pub provider_effect: bool,
}

/// Store a follow-up only after a live lease and a present receipt.
///
/// A dead lease does not start the send. A lost receipt does not store the
/// answer and does not mark the confirmed tool request unknown.
pub fn follow_up_exchange(lease_allowed: bool, receipt_present: bool) -> FollowUpExchange {
    if !lease_allowed {
        return FollowUpExchange {
            store: false,
            code: "AGENT_EXECUTION_LEASE_DENIED",
            mark_unknown: false,
            provider_effect: false,
        };
    }
    if !receipt_present {
        return FollowUpExchange {
            store: false,
            code: "AGENT_PROVIDER_RESULT_UNKNOWN",
            mark_unknown: false,
            provider_effect: false,
        };
    }
    FollowUpExchange {
        store: true,
        code: "",
        mark_unknown: false,
        provider_effect: false,
    }
}

/// Return the minimum of the grant expiry, the claim lease, and the writer lease.
///
/// The number is not a send. The caller passes it through `execution_send_lease`
/// before a provider exchange or a live workspace flag. A non-positive result
/// stays a number so that decision can refuse it.
pub fn agent_node_send_deadline(grant_expires: i64, claim_lease: i64, writer_lease: i64) -> i64 {
    grant_expires.min(claim_lease).min(writer_lease)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExecutionSendBudget {
    pub armed: bool,
    pub seconds: u64,
    pub code: &'static str,
    pub provider_effect: bool,
}

/// Arm a provider timeout only for the remaining seconds of a live lease.
///
/// `execution_send_lease` runs first. An elapsed or non-positive minimum
/// returns no timeout. A live minimum returns the smaller of the remaining
/// whole seconds and a positive cap.
pub fn execution_send_budget(deadline: i64, now: i64, cap_seconds: u64) -> ExecutionSendBudget {
    let admit = admitted_provider_send(execution_send_lease(deadline, now));
    let idle = ExecutionSendBudget {
        armed: false,
        seconds: 0,
        code: "AGENT_EXECUTION_LEASE_DENIED",
        provider_effect: false,
    };
    if !admit.allowed
        || !admit.code.is_empty()
        || admit.mark_unknown
        || admit.provider_effect
        || cap_seconds == 0
        || cap_seconds > i64::MAX as u64
    {
        return idle;
    }
    let remaining = (deadline - now) as u64;
    let seconds = remaining.min(cap_seconds);
    if seconds == 0 {
        return idle;
    }
    ExecutionSendBudget {
        armed: true,
        seconds,
        code: "",
        provider_effect: false,
    }
}
