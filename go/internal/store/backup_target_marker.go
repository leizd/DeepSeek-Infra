package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"path/filepath"
	"strings"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

var ErrTargetMarkerPending = errors.New("BACKUP_TARGET_MARKER_EFFECT_UNKNOWN")
var ErrTargetMarkerClaim = errors.New("BACKUP_TARGET_MARKER_CLAIM_DENIED")

const targetMarkerSignatureDomain = "deepseek-infra:backup-target-marker-effect-v1\x00"
const MaxTargetMarkerReceiptBytes = 64 * 1024

type TargetMarkerOperation struct {
	Claim       *actionv1.TargetMarkerClaim
	ClaimSHA256 []byte
	State       string
	actor       string
	preparation []byte
	receipt     []byte
}

func targetMarkerValidClaim(c *actionv1.TargetMarkerClaim) bool {
	return c != nil && c.Fence != nil && nativeKnown(c, c.Fence) && c.SchemaVersion == 1 &&
		strings.HasPrefix(c.OperationId, PublicTargetOperationPrefix) && len(c.OperationId) <= 128 &&
		c.Fence.ActionId == c.OperationId && c.Fence.ExecutionEpoch > 0 && c.Fence.ExecutionEpoch <= math.MaxInt64 &&
		c.WriterFencingToken > 0 && c.WriterFencingToken <= math.MaxInt64 && c.CutoverRevision > 0 && c.CutoverRevision <= math.MaxInt64 &&
		c.IssuedAtUnix > 0 && c.LeaseUntilUnix > c.IssuedAtUnix && c.LeaseUntilUnix-c.IssuedAtUnix <= 30 &&
		len(c.RequestedPath) <= 32768 && len(c.CanonicalPath) <= 32768 &&
		filepath.IsAbs(c.RequestedPath) && filepath.IsAbs(c.CanonicalPath) &&
		!strings.ContainsRune(c.RequestedPath, 0) && !strings.ContainsRune(c.CanonicalPath, 0) &&
		len(c.RequestSha256) == 32 && len(c.Nonce) == 32 && c.TargetId == "target_"+hex.EncodeToString(c.Nonce[:6]) &&
		c.FleetId != "" && c.Environment != "" && len(c.DirectoryIdentity) == 32
}

func markerMessageBytes(message proto.Message) ([]byte, [32]byte, error) {
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(message)
	return wire, sha256.Sum256(wire), err
}

func targetMarkerOperationTx(tx *sql.Tx, operation string) (TargetMarkerOperation, error) {
	var epoch uint64
	var claim []byte
	var row TargetMarkerOperation
	err := tx.QueryRow("SELECT execution_epoch,actor,claim,claim_sha256,state,preparation_sha256,receipt FROM control_target_marker_operations WHERE operation_id=? ORDER BY execution_epoch DESC LIMIT 1", operation).
		Scan(&epoch, &row.actor, &claim, &row.ClaimSHA256, &row.State, &row.preparation, &row.receipt)
	if err != nil {
		return TargetMarkerOperation{}, err
	}
	row.Claim = &actionv1.TargetMarkerClaim{}
	if proto.Unmarshal(claim, row.Claim) != nil || !targetMarkerValidClaim(row.Claim) ||
		row.Claim.OperationId != operation || row.Claim.Fence.ExecutionEpoch != epoch || row.actor == "" {
		return TargetMarkerOperation{}, ErrCorruptRecord
	}
	canonical, digest, err := markerMessageBytes(row.Claim)
	if err != nil || !bytes.Equal(canonical, claim) || !bytes.Equal(digest[:], row.ClaimSHA256) {
		return TargetMarkerOperation{}, ErrCorruptRecord
	}
	return row, nil
}

