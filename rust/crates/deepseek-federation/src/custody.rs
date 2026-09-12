//! Isolated Fleet online-signer custody and document issuance.
//! Private keys never leave this module and are never sent to Go.

use crate::canonical::{
    assert_secret_free, canonical_bytes, decode_fixed, encode_b64url, object, string_field,
    typed_sha256,
};
use crate::identity::{DOCUMENT_DOMAIN_PREFIX, validate_online_signer_certificate};
use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use argon2::{Algorithm, Argon2, Params, Version};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use ed25519_dalek::pkcs8::DecodePrivateKey;
use ed25519_dalek::{Signer, SigningKey};
use serde_json::{Map, Value};
use std::fmt;

const ONLINE_SIGNER_PRIVATE_BUNDLE_SCHEMA: &str = "fleet-federation-online-signer-private-v1";
const PRIVATE_KEY_ENVELOPE_SCHEMA: &str = "federation-private-key-envelope-v1";
const SIGNATURE_ALGORITHM: &str = "Ed25519";
const PRIVATE_KEY_ENVELOPE_DOMAIN: &[u8] = b"deepseek-infra:federation-private-key-envelope-v1\0";
const ARGON2_MEMORY_KIB: u32 = 64 * 1024;
const ARGON2_ITERATIONS: u32 = 3;
const ARGON2_LANES: u32 = 4;
const DERIVED_KEY_BYTES: usize = 32;
const MIN_PASSPHRASE_BYTES: usize = 16;
const MAX_PASSPHRASE_BYTES: usize = 1024;
const MIN_CIPHERTEXT_BYTES: usize = 17;
const ONLINE_SIGNER_PURPOSES: &[&str] = &[
    "DR_ATTESTATION",
    "EVIDENCE",
    "INGRESS_GRANT",
    "READINESS_ATTESTATION",
    "REPLICA_ATTESTATION",
    "SESSION_AUTHENTICATION",
];

#[derive(Clone, PartialEq, Eq)]
pub struct FederationIdentityError {
    code: &'static str,
}

impl FederationIdentityError {
    const fn new(code: &'static str) -> Self {
        Self { code }
    }

    pub const fn code(&self) -> &'static str {
        self.code
    }
}

impl fmt::Debug for FederationIdentityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("FederationIdentityError")
            .field("code", &self.code)
            .finish()
    }
}

impl fmt::Display for FederationIdentityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code)
    }
}

impl std::error::Error for FederationIdentityError {}

pub struct OnlineFleetSigner {
    signing_key: SigningKey,
    certificate: Map<String, Value>,
}

impl fmt::Debug for OnlineFleetSigner {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("OnlineFleetSigner")
            .field("signer_key_id", &self.signer_key_id())
            .finish()
    }
}

impl OnlineFleetSigner {
    pub fn signer_key_id(&self) -> &str {
        string_field(&self.certificate, "signerKeyId").unwrap_or_default()
    }

    pub fn fleet_id(&self) -> &str {
        string_field(&self.certificate, "fleetId").unwrap_or_default()
    }

    pub fn certificate(&self) -> Map<String, Value> {
        self.certificate.clone()
    }
}

pub fn load_online_signer(
    bundle: &Value,
    passphrase: &[u8],
    root_identity: &Value,
    now: &str,
) -> Result<OnlineFleetSigner, FederationIdentityError> {
    let fields = object(bundle).ok_or_else(|| error("FEDERATION_SIGNER_BUNDLE_SCHEMA_INVALID"))?;
    if string_field(fields, "schema") != Some(ONLINE_SIGNER_PRIVATE_BUNDLE_SCHEMA) {
        return Err(error("FEDERATION_SIGNER_BUNDLE_SCHEMA_INVALID"));
    }
    let certificate_value = fields.get("certificate").cloned().unwrap_or(Value::Null);
    let cert_errors =
        validate_online_signer_certificate(&certificate_value, root_identity, now, None);
    if let Some(code) = cert_errors.first() {
        return Err(static_code(code));
    }
    let certificate = object(&certificate_value)
        .cloned()
        .ok_or_else(|| error("FEDERATION_SIGNER_CERTIFICATE_INVALID"))?;
    let key = load_private_key(
        fields.get("privateKeyEnvelope"),
        passphrase,
        &certificate_value,
    )?;
    let expected = string_field(&certificate, "signerPublicKey").unwrap_or_default();
    if encode_b64url(key.verifying_key().as_bytes()) != expected {
        return Err(error("FEDERATION_SIGNER_PRIVATE_KEY_MISMATCH"));
    }
    Ok(OnlineFleetSigner {
        signing_key: key,
        certificate,
    })
}

