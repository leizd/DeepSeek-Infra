package store

import (
	"math"
	"strings"
)

// GetLeasedStorageDispatch resolves an existing operation under a live native
// claim. The operation's epoch may precede the claim after takeover: it is never
// rewritten or inferred from a caller-supplied operation ID. This read does not
// renew a lease, authorize dispatch, or settle an effect.
func (store *Control) GetLeasedStorageDispatch(actionID string, epoch uint64, claimToken string) (StorageDispatch, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return StorageDispatch{}, false, ErrWriterFenceHeld
	}
	if store.schema != CurrentSchema {
		return StorageDispatch{}, false, ErrSchemaInactive
	}
	if !ValidRecordID(actionID) || epoch == 0 || epoch > math.MaxInt64 {
		return StorageDispatch{}, false, ErrInvalidStorageIntent
	}
	if strings.TrimSpace(claimToken) == "" {
		return StorageDispatch{}, false, ErrInvalidClaimToken
	}
	tx, err := store.db.Begin()
	if err != nil {
		return StorageDispatch{}, false, err
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return StorageDispatch{}, false, err
	}
	now := store.now()
	var runtimeName, mode, owner string
	var writerToken, writerUntil int64
	err = tx.QueryRow(`SELECT runtime,mode,owner_instance_id,fencing_token,lease_until
		FROM control_writer WHERE singleton=1`).Scan(&runtimeName, &mode, &owner, &writerToken, &writerUntil)
	if err != nil || now < 0 || runtimeName != RuntimeGo || mode != ModeShadow || owner != store.owner || writerToken != store.token || now >= writerUntil {
		return StorageDispatch{}, false, ErrWriterFenceHeld
	}
	lease, err := readActiveActionLeaseTx(tx, actionID)
	if err != nil {
		return StorageDispatch{}, false, err
	}
	if lease.Epoch != epoch || lease.WriterFencingToken != writerToken {
		return StorageDispatch{}, false, ErrActionLeaseStale
	}
	if lease.ClaimToken != claimToken {
		return StorageDispatch{}, false, ErrInvalidClaimToken
	}
	if now < lease.UpdatedAt || now >= lease.LeaseUntil {
		return StorageDispatch{}, false, ErrActionLeaseExpired
	}
	if _, _, err := validateActionLeaseResourcesTx(tx, lease); err != nil {
		return StorageDispatch{}, false, err
	}
	latest, exists, err := readControlRecord(tx.QueryRow(`SELECT id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,updated_at
		FROM action_journal WHERE id=?`, actionID), "action")
	if err != nil || !exists {
		return StorageDispatch{}, false, ErrCorruptRecord
	}
	if err := validateControlHistory(tx, latest); err != nil {
		return StorageDispatch{}, false, err
	}
	if latest.ExecutionEpoch != epoch || (latest.State != "CLAIMED" && latest.State != "EXECUTING" && latest.State != "EFFECT_UNKNOWN") {
		return StorageDispatch{}, false, ErrActionLeaseStale
	}
	claim, metadata, exists, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at
		FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, actionID, lease.ClaimRevision), "action")
	if err != nil || !exists || claim.ExecutionEpoch != epoch || metadata.writerToken != writerToken || metadata.timestamp != lease.AcquiredAt {
		return StorageDispatch{}, false, ErrActionLeaseStale
	}
	originEpoch := epoch
	if claim.State == "EFFECT_UNKNOWN" {
		// The current native graph permits a single CLAIMED/EXECUTING -> UNKNOWN takeover.
		// Follow its exact predecessor, never "the latest available" older intent.
		previous, exists, err := readControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at
			FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, actionID, lease.ClaimRevision-1), "action")
		if err != nil || !exists || previous.ExecutionEpoch+1 != epoch || (previous.State != "CLAIMED" && previous.State != "EXECUTING") {
			return StorageDispatch{}, false, ErrActionLeaseStale
		}
		originEpoch = previous.ExecutionEpoch
	} else if claim.State != "CLAIMED" {
		return StorageDispatch{}, false, ErrActionLeaseStale
	}
	dispatch, bound, err := readStorageDispatchTx(tx, actionID, originEpoch)
	if err != nil {
		return StorageDispatch{}, false, err
	}
	finishNow := store.now()
	if finishNow < now || finishNow >= writerUntil {
		return StorageDispatch{}, false, ErrWriterFenceHeld
	}
	if finishNow >= lease.LeaseUntil {
		return StorageDispatch{}, false, ErrActionLeaseExpired
	}
	if err := tx.Commit(); err != nil {
		return StorageDispatch{}, false, err
	}
	return dispatch, bound, nil
}
