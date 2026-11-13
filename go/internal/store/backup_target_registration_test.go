package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"path/filepath"
	"reflect"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

// These use the existing public promotion-test identity. They test Go admission
// only; fixture signatures are never evidence of a physical Rust inspection.
func registrationFixture(t *testing.T, control *Control, operation, id, path string) (BackupTargetRegistrationScope, *actionv1.FilesystemBackupTargetInspectionReceipt, json.RawMessage) {
	t.Helper()
	scope, err := control.PrepareBackupTargetRegistration(operation, operation, "operator:test", path)
	if err != nil {
		t.Fatal(err)
	}
	request := scope.InspectionRequest()
	marker, _ := json.Marshal(map[string]any{"schemaVersion": 3, "targetId": id, "targetNonce": "original-nonce", "incarnationId": "inc_original", "createdAt": "2026-11-11T00:00:00Z", "targetGeneration": json.Number("9007199254740993")})
	digest := sha256.Sum256(marker)
	observation := &actionv1.FilesystemBackupTargetObservation{SchemaVersion: 1, Fence: request.Fence,
		OperationId: operation, WriterFencingToken: request.WriterFencingToken,
		RequestedPath: path, CanonicalPath: filepath.Clean(path), MarkerPresent: true,
		MarkerJson: marker, MarkerSha256: digest[:], ObservedAtUnix: control.now(),
		FleetId: "fleet-a", Environment: "production"}
	receipt := &actionv1.FilesystemBackupTargetInspectionReceipt{SchemaVersion: 1, Observation: observation, SignerPublicKey: promotionTestPublic}
	signRegistrationFixture(t, receipt)
	document, _ := json.Marshal(map[string]any{"schemaVersion": 3, "targetId": id, "kind": "filesystem", "path": filepath.Clean(path), "targetNonce": "original-nonce", "label": "registered", "drainState": "active", "createdAt": "2026-11-11T00:00:00Z"})
	return scope, receipt, document
}

func signRegistrationFixture(t *testing.T, receipt *actionv1.FilesystemBackupTargetInspectionReceipt) {
	t.Helper()
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Observation)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(promotionTestPrivate, append([]byte("deepseek-infra:backup-target-inspection-v1\x00"), wire...))
}

func publicTargetGeneration(t *testing.T, record Record) int64 {
	t.Helper()
	var document map[string]any
	if err := decodeSingleJSON(record.Payload, &document); err != nil {
		t.Fatal(err)
	}
	value, err := document["topologyGeneration"].(json.Number).Int64()
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func TestTargetRegistrationRetainsInternalHistoryAcrossOriginalPublicGenerationResets(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "original-directory")
	for cycle := 0; cycle < 3; cycle++ {
		operation := []string{"target-inspect-first", "target-inspect-second", "target-inspect-third"}[cycle]
		scope, receipt, document := registrationFixture(t, control, operation, "target_original", path)
		record, result, err := control.RegisterInspectedBackupTarget(scope, receipt, document)
		if err != nil || record.Revision != int64(cycle*3+1) || publicTargetGeneration(t, record) != 1 || result.ActionID != operation {
			t.Fatalf("same-id registration reset internal history or failed to reset public generation: %+v %+v %v", record, result, err)
		}
		active, _, err := control.ActivateBackupTarget("activate-"+operation, operation+"-action", "operator:test", "target_original")
		if err != nil || active.Revision != record.Revision+1 || publicTargetGeneration(t, active) != 2 {
			t.Fatalf("activation confused the public and internal counters: %+v %v", active, err)
		}
		if _, err := control.DeleteBackupTarget("delete-"+operation, operation+"-delete-action", "operator:test", "target_original"); err != nil {
			t.Fatal(err)
		}
	}
	record, exists, err := control.Get("target", "target_original")
	if err != nil || !exists || record.Revision != 9 || record.State != TombstoneState || publicTargetGeneration(t, record) != 3 {
		t.Fatalf("retained terminal history was not readable: %+v %v %v", record, exists, err)
	}
	var events, registrations int
	_ = control.db.QueryRow("SELECT COUNT(*) FROM control_events WHERE domain='target' AND record_id='target_original'").Scan(&events)
	_ = control.db.QueryRow("SELECT COUNT(*) FROM control_target_registrations").Scan(&registrations)
	if events != 9 || registrations != 3 {
		t.Fatalf("history was dropped or manufactured: events=%d registrations=%d", events, registrations)
	}
}

