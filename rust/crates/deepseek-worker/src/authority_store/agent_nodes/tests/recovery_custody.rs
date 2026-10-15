use super::*;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentNodeOutcomeReceipt, AgentRunRecoveryContext, AgentRunRecoveryEffectSource,
    AgentRunRecoveryScope,
};

fn confirmed(fixture: &Fixture, worker: &mut crate::Worker) -> AgentRunRecoveryEffectSource {
    let permit = started(fixture.begin(worker, true).unwrap());
    let intent = fixture.response.intent.as_ref().unwrap();
    let event = if intent.phase == 1 {
        json!({"type":"agent_plan","plan":{"tasks":[{"role":"coder","task":"implement"}]}})
    } else {
        json!({"type":"agent_output","phase":intent.node_id,
            "output":{"id":intent.node_id,"task":intent.task,"content":"settled output",
                "latencyMs":7,"usage":{"prompt_tokens":1,"completion_tokens":2},"failed":false}})
    };
    let mut artifacts = deepseek_policy::agent_run_data::ArtifactStore::open_native(
        fixture.root.path(),
        "python_disabled",
    )
    .unwrap();
    let output =
        authority_request::canonical_json_bytes(&json!({"runId":"run_rpc","publicEvent":event}))
            .unwrap();
    // Production projects from the canonical retained outcome. serde_json's
    // preserve_order feature makes the earlier unsorted input a different blob.
    let retained: Value = serde_json::from_slice(&output).unwrap();
    let reference = artifacts.put(&retained["publicEvent"]).unwrap();
    let store = worker.authority_store.as_mut().unwrap();
    store.confirm_agent_node(&permit, &output, NOW).unwrap();
    let evidence = store
        .agent_node_outcome_evidence(
            &crate::QueryAgentNodeEffect {
                operation_id: permit.operation_id.clone(),
                fence: permit.fence.clone(),
                run_id: "run_rpc".into(),
                node_id: intent.node_id.clone(),
            },
            AgentArtifactReference {
                sha256: reference.sha256,
                length: reference.length,
            },
            &fixture.config,
        )
        .unwrap();
    AgentRunRecoveryEffectSource {
        dispatch: fixture.response.intent.clone(),
        outcome: Some(AgentNodeOutcomeReceipt {
            schema_version: 1,
            evidence: Some(evidence),
            // This test exercises the independent effect journal. The separate
            // signer test requires the actual, durably retained signature.
            signature: vec![0; 64],
            signer_public_key: fixture.config.signer_public_key.clone(),
        }),
        settlement_metadata_index: 5,
    }
}

fn context(source: AgentRunRecoveryEffectSource) -> AgentRunRecoveryContext {
    AgentRunRecoveryContext {
        schema_version: 1,
        run_id: "run_rpc".into(),
        generation: 1,
        metadata_epoch: 3,
        expected_index: 6,
        expected_runtime_revision: 1,
        expected_request: source.dispatch.as_ref().unwrap().request.clone(),
        prior_effects: vec![source],
        ..Default::default()
    }
}

#[test]
fn recovery_custody_preserves_original_effects_after_worker_restart() {
    let fixture = planner_fixture();
    let mut worker = fixture.worker(true);
    let context = context(confirmed(&fixture, &mut worker));
    worker
        .authority_store
        .as_ref()
        .unwrap()
        .verify_agent_recovery_context(&context, &fixture.config)
        .unwrap();
    drop(worker);
    let worker = fixture.worker(false);
    worker
        .authority_store
        .as_ref()
        .unwrap()
        .verify_agent_recovery_context(&context, &fixture.config)
        .unwrap();
    let mut missing = context.clone();
    missing.prior_effects.clear();
    assert!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .verify_agent_recovery_context(&missing, &fixture.config,)
            .is_err()
    );
}

