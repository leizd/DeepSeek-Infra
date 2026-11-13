package store

import (
	"errors"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

func committedRelocationFixture(t *testing.T) (*Control, Record, BackupTargetRegistrationScope, *actionv1.FilesystemBackupTargetLocationReceipt) {
	t.Helper()
	control, first, previous := registeredLocationFixture(t)
	scope, marker, document := registrationFixture(t, control, "target-inspect-location-committed", first.ID, filepath.Join(t.TempDir(), "moved"))
	location := relocationFixture(t, control, previous, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING)
	current, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, location, document)
	if err != nil {
		t.Fatal(err)
	}
	return control, current, scope, location
}

func TestTargetRegistrationLocationWitnessAndUpgradeBoundaryAreImmutable(t *testing.T) {
	control, current, scope, _ := committedRelocationFixture(t)
	for _, statement := range []string{
		"UPDATE control_target_registration_locations SET previous_path=previous_path||'-other'",
		"DELETE FROM control_target_registration_locations",
		"INSERT OR REPLACE INTO control_target_registration_locations SELECT operation_id,previous_path,observation FROM control_target_registration_locations",
		"INSERT OR REPLACE INTO control_target_registration_locations(rowid,operation_id,previous_path,observation) SELECT rowid,operation_id||'-other',previous_path,observation FROM control_target_registration_locations",
		"UPDATE control_target_location_boundary SET last_legacy_event_id=last_legacy_event_id+1",
		"DELETE FROM control_target_location_boundary",
		"INSERT OR REPLACE INTO control_target_location_boundary VALUES(1,0)",
		"INSERT OR REPLACE INTO control_target_location_boundary(rowid,singleton,last_legacy_event_id) VALUES(99,1,0)",
	} {
		if _, err := control.db.Exec(statement); err == nil || !strings.Contains(err.Error(), "TARGET_LOCATION_HISTORY_IMMUTABLE") {
			t.Fatal("location history guard did not refuse the actual SQLite mutation", statement, err)
		}
	}
	var count int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_target_registration_locations WHERE operation_id=?", scope.operationID).Scan(&count); err != nil || count != 1 {
		t.Fatal("immutable witness was lost", err)
	}
	after, found, err := control.Get("target", current.ID)
	if err != nil || !found || !reflect.DeepEqual(after, current) {
		t.Fatal("refused mutation changed current target", err)
	}
}

