package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

var ErrTargetRegistrationInvalid = errors.New("TARGET_REGISTRATION_INVALID")
var targetRegistrationID = regexp.MustCompile(`^target_[a-z0-9][a-z0-9._-]{0,63}$`)

const targetInspectionOperationPrefix = "target-inspect-"
const targetInspectionSignatureDomain = "deepseek-infra:backup-target-inspection-v1\x00"
const maximumTargetInspectionBytes = 4*1024*1024 + 128*1024

// Scope is captured from Go authority before the read-only Rust observation.
// It admits no provider effect and cannot advance an action epoch.
type BackupTargetRegistrationScope struct {
	operationID, actionID, actor, requestedPath, snapshot string
	writerFence, cutoverRevision                          int64
	epoch                                                 uint64
}

func (scope BackupTargetRegistrationScope) InspectionRequest() *actionv1.InspectFilesystemBackupTargetRequest {
	return &actionv1.InspectFilesystemBackupTargetRequest{SchemaVersion: 1,
		OperationId: scope.operationID, Fence: &commonv1.ActionFence{ActionId: scope.actionID, ExecutionEpoch: scope.epoch},
		Path: scope.requestedPath, WriterFencingToken: uint64(scope.writerFence)}
}

func (store *Control) PrepareBackupTargetRegistration(operationID, actionID, actor, path string) (BackupTargetRegistrationScope, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if operationID != actionID || !strings.HasPrefix(operationID, targetInspectionOperationPrefix) || len(operationID) > 128 || !filepath.IsAbs(path) {
		return BackupTargetRegistrationScope{}, ErrTargetRegistrationInvalid
	}
	tx, context, err := store.beginOperatorMutationTx(operationID, actionID, actor, "target", true)
	if err != nil {
		return BackupTargetRegistrationScope{}, err
	}
	defer tx.Rollback()
	snapshot, err := targetRegistryDigestTx(tx)
	if err != nil {
		return BackupTargetRegistrationScope{}, err
	}
	scope := BackupTargetRegistrationScope{operationID: operationID, actionID: actionID, actor: actor, requestedPath: path,
		snapshot: snapshot, writerFence: store.token, cutoverRevision: context.cutover.Revision, epoch: uint64(context.cutover.Epoch)}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return BackupTargetRegistrationScope{}, err
	}
	return scope, nil
}

func targetRegistryDigestTx(tx *sql.Tx) (string, error) {
	rows, err := tx.Query(`SELECT id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,updated_at FROM targets ORDER BY id`)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	var records []Record
	for rows.Next() {
		record, exists, err := readControlRecord(rows, "target")
		if err != nil || !exists {
			return "", ErrCorruptRecord
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return "", err
	}
	if err := rows.Close(); err != nil {
		return "", err
	}
	ids := make(map[string]bool, len(records))
	for _, record := range records {
		if err := validateControlHistory(tx, record); err != nil {
			return "", err
		}
		ids[record.ID] = true
	}
	events, err := tx.Query("SELECT DISTINCT record_id FROM control_events WHERE domain='target'")
	if err != nil {
		return "", err
	}
	defer events.Close()
	for events.Next() {
		var id string
		if err := events.Scan(&id); err != nil {
			return "", err
		}
		if !ids[id] {
			return "", ErrCorruptRecord
		}
	}
	if err := events.Err(); err != nil {
		return "", err
	}
	raw, err := json.Marshal(records)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:]), nil
}

func verifyTargetObservation(receipt *actionv1.FilesystemBackupTargetInspectionReceipt, public, fleet, environment string) error {
	if err := verifyTargetObservationEnvelope(receipt, public, fleet, environment); err != nil {
		return err
	}
	if !receipt.Observation.MarkerPresent {
		return ErrTargetRegistrationInvalid
	}
	return nil
}

