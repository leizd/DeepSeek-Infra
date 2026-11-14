package store

import (
	"database/sql"
	"encoding/json"
	"math"
	"strings"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

type targetRegistrationBinding struct {
	operation                 string
	revision, generation      int64
	receipt                   *actionv1.FilesystemBackupTargetInspectionReceipt
	result                    OperatorMutationResult
	location                  *actionv1.FilesystemBackupTargetLocationReceipt
	previousPath              string
	locationRequiredByVersion bool
}

func targetRegistrationSchemaActiveTx(tx *sql.Tx) (bool, error) {
	var schema int
	if err := tx.QueryRow("PRAGMA user_version").Scan(&schema); err != nil {
		return false, err
	}
	return schema >= SchemaV21, nil
}

func targetRegistrationOperationAllowedTx(tx *sql.Tx, operation string, registration bool) error {
	if registration || !strings.HasPrefix(operation, targetInspectionOperationPrefix) {
		return nil
	}
	// The namespace is reserved only for new operations after v21. Existing
	// callers retain exact replay rights for any pre-migration operation id.
	var count int
	if err := tx.QueryRow(`SELECT COUNT(*) FROM control_operator_mutations o JOIN control_events e
        ON e.domain=o.domain AND e.record_id=o.record_id AND e.revision=o.record_revision
        WHERE o.operation_id=? AND e.event_id<=(SELECT last_legacy_event_id FROM control_target_registration_boundary WHERE singleton=1)`, operation).Scan(&count); err != nil {
		return err
	}
	if count != 1 {
		return ErrTargetRegistrationInvalid
	}
	return nil
}

func loadTargetRegistrationBindingsTx(tx *sql.Tx, id string) (map[int64]targetRegistrationBinding, error) {
	bindings := map[int64]targetRegistrationBinding{}
	active, err := targetRegistrationSchemaActiveTx(tx)
	if err != nil || !active {
		return bindings, err
	}
	var public, fleet, environment string
	err = tx.QueryRow("SELECT signer_public_key,fleet_id,environment FROM control_target_registration_identity WHERE singleton=1").Scan(&public, &fleet, &environment)
	if err != nil && err != sql.ErrNoRows {
		return nil, err
	}
	rows, err := tx.Query("SELECT operation_id,revision,public_generation,observation FROM control_target_registrations WHERE target_id=? ORDER BY revision", id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var binding targetRegistrationBinding
		var encoded []byte
		if err := rows.Scan(&binding.operation, &binding.revision, &binding.generation, &encoded); err != nil {
			return nil, err
		}
		binding.receipt = &actionv1.FilesystemBackupTargetInspectionReceipt{}
		if proto.Unmarshal(encoded, binding.receipt) != nil || verifyTargetObservation(binding.receipt, public, fleet, environment) != nil || binding.receipt.Observation.OperationId != binding.operation {
			return nil, ErrCorruptRecord
		}
		result, exists, err := readOperatorMutationTx(tx, binding.operation)
		if err != nil {
			return nil, err
		}
		if !exists || result.Domain != "target" || result.RecordID != id || result.Revision != binding.revision || result.State != "ACTIVE" || result.ActionID != binding.receipt.Observation.Fence.ActionId || result.ExecutionEpoch != binding.receipt.Observation.Fence.ExecutionEpoch {
			return nil, ErrCorruptRecord
		}
		binding.result = result
		bindings[binding.revision] = binding
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if err := rows.Close(); err != nil {
		return nil, err
	}
	// Operator identity is the durable command tag. Its immutable legacy event
	// boundary prevents a new native witness from reinterpreting an old operation.
	var expected int
	if err := tx.QueryRow(`SELECT COUNT(*) FROM control_operator_mutations o JOIN control_events e
        ON e.domain=o.domain AND e.record_id=o.record_id AND e.revision=o.record_revision
        WHERE o.domain='target' AND o.record_id=? AND substr(o.operation_id,1,15)='target-inspect-'
        AND e.event_id>(SELECT last_legacy_event_id FROM control_target_registration_boundary WHERE singleton=1)`, id).Scan(&expected); err != nil {
		return nil, err
	}
	if expected != len(bindings) {
		return nil, ErrCorruptRecord
	}
	if err := attachTargetLocationBindingsTx(tx, id, bindings); err != nil {
		return nil, err
	}
	if err := validatePublicTargetBindingsTx(tx, id, bindings); err != nil {
		return nil, err
	}
	return bindings, nil
}

func validateTargetRegistrationEvent(binding targetRegistrationBinding, before, event Record, metadata storedRecordMetadata) error {
	o := binding.receipt.Observation
	if event.State != "ACTIVE" || event.Revision != binding.revision || o.WriterFencingToken != uint64(metadata.writerToken) || o.ObservedAtUnix > metadata.timestamp || metadata.timestamp-o.ObservedAtUnix > 60 {
		return ErrCorruptRecord
	}
	expected := int64(1)
	if before.State != "" && before.State != TombstoneState {
		generation, err := targetPublicGeneration(before)
		if err != nil || generation == math.MaxInt64 {
			return ErrCorruptRecord
		}
		expected = generation + 1
	}
	generation, err := targetPublicGeneration(event)
	if err != nil || generation != expected || binding.generation != expected {
		return ErrCorruptRecord
	}
	if _, _, err := targetRegistrationDocument(o, event.Payload); err != nil {
		return ErrCorruptRecord
	}
	if err := validateTargetLocationEvent(binding, before, metadata); err != nil {
		return err
	}
	digest, err := operatorPayloadDigest(event.Payload)
	if err != nil || digest != binding.result.PayloadDigest {
		return ErrCorruptRecord
	}
	return nil
}

func targetBoundGenerationTx(tx *sql.Tx, record Record) (int64, bool, error) {
	var revision, generation int64
	err := tx.QueryRow(`SELECT revision,public_generation FROM control_target_registrations
        WHERE target_id=? AND revision<=? ORDER BY revision DESC LIMIT 1`, record.ID, record.Revision).Scan(&revision, &generation)
	if err == sql.ErrNoRows {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	delta := record.Revision - revision
	if delta < 0 || generation < 1 || generation > math.MaxInt64-delta {
		return 0, false, ErrCorruptRecord
	}
	return generation + delta, true, nil
}

func validateTargetPublicGeneration(record Record, expected int64) error {
	generation, err := targetPublicGeneration(record)
	if err != nil || generation != expected {
		return ErrRevisionConflict
	}
	return nil
}

// Keep exact JSON integer semantics in every subsequent metadata mutation.
func nextTargetPublicGenerationTx(tx *sql.Tx, record Record) (json.Number, error) {
	value, bound, err := targetBoundGenerationTx(tx, record)
	if err != nil {
		return "", err
	}
	// Preserve the pre-registration primitive for unbound records. Only a
	// retained native registration witness introduces an independent counter.
	if !bound {
		value = record.Revision
	}
	if value == math.MaxInt64 || (bound && validateTargetPublicGeneration(record, value) != nil) {
		return "", ErrCorruptRecord
	}
	encoded, err := json.Marshal(value + 1)
	return json.Number(encoded), err
}