pub fn sign_federation_document(
    signer: &OnlineFleetSigner,
    document: &Value,
    purpose: Option<&str>,
) -> Result<Value, FederationIdentityError> {
    let Some(fields) = document.as_object() else {
        return Err(error("FEDERATION_DOCUMENT_INVALID"));
    };
    assert_secret_free(document).map_err(|_| error("FEDERATION_DOCUMENT_CONTAINS_SECRET"))?;
    if let Some(purpose) = purpose {
        if let Some(code) = purpose_error(&signer.certificate, purpose) {
            return Err(error(code));
        }
    }
    if ["signerKeyId", "signatureAlgorithm", "signature"]
        .iter()
        .any(|field| fields.contains_key(*field))
    {
        return Err(error("FEDERATION_DOCUMENT_ALREADY_SIGNED"));
    }
    let schema = string_field(fields, "schema").filter(|value| !value.is_empty());
    let Some(schema) = schema else {
        return Err(error("FEDERATION_DOCUMENT_SCHEMA_INVALID"));
    };
    let fleet_id = string_field(fields, "fleetId").filter(|value| !value.is_empty());
    let Some(fleet_id) = fleet_id else {
        return Err(error("FEDERATION_DOCUMENT_FLEET_ID_REQUIRED"));
    };
    if fleet_id != signer.fleet_id() {
        return Err(error("FEDERATION_DOCUMENT_FLEET_MISMATCH"));
    }
    let mut payload = fields.clone();
    payload.insert(
        "signerKeyId".to_string(),
        Value::String(signer.signer_key_id().to_string()),
    );
    payload.insert(
        "signatureAlgorithm".to_string(),
        Value::String(SIGNATURE_ALGORITHM.to_string()),
    );
    let message = document_message(schema, &Value::Object(payload.clone()), &signer.certificate)?;
    let signature = signer.signing_key.sign(&message);
    payload.insert(
        "signature".to_string(),
        Value::String(encode_b64url(&signature.to_bytes())),
    );
    Ok(Value::Object(payload))
}

pub fn unlock_private_key_envelope(
    envelope: Option<&Value>,
    passphrase: &[u8],
    binding: &Value,
) -> Result<(), FederationIdentityError> {
    load_private_key(envelope, passphrase, binding).map(|_| ())
}

fn purpose_error(certificate: &Map<String, Value>, required_purpose: &str) -> Option<&'static str> {
    let raw = certificate.get("purposes").and_then(Value::as_array);
    let purposes_valid = raw.is_some_and(|purposes| {
        !purposes.is_empty()
            && purposes.iter().all(|item| {
                item.as_str()
                    .is_some_and(|purpose| ONLINE_SIGNER_PURPOSES.contains(&purpose))
            })
            && {
                let mut sorted: Vec<_> = purposes.iter().filter_map(Value::as_str).collect();
                let original = sorted.clone();
                sorted.sort_unstable();
                sorted.dedup();
                original == sorted
            }
    });
    if !purposes_valid {
        return Some("FEDERATION_SIGNER_CERTIFICATE_PURPOSES_INVALID");
    }
    if !ONLINE_SIGNER_PURPOSES.contains(&required_purpose) {
        return Some("FEDERATION_SIGNER_PURPOSE_INVALID");
    }
    if !raw.is_some_and(|purposes| {
        purposes
            .iter()
            .any(|item| item.as_str() == Some(required_purpose))
    }) {
        return Some("FEDERATION_SIGNER_PURPOSE_NOT_ALLOWED");
    }
    None
}

