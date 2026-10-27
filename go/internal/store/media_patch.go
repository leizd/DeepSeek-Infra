package store

import (
	"bytes"
	"crypto/rand"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"errors"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

var ErrMediaEffectUnknown = errors.New("EFFECT_UNKNOWN")
var ErrMediaClaimDenied = errors.New("MEDIA_CLAIM_DENIED")

func (store *Control) mediaTx(fn func(*sql.Tx, int64) error) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed || store.db == nil || store.schema < SchemaV19 {
		return ErrSchemaInactive
	}
	tx, err := store.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	now := store.now()
	if _, err = store.assertWriterTx(tx, now); err != nil {
		return err
	}
	if err = verifyMediaPatchSchemaTx(tx, store.schema); err != nil {
		return err
	}
	if err = fn(tx, now); err != nil {
		return err
	}
	return tx.Commit()
}
func readMediaClaim(tx *sql.Tx, operation string) (*mediav1.MediaPatchClaim, []byte, []byte, error) {
	var data, preparation, outcome []byte
	c := &mediav1.MediaPatchClaim{SchemaVersion: 1, Fence: &commonv1.ActionFence{}}
	err := tx.QueryRow(`SELECT intent,request_sha256,action_id,epoch,owner,claim_token,writer_fencing_token,lease_until,fleet_id,environment,state,preparation_sha256,outcome FROM control_media_operations WHERE operation_id=?`, operation).Scan(&data, &c.RequestSha256, &c.Fence.ActionId, &c.Fence.ExecutionEpoch, &c.Owner, &c.ClaimToken, &c.WriterFencingToken, &c.LeaseUntilUnix, &c.FleetId, &c.Environment, &c.State, &preparation, &outcome)
	if err != nil {
		return nil, nil, nil, err
	}
	c.Intent = &mediav1.MediaPatchIntent{}
	if proto.Unmarshal(data, c.Intent) != nil {
		return nil, nil, nil, ErrCorruptRecord
	}
	digest, err := mediamodel.RequestDigest(c.Intent)
	if err != nil || !bytes.Equal(digest, c.RequestSha256) || c.Intent.OperationId != operation || c.Fence.ActionId != operation || c.Fence.ExecutionEpoch != 1 || c.Owner == "" || c.ClaimToken == "" || c.WriterFencingToken < 1 || c.LeaseUntilUnix < 0 {
		return nil, nil, nil, ErrCorruptRecord
	}
	return c, preparation, outcome, nil
}

// AdmitMediaPatch serializes the whole library, since publication replaces its
// JSON collection. Expiry never releases a dispatch whose effect is unresolved.
func (store *Control) AdmitMediaPatch(intent *mediav1.MediaPatchIntent) (*mediav1.MediaPatchClaim, error) {
	digest, err := mediamodel.RequestDigest(intent)
	if err != nil {
		return nil, err
	}
	data, err := mediamodel.Encode(intent)
	if err != nil {
		return nil, err
	}
	var claim *mediav1.MediaPatchClaim
	err = store.mediaTx(func(tx *sql.Tx, now int64) error {
		old, _, _, readErr := readMediaClaim(tx, intent.OperationId)
		if readErr == nil {
			if !bytes.Equal(old.RequestSha256, digest) {
				return ErrRevisionConflict
			}
			claim = old
			return nil
		}
		if !errors.Is(readErr, sql.ErrNoRows) {
			return readErr
		}
		var active int
		if err := tx.QueryRow("SELECT COUNT(*) FROM control_media_operations WHERE state IN ('CLAIMED','DISPATCHED','EFFECT_UNKNOWN')").Scan(&active); err != nil {
			return err
		}
		if active != 0 {
			return ErrMediaEffectUnknown
		}
		var token [32]byte
		if _, err := rand.Read(token[:]); err != nil {
			return err
		}
		until := now + 60
		if store.leaseSeconds < 60 {
			until = now + store.leaseSeconds
		}
		if now < 0 || until < now {
			return ErrMediaClaimDenied
		}
		claim = &mediav1.MediaPatchClaim{SchemaVersion: 1, Intent: proto.Clone(intent).(*mediav1.MediaPatchIntent), Fence: &commonv1.ActionFence{ActionId: intent.OperationId, ExecutionEpoch: 1}, RequestSha256: digest, Owner: store.owner, ClaimToken: hex.EncodeToString(token[:]), WriterFencingToken: store.token, LeaseUntilUnix: until, FleetId: store.fleetID, Environment: store.environment, State: "CLAIMED"}
		_, err := tx.Exec(`INSERT INTO control_media_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,'CLAIMED',NULL,NULL,?,?)`, intent.OperationId, data, digest, claim.Fence.ActionId, claim.Fence.ExecutionEpoch, claim.Owner, claim.ClaimToken, claim.WriterFencingToken, until, claim.FleetId, claim.Environment, now, now)
		return err
	})
	return claim, err
}
func (store *Control) GetMediaPatchClaim(operation string, fence *commonv1.ActionFence) (*mediav1.MediaPatchClaim, error) {
	var c *mediav1.MediaPatchClaim
	err := store.mediaTx(func(tx *sql.Tx, now int64) error {
		var err error
		c, _, _, err = readMediaClaim(tx, operation)
		if err != nil {
			return err
		}
		if fence == nil || !proto.Equal(c.Fence, fence) {
			return ErrMediaClaimDenied
		}
		return nil
	})
	return c, err
}
func (store *Control) BeginMediaPatchDispatch(request *mediav1.BeginMediaPatchDispatchRequest) (*mediav1.MediaPatchDispatchPermit, error) {
	if request == nil || request.SchemaVersion != 1 || len(request.PreparationSha256) != 32 {
		return nil, ErrMediaClaimDenied
	}
	var result *mediav1.MediaPatchDispatchPermit
	err := store.mediaTx(func(tx *sql.Tx, now int64) error {
		claim, prep, _, err := readMediaClaim(tx, request.OperationId)
		if err != nil {
			return err
		}
		if !proto.Equal(claim.Fence, request.Fence) || claim.ClaimToken != request.ClaimToken || claim.WriterFencingToken != request.WriterFencingToken || claim.WriterFencingToken != store.token || claim.Owner != store.owner {
			return ErrMediaClaimDenied
		}
		if claim.State != "CLAIMED" {
			if !bytes.Equal(prep, request.PreparationSha256) {
				return ErrMediaClaimDenied
			}
			result = &mediav1.MediaPatchDispatchPermit{Claim: claim, PreparationSha256: prep, Fresh: false}
			return nil
		}
		if now >= claim.LeaseUntilUnix {
			return ErrActionLeaseExpired
		}
		if _, err := tx.Exec("UPDATE control_media_operations SET state='DISPATCHED',preparation_sha256=?,updated_at=? WHERE operation_id=?", request.PreparationSha256, now, request.OperationId); err != nil {
			return err
		}
		claim.State = "DISPATCHED"
		result = &mediav1.MediaPatchDispatchPermit{Claim: claim, PreparationSha256: request.PreparationSha256, Fresh: true}
		return nil
	})
	return result, err
}