func expireUndispatchedTargetMarkersTx(tx *sql.Tx, now int64, writer int64) error {
	// Rows must be decoded and verified before expiration. In particular, an
	// interrupted lookup of a dispatched intent cannot release the slot.
	var pending string
	err := tx.QueryRow("SELECT operation_id FROM control_target_marker_operations WHERE state IN ('CLAIMED','DISPATCHED')").Scan(&pending)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	row, err := targetMarkerOperationTx(tx, pending)
	if err != nil {
		return err
	}
	if row.State != "CLAIMED" {
		return nil
	}
	if row.Claim.LeaseUntilUnix > now && row.Claim.WriterFencingToken == uint64(writer) {
		return nil
	}
	_, err = tx.Exec("UPDATE control_target_marker_operations SET state='ABORTED' WHERE operation_id=? AND execution_epoch=? AND state='CLAIMED'",
		pending, row.Claim.Fence.ExecutionEpoch)
	return err
}

// PendingTargetMarker checks the original HTTP request binding before any new
// observation, so a retry after marker publication queries the original effect.
func (store *Control) PendingTargetMarker(operation, actor, bodySHA string) (TargetMarkerOperation, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.schema < SchemaV23 {
		return TargetMarkerOperation{}, false, nil
	}
	tx, context, err := store.beginOperatorMutationTx(operation, operation, actor, "target", true)
	if err != nil {
		return TargetMarkerOperation{}, false, err
	}
	defer tx.Rollback()
	if err := expireUndispatchedTargetMarkersTx(tx, context.now, store.token); err != nil {
		return TargetMarkerOperation{}, false, err
	}
	row, err := targetMarkerOperationTx(tx, operation)
	found := err == nil && row.State != "ABORTED"
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return TargetMarkerOperation{}, false, err
	}
	if err == nil && (row.actor != actor || hex.EncodeToString(row.Claim.RequestSha256) != bodySHA) {
		return TargetMarkerOperation{}, false, ErrMutationRequestReplayConflict
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return TargetMarkerOperation{}, false, err
	}
	return row, found, nil
}

