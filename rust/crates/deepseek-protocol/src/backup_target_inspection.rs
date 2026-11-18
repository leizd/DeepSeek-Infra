//! A read observation cannot substitute for admission or effect reconciliation.
use crate::generated::deepseek::action::v1::{
    BackupTargetLocationState, FilesystemBackupTargetLocationObservation,
    FilesystemBackupTargetObservation, TargetMarkerClaim,
};
use prost::Message as _;

pub const SIGNATURE_DOMAIN: &[u8] = b"deepseek-infra:backup-target-inspection-v1\0";
pub const MAX_MARKER_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_RECEIPT_BYTES: usize = MAX_MARKER_BYTES + 128 * 1024;
pub const LOCATION_SIGNATURE_DOMAIN: &[u8] = b"deepseek-infra:backup-target-location-v1\0";
pub const MARKER_EFFECT_SIGNATURE_DOMAIN: &[u8] =
    b"deepseek-infra:backup-target-marker-effect-v1\0";
pub const MARKER_INITIALIZATION_MAX_JSON: usize = 4096;
pub const MARKER_CLAIM_MAX_WIRE: usize = 96 * 1024;
pub const MARKER_EFFECT_MAX_WIRE: usize = 64 * 1024;

pub fn valid_marker_claim(value: &TargetMarkerClaim) -> bool {
    let Some(fence) = value.fence.as_ref() else {
        return false;
    };
    let abs_path = |path: &str| {
        path.len() <= 32768 && !path.contains('\0') && std::path::Path::new(path).is_absolute()
    };
    value.schema_version == 1
        && crate::validate_fence(fence).is_ok()
        && fence.action_id == value.operation_id
        && fence.execution_epoch <= i64::MAX as u64
        && value.operation_id.starts_with("target-inspect-public-v1-")
        && value.operation_id.len() <= 128
        && value
            .operation_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        && value.writer_fencing_token > 0
        && value.writer_fencing_token <= i64::MAX as u64
        && value.cutover_revision > 0
        && value.cutover_revision <= i64::MAX as u64
        && value.issued_at_unix > 0
        && (1..=30).contains(&value.lease_until_unix.saturating_sub(value.issued_at_unix))
        && abs_path(&value.requested_path)
        && abs_path(&value.canonical_path)
        && value.target_id.strip_prefix("target_").is_some_and(|tail| {
            tail.len() == 12
                && tail
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        && value.request_sha256.len() == 32
        && value.nonce.len() == 32
        && value.directory_identity.len() == 32
        && !value.fleet_id.is_empty()
        && !value.environment.is_empty()
        && value.encoded_len() <= MARKER_CLAIM_MAX_WIRE
}

pub fn valid_location_observation_shape(value: &FilesystemBackupTargetLocationObservation) -> bool {
    let Some(fence) = value.fence.as_ref() else {
        return false;
    };
    value.schema_version == 1
        && crate::validate_fence(fence).is_ok()
        && fence.execution_epoch <= i64::MAX as u64
        && value.operation_id == fence.action_id
        && value.operation_id.len() <= 128
        && value.operation_id.starts_with("target-inspect-")
        && value.writer_fencing_token > 0
        && value.writer_fencing_token <= i64::MAX as u64
        && !value.requested_path.is_empty()
        && value.requested_path.len() <= 32768
        && !value.requested_path.contains('\0')
        && std::path::Path::new(&value.requested_path).is_absolute()
        && (if value.canonical_path.is_empty() {
            matches!(
                BackupTargetLocationState::try_from(value.state),
                Ok(BackupTargetLocationState::Exists | BackupTargetLocationState::Missing)
            )
        } else {
            value.state == BackupTargetLocationState::Unspecified as i32
                && value.canonical_path.len() <= 32768
                && !value.canonical_path.contains('\0')
                && std::path::Path::new(&value.canonical_path).is_absolute()
        })
        && value.observed_at_unix > 0
        && !value.fleet_id.is_empty()
        && !value.environment.is_empty()
        && value.encoded_len() <= 128 * 1024
}

pub fn valid_observation_shape(value: &FilesystemBackupTargetObservation) -> bool {
    let Some(fence) = value.fence.as_ref() else {
        return false;
    };
    value.schema_version == 1
        && crate::validate_fence(fence).is_ok()
        && value.operation_id == fence.action_id
        && value.operation_id.len() <= 128
        && value.operation_id.starts_with("target-inspect-")
        && value.writer_fencing_token > 0
        && value.writer_fencing_token <= i64::MAX as u64
        && !value.requested_path.is_empty()
        && value.requested_path.len() <= 32768
        && !value.canonical_path.is_empty()
        && value.canonical_path.len() <= 32768
        && value.observed_at_unix > 0
        && !value.fleet_id.is_empty()
        && !value.environment.is_empty()
        && value.encoded_len() <= MAX_RECEIPT_BYTES
        && (value.directory_identity.is_empty()
            || (value.full_containment_checked && value.directory_identity.len() == 32))
        && if value.marker_present {
            !value.marker_json.is_empty()
                && value.marker_json.len() <= MAX_MARKER_BYTES
                && value.marker_sha256.len() == 32
        } else {
            value.marker_json.is_empty() && value.marker_sha256.is_empty()
        }
}
