//! Successor generation, binding and execution, plus retained tool continuation.
//!
//! These paths decide whether a settled effect may be turned into a second
//! provider call. Every refusal branch is pinned here: an accidental admission
//! would dispatch a duplicate upstream effect, which is the exact failure the
//! one_table/one-writer and unknown-remote-effect invariants forbid.

use deepseek_policy::agent_runtime_admission::*;
use serde_json::json;

fn base_input() -> SuccessorInput<'static> {
    SuccessorInput {
        identity_valid: true,
        lease: "live",
        epoch: "match",
        effect: "CONFIRMED",
        reset_admitted: true,
        reset_fence_matches: true,
        operation_id: "agent-node-original",
        action_id: "run-1",
        execution_epoch: 3,
        node_id: "coder",
        reset_index: 7,
        existing_successor: "",
    }
}

#[test]
fn successor_generation_admits_one_settled_same_fence_reset() {
    let input = base_input();
    let decision = successor_generation(&input);
    assert!(decision.admitted);
    assert_eq!(decision.dispatch, "successor");
    assert!(!decision.replayed);
    assert_eq!(decision.retained_operation_id, "agent-node-original");
    let want = successor_operation_id("agent-node-original", "run-1", 3, "coder", 7);
    assert_eq!(decision.successor_operation_id, want);
    assert!(want.starts_with("agent-successor-"));
    assert_eq!(want.len(), "agent-successor-".len() + 64);
    assert!(
        want["agent-successor-".len()..]
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    );

    // Replaying the identical successor is admitted and reported as a replay.
    let replay = SuccessorInput {
        existing_successor: &want,
        ..base_input()
    };
    let again = successor_generation(&replay);
    assert!(again.admitted);
    assert!(again.replayed);

    // A different stored successor is a generation conflict, not a replay.
    let conflicting = SuccessorInput {
        existing_successor: "agent-successor-ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        ..base_input()
    };
    let refused = successor_generation(&conflicting);
    assert!(!refused.admitted);
    assert_eq!(refused.code, "AGENT_RUNTIME_GENERATION_CONFLICT");
    assert!(refused.successor_operation_id.is_empty());
    assert_eq!(refused.dispatch, "none");
}

