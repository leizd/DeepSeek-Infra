use super::*;
use serde_json::json;

fn scope() -> AgentCredentialScope {
    AgentCredentialScope {
        fleet_id: "fleet-a".into(),
        environment: "production".into(),
        run_id: "run_fixture".into(),
        metadata_epoch: 3,
        runtime_revision: 1,
        request_sha256: "a".repeat(64),
        request_length: 120,
    }
}

fn binding() -> serde_json::Value {
    json!({"fleetId":"fleet-a","environment":"production"})
}

fn credentials() -> AgentProviderCredentials {
    AgentProviderCredentials::from_keys(
        Some("fixture-provider-value"),
        Some("fixture-search-value"),
    )
    .unwrap()
}

#[test]
fn independent_python_hkdf_aesgcm_vector_opens_with_the_exact_scope() {
    let sealed: SealedAgentCredentials =
        serde_json::from_str(include_str!("known_answer_v1.json")).unwrap();
    let opened = open(
        &[9; 32],
        "fixture-public-key",
        &binding(),
        &scope(),
        &sealed,
    )
    .unwrap();
    assert_eq!(opened.api_key(), Some("fixture-provider-value"));
    assert_eq!(opened.tavily_api_key(), Some("fixture-search-value"));
}

#[test]
fn roundtrip_keeps_provider_and_search_credentials_private() {
    let scope = scope();
    let input = credentials();
    let sealed = seal(&[9; 32], "fixture-public-key", &binding(), &scope, &input).unwrap();
    let serialized = serde_json::to_vec(&sealed).unwrap();
    let text = String::from_utf8(serialized.clone()).unwrap();
    assert!(!text.contains("fixture-provider-value"));
    assert!(!text.contains("fixture-search-value"));
    assert!(!format!("{input:?}").contains("fixture-provider-value"));
    assert!(!format!("{sealed:?}").contains(&sealed.ciphertext));
    let restored: SealedAgentCredentials = serde_json::from_slice(&serialized).unwrap();
    let opened = open(
        &[9; 32],
        "fixture-public-key",
        &binding(),
        &scope,
        &restored,
    )
    .unwrap();
    assert_eq!(opened.api_key(), Some("fixture-provider-value"));
    assert_eq!(opened.tavily_api_key(), Some("fixture-search-value"));
}

#[test]
fn single_provider_fields_and_trimmed_legacy_keys_are_supported() {
    for (api, search) in [
        (Some("  fixture-api  "), None),
        (None, Some(" fixture-search ")),
    ] {
        let input = AgentProviderCredentials::from_keys(api, search).unwrap();
        let scope = scope();
        let sealed = seal(&[9; 32], "fixture-public-key", &binding(), &scope, &input).unwrap();
        let opened = open(&[9; 32], "fixture-public-key", &binding(), &scope, &sealed).unwrap();
        assert_eq!(opened.api_key(), api.map(str::trim));
        assert_eq!(opened.tavily_api_key(), search.map(str::trim));
    }
}

#[test]
fn every_run_request_and_epoch_binding_is_authenticated() {
    let original = scope();
    let sealed = seal(
        &[9; 32],
        "fixture-public-key",
        &binding(),
        &original,
        &credentials(),
    )
    .unwrap();
    let mut candidates = vec![];
    let mut changed = original.clone();
    changed.run_id.push_str("_other");
    candidates.push(changed);
    let mut changed = original.clone();
    changed.metadata_epoch += 1;
    candidates.push(changed);
    let mut changed = original.clone();
    changed.runtime_revision += 1;
    candidates.push(changed);
    let mut changed = original.clone();
    changed.request_sha256 = "b".repeat(64);
    candidates.push(changed);
    let mut changed = original.clone();
    changed.request_length += 1;
    candidates.push(changed);
    for changed in candidates {
        assert!(
            open(
                &[9; 32],
                "fixture-public-key",
                &binding(),
                &changed,
                &sealed
            )
            .is_err()
        );
        let mut forged = sealed.clone();
        forged.scope = changed.clone();
        assert!(
            open(
                &[9; 32],
                "fixture-public-key",
                &binding(),
                &changed,
                &forged
            )
            .is_err()
        );
    }
}

