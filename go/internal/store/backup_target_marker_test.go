package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
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
	commitFault := inventoryReadFault{commit: true, armed: true}
	installInventoryReadFault(t, control, &commitFault)
	if read, err := control.AdmitTargetMarker(scope, absent, nil, body); err == nil || read.Claim != nil || !commitFault.injected {
		t.Fatal("an interrupted repeat admission returned the original claim as confirmed", err)
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
	if permit, err := control.BeginTargetMarkerDispatch(&actionv1.TargetMarkerDispatchRequest{}); permit != nil || !errors.Is(err, ErrTargetMarkerClaim) {
		t.Fatal("an incomplete request obtained a marker permit", err)
	}
	observedScope, firstRead, firstDocument := targetMarkerReadFixture(t, control, operation, effect)
	if _, _, err := control.RegisterPublicInitializedBackupTarget(observedScope, firstRead, nil, body, firstDocument, effect); !errors.Is(err, ErrTargetMarkerClaim) {
		t.Fatal("an effect could settle before authorizing its dispatch", err)
	}
	if pending, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body); err != nil || !found || pending.State != "CLAIMED" {
		t.Fatal("premature settlement disturbed the original claim", found, err)
	}
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
	for _, fault := range []inventoryReadFault{
		{query: "SELECT operation_id FROM control_target_marker_operations WHERE state"},
		{query: "SELECT execution_epoch,actor,claim,claim_sha256,state"},
		{commit: true},
	} {
		installInventoryReadFault(t, control, &fault)
		fault.armed = true
		if pending, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body); err == nil || found || pending.Claim != nil || !fault.injected {
			t.Fatal("read fault erased evidence of a previously dispatched effect", fault.query, err)
		}
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
	for _, change := range []func(*actionv1.TargetMarkerReceipt){
		func(receipt *actionv1.TargetMarkerReceipt) { receipt.Effect = nil },
		func(receipt *actionv1.TargetMarkerReceipt) { receipt.Effect.ClaimSha256[0] ^= 1 },
		func(receipt *actionv1.TargetMarkerReceipt) { receipt.Effect.MarkerSha256[0] ^= 1 },
	} {
		bad := proto.Clone(effect).(*actionv1.TargetMarkerReceipt)
		change(bad)
		if _, _, err := recovered.RegisterPublicInitializedBackupTarget(fresh, observed, nil, body, document, bad); !errors.Is(err, ErrTargetMarkerClaim) {
			t.Fatal("unbound effect receipt settled", err)
		}
	}
	for _, fault := range []inventoryReadFault{
		{query: "SELECT execution_epoch,actor,claim,claim_sha256,state"},
		{query: "UPDATE control_target_marker_operations SET state='SETTLED'", exec: true},
		{query: "UPDATE control_target_marker_operations SET state='SETTLED'", exec: true, zeroAffected: true},
		{query: "INSERT INTO control_target_registrations", exec: true},
		{query: "SELECT execution_epoch,actor,claim,claim_sha256,state", armAfterExec: "INSERT INTO control_target_registrations"},
		{commit: true},
	} {
		installInventoryReadFault(t, recovered, &fault)
		fault.armed = fault.armAfterExec == ""
		if _, _, err := recovered.RegisterPublicInitializedBackupTarget(fresh, observed, nil, body, document, effect); err == nil || !fault.injected {
			t.Fatal("effect settlement was not coupled to registration commit", fault.query, err)
		}
		if pending, found, err := recovered.PendingTargetMarker(scope.operationID, scope.actor, body); err != nil || !found || pending.State != "DISPATCHED" {
			t.Fatal("rollback consumed the initial effect", found, err)
		}
	}
	record, result, err := recovered.RegisterPublicInitializedBackupTarget(fresh, observed, nil, body, document, effect)
	if err != nil || record.ID != operation.Claim.TargetId || result.Status != OperatorMutationApplied {
		t.Fatal("authenticated original effect did not settle", err)
	}
	current, reply, found, err := recovered.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body)
	if err != nil || !found || !reflect.DeepEqual(current, record) || reply.Status != OperatorMutationAlreadyApplied {
		t.Fatal("lost reply did not replay the original committed result", found, err)
	}
	frame, err := decodePublicTargetContext(record.Payload)
	if err != nil || frame == nil {
		t.Fatal("original marker event lacks its signed custody context", err)
	}
	// These faults use a disposable database inside a rolled-back transaction.
	withRegistrationRollback(t, recovered, func(tx *sql.Tx) {
		if _, err := tx.Exec("DROP TRIGGER control_target_marker_advance"); err != nil {
			t.Fatal(err)
		}
		for _, damaged := range []*actionv1.TargetMarkerReceipt{nil, forged} {
			wire := []byte{0xff}
			if damaged != nil {
				var err error
				wire, _, err = markerMessageBytes(damaged)
				if err != nil {
					t.Fatal(err)
				}
			}
			if _, err := tx.Exec("UPDATE control_target_marker_operations SET receipt=? WHERE operation_id=?", wire, scope.operationID); err != nil {
				t.Fatal(err)
			}
			if err := validateRetainedTargetMarkerTx(tx, frame, record.ID, observed); !errors.Is(err, ErrCorruptRecord) {
				t.Fatal("corrupt retained effect could authorize registration replay", err)
			}
		}
	})
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
	fault := inventoryReadFault{query: "UPDATE control_target_marker_operations SET state='ABORTED'", exec: true, armed: true}
	installInventoryReadFault(t, control, &fault)
	if pending, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body); !errors.Is(err, errInventoryReadInterrupted) || found || pending.Claim != nil {
		t.Fatal("an interrupted expiration looked like a new eligible request", found, err)
	}
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

