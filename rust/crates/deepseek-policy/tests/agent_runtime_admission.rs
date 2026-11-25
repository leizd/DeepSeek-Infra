use deepseek_policy::agent_runtime_admission::*;
use serde_json::json;

#[test]
fn test_current_runtime_revision() {
    assert_eq!(current_runtime_revision(&[]), Ok(1));
    let events = vec![json!({"type": "runtime_payload", "runtimeRevision": 2})];
    assert_eq!(current_runtime_revision(&events), Ok(2));
    let gap = vec![json!({"type": "runtime_payload", "runtimeRevision": 3})];
    assert!(current_runtime_revision(&gap).is_err());
}

#[test]
fn test_node_effect_and_digest() {
    let (digest, len) = node_effect_digest("node-1", "AWAITING_TOOLS", "").unwrap();
    assert!(!digest.is_empty());
    assert!(len > 0);
    let val = node_effect_value("node-1", "AWAITING_TOOLS", "").unwrap();
    assert_eq!(val["type"], "node_effect");
    assert_eq!(val["phase"], "node-1");
    assert_eq!(val["status"], "AWAITING_TOOLS");

    let obs_val = node_effect_value("node-1", "", "missing").unwrap();
    assert_eq!(obs_val["observation"], "missing");
    assert_eq!(obs_val["scope"], "missing");

    assert!(node_effect_bytes("invalid\0token", "", "").is_none());
    assert!(node_effect_digest("invalid\0token", "", "").is_err());
    assert!(node_effect_value("invalid\0token", "", "").is_err());
}

#[test]
fn test_project_journal_observation() {
    let obs = project_journal_observation("node-1", true, Some("CONFIRMED"), None);
    assert_eq!(obs.kind, "settled");
    assert!(!obs.append);
    assert_eq!(obs.status, "CONFIRMED");

    let unreadable = project_journal_observation("bad\0id", true, Some("CONFIRMED"), None);
    assert_eq!(unreadable.kind, "unreadable");
    assert!(unreadable.append);

    let missing = project_journal_observation("node-1", false, None, None);
    assert_eq!(missing.kind, "unreadable");
    assert_eq!(missing.scope, "missing");
    assert!(missing.append);

    let timeout =
        project_journal_observation("node-1", true, None, Some("context Deadline Exceeded"));
    assert_eq!(timeout.kind, "unreadable");
    assert_eq!(timeout.scope, "timeout");

    let absent_log =
        project_journal_observation("node-1", true, None, Some("ABSENT_LOG in worker"));
    assert_eq!(absent_log.kind, "unreadable");
    assert_eq!(absent_log.scope, "absent_log");

    let awaiting = project_journal_observation("node-1", true, Some("AWAITING_TOOLS"), None);
    assert_eq!(awaiting.kind, "awaiting");
    assert_eq!(awaiting.status, "AWAITING_TOOLS");

    let not_applied = project_journal_observation("node-1", true, Some("NOT_APPLIED"), None);
    assert_eq!(not_applied.kind, "unreadable");
    assert_eq!(not_applied.scope, "not_applied");

    let unknown = project_journal_observation("node-1", true, Some("EFFECT_UNKNOWN"), None);
    assert_eq!(unknown.kind, "unknown");
    assert_eq!(unknown.status, "EFFECT_UNKNOWN");

    let dispatching = project_journal_observation("node-1", true, Some("DISPATCHING"), None);
    assert_eq!(dispatching.kind, "unknown");
    assert_eq!(dispatching.status, "DISPATCHING");
}

#[test]
fn test_execution_send_lease_and_budget() {
    let lease = execution_send_lease(100, 50);
    assert!(lease.allowed);
    assert_eq!(lease.code, "");
    assert!(!lease.mark_unknown);
    assert!(!lease.provider_effect);

    let admitted = admitted_provider_send(lease);
    assert!(admitted.allowed);

    let expired = execution_send_lease(50, 100);
    assert!(!expired.allowed);
    assert_eq!(expired.code, "AGENT_EXECUTION_LEASE_DENIED");
    assert!(expired.mark_unknown);

    let admitted_expired = admitted_provider_send(expired);
    assert!(!admitted_expired.allowed);

    let budget = execution_send_budget(100, 50, 30);
    assert!(budget.armed);
    assert_eq!(budget.seconds, 30);

    let budget_cap = execution_send_budget(100, 50, 60);
    assert!(budget_cap.armed);
    assert_eq!(budget_cap.seconds, 50);

    let budget_denied = execution_send_budget(50, 100, 30);
    assert!(!budget_denied.armed);
    assert_eq!(budget_denied.code, "AGENT_EXECUTION_LEASE_DENIED");
}

