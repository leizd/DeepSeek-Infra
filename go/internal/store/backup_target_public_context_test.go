package store

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

// These are isolated signed Go fixtures, not physical Rust read evidence.
func publicRegistrationFixture(t *testing.T, control *Control, suffix, id, path string) (BackupTargetRegistrationScope, *actionv1.FilesystemBackupTargetInspectionReceipt, json.RawMessage, string) {
	t.Helper()
	scope, receipt, document := registrationFixture(t, control, PublicTargetOperationPrefix+suffix, id, path)
	receipt.Observation.FullContainmentChecked = true
	signRegistrationFixture(t, receipt)
	digest := sha256.Sum256([]byte(suffix))
	return scope, receipt, document, hex.EncodeToString(digest[:])
}

func publicPathFixture(t *testing.T, control *Control, scope BackupTargetRegistrationScope, path BackupTargetPath, resolved string) BackupTargetPathProof {
	t.Helper()
	receipt := relocationFixture(t, control, path.Path, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_UNSPECIFIED)
	receipt.Observation.CanonicalPath = resolved
	signTargetLocationFixture(t, receipt, targetLocationSignatureDomain)
	return BackupTargetPathProof{TargetID: path.TargetID, Receipt: receipt}
}

func TestTargetRegistrationPublicRecoversOriginalReplyAfterExpiryDeletionAndTakeover(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, receipt, document, body := publicRegistrationFixture(t, control, "recover", "target_public", filepath.Join(t.TempDir(), "target"))
	if !scope.InspectionRequest().RequireFullContainment {
		t.Fatal("public inspection omitted complete containment")
	}
	first, result, err := control.RegisterPublicInspectedBackupTarget(scope, receipt, nil, nil, body, document)
	if err != nil {
		t.Fatal(err)
	}
	public, err := PublicBackupTargetRecord(first.Payload)
	if err != nil || bytes.Contains(public, []byte(publicTargetContextField)) || !bytes.Contains(first.Payload, []byte(publicTargetContextField)) {
		t.Fatal("durable evidence leaked or disappeared", err)
	}
	if _, err := control.DeleteBackupTarget("public-delete", "public-delete-action", "operator:test", first.ID); err != nil {
		t.Fatal(err)
	}
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	recovered := reopenTargetDeleteStore(t, path, func() int64 { return 1000 })
	replay, outcome, found, err := recovered.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body)
	if err != nil || !found || !reflect.DeepEqual(first, replay) || outcome.Status != OperatorMutationAlreadyApplied {
		t.Fatal("lost reply did not recover its original event", found, err)
	}
	outcome.Status = result.Status
	if !reflect.DeepEqual(result, outcome) {
		t.Fatal("recovery rebound historical writer or epoch")
	}
	for _, changed := range [][2]string{{"operator:other", body}, {scope.actor, strings.Repeat("0", 64)}} {
		if _, _, _, err := recovered.ReplayPublicBackupTargetRegistration(scope.operationID, changed[0], changed[1]); !errors.Is(err, ErrMutationRequestReplayConflict) {
			t.Fatal("replay accepted changed input", err)
		}
	}
	if _, _, found, err := recovered.ReplayPublicBackupTargetRegistration(PublicTargetOperationPrefix+"new", scope.actor, body); err != nil || found {
		t.Fatal("new request looked committed", found, err)
	}
	current, _, err := recovered.Get("target", first.ID)
	if err != nil || current.State != TombstoneState || current.Revision != 2 {
		t.Fatal("replay revived a deleted target", err)
	}
	if count, err := recovered.OperatorJournalSize(); err != nil || count != 2 {
		t.Fatal("recovery duplicated a mutation", count, err)
	}
}

