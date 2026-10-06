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
    pub fn public_key(&self) -> String {
        encode_b64url(self.key.verifying_key().as_bytes())
    }
    pub fn binding(&self) -> &Value {
        &self.binding
    }

    // No arbitrary-byte signing API. Callers still validate the complete document
    // before issuance; these two domain separators cannot be selected over RPC.
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
