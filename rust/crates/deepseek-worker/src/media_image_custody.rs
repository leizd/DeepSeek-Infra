//! Versioned private image manifests. Only the Rust effect-journal writer uses it.
use crate::{
    media_image::{Identity, Image, LibrarySource},
    media_library::reject_links,
    media_values::invalid,
};
use deepseek_protocol::generated::deepseek::media::v1::MediaPatchClaim;
use prost::Message as _;
use rusqlite::{Connection, OptionalExtension, params};
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
};
use tonic::Status;

pub(super) const MARKER: &[u8] = b"media-library-image-ref.v2\0";
pub(super) const SCHEMA: &[&str] = &[
    "CREATE TABLE media_image_manifests (operation_id TEXT PRIMARY KEY, claim BLOB NOT NULL, before_bytes INTEGER NOT NULL CHECK(before_bytes>=0), before_sha256 BLOB NOT NULL CHECK(length(before_sha256)=32), after_bytes INTEGER CHECK(after_bytes>=0), after_sha256 BLOB, phase TEXT NOT NULL CHECK(phase IN ('RETAINING','SEALED','COLLECTED')), collected_outcome_sha256 BLOB, CHECK((after_bytes IS NULL AND after_sha256 IS NULL) OR (after_bytes IS NOT NULL AND after_sha256 IS NOT NULL AND length(after_sha256)=32)), CHECK((phase='COLLECTED' AND collected_outcome_sha256 IS NOT NULL AND length(collected_outcome_sha256)=32) OR (phase!='COLLECTED' AND collected_outcome_sha256 IS NULL))) STRICT",
    "CREATE INDEX media_image_pending ON media_image_manifests(phase,operation_id) WHERE phase!='COLLECTED'",
    "CREATE TABLE media_image_gc_cursors (phase TEXT PRIMARY KEY CHECK(phase IN ('RETAINING','SEALED')), operation_id TEXT NOT NULL) STRICT",
    "CREATE TRIGGER media_image_manifests_no_update BEFORE UPDATE ON media_image_manifests WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.claim IS NOT OLD.claim OR NEW.before_bytes IS NOT OLD.before_bytes OR NEW.before_sha256 IS NOT OLD.before_sha256 OR NEW.after_bytes IS NOT OLD.after_bytes OR NEW.after_sha256 IS NOT OLD.after_sha256 OR NOT ((OLD.phase='RETAINING' AND NEW.phase='SEALED' AND EXISTS(SELECT 1 FROM media_operations WHERE operation_id=OLD.operation_id AND claim=OLD.claim)) OR (OLD.phase='SEALED' AND NEW.phase='COLLECTED' AND EXISTS(SELECT 1 FROM media_operations WHERE operation_id=OLD.operation_id AND state IN ('APPLIED','REFUSED') AND outcome IS NOT NULL))) BEGIN SELECT RAISE(ABORT,'immutable media image'); END",
    "CREATE TRIGGER media_image_manifests_no_delete BEFORE DELETE ON media_image_manifests WHEN OLD.phase!='RETAINING' OR EXISTS(SELECT 1 FROM media_operations WHERE operation_id=OLD.operation_id) BEGIN SELECT RAISE(ABORT,'immutable media image'); END",
    "CREATE TRIGGER media_image_manifests_no_replace BEFORE INSERT ON media_image_manifests WHEN EXISTS(SELECT 1 FROM media_image_manifests WHERE operation_id=NEW.operation_id OR rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable media image'); END",
    "CREATE TRIGGER media_image_manifests_sealed BEFORE INSERT ON media_image_manifests WHEN EXISTS(SELECT 1 FROM media_operations WHERE operation_id=NEW.operation_id) BEGIN SELECT RAISE(ABORT,'sealed media image'); END",
];

