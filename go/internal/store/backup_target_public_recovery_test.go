package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
)

// Storage fixtures sign test receipts to exercise what the Go store retains.
// Physical reads and native key custody are verified in the process workflow.
func publicHistoryFixture(t *testing.T) (*Control, Record, BackupTargetRegistrationScope, string) {
	t.Helper()
	control := targetDeletePromotedStore(t, true)
	root := t.TempDir()
	other := filepath.Join(root, "other")
	firstScope, firstReceipt, firstDocument := registrationFixture(t, control, "target-inspect-competing-history", "target_other", other)
	if _, _, err := control.RegisterInspectedBackupTarget(firstScope, firstReceipt, firstDocument); err != nil {
		t.Fatal(err)
	}
	previous := filepath.Join(root, "previous")
	var original Record
	var moveScope BackupTargetRegistrationScope
	var moveBody string
	for _, step := range []struct{ suffix, path string }{{"original", previous}, {"move", filepath.Join(root, "moved")}} {
		scope, receipt, document, body := publicRegistrationFixture(t, control, step.suffix, "target_public", step.path)
		proof := publicPathFixture(t, control, scope, BackupTargetPath{TargetID: "target_other", Path: other}, other)
		var old *actionv1.FilesystemBackupTargetLocationReceipt
		if step.suffix == "move" {
			old = relocationFixture(t, control, previous, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING)
		}
		var err error
		original, _, err = control.RegisterPublicInspectedBackupTarget(scope, receipt, old, []BackupTargetPathProof{proof}, body, document)
		if err != nil {
			t.Fatal(err)
		}
		moveScope, moveBody = scope, body
	}
	binding := control.BackupTargetObservationBinding()
	if binding.PublicKey != promotionTestPublic || binding.FleetID != "fleet-a" || binding.Now != control.now() {
		t.Fatal("registration uses a different runtime signer or clock")
	}
	return control, original, moveScope, moveBody
}

func TestTargetRegistrationPublicHistoryRejectsConflictingScopeAndProof(t *testing.T) {
	control, original, _, _ := publicHistoryFixture(t)
	withRegistrationRollback(t, control, func(tx *sql.Tx) {
		bindings, err := loadTargetRegistrationBindingsTx(tx, original.ID)
		if err != nil {
			t.Fatal(err)
		}
		if err := validatePublicTargetBindingsTx(tx, original.ID, bindings); err != nil {
			t.Fatal("original relocation and its location witness must validate", err)
		}
		for _, changed := range []struct {
			name   string
			change func(*targetRegistrationBinding)
		}{
			{"legacy namespace", func(b *targetRegistrationBinding) { b.operation = "target-inspect-legacy" }},
			{"changed authority", func(b *targetRegistrationBinding) { b.result.ExecutionEpoch++ }},
			{"wrong request digest", func(b *targetRegistrationBinding) { b.result.RequestDigest = strings.Repeat("0", 64) }},
			{"wrong event payload digest", func(b *targetRegistrationBinding) { b.result.PayloadDigest = strings.Repeat("0", 64) }},
		} {
			binding := bindings[original.Revision]
			changed.change(&binding)
			if err := validatePublicTargetBindingsTx(tx, original.ID, map[int64]targetRegistrationBinding{original.Revision: binding}); !errors.Is(err, ErrCorruptRecord) {
				t.Fatalf("%s survived public history validation: %v", changed.name, err)
			}
		}
	})
	for _, row := range []struct {
		name   string
		change func(*publicTargetContext)
	}{
		{"unbound body hash", func(f *publicTargetContext) { f.BodySHA256 = strings.Repeat("G", 64) }},
		{"different registry snapshot", func(f *publicTargetContext) { f.Scope.Snapshot = strings.Repeat("0", 64) }},
		{"malformed competing witness", func(f *publicTargetContext) { f.Paths[0].Wire = []byte{0xff} }},
		{"different document identity", func(f *publicTargetContext) { f.Document = json.RawMessage(`{"targetId":"target_other"}`) }},
		{"private admission in response", func(f *publicTargetContext) {
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(f.Document, &fields); err != nil {
				t.Fatal(err)
			}
			fields[publicTargetContextField] = json.RawMessage(`{}`)
			f.Document, _ = json.Marshal(fields)
		}},
		{"missing frame", nil},
	} {
		t.Run(row.name, func(t *testing.T) {
			withRegistrationRollback(t, control, func(tx *sql.Tx) {
				bindings, err := loadTargetRegistrationBindingsTx(tx, original.ID)
				if err != nil {
					t.Fatal(err)
				}
				frame, err := decodePublicTargetContext(original.Payload)
				if err != nil || frame == nil || len(frame.Paths) != 1 {
					t.Fatal("fixture lost the competing path witness", err)
				}
				payload := json.RawMessage(`{}`)
				if row.change != nil {
					row.change(frame)
					payload, err = json.Marshal(map[string]any{publicTargetContextField: frame})
					if err != nil {
						t.Fatal(err)
					}
				}
				if _, err := tx.Exec("DROP TRIGGER control_events_no_update"); err != nil {
					t.Fatal(err)
				}
				if _, err := tx.Exec("UPDATE control_events SET payload_json=? WHERE domain='target' AND record_id=? AND revision=?", string(payload), original.ID, original.Revision); err != nil {
					t.Fatal(err)
				}
				if err := validatePublicTargetBindingsTx(tx, original.ID, bindings); !errors.Is(err, ErrCorruptRecord) {
					t.Fatal("damaged publication context appeared healthy", err)
				}
			})
		})
	}
	after, found, err := control.Get("target", original.ID)
	if err != nil || !found || !reflect.DeepEqual(original, after) {
		t.Fatal("temporary corruption fixture changed the committed record", err)
	}
}

