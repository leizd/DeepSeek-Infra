package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

// These signatures are isolated Go fixtures. Physical Rust location reads are
// qualified separately; a fixture never authorizes a real target or provider.
func registeredLocationFixture(t *testing.T) (*Control, Record, string) {
	t.Helper()
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "original")
	scope, receipt, document := registrationFixture(t, control, "target-inspect-location-first", "target_relocated", path)
	record, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document)
	if err != nil {
		t.Fatal(err)
	}
	return control, record, path
}

func relocationFixture(t *testing.T, control *Control, previous string, scope BackupTargetRegistrationScope, state actionv1.BackupTargetLocationState) *actionv1.FilesystemBackupTargetLocationReceipt {
	t.Helper()
	request := scope.LocationRequest(previous)
	receipt := &actionv1.FilesystemBackupTargetLocationReceipt{SchemaVersion: 1, SignerPublicKey: promotionTestPublic,
		Observation: &actionv1.FilesystemBackupTargetLocationObservation{SchemaVersion: 1, Fence: request.Fence,
			OperationId: request.OperationId, WriterFencingToken: request.WriterFencingToken, RequestedPath: request.Path,
			State: state, ObservedAtUnix: control.now(), FleetId: "fleet-a", Environment: "production"}}
	signTargetLocationFixture(t, receipt, targetLocationSignatureDomain)
	return receipt
}

func TestTargetRegistrationRelocationRequiresSignedMissingOriginalLocation(t *testing.T) {
	control, first, previous := registeredLocationFixture(t)
	scope, marker, document := registrationFixture(t, control, "target-inspect-location-move", first.ID, filepath.Join(t.TempDir(), "moved"))
	if _, _, err := control.RegisterInspectedBackupTarget(scope, marker, document); !errors.Is(err, ErrTargetLocationRequired) {
		t.Fatal("relocation without Rust observation accepted", err)
	}
	exists := relocationFixture(t, control, previous, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_EXISTS)
	if _, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, exists, document); !errors.Is(err, ErrTargetCloneDetected) {
		t.Fatal("live old path did not refuse a clone", err)
	}
	missing := relocationFixture(t, control, previous, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING)
	after, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, missing, document)
	if err != nil || after.Revision != 2 || publicTargetGeneration(t, after) != 2 {
		t.Fatal("signed relocation lost metadata or public generation", err)
	}
	var oldPath string
	var raw []byte
	if err := control.db.QueryRow("SELECT previous_path,observation FROM control_target_registration_locations WHERE operation_id=?", scope.operationID).Scan(&oldPath, &raw); err != nil {
		t.Fatal(err)
	}
	wire, _ := proto.MarshalOptions{Deterministic: true}.Marshal(missing)
	if oldPath != previous || !bytes.Equal(wire, raw) {
		t.Fatal("original signed location bytes were not retained")
	}
	stored, found, err := control.Get("target", first.ID)
	if err != nil || !found || !reflect.DeepEqual(stored, after) {
		t.Fatal("committed location binding was not readable", err)
	}
}

func TestTargetRegistrationRelocationReplaysOriginalProofAfterDeletionAndTakeover(t *testing.T) {
	control, first, previous := registeredLocationFixture(t)
	scope, marker, document := registrationFixture(t, control, "target-inspect-location-replay", first.ID, filepath.Join(t.TempDir(), "moved"))
	missing := relocationFixture(t, control, previous, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING)
	original, result, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, missing, document)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.DeleteBackupTarget("delete-relocation", "delete-relocation-action", "operator:test", first.ID); err != nil {
		t.Fatal(err)
	}
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	recovered := reopenTargetDeleteStore(t, path, func() int64 { return 5000 })
	record, replay, err := recovered.RegisterInspectedBackupTargetWithLocation(scope, marker, missing, document)
	if err != nil || replay.Status != OperatorMutationAlreadyApplied || !reflect.DeepEqual(record, original) {
		t.Fatal("replay replaced original result or expired its historical proof", err)
	}
	replay.Status = result.Status
	if !reflect.DeepEqual(replay, result) {
		t.Fatal("replay changed the original action/epoch/writer")
	}
	if _, _, err := recovered.RegisterInspectedBackupTarget(scope, marker, document); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatal("replay omitted its original location proof", err)
	}
	changed := proto.Clone(missing).(*actionv1.FilesystemBackupTargetLocationReceipt)
	changed.Observation.RequestedPath += "-other"
	signTargetLocationFixture(t, changed, targetLocationSignatureDomain)
	if _, _, err := recovered.RegisterInspectedBackupTargetWithLocation(scope, marker, changed, document); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatal("replay rebound a different location", err)
	}
	current, _, err := recovered.Get("target", first.ID)
	if err != nil || current.State != TombstoneState || current.Revision != 3 {
		t.Fatal("replay revived the latest tombstone", err)
	}
}