// The current Go writer may settle historical signed evidence, including after
// takeover/expiry. Its original action/epoch and request cannot be rebound.
func (store *Control) RecordMediaPatchOutcome(outcome *mediav1.MediaPatchOutcome) (bool, error) {
	if outcome == nil {
		return false, ErrMediaClaimDenied
	}
	already := false
	err := store.mediaTx(func(tx *sql.Tx, now int64) error {
		claim, _, retained, err := readMediaClaim(tx, outcome.OperationId)
		if err != nil {
			return err
		}
		public, err := base64.RawURLEncoding.DecodeString(store.mediaOutcomeSignerKey)
		if err != nil {
			return ErrMediaClaimDenied
		}
		if err := mediamodel.VerifyOutcome(outcome, claim, public); err != nil {
			return err
		}
		data, err := mediamodel.Encode(outcome)
		if err != nil {
			return err
		}
		if len(retained) != 0 {
			if !bytes.Equal(data, retained) {
				return ErrRevisionConflict
			}
			already = true
			return nil
		}
		if claim.State != "DISPATCHED" && claim.State != "EFFECT_UNKNOWN" && !(claim.State == "CLAIMED" && outcome.State == mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED) {
			return ErrMediaClaimDenied
		}
		state := "APPLIED"
		if outcome.State == mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED {
			state = "REFUSED"
		}
		_, err = tx.Exec("UPDATE control_media_operations SET state=?,outcome=?,updated_at=? WHERE operation_id=?", state, data, now, outcome.OperationId)
		return err
	})
	return already, err
}
func (store *Control) PendingMediaPatch() (*mediav1.MediaPatchClaim, error) {
	return store.pendingMediaPatch(nil)
}

// RecoverableMediaPatch selects by small fencing/lease fields before decoding
// any immutable payload. A live request may contain megabytes of typed values;
// background polling must not repeatedly hash it while holding the store lock.
func (store *Control) RecoverableMediaPatch(now int64) (*mediav1.MediaPatchClaim, error) {
	return store.pendingMediaPatch(&now)
}

func (store *Control) pendingMediaPatch(recoveryAt *int64) (*mediav1.MediaPatchClaim, error) {
	var c *mediav1.MediaPatchClaim
	err := store.mediaTx(func(tx *sql.Tx, _ int64) error {
		var err error
		c, err = pendingMediaPatchTx(tx, store.token, recoveryAt)
		return err
	})
	return c, err
}

func pendingMediaPatchTx(tx *sql.Tx, writer int64, recoveryAt *int64) (*mediav1.MediaPatchClaim, error) {
	query := "SELECT operation_id FROM control_media_operations WHERE state IN ('CLAIMED','DISPATCHED','EFFECT_UNKNOWN')"
	var args []any
	if recoveryAt != nil {
		query += " AND (writer_fencing_token<? OR (writer_fencing_token=? AND lease_until<=?))"
		args = []any{writer, writer, *recoveryAt}
	}
	var id string
	err := tx.QueryRow(query, args...).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	claim, _, _, err := readMediaClaim(tx, id)
	return claim, err
}

func (store *Control) CancelUndispatchedMediaPatch(operation string) error {
	return store.mediaTx(func(tx *sql.Tx, now int64) error {
		c, _, _, err := readMediaClaim(tx, operation)
		if err != nil {
			return err
		}
		if c.State != "CLAIMED" {
			return ErrMediaEffectUnknown
		}
		_, err = tx.Exec("UPDATE control_media_operations SET state='CANCELLED',updated_at=? WHERE operation_id=?", now, operation)
		return err
	})
}