func TestTargetRegistrationPublicReplayCannotTreatDatabaseFaultAsAbsence(t *testing.T) {
	control, original, scope, body := publicHistoryFixture(t)
	for _, fault := range []inventoryReadFault{
		{query: "FROM control_operator_mutations"},
		{query: "SELECT event_id,recorded_at,payload_json FROM control_events"},
		{query: "FROM control_events e WHERE e.domain='target' AND e.event_id=("},
		{query: "FROM control_events e WHERE e.domain='target' AND e.event_id=(", cursor: true},
		{query: "FROM control_events e WHERE e.domain='target' AND e.event_id=(", malformed: true},
		{query: "FROM control_events WHERE domain = ? AND record_id = ? AND revision = ?"},
		{commit: true},
	} {
		t.Run(fault.query, func(t *testing.T) {
			installInventoryReadFault(t, control, &fault)
			fault.armed = true
			if _, result, found, err := control.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body); err == nil || found || !fault.injected || result.Status != "" {
				t.Fatalf("interrupted read returned a usable replay: %+v %v injected=%v", result, err, fault.injected)
			}
			replayed, _, found, err := control.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body)
			if err != nil || !found || !reflect.DeepEqual(original, replayed) {
				t.Fatal("original committed result did not recover after a transient database fault", err)
			}
		})
	}
	if _, _, _, err := control.ReplayPublicBackupTargetRegistration("other-operation", scope.actor, body); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("a legacy operation was interpreted as public registration", err)
	}
	oldKey := control.promotionSignerKey
	control.promotionSignerKey = ""
	if _, _, found, err := control.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body); found || !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatal("replay did not require the configured signer pin", found, err)
	}
	control.promotionSignerKey = oldKey

	for _, present := range []bool{true, false} {
		operation := scope.operationID
		if !present {
			operation = PublicTargetOperationPrefix + "missing"
		}
		fault := inventoryReadFault{commit: true}
		installInventoryReadFault(t, control, &fault)
		fault.armed = true
		if _, _, found, err := control.ReplayPublicBackupTargetRegistration(operation, scope.actor, body); found || err == nil || !fault.injected {
			t.Fatal("commit failure did not invalidate the lookup result", found, err)
		}
	}
	_ = control.Close()
	if _, _, found, err := control.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body); found || err == nil {
		t.Fatal("closed store returned a usable replay", found, err)
	}
}

func TestTargetRegistrationPublicPathsUseOnlyActiveFilesystemRecords(t *testing.T) {
	path, _ := json.Marshal(filepath.Join(t.TempDir(), "filesystem"))
	document := json.RawMessage(`{"kind":"filesystem","path":` + string(path) + `}`)
	paths, err := targetPaths([]Record{
		{ID: "target_current", State: "ACTIVE", Payload: document},
		{ID: "target_deleted", State: TombstoneState, Payload: document},
		{ID: "target_s3", State: "ACTIVE", Payload: json.RawMessage(`{"kind":"s3"}`)},
		{ID: "target_managed_local", State: "ACTIVE", Payload: json.RawMessage(`{"kind":"filesystem"}`)},
		{ID: "target_competing", State: "ACTIVE", Payload: document},
	}, "target_current")
	if err != nil || len(paths) != 1 || paths[0].TargetID != "target_competing" {
		t.Fatalf("wrong competing filesystem paths: %v %v", paths, err)
	}
	for _, raw := range []string{`[]`, `{"path":"relative"}`, `{"path":"relative\u0000"}`} {
		if _, err := targetPaths([]Record{{ID: "target_damaged", State: "ACTIVE", Payload: json.RawMessage(raw)}}, ""); !errors.Is(err, ErrCorruptRecord) {
			t.Fatal("damaged active filesystem record disappeared from overlap checks", err)
		}
	}
}

