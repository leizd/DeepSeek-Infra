package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"math"
	"strconv"
)

type targetMetadataCommand string

const (
	targetDeleteCommand   targetMetadataCommand = "backup-target-delete-v1"
	targetActivateCommand targetMetadataCommand = "backup-target-activate-v1"
)

// ActivateBackupTarget returns this operation's committed document, including on
// replay after later changes. It never probes or writes the target's provider.
func (store *Control) ActivateBackupTarget(operationID, actionID, actor, targetID string) (Record, OperatorMutationResult, error) {
	return store.mutateBackupTarget(operationID, actionID, actor, targetID, targetActivateCommand)
}

func (store *Control) mutateBackupTarget(operationID, actionID, actor, targetID string, command targetMetadataCommand) (Record, OperatorMutationResult, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if !ValidRecordID(targetID) {
		return Record{}, OperatorMutationResult{}, ErrBackupTargetNotFound
	}
	if command != targetDeleteCommand && command != targetActivateCommand {
		return Record{}, OperatorMutationResult{}, ErrMutationRequestInvalid
	}
	material, err := canonicalAuthorityJSON(map[string]any{
		"command": string(command), "operationId": operationID,
		"actionId": actionID, "actor": actor, "targetId": targetID,
	})
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	sum := sha256.Sum256(material)
	requestDigest := hex.EncodeToString(sum[:])
	tx, context, err := store.beginOperatorMutationTx(operationID, actionID, actor, "target")
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	defer tx.Rollback()
	if replay, found, err := replayOperatorMutationTx(tx, operationID, requestDigest); err != nil {
		return Record{}, OperatorMutationResult{}, err
	} else if found {
		record, err := readTargetMutationResultTx(tx, replay)
		if err != nil {
			return Record{}, OperatorMutationResult{}, err
		}
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return Record{}, OperatorMutationResult{}, err
		}
		return record, replay, nil
	}
	existing, exists, err := readTargetMetadataTx(tx, targetID)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	if !exists || existing.State == TombstoneState {
		return Record{}, OperatorMutationResult{}, ErrBackupTargetNotFound
	}
	var document map[string]any
	if err := decodeSingleJSON(existing.Payload, &document); err != nil || document == nil ||
		document["targetId"] != targetID || existing.Revision == math.MaxInt64 {
		return Record{}, OperatorMutationResult{}, ErrCorruptRecord
	}
	next := existing
	next.Revision++
	next.State = TombstoneState
	if command == targetActivateCommand {
		next.State = "ACTIVE"
		document["drainState"] = "active"
		delete(document, "drainReason")
		delete(document, "drainingAt")
		delete(document, "drainedAt")
	}
	// A separate registration command must preserve the target identity without
	// resetting its internal journal/fence. Activation never revives a deletion.
	document["topologyGeneration"] = json.Number(strconv.FormatInt(next.Revision, 10))
	next.Payload, err = json.Marshal(document)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	write, err := prepareControlRecordWrite(next, nil)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	payloadDigest, err := operatorPayloadDigest(write.record.Payload)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	result := OperatorMutationResult{
		Status: OperatorMutationApplied, OperationID: operationID, ActionID: actionID,
		Domain: "target", RecordID: targetID, Revision: next.Revision, State: next.State,
		ExecutionEpoch: uint64(context.cutover.Epoch), CutoverRevision: context.cutover.Revision,
		FencingToken: context.cutover.FencingToken, PayloadDigest: payloadDigest,
		RequestDigest: requestDigest, Actor: actor,
	}
	if err := insertOperatorMutationTx(tx, result, store.token, context.now); err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	return write.record, result, nil
}

func readTargetMetadataTx(tx *sql.Tx, targetID string) (Record, bool, error) {
	record, exists, err := readControlRecord(tx.QueryRow(
		`SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at
		 FROM targets WHERE id = ?`, targetID), "target")
	if err != nil {
		return Record{}, false, err
	}
	if !exists {
		if err := validateNoControlHistory(tx, "target", targetID); err != nil {
			return Record{}, false, err
		}
		return Record{}, false, nil
	}
	if err := validateControlHistory(tx, record); err != nil {
		return Record{}, false, err
	}
	return record, true, nil
}

func readTargetMutationResultTx(tx *sql.Tx, receipt OperatorMutationResult) (Record, error) {
	_, exists, err := readTargetMetadataTx(tx, receipt.RecordID)
	if err != nil {
		return Record{}, err
	}
	if !exists || receipt.Domain != "target" {
		return Record{}, ErrCorruptRecord
	}
	record, found, err := readControlRecord(tx.QueryRow(
		`SELECT record_id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, recorded_at
		 FROM control_events WHERE domain = ? AND record_id = ? AND revision = ?`,
		"target", receipt.RecordID, receipt.Revision), "target")
	if err != nil {
		return Record{}, err
	}
	if !found || record.ID != receipt.RecordID || record.Revision != receipt.Revision || record.State != receipt.State {
		return Record{}, ErrCorruptRecord
	}
	digest, err := operatorPayloadDigest(record.Payload)
	if err != nil || digest != receipt.PayloadDigest {
		return Record{}, ErrCorruptRecord
	}
	return record, nil
}