#[test]
fn foreign_signers_and_ciphertext_or_nonce_tampering_are_rejected() {
    let scope = scope();
    let sealed = seal(
        &[9; 32],
        "fixture-public-key",
        &binding(),
        &scope,
        &credentials(),
    )
    .unwrap();
    assert!(open(&[8; 32], "fixture-public-key", &binding(), &scope, &sealed).is_err());
    assert!(open(&[9; 32], "other-public-key", &binding(), &scope, &sealed).is_err());
    let mut changed = sealed.clone();
    let mut bytes = URL_SAFE_NO_PAD.decode(&changed.ciphertext).unwrap();
    bytes[0] ^= 1;
    changed.ciphertext = encode_b64url(&bytes);
    assert!(open(&[9; 32], "fixture-public-key", &binding(), &scope, &changed).is_err());
    let mut changed = sealed.clone();
    let mut nonce = URL_SAFE_NO_PAD.decode(&changed.nonce).unwrap();
    nonce[0] ^= 1;
    changed.nonce = encode_b64url(&nonce);
    assert!(open(&[9; 32], "fixture-public-key", &binding(), &scope, &changed).is_err());
}

#[test]
fn seal_uses_a_fresh_nonce_for_each_envelope() {
    let scope = scope();
    let input = credentials();
    let first = seal(&[9; 32], "fixture-public-key", &binding(), &scope, &input).unwrap();
    let second = seal(&[9; 32], "fixture-public-key", &binding(), &scope, &input).unwrap();
    assert_ne!(first.nonce, second.nonce);
    assert_ne!(first.ciphertext, second.ciphertext);
}

#[test]
fn invalid_scope_and_foreign_fleet_or_environment_fail_before_sealing() {
    let original = scope();
    for (field, value) in [
        ("runId", json!("../foreign")),
        ("runId", json!("")),
        ("fleetId", json!("fleet-b")),
        ("environment", json!("test")),
        ("metadataEpoch", json!(0)),
        ("metadataEpoch", json!(u64::MAX)),
        ("runtimeRevision", json!(0)),
        ("runtimeRevision", json!(u64::MAX)),
        ("requestSha256", json!("a".repeat(63))),
        ("requestSha256", json!("A".repeat(64))),
        ("requestLength", json!(0)),
        ("requestLength", json!(1_048_577)),
    ] {
        let mut value_scope = serde_json::to_value(&original).unwrap();
        value_scope[field] = value;
        let changed: AgentCredentialScope = serde_json::from_value(value_scope).unwrap();
        assert!(
            seal(
                &[9; 32],
                "fixture-public-key",
                &binding(),
                &changed,
                &credentials()
            )
            .is_err()
        );
    }
}

#[test]
fn invalid_or_oversized_key_inputs_are_not_accepted() {
    assert!(AgentProviderCredentials::from_keys(None, None).is_err());
    for value in [
        "".to_owned(),
        " ".to_owned(),
        "fixture\nvalue".to_owned(),
        "x".repeat(MAX_KEY_BYTES + 1),
    ] {
        assert!(AgentProviderCredentials::from_keys(Some(&value), None).is_err());
        assert!(AgentProviderCredentials::from_keys(None, Some(&value)).is_err());
    }
}

#[test]
fn malformed_or_future_envelopes_never_release_plaintext() {
    let scope = scope();
    let sealed = seal(
        &[9; 32],
        "fixture-public-key",
        &binding(),
        &scope,
        &credentials(),
    )
    .unwrap();
    for (field, value) in [
        ("schema", json!("future")),
        ("nonce", json!("short")),
        ("nonce", json!("!".repeat(16))),
        ("ciphertext", json!("!")),
        ("ciphertext", json!(encode_b64url(&[0u8; 1]))),
        (
            "ciphertext",
            json!("A".repeat(MAX_CIPHERTEXT_BYTES * 2 + 1)),
        ),
    ] {
        let mut document = serde_json::to_value(&sealed).unwrap();
        document[field] = value;
        let changed: SealedAgentCredentials = serde_json::from_value(document).unwrap();
        assert!(open(&[9; 32], "fixture-public-key", &binding(), &scope, &changed).is_err());
    }
    let mut document = serde_json::to_value(&sealed).unwrap();
    document["unexpected"] = json!(true);
    assert!(serde_json::from_value::<SealedAgentCredentials>(document).is_err());
}