func TestTargetRegistrationReplaysItsOriginalDocumentAfterDeletionAndWriterTakeover(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, receipt, document := registrationFixture(t, control, "target-inspect-replay", "target_original", filepath.Join(t.TempDir(), "target"))
	first, result, err := control.RegisterInspectedBackupTarget(scope, receipt, document)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.DeleteBackupTarget("delete-after-registration", "delete-action", "operator:test", first.ID); err != nil {
		t.Fatal(err)
	}
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	recovered := reopenTargetDeleteStore(t, path, func() int64 { return 1000 })
	replayed, replay, err := recovered.RegisterInspectedBackupTarget(scope, receipt, document)
	if err != nil || replay.Status != OperatorMutationAlreadyApplied || !reflect.DeepEqual(first, replayed) {
		t.Fatalf("replay used current tombstone/writer as the original result: %+v %+v %v", replayed, replay, err)
	}
	replay.Status = result.Status
	if !reflect.DeepEqual(result, replay) {
		t.Fatal("replay rebound the original action, epoch or fence")
	}
	changed := append(json.RawMessage(nil), document...)
	changed = bytes.Replace(changed, []byte("registered"), []byte("changed"), 1)
	if _, _, err := recovered.RegisterInspectedBackupTarget(scope, receipt, changed); !errors.Is(err, ErrMutationRequestReplayConflict) {
		t.Fatalf("the operation accepted a different request: %v", err)
	}
	latest, _, err := recovered.Get("target", first.ID)
	if err != nil || latest.State != TombstoneState || latest.Revision != 2 {
		t.Fatal("replay revived or rewrote the current target")
	}
}

func TestTargetRegistrationRejectsUnboundObservationsWithoutCommitting(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "target")
	scope, original, document := registrationFixture(t, control, "target-inspect-admission", "target_original", path)
	for _, mutation := range []func(*actionv1.FilesystemBackupTargetInspectionReceipt){
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) { r.Signature[0] ^= 1 },
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.MarkerSha256[0] ^= 1
			signRegistrationFixture(t, r)
		},
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.Fence.ExecutionEpoch++
			signRegistrationFixture(t, r)
		},
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.WriterFencingToken++
			signRegistrationFixture(t, r)
		},
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.ObservedAtUnix -= 61
			signRegistrationFixture(t, r)
		},
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.CanonicalPath += "other"
			signRegistrationFixture(t, r)
		},
		func(r *actionv1.FilesystemBackupTargetInspectionReceipt) {
			r.Observation.FleetId = "other"
			signRegistrationFixture(t, r)
		},
	} {
		receipt := proto.Clone(original).(*actionv1.FilesystemBackupTargetInspectionReceipt)
		mutation(receipt)
		if _, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document); err == nil {
			t.Fatal("an unbound observation became a target registration")
		}
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 0 {
		t.Fatalf("refused input changed durable authority: %d %v", count, err)
	}
	if _, exists, err := control.Get("target", "target_original"); err != nil || exists {
		t.Fatalf("refused input became an apparently valid target: %v %v", exists, err)
	}
}

func TestTargetRegistrationRequiresTheUnchangedRegistrySnapshot(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, receipt, document := registrationFixture(t, control, "target-inspect-raced", "target_original", filepath.Join(t.TempDir(), "target"))
	otherScope, otherReceipt, otherDocument := registrationFixture(t, control, "target-inspect-winner", "target_other", filepath.Join(t.TempDir(), "other"))
	if _, _, err := control.RegisterInspectedBackupTarget(otherScope, otherReceipt, otherDocument); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document); !errors.Is(err, ErrRevisionConflict) {
		t.Fatalf("a stale registry read could authorize a write: %v", err)
	}
}

func TestTargetRegistrationWitnessAndIdentityCannotBeDeletedOrReplaced(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, receipt, document := registrationFixture(t, control, "target-inspect-immutable", "target_original", filepath.Join(t.TempDir(), "target"))
	if _, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document); err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
		"DELETE FROM control_target_registrations", "UPDATE control_target_registrations SET public_generation=9",
		"INSERT OR REPLACE INTO control_target_registrations SELECT * FROM control_target_registrations",
		"DELETE FROM control_target_registration_identity", "UPDATE control_target_registration_identity SET signer_public_key='other'",
		"INSERT OR REPLACE INTO control_target_registration_identity SELECT * FROM control_target_registration_identity",
	} {
		if _, err := control.db.Exec(statement); err == nil {
			t.Fatalf("retained registration custody was mutable: %s", statement)
		}
	}
	if err := control.Rollback(0); !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatalf("rollback discarded registration custody: %v", err)
	}
}