#[test]
fn successor_generation_refuses_every_illegal_precondition() {
    let cases: [(&str, &str, SuccessorInput<'static>); 11] = [
        (
            "identity",
            "INVALID_IDENTITY",
            SuccessorInput {
                identity_valid: false,
                ..base_input()
            },
        ),
        (
            "lease",
            "LEASE_NOT_HELD",
            SuccessorInput {
                lease: "expired",
                ..base_input()
            },
        ),
        (
            "stale-epoch",
            "STALE_EXECUTION_EPOCH",
            SuccessorInput {
                epoch: "stale",
                ..base_input()
            },
        ),
        (
            "mismatch-epoch",
            "FENCE_MISMATCH",
            SuccessorInput {
                epoch: "mismatch",
                ..base_input()
            },
        ),
        (
            "awaiting-tools",
            "AGENT_DRIVER_TOOLS_PENDING",
            SuccessorInput {
                effect: "AWAITING_TOOLS",
                ..base_input()
            },
        ),
        (
            "unknown-effect",
            "EFFECT_UNKNOWN",
            SuccessorInput {
                effect: "EFFECT_UNKNOWN",
                ..base_input()
            },
        ),
        (
            "empty-effect",
            "EFFECT_UNKNOWN",
            SuccessorInput {
                effect: "",
                ..base_input()
            },
        ),
        (
            "reset-not-admitted",
            "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
            SuccessorInput {
                reset_admitted: false,
                ..base_input()
            },
        ),
        (
            "foreign-fence",
            "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
            SuccessorInput {
                reset_fence_matches: false,
                ..base_input()
            },
        ),
        (
            "zero-epoch",
            "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
            SuccessorInput {
                execution_epoch: 0,
                ..base_input()
            },
        ),
        (
            "empty-operation",
            "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
            SuccessorInput {
                operation_id: "",
                ..base_input()
            },
        ),
    ];
    for (name, code, input) in cases {
        let decision = successor_generation(&input);
        assert!(!decision.admitted, "{name}");
        assert_eq!(decision.code, code, "{name}");
        assert!(!decision.message.is_empty(), "{name}");
        assert_eq!(decision.dispatch, "none", "{name}");
        assert!(decision.successor_operation_id.is_empty(), "{name}");
        assert_eq!(decision.retained_operation_id, input.operation_id, "{name}");
    }
}

#[test]
fn successor_generation_refuses_a_newline_in_any_identity_token() {
    for (name, input) in [
        (
            "operation",
            SuccessorInput {
                operation_id: "bad\nid",
                ..base_input()
            },
        ),
        (
            "action",
            SuccessorInput {
                action_id: "bad\rid",
                ..base_input()
            },
        ),
        (
            "node",
            SuccessorInput {
                node_id: "bad\nnode",
                ..base_input()
            },
        ),
        (
            "long-node",
            SuccessorInput {
                node_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ..base_input()
            },
        ),
    ] {
        let decision = successor_generation(&input);
        assert!(!decision.admitted, "{name}");
        assert_eq!(
            decision.code, "AGENT_RUNTIME_RESET_REQUIRES_GENERATION",
            "{name}"
        );
    }
}

#[test]
fn successor_operation_id_is_stable_and_distinguishes_its_material() {
    let a = successor_operation_id("op", "run-1", 3, "coder", 7);
    let same = successor_operation_id("op", "run-1", 3, "coder", 7);
    assert_eq!(a, same, "the same material must mint the same id");

    let variants = [
        successor_operation_id("op2", "run-1", 3, "coder", 7),
        successor_operation_id("op", "run-2", 3, "coder", 7),
        successor_operation_id("op", "run-1", 4, "coder", 7),
        successor_operation_id("op", "run-1", 3, "critic", 7),
        successor_operation_id("op", "run-1", 3, "coder", 8),
    ];
    for (index, other) in variants.iter().enumerate() {
        assert_ne!(&a, other, "variant {index} collided");
    }
    // The minted id never equals the operation it replaces.
    assert_ne!(a, "op");
}

#[test]
fn successor_binding_persists_beside_the_original_and_never_replaces_it() {
    let input = base_input();
    let bound = successor_binding(&input);
    assert!(bound.persisted);
    assert!(!bound.replaced_operation_id);
    assert_eq!(bound.operation_id, "agent-node-original");
    assert_eq!(bound.reset_index, 7);
    assert!(!bound.replayed);
    assert_eq!(
        bound.successor_operation_id,
        successor_operation_id("agent-node-original", "run-1", 3, "coder", 7)
    );

    // A refusal keeps whatever successor was already stored and persists nothing.
    let refused = successor_binding(&SuccessorInput {
        effect: "EFFECT_UNKNOWN",
        existing_successor: "agent-successor-existing",
        ..base_input()
    });
    assert!(!refused.persisted);
    assert!(!refused.replaced_operation_id);
    assert_eq!(refused.operation_id, "agent-node-original");
    assert_eq!(refused.successor_operation_id, "agent-successor-existing");
    assert_eq!(refused.code, "EFFECT_UNKNOWN");
}

fn execution_input(successor: &'static str) -> SuccessorExecutionInput<'static> {
    SuccessorExecutionInput {
        identity_valid: true,
        lease: "live",
        epoch: "match",
        effect: "CONFIRMED",
        reset_admitted: true,
        reset_fence_matches: true,
        operation_id: "agent-node-original",
        action_id: "run-1",
        execution_epoch: 3,
        node_id: "coder",
        reset_index: 7,
        stored_successor: successor,
        stored_operation_id: "agent-node-original",
        authority_issued: true,
        worker_promoted: false,
        authority_epoch: 5,
        presented_epoch: 5,
    }
}

#[test]
fn successor_execution_runs_only_the_persisted_id_on_an_issued_new_epoch() {
    let successor: &'static str = Box::leak(
        successor_operation_id("agent-node-original", "run-1", 3, "coder", 7).into_boxed_str(),
    );
    let decision = successor_execution(&execution_input(successor));
    assert!(decision.admitted);
    assert!(decision.execute);
    assert!(!decision.promoted);
    assert_eq!(decision.operation_id, successor);
    assert_eq!(decision.execution_epoch, 5);
    assert_eq!(decision.retained_operation_id, "agent-node-original");
    assert_eq!(decision.retained_epoch, 3);
}

#[test]
fn successor_execution_refuses_every_illegal_precondition() {
    let successor = successor_operation_id("agent-node-original", "run-1", 3, "coder", 7);
    let leaked: &'static str = Box::leak(successor.clone().into_boxed_str());

    let cases: Vec<(&str, SuccessorExecutionInput<'static>)> = vec![
        (
            "worker-promoted",
            SuccessorExecutionInput {
                worker_promoted: true,
                ..execution_input(leaked)
            },
        ),
        (
            "identity",
            SuccessorExecutionInput {
                identity_valid: false,
                ..execution_input(leaked)
            },
        ),
        (
            "lease",
            SuccessorExecutionInput {
                lease: "expired",
                ..execution_input(leaked)
            },
        ),
        (
            "stale",
            SuccessorExecutionInput {
                epoch: "stale",
                ..execution_input(leaked)
            },
        ),
        (
            "presented-older",
            SuccessorExecutionInput {
                presented_epoch: 4,
                ..execution_input(leaked)
            },
        ),
        (
            "mismatch",
            SuccessorExecutionInput {
                epoch: "mismatch",
                ..execution_input(leaked)
            },
        ),
        (
            "no-authority",
            SuccessorExecutionInput {
                authority_epoch: 0,
                presented_epoch: 0,
                ..execution_input(leaked)
            },
        ),
        (
            "presented-newer",
            SuccessorExecutionInput {
                presented_epoch: 6,
                ..execution_input(leaked)
            },
        ),
        (
            "awaiting-tools",
            SuccessorExecutionInput {
                effect: "AWAITING_TOOLS",
                ..execution_input(leaked)
            },
        ),
        (
            "unknown-effect",
            SuccessorExecutionInput {
                effect: "EFFECT_UNKNOWN",
                ..execution_input(leaked)
            },
        ),
        (
            "reset-not-admitted",
            SuccessorExecutionInput {
                reset_admitted: false,
                ..execution_input(leaked)
            },
        ),
        (
            "foreign-fence",
            SuccessorExecutionInput {
                reset_fence_matches: false,
                ..execution_input(leaked)
            },
        ),
        (
            "not-issued",
            SuccessorExecutionInput {
                authority_issued: false,
                ..execution_input(leaked)
            },
        ),
        (
            "empty-operation",
            SuccessorExecutionInput {
                operation_id: "",
                ..execution_input(leaked)
            },
        ),
        (
            "foreign-original",
            SuccessorExecutionInput {
                stored_operation_id: "someone-else",
                ..execution_input(leaked)
            },
        ),
        (
            "no-successor",
            SuccessorExecutionInput {
                stored_successor: "",
                ..execution_input(leaked)
            },
        ),
        (
            "successor-is-original",
            SuccessorExecutionInput {
                stored_successor: "agent-node-original",
                ..execution_input(leaked)
            },
        ),
        (
            "same-epoch",
            SuccessorExecutionInput {
                authority_epoch: 3,
                presented_epoch: 3,
                ..execution_input(leaked)
            },
        ),
    ];
    for (name, input) in cases {
        let decision = successor_execution(&input);
        assert!(!decision.admitted, "{name}");
        assert!(!decision.execute, "{name}");
        assert!(decision.operation_id.is_empty(), "{name}");
        assert!(!decision.code.is_empty(), "{name}");
        assert!(!decision.message.is_empty(), "{name}");
    }
}

#[test]
fn successor_execution_holds_while_the_fence_is_unchanged() {
    let successor: &'static str = Box::leak(
        successor_operation_id("agent-node-original", "run-1", 3, "coder", 7).into_boxed_str(),
    );
    let decision = successor_execution(&execution_input(successor));
    assert!(decision.execute);

    let held = successor_execution(&SuccessorExecutionInput {
        authority_epoch: 3,
        presented_epoch: 3,
        ..execution_input(successor)
    });
    assert!(!held.execute);
    assert_eq!(held.code, "AGENT_RUNTIME_SUCCESSOR_HELD");
}

