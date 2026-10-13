//! A distinct control-signing key: never reuse an online Fleet certificate/key.
//! The encrypted envelope retains the existing frozen Argon2id/AES-GCM format.
use crate::canonical::{canonical_bytes, encode_b64url, typed_sha256};
use crate::custody::{FederationIdentityError, derive_key, error, load_private_key};
use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use ed25519_dalek::pkcs8::EncodePrivateKey;
use ed25519_dalek::{Signer, SigningKey};
use serde_json::{Value, json};
use zeroize::Zeroizing;

const BUNDLE_SCHEMA: &str = "native-control-signer-bundle-v1";
const BINDING_SCHEMA: &str = "native-control-signer-binding-v1";
const ENVELOPE_DOMAIN: &[u8] = b"deepseek-infra:federation-private-key-envelope-v1\0";

pub struct ControlSigningKey {
    key: SigningKey,
    binding: Value,
}

impl std::fmt::Debug for ControlSigningKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ControlSigningKey")
            .field("public_key", &self.public_key())
            .finish()
    }
}

impl ControlSigningKey {
    pub fn sign_agent_synthesis_projection(
        &self,
        evidence: &deepseek_protocol::generated::deepseek::agent::v1::AgentSynthesisProjectionEvidence,
    ) -> Result<Vec<u8>, FederationIdentityError> {
        use prost::Message as _;
        let digest = |s: &str| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        };
        let reference = |r: Option<
            &deepseek_protocol::generated::deepseek::agent::v1::AgentArtifactReference,
        >| {
            r.is_some_and(|r| digest(&r.sha256) && r.length > 0 && r.length <= 128 * 1024 * 1024)
        };
        if evidence.schema_version != 1
            || !digest(&evidence.source_receipt_sha256)
            || evidence
                .settlement_fence
                .as_ref()
                .is_none_or(|f| deepseek_protocol::validate_fence(f).is_err())
            || evidence.settlement_writer_token <= 0
            || evidence.run_id.is_empty()
            || evidence.metadata_epoch == 0
            || evidence.expected_index < 0
            || evidence.expected_index > i64::MAX - 2
            || !reference(evidence.content_body.as_ref())
            || !reference(evidence.done_body.as_ref())
            || self.binding["fleetId"] != evidence.fleet_id
            || self.binding["environment"] != evidence.environment
        {
            return Err(error("AGENT_SYNTHESIS_PROJECTION_INVALID"));
        }
        let bytes = evidence.encode_to_vec();
        if bytes.len() > 1024 * 1024 {
            return Err(error("AGENT_SYNTHESIS_PROJECTION_INVALID"));
        }
        let mut message = b"deepseek-infra:agent-synthesis-projection-proof-v1\0".to_vec();
        message.extend(bytes);
        Ok(self.key.sign(&message).to_bytes().to_vec())
    }
    pub fn sign_agent_plan_projection(
        &self,
        evidence: &deepseek_protocol::generated::deepseek::agent::v1::AgentPlanProjectionEvidence,
    ) -> Result<Vec<u8>, FederationIdentityError> {
        use prost::Message as _;
        let digest = |s: &str| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        };
        let reference = |r: Option<
            &deepseek_protocol::generated::deepseek::agent::v1::AgentArtifactReference,
        >| {
            r.is_some_and(|r| digest(&r.sha256) && r.length > 0 && r.length <= 128 * 1024 * 1024)
        };
        if evidence.schema_version != 1
            || !digest(&evidence.source_receipt_sha256)
            || evidence
                .settlement_fence
                .as_ref()
                .is_none_or(|f| deepseek_protocol::validate_fence(f).is_err())
            || evidence.settlement_writer_token <= 0
            || evidence.run_id.is_empty()
            || evidence.metadata_epoch == 0
            || evidence.expected_index < 0
            || evidence.normalized_plan.is_empty()
            || evidence.normalized_plan.len() > 32
            || !matches!(evidence.next_status.as_str(), "running" | "awaiting_plan")
            || !reference(evidence.plan_body.as_ref())
            || !reference(evidence.status_body.as_ref())
            || (evidence.planning_required && !reference(evidence.planning_body.as_ref()))
            || (!evidence.planning_required && evidence.planning_body.is_some())
            || self.binding["fleetId"] != evidence.fleet_id
            || self.binding["environment"] != evidence.environment
        {
            return Err(error("AGENT_PLAN_PROJECTION_INVALID"));
        }
        let bytes = evidence.encode_to_vec();
        if bytes.len() > 1024 * 1024 {
            return Err(error("AGENT_PLAN_PROJECTION_INVALID"));
        }
        let mut message = b"deepseek-infra:agent-plan-projection-proof-v1\0".to_vec();
        message.extend(bytes);
        Ok(self.key.sign(&message).to_bytes().to_vec())
    }
    pub fn public_key(&self) -> String {
        encode_b64url(self.key.verifying_key().as_bytes())
    }
    pub fn binding(&self) -> &Value {
        &self.binding
    }

    /// A distinct typed purpose. Neither arbitrary bytes nor a caller-selected
    /// signature domain can reach this API or the control-signing RPC.
    pub fn sign_agent_node_outcome(
        &self,
        evidence: &deepseek_protocol::generated::deepseek::agent::v1::AgentNodeOutcomeEvidence,
    ) -> Result<Vec<u8>, FederationIdentityError> {
        use prost::Message as _;
        let invalid = || error("AGENT_NODE_OUTCOME_INVALID");
        let hex = |s: &str| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        };
        let reference = |r: Option<
            &deepseek_protocol::generated::deepseek::agent::v1::AgentArtifactReference,
        >| {
            r.is_some_and(|r| hex(&r.sha256) && r.length > 0 && r.length <= 128 * 1024 * 1024)
        };
        let fence = evidence.fence.as_ref().ok_or_else(invalid)?;
        if evidence.schema_version != 1
            || deepseek_protocol::validate_fence(fence).is_err()
            || evidence.operation_id.len() != 75
            || !evidence.operation_id.starts_with("agent-node-")
            || !hex(&evidence.operation_id[11..])
            || evidence.run_id.is_empty()
            || evidence.node_id.is_empty()
            || !matches!(evidence.phase, 1 | 2 | 3)
            || (evidence.phase == 3
                && (evidence.node_id != "synthesizer"
                    || !evidence.provider_plan.is_empty()
                    || evidence.task_output.as_ref().is_none_or(|stats| {
                        stats.schema_version != 1
                            || stats.failed
                            || stats.prompt_tokens > i64::MAX as u64
                            || stats.completion_tokens > i64::MAX as u64
                            || stats.latency_ms < 0
                    })))
            || (evidence.phase == 1 && evidence.node_id != "leader")
            || !reference(evidence.request.as_ref())
            || !reference(evidence.public_event.as_ref())
            || !hex(&evidence.plan_digest)
            || evidence.metadata_index < 0
            || evidence.metadata_epoch == 0
            || evidence.writer_fencing_token <= 0
            || evidence.created_at.is_empty()
            || evidence.confirmed_at.is_empty()
            || !hex(&evidence.outcome_sha256)
            || evidence.outcome_length == 0
            || evidence.outcome_length > 1024 * 1024
            || self.binding["fleetId"] != evidence.fleet_id
            || self.binding["environment"] != evidence.environment
        {
            return Err(invalid());
        }
        let bytes = evidence.encode_to_vec();
        if bytes.len() > 1024 * 1024 {
            return Err(invalid());
        }
        let mut message = b"deepseek-infra:agent-node-outcome-proof-v1\0".to_vec();
        message.extend(bytes);
        Ok(self.key.sign(&message).to_bytes().to_vec())
    }

    // No arbitrary-byte signing API. Callers still validate the complete document
    // before issuance; the domain separator cannot be selected over RPC.
    pub fn sign_control_document(
        &self,
        document: &Value,
    ) -> Result<String, FederationIdentityError> {
        let schema = document.get("schema").and_then(Value::as_str).unwrap_or("");
        let domain: &[u8] = match schema {
            "control-authority-request-v1" => b"deepseek-infra:control-authority-request-v1\0",
            "control-storage-operation-grant-v1" => {
                b"deepseek-infra:control-storage-operation-grant-v1\0"
            }
            "control-agent-execution-grant-v1" => {
                b"deepseek-infra:control-agent-execution-grant-v1\0"
            }
            _ => return Err(error("CONTROL_SIGNER_PURPOSE_INVALID")),
        };
        let mut message = domain.to_vec();
        message.extend(
            canonical_bytes(document).map_err(|_| error("CONTROL_SIGNER_REQUEST_INVALID"))?,
        );
        Ok(encode_b64url(&self.key.sign(&message).to_bytes()))
    }
}