func TestTargetRegistrationLocationMissingOrAlteredEvidenceCannotLookHealthy(t *testing.T) {
	control, current, scope, location := committedRelocationFixture(t)
	wrong := proto.Clone(location).(*actionv1.FilesystemBackupTargetLocationReceipt)
	wrong.Observation.RequestedPath += "-other"
	signTargetLocationFixture(t, wrong, targetLocationSignatureDomain)
	wire, _ := proto.MarshalOptions{Deterministic: true}.Marshal(wrong)
	for _, name := range []string{"missing-proof", "wrong-path-column", "malformed-wire", "wrong-signed-path", "missing-boundary", "missing-event", "missing-guard"} {
		t.Run(name, func(t *testing.T) {
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			var statement string
			var args []any
			switch name {
			case "missing-proof":
				_, err = tx.Exec("DROP TRIGGER control_target_registration_locations_no_delete")
				statement = "DELETE FROM control_target_registration_locations WHERE operation_id=?"
				args = []any{scope.operationID}
			case "wrong-path-column":
				_, err = tx.Exec("DROP TRIGGER control_target_registration_locations_no_update")
				statement = "UPDATE control_target_registration_locations SET previous_path=previous_path||'-other' WHERE operation_id=?"
				args = []any{scope.operationID}
			case "malformed-wire":
				_, err = tx.Exec("DROP TRIGGER control_target_registration_locations_no_update")
				statement = "UPDATE control_target_registration_locations SET observation=? WHERE operation_id=?"
				args = []any{[]byte{255}, scope.operationID}
			case "wrong-signed-path":
				_, err = tx.Exec("DROP TRIGGER control_target_registration_locations_no_update")
				statement = "UPDATE control_target_registration_locations SET observation=? WHERE operation_id=?"
				args = []any{wire, scope.operationID}
			case "missing-boundary":
				_, err = tx.Exec("DROP TRIGGER control_target_location_boundary_no_delete")
				statement = "DELETE FROM control_target_location_boundary"
			case "missing-event":
				_, err = tx.Exec("DROP TRIGGER control_events_no_delete")
				statement = "DELETE FROM control_events WHERE domain='target' AND record_id=? AND revision=?"
				args = []any{current.ID, current.Revision}
			case "missing-guard":
				statement = "DROP TRIGGER control_target_registration_locations_no_update"
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, err = tx.Exec(statement, args...); err != nil {
				t.Fatal(err)
			}
			if name == "missing-guard" {
				err = verifyTargetLocationSchemaTx(tx)
			} else {
				_, _, err = readTargetMetadataTx(tx, current.ID)
			}
			if err == nil {
				t.Fatal("damaged location evidence was reported healthy")
			}
			if err = tx.Rollback(); err != nil {
				t.Fatal(err)
			}
			after, found, err := control.Get("target", current.ID)
			if err != nil || !found || !reflect.DeepEqual(after, current) {
				t.Fatal("proven rollback failed to restore original record", err)
			}
		})
	}
}

func TestTargetRegistrationLocationCannotPublishPartialWitnessOnDatabaseFailure(t *testing.T) {
	for _, commit := range []bool{false, true} {
		control, first, previous := registeredLocationFixture(t)
		scope, marker, document := registrationFixture(t, control, "target-inspect-location-database", first.ID, filepath.Join(t.TempDir(), "moved"))
		location := relocationFixture(t, control, previous, scope, actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING)
		fault := inventoryReadFault{query: "INSERT INTO control_target_registration_locations", exec: !commit, commit: commit, armed: true}
		installInventoryReadFault(t, control, &fault)
		if _, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, location, document); err == nil || !fault.injected {
			t.Fatal("actual late insert/commit failure was hidden", err)
		}
		current, found, err := control.Get("target", first.ID)
		if err != nil || !found || !reflect.DeepEqual(first, current) {
			t.Fatal("late failure published partial metadata", err)
		}
		for _, table := range []string{"control_target_registrations", "control_target_registration_locations", "control_operator_mutations"} {
			var count int
			if err := control.db.QueryRow("SELECT COUNT(*) FROM "+table+" WHERE operation_id=?", scope.operationID).Scan(&count); err != nil || count != 0 {
				t.Fatal("late failure retained a partial command", table, err)
			}
		}
		if _, _, err := control.RegisterInspectedBackupTargetWithLocation(scope, marker, location, document); err != nil {
			t.Fatal("same command failed after proven database rollback", err)
		}
	}
}