fn document_message(
    schema: &str,
    payload: &Value,
    certificate: &Map<String, Value>,
) -> Result<Vec<u8>, FederationIdentityError> {
    let canonical = canonical_bytes(&Value::Object(certificate.clone()))
        .map_err(|_| error("FEDERATION_CANONICAL_PAYLOAD_INVALID"))?;
    let mut certificate_context = Map::new();
    for field in ["fleetId", "rootKeyId", "rootFingerprint", "signerKeyId"] {
        let value = string_field(certificate, field)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| error("FEDERATION_SIGNER_CERTIFICATE_INVALID"))?;
        certificate_context.insert(field.to_string(), Value::String(value.to_string()));
    }
    certificate_context.insert(
        "certificateDigest".to_string(),
        Value::String(typed_sha256(&canonical)),
    );
    let message_document = serde_json::json!({
        "schema": schema,
        "certificateContext": certificate_context,
        "document": payload,
    });
    let mut message = DOCUMENT_DOMAIN_PREFIX.to_vec();
    message.extend(
        canonical_bytes(&message_document)
            .map_err(|_| error("FEDERATION_CANONICAL_PAYLOAD_INVALID"))?,
    );
    Ok(message)
}

fn load_private_key(
    envelope: Option<&Value>,
    passphrase: &[u8],
    binding: &Value,
) -> Result<SigningKey, FederationIdentityError> {
    let password = passphrase_bytes(passphrase)?;
    let Some(envelope) = envelope else {
        return Err(error("FEDERATION_PRIVATE_KEY_UNAVAILABLE"));
    };
    let parts = envelope_parts(envelope, binding)?;
    let key = derive_key(&password, &parts.salt)?;
    let aad = envelope_aad(&parts.metadata)?;
    let cipher =
        Aes256Gcm::new_from_slice(&key).map_err(|_| error("FEDERATION_PRIVATE_KEY_UNAVAILABLE"))?;
    let private_der = cipher
        .decrypt(
            Nonce::from_slice(&parts.nonce),
            Payload {
                msg: &parts.ciphertext,
                aad: &aad,
            },
        )
        .map_err(|_| error("FEDERATION_PRIVATE_KEY_UNAVAILABLE"))?;
    SigningKey::from_pkcs8_der(&private_der)
        .map_err(|_| error("FEDERATION_PRIVATE_KEY_UNAVAILABLE"))
}

struct EnvelopeParts {
    metadata: Map<String, Value>,
    salt: [u8; 16],
    nonce: [u8; 12],
    ciphertext: Vec<u8>,
}

