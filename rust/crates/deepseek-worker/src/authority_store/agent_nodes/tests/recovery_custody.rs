use super::*;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentNodeOutcomeReceipt, AgentRunRecoveryContext, AgentRunRecoveryEffectSource,
    AgentRunRecoveryScope,
};

#[test]
fn rerun_progress_requires_the_original_selected_effect_below_authoritative_go_metadata() {
    use deepseek_protocol::generated::deepseek::agent::v1::{
        AgentRecoveryProgressContext, AgentRunPlanNode, AgentRunRecoveryKind,
        AgentRunRecoverySelection,
    };
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let original = confirmed(&fixture, &mut worker);
    let mut recovery = context(original.clone());
    recovery.selection = Some(AgentRunRecoverySelection {
        kind: AgentRunRecoveryKind::Rerun as i32,
        agent_id: "critic".into(),
        resynthesize: Some(true),
    });
    recovery.execute = vec![AgentRunPlanNode {
        id: "critic".into(),
        task: "review".into(),
        depends_on: vec!["coder".into()],
    }];
    recovery.retained_ids = vec!["coder".into()];
    recovery.retained_outputs = vec![original.clone()];
    recovery.synthesize = true;
    let selected = recovered_output_fixture(&fixture, false);
    worker
        .authority_store
        .as_mut()
        .unwrap()
        .install(
            worker.authority.as_ref().unwrap(),
            selected
                .response
                .claim
                .as_ref()
                .unwrap()
                .fence
                .as_ref()
                .unwrap(),
            &selected.epoch,
        )
        .unwrap();
    let mut source = confirmed(&selected, &mut worker);
    source.settlement_metadata_index = 9;
    let progress = AgentRecoveryProgressContext {
        schema_version: 1,
        run_id: recovery.run_id.clone(),
        generation: recovery.generation,
        metadata_epoch: recovery.metadata_epoch,
        expected_index: 10,
        runtime_revision: recovery.expected_runtime_revision + 1,
        request: source.dispatch.as_ref().unwrap().request.clone(),
        recovery_context_sha256: crate::agent_recovery_context::digest(&recovery).unwrap(),
        task_action_id: source
            .dispatch
            .as_ref()
            .unwrap()
            .fence
            .as_ref()
            .unwrap()
            .action_id
            .clone(),
        task_action_revision: 7,
        task_metadata_index: 10,
        role: "critic".into(),
        synthesize: true,
        outputs: vec![source],
    };
    let store = worker.authority_store.as_ref().unwrap();
    store
        .verify_agent_recovery_progress(&progress, &recovery, &fixture.config)
        .unwrap();
    assert_eq!(recovery.retained_outputs[0], original);
    for change in [
        "schema",
        "generation",
        "epoch",
        "cursor",
        "task-cursor",
        "runtime",
        "digest",
        "request",
        "action",
        "revision",
        "role",
        "synthesize",
        "outputs",
        "node",
        "source-request",
        "settlement",
        "receipt",
        "proof",
    ] {
        let mut bad = progress.clone();
        match change {
            "schema" => bad.schema_version = 0,
            "generation" => bad.generation += 1,
            "epoch" => bad.metadata_epoch = 0,
            "cursor" => bad.expected_index = i64::MAX,
            "task-cursor" => bad.task_metadata_index += 1,
            "runtime" => bad.runtime_revision += 1,
            "digest" => bad.recovery_context_sha256 = "f".repeat(64),
            "request" => bad.request = recovery.expected_request.clone(),
            "action" => bad.task_action_id = "foreign".into(),
            "revision" => bad.task_action_revision = 0,
            "role" => bad.role = "coder".into(),
            "synthesize" => bad.synthesize = false,
            "outputs" => bad.outputs.clear(),
            "node" => bad.outputs[0].dispatch.as_mut().unwrap().node_id = "coder".into(),
            "source-request" => {
                bad.outputs[0].dispatch.as_mut().unwrap().request =
                    recovery.expected_request.clone()
            }
            "settlement" => bad.outputs[0].settlement_metadata_index -= 1,
            "receipt" => bad.outputs[0].outcome = None,
            "proof" => {
                bad.outputs[0]
                    .outcome
                    .as_mut()
                    .unwrap()
                    .evidence
                    .as_mut()
                    .unwrap()
                    .outcome_sha256 = "e".repeat(64)
            }
            _ => unreachable!(),
        }
        assert!(
            store
                .verify_agent_recovery_progress(&bad, &recovery, &fixture.config)
                .is_err(),
            "{change}"
        );
    }
    // A later unresolved node is not proof of an unapplied operation. Metadata
    // progress cannot close the public run over that durable uncertainty.
    let mut unknown = recovered_output_fixture(&fixture, true);
    let mut epoch: Value = serde_json::from_slice(&unknown.epoch).unwrap();
    epoch["requestId"] = format!("{:x}", Sha256::digest(b"progress-unknown-epoch-request")).into();
    epoch["nonce"] = format!("{:x}", Sha256::digest(b"progress-unknown-epoch-nonce")).into();
    unknown.epoch = signed(epoch, b"deepseek-infra:control-authority-request-v1\0");
    let mut grant: Value = serde_json::from_slice(&unknown.grant).unwrap();
    grant["requestId"] = format!("{:x}", Sha256::digest(b"progress-unknown-grant-request")).into();
    grant["nonce"] = format!("{:x}", Sha256::digest(b"progress-unknown-grant-nonce")).into();
    unknown.grant = signed(grant, b"deepseek-infra:control-agent-execution-grant-v1\0");
    worker
        .authority_store
        .as_mut()
        .unwrap()
        .install(
            worker.authority.as_ref().unwrap(),
            unknown
                .response
                .claim
                .as_ref()
                .unwrap()
                .fence
                .as_ref()
                .unwrap(),
            &unknown.epoch,
        )
        .unwrap();
    let _permit = started(unknown.begin(&mut worker, true).unwrap());
    assert!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .verify_agent_recovery_progress(&progress, &recovery, &fixture.config)
            .is_err()
    );
}

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