func TestTargetRegistrationPublicBindsEverySnapshotPathAndPreservesHistoricalSnapshot(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	otherPath := filepath.Join(t.TempDir(), "other")
	os, or, od := registrationFixture(t, control, "target-inspect-legacy-other", "target_other", otherPath)
	if _, _, err := control.RegisterInspectedBackupTarget(os, or, od); err != nil {
		t.Fatal(err)
	}
	scope, receipt, document, body := publicRegistrationFixture(t, control, "snapshot", "target_public", filepath.Join(t.TempDir(), "target"))
	paths, err := control.BackupTargetRegistrationPaths(scope, "target_public")
	if err != nil || len(paths) != 1 || paths[0].TargetID != "target_other" {
		t.Fatal("snapshot lost other target", paths, err)
	}
	proof := publicPathFixture(t, control, scope, paths[0], otherPath)
	first, _, err := control.RegisterPublicInspectedBackupTarget(scope, receipt, nil, []BackupTargetPathProof{proof}, body, document)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.DeleteBackupTarget("other-delete", "other-delete-action", "operator:test", "target_other"); err != nil {
		t.Fatal(err)
	}
	current, found, err := control.Get("target", first.ID)
	if err != nil || !found || !reflect.DeepEqual(current, first) {
		t.Fatal("later registry mutation invalidated historical admission", err)
	}
	if _, _, _, err := control.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body); err != nil {
		t.Fatal(err)
	}
	if _, err := control.BackupTargetRegistrationPaths(scope, first.ID); !errors.Is(err, ErrRevisionConflict) {
		t.Fatal("old snapshot remained writable", err)
	}
}

func TestTargetRegistrationPublicRejectsMissingExtraReboundExpiredAndOverlappingPaths(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	otherPath := filepath.Join(t.TempDir(), "other")
	os, or, od := registrationFixture(t, control, "target-inspect-other-admission", "target_other", otherPath)
	if _, _, err := control.RegisterInspectedBackupTarget(os, or, od); err != nil {
		t.Fatal(err)
	}
	scope, marker, document, body := publicRegistrationFixture(t, control, "denials", "target_public", filepath.Join(t.TempDir(), "target"))
	proof := publicPathFixture(t, control, scope, BackupTargetPath{"target_other", otherPath}, otherPath)
	cases := []struct {
		name   string
		change func(*BackupTargetPathProof)
	}{
		{"wrong target", func(p *BackupTargetPathProof) { p.TargetID = "target_different" }},
		{"signature", func(p *BackupTargetPathProof) { p.Receipt.Signature[0] ^= 1 }},
		{"scope", func(p *BackupTargetPathProof) {
			p.Receipt.Observation.OperationId += "changed"
			signTargetLocationFixture(t, p.Receipt, targetLocationSignatureDomain)
		}},
		{"expired", func(p *BackupTargetPathProof) {
			p.Receipt.Observation.ObservedAtUnix -= 61
			signTargetLocationFixture(t, p.Receipt, targetLocationSignatureDomain)
		}},
		{"wrong purpose", func(p *BackupTargetPathProof) {
			p.Receipt.Observation.State = actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_EXISTS
			signTargetLocationFixture(t, p.Receipt, targetLocationSignatureDomain)
		}},
		{"parent overlap", func(p *BackupTargetPathProof) {
			p.Receipt.Observation.CanonicalPath = filepath.Dir(marker.Observation.CanonicalPath)
			signTargetLocationFixture(t, p.Receipt, targetLocationSignatureDomain)
		}},
		{"child overlap", func(p *BackupTargetPathProof) {
			p.Receipt.Observation.CanonicalPath = filepath.Join(marker.Observation.CanonicalPath, "child")
			signTargetLocationFixture(t, p.Receipt, targetLocationSignatureDomain)
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := BackupTargetPathProof{proof.TargetID, proto.Clone(proof.Receipt).(*actionv1.FilesystemBackupTargetLocationReceipt)}
			tc.change(&p)
			if _, _, err := control.RegisterPublicInspectedBackupTarget(scope, marker, nil, []BackupTargetPathProof{p}, body, document); err == nil {
				t.Fatal("unbound path was admitted")
			}
		})
	}
	for _, proofs := range [][]BackupTargetPathProof{nil, {proof, proof}, {{TargetID: "target_other"}}} {
		if _, _, err := control.RegisterPublicInspectedBackupTarget(scope, marker, nil, proofs, body, document); err == nil {
			t.Fatal("incomplete or duplicate proof was admitted")
		}
	}
	downgraded := proto.Clone(marker).(*actionv1.FilesystemBackupTargetInspectionReceipt)
	downgraded.Observation.FullContainmentChecked = false
	signRegistrationFixture(t, downgraded)
	if _, _, err := control.RegisterPublicInspectedBackupTarget(scope, downgraded, nil, []BackupTargetPathProof{proof}, body, document); err == nil {
		t.Fatal("old worker bypassed full containment")
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 1 {
		t.Fatal("refusal published state", count, err)
	}
	if _, found, err := control.Get("target", "target_public"); err != nil || found {
		t.Fatal("refusal fabricated target", err)
	}
}

func TestTargetRegistrationPublicContextCannotBeRemovedOrInjectedByOrdinaryMetadataWrites(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, receipt, document, body := publicRegistrationFixture(t, control, "immutable", "target_public", filepath.Join(t.TempDir(), "target"))
	first, _, err := control.RegisterPublicInspectedBackupTarget(scope, receipt, nil, nil, body, document)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(first.Payload, &fields); err != nil {
		t.Fatal(err)
	}
	fields["topologyGeneration"] = json.RawMessage("2")
	delete(fields, publicTargetContextField)
	changed := first
	changed.Revision++
	changed.Payload, _ = json.Marshal(fields)
	if _, err := control.ApplyOperatorMutation(OperatorMutation{OperationID: "ordinary-metadata", ActionID: "ordinary-action", Actor: "operator:test", Record: changed}); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("ordinary write removed the admission", err)
	}
	active, _, err := control.ActivateBackupTarget("public-activate", "public-activate-action", "operator:test", first.ID)
	if err != nil || !samePublicTargetContext(first.Payload, active.Payload) {
		t.Fatal("ordinary activation lost immutable context", err)
	}
	if _, err := control.DeleteBackupTarget("public-immutable-delete", "public-immutable-delete-action", "operator:test", first.ID); err != nil {
		t.Fatal(err)
	}
	current, _, err := control.Get("target", first.ID)
	if err != nil || !samePublicTargetContext(first.Payload, current.Payload) {
		t.Fatal("deletion lost retained context", err)
	}
}