func TestTargetRegistrationMarkerRefusesUntrustedAbsenceAndOverlapBeforeClaim(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	root := t.TempDir()
	registered := filepath.Join(root, "registered")
	original, originalRead, originalDoc := registrationFixture(t, control, "target-inspect-previous-marker", "target_prior", registered)
	if _, _, err := control.RegisterInspectedBackupTarget(original, originalRead, originalDoc); err != nil {
		t.Fatal(err)
	}
	scope, absent, body := absentMarkerFixture(t, control, "bad-absence", filepath.Join(root, "candidate"))
	paths, err := control.BackupTargetRegistrationPaths(scope, "")
	if err != nil || len(paths) != 1 {
		t.Fatal("original competing target unavailable", err)
	}
	proof := publicPathFixture(t, control, scope, paths[0], registered)
	present := proto.Clone(absent).(*actionv1.FilesystemBackupTargetInspectionReceipt)
	present.Observation.MarkerPresent = true
	present.Observation.MarkerJson = originalRead.Observation.MarkerJson
	present.Observation.MarkerSha256 = originalRead.Observation.MarkerSha256
	signRegistrationFixture(t, present)
	missingIdentity := proto.Clone(absent).(*actionv1.FilesystemBackupTargetInspectionReceipt)
	missingIdentity.Observation.DirectoryIdentity = nil
	signRegistrationFixture(t, missingIdentity)
	staleScope := scope
	staleScope.snapshot = strings.Repeat("0", 64)
	oldWriter := scope
	oldWriter.writerFence--
	overlap := publicPathFixture(t, control, scope, paths[0], root)

	for _, test := range []struct {
		name   string
		scope  BackupTargetRegistrationScope
		read   *actionv1.FilesystemBackupTargetInspectionReceipt
		proofs []BackupTargetPathProof
		digest string
		want   error
	}{
		{"invalid-body", scope, absent, []BackupTargetPathProof{proof}, "bad", ErrTargetMarkerClaim},
		{"present", scope, present, []BackupTargetPathProof{proof}, body, ErrTargetMarkerClaim},
		{"missing-directory-id", scope, missingIdentity, []BackupTargetPathProof{proof}, body, ErrTargetMarkerClaim},
		{"stale-snapshot", staleScope, absent, []BackupTargetPathProof{proof}, body, ErrRevisionConflict},
		{"old-writer", oldWriter, absent, []BackupTargetPathProof{proof}, body, ErrTargetMarkerClaim},
		{"missing-path-proof", scope, absent, nil, body, ErrTargetRegistrationInvalid},
		{"invalid-path-proof", scope, absent, []BackupTargetPathProof{{TargetID: paths[0].TargetID}}, body, ErrTargetRegistrationInvalid},
		{"overlap", scope, absent, []BackupTargetPathProof{overlap}, body, ErrTargetOverlap},
	} {
		t.Run(test.name, func(t *testing.T) {
			if op, err := control.AdmitTargetMarker(test.scope, test.read, test.proofs, test.digest); !errors.Is(err, test.want) || op.Claim != nil {
				t.Fatal("unsafe absence produced a claim", err)
			}
			if _, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body); err != nil || found {
				t.Fatal("denied admission entered the journal", found, err)
			}
		})
	}
	if _, err := control.AdmitTargetMarker(scope, absent, []BackupTargetPathProof{proof}, body); err != nil {
		t.Fatal("valid absence and non-overlap did not obtain a claim", err)
	}
}

