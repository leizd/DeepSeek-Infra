//! Compare real Skill request composition with the offline original Python callable.
use deepseek_policy::skills::{registry::Registry, runner};
use serde_json::Value;

#[test]
fn online_skill_payloads_match_original_callable_for_tools_search_memory_and_models() {
    let cases: Value =
        serde_json::from_str(include_str!("fixtures/skills_online_payloads.json")).unwrap();
    for case in cases["cases"].as_array().unwrap() {
        let root = tempfile::tempdir().unwrap();
        let builtin = root.path().join("skills/builtin");
        std::fs::create_dir_all(&builtin).unwrap();
        std::fs::write(
            builtin.join("online-payload.json"),
            case["skill"].to_string(),
        )
        .unwrap();
        let registry = Registry {
            root: root.path().into(),
            bundle_root: root.path().into(),
            data: root.path().join(".skills"),
            builtin,
            packs: root.path().join("skills/packs"),
            clock: Some(1_770_000_000),
        };
        let run = runner::prepare(
            &registry,
            "online-payload",
            &case["input"],
            &case["options"],
        )
        .unwrap();
        assert_eq!(
            run.request().unwrap(),
            case["expectedRequest"],
            "{}",
            case["name"]
        );
        // Request composition is the scope of this test. Close the real trace
        // with an explicit failure, never fabricate a successful provider run.
        let error = deepseek_policy::skills::error(
            "offline request-shape verification; no provider called",
            502,
        );
        assert!(run.finish(&registry, Err(error)).is_err());
    }
}