fn valid_name(value: &str) -> bool {
    (1..=128).contains(&value.len())
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-".contains(&b))
        && value.as_bytes()[0].is_ascii_alphanumeric()
}

fn binding(public: &str, fleet: &str, environment: &str) -> Result<Value, FederationIdentityError> {
    if !valid_name(fleet) || !valid_name(environment) {
        return Err(error("CONTROL_SIGNER_BINDING_INVALID"));
    }
    Ok(
        json!({"schema": BINDING_SCHEMA, "signerPublicKey": public, "fleetId": fleet,
        "environment": environment, "domain": "action", "runtime": "go", "role": "control-plane"}),
    )
}

pub fn load_control_signer(
    bundle: &Value,
    password: &[u8],
    fleet: &str,
    environment: &str,
    expected_public: &str,
) -> Result<ControlSigningKey, FederationIdentityError> {
    let expected = binding(expected_public, fleet, environment)?;
    let fields = bundle
        .as_object()
        .ok_or_else(|| error("CONTROL_SIGNER_BUNDLE_INVALID"))?;
    if fields.len() != 3
        || fields.get("schema") != Some(&Value::from(BUNDLE_SCHEMA))
        || fields.get("binding") != Some(&expected)
    {
        return Err(error("CONTROL_SIGNER_BINDING_INVALID"));
    }
    let key = load_private_key(fields.get("privateKeyEnvelope"), password, &expected)?;
    if encode_b64url(key.verifying_key().as_bytes()) != expected_public {
        return Err(error("CONTROL_SIGNER_KEY_MISMATCH"));
    }
    Ok(ControlSigningKey {
        key,
        binding: expected,
    })
}

