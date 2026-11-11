package store

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
)

var ErrBackupTargetNotFound = errors.New("BACKUP_TARGET_NOT_FOUND")

// DeleteBackupTarget removes a registration, never its provider data. The live
// owner, current record, deletion event and operator receipt share one transaction.
// Retrying the same operation after losing its response reads the retained receipt.
func (store *Control) DeleteBackupTarget(operationID, actionID, actor, targetID string) (OperatorMutationResult, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if !ValidRecordID(targetID) {
		return OperatorMutationResult{}, ErrBackupTargetNotFound
	}
	material, err := canonicalAuthorityJSON(map[string]any{
		"command": "backup-target-delete-v1", "operationId": operationID,
		"actionId": actionID, "actor": actor, "targetId": targetID,
	})
	if err != nil {
		return OperatorMutationResult{}, err
	}
	sum := sha256.Sum256(material)
	requestDigest := hex.EncodeToString(sum[:])
	tx, context, err := store.beginOperatorMutationTx(operationID, actionID, actor, "target")
	if err != nil {
		return OperatorMutationResult{}, err
	}
	defer tx.Rollback()
	if replay, found, err := replayOperatorMutationTx(tx, operationID, requestDigest); err != nil {
		return OperatorMutationResult{}, err
	} else if found {
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return OperatorMutationResult{}, err
		}
		return replay, nil
	}
	table, _ := tableForDomain("target")
	existing, exists, err := readControlRecord(tx.QueryRow(fmt.Sprintf(
		"SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at FROM %s WHERE id = ?", table),
		targetID), "target")
	if err != nil {
		return OperatorMutationResult{}, err
	}
	if !exists {
		if err := validateNoControlHistory(tx, "target", targetID); err != nil {
			return OperatorMutationResult{}, err
		}
		return OperatorMutationResult{}, ErrBackupTargetNotFound
	}
	if err := validateControlHistory(tx, existing); err != nil {
		return OperatorMutationResult{}, err
	}
	if existing.State == TombstoneState {
		return OperatorMutationResult{}, ErrBackupTargetNotFound
	}
	var document map[string]any
	if err := decodeSingleJSON(existing.Payload, &document); err != nil || document == nil ||
		document["targetId"] != targetID || existing.Revision == math.MaxInt64 {
		return OperatorMutationResult{}, ErrCorruptRecord
	}
	// Retain the target identity and immutable history. Re-registration must be a
	// separately admitted lifecycle operation, never a reset of this revision/fence.
	next := existing
	next.Revision++
	next.State = TombstoneState
	document["topologyGeneration"] = json.Number(fmt.Sprintf("%d", next.Revision))
	next.Payload, err = json.Marshal(document)
	if err != nil {
		return OperatorMutationResult{}, err
	}
	write, err := prepareControlRecordWrite(next, nil)
	if err != nil {
		return OperatorMutationResult{}, err
	}
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return OperatorMutationResult{}, err
	}
	payloadDigest, err := operatorPayloadDigest(write.record.Payload)
	if err != nil {
		return OperatorMutationResult{}, err
	}
	result := OperatorMutationResult{
		Status: OperatorMutationApplied, OperationID: operationID, ActionID: actionID,
		Domain: "target", RecordID: targetID, Revision: next.Revision, State: next.State,
		ExecutionEpoch: uint64(context.cutover.Epoch), CutoverRevision: context.cutover.Revision,
		FencingToken: context.cutover.FencingToken, PayloadDigest: payloadDigest,
		RequestDigest: requestDigest, Actor: actor,
	}
	if err := insertOperatorMutationTx(tx, result, store.token, context.now); err != nil {
		return OperatorMutationResult{}, err
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return OperatorMutationResult{}, err
	}
	return result, nil
}