func (store *Control) AdmitTargetMarker(scope BackupTargetRegistrationScope, absence *actionv1.FilesystemBackupTargetInspectionReceipt,
	proofs []BackupTargetPathProof, bodySHA string) (TargetMarkerOperation, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	bodyDigest, err := hex.DecodeString(bodySHA)
	if store.schema < SchemaV23 || !strings.HasPrefix(scope.operationID, PublicTargetOperationPrefix) || err != nil || len(bodyDigest) != 32 {
		return TargetMarkerOperation{}, ErrTargetMarkerClaim
	}
	tx, context, err := store.beginOperatorMutationTx(scope.operationID, scope.actionID, scope.actor, "target", true)
	if err != nil {
		return TargetMarkerOperation{}, err
	}
	defer tx.Rollback()
	if err := expireUndispatchedTargetMarkersTx(tx, context.now, store.token); err != nil {
		return TargetMarkerOperation{}, err
	}
	previous, err := targetMarkerOperationTx(tx, scope.operationID)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return TargetMarkerOperation{}, err
	}
	var epoch uint64 = 1
	if err == nil {
		if previous.actor != scope.actor || !bytes.Equal(previous.Claim.RequestSha256, bodyDigest) {
			return TargetMarkerOperation{}, ErrMutationRequestReplayConflict
		}
		if previous.State != "ABORTED" {
			if err := store.finishOperatorMutationTx(tx, context); err != nil {
				return TargetMarkerOperation{}, err
			}
			return previous, nil
		}
		if previous.Claim.Fence.ExecutionEpoch >= math.MaxInt64 {
			return TargetMarkerOperation{}, ErrTargetMarkerClaim
		}
		epoch = previous.Claim.Fence.ExecutionEpoch + 1
	}
	if _, found, err := readOperatorMutationTx(tx, scope.operationID); err != nil {
		return TargetMarkerOperation{}, err
	} else if found {
		return TargetMarkerOperation{}, ErrMutationRequestReplayConflict
	}
	if scope.writerFence != store.token || scope.cutoverRevision != context.cutover.Revision || scope.epoch != uint64(context.cutover.Epoch) {
		return TargetMarkerOperation{}, ErrTargetMarkerClaim
	}
	binding := BackupTargetObservationContext{PublicKey: store.promotionSignerKey, FleetID: store.fleetID, Environment: store.environment, Now: context.now}
	if err := VerifyBackupTargetInspectionReceipt(scope.InspectionRequest(), absence, binding); err != nil ||
		absence.Observation.MarkerPresent || !absence.Observation.FullContainmentChecked || len(absence.Observation.DirectoryIdentity) != 32 {
		return TargetMarkerOperation{}, ErrTargetMarkerClaim
	}
	records, snapshot, err := targetRegistrySnapshotTx(tx)
	if err != nil {
		return TargetMarkerOperation{}, err
	}
	if snapshot != scope.snapshot {
		return TargetMarkerOperation{}, ErrRevisionConflict
	}
	paths, err := targetPaths(records, "")
	if err != nil {
		return TargetMarkerOperation{}, err
	}
	verifiedPaths, err := encodePublicTargetPathProofs(proofs)
	if err != nil {
		return TargetMarkerOperation{}, err
	}
	if err := verifyPublicTargetPaths(scope, absence, paths, verifiedPaths, context.now); err != nil {
		return TargetMarkerOperation{}, err
	}
	var active bool
	if err := tx.QueryRow("SELECT " + targetMarkerPending).Scan(&active); err != nil {
		return TargetMarkerOperation{}, err
	}
	if active {
		return TargetMarkerOperation{}, ErrTargetMarkerPending
	}

	nonce := make([]byte, 32)
	if _, err := rand.Read(nonce); err != nil {
		return TargetMarkerOperation{}, err
	}
	targetID := "target_" + hex.EncodeToString(nonce[:6])
	for _, record := range records {
		if record.ID == targetID {
			return TargetMarkerOperation{}, ErrRevisionConflict
		}
	}
	if context.now <= 0 || context.now > math.MaxInt64-30 {
		return TargetMarkerOperation{}, ErrTargetMarkerClaim
	}
	claim := &actionv1.TargetMarkerClaim{
		SchemaVersion: 1, Fence: &commonv1.ActionFence{ActionId: scope.operationID, ExecutionEpoch: epoch}, OperationId: scope.operationID,
		WriterFencingToken: uint64(store.token), CutoverRevision: uint64(context.cutover.Revision),
		IssuedAtUnix: context.now, LeaseUntilUnix: min(context.leaseUntil, context.now+30),
		RequestedPath: scope.requestedPath, CanonicalPath: absence.Observation.CanonicalPath, TargetId: targetID,
		RequestSha256: bodyDigest, Nonce: nonce, FleetId: store.fleetID, Environment: store.environment,
		DirectoryIdentity: bytes.Clone(absence.Observation.DirectoryIdentity),
	}
	if !targetMarkerValidClaim(claim) {
		return TargetMarkerOperation{}, ErrTargetMarkerClaim
	}
	encoded, digest, err := markerMessageBytes(claim)
	if err != nil {
		return TargetMarkerOperation{}, err
	}
	if _, err := tx.Exec("INSERT INTO control_target_marker_operations(operation_id,execution_epoch,actor,claim,claim_sha256,state) VALUES(?,?,?,?,?,'CLAIMED')",
		claim.OperationId, epoch, scope.actor, encoded, digest[:]); err != nil {
		return TargetMarkerOperation{}, err
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return TargetMarkerOperation{}, err
	}
	return TargetMarkerOperation{Claim: claim, ClaimSHA256: digest[:], State: "CLAIMED", actor: scope.actor}, nil
}