func TestTargetRegistrationDatabaseFailuresCannotLeavePartialPublication(t *testing.T) {
	for _, fault := range []inventoryReadFault{
		{query: "INSERT INTO control_events", exec: true},
		{query: "INSERT INTO control_operator_mutations", exec: true},
		{query: "INSERT INTO control_target_registration_identity", exec: true},
		{query: "INSERT INTO control_target_registrations", exec: true},
		{commit: true},
	} {
		t.Run(fault.query, func(t *testing.T) {
			control := targetDeletePromotedStore(t, true)
			scope, receipt, document := registrationFixture(t, control, "target-inspect-atomic", "target_original", filepath.Join(t.TempDir(), "target"))
			installInventoryReadFault(t, control, &fault)
			fault.armed = true
			if _, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document); err == nil || !fault.injected {
				t.Fatalf("the actual database interruption did not refuse settlement: %v injected=%v", err, fault.injected)
			}
			if count, err := control.OperatorJournalSize(); err != nil || count != 0 {
				t.Fatalf("failed settlement published an operator row: %d %v", count, err)
			}
			for _, table := range []string{"targets", "control_target_registrations", "control_target_registration_identity"} {
				var count int
				if err := control.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
					t.Fatalf("failed settlement changed %s: %d %v", table, count, err)
				}
			}
			if record, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document); err != nil || record.Revision != 1 {
				t.Fatalf("the same operation could not reconcile after a proven rollback: %+v %v", record, err)
			}
		})
	}
}

func TestTargetRegistrationLostWitnessAndSignerPinNeverLookHealthy(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, receipt, document := registrationFixture(t, control, "target-inspect-retained", "target_original", filepath.Join(t.TempDir(), "target"))
	if _, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document); err != nil {
		t.Fatal(err)
	}
	public := control.promotionSignerKey
	control.promotionSignerKey = "a different configured public pin"
	if _, _, err := control.Get("target", "target_original"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("a changed configured signer was admitted: %v", err)
	}
	control.promotionSignerKey = public
	// Model storage damage after the immutable guard itself is restored. A
	// reader must detect the lost witness, not merely a missing SQL trigger.
	if _, err := control.db.Exec("DROP TRIGGER control_target_registrations_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DELETE FROM control_target_registrations"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(targetRegistrationSchemaObjects["control_target_registrations_no_delete"]); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.Get("target", "target_original"); err == nil {
		t.Fatal("a lost original observation appeared as a healthy target")
	}
	if _, _, err := control.ListAuthoritativeTargets(); err == nil {
		t.Fatal("a lost witness appeared as a valid public inventory")
	}
}

func TestTargetRegistrationV20UpgradePreservesOldRecordsWithoutInventingWitnesses(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	before := Record{Domain: "target", ID: "target_legacy", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{"targetId":"target_legacy","topologyGeneration":1}`)}
	if _, err := control.ApplyOperatorMutation(OperatorMutation{OperationID: "legacy-target-operation", ActionID: "legacy-action", Actor: "operator:test", Record: before}); err != nil {
		t.Fatal(err)
	}
	before, _, _ = control.Get("target", before.ID)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	dropEmptyTargetRegistrationFixture(t, tx)
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v20 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 20),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v20 SELECT singleton,runtime,mode,20,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v20 RENAME TO control_store_meta`,
		`DELETE FROM schema_migrations WHERE version=21`, `PRAGMA user_version=20`,
	} {
		if _, err := tx.Exec(statement); err != nil {
			_ = tx.Rollback()
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV20); err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV20
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	recovered := reopenTargetDeleteStore(t, path, func() int64 { return 1000 })
	after, exists, err := recovered.Get("target", before.ID)
	if err != nil || !exists || !reflect.DeepEqual(before, after) || recovered.SchemaVersion() != CurrentSchema {
		t.Fatalf("v20 upgrade changed retained state: %+v %+v %v", before, after, err)
	}
	var witnesses int
	if err := recovered.db.QueryRow("SELECT COUNT(*) FROM control_target_registrations").Scan(&witnesses); err != nil || witnesses != 0 {
		t.Fatal("migration invented native read evidence")
	}
}