#[test]
fn test_provider_exchange_and_follow_up() {
    let unk_receipt = provider_exchange_unknown(true);
    assert_eq!(unk_receipt.code, "");
    assert!(!unk_receipt.mark_unknown);

    let unk_no_receipt = provider_exchange_unknown(false);
    assert_eq!(unk_no_receipt.code, "AGENT_PROVIDER_RESULT_UNKNOWN");
    assert!(unk_no_receipt.mark_unknown);

    let follow_up_ok = follow_up_exchange(true, true);
    assert!(follow_up_ok.store);
    assert_eq!(follow_up_ok.code, "");
    assert!(!follow_up_ok.mark_unknown);

    let follow_up_no_receipt = follow_up_exchange(true, false);
    assert!(!follow_up_no_receipt.store);
    assert_eq!(follow_up_no_receipt.code, "AGENT_PROVIDER_RESULT_UNKNOWN");
    assert!(!follow_up_no_receipt.mark_unknown);

    let follow_up_no_lease = follow_up_exchange(false, true);
    assert!(!follow_up_no_lease.store);
    assert_eq!(follow_up_no_lease.code, "AGENT_EXECUTION_LEASE_DENIED");
    assert!(!follow_up_no_lease.mark_unknown);
}

#[test]
fn test_agent_node_send_deadline() {
    assert_eq!(agent_node_send_deadline(100, 90, 80), 80);
    assert_eq!(agent_node_send_deadline(70, 90, 80), 70);
    assert_eq!(agent_node_send_deadline(100, 60, 80), 60);
}

#[test]
fn test_dispatch_allowed() {
    // The effect vocabulary is the uppercase wire form; "same"/"none" belong to
    // the older epoch and effect spellings and are refused as unknown.
    let (reason, allowed) = dispatch_allowed("live", "match", "CONFIRMED");
    assert!(allowed);
    assert_eq!(reason, "");

    let (reason, allowed) = dispatch_allowed("live", "match", "SETTLED_FAILURE");
    assert!(allowed);
    assert_eq!(reason, "");

    let (reason, allowed) = dispatch_allowed("live", "match", "NONE");
    assert!(allowed);
    assert_eq!(reason, "");

    let (reason, allowed) = dispatch_allowed("expired", "match", "CONFIRMED");
    assert!(!allowed);
    assert_eq!(reason, "LEASE_NOT_HELD");

    let (reason, allowed) = dispatch_allowed("live", "stale", "CONFIRMED");
    assert!(!allowed);
    assert_eq!(reason, "STALE_EXECUTION_EPOCH");

    let (reason, allowed) = dispatch_allowed("live", "mismatch", "CONFIRMED");
    assert!(!allowed);
    assert_eq!(reason, "FENCE_MISMATCH");

    let (reason, allowed) = dispatch_allowed("live", "match", "EFFECT_UNKNOWN");
    assert!(!allowed);
    assert_eq!(reason, "EFFECT_UNKNOWN");

    let (reason, allowed) = dispatch_allowed("live", "match", "");
    assert!(!allowed);
    assert_eq!(reason, "EFFECT_UNKNOWN");

    let (reason, allowed) = dispatch_allowed("live", "match", "AWAITING_TOOLS");
    assert!(!allowed);
    assert_eq!(reason, "AGENT_DRIVER_TOOLS_PENDING");

    let (reason, allowed) = dispatch_allowed("live", "match", "DISPATCHING");
    assert!(!allowed);
    assert_eq!(reason, "EFFECT_UNKNOWN");
}

