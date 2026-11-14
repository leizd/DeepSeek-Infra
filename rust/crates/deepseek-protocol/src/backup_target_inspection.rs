//! A read observation cannot substitute for admission or effect reconciliation.
use crate::generated::deepseek::action::v1::{
    BackupTargetLocationState, FilesystemBackupTargetLocationObservation,
    FilesystemBackupTargetObservation,
};
use prost::Message as _;

pub const SIGNATURE_DOMAIN: &[u8] = b"deepseek-infra:backup-target-inspection-v1\0";
pub const MAX_MARKER_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_RECEIPT_BYTES: usize = MAX_MARKER_BYTES + 128 * 1024;
pub const LOCATION_SIGNATURE_DOMAIN: &[u8] = b"deepseek-infra:backup-target-location-v1\0";

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
        && if value.marker_present {
            !value.marker_json.is_empty()
                && value.marker_json.len() <= MAX_MARKER_BYTES
                && value.marker_sha256.len() == 32
        } else {
            value.marker_json.is_empty() && value.marker_sha256.is_empty()
        }
}
