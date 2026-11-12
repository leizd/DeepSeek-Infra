package store

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"path/filepath"
	"reflect"
	"testing"
)

// This reconstructs the actual old SQL layout in an isolated test database.
// Existing Go-authorized rows/events remain intact; it never seeds promotion.
func registrationV20Fixture(t *testing.T, control *Control) Record {
	t.Helper()
	row := Record{Domain: "target", ID: "target_legacy", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{"targetId":"target_legacy","topologyGeneration":1}`)}
	if _, err := control.ApplyOperatorMutation(OperatorMutation{OperationID: "legacy-before-registration", ActionID: "legacy-action", Actor: "operator:test", Record: row}); err != nil {
		t.Fatal(err)
	}
	row, _, err := control.Get("target", row.ID)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	dropEmptyTargetRegistrationFixture(t, tx)
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v20 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 20),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v20 SELECT singleton,runtime,mode,20,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v20 RENAME TO control_store_meta`,
		`DELETE FROM schema_migrations WHERE version=21`, `PRAGMA user_version=20`,
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV20); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV20
	return row
}

func TestTargetRegistrationMigrationInterruptionsRetainOriginalV20State(t *testing.T) {
	for _, phase := range []string{
		"CREATE TABLE control_store_meta_v21",
		"INSERT INTO control_target_registration_boundary",
		"INSERT INTO schema_migrations VALUES(21",
		"UPDATE control_store_meta SET schema_version=21",
		"PRAGMA user_version=21",
		"negative-clock",
	} {
		t.Run(phase, func(t *testing.T) {
			control := targetDeletePromotedStore(t, true)
			before := registrationV20Fixture(t, control)
			fault := inventoryReadFault{query: phase, exec: true}
			installInventoryReadFault(t, control, &fault)
			clock := control.now
			if phase == "negative-clock" {
				control.now = func() int64 { return -1 }
			} else {
				fault.armed = true
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			migrationErr := control.migrateToV21Tx(tx)
			if err := tx.Rollback(); err != nil {
				t.Fatal(err)
			}
			control.now = clock
			// An unsuccessful constructor exposes no Control. Restore the test
			// harness's cached version to the persistent rollback before retry.
			control.schema = SchemaV20
			if migrationErr == nil || (phase != "negative-clock" && !fault.injected) {
				t.Fatalf("actual migration interruption was hidden: %v injected=%v", migrationErr, fault.injected)
			}
			tx, err = control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			if err := verifySchemaTx(tx, SchemaV20); err != nil {
				_ = tx.Rollback()
				t.Fatal(err)
			}
			var payload []byte
			if err := tx.QueryRow("SELECT payload_json FROM targets WHERE id=?", before.ID).Scan(&payload); err != nil || !bytes.Equal(payload, before.Payload) {
				_ = tx.Rollback()
				t.Fatalf("rollback changed the original target payload: %v", err)
			}
			if err := control.migrateToV21Tx(tx); err != nil {
				_ = tx.Rollback()
				t.Fatal(err)
			}
			if err := tx.Commit(); err != nil {
				t.Fatal(err)
			}
			after, exists, err := control.Get("target", before.ID)
			if err != nil || !exists || !reflect.DeepEqual(after, before) || control.SchemaVersion() != SchemaV21 {
				t.Fatalf("the same proven-rolled-back migration lost the original record: %+v %v", after, err)
			}
			var witnesses int
			if err := control.db.QueryRow("SELECT COUNT(*) FROM control_target_registrations").Scan(&witnesses); err != nil || witnesses != 0 {
				t.Fatal("migration invented filesystem witnesses")
			}
		})
	}
}

func withRegistrationRollback(t *testing.T, control *Control, check func(*sql.Tx)) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	check(tx)
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
}

