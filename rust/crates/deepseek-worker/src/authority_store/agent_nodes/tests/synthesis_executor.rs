use super::*;

fn synthesis_fixture(source: &Fixture) -> Fixture {
    let mut next = Fixture::new();
    next.root = tempfile::tempdir().unwrap();
    let claim = next.response.claim.as_mut().unwrap();
    claim.phase = 3;
    claim.metadata_index = 6;
    claim.fence.as_mut().unwrap().action_id = format!(
        "agent-exec-{:x}",
        Sha256::digest(b"agent-run-execution-v1\nrun_rpc\nsynthesis\n6\n")
    );
    let intent = next.response.intent.as_mut().unwrap();
    intent.fence = claim.fence.clone();
    intent.phase = 3;
    intent.metadata_index = 6;
    intent.node_id = "synthesizer".into();
    intent.task = "synthesize verified agent outputs".into();
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

fn complete_coder(fixture: &Fixture, worker: &mut crate::Worker) -> AgentNodeEffectOutcome {
    let permit = started(fixture.begin(worker, true).unwrap());
    let intent = fixture.response.intent.as_ref().unwrap();
    let envelope = json!({"runId":intent.run_id,"publicEvent":{"type":"agent_output","phase":"coder",
        "output":{"id":"coder","task":intent.task,"content":"verified original output","usage":{},"latencyMs":1}},
        "providerResponseSha256":"e".repeat(64)});
    worker
        .authority_store
        .as_mut()
        .unwrap()
        .confirm_agent_node(
            &permit,
            &authority_request::canonical_json_bytes(&envelope).unwrap(),
            NOW,
        )
        .unwrap()
}

#[test]
fn synthesis_uses_confirmed_bodies_attests_content_and_replays_after_restart() {
    let fixture = Fixture::new();
    let synthesis = synthesis_fixture(&fixture);
    let mut worker = fixture.worker(true);
    worker
        .authority_store
        .as_mut()
        .unwrap()
        .install(
            worker.authority.as_ref().unwrap(),
            synthesis
                .response
                .claim
                .as_ref()
                .unwrap()
                .fence
                .as_ref()
                .unwrap(),
            &synthesis.epoch,
        )
        .unwrap();
    let permit = started(synthesis.begin(&mut worker, true).unwrap());
    assert!(
        worker
            .authority_store
            .as_ref()
            .unwrap()
            .agent_task_prior_outputs(&permit)
            .is_err()
    );
    complete_coder(&fixture, &mut worker);
    let outputs = worker
        .authority_store
        .as_ref()
        .unwrap()
        .agent_task_prior_outputs(&permit)
        .unwrap();
    assert_eq!(outputs.len(), 1);
    assert_eq!(outputs[0]["content"], "verified original output");
    let payload = deepseek_policy::multi_agent::synthesis_payload(
        &json!({"messages":[{"role":"user","content":"original question"}]}),
        "deepseek-v4-pro",
        "original question",
        &outputs,
    );
    assert!(payload.to_string().contains("verified original output"));
    assert_eq!(payload["toolsEnabled"], false);
    let envelope = json!({"runId":"run_rpc","publicEvent":{"type":"content","text":"confirmed final answer"},
        "synthesisOutput":{"usage":{"prompt_tokens":10,"completion_tokens":20},"latencyMs":30},"providerResponseSha256":"e".repeat(64)});
    let bytes = authority_request::canonical_json_bytes(&envelope).unwrap();
    let known = worker
        .authority_store
        .as_mut()
        .unwrap()
        .confirm_agent_node(&permit, &bytes, NOW)
        .unwrap();
    let event: Value =
        serde_json::from_slice::<Value>(&known.output).unwrap()["publicEvent"].clone();
    let event_bytes = serde_json::to_vec(&event).unwrap();
    let query = crate::QueryAgentNodeEffect {
        operation_id: known.operation_id.clone(),
        fence: known.fence.clone(),
        run_id: "run_rpc".into(),
        node_id: "synthesizer".into(),
    };
    let evidence = worker
        .authority_store
        .as_ref()
        .unwrap()
        .agent_node_outcome_evidence(
            &query,
            AgentArtifactReference {
                sha256: agent_node_effect::digest(&event_bytes),
                length: event_bytes.len() as u64,
            },
            &fixture.config,
        )
        .unwrap();
    assert_eq!(evidence.phase, 3);
    assert!(evidence.provider_plan.is_empty());
    let counters = evidence.task_output.unwrap();
    assert_eq!(
        (
            counters.prompt_tokens,
            counters.completion_tokens,
            counters.latency_ms
        ),
        (10, 20, 30)
    );
    drop(worker);
    let mut reopened = fixture.worker(false);
    let replay = match synthesis.begin(&mut reopened, false).unwrap() {
        AgentNodeEffectStart::Confirmed(value) => value,
        _ => panic!("confirmed synthesis redispatched"),
    };
    assert_eq!(replay.output, known.output);
    assert_eq!(replay.operation_id, known.operation_id);
    assert!(
        reopened
            .authority_store
            .as_ref()
            .unwrap()
            .agent_task_prior_outputs(&permit)
            .is_err()
    );
}

fn restore_v5_schema(store: &mut AuthorityStore) {
    let transaction = store.connection.transaction().unwrap();
    transaction.execute_batch("CREATE TEMP TABLE saved_agent_nodes AS SELECT rowid AS original_rowid,* FROM agent_node_effects; DROP TABLE agent_node_effects;").unwrap();
    transaction.execute_batch(SCHEMA_V5[0]).unwrap();
    transaction.execute_batch("INSERT INTO agent_node_effects(rowid,operation_id,action_id,epoch,fencing_token,run_id,phase,metadata_index,node_id,claim,intent,grant,claim_scope_digest,intent_digest,grant_digest,state,output,output_sha256,created_at,updated_at) SELECT * FROM saved_agent_nodes; DROP TABLE saved_agent_nodes;").unwrap();
    for sql in &SCHEMA_V5[1..] {
        transaction.execute_batch(sql).unwrap();
    }
    transaction.pragma_update(None, "user_version", 5).unwrap();
    transaction.commit().unwrap();
}

#[test]
fn v5_migration_preserves_original_rows_and_rolls_back_uncommitted_changes() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let known = complete_coder(&fixture, &mut worker);
    let store = worker.authority_store.as_mut().unwrap();
    restore_v5_schema(store);
    assert_eq!(
        super::super::super::validate_schema(&store.connection, false).unwrap(),
        5
    );
    let before: (i64, Vec<u8>, Vec<u8>, Vec<u8>, Vec<u8>) = store
        .connection
        .query_row(
            "SELECT rowid,claim,intent,grant,output FROM agent_node_effects WHERE operation_id=?1",
            [&known.operation_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ))
            },
        )
        .unwrap();
    {
        let tx = store.connection.transaction().unwrap();
        migrate_v6(&tx).unwrap();
    }
    assert_eq!(
        super::super::super::validate_schema(&store.connection, false).unwrap(),
        5
    );
    drop(worker);
    let reopened = fixture.worker(false);
    let store = reopened.authority_store.as_ref().unwrap();
    assert_eq!(
        super::super::super::validate_schema(&store.connection, false).unwrap(),
        6
    );
    let after: (i64, Vec<u8>, Vec<u8>, Vec<u8>, Vec<u8>) = store
        .connection
        .query_row(
            "SELECT rowid,claim,intent,grant,output FROM agent_node_effects WHERE operation_id=?1",
            [&known.operation_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ))
            },
        )
        .unwrap();
    assert_eq!(before, after);
    assert!(
        store
            .connection
            .execute("DELETE FROM agent_node_effects", [])
            .is_err()
    );
    assert!(
        store
            .connection
            .execute(
                "UPDATE agent_node_effects SET output=?1 WHERE operation_id=?2",
                params![vec![1_u8], known.operation_id]
            )
            .is_err()
    );
}
