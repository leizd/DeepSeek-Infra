//! Versions and binding domains for compact media PATCH results.
use crate::generated::deepseek::media::v1::{MediaMutationKind, MediaPatchIntent};

pub const COMPACT_VERSION: u32 = 2;
pub const FLAT_VERSION: u32 = 4;

pub fn signature_domain(version: u32) -> Option<&'static [u8]> {
    match version {
        COMPACT_VERSION => Some(b"deepseek-infra:media-patch-outcome-v2\x00"),
        FLAT_VERSION => Some(b"deepseek-infra:media-patch-outcome-v4\x00"),
        _ => None,
    }
}

pub fn preparation_schema(version: u32) -> Option<u32> {
    match version {
        COMPACT_VERSION => Some(1),
        FLAT_VERSION => Some(2),
        _ => None,
    }
}

pub fn negotiated(intent: &MediaPatchIntent, version: u32) -> bool {
    if intent.kind != MediaMutationKind::Unspecified as i32 || intent.patch.is_none() {
        return false;
    }
    match version {
        COMPACT_VERSION => matches!(
            intent.max_outcome_schema_version,
            COMPACT_VERSION | crate::media_server_error::VERSION | FLAT_VERSION
        ),
        FLAT_VERSION => intent.max_outcome_schema_version == FLAT_VERSION,
        _ => false,
    }
}
