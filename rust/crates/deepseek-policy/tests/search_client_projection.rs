//! Frozen expectations extracted from the unchanged offline Python oracle.
use deepseek_policy::search::search_for_client;
use serde_json::Value;

#[test]
fn browser_search_projection_matches_the_retained_oracle() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/search_client_projection.json")).unwrap();
    let cases = fixture["cases"].as_array().unwrap();
    for (index, case) in cases.iter().enumerate() {
        assert_eq!(
            search_for_client(Some(&case["input"])),
            case["expected"],
            "oracle case {index}"
        );
    }
    assert_eq!(search_for_client(None), Value::Null);
}