func TestTargetRegistrationPublicPreparationDoesNotPublishPartialRegistryRead(t *testing.T) {
	control, _, _, _ := publicHistoryFixture(t)
	scope, _, _, _ := publicRegistrationFixture(t, control, "prepare-after-move", "target_new", filepath.Join(t.TempDir(), "candidate"))
	original, err := control.BackupTargetRegistrationPaths(scope, "target_new")
	if err != nil || len(original) != 2 {
		t.Fatal("complete registry failed before the interruption fixture", original, err)
	}
	for _, fault := range []inventoryReadFault{{query: "FROM targets ORDER BY id"}, {commit: true}} {
		installInventoryReadFault(t, control, &fault)
		fault.armed = true
		if paths, err := control.BackupTargetRegistrationPaths(scope, "target_new"); paths != nil || err == nil || !fault.injected {
			t.Fatal("a partial or uncommitted read became an overlap check", paths, err, fault.injected)
		}
		current, err := control.BackupTargetRegistrationPaths(scope, "target_new")
		if err != nil || !reflect.DeepEqual(current, original) {
			t.Fatal("a transient interruption discarded original competing paths", current, err)
		}
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if paths, err := control.BackupTargetRegistrationPaths(scope, "target_new"); paths != nil || err == nil {
		t.Fatal("a closed store reported a valid registry", paths, err)
	}
}

func TestTargetRegistrationPublicRechecksCurrentRecordAfterVerifyingPathProofs(t *testing.T) {
	control, original, _, _ := publicHistoryFixture(t)
	var retained struct {
		Path string
	}
	if err := json.Unmarshal(original.Payload, &retained); err != nil || !filepath.IsAbs(retained.Path) {
		t.Fatal("invalid original relocated record", err)
	}
	scope, receipt, document, body := publicRegistrationFixture(t, control, "retry-registered-path", original.ID, retained.Path)
	paths, err := control.BackupTargetRegistrationPaths(scope, original.ID)
	if err != nil || len(paths) != 1 {
		t.Fatal("competing path proof unavailable", paths, err)
	}
	pathProof := publicPathFixture(t, control, scope, paths[0], paths[0].Path)
	journalSize, err := control.OperatorJournalSize()
	if err != nil {
		t.Fatal(err)
	}
	for _, fault := range []inventoryReadFault{
		{query: "FROM targets WHERE id = ?", armed: true},
		{query: "SELECT event_id,recorded_at,payload_json FROM control_events", armAfter: "INSERT INTO control_target_registrations"},
	} {
		installInventoryReadFault(t, control, &fault)
		if _, result, err := control.RegisterPublicInspectedBackupTarget(scope, receipt, nil, []BackupTargetPathProof{pathProof}, body, document); !errors.Is(err, errInventoryReadInterrupted) || !fault.injected || result.Status != "" {
			t.Fatal("an interrupted identity or history read published a target", fault.query, result.Status, err)
		}
		if n, err := control.OperatorJournalSize(); err != nil || n != journalSize {
			t.Fatal("interrupted registration appended an operator result", n, err)
		}
		if current, found, err := control.Get("target", original.ID); err != nil || !found || !reflect.DeepEqual(current, original) {
			t.Fatal("interrupted registration changed the current target", err)
		}
		if _, _, found, err := control.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body); err != nil || found {
			t.Fatal("interrupted registration acquired a recoverable response", found, err)
		}
	}
	current, _, err := control.RegisterPublicInspectedBackupTarget(scope, receipt, nil, []BackupTargetPathProof{pathProof}, body, document)
	if err != nil || current.ID != original.ID || current.Revision != original.Revision+1 {
		t.Fatal("the original signed request did not recover after the read fault", err)
	}
}

