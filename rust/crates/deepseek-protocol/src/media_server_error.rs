//! Signed terminal HTTP 500 receipts using the existing media outcome envelope.
use crate::generated::deepseek::media::v1::{
    MediaMutationKind, MediaPatchEffectState, MediaPatchIntent, MediaPatchOutcome,
};

pub const VERSION: u32 = 3;
pub const SIGNATURE_DOMAIN: &[u8] = b"deepseek-infra:media-patch-outcome-v3\x00";
pub const CODE: &str = "internal";
pub const MESSAGE: &str = "Server error";

pub fn negotiated(intent: &MediaPatchIntent) -> bool {
    intent.max_outcome_schema_version == VERSION
        && (intent.kind == MediaMutationKind::Unspecified as i32
            || intent.kind == MediaMutationKind::Delete as i32)
}

pub fn valid_shape(outcome: &MediaPatchOutcome) -> bool {
    let Some(fence) = outcome.original_fence.as_ref() else {
        return false;
    };
    let patch = outcome.operation_id.starts_with("media-patch-");
    let deletion = outcome.operation_id.starts_with("media-delete-");
    outcome.schema_version == VERSION
        && (patch || deletion)
        && crate::validate_fence(fence).is_ok()
        && fence.action_id == outcome.operation_id
        && fence.execution_epoch == 1
        && outcome.operation_id.len() <= 128
        && outcome.request_sha256.len() == 32
        && outcome.before_sha256.len() == 32
        && outcome.writer_fencing_token > 0
        && outcome.http_status == 500
        && outcome.error_code == CODE
        && outcome.error_message == MESSAGE
        && outcome.media.is_none()
        && outcome.patched.is_none()
        && outcome.created.is_none()
        && outcome.processed.is_none()
        && outcome.deleted_count == 0
        && match MediaPatchEffectState::try_from(outcome.state) {
            Ok(MediaPatchEffectState::Applied) => {
                patch
                    && outcome.after_sha256.len() == 32
                    && outcome.after_sha256 != outcome.before_sha256
            }
            Ok(MediaPatchEffectState::Refused) => outcome.after_sha256.is_empty(),
            _ => false,
        }
}