fn envelope_parts(
    envelope: &Value,
    binding: &Value,
) -> Result<EnvelopeParts, FederationIdentityError> {
    let fields =
        object(envelope).ok_or_else(|| error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"))?;
    let expected_keys = ["aead", "bindingDigest", "ciphertext", "kdf", "schema"];
    let mut actual: Vec<_> = fields.keys().map(String::as_str).collect();
    actual.sort_unstable();
    if actual != expected_keys {
        return Err(error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"));
    }
    if string_field(fields, "schema") != Some(PRIVATE_KEY_ENVELOPE_SCHEMA) {
        return Err(error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"));
    }
    let expected_binding = typed_sha256(
        &canonical_bytes(binding).map_err(|_| error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"))?,
    );
    if string_field(fields, "bindingDigest") != Some(expected_binding.as_str()) {
        return Err(error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"));
    }
    let kdf = object(fields.get("kdf").unwrap_or(&Value::Null))
        .ok_or_else(|| error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"))?;
    let aead = object(fields.get("aead").unwrap_or(&Value::Null))
        .ok_or_else(|| error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"))?;
    let mut kdf_keys: Vec<_> = kdf.keys().map(String::as_str).collect();
    kdf_keys.sort_unstable();
    let mut aead_keys: Vec<_> = aead.keys().map(String::as_str).collect();
    aead_keys.sort_unstable();
    if kdf_keys
        != [
            "algorithm",
            "iterations",
            "lanes",
            "length",
            "memoryKiB",
            "salt",
        ]
        || string_field(kdf, "algorithm") != Some("Argon2id")
        || kdf.get("length").and_then(Value::as_u64) != Some(DERIVED_KEY_BYTES as u64)
        || kdf.get("iterations").and_then(Value::as_u64) != Some(u64::from(ARGON2_ITERATIONS))
        || kdf.get("lanes").and_then(Value::as_u64) != Some(u64::from(ARGON2_LANES))
        || kdf.get("memoryKiB").and_then(Value::as_u64) != Some(u64::from(ARGON2_MEMORY_KIB))
        || aead_keys != ["algorithm", "nonce"]
        || string_field(aead, "algorithm") != Some("AES-256-GCM")
    {
        return Err(error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"));
    }
    let salt = string_field(kdf, "salt")
        .and_then(decode_fixed::<16>)
        .ok_or_else(|| error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"))?;
    let nonce = string_field(aead, "nonce")
        .and_then(decode_fixed::<12>)
        .ok_or_else(|| error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"))?;
    let ciphertext = string_field(fields, "ciphertext")
        .and_then(|value| URL_SAFE_NO_PAD.decode(value).ok())
        .ok_or_else(|| error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"))?;
    if ciphertext.len() < MIN_CIPHERTEXT_BYTES {
        return Err(error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"));
    }
    let mut metadata = fields.clone();
    metadata.remove("ciphertext");
    Ok(EnvelopeParts {
        metadata,
        salt,
        nonce,
        ciphertext,
    })
}

fn envelope_aad(metadata: &Map<String, Value>) -> Result<Vec<u8>, FederationIdentityError> {
    let mut aad = PRIVATE_KEY_ENVELOPE_DOMAIN.to_vec();
    aad.extend(
        canonical_bytes(&Value::Object(metadata.clone()))
            .map_err(|_| error("FEDERATION_PRIVATE_KEY_ENVELOPE_INVALID"))?,
    );
    Ok(aad)
}

fn derive_key(password: &[u8], salt: &[u8]) -> Result<[u8; 32], FederationIdentityError> {
    let params = Params::new(
        ARGON2_MEMORY_KIB,
        ARGON2_ITERATIONS,
        ARGON2_LANES,
        Some(DERIVED_KEY_BYTES),
    )
    .map_err(|_| error("FEDERATION_PRIVATE_KEY_ENCRYPTION_UNAVAILABLE"))?;
    let argon2 = Argon2::new(Algorithm::Argon2id, Version::V0x13, params);
    let mut output = [0_u8; 32];
    argon2
        .hash_password_into(password, salt, &mut output)
        .map_err(|_| error("FEDERATION_PRIVATE_KEY_ENCRYPTION_UNAVAILABLE"))?;
    Ok(output)
}

fn passphrase_bytes(passphrase: &[u8]) -> Result<Vec<u8>, FederationIdentityError> {
    if !(MIN_PASSPHRASE_BYTES..=MAX_PASSPHRASE_BYTES).contains(&passphrase.len())
        || passphrase.contains(&0)
    {
        return Err(error("FEDERATION_PRIVATE_KEY_PASSPHRASE_INVALID"));
    }
    Ok(passphrase.to_vec())
}

fn error(code: &'static str) -> FederationIdentityError {
    FederationIdentityError::new(code)
}

fn static_code(code: &str) -> FederationIdentityError {
    match code {
        "FEDERATION_ROOT_IDENTITY_INVALID" => error("FEDERATION_ROOT_IDENTITY_INVALID"),
        "FEDERATION_ROOT_IDENTITY_SCHEMA_INVALID" => {
            error("FEDERATION_ROOT_IDENTITY_SCHEMA_INVALID")
        }
        "FEDERATION_ROOT_IDENTITY_ALGORITHM_INVALID" => {
            error("FEDERATION_ROOT_IDENTITY_ALGORITHM_INVALID")
        }
        "FEDERATION_FLEET_ID_INVALID" => error("FEDERATION_FLEET_ID_INVALID"),
        "FEDERATION_ROOT_PUBLIC_KEY_INVALID" => error("FEDERATION_ROOT_PUBLIC_KEY_INVALID"),
        "FEDERATION_ROOT_KEY_ID_INVALID" => error("FEDERATION_ROOT_KEY_ID_INVALID"),
        "FEDERATION_ROOT_FINGERPRINT_INVALID" => error("FEDERATION_ROOT_FINGERPRINT_INVALID"),
        "FEDERATION_ROOT_IDENTITY_TIMESTAMP_INVALID" => {
            error("FEDERATION_ROOT_IDENTITY_TIMESTAMP_INVALID")
        }
        "FEDERATION_SIGNER_CERTIFICATE_INVALID" => error("FEDERATION_SIGNER_CERTIFICATE_INVALID"),
        "FEDERATION_SIGNER_CERTIFICATE_SCHEMA_INVALID" => {
            error("FEDERATION_SIGNER_CERTIFICATE_SCHEMA_INVALID")
        }
        "FEDERATION_SIGNER_CERTIFICATE_FLEET_MISMATCH" => {
            error("FEDERATION_SIGNER_CERTIFICATE_FLEET_MISMATCH")
        }
        "FEDERATION_SIGNER_CERTIFICATE_ROOT_MISMATCH" => {
            error("FEDERATION_SIGNER_CERTIFICATE_ROOT_MISMATCH")
        }
        "FEDERATION_SIGNER_CERTIFICATE_ALGORITHM_INVALID" => {
            error("FEDERATION_SIGNER_CERTIFICATE_ALGORITHM_INVALID")
        }
        "FEDERATION_SIGNER_CERTIFICATE_PURPOSES_INVALID" => {
            error("FEDERATION_SIGNER_CERTIFICATE_PURPOSES_INVALID")
        }
        "FEDERATION_SIGNER_PURPOSE_INVALID" => error("FEDERATION_SIGNER_PURPOSE_INVALID"),
        "FEDERATION_SIGNER_PURPOSE_NOT_ALLOWED" => error("FEDERATION_SIGNER_PURPOSE_NOT_ALLOWED"),
        "FEDERATION_SIGNER_CERTIFICATE_PUBLIC_KEY_INVALID" => {
            error("FEDERATION_SIGNER_CERTIFICATE_PUBLIC_KEY_INVALID")
        }
        "FEDERATION_SIGNER_CERTIFICATE_SIGNER_KEY_ID_INVALID" => {
            error("FEDERATION_SIGNER_CERTIFICATE_SIGNER_KEY_ID_INVALID")
        }
        "FEDERATION_SIGNER_CERTIFICATE_SEQUENCE_INVALID" => {
            error("FEDERATION_SIGNER_CERTIFICATE_SEQUENCE_INVALID")
        }
        "FEDERATION_SIGNER_CERTIFICATE_TIMESTAMP_INVALID" => {
            error("FEDERATION_SIGNER_CERTIFICATE_TIMESTAMP_INVALID")
        }
        "FEDERATION_SIGNER_CERTIFICATE_WINDOW_INVALID" => {
            error("FEDERATION_SIGNER_CERTIFICATE_WINDOW_INVALID")
        }
        "FEDERATION_SIGNER_CERTIFICATE_ISSUED_IN_FUTURE" => {
            error("FEDERATION_SIGNER_CERTIFICATE_ISSUED_IN_FUTURE")
        }
        "FEDERATION_SIGNER_CERTIFICATE_NOT_YET_VALID" => {
            error("FEDERATION_SIGNER_CERTIFICATE_NOT_YET_VALID")
        }
        "FEDERATION_SIGNER_CERTIFICATE_EXPIRED" => error("FEDERATION_SIGNER_CERTIFICATE_EXPIRED"),
        "FEDERATION_CERTIFICATE_VALIDATION_TIME_INVALID" => {
            error("FEDERATION_CERTIFICATE_VALIDATION_TIME_INVALID")
        }
        "FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID" => {
            error("FEDERATION_SIGNER_CERTIFICATE_SIGNATURE_INVALID")
        }
        _ => error("FEDERATION_SIGNER_CERTIFICATE_INVALID"),
    }
}