/// Provision only in Rust. Returns an encrypted bundle and public metadata;
/// the random seed and PKCS#8 plaintext are never returned or logged.
pub fn create_control_signer_bundle(
    password: &[u8],
    fleet: &str,
    environment: &str,
) -> Result<Value, FederationIdentityError> {
    if !(16..=1024).contains(&password.len()) || password.contains(&0) {
        return Err(error("FEDERATION_PRIVATE_KEY_PASSPHRASE_INVALID"));
    }
    let mut seed = Zeroizing::new([0u8; 32]);
    let mut salt = [0u8; 16];
    let mut nonce = [0u8; 12];
    getrandom::getrandom(seed.as_mut())
        .and_then(|_| getrandom::getrandom(&mut salt))
        .and_then(|_| getrandom::getrandom(&mut nonce))
        .map_err(|_| error("CONTROL_SIGNER_RANDOM_UNAVAILABLE"))?;
    let key = SigningKey::from_bytes(&seed);
    let public_binding = binding(
        &encode_b64url(key.verifying_key().as_bytes()),
        fleet,
        environment,
    )?;
    let metadata = json!({"schema": "federation-private-key-envelope-v1", "bindingDigest": typed_sha256(&canonical_bytes(&public_binding).map_err(|_| error("CONTROL_SIGNER_BINDING_INVALID"))?),
        "kdf": {"algorithm": "Argon2id", "iterations": 3, "lanes": 4, "length": 32, "memoryKiB": 65536, "salt": encode_b64url(&salt)},
        "aead": {"algorithm": "AES-256-GCM", "nonce": encode_b64url(&nonce)}});
    let derived = Zeroizing::new(derive_key(password, &salt)?);
    let cipher = Aes256Gcm::new_from_slice(derived.as_ref())
        .map_err(|_| error("FEDERATION_PRIVATE_KEY_ENCRYPTION_UNAVAILABLE"))?;
    let der = key
        .to_pkcs8_der()
        .map_err(|_| error("FEDERATION_PRIVATE_KEY_ENCRYPTION_UNAVAILABLE"))?;
    let mut aad = ENVELOPE_DOMAIN.to_vec();
    aad.extend(canonical_bytes(&metadata).map_err(|_| error("CONTROL_SIGNER_BUNDLE_INVALID"))?);
    let ciphertext = cipher
        .encrypt(
            Nonce::from_slice(&nonce),
            Payload {
                msg: der.as_bytes(),
                aad: &aad,
            },
        )
        .map_err(|_| error("FEDERATION_PRIVATE_KEY_ENCRYPTION_UNAVAILABLE"))?;
    let mut envelope = metadata;
    envelope["ciphertext"] = Value::from(encode_b64url(&ciphertext));
    Ok(json!({"schema": BUNDLE_SCHEMA, "binding": public_binding, "privateKeyEnvelope": envelope}))
}
