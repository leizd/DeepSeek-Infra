package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	targetmodel "github.com/leizd/DeepSeek-Infra/go/internal/targets"
	"google.golang.org/protobuf/proto"
)

func absentMarkerFixture(t *testing.T, control *Control, op, path string) (BackupTargetRegistrationScope, *actionv1.FilesystemBackupTargetInspectionReceipt, string) {
	t.Helper()
	scope, read, _, digest := publicRegistrationFixture(t, control, op, "target_read_fixture", path)
	read.Observation.MarkerPresent = false
	read.Observation.MarkerJson, read.Observation.MarkerSha256 = nil, nil
	read.Observation.DirectoryIdentity = bytes.Repeat([]byte{1}, 32)
	signRegistrationFixture(t, read)
	return scope, read, digest
}

func targetMarkerEffectFixture(t *testing.T, operation TargetMarkerOperation) (*actionv1.TargetMarkerDispatchRequest, *actionv1.TargetMarkerReceipt) {
	t.Helper()
	claim := operation.Claim
	marker, err := json.Marshal(map[string]any{
		"schemaVersion": 3, "targetId": claim.TargetId, "targetNonce": strings.Repeat("a", 32),
		"incarnationId": "inc_0123456789abcdef", "ownerInstallationId": "inst_0123456789abcdef",
		"targetGeneration": 0, "latestCommitHash": strings.Repeat("0", 64),
		"createdAt": time.Unix(claim.IssuedAtUnix, 0).UTC().Format(time.RFC3339),
	})
	if err != nil {
		t.Fatal(err)
	}
	_, prep, err := markerMessageBytes(&actionv1.TargetMarkerPreparation{Claim: claim, DirectoryIdentity: claim.DirectoryIdentity, MarkerJson: marker})
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(marker)
	effect := &actionv1.TargetMarkerEffect{SchemaVersion: 1, Fence: claim.Fence, OperationId: claim.OperationId,
		ClaimSha256: operation.ClaimSHA256, PreparationSha256: prep[:], CanonicalPath: claim.CanonicalPath,
		DirectoryIdentity: claim.DirectoryIdentity, MarkerJson: marker, MarkerSha256: sum[:]}
	wire, _, err := markerMessageBytes(effect)
	if err != nil {
		t.Fatal(err)
	}
	sig := ed25519.Sign(promotionTestPrivate, append([]byte(targetMarkerSignatureDomain), wire...))
	return &actionv1.TargetMarkerDispatchRequest{Claim: claim, Fence: claim.Fence, PreparationSha256: prep[:]},
		&actionv1.TargetMarkerReceipt{Effect: effect, SignerPublicKey: promotionTestPublic, Signature: sig}
}

func targetMarkerReadFixture(t *testing.T, control *Control, op TargetMarkerOperation, effect *actionv1.TargetMarkerReceipt) (BackupTargetRegistrationScope,
	*actionv1.FilesystemBackupTargetInspectionReceipt, json.RawMessage) {
	t.Helper()
	scope, read, _ := absentMarkerFixture(t, control, strings.TrimPrefix(op.Claim.OperationId, PublicTargetOperationPrefix), op.Claim.RequestedPath)
	read.Observation.MarkerPresent = true
	read.Observation.MarkerJson = effect.Effect.MarkerJson
	read.Observation.MarkerSha256 = effect.Effect.MarkerSha256
	signRegistrationFixture(t, read)
	input, err := targetmodel.ReadInput([]byte(`{"kind":"filesystem","label":"initialized"}`))
	if err != nil {
		t.Fatal(err)
	}
	document, err := input.FilesystemDocument(effect.Effect.MarkerJson, op.Claim.CanonicalPath, read.Observation.ObservedAtUnix)
	if err != nil {
		t.Fatal(err)
	}
	return scope, read, document
}