// BeginTargetMarkerDispatch is called by the Rust worker over the existing
// authenticated control RPC after it durably records the original preparation.
func (store *Control) BeginTargetMarkerDispatch(request *actionv1.TargetMarkerDispatchRequest) (*actionv1.TargetMarkerDispatchPermit, error) {
	if request == nil || !targetMarkerValidClaim(request.Claim) ||
		!proto.Equal(request.Fence, request.Claim.Fence) || !nativeKnown(request) || len(request.PreparationSha256) != 32 {
		return nil, ErrTargetMarkerClaim
	}
	claim := request.Claim
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.schema < SchemaV23 {
		return nil, ErrTargetMarkerClaim
	}
	tx, context, err := store.beginOperatorMutationTx(claim.OperationId, claim.OperationId, "worker:target-marker", "target", true)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	operation, err := targetMarkerOperationTx(tx, claim.OperationId)
	if err != nil {
		return nil, err
	}
	if operation.State != "CLAIMED" || !proto.Equal(operation.Claim, claim) ||
		claim.WriterFencingToken != uint64(store.token) || claim.CutoverRevision != uint64(context.cutover.Revision) ||
		context.now < claim.IssuedAtUnix || context.now >= claim.LeaseUntilUnix {
		return nil, ErrTargetMarkerClaim
	}
	change, err := tx.Exec("UPDATE control_target_marker_operations SET state='DISPATCHED',preparation_sha256=? WHERE operation_id=? AND execution_epoch=? AND state='CLAIMED'",
		request.PreparationSha256, claim.OperationId, claim.Fence.ExecutionEpoch)
	if err != nil {
		return nil, err
	}
	count, err := change.RowsAffected()
	if err != nil || count != 1 {
		return nil, ErrTargetMarkerClaim
	}
	context.actionLeaseUntil = claim.LeaseUntilUnix
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return nil, err
	}
	return &actionv1.TargetMarkerDispatchPermit{SchemaVersion: 1, PreparationSha256: bytes.Clone(request.PreparationSha256), LeaseUntilUnix: claim.LeaseUntilUnix}, nil
}

func verifyTargetMarkerReceipt(operation TargetMarkerOperation, receipt *actionv1.TargetMarkerReceipt, signer string) error {
	if receipt == nil || receipt.Effect == nil || receipt.Effect.Fence == nil ||
		proto.Size(receipt) > MaxTargetMarkerReceiptBytes ||
		!nativeKnown(receipt, receipt.Effect, receipt.Effect.Fence) || receipt.SignerPublicKey != signer ||
		len(receipt.Signature) != ed25519.SignatureSize || proto.Size(receipt) > 65536 {
		return ErrTargetMarkerClaim
	}
	effect, claim := receipt.Effect, operation.Claim
	if !targetMarkerValidClaim(claim) || effect.SchemaVersion != 1 || !proto.Equal(effect.Fence, claim.Fence) ||
		effect.OperationId != claim.OperationId || effect.CanonicalPath != claim.CanonicalPath ||
		!bytes.Equal(effect.ClaimSha256, operation.ClaimSHA256) || !bytes.Equal(effect.PreparationSha256, operation.preparation) ||
		!bytes.Equal(effect.DirectoryIdentity, claim.DirectoryIdentity) || len(effect.MarkerJson) == 0 || len(effect.MarkerJson) > 4096 {
		return ErrTargetMarkerClaim
	}
	markerHash := sha256.Sum256(effect.MarkerJson)
	_, preparedHash, err := markerMessageBytes(&actionv1.TargetMarkerPreparation{
		Claim: claim, DirectoryIdentity: effect.DirectoryIdentity, MarkerJson: effect.MarkerJson,
	})
	if err != nil || !bytes.Equal(markerHash[:], effect.MarkerSha256) || !bytes.Equal(preparedHash[:], effect.PreparationSha256) {
		return ErrTargetMarkerClaim
	}
	var marker struct {
		SchemaVersion       int
		TargetID            string
		TargetNonce         string
		IncarnationID       string
		OwnerInstallationID string
		TargetGeneration    *uint64
		LatestCommitHash    string
		CreatedAt           string
	}
	expectedTime := time.Unix(claim.IssuedAtUnix, 0).UTC().Format("2006-01-02T15:04:05Z")
	if json.Unmarshal(effect.MarkerJson, &marker) != nil || marker.SchemaVersion != 3 ||
		marker.TargetID != claim.TargetId || len(marker.TargetNonce) != 32 ||
		!strings.HasPrefix(marker.IncarnationID, "inc_") || len(marker.IncarnationID) != 20 ||
		marker.OwnerInstallationID == "" || marker.TargetGeneration == nil || *marker.TargetGeneration != 0 ||
		marker.LatestCommitHash != strings.Repeat("0", 64) || marker.CreatedAt != expectedTime {
		return ErrTargetMarkerClaim
	}
	key, err := decodeFixedBase64(signer, ed25519.PublicKeySize)
	if err != nil {
		return ErrTargetMarkerClaim
	}
	wire, _, err := markerMessageBytes(effect)
	if err != nil || !ed25519.Verify(key, append([]byte(targetMarkerSignatureDomain), wire...), receipt.Signature) {
		return ErrTargetMarkerClaim
	}
	return nil
}