func TestTargetRegistrationRelocationRejectsUnboundUnresolvedAndExpiredProofs(t *testing.T) {
	control, first, previous := registeredLocationFixture(t)
	scope, marker, document := registrationFixture(t, control, "target-inspect-location-bindings", first.ID, filepath.Join(t.TempDir(), "moved"))
	original := relocationFixture(t, control, previous, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING)
	for name, change := range map[string]func(*actionv1.FilesystemBackupTargetLocationReceipt){
		"schema":             func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.SchemaVersion++ },
		"observation-schema": func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.SchemaVersion++ },
		"epoch":              func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.Fence.ExecutionEpoch++ },
		"operation":          func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.OperationId += "-other" },
		"writer":             func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.WriterFencingToken++ },
		"path":               func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.RequestedPath += "-other" },
		"fleet":              func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.FleetId += "-other" },
		"environment":        func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.Environment += "-other" },
		"key":                func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.SignerPublicKey = "not-pinned" },
		"unresolved": func(r *actionv1.FilesystemBackupTargetLocationReceipt) {
			r.Observation.State = actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_UNSPECIFIED
		},
		"future":  func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.ObservedAtUnix++ },
		"stale":   func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.Observation.ObservedAtUnix -= 61 },
		"unknown": func(r *actionv1.FilesystemBackupTargetLocationReceipt) { r.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
		"oversized": func(r *actionv1.FilesystemBackupTargetLocationReceipt) {
			r.Observation.Environment = strings.Repeat("x", MaxBackupTargetLocationReceiptBytes+1)
		},
	} {
		t.Run(name, func(t *testing.T) {
			bad := proto.Clone(original).(*actionv1.FilesystemBackupTargetLocationReceipt)
			change(bad)
			signTargetLocationFixture(t, bad, targetLocationSignatureDomain)
			if _, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, bad, document); err == nil {
				t.Fatal("unbound signed relocation committed")
			}
			current, _, err := control.Get("target", first.ID)
			if err != nil || !reflect.DeepEqual(current, first) {
				t.Fatal("refusal changed original target", err)
			}
		})
	}
	for _, wrongPurpose := range []bool{false, true} {
		bad := proto.Clone(original).(*actionv1.FilesystemBackupTargetLocationReceipt)
		if wrongPurpose {
			signTargetLocationFixture(t, bad, targetInspectionSignatureDomain)
		} else {
			bad.Signature[0] ^= 1
		}
		if _, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, bad, document); err == nil {
			t.Fatal("forged or cross-purpose proof accepted")
		}
	}
	unencodable := proto.Clone(original).(*actionv1.FilesystemBackupTargetLocationReceipt)
	unencodable.Observation.Environment = "\xff"
	if _, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, unencodable, document); err == nil {
		t.Fatal("unencodable location proof entered the journal")
	}
	if _, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, original, document); err != nil {
		t.Fatal("valid signed missing state failed after refusals", err)
	}
}

func TestTargetRegistrationLocationIsNotAnOptionalWitnessForOtherMutations(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "original")
	scope, marker, document := registrationFixture(t, control, "target-inspect-location-no-prior", "target_relocated", path)
	extra := relocationFixture(t, control, path, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING)
	if _, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, extra, document); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("unnecessary proof was accepted", err)
	}
	if _, _, err := control.RegisterInspectedBackupTarget(scope, marker, document); err != nil {
		t.Fatal(err)
	}
	scope, marker, document = registrationFixture(t, control, "target-inspect-location-same", "target_relocated", filepath.Join(path, "."))
	extra = relocationFixture(t, control, path, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING)
	if _, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, extra, document); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("same-path registration retained unrelated proof", err)
	}
	if _, _, err := control.RegisterInspectedBackupTarget(scope, marker, document); err != nil {
		t.Fatal(err)
	}
	if _, err := control.DeleteBackupTarget("delete-before-reregister", "delete-before-reregister", "operator:test", "target_relocated"); err != nil {
		t.Fatal(err)
	}
	scope, marker, document = registrationFixture(t, control, "target-inspect-location-after-delete", "target_relocated", filepath.Join(t.TempDir(), "new"))
	if record, _, err := control.RegisterInspectedBackupTarget(scope, marker, document); err != nil || publicTargetGeneration(t, record) != 1 {
		t.Fatal("deleted registrations invented a live-clone requirement", err)
	}
	for _, raw := range []json.RawMessage{[]byte(`null`), []byte(`{`), []byte(`{"path":"relative"}`), []byte(`{"path":""}`)} {
		if _, _, err := priorTargetRegistrationPath(Record{State: "ACTIVE", Payload: raw}, path); err == nil {
			t.Fatal("invalid prior metadata was treated as an absent path")
		}
	}
}