func TestTargetRegistrationDamagedSchemaIdentityAndCatalogNeverLookEmpty(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "target")
	scope, receipt, document := registrationFixture(t, control, "target-inspect-integrity", "target_original", path)
	original, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document)
	if err != nil {
		t.Fatal(err)
	}
	withRegistrationRollback(t, control, func(tx *sql.Tx) {
		if _, err := tx.Exec("DROP TRIGGER control_target_registrations_no_replace"); err != nil {
			t.Fatal(err)
		}
		if !errors.Is(verifyTargetRegistrationSchemaTx(tx), ErrForeignRuntimeStore) {
			t.Fatal("missing immutable guard looked healthy")
		}
	})
	withRegistrationRollback(t, control, func(tx *sql.Tx) {
		if _, err := tx.Exec("DROP TRIGGER control_target_registration_boundary_no_delete"); err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec("DELETE FROM control_target_registration_boundary"); err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec(targetRegistrationSchemaObjects["control_target_registration_boundary_no_delete"]); err != nil {
			t.Fatal(err)
		}
		if !errors.Is(verifyTargetRegistrationSchemaTx(tx), ErrForeignRuntimeStore) {
			t.Fatal("missing legacy boundary looked healthy")
		}
	})
	withRegistrationRollback(t, control, func(tx *sql.Tx) {
		rows, err := tx.Query("SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='targets'")
		if err != nil {
			t.Fatal(err)
		}
		var names []string
		for rows.Next() {
			var name string
			if err := rows.Scan(&name); err != nil {
				t.Fatal(err)
			}
			names = append(names, name)
		}
		if err := rows.Close(); err != nil {
			t.Fatal(err)
		}
		for _, name := range names {
			if _, err := tx.Exec(`DROP TRIGGER "` + name + `"`); err != nil {
				t.Fatal(err)
			}
		}
		if _, err := tx.Exec("DELETE FROM targets WHERE id=?", original.ID); err != nil {
			t.Fatal(err)
		}
		if _, err := targetRegistryDigestTx(tx); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("an orphaned original event looked like an empty target catalog: %v", err)
		}
	})
	withRegistrationRollback(t, control, func(tx *sql.Tx) {
		if _, err := tx.Exec("DROP TRIGGER control_target_registrations_no_update"); err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec("UPDATE control_target_registrations SET public_generation=2 WHERE operation_id=?", scope.operationID); err != nil {
			t.Fatal(err)
		}
		if _, _, err := targetBoundGenerationTx(tx, Record{ID: original.ID, Revision: math.MaxInt64}); !errors.Is(err, ErrCorruptRecord) {
			t.Fatal("overflowed retained generation was accepted")
		}
	})
	// The pin cannot vanish while any signed registration witness survives.
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{"DROP TRIGGER control_target_registration_identity_no_delete", "DELETE FROM control_target_registration_identity", targetRegistrationSchemaObjects["control_target_registration_identity_no_delete"]} {
		if _, err := tx.Exec(statement); err != nil {
			_ = tx.Rollback()
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.Get("target", original.ID); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("missing signer pin looked healthy: %v", err)
	}
	fault := inventoryReadFault{query: "SELECT COUNT(*) FROM control_target_registrations"}
	installInventoryReadFault(t, control, &fault)
	fault.armed = true
	if _, _, err := control.Get("target", original.ID); err == nil || !fault.injected {
		t.Fatal("missing-pin witness read failure became an empty healthy catalog")
	}
	if _, err := control.db.Exec("INSERT INTO control_target_registration_identity VALUES(1,?,?,?)", control.promotionSignerKey, control.fleetID, control.environment); err != nil {
		t.Fatal(err)
	}
	after, exists, err := control.Get("target", original.ID)
	if err != nil || !exists || !reflect.DeepEqual(after, original) {
		t.Fatal("original signed witness did not recover after restoring the exact pin")
	}
	mutation := OperatorMutation{OperationID: "target-inspect-generic-bypass", ActionID: "generic-bypass", Actor: "operator:test", Record: Record{Domain: "target", ID: original.ID, Revision: 2, State: "ACTIVE", Payload: json.RawMessage(`{"targetId":"target_original","topologyGeneration":2}`)}}
	if _, err := control.ApplyOperatorMutation(mutation); !errors.Is(err, ErrTargetRegistrationInvalid) {
		t.Fatalf("generic mutation reused a new inspection operation without its witness: %v", err)
	}
}

func TestTargetRegistrationReplayAndPreparationCannotHideReadOrCommitFailure(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	path := filepath.Join(t.TempDir(), "target")
	scope, receipt, document := registrationFixture(t, control, "target-inspect-replay-faults", "target_original", path)
	original, _, err := control.RegisterInspectedBackupTarget(scope, receipt, document)
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range []inventoryReadFault{
		{query: "FROM control_operator_mutations"},
		{query: "FROM control_events WHERE domain = ? AND record_id = ? AND revision = ?"},
		{commit: true},
	} {
		fault := value
		installInventoryReadFault(t, control, &fault)
		fault.armed = true
		if _, result, err := control.RegisterInspectedBackupTarget(scope, receipt, document); err == nil || !fault.injected || result.Status != "" {
			t.Fatalf("unreadable/uncommitted replay returned success: %+v %v injected=%v", result, err, fault.injected)
		}
		replayed, result, err := control.RegisterInspectedBackupTarget(scope, receipt, document)
		if err != nil || result.Status != OperatorMutationAlreadyApplied || !reflect.DeepEqual(replayed, original) {
			t.Fatal("the same original request could not recover its original result")
		}
	}
	fault := inventoryReadFault{query: "FROM control_operator_mutations"}
	installInventoryReadFault(t, control, &fault)
	fault.armed = true
	if _, _, err := control.Get("target", original.ID); err == nil || !fault.injected {
		t.Fatal("an unreadable original operator receipt looked healthy")
	}
	fault = inventoryReadFault{commit: true}
	installInventoryReadFault(t, control, &fault)
	fault.armed = true
	if _, err := control.PrepareBackupTargetRegistration("target-inspect-prepare-commit", "target-inspect-prepare-commit", "operator:test", path); err == nil || !fault.injected {
		t.Fatal("failed preparation lease commit returned a usable scope")
	}
	after, exists, err := control.Get("target", original.ID)
	if err != nil || !exists || !reflect.DeepEqual(after, original) {
		t.Fatal("failed replay or preparation changed the original target")
	}
	// Retiring an empty extension is also transactional when DDL fails.
	empty := targetDeletePromotedStore(t, true)
	fault = inventoryReadFault{query: "DROP TRIGGER", exec: true}
	installInventoryReadFault(t, empty, &fault)
	fault.armed = true
	withRegistrationRollback(t, empty, func(tx *sql.Tx) {
		if err := retireEmptyTargetRegistrationsTx(tx); err == nil || !fault.injected {
			t.Fatal("failed empty-schema retirement was hidden")
		}
	})
	withRegistrationRollback(t, empty, func(tx *sql.Tx) {
		if err := verifyTargetRegistrationSchemaTx(tx); err != nil {
			t.Fatalf("failed retirement damaged the original immutable schema: %v", err)
		}
	})
}
