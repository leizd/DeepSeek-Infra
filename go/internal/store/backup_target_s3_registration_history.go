package store

import (
	"database/sql"
	"encoding/json"
	"strings"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

type s3TargetRegistrationBinding struct {
	frame   s3TargetRegistrationContext
	receipt *actionv1.S3BackupTargetInspectionReceipt
	wire    []byte
}

func decodeS3TargetRegistrationContext(encoded string) (s3TargetRegistrationContext, error) {
	var frame s3TargetRegistrationContext
	if len(encoded) > 8192 || decodeSingleJSON([]byte(encoded), &frame) != nil ||
		frame.Version != 1 || !isLowerSHA256(frame.BodySHA256) || !isLowerSHA256(frame.Snapshot) ||
		frame.Actor == "" || frame.CutoverRevision < 1 || frame.Placement.Endpoint == "" || frame.Placement.Region == "" {
		return frame, ErrCorruptRecord
	}
	canonical, err := json.Marshal(frame)
	if err != nil || string(canonical) != encoded {
		return frame, ErrCorruptRecord
	}
	return frame, nil
}

func loadS3TargetRegistrationBinding(operation, encoded string, wire []byte, public, fleet, environment string) (*s3TargetRegistrationBinding, error) {
	receipt := &actionv1.S3BackupTargetInspectionReceipt{}
	frame, err := decodeS3TargetRegistrationContext(encoded)
	if err != nil || !strings.HasPrefix(operation, PublicTargetOperationPrefix) || len(wire) > MaxBackupTargetS3ReceiptBytes ||
		proto.Unmarshal(wire, receipt) != nil || receipt.Observation == nil {
		return nil, ErrCorruptRecord
	}
	o := receipt.Observation
	request := s3RegistrationInspection(operation, o.GetFence().GetExecutionEpoch(), o.WriterFencingToken, frame.Placement)
	context := BackupTargetObservationContext{PublicKey: public, FleetID: fleet, Environment: environment, Now: o.ObservedAtUnix}
	if VerifyBackupTargetS3Receipt(request, receipt, context) != nil || o.Bucket != frame.Placement.Bucket || o.Prefix != frame.Placement.Prefix {
		return nil, ErrCorruptRecord
	}
	return &s3TargetRegistrationBinding{frame: frame, receipt: receipt, wire: wire}, nil
}

func validateS3RegistrationContextTx(tx *sql.Tx, id string, binding targetRegistrationBinding, eventID, recorded int64, payload []byte) error {
	s3 := binding.s3
	identity, head, err := readS3RegistrationLayout(s3.receipt.Observation)
	if err != nil || id != identity.TargetID || s3.frame.Actor != binding.result.Actor || s3.frame.CutoverRevision != binding.result.CutoverRevision {
		return ErrCorruptRecord
	}
	var document s3TargetDocument
	if decodeSingleJSON(payload, &document) != nil || document.SchemaVersion != identity.SchemaVersion || document.TargetID != id ||
		document.CreatedAt != identity.CreatedAt || document.RegisteredAt != time.Unix(recorded, 0).UTC().Format("2006-01-02T15:04:05Z") ||
		!document.matchesLocation(s3.frame.Placement) {
		return ErrCorruptRecord
	}
	records, err := targetRecordsAtEvent(tx, eventID)
	if err != nil {
		return err
	}
	snapshot, err := snapshotRecords(records)
	if err != nil || snapshot != s3.frame.Snapshot {
		return ErrCorruptRecord
	}
	prior, err := s3TargetRegistryRecord(records, id, s3.frame.Placement)
	if err != nil {
		return ErrCorruptRecord
	}
	if err := validateS3HeadContinuityTx(tx, prior, head, s3.frame.Placement); err != nil {
		return ErrCorruptRecord
	}
	digest, err := s3RegistrationRequestDigest(s3.frame, s3.wire, binding.result.PayloadDigest)
	if err != nil || digest != binding.result.RequestDigest {
		return ErrCorruptRecord
	}
	return nil
}

func targetRegistrationBodySHATx(tx *sql.Tx, schema int, operation string, payload []byte) (string, error) {
	if schema >= SchemaV24 {
		var encoded string
		err := tx.QueryRow("SELECT context_json FROM "+s3TargetRegistrationTable+" WHERE operation_id=?", operation).Scan(&encoded)
		if err == nil {
			frame, err := decodeS3TargetRegistrationContext(encoded)
			return frame.BodySHA256, err
		}
		if err != sql.ErrNoRows {
			return "", err
		}
	}
	frame, err := decodePublicTargetContext(payload)
	if err != nil || frame == nil {
		return "", ErrCorruptRecord
	}
	return frame.BodySHA256, nil
}