func TestTargetRegistrationPublicContextRejectsMalformedFramesAndKeepsUserProjectionClean(t *testing.T) {
	for _, raw := range []string{`{`, `{"_nativeTargetRegistrationV1":null}`, `{"_nativeTargetRegistrationV1":{}}`,
		`{"_nativeTargetRegistrationV1":{"Version":1,"BodySHA256":"bad"}}`} {
		if _, err := decodePublicTargetContext([]byte(raw)); err == nil {
			t.Fatal("damaged private frame looked healthy", raw)
		}
	}
	for _, raw := range []string{`[]`, `null`, `1`, `"legacy scalar"`, `{"legacy":true}`} {
		if frame, err := decodePublicTargetContext([]byte(raw)); err != nil || frame != nil {
			t.Fatal("legacy payload invented private context", raw, err)
		}
	}
	if !samePublicTargetContext([]byte(`{"legacy":1}`), []byte(`{"legacy":2}`)) || samePublicTargetContext([]byte(`{`), []byte(`{}`)) {
		t.Fatal("private comparison hid damage or compared unrelated fields")
	}
	for _, raw := range []string{`null`, `[]`, `{`} {
		if _, err := PublicBackupTargetRecord([]byte(raw)); err == nil {
			t.Fatal("non-object target looked public", raw)
		}
	}
	if SameBackupTargetLocation(filepath.Join("a", "b"), filepath.Join("a", "bc")) || !pathsOverlap(filepath.Join("a", "b"), filepath.Join("a", "b", "child")) || pathsOverlap(filepath.Join("a", "b"), filepath.Join("a", "bc")) {
		t.Fatal("containment confused component boundaries")
	}
}

func TestTargetRegistrationPublicAtomicFailureCannotPublishContextOrReplay(t *testing.T) {
	for _, fault := range []inventoryReadFault{{query: "INSERT INTO control_events", exec: true}, {query: "INSERT INTO control_target_registrations", exec: true}, {commit: true}} {
		t.Run(fault.query, func(t *testing.T) {
			control := targetDeletePromotedStore(t, true)
			scope, receipt, document, body := publicRegistrationFixture(t, control, "atomic", "target_public", filepath.Join(t.TempDir(), "target"))
			installInventoryReadFault(t, control, &fault)
			fault.armed = true
			if _, _, err := control.RegisterPublicInspectedBackupTarget(scope, receipt, nil, nil, body, document); err == nil || !fault.injected {
				t.Fatal("database interruption was hidden", err, fault.injected)
			}
			if _, _, found, err := control.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body); err != nil || found {
				t.Fatal("partial registration looked committed", found, err)
			}
			if _, found, err := control.Get("target", "target_public"); err != nil || found {
				t.Fatal("failed transaction published target", found, err)
			}
		})
	}
}
