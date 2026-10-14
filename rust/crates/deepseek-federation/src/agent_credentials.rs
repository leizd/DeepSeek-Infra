//! Rust-only, request-bound provider credentials. Public request objects, Go
//! metadata and execution receipts contain no plaintext credential fields.
use crate::canonical::{canonical_bytes, encode_b64url};
use crate::custody::{FederationIdentityError, error};
use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use hkdf::Hkdf;
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use zeroize::Zeroizing;

const SCHEMA: &str = "native-agent-provider-credentials-v1";
const KEY_DOMAIN: &[u8] = b"deepseek-infra:agent-provider-credential-encryption-v1\0";
const AAD_DOMAIN: &[u8] = b"deepseek-infra:agent-provider-credential-scope-v1\0";
const MAX_KEY_BYTES: usize = 4096;
const MAX_CIPHERTEXT_BYTES: usize = 16_384;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AgentCredentialScope {
    pub fleet_id: String,
    pub environment: String,
    pub run_id: String,
    pub metadata_epoch: u64,
    pub runtime_revision: u64,
    pub request_sha256: String,
    pub request_length: u64,
}

pub struct AgentProviderCredentials {
    api_key: Option<Zeroizing<String>>,
    tavily_api_key: Option<Zeroizing<String>>,
}

impl std::fmt::Debug for AgentProviderCredentials {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AgentProviderCredentials")
            .field("api_key_present", &self.api_key.is_some())
            .field("tavily_api_key_present", &self.tavily_api_key.is_some())
            .finish()
    }
}

impl AgentProviderCredentials {
    pub fn from_keys(
        api_key: Option<&str>,
        tavily_api_key: Option<&str>,
    ) -> Result<Self, FederationIdentityError> {
        fn key(value: Option<&str>) -> Result<Option<Zeroizing<String>>, FederationIdentityError> {
            value
                .map(|value| {
                    let value = value.trim();
                    if value.is_empty()
                        || value.len() > MAX_KEY_BYTES
                        || value.chars().any(char::is_control)
                    {
                        return Err(error("AGENT_PROVIDER_CREDENTIAL_INVALID"));
                    }
                    Ok(Zeroizing::new(value.to_owned()))
                })
                .transpose()
        }
        let result = Self {
            api_key: key(api_key)?,
            tavily_api_key: key(tavily_api_key)?,
        };
        if result.api_key.is_none() && result.tavily_api_key.is_none() {
            return Err(error("AGENT_PROVIDER_CREDENTIAL_INVALID"));
        }
        Ok(result)
    }

    pub fn api_key(&self) -> Option<&str> {
        self.api_key.as_ref().map(|value| value.as_str())
    }

