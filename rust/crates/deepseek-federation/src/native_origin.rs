//! Typed new-profile proofs. These purposes cannot sign migration documents or
//! authorize effects. Publication always refers to the original birth bytes.
use super::ControlSigningKey;
use crate::canonical::{canonical_bytes, parse_timestamp};
use crate::custody::{FederationIdentityError, error};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_protocol::generated::deepseek::action::v1::{
    NativeOriginEvidence, NativeOriginPublication, NativeOriginReceipt,
};
use deepseek_protocol::generated::deepseek::common::v1::ActionFence;
use ed25519_dalek::{Signature, Signer as _, VerifyingKey};
use prost::Message as _;
use serde_json::json;
use sha2::{Digest, Sha256};

const BIRTH: &[u8] = b"deepseek-infra:native-control-origin:v1\0";
const PUBLICATION: &[u8] = b"deepseek-infra:native-control-origin-publication:v1\0";
fn invalid() -> FederationIdentityError {
    error("NATIVE_ORIGIN_INVALID")
}
fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn checkpoint_digest(created_at: &str) -> Result<String, FederationIdentityError> {
    let payload = json!({"schema":"control-authority-v1", "authorityGeneration":1,
        "createdAt":created_at, "controlSchemaVersion":18, "policies":[], "targets":[],
        "receiptMutationGenerations":{}, "promotionEpochs":{}, "drainGenerations":{},
        "placementGenerations":{}});
    let payload_digest = digest(&canonical_bytes(&payload).map_err(|_| invalid())?);
    Ok(digest(
        &canonical_bytes(&json!({"schema":"control-authority-v1",
        "authorityGeneration":1, "previousDigest":null, "payloadDigest":payload_digest}))
        .map_err(|_| invalid())?,
    ))
}
fn validate(e: &NativeOriginEvidence) -> Result<(), FederationIdentityError> {
    let fence = e.fence.as_ref().ok_or_else(invalid)?;
    if e.schema_version != 1
        || e.control_schema_version != 18
        || !hex(&e.profile_id, 32)
        || !hex(&e.incarnation_id, 64)
        || e.fleet_id != format!("native-{}", e.profile_id)
        || e.environment != "local"
        || fence.action_id != format!("native-origin:{}", e.profile_id)
        || fence.execution_epoch != 1
        || parse_timestamp(&e.created_at).is_none_or(|time| time <= 0)
        || e.authority_digest != checkpoint_digest(&e.created_at)?
    {
        return Err(invalid());
    }
    Ok(())
}
fn verify(
    public: &str,
    domain: &[u8],
    payload: &[u8],
    signature: &[u8],
) -> Result<(), FederationIdentityError> {
    let bytes: [u8; 32] = URL_SAFE_NO_PAD
        .decode(public)
        .map_err(|_| invalid())?
        .try_into()
        .map_err(|_| invalid())?;
    let key = VerifyingKey::from_bytes(&bytes).map_err(|_| invalid())?;
    let signature = Signature::from_slice(signature).map_err(|_| invalid())?;
    let mut message = domain.to_vec();
    message.extend(payload);
    key.verify_strict(&message, &signature)
        .map_err(|_| invalid())
}
pub fn verify_native_origin(origin: &NativeOriginReceipt) -> Result<(), FederationIdentityError> {
    let e = origin.evidence.as_ref().ok_or_else(invalid)?;
    validate(e)?;
    verify(
        &e.signer_public_key,
        BIRTH,
        &e.encode_to_vec(),
        &origin.signature,
    )
}
pub fn verify_native_origin_publication(
    origin: &NativeOriginReceipt,
    publication: &NativeOriginPublication,
) -> Result<(), FederationIdentityError> {
    verify_native_origin(origin)?;
    let e = origin.evidence.as_ref().ok_or_else(invalid)?;
    if publication.schema_version != 1
        || publication.fence != e.fence
        || publication.origin_sha256 != digest(&origin.encode_to_vec())
        || publication.authority_digest != e.authority_digest
    {
        return Err(invalid());
    }
    let mut unsigned = publication.clone();
    unsigned.signature.clear();
    verify(
        &e.signer_public_key,
        PUBLICATION,
        &unsigned.encode_to_vec(),
        &publication.signature,
    )
}
impl ControlSigningKey {
    /// Only the exclusive new-custody initializer calls this constructor.
    pub fn create_native_origin(
        &self,
        profile_id: &str,
        incarnation_id: &str,
        created_at: &str,
    ) -> Result<NativeOriginReceipt, FederationIdentityError> {
        let evidence = NativeOriginEvidence {
            schema_version: 1,
            fence: Some(ActionFence {
                action_id: format!("native-origin:{profile_id}"),
                execution_epoch: 1,
            }),
            profile_id: profile_id.into(),
            incarnation_id: incarnation_id.into(),
            fleet_id: format!("native-{profile_id}"),
            environment: "local".into(),
            signer_public_key: self.public_key(),
            created_at: created_at.into(),
            control_schema_version: 18,
            authority_digest: checkpoint_digest(created_at)?,
        };
        validate(&evidence)?;
        if self.binding["fleetId"] != evidence.fleet_id
            || self.binding["environment"] != evidence.environment
        {
            return Err(invalid());
        }
        let mut message = BIRTH.to_vec();
        message.extend(evidence.encode_to_vec());
        Ok(NativeOriginReceipt {
            evidence: Some(evidence),
            signature: self.key.sign(&message).to_bytes().to_vec(),
        })
    }
    /// No caller-selected document/digest is accepted for this purpose.
    pub fn sign_native_origin_publication(
        &self,
        origin: &NativeOriginReceipt,
    ) -> Result<NativeOriginPublication, FederationIdentityError> {
        verify_native_origin(origin)?;
        let e = origin.evidence.as_ref().ok_or_else(invalid)?;
        if e.signer_public_key != self.public_key()
            || self.binding["fleetId"] != e.fleet_id
            || self.binding["environment"] != e.environment
        {
            return Err(invalid());
        }
        let mut publication = NativeOriginPublication {
            schema_version: 1,
            fence: e.fence.clone(),
            origin_sha256: digest(&origin.encode_to_vec()),
            authority_digest: e.authority_digest.clone(),
            signature: vec![],
        };
        let mut message = PUBLICATION.to_vec();
        message.extend(publication.encode_to_vec());
        publication.signature = self.key.sign(&message).to_bytes().to_vec();
        Ok(publication)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_origin_purposes_are_bound_and_not_interchangeable() {
        let profile = "0123456789abcdef0123456789abcdef";
        let password = b"isolated-native-origin-test-passphrase";
        let bundle =
            crate::create_control_signer_bundle(password, &format!("native-{profile}"), "local")
                .unwrap();
        let key = crate::load_control_signer(
            &bundle,
            password,
            &format!("native-{profile}"),
            "local",
            bundle["binding"]["signerPublicKey"].as_str().unwrap(),
        )
        .unwrap();
        let origin = key
            .create_native_origin(profile, &"a".repeat(64), "2026-10-21T01:00:00Z")
            .unwrap();
        verify_native_origin(&origin).unwrap();
        let publication = key.sign_native_origin_publication(&origin).unwrap();
        verify_native_origin_publication(&origin, &publication).unwrap();
        let mut wrong = origin.clone();
        wrong
            .evidence
            .as_mut()
            .unwrap()
            .fence
            .as_mut()
            .unwrap()
            .execution_epoch = 2;
        assert!(verify_native_origin(&wrong).is_err());
        assert!(key.sign_native_origin_publication(&wrong).is_err());
        wrong = origin.clone();
        wrong.signature = publication.signature.clone();
        assert!(verify_native_origin(&wrong).is_err());
        wrong = origin.clone();
        wrong.evidence.as_mut().unwrap().incarnation_id = "b".repeat(64);
        assert!(verify_native_origin_publication(&wrong, &publication).is_err());
        let mut tampered = publication.clone();
        tampered.authority_digest = "f".repeat(64);
        assert!(verify_native_origin_publication(&origin, &tampered).is_err());
        assert!(
            key.create_native_origin(profile, &"a".repeat(64), "not-a-date")
                .is_err()
        );
        assert!(
            key.create_native_origin("foreign-profile", &"a".repeat(64), "2026-10-21T01:00:00Z")
                .is_err()
        );
    }
}
