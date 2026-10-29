//! Purpose-bound media publication receipts; no generic signing oracle.
use super::*;
use deepseek_protocol::generated::deepseek::media::v1::{MediaPatchEffectState, MediaPatchOutcome};
use prost::Message as _;

impl ControlSigningKey {
    pub fn sign_media_patch_outcome(
        &self,
        outcome: &MediaPatchOutcome,
    ) -> Result<Vec<u8>, FederationIdentityError> {
        let invalid = || error("MEDIA_OUTCOME_INVALID");
        let fence = outcome.original_fence.as_ref().ok_or_else(invalid)?;
        deepseek_protocol::validate_fence(fence).map_err(|_| invalid())?;
        let applied = outcome.state == MediaPatchEffectState::Applied as i32;
        let refused = outcome.state == MediaPatchEffectState::Refused as i32;
        // The retained v1 envelope also carries DELETE receipts. Bind its
        // distinct result shape to the operation already included in the
        // signature; allowing an absent PATCH record would erase that boundary.
        let deletion = outcome.operation_id.starts_with("media-delete-");
        let patch = outcome.operation_id.starts_with("media-patch-");
        let applied_shape = if deletion {
            outcome.media.is_none() && outcome.deleted_count <= 1
        } else {
            outcome.media.is_some() && outcome.deleted_count == 0
        };
        if outcome.schema_version != 1
            || !(patch || deletion)
            || outcome.operation_id != fence.action_id
            || outcome.operation_id.len() > 128
            || outcome.request_sha256.len() != 32
            || outcome.before_sha256.len() != 32
            || outcome.writer_fencing_token < 1
            || self.binding["fleetId"].as_str() != Some(outcome.fleet_id.as_str())
            || self.binding["environment"].as_str() != Some(outcome.environment.as_str())
            || !outcome.signature.is_empty()
            || !(applied || refused)
            || (applied
                && (outcome.http_status != 200
                    || !applied_shape
                    || outcome.after_sha256.len() != 32
                    || !outcome.error_code.is_empty()
                    || !outcome.error_message.is_empty()))
            || (refused
                && (!(400..500).contains(&outcome.http_status)
                    || outcome.media.is_some()
                    || outcome.deleted_count != 0
                    || !outcome.after_sha256.is_empty()
                    || outcome.error_code.is_empty()))
        {
            return Err(invalid());
        }
        let bytes = outcome.encode_to_vec();
        if bytes.len() > 2 * 1024 * 1024 {
            return Err(invalid());
        }
        let mut message = b"deepseek-infra:media-patch-outcome-v1\0".to_vec();
        message.extend(bytes);
        Ok(self.key.sign(&message).to_bytes().to_vec())
    }
}
