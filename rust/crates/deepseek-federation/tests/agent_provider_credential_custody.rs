use deepseek_federation::{
    AgentCredentialScope, AgentCredentialVault, AgentProviderCredentials,
    create_control_signer_bundle, load_control_signer,
};
use std::path::PathBuf;

struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        let mut nonce = [0u8; 16];
        getrandom::getrandom(&mut nonce).unwrap();
        let name = format!("{:032x}", u128::from_be_bytes(nonce));
        let root = std::env::temp_dir().join(format!("native-agent-credential-test-{name}"));
        std::fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn scope() -> AgentCredentialScope {
    AgentCredentialScope {
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
        run_id: "run-private-fixture".into(),
        metadata_epoch: 3,
        runtime_revision: 1,
        request_sha256: "b".repeat(64),
        request_length: 40,
    }
}

#[test]
fn encrypted_custody_survives_reopen_and_rejects_replacement_or_wrong_revision() {
    let scratch = Scratch::new();
    let password = b"isolated-agent-credential-fixture-password";
    let bundle = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    let public = bundle["binding"]["signerPublicKey"].as_str().unwrap();
    let signer = load_control_signer(&bundle, password, "fleet-a", "test", public).unwrap();
    let credentials = AgentProviderCredentials::from_keys(
        Some("fixture-provider-value"),
        Some("fixture-search-value"),
    )
    .unwrap();
    let scope = scope();
    let vault = AgentCredentialVault::open(&scratch.0).unwrap();
    let object = vault.put(&signer, &scope, &credentials).unwrap();
    let file = scratch
        .0
        .join("agent-provider-credentials-v1")
        .join(format!("{}.json", object.id));
    let before = std::fs::read(&file).unwrap();
    let text = std::str::from_utf8(&before).unwrap();
    assert!(!text.contains("fixture-provider-value"));
    assert!(!text.contains("fixture-search-value"));
    assert_eq!(vault.put(&signer, &scope, &credentials).unwrap(), object);
    let changed = AgentProviderCredentials::from_keys(
        Some("other-fixture-value"),
        Some("fixture-search-value"),
    )
    .unwrap();
    assert!(vault.put(&signer, &scope, &changed).is_err());
    assert_eq!(std::fs::read(&file).unwrap(), before);
    drop(vault);
    drop(signer);
    let signer = load_control_signer(&bundle, password, "fleet-a", "test", public).unwrap();
    let reopened = AgentCredentialVault::open(&scratch.0).unwrap();
    let recovered = reopened.get(&signer, &scope, &object).unwrap();
    assert_eq!(recovered.api_key(), Some("fixture-provider-value"));
    assert_eq!(recovered.tavily_api_key(), Some("fixture-search-value"));
    let mut wrong = scope.clone();
    wrong.runtime_revision += 1;
    assert!(reopened.get(&signer, &wrong, &object).is_err());
    let mut wrong_object = object.clone();
    wrong_object.sha256 = "0".repeat(64);
    assert!(reopened.get(&signer, &scope, &wrong_object).is_err());
    wrong_object = object.clone();
    wrong_object.length += 1;
    assert!(reopened.get(&signer, &scope, &wrong_object).is_err());
    std::fs::write(&file, b"partial").unwrap();
    assert!(reopened.get(&signer, &scope, &object).is_err());
    assert!(reopened.put(&signer, &scope, &credentials).is_err());
    assert_eq!(std::fs::read(&file).unwrap(), b"partial");
}

#[test]
fn relative_roots_and_parent_traversal_are_not_credential_stores() {
    assert!(AgentCredentialVault::open(std::path::Path::new("relative-state")).is_err());
    let scratch = Scratch::new();
    assert!(AgentCredentialVault::open(&scratch.0.join("../foreign-state")).is_err());
}

#[cfg(unix)]
#[test]
fn private_files_and_directories_have_restricted_modes_and_symlinks_are_denied() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let scratch = Scratch::new();
    let target = scratch.0.join("real");
    std::fs::create_dir(&target).unwrap();
    symlink(&target, scratch.0.join("linked")).unwrap();
    assert!(AgentCredentialVault::open(&scratch.0.join("linked")).is_err());
    let vault = AgentCredentialVault::open(&scratch.0).unwrap();
    let password = b"isolated-agent-mode-fixture-password";
    let bundle = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    let public = bundle["binding"]["signerPublicKey"].as_str().unwrap();
    let signer = load_control_signer(&bundle, password, "fleet-a", "test", public).unwrap();
    let scope = scope();
    let credentials =
        AgentProviderCredentials::from_keys(Some("fixture-provider-value"), None).unwrap();
    let object = vault.put(&signer, &scope, &credentials).unwrap();
    let directory = scratch.0.join("agent-provider-credentials-v1");
    let file = directory.join(format!("{}.json", object.id));
    assert_eq!(
        std::fs::metadata(&directory).unwrap().permissions().mode() & 0o777,
        0o700
    );
    assert_eq!(
        std::fs::metadata(&file).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let retained = directory.join("retained");
    std::fs::rename(&file, &retained).unwrap();
    symlink(&retained, &file).unwrap();
    assert!(vault.get(&signer, &scope, &object).is_err());
    assert!(vault.put(&signer, &scope, &credentials).is_err());
}
