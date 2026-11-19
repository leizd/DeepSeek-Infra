package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	targetmodel "github.com/leizd/DeepSeek-Infra/go/internal/targets"
	"google.golang.org/protobuf/proto"
)

var ErrS3BackupTargetLayoutIncomplete = errors.New("S3_BACKUP_TARGET_LAYOUT_INCOMPLETE")
var ErrS3BackupTargetHeadConflict = errors.New("S3_BACKUP_TARGET_HEAD_CONFLICT")

type s3TargetRegistrationContext struct {
	Version         int
	Placement       targetmodel.S3Placement
	Actor           string
	BodySHA256      string
	Snapshot        string
	CutoverRevision int64
}

type S3BackupTargetRegistrationScope struct {
	frame        s3TargetRegistrationContext
	registration targetmodel.S3Registration
	operation    string
	actor        string
	epoch        uint64
	writer       int64
}

func (scope S3BackupTargetRegistrationScope) InspectionRequest() *actionv1.InspectS3BackupTargetRequest {
	return s3RegistrationInspection(scope.operation, scope.epoch, uint64(scope.writer), scope.frame.Placement)
}

func s3RegistrationInspection(operation string, epoch, writer uint64, placement targetmodel.S3Placement) *actionv1.InspectS3BackupTargetRequest {
	return &actionv1.InspectS3BackupTargetRequest{SchemaVersion: 1, OperationId: operation,
		Fence: &commonv1.ActionFence{ActionId: operation, ExecutionEpoch: epoch}, WriterFencingToken: writer,
		TargetIdentity: placement.Fingerprint()}
}

func (store *Control) PrepareS3BackupTargetRegistration(operation, actor, bodySHA string, registration targetmodel.S3Registration) (S3BackupTargetRegistrationScope, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	placement := registration.Placement()
	if store.schema < SchemaV24 || !strings.HasPrefix(operation, PublicTargetOperationPrefix) || !isLowerSHA256(bodySHA) ||
		placement.Endpoint == "" || placement.Region == "" || !targetS3Bucket.MatchString(placement.Bucket) {
		return S3BackupTargetRegistrationScope{}, ErrTargetRegistrationInvalid
	}
	tx, authority, err := store.beginOperatorMutationTx(operation, operation, actor, "target", true)
	if err != nil {
		return S3BackupTargetRegistrationScope{}, err
	}
	defer tx.Rollback()
	if err := store.verifyTargetRegistrationIdentityTx(tx); err != nil {
		return S3BackupTargetRegistrationScope{}, err
	}
	if err := refuseS3RegistrationDuringMarkerTx(tx); err != nil {
		return S3BackupTargetRegistrationScope{}, err
	}
	snapshot, err := targetRegistryDigestTx(tx)
	if err != nil {
		return S3BackupTargetRegistrationScope{}, err
	}
	scope := S3BackupTargetRegistrationScope{frame: s3TargetRegistrationContext{Version: 1,
		Placement: placement, Actor: actor, BodySHA256: bodySHA, Snapshot: snapshot, CutoverRevision: authority.cutover.Revision},
		registration: registration, operation: operation, actor: actor, epoch: uint64(authority.cutover.Epoch), writer: store.token}
	if err := store.finishOperatorMutationTx(tx, authority); err != nil {
		return S3BackupTargetRegistrationScope{}, err
	}
	return scope, nil
}

func refuseS3RegistrationDuringMarkerTx(tx *sql.Tx) error {
	var pending bool
	if err := tx.QueryRow("SELECT " + targetMarkerPending).Scan(&pending); err != nil {
		return err
	}
	if pending {
		return ErrTargetMarkerPending
	}
	return nil
}

type s3TargetIdentity struct {
	SchemaVersion int     `json:"schemaVersion"`
	TargetID      string  `json:"targetId"`
	Kind          string  `json:"kind"`
	Bucket        string  `json:"bucket"`
	Prefix        *string `json:"prefix"`
	IncarnationID string  `json:"incarnationId"`
	CreatedAt     string  `json:"createdAt"`
}

type s3TargetHead struct {
	SchemaVersion    int    `json:"schemaVersion"`
	TargetGeneration *int64 `json:"targetGeneration"`
	LatestCommitHash string `json:"latestCommitHash"`
	IncarnationID    string `json:"incarnationId"`
}

