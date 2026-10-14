use deepseek_policy::multi_agent::preset_plan;
use serde_json::Value;

#[test]
fn retained_preset_selection_and_unicode_auto_signals_match_offline_python() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/agent_preset_plan.json")).unwrap();
    for (index, case) in fixture["cases"].as_array().unwrap().iter().enumerate() {
        let actual = preset_plan(&case["payload"], case["preset"].as_str().unwrap());
        let expected = if case["expected"].is_null() {
            None
        } else {
            Some(case["expected"].clone())
        };
        assert_eq!(actual, expected, "preset case {index}");
    }
}
