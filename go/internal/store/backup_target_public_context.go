package store

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	targetmodel "github.com/leizd/DeepSeek-Infra/go/internal/targets"
	"google.golang.org/protobuf/proto"
)

// This Go-only durable context is part of the immutable target event payload.
// Public projections remove it; no JSON crosses a Go/Rust process boundary.
const publicTargetContextField = "_nativeTargetRegistrationV1"
const PublicTargetOperationPrefix = "target-inspect-public-v1-"
const maximumPublicTargetContext = 8 * 1024 * 1024

var ErrTargetOverlap = errors.New("TARGET_REGISTRATION_OVERLAP")

type BackupTargetPath struct{ TargetID, Path string }
type BackupTargetPathProof struct {
	TargetID string
	Receipt  *actionv1.FilesystemBackupTargetLocationReceipt
}
type publicTargetScope struct {
	OperationID, ActionID, Actor, RequestedPath, Snapshot string
	WriterFence, CutoverRevision                          int64
	Epoch                                                 uint64
}
type publicTargetPathProof struct {
	TargetID string
	Wire     []byte
}
type publicTargetContext struct {
	Version    uint32
	BodySHA256 string
	Scope      publicTargetScope
	Document   json.RawMessage
	Paths      []publicTargetPathProof
}

func (scope BackupTargetRegistrationScope) publicScope() publicTargetScope {
	return publicTargetScope{scope.operationID, scope.actionID, scope.actor, scope.requestedPath, scope.snapshot,
		scope.writerFence, scope.cutoverRevision, scope.epoch}
}
func (value publicTargetScope) scope() BackupTargetRegistrationScope {
	return BackupTargetRegistrationScope{operationID: value.OperationID, actionID: value.ActionID, actor: value.Actor,
		requestedPath: value.RequestedPath, snapshot: value.Snapshot, writerFence: value.WriterFence,
		cutoverRevision: value.CutoverRevision, epoch: value.Epoch}
}

func targetPaths(records []Record, excluded string) ([]BackupTargetPath, error) {
	var result []BackupTargetPath
	for _, record := range records {
		if record.ID == excluded || record.State == TombstoneState {
			continue
		}
		path, filesystem, err := targetmodel.CompetingFilesystemPath(record.Payload)
		if err != nil {
			return nil, ErrCorruptRecord
		}
		if !filesystem {
			continue
		}
		if path == "" {
			continue
		}
		if !filepath.IsAbs(path) || len(path) > 32768 || strings.ContainsRune(path, 0) {
			return nil, ErrCorruptRecord
		}
		result = append(result, BackupTargetPath{record.ID, path})
	}
	return result, nil
}

func targetRecordsAtEvent(tx *sql.Tx, before int64) ([]Record, error) {
	rows, err := tx.Query(`SELECT e.record_id,e.revision,e.execution_epoch,e.state,e.payload_json,e.record_digest,e.writer_fencing_token,e.recorded_at
        FROM control_events e WHERE e.domain='target' AND e.event_id=(
          SELECT MAX(p.event_id) FROM control_events p WHERE p.domain='target' AND p.record_id=e.record_id AND p.event_id<?)
        ORDER BY e.record_id`, before)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var records []Record
	for rows.Next() {
		record, found, err := readControlRecord(rows, "target")
		if err != nil {
			return nil, err
		}
		if !found {
			return nil, ErrCorruptRecord
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return records, nil
}

func snapshotRecords(records []Record) (string, error) {
	raw, err := json.Marshal(records)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:]), nil
}