#[test]
fn test_admit_and_settlement() {
    // The admitted contract takes only resume, rerun, runtime_update and
    // tool_continuation; a request that bumps the runtime revision must name the
    // next revision, and only a resumable status can pass the active gate.
    let empty_nodes = vec![];
    let empty_plan = vec![];
    let adm = RuntimeAdmission {
        operation: "runtime_update",
        status: "done",
        identity_valid: true,
        lease: "live",
        epoch: "match",
        effect: "",
        runtime_revision: 1,
        requested_revision: 2,
        agent_id: "",
        resynthesize: false,
        plan: &empty_plan,
        nodes: &empty_nodes,
        has_final_answer: true,
        same_effect: false,
    };
    let dec = admit(&adm);
    assert!(dec.admitted);
    assert_eq!(dec.http_status, 200);
    assert_eq!(dec.code, "");

    let invalid_op = RuntimeAdmission {
        operation: "unknown_op",
        ..adm
    };
    assert!(!admit(&invalid_op).admitted);

    let invalid_id = RuntimeAdmission {
        identity_valid: false,
        ..adm
    };
    assert!(!admit(&invalid_id).admitted);

    let dead_lease = RuntimeAdmission {
        lease: "dead",
        ..adm
    };
    assert!(!admit(&dead_lease).admitted);

    let stale_epoch = RuntimeAdmission {
        epoch: "stale",
        ..adm
    };
    assert!(!admit(&stale_epoch).admitted);

    // A revision that does not advance, or skips a number, is refused.
    let same_revision = RuntimeAdmission {
        requested_revision: 1,
        ..adm
    };
    assert!(!admit(&same_revision).admitted);

    let skipped_revision = RuntimeAdmission {
        requested_revision: 3,
        ..adm
    };
    assert!(!admit(&skipped_revision).admitted);

    // An active run cannot take a resume-style action.
    let still_running = RuntimeAdmission {
        status: "running",
        ..adm
    };
    assert!(!admit(&still_running).admitted);
}

#[test]
fn test_published_model_answer_and_dependency() {
    // A tool request with pending calls leaves the node waiting, so a saved
    // answer for that same phase is the published continuation.
    let events = vec![json!({
        "type": "agent_tool_request",
        "phase": "task-1",
        "toolCount": 1
    })];
    let saved = json!({
        "type": "agent_output",
        "phase": "task-1",
        "output": {"id": "task-1", "content": "retained answer", "latencyMs": 100}
    });
    let ans = published_model_answer(&events, &saved);
    assert!(ans.published);
    assert_eq!(ans.code, "");

    // A saved envelope that still asks for tools is not a final answer.
    let with_calls = json!({
        "type": "agent_output",
        "phase": "task-1",
        "output": {"id": "task-1", "content": "partial", "latencyMs": 100},
        "toolContinuation": {"toolCalls": [{"id": "call-1", "name": "read_file"}]}
    });
    assert!(!published_model_answer(&events, &with_calls).published);

    // An empty answer body does not publish.
    let blank = json!({
        "type": "agent_output",
        "phase": "task-1",
        "output": {"id": "task-1", "content": "   ", "latencyMs": 100}
    });
    assert!(!published_model_answer(&events, &blank).published);

    // A settled effect is not awaiting tools, so nothing publishes.
    let settled = vec![json!({"type": "agent_output", "phase": "task-1"})];
    assert!(!published_model_answer(&settled, &saved).published);

    // The dependency reader takes a run envelope, not a bare saved event, and
    // needs the run id plus a public agent_output for the dependency node.
    let envelope = json!({
        "runId": "run-1",
        "publicEvent": {
            "type": "agent_output",
            "phase": "task-1",
            "output": {"id": "task-1", "task": "task-1", "content": "done"}
        }
    });
    let dep = published_dependency_output(&envelope, None, "task-1", "task-1", "run-1");
    assert!(dep.ready);
    assert_eq!(dep.code, "");

    // A mismatched run, node or task must not satisfy a dependency.
    for (name, changed) in [
        ("run", json!({"runId": "run-2", "publicEvent": envelope["publicEvent"].clone()})),
        ("phase", json!({"runId": "run-1", "publicEvent": {
            "type": "agent_output", "phase": "task-2",
            "output": {"id": "task-2", "task": "task-1", "content": "done"}}})),
    ] {
        let refused = published_dependency_output(&changed, None, "task-1", "task-1", "run-1");
        assert!(!refused.ready, "{name}");
    }
}