fn recovered_output_fixture(source: &Fixture, synthesis: bool) -> Fixture {
    let mut next = Fixture::new();
    let phase = if synthesis { 3 } else { 2 };
    let name = if synthesis { "synthesis" } else { "tasks" };
    let claim = next.response.claim.as_mut().unwrap();
    claim.phase = phase;
    claim.metadata_index = 8;
    claim.request.as_mut().unwrap().sha256 = "d".repeat(64);
    claim.fence.as_mut().unwrap().action_id = format!(
        "agent-exec-{:x}",
        Sha256::digest(format!("agent-run-execution-v1\nrun_rpc\n{name}\n8\n").as_bytes())
    );
    let intent = next.response.intent.as_mut().unwrap();
    intent.fence = claim.fence.clone();
    intent.request = claim.request.clone();
    intent.phase = phase;
    intent.metadata_index = 8;
    intent.node_id = if synthesis { "synthesizer" } else { "critic" }.into();
    intent.task = if synthesis {
        "synthesize verified agent outputs"
    } else {
        "review"
    }
    .into();
    intent.depends_on = vec!["coder".into()];
    intent.operation_id = agent_node_effect::node_operation_id(intent).unwrap();
    let mut epoch: Value = serde_json::from_slice(&source.epoch).unwrap();
    epoch["actionId"] = claim.fence.as_ref().unwrap().action_id.clone().into();
    epoch["requestId"] = "5".repeat(64).into();
    epoch["nonce"] = "6".repeat(64).into();
    next.epoch = signed(epoch, b"deepseek-infra:control-authority-request-v1\0");
    let mut grant: Value = serde_json::from_slice(&source.grant).unwrap();
    grant["actionId"] = claim.fence.as_ref().unwrap().action_id.clone().into();
    grant["requestId"] = "7".repeat(64).into();
    grant["nonce"] = "8".repeat(64).into();
    grant["payload"] = crate::agent_execution_grant::claim_payload(claim).unwrap();
    next.grant = signed(grant, b"deepseek-infra:control-agent-execution-grant-v1\0");
    next
}

