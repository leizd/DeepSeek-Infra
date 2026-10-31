//! Durable Rust custody for one library publication, its original fence and receipt.
use crate::{
    media_library::{self, read_library, reject_links, sha},
    media_values::invalid,
};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_federation::ControlSigningKey;
use deepseek_policy::mutation_gate::{assert_mutation_allowed, exclusive_gate, mutation_scope};
use deepseek_protocol::{
    ActionFence,
    generated::deepseek::media::v1::{MediaPatchClaim, MediaPatchEffectState, MediaPatchOutcome},
};
use ed25519_dalek::{Signature, VerifyingKey};
use prost::Message as _;
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    time::Duration,
};
use tonic::Status;

const APPLICATION_ID: i64 = 0x44534d50;
const SCHEMA: &[&str] = &[
    "CREATE TABLE media_identity (id INTEGER PRIMARY KEY CHECK(id=1), workspace TEXT NOT NULL, signer TEXT NOT NULL, fleet TEXT NOT NULL, environment TEXT NOT NULL) STRICT",
    "CREATE TRIGGER media_identity_no_update BEFORE UPDATE ON media_identity BEGIN SELECT RAISE(ABORT,'immutable media identity'); END",
    "CREATE TRIGGER media_identity_no_delete BEFORE DELETE ON media_identity BEGIN SELECT RAISE(ABORT,'immutable media identity'); END",
    "CREATE TABLE media_operations (operation_id TEXT PRIMARY KEY, claim BLOB NOT NULL, preparation_sha256 BLOB NOT NULL CHECK(length(preparation_sha256)=32), before_bytes BLOB NOT NULL, after_bytes BLOB, planned BLOB NOT NULL, state TEXT NOT NULL CHECK(state IN ('PREPARED','APPLYING','APPLIED','REFUSED')), outcome BLOB, CHECK((state IN ('PREPARED','APPLYING') AND outcome IS NULL) OR (state IN ('APPLIED','REFUSED') AND length(outcome)>0))) STRICT",
    "CREATE UNIQUE INDEX media_one_publication ON media_operations((1)) WHERE state IN ('PREPARED','APPLYING')",
    "CREATE TRIGGER media_operations_no_delete BEFORE DELETE ON media_operations BEGIN SELECT RAISE(ABORT,'immutable media effect'); END",
    "CREATE TRIGGER media_operations_no_replace BEFORE INSERT ON media_operations WHEN EXISTS(SELECT 1 FROM media_operations WHERE operation_id=NEW.operation_id OR rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'immutable media effect'); END",
    "CREATE TRIGGER media_operations_identity BEFORE UPDATE ON media_operations WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.claim IS NOT OLD.claim OR NEW.preparation_sha256 IS NOT OLD.preparation_sha256 OR NEW.before_bytes IS NOT OLD.before_bytes OR NEW.after_bytes IS NOT OLD.after_bytes OR NEW.planned IS NOT OLD.planned BEGIN SELECT RAISE(ABORT,'immutable media effect'); END",
    "CREATE TRIGGER media_operations_transition BEFORE UPDATE ON media_operations WHEN NOT ((OLD.state='PREPARED' AND NEW.state IN ('APPLYING','REFUSED')) OR (OLD.state='APPLYING' AND NEW.state IN ('APPLIED','REFUSED'))) BEGIN SELECT RAISE(ABORT,'illegal media effect transition'); END",
];
pub(crate) struct MediaJournal {
    connection: Connection,
    root: PathBuf,
    public_key: String,
    creation_staging: PathBuf,
}
pub(super) struct Prepared {
    pub digest: Vec<u8>,
    pub terminal: Option<MediaPatchOutcome>,
}
struct Retained {
    claim: Vec<u8>,
    digest: Vec<u8>,
    before: Vec<u8>,
    after: Option<Vec<u8>>,
    planned: MediaPatchOutcome,
    state: String,
    outcome: Option<Vec<u8>>,
}
fn normalized_claim(c: &MediaPatchClaim) -> Vec<u8> {
    let mut copy = c.clone();
    copy.state = "CLAIMED".into();
    copy.encode_to_vec()
}
impl MediaJournal {
    pub(crate) fn open(
        root: &Path,
        state: &Path,
        key: &str,
        fleet: &str,
        environment: &str,
    ) -> Result<Self, Status> {
        reject_links(root)?;
        let root = fs::canonicalize(root).map_err(|_| invalid())?;
        let path = state.join("rust-worker/media-effects.sqlite3");
        reject_links(&path)?;
        fs::create_dir_all(path.parent().ok_or_else(invalid)?).map_err(|_| invalid())?;
        reject_links(&path)?;
        let new = match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(f) => {
                f.sync_all().map_err(|_| invalid())?;
                true
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => false,
            Err(_) => return Err(invalid()),
        };
        if !new {
            let mut header = [0u8; 100];
            fs::File::open(&path)
                .and_then(|mut f| f.read_exact(&mut header))
                .map_err(|_| invalid())?;
            if &header[..16] != b"SQLite format 3\0"
                || header[68..72] != (APPLICATION_ID as u32).to_be_bytes()
            {
                return Err(invalid());
            }
        }
        let mut connection = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_WRITE)
            .map_err(|_| invalid())?;
        connection
            .busy_timeout(Duration::from_secs(2))
            .map_err(|_| invalid())?;
        connection
            .execute_batch(
                "PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL; PRAGMA trusted_schema=OFF;",
            )
            .map_err(|_| invalid())?;
        let tx = connection.transaction().map_err(|_| invalid())?;
        if new {
            for q in SCHEMA {
                tx.execute(q, []).map_err(|_| invalid())?;
            }
            tx.pragma_update(None, "application_id", APPLICATION_ID)
                .map_err(|_| invalid())?;
            tx.pragma_update(None, "user_version", 1)
                .map_err(|_| invalid())?;
            tx.execute(
                "INSERT INTO media_identity VALUES(1,?1,?2,?3,?4)",
                params![root.to_str().ok_or_else(invalid)?, key, fleet, environment],
            )
            .map_err(|_| invalid())?;
        }
        let version: i64 = tx
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .map_err(|_| invalid())?;
        let application: i64 = tx
            .pragma_query_value(None, "application_id", |r| r.get(0))
            .map_err(|_| invalid())?;
        let mut actual = {
            let mut statement = tx
                .prepare("SELECT sql FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'")
                .map_err(|_| invalid())?;
            statement
                .query_map([], |r| r.get::<_, String>(0))
                .map_err(|_| invalid())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|_| invalid())?
        };
        let mut expected: Vec<String> = SCHEMA.iter().map(|s| s.to_string()).collect();
        actual.sort();
        expected.sort();
        if version != 1 || application != APPLICATION_ID || actual != expected {
            return Err(invalid());
        }
        let identity: (String, String, String, String) = tx
            .query_row(
                "SELECT workspace,signer,fleet,environment FROM media_identity WHERE id=1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .map_err(|_| invalid())?;
        if identity
            != (
                root.to_str().ok_or_else(invalid)?.into(),
                key.into(),
                fleet.into(),
                environment.into(),
            )
        {
            return Err(invalid());
        }
        tx.commit().map_err(|_| invalid())?;
        Ok(Self {
            connection,
            root,
            public_key: key.into(),
            creation_staging: state.join("rust-worker/media-create"),
        })
    }
    pub(super) fn creation_staging(&self) -> &Path {
        &self.creation_staging
    }
    pub(super) fn created_record(
        &self,
        claim: &MediaPatchClaim,
    ) -> Result<deepseek_protocol::generated::deepseek::media::v1::MediaObject, Status> {
        self.verify_original_claim(claim)?;
        let operation = &claim.intent.as_ref().ok_or_else(invalid)?.operation_id;
        let row = self.retained(operation)?.ok_or_else(invalid)?;
        let outcome = self.terminal(&row)?.ok_or_else(invalid)?;
        if row.state != "APPLIED" || outcome.created.is_none() {
            return Err(invalid());
        }
        crate::media_create::retained_preparation(&self.creation_staging, claim, &outcome)?
            .record
            .ok_or_else(invalid)
    }
    pub(super) fn processed_result(
        &self,
        claim: &MediaPatchClaim,
    ) -> Result<
        deepseek_protocol::generated::deepseek::media::v1::ReadMediaCreateResultResponse,
        Status,
    > {
        self.verify_original_claim(claim)?;
        let operation = &claim.intent.as_ref().ok_or_else(invalid)?.operation_id;
        let row = self.retained(operation)?.ok_or_else(invalid)?;
        let outcome = self.terminal(&row)?.ok_or_else(invalid)?;
        if row.state != "APPLIED" || outcome.processed.is_none() {
            return Err(invalid());
        }
        crate::media_process::result(&self.creation_staging, claim, &outcome)
    }
    pub(super) fn active(&self) -> Result<Option<MediaPatchClaim>, Status> {
        let bytes = self
            .connection
            .query_row(
                "SELECT claim FROM media_operations WHERE state IN ('PREPARED','APPLYING')",
                [],
                |r| r.get::<_, Vec<u8>>(0),
            )
            .optional()
            .map_err(|_| invalid())?;
        bytes
            .map(|b| MediaPatchClaim::decode(b.as_slice()).map_err(|_| invalid()))
            .transpose()
    }
    fn retained(&self, operation: &str) -> Result<Option<Retained>, Status> {
        let row=self.connection.query_row("SELECT claim,preparation_sha256,before_bytes,after_bytes,planned,state,outcome FROM media_operations WHERE operation_id=?1",[operation],|r|Ok((r.get::<_,Vec<u8>>(0)?,r.get::<_,Vec<u8>>(1)?,r.get::<_,Vec<u8>>(2)?,r.get::<_,Option<Vec<u8>>>(3)?,r.get::<_,Vec<u8>>(4)?,r.get::<_,String>(5)?,r.get::<_,Option<Vec<u8>>>(6)?))).optional().map_err(|_|invalid())?;
        let Some((claim, digest, before, after, planned, state, outcome)) = row else {
            return Ok(None);
        };
        let planned = MediaPatchOutcome::decode(planned.as_slice()).map_err(|_| invalid())?;
        let binding = MediaPatchClaim::decode(claim.as_slice()).map_err(|_| invalid())?;
        let intent = binding.intent.as_ref().ok_or_else(invalid)?;
        if planned.original_fence != binding.fence
            || intent.operation_id != operation
            || binding.request_sha256 != sha(&intent.encode_to_vec())
            || planned.request_sha256 != binding.request_sha256
            || planned.fleet_id != binding.fleet_id
            || planned.environment != binding.environment
            || planned.writer_fencing_token != binding.writer_fencing_token
            || !planned.signature.is_empty()
        {
            return Err(invalid());
        }

        if digest != sha(&planned.encode_to_vec())
            || planned.before_sha256 != sha(&before)
            || after
                .as_ref()
                .is_some_and(|bytes| planned.after_sha256 != sha(bytes))
            || planned.operation_id != operation
        {
            return Err(invalid());
        }
        Ok(Some(Retained {
            claim,
            digest,
            before,
            after,
            planned,
            state,
            outcome,
        }))
    }
    fn terminal(&self, row: &Retained) -> Result<Option<MediaPatchOutcome>, Status> {
        let Some(bytes) = row.outcome.as_ref() else {
            return Ok(None);
        };
        let result = MediaPatchOutcome::decode(bytes.as_slice()).map_err(|_| invalid())?;
        let claim = MediaPatchClaim::decode(row.claim.as_slice()).map_err(|_| invalid())?;
        if result.operation_id != row.planned.operation_id
            || result.original_fence != claim.fence
            || result.request_sha256 != claim.request_sha256
            || result.fleet_id != claim.fleet_id
            || result.environment != claim.environment
            || result.writer_fencing_token != claim.writer_fencing_token
            || (row.state == "APPLIED" && result.state != MediaPatchEffectState::Applied as i32)
            || (row.state == "REFUSED" && result.state != MediaPatchEffectState::Refused as i32)
        {
            return Err(invalid());
        }
        let public: [u8; 32] = URL_SAFE_NO_PAD
            .decode(&self.public_key)
            .map_err(|_| invalid())?
            .try_into()
            .map_err(|_| invalid())?;
        let key = VerifyingKey::from_bytes(&public).map_err(|_| invalid())?;
        let signature = Signature::from_slice(&result.signature).map_err(|_| invalid())?;
        let mut unsigned = result.clone();
        unsigned.signature.clear();
        let mut message = b"deepseek-infra:media-patch-outcome-v1\0".to_vec();
        message.extend(unsigned.encode_to_vec());
        key.verify_strict(&message, &signature)
            .map_err(|_| invalid())?;
        Ok(Some(result))
    }
    pub(super) fn prepare(&mut self, claim: &MediaPatchClaim) -> Result<Prepared, Status> {
        let intent = claim.intent.as_ref().ok_or_else(invalid)?;
        let _gate = exclusive_gate(&self.root).map_err(|_| invalid())?;
        assert_mutation_allowed(None, &self.root).map_err(|_| invalid())?;
        if let Some(row) = self.retained(&intent.operation_id)? {
            if row.claim != normalized_claim(claim) {
                return Err(invalid());
            }
            return Ok(Prepared {
                digest: row.digest.clone(),
                terminal: self.terminal(&row)?,
            });
        }
        let before = read_library(&self.root)?;
        let (planned, after) = if intent.kind
            == deepseek_protocol::generated::deepseek::media::v1::MediaMutationKind::Process as i32
        {
            crate::media_process::plan(&self.root, &self.creation_staging, claim, &before)?
        } else if intent.kind
            == deepseek_protocol::generated::deepseek::media::v1::MediaMutationKind::Create as i32
        {
            crate::media_create::plan(&self.root, &self.creation_staging, claim, &before)?
        } else {
            media_library::plan(&self.root, claim, &before)?
        };
        let digest = sha(&planned.encode_to_vec());
        self.connection
            .execute(
                "INSERT INTO media_operations VALUES(?1,?2,?3,?4,?5,?6,'PREPARED',NULL)",
                params![
                    intent.operation_id,
                    normalized_claim(claim),
                    digest,
                    before,
                    after,
                    planned.encode_to_vec()
                ],
            )
            .map_err(|_| Status::unavailable("MEDIA_PUBLICATION_UNRESOLVED"))?;
        Ok(Prepared {
            digest,
            terminal: None,
        })
    }

    pub(super) fn verify_original_claim(&self, claim: &MediaPatchClaim) -> Result<(), Status> {
        let intent = claim.intent.as_ref().ok_or_else(invalid)?;
        if let Some(row) = self.retained(&intent.operation_id)? {
            if row.claim != normalized_claim(claim) {
                return Err(invalid());
            }
        }
        Ok(())
    }

    fn finish_original_cleanup(&self, row: &Retained) -> Result<(), Status> {
        use deepseek_protocol::generated::deepseek::media::v1::MediaMutationKind;
        let claim = MediaPatchClaim::decode(row.claim.as_slice()).map_err(|_| invalid())?;
        let intent = claim.intent.as_ref().ok_or_else(invalid)?;
        match MediaMutationKind::try_from(intent.kind).map_err(|_| invalid())? {
            MediaMutationKind::Unspecified
                if intent.create.is_none()
                    && row.planned.created.is_none()
                    && intent.patch.is_some()
                    && row.planned.deleted_count == 0 =>
            {
                Ok(())
            }
            MediaMutationKind::Delete
                if intent.create.is_none()
                    && row.planned.created.is_none()
                    && intent.patch.is_none()
                    && row.planned.media.is_none()
                    && row.planned.deleted_count <= 1 =>
            {
                if row.planned.deleted_count == 1 {
                    crate::media_delete::finish(&self.root, &intent.media_id)?;
                }
                Ok(())
            }
            MediaMutationKind::Create
                if intent.patch.is_none()
                    && intent.create.is_some()
                    && row.planned.media.is_none()
                    && row.planned.deleted_count == 0
                    && row.planned.created.is_some() =>
            {
                crate::media_create::finish(
                    &self.root,
                    &self.creation_staging,
                    &claim,
                    &row.planned,
                )
            }
            _ => Err(invalid()),
        }
    }
    fn settle(
        &mut self,
        operation: &str,
        state: &str,
        mut result: MediaPatchOutcome,
        key: &ControlSigningKey,
    ) -> Result<MediaPatchOutcome, Status> {
        let noop = state == "PREPARED" && result.state == MediaPatchEffectState::Applied as i32;
        if noop {
            use deepseek_protocol::generated::deepseek::media::v1::MediaMutationKind;
            let row = self.retained(operation)?.ok_or_else(invalid)?;
            let claim = MediaPatchClaim::decode(row.claim.as_slice()).map_err(|_| invalid())?;
            let intent = claim.intent.as_ref().ok_or_else(invalid)?;
            if row.after.is_some()
                || intent.kind != MediaMutationKind::Delete as i32
                || intent.patch.is_some()
                || result.media.is_some()
                || result.deleted_count != 0
                || result.after_sha256 != result.before_sha256
            {
                return Err(invalid());
            }
        }
        result.signature = key
            .sign_media_patch_outcome(&result)
            .map_err(|_| invalid())?;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| invalid())?;
        let expected = if noop {
            // Keep the original legal state edges. Both journal-only steps
            // commit atomically, with no external mutation or generation scope.
            let count = transaction.execute("UPDATE media_operations SET state='APPLYING' WHERE operation_id=?1 AND state='PREPARED'", [operation]).map_err(|_| invalid())?;
            if count != 1 {
                return Err(invalid());
            }
            "APPLYING"
        } else {
            state
        };
        let count=transaction.execute("UPDATE media_operations SET state=?1,outcome=?2 WHERE operation_id=?3 AND state=?4",params![if result.state==MediaPatchEffectState::Applied as i32{"APPLIED"}else{"REFUSED"},result.encode_to_vec(),operation,expected]).map_err(|_|invalid())?;
        if count != 1 {
            return Err(invalid());
        }
        transaction.commit().map_err(|_| invalid())?;
        Ok(result)
    }
    pub(super) fn publish(
        &mut self,
        claim: &MediaPatchClaim,
        digest: &[u8],
        key: &ControlSigningKey,
    ) -> Result<MediaPatchOutcome, Status> {
        let operation = &claim.intent.as_ref().ok_or_else(invalid)?.operation_id;
        let _gate = exclusive_gate(&self.root).map_err(|_| invalid())?;
        assert_mutation_allowed(None, &self.root).map_err(|_| invalid())?;
        let row = self.retained(operation)?.ok_or_else(invalid)?;
        if row.claim != normalized_claim(claim) || row.digest != digest {
            return Err(invalid());
        }
        if let Some(result) = self.terminal(&row)? {
            return Ok(result);
        }
        if row.state != "PREPARED" {
            return Err(Status::unavailable("EFFECT_UNKNOWN"));
        }
        if let Some(after) = row.after.as_ref() {
            if read_library(&self.root)? != row.before {
                let refused = media_library::refusal(
                    row.planned,
                    409,
                    "revision_conflict",
                    "Media library changed before publication",
                );
                return self.settle(operation, "PREPARED", refused, key);
            }
            let _publication = mutation_scope(None, &self.root).map_err(|_| invalid())?;
            let count=self.connection.execute("UPDATE media_operations SET state='APPLYING' WHERE operation_id=?1 AND state='PREPARED'",[operation]).map_err(|_|invalid())?;
            if count != 1 {
                return Err(invalid());
            }
            let standalone = row.planned.processed.is_some();
            let processing = standalone
                || crate::media_processing::has_processing(
                    &self.creation_staging,
                    claim,
                    &row.planned,
                )?;
            let published = if standalone {
                crate::media_process::publish(
                    &self.root,
                    &self.creation_staging,
                    claim,
                    &row.planned,
                    &row.before,
                    after,
                    true,
                )?
            } else if processing {
                crate::media_processing::publish(
                    &self.root,
                    &self.creation_staging,
                    claim,
                    &row.planned,
                    &row.before,
                    after,
                    true,
                )?
            } else {
                media_library::publish(&self.root, after, Some(claim.lease_until_unix))
                    .map_err(|_| Status::unavailable("EFFECT_UNKNOWN"))?
            };
            if !published {
                let refused = media_library::refusal(
                    row.planned,
                    409,
                    "lease_expired",
                    "Media lease expired before publication",
                );
                return self.settle(operation, "APPLYING", refused, key);
            }
            if !processing {
                self.finish_original_cleanup(&row)?;
            }
            self.settle(operation, "APPLYING", row.planned, key)
        } else {
            if row.planned.state == MediaPatchEffectState::Applied as i32
                && read_library(&self.root)? != row.before
            {
                return self.settle(
                    operation,
                    "PREPARED",
                    media_library::refusal(
                        row.planned,
                        409,
                        "revision_conflict",
                        "Media library changed before publication",
                    ),
                    key,
                );
            }
            self.settle(operation, "PREPARED", row.planned, key)
        }
    }
    // Queries can reconcile an existing original publication; they never publish
    // media bytes. Missing/old bytes remain unknown, rather than proof to retry.
    pub(super) fn query(
        &mut self,
        operation: &str,
        fence: &ActionFence,
        dispatch_confirmed: bool,
        key: &ControlSigningKey,
    ) -> Result<Option<MediaPatchOutcome>, Status> {
        let _gate = exclusive_gate(&self.root).map_err(|_| invalid())?;
        let Some(row) = self.retained(operation)? else {
            return Ok(None);
        };
        let claim = MediaPatchClaim::decode(row.claim.as_slice()).map_err(|_| invalid())?;
        if claim.fence.as_ref() != Some(fence) {
            return Err(invalid());
        }
        if let Some(outcome) = self.terminal(&row)? {
            return Ok(Some(outcome));
        }
        if row.state == "APPLYING" && row.planned.processed.is_some() {
            let _reconciliation = mutation_scope(None, &self.root).map_err(|_| invalid())?;
            crate::media_process::publish(
                &self.root,
                &self.creation_staging,
                &claim,
                &row.planned,
                &row.before,
                row.after.as_ref().ok_or_else(invalid)?,
                false,
            )?;
            return self
                .settle(operation, "APPLYING", row.planned, key)
                .map(Some);
        }
        if row.state == "APPLYING"
            && crate::media_processing::has_processing(
                &self.creation_staging,
                &claim,
                &row.planned,
            )?
        {
            let _reconciliation = mutation_scope(None, &self.root).map_err(|_| invalid())?;
            crate::media_processing::publish(
                &self.root,
                &self.creation_staging,
                &claim,
                &row.planned,
                &row.before,
                row.after.as_ref().ok_or_else(invalid)?,
                false,
            )?;
            return self
                .settle(operation, "APPLYING", row.planned, key)
                .map(Some);
        }
        if row.state == "APPLYING"
            && row.after.as_ref().is_some_and(|after| {
                read_library(&self.root).is_ok_and(|current| current == *after)
            })
        {
            media_library::confirm_publication(&self.root, row.after.as_ref().ok_or_else(invalid)?)
                .map_err(|_| Status::unavailable("EFFECT_UNKNOWN"))?;
            let _reconciliation = mutation_scope(None, &self.root).map_err(|_| invalid())?;
            // Resume only the original fenced local cleanup. The durable
            // after-image identifies that operation; no second publication,
            // admission, action id or execution epoch is manufactured.
            self.finish_original_cleanup(&row)?;
            return self
                .settle(operation, "APPLYING", row.planned, key)
                .map(Some);
        }
        // PREPARED positively proves the durable pre-publication phase. The same
        // OS lock and SQL compare-and-swap prevent a holder of an earlier permit
        // from writing after this conclusive abort.
        if row.state == "PREPARED" && dispatch_confirmed {
            let refused = media_library::refusal(
                row.planned,
                409,
                "execution_interrupted",
                "Media update stopped before publication",
            );
            return self.settle(operation, "PREPARED", refused, key).map(Some);
        }
        let mut unknown = row.planned;
        unknown.state = MediaPatchEffectState::Unknown as i32;
        unknown.media = None;
        unknown.deleted_count = 0;
        unknown.created = None;
        unknown.processed = None;
        unknown.http_status = 503;
        unknown.error_code = "EFFECT_UNKNOWN".into();
        unknown.error_message = "Media publication requires reconciliation".into();
        unknown.after_sha256.clear();
        Ok(Some(unknown))
    }
}

#[cfg(test)]
#[path = "media_journal_tests.rs"]
mod tests;