pub(super) struct Manifest {
    pub claim: Vec<u8>,
    pub before: Identity,
    pub after: Option<Identity>,
    pub phase: String,
    pub collected_outcome_sha256: Option<Vec<u8>>,
}
// This is an inert descriptor. Terminal replay must not depend on permissions,
// links or presence in the image directory. File users enforce those checks.
pub(super) fn directory(root: &Path, claim: &MediaPatchClaim) -> Result<PathBuf, Status> {
    let intent = claim.intent.as_ref().ok_or_else(invalid)?;
    if claim
        .fence
        .as_ref()
        .is_none_or(|f| f.action_id != intent.operation_id || f.execution_epoch != 1)
        || intent.operation_id.len() > 128
        || claim.request_sha256.len() != 32
    {
        return Err(invalid());
    }
    let mut original = claim.clone();
    original.state = "CLAIMED".into();
    let hash = format!("{:x}", Sha256::digest(original.encode_to_vec()));
    Ok(root.join(hash))
}
pub(super) fn retain(
    root: &Path,
    claim: &MediaPatchClaim,
    before: &Image,
    after: Option<&Image>,
) -> Result<(), Status> {
    let directory = directory(root, claim)?;
    before.retain(&directory.join("before.json"))?;
    if let Some(after) = after {
        after.retain(&directory.join("after.json"))?;
    }
    Ok(())
}
// Commit the RETAINING ownership row before copying either image. The same
// exclusive workspace gate covers reservation, copying and sealing. Recovery
// may retire an unsealed private preparation, never infer a public effect.
pub(super) fn reserve(
    connection: &Connection,
    claim: &MediaPatchClaim,
    before: &Image,
    after: Option<&Image>,
) -> Result<(), Status> {
    let before = before.identity();
    let after = after.map(LibrarySource::identity);
    before.validate()?;
    if let Some(after) = &after {
        after.validate()?;
    }
    let mut original = claim.clone();
    original.state = "CLAIMED".into();
    let operation = &original.intent.as_ref().ok_or_else(invalid)?.operation_id;
    connection
        .execute(
            "INSERT INTO media_image_manifests VALUES(?1,?2,?3,?4,?5,?6,'RETAINING',NULL)",
            params![
                operation,
                original.encode_to_vec(),
                i64::try_from(before.bytes).map_err(|_| invalid())?,
                before.sha256,
                after
                    .as_ref()
                    .map(|v| i64::try_from(v.bytes))
                    .transpose()
                    .map_err(|_| invalid())?,
                after.as_ref().map(|v| v.sha256.as_slice())
            ],
        )
        .map_err(|_| invalid())?;
    Ok(())
}
pub(super) fn seal(connection: &Connection, operation: &str) -> Result<(), Status> {
    let count = connection.execute(
        "UPDATE media_image_manifests SET phase='SEALED' WHERE operation_id=?1 AND phase='RETAINING'",
        [operation],
    ).map_err(|_| invalid())?;
    if count != 1 {
        return Err(invalid());
    }
    Ok(())
}
pub(super) fn manifest(
    connection: &Connection,
    operation: &str,
) -> Result<Option<Manifest>, Status> {
    let row=connection.query_row("SELECT claim,before_bytes,before_sha256,after_bytes,after_sha256,phase,collected_outcome_sha256 FROM media_image_manifests WHERE operation_id=?1",[operation],|r|
        Ok((r.get::<_,Vec<u8>>(0)?,r.get::<_,u64>(1)?,r.get::<_,Vec<u8>>(2)?,r.get::<_,Option<u64>>(3)?,r.get::<_,Option<Vec<u8>>>(4)?,r.get::<_,String>(5)?,r.get::<_,Option<Vec<u8>>>(6)?)))
        .optional().map_err(|_|invalid())?;
    row.map(
        |(claim, bytes, sha256, after_bytes, after_sha, phase, collected_outcome_sha256)| {
            let before = Identity { bytes, sha256 };
            before.validate()?;
            let after = match (after_bytes, after_sha) {
                (None, None) => None,
                (Some(bytes), Some(sha256)) => {
                    let v = Identity { bytes, sha256 };
                    v.validate()?;
                    Some(v)
                }
                _ => return Err(invalid()),
            };
            Ok(Manifest {
                claim,
                before,
                after,
                phase,
                collected_outcome_sha256,
            })
        },
    )
    .transpose()
}
pub(super) fn images(
    root: &Path,
    claim: &MediaPatchClaim,
    manifest: Manifest,
) -> Result<(Image, Option<Image>), Status> {
    let directory = directory(root, claim)?;
    Ok((
        Image::Retained {
            path: directory.join("before.json"),
            identity: manifest.before,
        },
        manifest.after.map(|identity| Image::Retained {
            path: directory.join("after.json"),
            identity,
        }),
    ))
}
// The caller must first verify a durable terminal receipt AND retained result
// custody. Missing raw images are a legitimate post-GC condition only there.
pub(super) fn collect(root: &Path, claim: &MediaPatchClaim) -> Result<(), Status> {
    let directory = directory(root, claim)?;
    reject_links(&directory)?;
    for filename in ["before.json", "after.json"] {
        let path = directory.join(filename);
        reject_links(&path)?;
        match fs::remove_file(&path) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => return Err(invalid()),
        }
    }
    if directory.exists() {
        crate::media_image::sync_directory(&directory)?;
    }
    match fs::remove_dir(&directory) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return Err(invalid()),
    }
    if root.exists() {
        crate::media_image::sync_directory(root)?;
    }
    Ok(())
}

// A bounded retry queue, independent of the number of completed operations.
// Cursors advance even past failed cleanups; one inaccessible directory must
// not indefinitely prevent retirement of other operations' full images.
pub(super) fn pending(
    connection: &Connection,
    phase: &str,
    cursor: &str,
) -> Result<Vec<String>, Status> {
    let mut statement = connection.prepare(
        "SELECT m.operation_id FROM media_image_manifests m LEFT JOIN media_operations o USING(operation_id) WHERE m.phase=?1 AND m.phase!='COLLECTED' AND m.operation_id>?2 AND ((m.phase='RETAINING' AND o.operation_id IS NULL) OR (m.phase='SEALED' AND o.state IN ('APPLIED','REFUSED'))) ORDER BY m.operation_id LIMIT 4"
    ).map_err(|_| invalid())?;
    statement
        .query_map(params![phase, cursor], |r| r.get(0))
        .map_err(|_| invalid())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| invalid())
}
