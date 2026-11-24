use deepseek_policy::agent_runtime_admission::*;
use serde_json::{Value, json};

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
    let (reason, allowed) = dispatch_allowed("live", "same", "none");
    assert!(allowed);
    assert_eq!(reason, "");

    let (reason, allowed) = dispatch_allowed("expired", "same", "none");
    assert!(!allowed);
    assert_eq!(reason, "lease not live");

    let (reason, allowed) = dispatch_allowed("live", "stale", "none");
    assert!(!allowed);
    assert_eq!(reason, "stale execution epoch");

    let (reason, allowed) = dispatch_allowed("live", "same", "unknown");
    assert!(!allowed);
    assert_eq!(reason, "effect unknown");
}

#[test]
fn test_admit_and_settlement() {
    let empty_nodes = vec![];
    let empty_plan = vec![];
    let adm = RuntimeAdmission {
        operation: "agent_reset",
        status: "running",
        identity_valid: true,
        lease: "live",
        epoch: "same",
        effect: "none",
        runtime_revision: 1,
        requested_revision: 1,
        agent_id: "agent-1",
        resynthesize: false,
        plan: &empty_plan,
        nodes: &empty_nodes,
        has_final_answer: false,
        same_effect: false,
    };
    let dec = admit(&adm);
    assert!(dec.allowed);

    let invalid_op = RuntimeAdmission {
        operation: "unknown_op",
        ..adm
    };
    assert!(!admit(&invalid_op).allowed);

    let invalid_id = RuntimeAdmission {
        identity_valid: false,
        ..adm
    };
    assert!(!admit(&invalid_id).allowed);

    let dead_lease = RuntimeAdmission {
        lease: "dead",
        ..adm
    };
    assert!(!admit(&dead_lease).allowed);

    let stale_epoch = RuntimeAdmission {
        epoch: "stale",
        ..adm
    };
    assert!(!admit(&stale_epoch).allowed);
}

#[test]
fn test_published_model_answer_and_dependency() {
    let events = vec![json!({
        "type": "agent_tool_request",
        "phase": "task-1",
        "toolCount": 1
    })];
    let saved = json!({
        "type": "agent_output",
        "output": {"id": "task-1", "task": "task-1", "latencyMs": 100}
    });
    let ans = published_model_answer(&events, &saved);
    assert!(ans.published);
    assert_eq!(ans.code, "");

    let dep = published_dependency_output(&saved, None, "task-1", "task-1", "run-1");
    assert!(dep.ready);
    assert_eq!(dep.code, "");
}