    pub fn tavily_api_key(&self) -> Option<&str> {
        self.tavily_api_key.as_ref().map(|value| value.as_str())
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SealedAgentCredentials {
    schema: String,
    scope: AgentCredentialScope,
    signer_public_key: String,
    nonce: String,
    ciphertext: String,
}

impl std::fmt::Debug for SealedAgentCredentials {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SealedAgentCredentials")
            .field("schema", &self.schema)
            .field("scope", &self.scope)
            .field("signer_public_key", &self.signer_public_key)
            .finish()
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct BorrowedCredentials<'a> {
    api_key: Option<&'a str>,
    tavily_api_key: Option<&'a str>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OwnedCredentials {
    api_key: Option<String>,
    tavily_api_key: Option<String>,
}

fn validate_scope(
    binding: &serde_json::Value,
    scope: &AgentCredentialScope,
) -> Result<(), FederationIdentityError> {
    let valid_id = |value: &str| {
        (1..=128).contains(&value.len())
            && value
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
            && value.as_bytes()[0].is_ascii_alphanumeric()
    };
    if !valid_id(&scope.run_id)
        || scope.metadata_epoch == 0
        || scope.metadata_epoch > i64::MAX as u64
        || scope.runtime_revision == 0
        || scope.runtime_revision > i64::MAX as u64
        || scope.request_sha256.len() != 64
        || !scope
            .request_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        || !(1..=1_048_576).contains(&scope.request_length)
        || binding.get("fleetId").and_then(serde_json::Value::as_str)
            != Some(scope.fleet_id.as_str())
        || binding
            .get("environment")
            .and_then(serde_json::Value::as_str)
            != Some(scope.environment.as_str())
    {
        return Err(error("AGENT_PROVIDER_CREDENTIAL_SCOPE_INVALID"));
    }
    Ok(())
}

fn cipher(seed: &[u8; 32]) -> Result<Aes256Gcm, FederationIdentityError> {
    let mut key = Zeroizing::new([0u8; 32]);
    Hkdf::<Sha256>::new(Some(KEY_DOMAIN), seed)
        .expand(KEY_DOMAIN, key.as_mut())
        .map_err(|_| error("AGENT_PROVIDER_CREDENTIAL_UNAVAILABLE"))?;
    Aes256Gcm::new_from_slice(key.as_ref())
        .map_err(|_| error("AGENT_PROVIDER_CREDENTIAL_UNAVAILABLE"))
}

fn aad(
    scope: &AgentCredentialScope,
    signer_public_key: &str,
) -> Result<Vec<u8>, FederationIdentityError> {
    let mut value = AAD_DOMAIN.to_vec();
    value.extend(
        canonical_bytes(
            &serde_json::json!({"schema":SCHEMA,"scope":scope,"signerPublicKey":signer_public_key}),
        )
        .map_err(|_| error("AGENT_PROVIDER_CREDENTIAL_SCOPE_INVALID"))?,
    );
    Ok(value)
}

pub(crate) fn seal(
    seed: &[u8; 32],
    signer_public_key: &str,
    binding: &serde_json::Value,
    scope: &AgentCredentialScope,
    credentials: &AgentProviderCredentials,
) -> Result<SealedAgentCredentials, FederationIdentityError> {
    validate_scope(binding, scope)?;
    let plaintext = Zeroizing::new(
        serde_json::to_vec(&BorrowedCredentials {
            api_key: credentials.api_key(),
            tavily_api_key: credentials.tavily_api_key(),
        })
        .map_err(|_| error("AGENT_PROVIDER_CREDENTIAL_INVALID"))?,
    );
    let mut nonce = [0u8; 12];
    getrandom::getrandom(&mut nonce).map_err(|_| error("AGENT_PROVIDER_CREDENTIAL_UNAVAILABLE"))?;
    let ciphertext = cipher(seed)?
        .encrypt(
            Nonce::from_slice(&nonce),
            Payload {
                msg: plaintext.as_ref(),
                aad: &aad(scope, signer_public_key)?,
            },
        )
        .map_err(|_| error("AGENT_PROVIDER_CREDENTIAL_UNAVAILABLE"))?;
    Ok(SealedAgentCredentials {
        schema: SCHEMA.to_owned(),
        scope: scope.clone(),
        signer_public_key: signer_public_key.to_owned(),
        nonce: encode_b64url(&nonce),
        ciphertext: encode_b64url(&ciphertext),
    })
}

pub(crate) fn open(
    seed: &[u8; 32],
    signer_public_key: &str,
    binding: &serde_json::Value,
    scope: &AgentCredentialScope,
    sealed: &SealedAgentCredentials,
) -> Result<AgentProviderCredentials, FederationIdentityError> {
    validate_scope(binding, scope)?;
    if sealed.schema != SCHEMA
        || sealed.scope != *scope
        || sealed.signer_public_key != signer_public_key
        || sealed.nonce.len() != 16
        || sealed.ciphertext.len() > MAX_CIPHERTEXT_BYTES * 2
    {
        return Err(error("AGENT_PROVIDER_CREDENTIAL_SCOPE_INVALID"));
    }
    let nonce = URL_SAFE_NO_PAD
        .decode(&sealed.nonce)
        .map_err(|_| error("AGENT_PROVIDER_CREDENTIAL_INVALID"))?;
    let ciphertext = URL_SAFE_NO_PAD
        .decode(&sealed.ciphertext)
        .map_err(|_| error("AGENT_PROVIDER_CREDENTIAL_INVALID"))?;
    if nonce.len() != 12 || !(16..=MAX_CIPHERTEXT_BYTES).contains(&ciphertext.len()) {
        return Err(error("AGENT_PROVIDER_CREDENTIAL_INVALID"));
    }
    let plaintext = Zeroizing::new(
        cipher(seed)?
            .decrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: &ciphertext,
                    aad: &aad(scope, signer_public_key)?,
                },
            )
            .map_err(|_| error("AGENT_PROVIDER_CREDENTIAL_INVALID"))?,
    );
    let mut decoded: OwnedCredentials = serde_json::from_slice(&plaintext)
        .map_err(|_| error("AGENT_PROVIDER_CREDENTIAL_INVALID"))?;
    let api_key = decoded.api_key.take().map(Zeroizing::new);
    let tavily_api_key = decoded.tavily_api_key.take().map(Zeroizing::new);
    AgentProviderCredentials::from_keys(
        api_key.as_ref().map(|key| key.as_str()),
        tavily_api_key.as_ref().map(|key| key.as_str()),
    )
}

#[cfg(test)]
mod tests;