func TestTargetRegistrationLocationSchemaReadErrorsAndOrphansFailClosed(t *testing.T) {
	control, _, _, _ := committedRelocationFixture(t)
	for _, query := range []string{"SELECT sql FROM sqlite_schema WHERE name=?", "SELECT COUNT(*) FROM control_target_location_boundary", "SELECT COUNT(*) FROM control_target_registration_locations l LEFT JOIN"} {
		fault := inventoryReadFault{query: query, armed: true}
		installInventoryReadFault(t, control, &fault)
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		err = verifyTargetLocationSchemaTx(tx)
		_ = tx.Rollback()
		if err == nil || !fault.injected {
			t.Fatal("location schema read failed open", query, err)
		}
	}
	emptyBoundary, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{"DROP TRIGGER control_target_location_boundary_no_delete", "DELETE FROM control_target_location_boundary", targetLocationSchemaObjects["control_target_location_boundary_no_delete"]} {
		if _, err := emptyBoundary.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifyTargetLocationSchemaTx(emptyBoundary); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatal("empty boundary with canonical guards looked healthy", err)
	}
	if err := emptyBoundary.Rollback(); err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("PRAGMA defer_foreign_keys=ON"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("INSERT INTO control_target_registration_locations VALUES(?,?,?)", "target-inspect-orphan", filepath.Join(t.TempDir(), "missing"), []byte{1}); err != nil {
		t.Fatal(err)
	}
	if err := verifyTargetLocationSchemaTx(tx); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("orphan location evidence looked healthy", err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
}

func TestTargetRegistrationLocationBindingReadFailuresNeverBecomeAbsentEvidence(t *testing.T) {
	for _, mode := range []string{"query", "cursor", "malformed"} {
		control, current, _, _ := committedRelocationFixture(t)
		fault := inventoryReadFault{query: "FROM control_target_registrations r JOIN control_events e", cursor: mode == "cursor", malformed: mode == "malformed", armed: true}
		installInventoryReadFault(t, control, &fault)
		if _, _, err := control.Get("target", current.ID); err == nil || !fault.injected {
			t.Fatal("binding transport failure looked like absent evidence", mode, err)
		}
		after, found, err := control.Get("target", current.ID)
		if err != nil || !found || !reflect.DeepEqual(after, current) {
			t.Fatal("binding read did not recover original state", err)
		}
	}
	control, current, _, _ := committedRelocationFixture(t)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	bindings, err := loadTargetRegistrationBindingsTx(tx, current.ID)
	if err != nil {
		t.Fatal(err)
	}
	bindings[99] = targetRegistrationBinding{}
	if err := attachTargetLocationBindingsTx(tx, current.ID, bindings); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("unaccounted registration binding was ignored", err)
	}
	delete(bindings, 99)
	bad := bindings[current.Revision]
	bad.operation += "-other"
	bindings[current.Revision] = bad
	if err := attachTargetLocationBindingsTx(tx, current.ID, bindings); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("different operation was attached to a revision", err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	for _, check := range []func() error{func() error { return verifyTargetLocationSchemaTx(tx) }, func() error { return attachTargetLocationBindingsTx(tx, current.ID, nil) }, func() error { return control.migrateToV22Tx(tx) }, func() error { return retireEmptyTargetLocationsTx(tx) }} {
		if err := check(); err == nil {
			t.Fatal("closed transaction looked like a healthy location store")
		}
	}
}

func TestTargetRegistrationLocationRejectsUnneededOrMalformedWitnesses(t *testing.T) {
	control, current, _, _ := committedRelocationFixture(t)
	for _, malformed := range []bool{false, true} {
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		var raw []byte
		if err := tx.QueryRow("SELECT observation FROM control_target_registrations WHERE target_id=? AND revision=1", current.ID).Scan(&raw); err != nil {
			t.Fatal(err)
		}
		marker := &actionv1.FilesystemBackupTargetInspectionReceipt{}
		if err := proto.Unmarshal(raw, marker); err != nil {
			t.Fatal(err)
		}
		request := &actionv1.FilesystemBackupTargetLocationReceipt{SchemaVersion: 1, SignerPublicKey: promotionTestPublic, Observation: &actionv1.FilesystemBackupTargetLocationObservation{SchemaVersion: 1, Fence: marker.Observation.Fence, OperationId: marker.Observation.OperationId, WriterFencingToken: marker.Observation.WriterFencingToken, RequestedPath: marker.Observation.CanonicalPath, State: actionv1.BackupTargetLocationState_BACKUP_TARGET_LOCATION_STATE_MISSING, ObservedAtUnix: control.now(), FleetId: "fleet-a", Environment: "production"}}
		signTargetLocationFixture(t, request, targetLocationSignatureDomain)
		encoded, _ := proto.MarshalOptions{Deterministic: true}.Marshal(request)
		if malformed {
			encoded = []byte{255}
		}
		if _, err := tx.Exec("INSERT INTO control_target_registration_locations VALUES(?,?,?)", marker.Observation.OperationId, marker.Observation.CanonicalPath, encoded); err != nil {
			t.Fatal(err)
		}
		if _, _, err := readTargetMetadataTx(tx, current.ID); err == nil {
			t.Fatal("unneeded location evidence was accepted")
		}
		if err := tx.Rollback(); err != nil {
			t.Fatal(err)
		}
	}
	var count int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_target_registration_locations").Scan(&count); err != nil || count != 1 {
		t.Fatal("refused evidence changed durable witness count", err)
	}
}
