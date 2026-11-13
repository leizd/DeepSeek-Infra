package store

import (
	"database/sql"
	"errors"
	"path/filepath"
	"runtime"
	"strings"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

var ErrTargetLocationRequired = errors.New("TARGET_LOCATION_OBSERVATION_REQUIRED")
var ErrTargetCloneDetected = errors.New("target-clone-detected")

func (scope BackupTargetRegistrationScope) LocationRequest(path string) *actionv1.ProbeFilesystemBackupTargetLocationRequest {
	request := scope.InspectionRequest()
	return &actionv1.ProbeFilesystemBackupTargetLocationRequest{SchemaVersion: 1, Fence: request.Fence,
		OperationId: request.OperationId, Path: path, WriterFencingToken: request.WriterFencingToken}
}

func priorTargetRegistrationPath(before Record, canonical string) (string, bool, error) {
	if before.State == "" || before.State == TombstoneState {
		return "", false, nil
	}
	var document map[string]any
	if decodeSingleJSON(before.Payload, &document) != nil || document == nil {
		return "", false, ErrCorruptRecord
	}
	path, ok := document["path"].(string)
	if !ok || path == "" {
		return "", false, ErrTargetCloneDetected
	}
	if !filepath.IsAbs(path) || len(path) > 32768 || strings.ContainsRune(path, 0) {
		return "", false, ErrCorruptRecord
	}
	same := filepath.Clean(path) == filepath.Clean(canonical)
	if runtime.GOOS == "windows" {
		same = strings.EqualFold(filepath.Clean(path), filepath.Clean(canonical))
	}
	if same {
		return "", false, nil
	}
	return path, true, nil
}

func validateTargetRelocation(before Record, inspection *actionv1.FilesystemBackupTargetInspectionReceipt, location *actionv1.FilesystemBackupTargetLocationReceipt, now int64) (string, error) {
	o := inspection.Observation
	previous, needed, err := priorTargetRegistrationPath(before, o.CanonicalPath)
	if err != nil {
		return "", err
	}
	if !needed {
		if location != nil {
			return "", ErrTargetRegistrationInvalid
		}
		return "", nil
	}
	if location == nil {
		return "", ErrTargetLocationRequired
	}
	request := &actionv1.ProbeFilesystemBackupTargetLocationRequest{SchemaVersion: 1, Fence: o.Fence,
		OperationId: o.OperationId, Path: previous, WriterFencingToken: o.WriterFencingToken}
	context := BackupTargetObservationContext{PublicKey: inspection.SignerPublicKey, FleetID: o.FleetId, Environment: o.Environment, Now: now}
	if err := VerifyBackupTargetLocationReceipt(request, location, context); err != nil {
		return "", err
	}
	if location.Observation.State != actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING {
		return "", ErrTargetCloneDetected
	}
	return previous, nil
}

func attachTargetLocationBindingsTx(tx *sql.Tx, id string, bindings map[int64]targetRegistrationBinding) error {
	var version int
	if err := tx.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		return err
	}
	if version < SchemaV22 {
		return nil
	}
	var boundary int64
	if err := tx.QueryRow("SELECT last_legacy_event_id FROM control_target_location_boundary WHERE singleton=1").Scan(&boundary); err != nil {
		return err
	}
	rows, err := tx.Query(`SELECT r.operation_id,r.revision,e.event_id,l.previous_path,l.observation
        FROM control_target_registrations r JOIN control_events e
        ON e.domain='target' AND e.record_id=r.target_id AND e.revision=r.revision
        LEFT JOIN control_target_registration_locations l ON l.operation_id=r.operation_id
        WHERE r.target_id=? ORDER BY r.revision`, id)
	if err != nil {
		return err
	}
	defer rows.Close()
	seen := 0
	for rows.Next() {
		var operation string
		var revision, event int64
		var previous sql.NullString
		var encoded []byte
		if err := rows.Scan(&operation, &revision, &event, &previous, &encoded); err != nil {
			return err
		}
		binding, exists := bindings[revision]
		if !exists || operation != binding.operation {
			return ErrCorruptRecord
		}
		binding.locationRequiredByVersion = event > boundary
		if previous.Valid || len(encoded) != 0 {
			if !binding.locationRequiredByVersion || !previous.Valid || previous.String == "" || len(encoded) == 0 || len(encoded) > MaxBackupTargetLocationReceiptBytes {
				return ErrCorruptRecord
			}
			binding.location = &actionv1.FilesystemBackupTargetLocationReceipt{}
			if proto.Unmarshal(encoded, binding.location) != nil {
				return ErrCorruptRecord
			}
			binding.previousPath = previous.String
		}
		bindings[revision] = binding
		seen++
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if seen != len(bindings) {
		return ErrCorruptRecord
	}
	return nil
}

func validateTargetLocationEvent(binding targetRegistrationBinding, before Record, metadata storedRecordMetadata) error {
	if !binding.locationRequiredByVersion {
		if binding.location != nil || binding.previousPath != "" {
			return ErrCorruptRecord
		}
		return nil
	}
	previous, err := validateTargetRelocation(before, binding.receipt, binding.location, metadata.timestamp)
	if err != nil || previous != binding.previousPath {
		return ErrCorruptRecord
	}
	return nil
}
