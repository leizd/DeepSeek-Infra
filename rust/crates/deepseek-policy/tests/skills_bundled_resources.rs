use deepseek_policy::skills::{eval, registry::Registry};
use serde_json::{Value, json};
use std::{fs, path::Path, process::Command};

#[test]
fn bundled_skills_and_golden_cases_keep_all_user_writes_in_the_data_root() {
    let data = tempfile::tempdir().unwrap();
    let assets = tempfile::tempdir().unwrap();
    let originals = [
        (
            "skills/builtin/study_tutor.json",
            include_str!("../../../../skills/builtin/study_tutor.json"),
        ),
        (
            "skills/packs/study.json",
            include_str!("../../../../skills/packs/study.json"),
        ),
        (
            eval::GOLDEN_CASES,
            include_str!("../../../../evals/golden/skills/skill_eval_cases.jsonl"),
        ),
    ];
    for (relative, bytes) in originals {
        let destination = assets.path().join(relative);
        fs::create_dir_all(destination.parent().unwrap()).unwrap();
        fs::write(destination, bytes).unwrap();
    }
    let registry = Registry::new(data.path()).with_bundle_root(assets.path());
    assert_eq!(registry.root, data.path());
    assert_eq!(
        registry.get("skill_study_tutor", true).unwrap()["builtin"],
        true
    );
    assert_eq!(registry.get_pack("pack_study").unwrap()["builtin"], true);
    assert!(
        eval::cases(&registry)
            .as_array()
            .unwrap()
            .iter()
            .any(|case| case["caseId"] == "study-os-scheduling")
    );
    let mut skill: Value = serde_json::from_str(originals[0].1).unwrap();
    skill["skillId"] = "bundle-custom".into();
    registry.create(&skill, false).unwrap();
    eval::save(
        &registry,
        &json!({"caseId":"bundle-user-case", "skillId":"bundle-custom"}),
    )
    .unwrap();
    assert!(
        data.path()
            .join(".skills/custom/bundle-custom.json")
            .is_file()
    );
    assert!(data.path().join(".skills/eval_cases.jsonl").is_file());
    assert!(
        eval::cases(&registry)
            .as_array()
            .unwrap()
            .iter()
            .any(|case| case["caseId"] == "bundle-user-case")
    );
    assert!(!data.path().join("skills").exists());
    assert!(!data.path().join("evals").exists());
    assert!(!assets.path().join(".skills").exists());
    assert!(!assets.path().join(".workspace-generation").exists());
    for (relative, bytes) in originals {
        assert_eq!(
            fs::read(assets.path().join(relative)).unwrap(),
            bytes.as_bytes()
        );
    }
}

#[test]
fn registry_bundle_defaults_retain_the_existing_layout() {
    let root = tempfile::tempdir().unwrap();
    let registry = Registry::new(root.path());
    assert_eq!(registry.bundle_root, root.path());
    assert_eq!(registry.builtin, root.path().join("skills/builtin"));
    assert_eq!(registry.packs, root.path().join("skills/packs"));
    assert_eq!(registry.data, root.path().join(".skills"));
}

#[test]
fn registry_bundle_environment_is_process_scoped() {
    if let Ok(mode) = std::env::var("SKILL_BUNDLE_ENV_CHILD") {
        let data = std::env::var_os("DEEPSEEK_INFRA_ROOT").unwrap();
        let data = Path::new(&data);
        let registry = Registry::from_env();
        let expected_bundle = if mode == "default" || mode == "empty" {
            data.to_path_buf()
        } else {
            data.join("assets")
        };
        assert_eq!(registry.root, data);
        assert_eq!(registry.bundle_root, expected_bundle);
        if mode == "override" {
            assert_eq!(registry.data, data.join("custom-data"));
            assert_eq!(registry.builtin, data.join("custom-builtin"));
            assert_eq!(registry.packs, data.join("custom-packs"));
        } else {
            assert_eq!(registry.data, data.join(".skills"));
            assert_eq!(registry.builtin, expected_bundle.join("skills/builtin"));
            assert_eq!(registry.packs, expected_bundle.join("skills/packs"));
        }
        return;
    }
    let root = tempfile::tempdir().unwrap();
    for mode in ["default", "empty", "bundle", "override"] {
        let mut child = Command::new(std::env::current_exe().unwrap());
        child
            .arg("--exact")
            .arg("registry_bundle_environment_is_process_scoped")
            .env("SKILL_BUNDLE_ENV_CHILD", mode)
            .env("DEEPSEEK_INFRA_ROOT", root.path());
        for key in [
            "DEEPSEEK_INFRA_BUNDLE_ROOT",
            "SKILLS_DIR",
            "DEEPSEEK_SKILLS_DIR",
            "BUILTIN_SKILLS_DIR",
            "DEEPSEEK_BUILTIN_SKILLS_DIR",
            "BUILTIN_PACKS_DIR",
            "DEEPSEEK_BUILTIN_PACKS_DIR",
        ] {
            child.env_remove(key);
        }
        if mode == "empty" {
            child.env("DEEPSEEK_INFRA_BUNDLE_ROOT", "");
        } else if mode != "default" {
            child.env("DEEPSEEK_INFRA_BUNDLE_ROOT", root.path().join("assets"));
        }
        if mode == "override" {
            child
                .env("SKILLS_DIR", root.path().join("custom-data"))
                .env("BUILTIN_SKILLS_DIR", root.path().join("custom-builtin"))
                .env("BUILTIN_PACKS_DIR", root.path().join("custom-packs"));
        }
        let output = child.output().unwrap();
        assert!(
            output.status.success(),
            "{mode}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}