#[test]
fn recovery_outputs_keep_original_custody_for_new_task_and_synthesis_scopes() {
    for synthesis in [false, true] {
        let fixture = Fixture::new();
        let mut worker = fixture.worker(true);
        let source = confirmed(&fixture, &mut worker);
        let mut context = context(source.clone());
        context.retained_ids = vec!["coder".into()];
        context.retained_outputs = vec![source.clone()];
        context.synthesize = true;
        context.synthesis_output_ids = vec!["coder".into()];
        context.execute = vec![
            deepseek_protocol::generated::deepseek::agent::v1::AgentRunPlanNode {
                id: "critic".into(),
                task: "review".into(),
                depends_on: vec!["coder".into()],
            },
        ];
        let next = recovered_output_fixture(&fixture, synthesis);
        worker
            .authority_store
            .as_mut()
            .unwrap()
            .install(
                worker.authority.as_ref().unwrap(),
                next.response
                    .claim
                    .as_ref()
                    .unwrap()
                    .fence
                    .as_ref()
                    .unwrap(),
                &next.epoch,
            )
            .unwrap();
        let permit = started(next.begin(&mut worker, true).unwrap());
        let store = worker.authority_store.as_ref().unwrap();
        assert!(store.agent_task_prior_outputs(&permit).is_err());
        let outputs = store
            .agent_recovery_task_prior_outputs(&permit, &context, &fixture.config)
            .unwrap();
        assert_eq!(outputs.len(), 1);
        assert_eq!(outputs[0]["id"], "coder");
        assert_eq!(outputs[0]["content"], "settled output");
        assert_eq!(context.retained_outputs[0], source);
        let mut wrong = context.clone();
        wrong.retained_outputs[0]
            .outcome
            .as_mut()
            .unwrap()
            .evidence
            .as_mut()
            .unwrap()
            .request
            .as_mut()
            .unwrap()
            .sha256 = "f".repeat(64);
        assert!(
            store
                .agent_recovery_task_prior_outputs(&permit, &wrong, &fixture.config)
                .is_err()
        );
        wrong = context.clone();
        wrong.complete_immediately = true;
        assert!(
            store
                .agent_recovery_task_prior_outputs(&permit, &wrong, &fixture.config)
                .is_err()
        );
        wrong = context.clone();
        wrong.expected_request = permit.intent.request.clone();
        assert!(
            store
                .agent_recovery_task_prior_outputs(&permit, &wrong, &fixture.config)
                .is_err()
        );
        wrong = context.clone();
        wrong.execute.clear();
        wrong.synthesize = false;
        assert!(
            store
                .agent_recovery_task_prior_outputs(&permit, &wrong, &fixture.config)
                .is_err()
        );
    }
}

#[test]
fn recovery_output_preflight_refuses_incomplete_duplicate_or_foreign_source_sets() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let source = confirmed(&fixture, &mut worker);
    let mut context = context(source.clone());
    context.retained_ids = vec!["coder".into()];
    context.retained_outputs = vec![source];
    let store = worker.authority_store.as_ref().unwrap();
    store
        .verify_agent_recovery_retained_outputs(&context, &fixture.config)
        .unwrap();
    for change in [0, 1, 2, 3, 4] {
        let mut wrong = context.clone();
        match change {
            0 => wrong.retained_ids.clear(),
            1 => wrong.retained_ids[0] = "critic".into(),
            2 => {
                wrong.retained_ids.push("coder".into());
                wrong
                    .retained_outputs
                    .push(wrong.retained_outputs[0].clone());
            }
            3 => wrong.retained_outputs[0].dispatch = None,
            _ => wrong.generation = 0,
        }
        assert!(
            store
                .verify_agent_recovery_retained_outputs(&wrong, &fixture.config)
                .is_err()
        );
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