func verifyTargetObservationEnvelope(receipt *actionv1.FilesystemBackupTargetInspectionReceipt, public, fleet, environment string) error {
	if receipt == nil || receipt.Observation == nil || receipt.SchemaVersion != 1 || receipt.SignerPublicKey != public || len(receipt.Signature) != ed25519.SignatureSize || proto.Size(receipt) > maximumTargetInspectionBytes {
		return ErrTargetRegistrationInvalid
	}
	o := receipt.Observation
	if o.Fence == nil || !nativeKnown(receipt, o, o.Fence) || o.SchemaVersion != 1 || o.Fence.ExecutionEpoch == 0 || o.Fence.ExecutionEpoch > math.MaxInt64 || o.OperationId != o.Fence.ActionId || !strings.HasPrefix(o.OperationId, targetInspectionOperationPrefix) || len(o.OperationId) > 128 || o.WriterFencingToken == 0 || o.WriterFencingToken > math.MaxInt64 || o.ObservedAtUnix <= 0 || o.FleetId != fleet || o.Environment != environment || len(o.RequestedPath) == 0 || len(o.RequestedPath) > 32768 || len(o.CanonicalPath) > 32768 || !filepath.IsAbs(o.CanonicalPath) {
		return ErrTargetRegistrationInvalid
	}
	if o.MarkerPresent {
		digest := sha256.Sum256(o.MarkerJson)
		if len(o.MarkerJson) == 0 || len(o.MarkerJson) > 4*1024*1024 || !bytes.Equal(digest[:], o.MarkerSha256) {
			return ErrTargetRegistrationInvalid
		}
	} else if len(o.MarkerJson) != 0 || len(o.MarkerSha256) != 0 {
		return ErrTargetRegistrationInvalid
	}
	key, err := decodeFixedBase64(public, ed25519.PublicKeySize)
	if err != nil {
		return ErrTargetRegistrationInvalid
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(o)
	if err != nil || !ed25519.Verify(key, append([]byte(targetInspectionSignatureDomain), wire...), receipt.Signature) {
		return ErrTargetRegistrationInvalid
	}
	return nil
}

func targetPublicGeneration(record Record) (int64, error) {
	var document map[string]any
	if err := decodeSingleJSON(record.Payload, &document); err != nil {
		return 0, ErrCorruptRecord
	}
	number, ok := document["topologyGeneration"].(json.Number)
	if !ok {
		return 0, ErrCorruptRecord
	}
	generation, err := number.Int64()
	if err != nil || generation < 1 {
		return 0, ErrCorruptRecord
	}
	return generation, nil
}

func targetRegistrationDocument(o *actionv1.FilesystemBackupTargetObservation, document json.RawMessage) (map[string]any, string, error) {
	var body, marker map[string]any
	if decodeSingleJSON(document, &body) != nil || body == nil || decodeSingleJSON(o.MarkerJson, &marker) != nil || marker == nil {
		return nil, "", ErrTargetRegistrationInvalid
	}
	id, validID := marker["targetId"].(string)
	nonce, validNonce := marker["targetNonce"].(string)
	if marker["targetNonce"] == nil {
		nonce, validNonce = "", true
	}
	if !validID || !validNonce || !targetRegistrationID.MatchString(id) || body["targetId"] != id || body["targetNonce"] != nonce || body["kind"] != "filesystem" || body["path"] != o.CanonicalPath || body["drainState"] != "active" {
		return nil, "", ErrTargetRegistrationInvalid
	}
	return body, id, nil
}

// RegisterInspectedBackupTarget settles control metadata only. The caller must
// still supply the public route's complete containment/clone/contributor checks;
// this method never initializes a marker or treats absence as a failed effect.
func (store *Control) RegisterInspectedBackupTarget(scope BackupTargetRegistrationScope, receipt *actionv1.FilesystemBackupTargetInspectionReceipt, document json.RawMessage) (Record, OperatorMutationResult, error) {
	return store.registerInspectedBackupTarget(scope, receipt, nil, document)
}

// The second receipt is a separate Rust read of the previously registered path.
// It cannot authorize initialization, a provider retry, or an effect epoch.
func (store *Control) RegisterInspectedBackupTargetWithLocation(scope BackupTargetRegistrationScope, receipt *actionv1.FilesystemBackupTargetInspectionReceipt, location *actionv1.FilesystemBackupTargetLocationReceipt, document json.RawMessage) (Record, OperatorMutationResult, error) {
	return store.registerInspectedBackupTarget(scope, receipt, location, document)
}

func (store *Control) registerInspectedBackupTarget(scope BackupTargetRegistrationScope, receipt *actionv1.FilesystemBackupTargetInspectionReceipt, location *actionv1.FilesystemBackupTargetLocationReceipt, document json.RawMessage) (Record, OperatorMutationResult, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := verifyTargetObservation(receipt, store.promotionSignerKey, store.fleetID, store.environment); err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	observation := receipt.Observation
	if scope.operationID != observation.OperationId || scope.actionID != observation.Fence.ActionId || scope.requestedPath != observation.RequestedPath {
		return Record{}, OperatorMutationResult{}, ErrTargetRegistrationInvalid
	}
	body, id, err := targetRegistrationDocument(observation, document)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	witness, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	requestMaterial := map[string]any{"command": "backup-target-register-v1", "operationId": scope.operationID,
		"actionId": scope.actionID, "actor": scope.actor, "snapshot": scope.snapshot, "writerFence": scope.writerFence,
		"cutoverRevision": scope.cutoverRevision, "epoch": scope.epoch, "document": json.RawMessage(document), "observation": hex.EncodeToString(witness)}
	var locationWitness []byte
	if location != nil {
		if store.schema < SchemaV22 || proto.Size(location) > MaxBackupTargetLocationReceiptBytes {
			return Record{}, OperatorMutationResult{}, ErrTargetRegistrationInvalid
		}
		locationWitness, err = proto.MarshalOptions{Deterministic: true}.Marshal(location)
		if err != nil {
			return Record{}, OperatorMutationResult{}, err
		}
		requestMaterial["locationObservation"] = hex.EncodeToString(locationWitness)
	}
	material, err := canonicalAuthorityJSON(requestMaterial)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	sum := sha256.Sum256(material)
	digest := hex.EncodeToString(sum[:])
	tx, context, err := store.beginOperatorMutationTx(scope.operationID, scope.actionID, scope.actor, "target", true)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	defer tx.Rollback()
	if result, found, err := replayOperatorMutationTx(tx, scope.operationID, digest); err != nil {
		return Record{}, OperatorMutationResult{}, err
	} else if found {
		record, err := readTargetMutationResultTx(tx, result)
		if err != nil {
			return Record{}, OperatorMutationResult{}, err
		}
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return Record{}, OperatorMutationResult{}, err
		}
		return record, result, nil
	}
	if scope.writerFence != store.token || observation.WriterFencingToken != uint64(store.token) || scope.cutoverRevision != context.cutover.Revision || scope.epoch != uint64(context.cutover.Epoch) || observation.Fence.ExecutionEpoch != scope.epoch || observation.ObservedAtUnix > context.now || context.now-observation.ObservedAtUnix > 60 {
		return Record{}, OperatorMutationResult{}, ErrTargetRegistrationInvalid
	}
	snapshot, err := targetRegistryDigestTx(tx)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	if snapshot != scope.snapshot {
		return Record{}, OperatorMutationResult{}, ErrRevisionConflict
	}
	existing, exists, err := readTargetMetadataTx(tx, id)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	previousPath := ""
	if store.schema >= SchemaV22 {
		previousPath, err = validateTargetRelocation(existing, receipt, location, context.now)
		if err != nil {
			return Record{}, OperatorMutationResult{}, err
		}
	}
	revision, generation := int64(1), int64(1)
	if exists {
		if existing.Revision == math.MaxInt64 {
			return Record{}, OperatorMutationResult{}, ErrRevisionConflict
		}
		revision = existing.Revision + 1
		if existing.State != TombstoneState {
			current, err := targetPublicGeneration(existing)
			if err != nil || current == math.MaxInt64 {
				return Record{}, OperatorMutationResult{}, ErrCorruptRecord
			}
			generation = current + 1
		}
	}
	body["topologyGeneration"] = generation
	body["registeredAt"] = time.Unix(context.now, 0).UTC().Format("2006-01-02T15:04:05Z")
	payload, err := json.Marshal(body)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	write, err := prepareControlRecordWrite(Record{Domain: "target", ID: id, Revision: revision, State: "ACTIVE", Payload: payload}, nil)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	write.targetRegistrationGeneration = generation
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	payloadDigest, err := operatorPayloadDigest(write.record.Payload)
	if err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	result := OperatorMutationResult{Status: OperatorMutationApplied, OperationID: scope.operationID, ActionID: scope.actionID, Actor: scope.actor,
		Domain: "target", RecordID: id, Revision: revision, State: "ACTIVE", ExecutionEpoch: scope.epoch,
		CutoverRevision: context.cutover.Revision, FencingToken: context.cutover.FencingToken, PayloadDigest: payloadDigest, RequestDigest: digest}
	if err := insertOperatorMutationTx(tx, result, store.token, context.now); err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	if _, err := tx.Exec(`INSERT INTO control_target_registration_identity(singleton,signer_public_key,fleet_id,environment)
        SELECT 1,?,?,? WHERE NOT EXISTS(SELECT 1 FROM control_target_registration_identity)`, store.promotionSignerKey, store.fleetID, store.environment); err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	if _, err := tx.Exec(`INSERT INTO control_target_registrations(operation_id,target_id,revision,public_generation,observation) VALUES(?,?,?,?,?)`, scope.operationID, id, revision, generation, witness); err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	if len(locationWitness) != 0 {
		if _, err := tx.Exec(`INSERT INTO control_target_registration_locations(operation_id,previous_path,observation) VALUES(?,?,?)`, scope.operationID, previousPath, locationWitness); err != nil {
			return Record{}, OperatorMutationResult{}, err
		}
	}
	if err := validateControlHistory(tx, write.record); err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return Record{}, OperatorMutationResult{}, err
	}
	return write.record, result, nil
}