func TestTargetRegistrationMarkerAdmissionAndDispatchFaultsPreserveOriginalState(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, absent, body := absentMarkerFixture(t, control, "marker-transaction-faults", filepath.Join(t.TempDir(), "candidate"))
	for _, fault := range []inventoryReadFault{
		{query: "SELECT operation_id FROM control_target_marker_operations WHERE state"},
		{query: "SELECT execution_epoch,actor,claim,claim_sha256,state"},
		{query: "FROM targets ORDER BY id"},
		{query: "SELECT EXISTS(SELECT 1 FROM control_target_marker_operations"},
		{query: "INSERT INTO control_target_marker_operations", exec: true},
		{commit: true},
	} {
		installInventoryReadFault(t, control, &fault)
		fault.armed = true
		if op, err := control.AdmitTargetMarker(scope, absent, nil, body); !errors.Is(err, errInventoryReadInterrupted) || !fault.injected || op.Claim != nil {
			t.Fatal("a fault admitted an initialization", fault.query, err)
		}
		if _, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body); err != nil || found {
			t.Fatal("admission fault retained a partial claim", found, err)
		}
	}
	operation, err := control.AdmitTargetMarker(scope, absent, nil, body)
	if err != nil {
		t.Fatal(err)
	}
	request, _ := targetMarkerEffectFixture(t, operation)
	for _, fault := range []inventoryReadFault{
		{query: "SELECT execution_epoch,actor,claim,claim_sha256,state"},
		{query: "UPDATE control_target_marker_operations SET state='DISPATCHED'", exec: true},
		{query: "UPDATE control_target_marker_operations SET state='DISPATCHED'", exec: true, zeroAffected: true},
		{commit: true},
	} {
		installInventoryReadFault(t, control, &fault)
		fault.armed = true
		if permit, err := control.BeginTargetMarkerDispatch(request); err == nil || !fault.injected || permit != nil {
			t.Fatal("a failed transaction issued a permit", fault.query, err)
		}
		current, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body)
		if err != nil || !found || current.State != "CLAIMED" || !proto.Equal(current.Claim, operation.Claim) {
			t.Fatal("dispatch fault changed the original claim", found, err)
		}
	}
	if _, err := control.BeginTargetMarkerDispatch(request); err != nil {
		t.Fatal("proven rollback could not use original claim", err)
	}
}

func TestTargetRegistrationMarkerCorruptCustodyCannotLookUnclaimed(t *testing.T) {
	for _, corruption := range []string{"encoding", "digest", "row-binding"} {
		t.Run(corruption, func(t *testing.T) {
			control := targetDeletePromotedStore(t, true)
			scope, absent, body := absentMarkerFixture(t, control, "corrupt-"+corruption, filepath.Join(t.TempDir(), "candidate"))
			requestHash, err := hex.DecodeString(body)
			if err != nil {
				t.Fatal(err)
			}
			claim := &actionv1.TargetMarkerClaim{SchemaVersion: 1, OperationId: scope.operationID, WriterFencingToken: uint64(scope.writerFence),
				Fence: &commonv1.ActionFence{ActionId: scope.operationID, ExecutionEpoch: 1}, CutoverRevision: uint64(scope.cutoverRevision),
				IssuedAtUnix: 1000, LeaseUntilUnix: 1020, RequestedPath: scope.requestedPath, CanonicalPath: absent.Observation.CanonicalPath,
				TargetId: "target_202020202020", Nonce: bytes.Repeat([]byte{0x20}, 32), RequestSha256: requestHash,
				DirectoryIdentity: absent.Observation.DirectoryIdentity, FleetId: absent.Observation.FleetId, Environment: absent.Observation.Environment}
			if corruption == "row-binding" {
				claim.Fence.ExecutionEpoch++
			}
			wire, digest, err := markerMessageBytes(claim)
			if err != nil {
				t.Fatal(err)
			}
			switch corruption {
			case "encoding":
				wire = []byte{0xff}
				digest = sha256.Sum256(wire)
			case "digest":
				digest[0] ^= 1
			}
			if _, err := control.db.Exec("INSERT INTO control_target_marker_operations(operation_id,execution_epoch,actor,claim,claim_sha256,state) VALUES(?,1,?,?,?,'CLAIMED')",
				scope.operationID, scope.actor, wire, digest[:]); err != nil {
				t.Fatal(err)
			}
			if pending, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body); !errors.Is(err, ErrCorruptRecord) || found || pending.Claim != nil {
				t.Fatal("corruption appeared as absent or eligible operation", found, err)
			}
			if op, err := control.AdmitTargetMarker(scope, absent, nil, body); !errors.Is(err, ErrCorruptRecord) || op.Claim != nil {
				t.Fatal("new effect claim replaced damaged evidence", err)
			}
			if permit, err := control.BeginTargetMarkerDispatch(&actionv1.TargetMarkerDispatchRequest{
				Claim: claim, Fence: claim.Fence, PreparationSha256: requestHash,
			}); !errors.Is(err, ErrCorruptRecord) || permit != nil {
				t.Fatal("damaged evidence qualified for dispatch", err)
			}
		})
	}
}

