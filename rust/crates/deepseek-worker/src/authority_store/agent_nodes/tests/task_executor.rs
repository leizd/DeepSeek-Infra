use super::*;

fn task_result(fixture: &Fixture) -> Value {
    let intent = fixture.response.intent.as_ref().unwrap();
    json!({"runId":intent.run_id,"publicEvent":{"type":"agent_output","phase":intent.node_id,
        "output":{"id":intent.node_id,"task":intent.task,"content":"public summary"}},
        "providerResponseSha256":"e".repeat(64)})
}

fn dependent_response(fixture: &Fixture) -> BeginAgentNodeDispatchResponse {
    let mut response = fixture.response.clone();
    let intent = response.intent.as_mut().unwrap();
    intent.node_id = "critic".into();
    intent.task = "Review the predecessor".into();
    intent.depends_on = vec!["coder".into()];
    intent.operation_id = agent_node_effect::node_operation_id(intent).unwrap();
    response
}

#[test]
fn task_outcome_custody_attests_only_the_admitted_complete_public_projection() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let store = worker.authority_store.as_mut().unwrap();
    let envelope = task_result(&fixture);
    let bytes = authority_request::canonical_json_bytes(&envelope).unwrap();
    let known = store.confirm_agent_node(&permit, &bytes, NOW).unwrap();
    let event = deepseek_policy::agent_run_data::sanitize(&envelope["publicEvent"]);
    let public = serde_json::to_vec(&event).unwrap();
    let reference = AgentArtifactReference {
        sha256: agent_node_effect::digest(&public),
        length: public.len() as u64,
    };
    let query = crate::QueryAgentNodeEffect {
        operation_id: known.operation_id,
        fence: known.fence,
        run_id: "run_rpc".into(),
        node_id: "coder".into(),
    };
    let evidence = store
        .agent_node_outcome_evidence(&query, reference.clone(), &fixture.config)
        .unwrap();
    assert_eq!(evidence.phase, 2);
    assert!(evidence.provider_plan.is_empty());
    let mut wrong = reference;
    wrong.length += 1;
    assert!(
        store
            .agent_node_outcome_evidence(&query, wrong, &fixture.config)
            .is_err()
    );
}

#[test]
fn dependency_bodies_come_from_confirmed_same_plan_journals() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let coder = started(fixture.begin(&mut worker, true).unwrap());
    let store = worker.authority_store.as_mut().unwrap();
    let critic = started(
        store
            .begin_agent_node(
                &dependent_response(&fixture),
                &fixture.grant,
                &fixture.config,
                NOW,
            )
            .unwrap(),
    );
    assert!(store.agent_task_prior_outputs(&critic).is_err());
    let bytes = authority_request::canonical_json_bytes(&task_result(&fixture)).unwrap();
    store.confirm_agent_node(&coder, &bytes, NOW).unwrap();
    let output = store.agent_task_prior_outputs(&critic).unwrap();
    assert_eq!(output.len(), 1);
    assert_eq!(output[0]["id"], "coder");
    assert_eq!(output[0]["content"], "public summary");
    let mut rebound = critic;
    rebound.intent.task = "injected task".into();
    assert!(store.agent_task_prior_outputs(&rebound).is_err());
    assert!(
        store
            .agent_node_send_deadline(&rebound, &fixture.config, NOW)
            .is_err()
    );
    assert!(store.confirm_agent_node(&rebound, &bytes, NOW).is_err());
}

#[test]
fn retained_tool_request_cannot_attest_completion_or_satisfy_dependencies() {
    let fixture = Fixture::new();
    let mut worker = fixture.worker(true);
    let permit = started(fixture.begin(&mut worker, true).unwrap());
    let store = worker.authority_store.as_mut().unwrap();
    let envelope = json!({"runId":"run_rpc","publicEvent":{"type":"agent_tool_request","phase":"coder","toolCount":1},
        "toolContinuation":{"assistant":{"reasoning_content":"private"},"toolCalls":[{"id":"call"}]}});
    let bytes = authority_request::canonical_json_bytes(&envelope).unwrap();
    let known = store.confirm_agent_node(&permit, &bytes, NOW).unwrap();
    let query = crate::QueryAgentNodeEffect {
        operation_id: known.operation_id,
        fence: known.fence,
        run_id: "run_rpc".into(),
        node_id: "coder".into(),
    };
    let event = serde_json::to_vec(&envelope["publicEvent"]).unwrap();
    assert!(
        store
            .agent_node_outcome_evidence(
                &query,
                AgentArtifactReference {
                    sha256: agent_node_effect::digest(&event),
                    length: event.len() as u64
                },
                &fixture.config
            )
            .is_err()
    );
    let critic = started(
        store
            .begin_agent_node(
                &dependent_response(&fixture),
                &fixture.grant,
                &fixture.config,
                NOW,
            )
            .unwrap(),
    );
    assert!(store.agent_task_prior_outputs(&critic).is_err());
    assert!(matches!(
        fixture.begin(&mut worker, false).unwrap(),
        AgentNodeEffectStart::Confirmed(_)
    ));
}