#[test]
fn provider_send_and_follow_up_guards_stay_closed() {
    let lease = execution_send_lease(100, 50);
    assert!(lease.allowed);
    assert!(lease.code.is_empty());
    assert!(!lease.mark_unknown);
    assert!(!lease.provider_effect);

    for (deadline, now, name) in [
        (0_i64, 9_i64, "no-deadline"),
        (9, 0, "no-clock"),
        (50, 50, "expired"),
        (40, 90, "long-past"),
    ] {
        let denied = execution_send_lease(deadline, now);
        assert!(!denied.allowed, "{name}");
        assert_eq!(denied.code, "AGENT_EXECUTION_LEASE_DENIED", "{name}");
        assert!(denied.mark_unknown, "{name}");
        assert!(!denied.provider_effect, "{name}");
        // A denied lease can never be upgraded into a send.
        assert!(!admitted_provider_send(denied).allowed, "{name}");
    }

    assert!(!provider_exchange_unknown(true).mark_unknown);
    assert!(provider_exchange_unknown(true).code.is_empty());
    let unknown = provider_exchange_unknown(false);
    assert!(unknown.mark_unknown);
    assert_eq!(unknown.code, "AGENT_PROVIDER_RESULT_UNKNOWN");

    let follow = follow_up_exchange(true, true);
    assert!(follow.store);
    assert!(follow.code.is_empty());
    assert!(!follow.mark_unknown);

    let no_receipt = follow_up_exchange(true, false);
    assert!(!no_receipt.store);
    assert_eq!(no_receipt.code, "AGENT_PROVIDER_RESULT_UNKNOWN");
    assert!(!no_receipt.mark_unknown);

    let no_lease = follow_up_exchange(false, true);
    assert!(!no_lease.store);
    assert_eq!(no_lease.code, "AGENT_EXECUTION_LEASE_DENIED");
    assert!(!no_lease.mark_unknown);
}

