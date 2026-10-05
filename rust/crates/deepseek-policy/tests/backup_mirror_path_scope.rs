use deepseek_policy::backup_mirror::{MirrorStore, PutRequest, RecipientGroups, sha256_hex};
use deepseek_policy::python_json::dumps_compact;
use serde_json::json;

#[test]
fn a_profile_cannot_alias_the_store_or_its_parent() {
    let workspace = tempfile::tempdir().unwrap();
    let root = workspace.path().join(".backup-mirror");
    let store = MirrorStore::new(&root);
    for profile in [".", ".."] {
        let mut envelope =
            json!({"schemaVersion":1,"conversations":[{"id":"scope-probe"}],"conflicts":[]});
        envelope["digest"] = json!(sha256_hex(dumps_compact(&envelope).as_bytes()));
        let result = store.put(
            PutRequest {
                profile_id: profile.to_string(),
                envelope,
                source_epoch: "epoch-scope".to_string(),
                recipients: RecipientGroups::Explicit(vec![
                    "age1fu59d59ghmr8x2t5dyzjs9xdcjgnakujp7mjy7cz2v7fq6vjqypskh4e62".to_string(),
                ]),
                acknowledged_at: None,
                client_replica_id: "scope-probe".to_string(),
                client_sequence: 1,
                expected_head_generation_id: None,
                now: None,
            },
            false,
        );
        let error = result.expect_err("a profile must name a child of the mirror store");
        assert_eq!(error.status, 400);
        assert_eq!(error.message, "Invalid backup mirror profile id");
        assert!(!root.exists());
        assert!(!workspace.path().join("HEAD.json").exists());
    }
}
