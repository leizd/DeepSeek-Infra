use deepseek_federation::{
    load_online_signer, sign_federation_document, unlock_private_key_envelope,
    verify_federation_document,
};
use serde_json::Value;

const SIGNER_PASSPHRASE: &[u8] = b"signer-passphrase16";
const SHORT_PASSPHRASE: &[u8] = b"short";
const WRONG_PASSPHRASE: &[u8] = b"wrong-passphrase-16";
const NUL_PASSPHRASE: &[u8] = b"root-passphrase-16\x00";

fn fixture() -> Value {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v20/federation/federation_custody_vector.json"
    )))
    .unwrap()
}

fn passphrase(kind: &str) -> &'static [u8] {
    match kind {
        "signer" => SIGNER_PASSPHRASE,
        "short" => SHORT_PASSPHRASE,
        "wrong" => WRONG_PASSPHRASE,
        "nul" => NUL_PASSPHRASE,
        _ => panic!("unknown passphrase kind {kind}"),
    }
}

#[test]
fn rust_loads_custody_and_issues_the_frozen_attestation() {
    let fixture = fixture();
    let signer = load_online_signer(
        &fixture["signer_bundle"],
        SIGNER_PASSPHRASE,
        &fixture["root_identity"],
        fixture["now"].as_str().unwrap(),
    )
    .unwrap();
    assert_eq!(
        format!("{signer:?}"),
        format!(
            "OnlineFleetSigner {{ signer_key_id: {:?} }}",
            fixture["certificate"]["signerKeyId"].as_str().unwrap()
        )
    );
    assert!(!format!("{signer:?}").to_lowercase().contains("private"));
    let signed = sign_federation_document(
        &signer,
        &fixture["unsigned_document"],
        Some("REPLICA_ATTESTATION"),
    )
    .unwrap();
    assert_eq!(signed, fixture["signed_document"]);
    let certificate = signer.certificate();
    verify_federation_document(
        &signed,
        &certificate,
        &fixture["root_identity"],
        "federated-replica-attestation-v1",
        fixture["now"].as_str().unwrap(),
        "REPLICA_ATTESTATION",
    )
    .unwrap();
}

#[test]
fn rust_replays_sign_and_envelope_fail_closed_errors() {
    let fixture = fixture();
    let signer = load_online_signer(
        &fixture["signer_bundle"],
        SIGNER_PASSPHRASE,
        &fixture["root_identity"],
        fixture["now"].as_str().unwrap(),
    )
    .unwrap();
    for case in fixture["sign_cases"].as_array().unwrap() {
        let result = sign_federation_document(&signer, &case["document"], case["purpose"].as_str());
        match case["expected_error"].as_str() {
            None => {
                assert!(result.is_ok(), "{}", case["name"]);
            }
            Some(code) => {
                assert_eq!(result.unwrap_err().code(), code, "{}", case["name"]);
            }
        }
    }
    for case in fixture["envelope_cases"].as_array().unwrap() {
        let envelope = case.get("envelope").filter(|value| !value.is_null());
        let result = unlock_private_key_envelope(
            envelope,
            passphrase(case["passphrase_kind"].as_str().unwrap()),
            &case["binding"],
        );
        match case["expected_error"].as_str() {
            None => assert!(result.is_ok(), "{}", case["name"]),
            Some(code) => assert_eq!(result.unwrap_err().code(), code, "{}", case["name"]),
        }
    }
}