#[test]
fn send_budget_and_deadline_never_exceed_the_earliest_bound() {
    assert_eq!(agent_node_send_deadline(100, 90, 80), 80);
    assert_eq!(agent_node_send_deadline(70, 90, 80), 70);
    assert_eq!(agent_node_send_deadline(100, 60, 80), 60);

    let armed = execution_send_budget(100, 50, 30);
    assert!(armed.armed);
    assert_eq!(armed.seconds, 30);

    let capped = execution_send_budget(100, 50, 60);
    assert!(capped.armed);
    assert_eq!(capped.seconds, 50, "the cap is the remaining lease");

    let denied = execution_send_budget(50, 100, 30);
    assert!(!denied.armed);
    assert_eq!(denied.code, "AGENT_EXECUTION_LEASE_DENIED");
}

#[test]
fn vault_revision_admits_a_replay_or_exactly_the_next_revision() {
    assert!(vault_revision_allowed(None, 1));
    assert!(vault_revision_allowed(Some(1), 2));
    assert!(vault_revision_allowed(Some(4), 5));
    // A same-revision rehearsal is legal: it re-seals the identical request and
    // does not advance the run.
    assert!(vault_revision_allowed(Some(2), 2));
    assert!(
        !vault_revision_allowed(Some(2), 1),
        "a regression is refused"
    );
    assert!(!vault_revision_allowed(Some(2), 4), "a gap is refused");
    assert!(!vault_revision_allowed(None, 0));
}