func decodeS3LayoutFields(raw []byte, layout any, keys ...string) error {
	var observed map[string]json.RawMessage
	if err := decodeSingleJSON(raw, &observed); err != nil {
		return err
	}
	// Select exact key names as the retained provider reader did. Additional
	// fields remain bound by the signed raw bytes and are not reinterpreted.
	fields := make(map[string]json.RawMessage, len(keys))
	for _, key := range keys {
		fields[key] = observed[key]
	}
	projected, err := json.Marshal(fields)
	if err != nil {
		return err
	}
	return json.Unmarshal(projected, layout)
}

func readS3RegistrationLayout(observation *actionv1.S3BackupTargetObservation) (s3TargetIdentity, s3TargetHead, error) {
	var identity s3TargetIdentity
	var head s3TargetHead
	if observation.Identity == nil || observation.Head == nil {
		return identity, head, ErrS3BackupTargetLayoutIncomplete
	}
	if decodeS3LayoutFields(observation.Identity.Json, &identity, "schemaVersion", "targetId", "kind", "bucket", "prefix", "incarnationId", "createdAt") != nil ||
		decodeS3LayoutFields(observation.Head.Json, &head, "schemaVersion", "targetGeneration", "latestCommitHash", "incarnationId") != nil ||
		identity.SchemaVersion != 3 || identity.Kind != "s3" || !targetRegistrationID.MatchString(identity.TargetID) ||
		identity.Bucket != observation.Bucket || identity.Prefix == nil || *identity.Prefix != observation.Prefix ||
		identity.IncarnationID == "" || len(identity.IncarnationID) > 128 || !s3PrintableAttribute(identity.IncarnationID) ||
		head.SchemaVersion != 1 || head.IncarnationID != identity.IncarnationID || head.TargetGeneration == nil ||
		*head.TargetGeneration < 0 || !isLowerSHA256(head.LatestCommitHash) ||
		(*head.TargetGeneration == 0) != (head.LatestCommitHash == strings.Repeat("0", 64)) {
		return identity, head, ErrTargetRegistrationInvalid
	}
	if _, err := time.Parse(time.RFC3339, identity.CreatedAt); err != nil {
		return identity, head, ErrTargetRegistrationInvalid
	}
	return identity, head, nil
}

type s3TargetDocument struct {
	SchemaVersion int
	TargetID      string
	Kind          string
	Bucket        string
	Prefix        string
	Region        string
	EndpointURL   string
	CreatedAt     string
	RegisteredAt  string
}

func (d s3TargetDocument) matchesLocation(placement targetmodel.S3Placement) bool {
	return d.Kind == "s3" && d.Bucket == placement.Bucket && d.Prefix == placement.Prefix &&
		strings.TrimRight(d.EndpointURL, "/") == strings.TrimRight(placement.Endpoint, "/") &&
		(d.Region == "" || d.Region == placement.Region)
}

func s3TargetRegistryRecord(records []Record, id string, placement targetmodel.S3Placement) (Record, error) {
	var previous Record
	for _, record := range records {
		if record.ID == id {
			previous = record
		}
		if record.State == TombstoneState {
			continue
		}
		var current s3TargetDocument
		// The registry supports pre-cutover payloads, including filesystem
		// records whose shape is independent of the S3 location.
		if err := json.Unmarshal(record.Payload, &current); err != nil {
			return Record{}, ErrCorruptRecord
		}
		same := current.matchesLocation(placement)
		if (record.ID == id) != same {
			return Record{}, ErrTargetCloneDetected
		}
	}
	return previous, nil
}

func s3RegistrationRequestDigest(frame s3TargetRegistrationContext, wire []byte, payloadDigest string) (string, error) {
	raw, err := json.Marshal(struct {
		Command       string
		Context       s3TargetRegistrationContext
		Receipt       []byte
		PayloadSHA256 string
	}{"backup-target-register-existing-s3-v1", frame, wire, payloadDigest})
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:]), nil
}