func (store *Control) BackupTargetRegistrationPaths(scope BackupTargetRegistrationScope, excluded string) ([]BackupTargetPath, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	tx, context, err := store.beginOperatorMutationTx(scope.operationID, scope.actionID, scope.actor, "target", true)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	digest, err := targetRegistryDigestTx(tx)
	if err != nil {
		return nil, err
	}
	if digest != scope.snapshot {
		return nil, ErrRevisionConflict
	}
	rows, err := tx.Query(`SELECT id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,updated_at FROM targets ORDER BY id`)
	if err != nil {
		return nil, err
	}
	var records []Record
	for rows.Next() {
		record, found, err := readControlRecord(rows, "target")
		if err != nil || !found {
			rows.Close()
			if err != nil {
				return nil, err
			}
			return nil, ErrCorruptRecord
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	if err := rows.Close(); err != nil {
		return nil, err
	}
	paths, err := targetPaths(records, excluded)
	if err != nil {
		return nil, err
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return nil, err
	}
	return paths, nil
}

func comparisonPath(path string) string {
	if runtime.GOOS == "windows" {
		path = strings.ReplaceAll(path, "/", `\`)
		if strings.HasPrefix(strings.ToUpper(path), `\\?\UNC\`) {
			path = `\\` + path[8:]
		} else {
			path = strings.TrimPrefix(path, `\\?\`)
		}
	}
	return filepath.Clean(path)
}
func SameBackupTargetLocation(first, second string) bool {
	first, second = comparisonPath(first), comparisonPath(second)
	if runtime.GOOS == "windows" {
		return strings.EqualFold(first, second)
	}
	return first == second
}
func pathsOverlap(first, second string) bool {
	first, second = comparisonPath(first), comparisonPath(second)
	inside := func(path, base string) bool {
		relative, err := filepath.Rel(base, path)
		return err == nil && !filepath.IsAbs(relative) && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))
	}
	return inside(first, second) || inside(second, first)
}

func verifyPublicTargetPaths(scope BackupTargetRegistrationScope, marker *actionv1.FilesystemBackupTargetInspectionReceipt,
	paths []BackupTargetPath, proofs []publicTargetPathProof, now int64) error {
	if marker == nil || marker.Observation == nil || !marker.Observation.FullContainmentChecked || len(paths) != len(proofs) {
		return ErrTargetRegistrationInvalid
	}
	for index, path := range paths {
		proof := proofs[index]
		if proof.TargetID != path.TargetID || len(proof.Wire) == 0 || len(proof.Wire) > MaxBackupTargetLocationReceiptBytes {
			return ErrTargetRegistrationInvalid
		}
		receipt := &actionv1.FilesystemBackupTargetLocationReceipt{}
		if proto.Unmarshal(proof.Wire, receipt) != nil {
			return ErrTargetRegistrationInvalid
		}
		request := scope.LocationRequest(path.Path)
		request.ResolvePath = true
		context := BackupTargetObservationContext{PublicKey: marker.SignerPublicKey, FleetID: marker.Observation.FleetId, Environment: marker.Observation.Environment, Now: now}
		if VerifyBackupTargetLocationReceipt(request, receipt, context) != nil {
			return ErrTargetRegistrationInvalid
		}
		if pathsOverlap(marker.Observation.CanonicalPath, receipt.Observation.CanonicalPath) {
			return ErrTargetOverlap
		}
	}
	return nil
}

func decodePublicTargetContext(payload []byte) (*publicTargetContext, error) {
	trimmed := bytes.TrimSpace(payload)
	if len(trimmed) != 0 && trimmed[0] != '{' && json.Valid(trimmed) {
		return nil, nil
	}
	var fields map[string]json.RawMessage
	if decodeSingleJSON(payload, &fields) != nil {
		return nil, ErrCorruptRecord
	}
	raw, exists := fields[publicTargetContextField]
	if !exists {
		return nil, nil
	}
	if len(raw) == 0 || len(raw) > maximumPublicTargetContext {
		return nil, ErrCorruptRecord
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var value publicTargetContext
	if decoder.Decode(&value) != nil || value.Version != 1 || len(value.BodySHA256) != 64 || !strings.HasPrefix(value.Scope.OperationID, PublicTargetOperationPrefix) {
		return nil, ErrCorruptRecord
	}
	if b, err := hex.DecodeString(value.BodySHA256); err != nil || len(b) != 32 || value.BodySHA256 != strings.ToLower(value.BodySHA256) {
		return nil, ErrCorruptRecord
	}
	return &value, nil
}

func (store *Control) RegisterPublicInspectedBackupTarget(scope BackupTargetRegistrationScope, marker *actionv1.FilesystemBackupTargetInspectionReceipt,
	location *actionv1.FilesystemBackupTargetLocationReceipt, proofs []BackupTargetPathProof, bodySHA string, document json.RawMessage) (Record, OperatorMutationResult, error) {
	if !strings.HasPrefix(scope.operationID, PublicTargetOperationPrefix) {
		return Record{}, OperatorMutationResult{}, ErrTargetRegistrationInvalid
	}
	value := publicTargetContext{Version: 1, BodySHA256: bodySHA, Scope: scope.publicScope(), Document: append(json.RawMessage(nil), document...)}
	for _, proof := range proofs {
		if proof.Receipt == nil || proto.Size(proof.Receipt) > MaxBackupTargetLocationReceiptBytes {
			return Record{}, OperatorMutationResult{}, ErrTargetRegistrationInvalid
		}
		wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(proof.Receipt)
		if err != nil {
			return Record{}, OperatorMutationResult{}, err
		}
		value.Paths = append(value.Paths, publicTargetPathProof{proof.TargetID, wire})
	}
	sort.Slice(value.Paths, func(i, j int) bool { return value.Paths[i].TargetID < value.Paths[j].TargetID })
	encoded, err := json.Marshal(value)
	if err != nil || len(encoded) > maximumPublicTargetContext {
		return Record{}, OperatorMutationResult{}, ErrTargetRegistrationInvalid
	}
	return store.registerInspectedBackupTarget(scope, marker, location, document, encoded)
}

// PublicBackupTargetRecord removes durable admission internals from all user projections.
func PublicBackupTargetRecord(payload json.RawMessage) (json.RawMessage, error) {
	var fields map[string]json.RawMessage
	if decodeSingleJSON(payload, &fields) != nil || fields == nil {
		return nil, ErrCorruptRecord
	}
	delete(fields, publicTargetContextField)
	return json.Marshal(fields)
}

func (store *Control) BackupTargetObservationBinding() BackupTargetObservationContext {
	store.mu.Lock()
	defer store.mu.Unlock()
	return BackupTargetObservationContext{PublicKey: store.promotionSignerKey, FleetID: store.fleetID, Environment: store.environment, Now: store.now()}
}

func samePublicTargetContext(before, after []byte) bool {
	read := func(payload []byte) (*publicTargetContext, error) {
		if len(payload) == 0 {
			return nil, nil
		}
		return decodePublicTargetContext(payload)
	}
	a, err := read(before)
	if err != nil {
		return false
	}
	b, err := read(after)
	if err != nil {
		return false
	}
	left, err := json.Marshal(a)
	if err != nil {
		return false
	}
	right, err := json.Marshal(b)
	return err == nil && bytes.Equal(left, right)
}

func validatePublicTargetBindingsTx(tx *sql.Tx, id string, bindings map[int64]targetRegistrationBinding) error {
	for revision, binding := range bindings {
		var eventID, recorded int64
		var payload []byte
		if err := tx.QueryRow(`SELECT event_id,recorded_at,payload_json FROM control_events WHERE domain='target' AND record_id=? AND revision=?`, id, revision).Scan(&eventID, &recorded, &payload); err != nil {
			return err
		}
		frame, err := decodePublicTargetContext(payload)
		if err != nil {
			return err
		}
		required := strings.HasPrefix(binding.operation, PublicTargetOperationPrefix)
		if !required {
			if frame != nil {
				return ErrCorruptRecord
			}
			continue
		}
		if frame == nil || frame.Scope.OperationID != binding.operation || frame.Scope.ActionID != binding.result.ActionID || frame.Scope.Actor != binding.result.Actor ||
			frame.Scope.CutoverRevision != binding.result.CutoverRevision || frame.Scope.Epoch != binding.result.ExecutionEpoch ||
			frame.Scope.RequestedPath != binding.receipt.Observation.RequestedPath || frame.Scope.WriterFence != int64(binding.receipt.Observation.WriterFencingToken) {
			return ErrCorruptRecord
		}
		records, err := targetRecordsAtEvent(tx, eventID)
		if err != nil {
			return err
		}
		snapshot, err := snapshotRecords(records)
		if err != nil {
			return err
		}
		if snapshot != frame.Scope.Snapshot {
			return ErrCorruptRecord
		}
		paths, err := targetPaths(records, id)
		if err != nil {
			return err
		}
		if verifyPublicTargetPaths(frame.Scope.scope(), binding.receipt, paths, frame.Paths, recorded) != nil {
			return ErrCorruptRecord
		}
		body, observedID, err := targetRegistrationDocument(binding.receipt.Observation, frame.Document)
		if err != nil || observedID != id {
			return ErrCorruptRecord
		}
		if _, exists := body[publicTargetContextField]; exists {
			return ErrCorruptRecord
		}
		encoded, err := json.Marshal(frame)
		if err != nil {
			return err
		}
		marker, err := proto.MarshalOptions{Deterministic: true}.Marshal(binding.receipt)
		if err != nil {
			return err
		}
		material := map[string]any{"command": "backup-target-register-v1", "operationId": frame.Scope.OperationID, "actionId": frame.Scope.ActionID,
			"actor": frame.Scope.Actor, "snapshot": frame.Scope.Snapshot, "writerFence": frame.Scope.WriterFence, "cutoverRevision": frame.Scope.CutoverRevision,
			"epoch": frame.Scope.Epoch, "document": frame.Document, "observation": hex.EncodeToString(marker)}
		if binding.location != nil {
			wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(binding.location)
			if err != nil {
				return err
			}
			material["locationObservation"] = hex.EncodeToString(wire)
		}
		digest := sha256.Sum256(encoded)
		material["publicRegistrationContextSHA256"] = hex.EncodeToString(digest[:])
		raw, err := canonicalAuthorityJSON(material)
		if err != nil {
			return err
		}
		digest = sha256.Sum256(raw)
		if hex.EncodeToString(digest[:]) != binding.result.RequestDigest {
			return ErrCorruptRecord
		}
		body["topologyGeneration"] = binding.generation
		body["registeredAt"] = time.Unix(recorded, 0).UTC().Format("2006-01-02T15:04:05Z")
		body[publicTargetContextField] = json.RawMessage(encoded)
		expected, err := json.Marshal(body)
		if err != nil {
			return err
		}
		payloadDigest, err := operatorPayloadDigest(expected)
		if err != nil || payloadDigest != binding.result.PayloadDigest {
			return ErrCorruptRecord
		}
	}
	return nil
}

// A committed public request can be recovered using its original request bytes
// without re-reading an expired filesystem receipt or issuing another mutation.
func (store *Control) ReplayPublicBackupTargetRegistration(operation, actor, bodySHA string) (Record, OperatorMutationResult, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if !strings.HasPrefix(operation, PublicTargetOperationPrefix) {
		return Record{}, OperatorMutationResult{}, false, ErrTargetRegistrationInvalid
	}
	tx, context, err := store.beginOperatorMutationTx(operation, operation, actor, "target", true)
	if err != nil {
		return Record{}, OperatorMutationResult{}, false, err
	}
	defer tx.Rollback()
	result, found, err := readOperatorMutationTx(tx, operation)
	if err != nil {
		return Record{}, OperatorMutationResult{}, false, err
	}
	if !found {
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return Record{}, OperatorMutationResult{}, false, err
		}
		return Record{}, OperatorMutationResult{}, false, nil
	}
	if result.Actor != actor || result.Domain != "target" || result.ActionID != operation {
		return Record{}, OperatorMutationResult{}, false, ErrMutationRequestReplayConflict
	}
	current, found, err := readTargetMetadataTx(tx, result.RecordID)
	if err != nil {
		return Record{}, OperatorMutationResult{}, false, err
	}
	if !found {
		return Record{}, OperatorMutationResult{}, false, ErrCorruptRecord
	}
	if err := validateControlHistory(tx, current); err != nil {
		return Record{}, OperatorMutationResult{}, false, err
	}
	record, err := readTargetMutationResultTx(tx, result)
	if err != nil {
		return Record{}, OperatorMutationResult{}, false, err
	}
	frame, err := decodePublicTargetContext(record.Payload)
	if err != nil || frame == nil {
		return Record{}, OperatorMutationResult{}, false, ErrCorruptRecord
	}
	if frame.BodySHA256 != bodySHA {
		return Record{}, OperatorMutationResult{}, false, ErrMutationRequestReplayConflict
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return Record{}, OperatorMutationResult{}, false, err
	}
	result.Status = OperatorMutationAlreadyApplied
	return record, result, true, nil
}