func TestTargetRegistrationMarkerConsumedOperationCannotIssueFreshClaim(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, past, document, body := publicRegistrationFixture(t, control, "consumed", "target_prior", filepath.Join(t.TempDir(), "prior"))
	if _, _, err := control.RegisterPublicInspectedBackupTarget(scope, past, nil, nil, body, document); err != nil {
		t.Fatal(err)
	}
	_, absent, _ := absentMarkerFixture(t, control, "consumed", scope.requestedPath)
	historyFault := inventoryReadFault{query: "SELECT operation_id, domain, record_id, record_revision", armed: true}
	installInventoryReadFault(t, control, &historyFault)
	if op, err := control.AdmitTargetMarker(scope, absent, nil, body); !errors.Is(err, errInventoryReadInterrupted) || op.Claim != nil || !historyFault.injected {
		t.Fatal("could not distinguish inaccessible operator history from an unused operation", err)
	}
	if op, err := control.AdmitTargetMarker(scope, absent, nil, body); !errors.Is(err, ErrMutationRequestReplayConflict) || op.Claim != nil {
		t.Fatal("a consumed public request authorized a new filesystem effect", err)
	}
	if pending, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body); err != nil || found || pending.Claim != nil {
		t.Fatal("refused claim created marker history", found, err)
	}
	fresh, read, digest := absentMarkerFixture(t, control, "closed-store", filepath.Join(t.TempDir(), "fresh"))
	// Reuse the authentic competing path while constructing the valid claim.
	paths, err := control.BackupTargetRegistrationPaths(fresh, "")
	if err != nil || len(paths) != 1 {
		t.Fatal("fixture lacks prior registered path", paths, err)
	}
	proof := publicPathFixture(t, control, fresh, paths[0], paths[0].Path)
	op, err := control.AdmitTargetMarker(fresh, read, []BackupTargetPathProof{proof}, digest)
	if err != nil {
		t.Fatal(err)
	}
	if later, err := control.AdmitTargetMarker(fresh, read, []BackupTargetPathProof{proof}, body); !errors.Is(err, ErrMutationRequestReplayConflict) || later.Claim != nil {
		t.Fatal("pending claim accepted a different body", err)
	}
	prepared, receipt := targetMarkerEffectFixture(t, op)
	observed, observation, updated := targetMarkerReadFixture(t, control, op, receipt)
	if _, _, err := control.RegisterPublicInitializedBackupTarget(observed, observation, nil, digest, updated, nil); !errors.Is(err, ErrTargetMarkerClaim) {
		t.Fatal("initialization settled without any original effect proof", err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, found, err := control.PendingTargetMarker(fresh.operationID, fresh.actor, digest); found || !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatal("closed store appeared to have no eligible marker work", found, err)
	}
	if _, err := control.AdmitTargetMarker(fresh, read, []BackupTargetPathProof{proof}, digest); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatal("closed store admitted target initialization", err)
	}
	if grant, err := control.BeginTargetMarkerDispatch(prepared); grant != nil || !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatal("closed store released an original permit", err)
	}
}