#[test]
fn recovery_custody_rejects_rebound_or_missing_original_evidence() {
    let fixture = planner_fixture();
    let mut worker = fixture.worker(true);
    let context = context(confirmed(&fixture, &mut worker));
    type Change = fn(&mut AgentRunRecoveryContext);
    let changes: &[(&str, Change)] = &[
        ("run", |c| c.run_id = "foreign".into()),
        ("missing dispatch", |c| c.prior_effects[0].dispatch = None),
        ("missing receipt", |c| c.prior_effects[0].outcome = None),
        ("intent task", |c| {
            c.prior_effects[0].dispatch.as_mut().unwrap().task = "changed".into()
        }),
        ("original request", |c| {
            c.prior_effects[0]
                .outcome
                .as_mut()
                .unwrap()
                .evidence
                .as_mut()
                .unwrap()
                .request
                .as_mut()
                .unwrap()
                .sha256 = "f".repeat(64)
        }),
        ("original epoch", |c| {
            c.prior_effects[0]
                .outcome
                .as_mut()
                .unwrap()
                .evidence
                .as_mut()
                .unwrap()
                .fence
                .as_mut()
                .unwrap()
                .execution_epoch += 1
        }),
        ("output hash", |c| {
            c.prior_effects[0]
                .outcome
                .as_mut()
                .unwrap()
                .evidence
                .as_mut()
                .unwrap()
                .outcome_sha256 = "e".repeat(64)
        }),
        ("signer", |c| {
            c.prior_effects[0]
                .outcome
                .as_mut()
                .unwrap()
                .signer_public_key = "foreign".into()
        }),
        ("empty signature", |c| {
            c.prior_effects[0]
                .outcome
                .as_mut()
                .unwrap()
                .signature
                .clear()
        }),
        ("future settlement", |c| {
            c.prior_effects[0].settlement_metadata_index = 6
        }),
        ("negative settlement", |c| {
            c.prior_effects[0].settlement_metadata_index = -1
        }),
        ("duplicate effect", |c| {
            c.prior_effects.push(c.prior_effects[0].clone())
        }),
        ("unproven retained", |c| {
            c.retained_ids = vec!["coder".into()];
            c.retained_outputs = vec![c.prior_effects[0].clone()];
        }),
    ];
    for (label, change) in changes {
        let mut changed = context.clone();
        change(&mut changed);
        assert!(
            worker
                .authority_store
                .as_ref()
                .unwrap()
                .verify_agent_recovery_context(&changed, &fixture.config,)
                .is_err(),
            "{label}"
        );
    }
}

#[test]
fn recovery_custody_refuses_unsettled_effects_even_before_previous_generation() {
    for unknown in [false, true] {
        let fixture = planner_fixture();
        let mut worker = fixture.worker(true);
        let permit = started(fixture.begin(&mut worker, true).unwrap());
        if unknown {
            worker
                .authority_store
                .as_mut()
                .unwrap()
                .mark_agent_node_unknown(&permit, NOW)
                .unwrap();
        }
        let context = AgentRunRecoveryContext {
            schema_version: 1,
            run_id: "run_rpc".into(),
            generation: 2,
            metadata_epoch: 3,
            expected_index: 10,
            expected_runtime_revision: 2,
            previous_scope: Some(AgentRunRecoveryScope {
                generation: 1,
                start_index: 8,
                context: None,
                context_sha256: "a".repeat(64),
            }),
            ..Default::default()
        };
        assert!(
            worker
                .authority_store
                .as_ref()
                .unwrap()
                .verify_agent_recovery_context(&context, &fixture.config,)
                .is_err()
        );
    }
}

#[test]
fn recovery_custody_generation_bounds_do_not_rebind_older_confirmed_effects() {
    let fixture = planner_fixture();
    let mut worker = fixture.worker(true);
    let source = confirmed(&fixture, &mut worker);
    let mut context = context(source);
    context.generation = 2;
    context.expected_index = 10;
    context.previous_scope = Some(AgentRunRecoveryScope {
        generation: 1,
        start_index: 8,
        context: None,
        context_sha256: "a".repeat(64),
    });
    // The old effect remains in its original generation. It is not a new effect
    // in this scope and cannot be relabelled as one.
    assert!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .verify_agent_recovery_context(&context, &fixture.config,)
            .is_err()
    );
    context.prior_effects.clear();
    worker
        .authority_store
        .as_ref()
        .unwrap()
        .verify_agent_recovery_context(&context, &fixture.config)
        .unwrap();
}

#[test]
fn recovery_custody_retains_only_original_published_task_outputs_across_generations() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let source = confirmed(&fixture, &mut worker);
    let mut context = context(source.clone());
    context.generation = 2;
    context.expected_index = 10;
    context.previous_scope = Some(AgentRunRecoveryScope {
        generation: 1,
        start_index: 8,
        context: None,
        context_sha256: "a".repeat(64),
    });
    context.prior_effects.clear();
    context.retained_ids = vec!["coder".into()];
    context.retained_outputs = vec![source];
    worker
        .authority_store
        .as_ref()
        .unwrap()
        .verify_agent_recovery_context(&context, &fixture.config)
        .unwrap();
    let mut late = context.clone();
    late.retained_outputs[0].settlement_metadata_index = 0;
    assert!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .verify_agent_recovery_context(&late, &fixture.config,)
            .is_err()
    );
    let mut rebound = context.clone();
    rebound.retained_ids[0] = "critic".into();
    assert!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .verify_agent_recovery_context(&rebound, &fixture.config,)
            .is_err()
    );
    let mut duplicate = context.clone();
    duplicate.retained_ids.push("coder".into());
    duplicate
        .retained_outputs
        .push(context.retained_outputs[0].clone());
    assert!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .verify_agent_recovery_context(&duplicate, &fixture.config,)
            .is_err()
    );
}