#[test]
fn journal_documents_are_derived_from_one_shared_template() {
    let (digest, length) = node_effect_digest("coder", "CONFIRMED", "").unwrap();
    assert_eq!(digest.len(), 64);
    assert!(length > 0);
    let value = node_effect_value("coder", "CONFIRMED", "").unwrap();
    assert_eq!(value["type"], "node_effect");
    assert_eq!(value["phase"], "coder");
    // A state document carries both status and state; it has no scope.
    assert_eq!(value["state"], "CONFIRMED");
    assert_eq!(value["status"], "CONFIRMED");
    assert!(value.get("scope").is_none());

    let observation = node_effect_value("coder", "", "timeout").unwrap();
    assert_eq!(observation["observation"], "timeout");
    assert_eq!(observation["scope"], "timeout");
    assert!(observation.get("status").is_none());
    assert!(observation.get("state").is_none());

    // Unsafe tokens are refused rather than escaped into the document.
    for (phase, status, scope) in [
        ("bad\u{0}phase", "CONFIRMED", ""),
        ("coder", "bad status", ""),
        ("coder", "CONFIRMED", "bad\nscope"),
    ] {
        assert!(node_effect_bytes(phase, status, scope).is_none());
        assert!(node_effect_digest(phase, status, scope).is_err());
        assert!(node_effect_value(phase, status, scope).is_err());
    }

    // A safe but unknown scope is treated as a state, never an observation.
    let unknown_state = node_effect_value("coder", "DISPATCHING", "").unwrap();
    assert_eq!(unknown_state["state"], "DISPATCHING");
}

#[test]
fn retained_effect_classifies_the_stream_once() {
    let awaiting = vec![json!({"type":"agent_tool_request","phase":"coder","toolCount":2})];
    let waiting = retained_effect(&awaiting);
    assert_eq!(waiting.effect, "awaiting_tools");
    assert!(
        waiting.same_effect,
        "a tool wait stays on the original effect"
    );

    // EFFECT_UNKNOWN, NOT_APPLIED and DISPATCHING are unresolved: they stay
    // sticky and never become a settled "none" that could be re-dispatched.
    for state in ["EFFECT_UNKNOWN", "NOT_APPLIED", "DISPATCHING", ""] {
        let events = vec![json!({"type":"node_effect","phase":"coder","state":state})];
        assert_eq!(retained_effect(&events).effect, "effect_unknown", "{state}");
    }

    let settled = vec![json!({"type":"agent_output","phase":"coder"})];
    let done = retained_effect(&settled);
    assert_eq!(done.effect, "none");
    assert!(!done.same_effect);

    // CONFIRMED is settled, not unknown.
    let confirmed = vec![json!({"type":"node_effect","phase":"coder","state":"CONFIRMED"})];
    assert_eq!(retained_effect(&confirmed).effect, "none");

    // A non-object entry cannot be classified and is sticky, never settled.
    let malformed = vec![json!("not an object")];
    assert_eq!(retained_effect(&malformed).effect, "effect_unknown");
}

#[test]
fn runtime_revision_must_advance_without_a_gap() {
    assert_eq!(current_runtime_revision(&[]), Ok(1));
    assert_eq!(
        current_runtime_revision(&[json!({"type":"runtime_payload","runtimeRevision":2})]),
        Ok(2)
    );
    assert_eq!(
        current_runtime_revision(&[
            json!({"type":"runtime_payload","runtimeRevision":2}),
            json!({"type":"runtime_payload","runtimeRevision":3}),
        ]),
        Ok(3)
    );
    // A gap is refused so a lost revision can never be replayed over.
    assert!(
        current_runtime_revision(&[json!({"type":"runtime_payload","runtimeRevision":3})]).is_err()
    );
    // A repeat, a regression and a missing field are all corrupt.
    assert!(
        current_runtime_revision(&[
            json!({"type":"runtime_payload","runtimeRevision":2}),
            json!({"type":"runtime_payload","runtimeRevision":2}),
        ])
        .is_err()
    );
    assert!(
        current_runtime_revision(&[
            json!({"type":"runtime_payload","runtimeRevision":2}),
            json!({"type":"runtime_payload"}),
        ])
        .is_err()
    );
    // Non-runtime events never move the revision.
    assert_eq!(
        current_runtime_revision(&[json!({"type":"run_status","status":"running"})]),
        Ok(1)
    );
}
