use deepseek_federation::{create_control_signer_bundle, load_control_signer};
use serde_json::json;

#[test]
fn encrypted_control_key_is_random_bound_and_secret_free() {
    let password = b"isolated-control-password-32-bytes";
    let first = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    let second = create_control_signer_bundle(password, "fleet-a", "test").unwrap();
    assert_ne!(
        first["binding"]["signerPublicKey"],
        second["binding"]["signerPublicKey"]
    );
    let public = first["binding"]["signerPublicKey"].as_str().unwrap();
    let signer = load_control_signer(&first, password, "fleet-a", "test", public).unwrap();
    assert_eq!(signer.public_key(), public);
    let debug = format!("{signer:?}");
    assert!(!debug.contains("password"));
    assert!(!debug.contains("ciphertext"));
    assert!(
        signer
            .sign_control_document(&json!({"schema":"control-authority-request-v1"}))
            .is_ok()
    );
    assert!(
        signer
            .sign_control_document(&json!({"schema":"control-storage-operation-grant-v1"}))
            .is_ok()
    );
    assert!(
        signer
            .sign_control_document(&json!({"schema":"fleet-identity-v1"}))
            .is_err()
    );
    for (fleet, environment, key) in [
        ("fleet-b", "test", public),
        ("fleet-a", "prod", public),
        ("fleet-a", "test", "invalid"),
    ] {
        assert!(load_control_signer(&first, password, fleet, environment, key).is_err());
    }
    assert!(
        load_control_signer(
            &first,
            b"different-control-password",
            "fleet-a",
            "test",
            public
        )
        .is_err()
    );
    assert!(create_control_signer_bundle(b"short", "fleet-a", "test").is_err());
    assert!(create_control_signer_bundle(password, "../fleet", "test").is_err());
    let mut tampered = first.clone();
    tampered["privateKeyEnvelope"]["bindingDigest"] = json!("sha256:wrong");
    assert!(load_control_signer(&tampered, password, "fleet-a", "test", public).is_err());
}