func TestTargetRegistrationPublicMetadataWriteFailuresPreserveOriginalResult(t *testing.T) {
	for _, command := range []string{"activate", "delete"} {
		t.Run(command, func(t *testing.T) {
			control, original, scope, body := publicHistoryFixture(t)
			operation := "target-retained-registration-" + command
			mutate := func() (OperatorMutationResult, error) {
				if command == "activate" {
					_, result, err := control.ActivateBackupTarget(operation, operation, scope.actor, original.ID)
					return result, err
				}
				return control.DeleteBackupTarget(operation, operation, scope.actor, original.ID)
			}
			journalSize, err := control.OperatorJournalSize()
			if err != nil {
				t.Fatal(err)
			}
			for _, fault := range []inventoryReadFault{
				{query: "UPDATE targets", exec: true},
				{query: "INSERT INTO control_operator_mutations", exec: true},
				{commit: true},
			} {
				installInventoryReadFault(t, control, &fault)
				fault.armed = true
				if result, err := mutate(); err == nil || !fault.injected || result.Status != "" {
					t.Fatal("interrupted metadata mutation published a result", command, fault.query, result.Status, err)
				}
				if n, err := control.OperatorJournalSize(); err != nil || n != journalSize {
					t.Fatal("interrupted metadata mutation appended to the journal", n, err)
				}
				current, found, err := control.Get("target", original.ID)
				if err != nil || !found || !reflect.DeepEqual(original, current) {
					t.Fatal("interrupted metadata mutation replaced the registered target", err)
				}
			}
			result, err := mutate()
			expectedState := "ACTIVE"
			if command == "delete" {
				expectedState = TombstoneState
			}
			if err != nil || result.Revision != original.Revision+1 || result.State != expectedState {
				t.Fatal("the retained metadata operation could not retry", result, err)
			}
			replayFault := inventoryReadFault{commit: true}
			installInventoryReadFault(t, control, &replayFault)
			replayFault.armed = true
			if retry, err := mutate(); !errors.Is(err, errInventoryReadInterrupted) || retry.Status != "" || !replayFault.injected {
				t.Fatal("metadata replay reported success with a failed transaction", retry, err)
			}
			if retry, err := mutate(); err != nil || retry.Revision != result.Revision || retry.State != result.State {
				t.Fatal("metadata operation failed to recover its committed result", retry, err)
			}
			replayed, _, found, err := control.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body)
			if err != nil || !found || !reflect.DeepEqual(replayed, original) {
				t.Fatal("metadata mutation changed the original registration response", err)
			}
		})
	}
}

func TestTargetRegistrationPublicPreservesReservedContextAndRejectsMalformedFrames(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "candidate")
	scope, receipt, document, body := publicRegistrationFixture(t, control, "document-boundary", "target_public", path)
	if _, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("legacy entry admitted a public operation without its context", err)
	}
	legacy, _, _ := registrationFixture(t, control, "target-inspect-legacy", "target_other", path)
	if _, _, err := control.RegisterPublicInspectedBackupTarget(legacy, receipt, nil, nil, body, document); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatal("public entry accepted a legacy operation", err)
	}
	for _, candidate := range []json.RawMessage{
		json.RawMessage(`{"broken":`),
		json.RawMessage(`{"label":"` + strings.Repeat("x", maximumPublicTargetContext) + `"}`),
	} {
		if _, _, err := control.RegisterPublicInspectedBackupTarget(scope, receipt, nil, nil, body, candidate); !errors.Is(err, ErrTargetRegistrationInvalid) {
			t.Fatal("malformed or oversized registration context was admitted", err)
		}
	}
	invalidPath := string([]byte{0xff})
	invalidProof := &actionv1.FilesystemBackupTargetLocationReceipt{Observation: &actionv1.FilesystemBackupTargetLocationObservation{
		CanonicalPath: invalidPath,
	}}
	if _, _, err := control.RegisterPublicInspectedBackupTarget(scope, receipt, nil, []BackupTargetPathProof{
		{TargetID: "target_invalid", Receipt: invalidProof},
	}, body, document); err == nil {
		t.Fatal("unencodable competing observation entered registration")
	}
	if _, err := decodePublicTargetContext([]byte(`{"_nativeTargetRegistrationV1":"` + strings.Repeat("x", maximumPublicTargetContext) + `"}`)); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("oversized persisted context was ignored", err)
	}
	if samePublicTargetContext([]byte(`{}`), []byte(`{`)) {
		t.Fatal("a damaged successor frame looked unchanged")
	}
	if n, err := control.OperatorJournalSize(); err != nil || n != 0 {
		t.Fatal("malformed evidence entered the operator journal", n, err)
	}
}

func TestTargetRegistrationPublicInvalidLegacyPathDoesNotDisappearFromSnapshot(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	old := Record{Domain: "target", ID: "target_old", State: "ACTIVE", Revision: 1,
		Payload: json.RawMessage(`{"targetId":"target_old","path":"relative","kind":"filesystem","topologyGeneration":1}`)}
	if _, err := control.ApplyOperatorMutation(OperatorMutation{
		OperationID: "legacy-location", ActionID: "legacy-action", Actor: "operator:test", Record: old,
	}); err != nil {
		t.Fatal(err)
	}
	scope, receipt, document, body := publicRegistrationFixture(t, control, "unresolved-competitor", "target_new", filepath.Join(t.TempDir(), "candidate"))
	if paths, err := control.BackupTargetRegistrationPaths(scope, "target_new"); paths != nil || !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("a damaged legacy path was silently left out", paths, err)
	}
	if _, _, err := control.RegisterPublicInspectedBackupTarget(scope, receipt, nil, nil, body, document); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("registration committed without resolving every competitor", err)
	}
}