func (store *Control) RegisterObservedS3BackupTarget(scope S3BackupTargetRegistrationScope, receipt *actionv1.S3BackupTargetInspectionReceipt) (Record, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, authority, err := store.beginOperatorMutationTx(scope.operation, scope.operation, scope.actor, "target", true)
	if err != nil {
		return Record{}, err
	}
	defer tx.Rollback()
	if err := store.verifyTargetRegistrationIdentityTx(tx); err != nil {
		return Record{}, err
	}
	context := BackupTargetObservationContext{PublicKey: store.promotionSignerKey, FleetID: store.fleetID, Environment: store.environment, Now: authority.now}
	if scope.writer != store.token || scope.frame.CutoverRevision != authority.cutover.Revision || scope.epoch != uint64(authority.cutover.Epoch) ||
		VerifyBackupTargetS3Receipt(scope.InspectionRequest(), receipt, context) != nil ||
		receipt.Observation.Bucket != scope.frame.Placement.Bucket || receipt.Observation.Prefix != scope.frame.Placement.Prefix {
		return Record{}, ErrTargetRegistrationInvalid
	}
	identity, head, err := readS3RegistrationLayout(receipt.Observation)
	if err != nil {
		return Record{}, err
	}
	if err := refuseS3RegistrationDuringMarkerTx(tx); err != nil {
		return Record{}, err
	}
	if _, exists, err := readOperatorMutationTx(tx, scope.operation); err != nil {
		return Record{}, err
	} else if exists {
		return Record{}, ErrMutationRequestReplayConflict
	}
	records, snapshot, err := targetRegistrySnapshotTx(tx)
	if err != nil {
		return Record{}, err
	}
	if snapshot != scope.frame.Snapshot {
		return Record{}, ErrRevisionConflict
	}
	previous, err := s3TargetRegistryRecord(records, identity.TargetID, scope.frame.Placement)
	if err != nil {
		return Record{}, err
	}
	if err := validateS3HeadContinuityTx(tx, previous, head, scope.frame.Placement); err != nil {
		return Record{}, err
	}
	document, err := scope.registration.Document(identity.TargetID, identity.CreatedAt)
	if err != nil {
		return Record{}, err
	}
	var fields map[string]any
	if err := decodeSingleJSON(document, &fields); err != nil {
		return Record{}, err
	}
	write, err := prepareTargetRegistrationWrite(previous, previous.ID != "", fields, identity.TargetID, authority.now)
	if err != nil {
		return Record{}, err
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt)
	if err != nil {
		return Record{}, err
	}
	payloadDigest, err := operatorPayloadDigest(write.record.Payload)
	if err != nil {
		return Record{}, err
	}
	digest, err := s3RegistrationRequestDigest(scope.frame, wire, payloadDigest)
	if err != nil {
		return Record{}, err
	}
	result := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: scope.operation, ActionID: scope.operation, Actor: scope.actor,
		Domain: "target", RecordID: write.record.ID, Revision: write.record.Revision, State: write.record.State, ExecutionEpoch: scope.epoch,
		CutoverRevision: authority.cutover.Revision, FencingToken: authority.cutover.FencingToken, PayloadDigest: payloadDigest, RequestDigest: digest}
	if err := store.putControlRecordTx(tx, write, authority.now); err != nil {
		return Record{}, err
	}
	if err := store.insertTargetRegistrationWitnessTx(tx, result, write.targetRegistrationGeneration, wire, authority.now); err != nil {
		return Record{}, err
	}
	frame, err := json.Marshal(scope.frame)
	if err != nil {
		return Record{}, err
	}
	if _, err := tx.Exec("INSERT INTO "+s3TargetRegistrationTable+"(operation_id,context_json) VALUES(?,?)", scope.operation, string(frame)); err != nil {
		return Record{}, err
	}
	if err := validateControlHistory(tx, write.record); err != nil {
		return Record{}, err
	}
	if err := store.finishOperatorMutationTx(tx, authority); err != nil {
		return Record{}, err
	}
	return write.record, nil
}

func validateS3HeadContinuityTx(tx *sql.Tx, previous Record, current s3TargetHead, placement targetmodel.S3Placement) error {
	if previous.ID == "" {
		return nil
	}
	var wire []byte
	var raw string
	err := tx.QueryRow(`SELECT r.observation,s.context_json FROM control_target_registrations r
		JOIN control_target_registration_s3 s ON s.operation_id=r.operation_id
		WHERE r.target_id=? AND r.revision<=? ORDER BY r.revision DESC LIMIT 1`, previous.ID, previous.Revision).Scan(&wire, &raw)
	if err == sql.ErrNoRows {
		return nil
	}
	if err != nil {
		return err
	}
	prior := &actionv1.S3BackupTargetInspectionReceipt{}
	frame, err := decodeS3TargetRegistrationContext(raw)
	if err != nil || proto.Unmarshal(wire, prior) != nil || prior.Observation == nil {
		return ErrCorruptRecord
	}
	_, head, err := readS3RegistrationLayout(prior.Observation)
	if err != nil {
		return ErrCorruptRecord
	}
	if frame.Placement.Region != placement.Region ||
		head.IncarnationID != current.IncarnationID || *current.TargetGeneration < *head.TargetGeneration ||
		(*current.TargetGeneration == *head.TargetGeneration && current.LatestCommitHash != head.LatestCommitHash) {
		return ErrS3BackupTargetHeadConflict
	}
	return nil
}