func TestTargetRegistrationMarkerDispatchIsSingleAndOriginalResultRecoversAfterTakeover(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, absent, body := absentMarkerFixture(t, control, "new-marker", filepath.Join(t.TempDir(), "new"))
	if _, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body); found || err != nil {
		t.Fatal("new operation already claimed", found, err)
	}
	operation, err := control.AdmitTargetMarker(scope, absent, nil, body)
	if err != nil || operation.State != "CLAIMED" || operation.Claim.Fence.ExecutionEpoch != 1 {
		t.Fatal("legal absent target failed admission", err)
	}
	same, err := control.AdmitTargetMarker(scope, absent, nil, body)
	if err != nil || !proto.Equal(operation.Claim, same.Claim) {
		t.Fatal("admission changed the original action fence", err)
	}
	otherScope, otherAbsent, otherBody := absentMarkerFixture(t, control, "other-marker", filepath.Join(t.TempDir(), "other"))
	if _, err := control.AdmitTargetMarker(otherScope, otherAbsent, nil, otherBody); !errors.Is(err, ErrTargetMarkerPending) {
		t.Fatal("another target could start before original reconciliation", err)
	}
	if _, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, otherBody); found || !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatal("pending work accepted a different body", found, err)
	}
	dispatch, effect := targetMarkerEffectFixture(t, operation)
	badDispatch := proto.Clone(dispatch).(*actionv1.TargetMarkerDispatchRequest)
	badDispatch.Claim.Nonce[31] ^= 1
	if _, err := control.BeginTargetMarkerDispatch(badDispatch); !errors.Is(err, ErrTargetMarkerClaim) {
		t.Fatal("different claim used the original reservation", err)
	}
	permit, err := control.BeginTargetMarkerDispatch(dispatch)
	if err != nil || !bytes.Equal(permit.PreparationSha256, dispatch.PreparationSha256) || permit.LeaseUntilUnix != operation.Claim.LeaseUntilUnix {
		t.Fatal("valid preparation did not obtain the original permit", err)
	}
	if _, err := control.BeginTargetMarkerDispatch(dispatch); !errors.Is(err, ErrTargetMarkerClaim) {
		t.Fatal("second dispatch permit was issued", err)
	}

	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	recovered := reopenTargetDeleteStore(t, path, func() int64 { return 1001 })
	original, found, err := recovered.PendingTargetMarker(scope.operationID, scope.actor, body)
	if err != nil || !found || original.State != "DISPATCHED" || !proto.Equal(original.Claim, operation.Claim) {
		t.Fatal("takeover lost the original dispatched epoch", found, err)
	}
	fresh, observed, document := targetMarkerReadFixture(t, recovered, operation, effect)
	if fresh.writerFence <= scope.writerFence {
		t.Fatal("fixture did not advance the writer fence")
	}
	if _, _, err := recovered.RegisterPublicInspectedBackupTarget(fresh, observed, nil, nil, body, document); !errors.Is(err, ErrTargetMarkerPending) {
		t.Fatal("ordinary registration displaced an unfinished effect", err)
	}
	swapped := proto.Clone(observed).(*actionv1.FilesystemBackupTargetInspectionReceipt)
	swapped.Observation.DirectoryIdentity[0] ^= 1
	signRegistrationFixture(t, swapped)
	if _, _, err := recovered.RegisterPublicInitializedBackupTarget(fresh, swapped, nil, body, document, effect); !errors.Is(err, ErrTargetMarkerClaim) {
		t.Fatal("unchanged marker bytes from a replaced directory authorized settlement", err)
	}
	forged := proto.Clone(effect).(*actionv1.TargetMarkerReceipt)
	forged.Signature[0] ^= 1
	if _, _, err := recovered.RegisterPublicInitializedBackupTarget(fresh, observed, nil, body, document, forged); !errors.Is(err, ErrTargetMarkerClaim) {
		t.Fatal("forged receipt settled", err)
	}
	fault := inventoryReadFault{query: "INSERT INTO control_target_registrations", exec: true, armed: true}
	installInventoryReadFault(t, recovered, &fault)
	if _, _, err := recovered.RegisterPublicInitializedBackupTarget(fresh, observed, nil, body, document, effect); !errors.Is(err, errInventoryReadInterrupted) || !fault.injected {
		t.Fatal("effect settlement was not coupled to registration commit", err)
	}
	if pending, found, err := recovered.PendingTargetMarker(scope.operationID, scope.actor, body); err != nil || !found || pending.State != "DISPATCHED" {
		t.Fatal("rollback consumed the initial effect", found, err)
	}
	record, result, err := recovered.RegisterPublicInitializedBackupTarget(fresh, observed, nil, body, document, effect)
	if err != nil || record.ID != operation.Claim.TargetId || result.Status != OperatorMutationApplied {
		t.Fatal("authenticated original effect did not settle", err)
	}
	current, reply, found, err := recovered.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body)
	if err != nil || !found || !reflect.DeepEqual(current, record) || reply.Status != OperatorMutationAlreadyApplied {
		t.Fatal("lost reply did not replay the original committed result", found, err)
	}
	tx, err := recovered.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := retireEmptyTargetMarkersTx(tx); !errors.Is(err, ErrSchemaInactive) {
		t.Fatal("retirement ignored a durable marker effect", err)
	}
}

func TestTargetRegistrationMarkerOnlyAnUndispatchedClaimCanAdvanceEpoch(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, absent, body := absentMarkerFixture(t, control, "ungranted", filepath.Join(t.TempDir(), "new"))
	operation, err := control.AdmitTargetMarker(scope, absent, nil, body)
	if err != nil {
		t.Fatal(err)
	}
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	now := operation.Claim.LeaseUntilUnix + 1
	control = reopenTargetDeleteStore(t, path, func() int64 { return now })
	if _, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body); found || err != nil {
		t.Fatal("never-dispatched permit retained an effect", found, err)
	}
	fresh, observed, _ := absentMarkerFixture(t, control, "ungranted", operation.Claim.RequestedPath)
	next, err := control.AdmitTargetMarker(fresh, observed, nil, body)
	if err != nil || next.Claim.Fence.ExecutionEpoch != 2 || bytes.Equal(next.ClaimSHA256, operation.ClaimSHA256) {
		t.Fatal("expired, never-issued permit did not advance independently", err)
	}
	dispatch, _ := targetMarkerEffectFixture(t, operation)
	if _, err := control.BeginTargetMarkerDispatch(dispatch); !errors.Is(err, ErrTargetMarkerClaim) {
		t.Fatal("the expired original action was redispatched", err)
	}
}