func bindTargetMarkerRegistration(operation TargetMarkerOperation, frame *publicTargetContext, id string,
	inspection *actionv1.FilesystemBackupTargetInspectionReceipt, receipt *actionv1.TargetMarkerReceipt) ([]byte, error) {
	claim := operation.Claim
	observed := inspection.Observation
	if claim.OperationId != frame.Scope.OperationID || operation.actor != frame.Scope.Actor ||
		hex.EncodeToString(claim.RequestSha256) != frame.BodySHA256 || claim.TargetId != id ||
		claim.WriterFencingToken > observed.WriterFencingToken || claim.CutoverRevision != uint64(frame.Scope.CutoverRevision) ||
		claim.RequestedPath != observed.RequestedPath || claim.CanonicalPath != observed.CanonicalPath ||
		claim.FleetId != observed.FleetId || claim.Environment != observed.Environment ||
		!observed.FullContainmentChecked || !observed.MarkerPresent || !bytes.Equal(claim.DirectoryIdentity, observed.DirectoryIdentity) ||
		verifyTargetMarkerReceipt(operation, receipt, inspection.SignerPublicKey) != nil ||
		!bytes.Equal(receipt.Effect.MarkerJson, observed.MarkerJson) {
		return nil, ErrTargetMarkerClaim
	}
	encoded, digest, err := markerMessageBytes(receipt)
	if err != nil || hex.EncodeToString(digest[:]) != frame.MarkerEffectSHA256 {
		return nil, ErrTargetMarkerClaim
	}
	return encoded, nil
}

func settleTargetMarkerTx(tx *sql.Tx, frame *publicTargetContext, id string,
	inspection *actionv1.FilesystemBackupTargetInspectionReceipt, receipt *actionv1.TargetMarkerReceipt) error {
	operation, err := targetMarkerOperationTx(tx, frame.Scope.OperationID)
	if err != nil {
		return err
	}
	if operation.State != "DISPATCHED" {
		return ErrTargetMarkerClaim
	}
	encoded, err := bindTargetMarkerRegistration(operation, frame, id, inspection, receipt)
	if err != nil {
		return err
	}
	change, err := tx.Exec("UPDATE control_target_marker_operations SET state='SETTLED',receipt=? WHERE operation_id=? AND execution_epoch=? AND state='DISPATCHED'",
		encoded, operation.Claim.OperationId, operation.Claim.Fence.ExecutionEpoch)
	if err != nil {
		return err
	}
	count, err := change.RowsAffected()
	if err != nil || count != 1 {
		return ErrTargetMarkerClaim
	}
	return nil
}

func validateRetainedTargetMarkerTx(tx *sql.Tx, frame *publicTargetContext, id string,
	inspection *actionv1.FilesystemBackupTargetInspectionReceipt) error {
	operation, err := targetMarkerOperationTx(tx, frame.Scope.OperationID)
	if err != nil {
		return err
	}
	receipt := &actionv1.TargetMarkerReceipt{}
	if operation.State != "SETTLED" || proto.Unmarshal(operation.receipt, receipt) != nil {
		return ErrCorruptRecord
	}
	canonical, err := bindTargetMarkerRegistration(operation, frame, id, inspection, receipt)
	if err != nil || !bytes.Equal(canonical, operation.receipt) {
		return ErrCorruptRecord
	}
	return nil
}
