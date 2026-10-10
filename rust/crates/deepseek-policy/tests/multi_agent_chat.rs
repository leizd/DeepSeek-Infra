use deepseek_policy::multi_agent as policy;
use serde_json::{Value, json};

#[test]
fn retained_pure_multi_agent_behaviour_matches_the_offline_reference() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/multi_agent_chat.json")).unwrap();
    let mut differences = Vec::new();
    for (index, case) in fixture["cases"].as_array().unwrap().iter().enumerate() {
        let string = |key: &str| case[key].as_str().unwrap();
        let outputs = || case["outputs"].as_array().unwrap().as_slice();
        let actual = match string("op") {
            "plan" => policy::parse_plan_response(string("content")),
            "structured" => policy::structured_output(string("content")),
            "base" => policy::base_payload(&case["payload"]),
            "prior" => json!(policy::prior_context(outputs())),
            "worker" => policy::worker_payload(
                &case["payload"],
                string("role"),
                string("task"),
                outputs(),
                string("model"),
            )
            .unwrap(),
            "synthesis" => policy::synthesis_payload(
                &case["payload"],
                string("model"),
                string("query"),
                outputs(),
            ),
            "output" => policy::worker_output(
                string("role"),
                string("task"),
                string("content"),
                case["usage"].clone(),
                &case["search"],
            ),
            "critic" => json!(policy::critic_target(&case["output"])),
            "failed" => policy::failed_output(string("role"), string("task"), string("error")),
            "cache" => policy::cache_usage(&case["usage"]),
            "agent_cache" => policy::agent_cache(outputs(), &case["usage"]),
            unknown => panic!("unknown oracle operation {unknown}"),
        };
        if actual != case["expected"] {
            differences.push(format!("reference case {index} ({})", string("op")));
        }
    }
    assert!(
        differences.is_empty(),
        "{} differences: {}",
        differences.len(),
        differences.join(", ")
    );
}
