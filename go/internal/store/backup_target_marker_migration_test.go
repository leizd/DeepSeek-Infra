package store

import (
	"database/sql"
	"errors"
	"path/filepath"
	"reflect"
	"testing"
)

func TestTargetRegistrationMarkerV22UpgradePreservesRegistrationAfterRollback(t *testing.T) {
	control := targetDeletePromotedStore(t, true)
	scope, observation, document, body := publicRegistrationFixture(t, control, "v22-retained", "target_original", filepath.Join(t.TempDir(), "retained"))
	original, _, err := control.RegisterPublicInspectedBackupTarget(scope, observation, nil, nil, body, document)
	if err != nil {
		t.Fatal(err)
	}
	for _, fault := range []inventoryReadFault{
		{query: "SELECT sql FROM sqlite_schema WHERE name=? AND type=?", armed: true},
		{query: "SELECT COUNT(*) FROM control_target_marker_operations", armed: true},
		{query: "DROP table control_target_marker_operations", exec: true, armed: true},
	} {
		installInventoryReadFault(t, control, &fault)
		withRegistrationRollback(t, control, func(tx *sql.Tx) {
			retireErr := retireEmptyTargetMarkersTx(tx)
			if !fault.injected || (!errors.Is(retireErr, errInventoryReadInterrupted) && !errors.Is(retireErr, ErrForeignRuntimeStore)) {
				t.Fatal("marker history retirement hid a storage failure", fault.query, retireErr)
			}
		})
		withRegistrationRollback(t, control, func(tx *sql.Tx) {
			if err := verifyTargetMarkerSchemaTx(tx); err != nil {
				t.Fatal("interrupted retirement left an incomplete marker schema", fault.query, err)
			}
		})
	}
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	dropEmptyTargetMarkerFixture(t, tx)
	for _, statement := range []string{
		"CREATE TABLE control_store_meta_v22 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 22),unique_writer TEXT NOT NULL) STRICT",
		"INSERT INTO control_store_meta_v22 SELECT singleton,runtime,mode,22,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta", "ALTER TABLE control_store_meta_v22 RENAME TO control_store_meta", "PRAGMA user_version=22",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV22); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV22
	if pending, found, err := control.PendingTargetMarker(scope.operationID, scope.actor, body); err != nil || found || pending.Claim != nil {
		t.Fatal("legacy store reported marker operation history", found, err)
	}
	if op, err := control.AdmitTargetMarker(scope, observation, nil, body); !errors.Is(err, ErrTargetMarkerClaim) || op.Claim != nil {
		t.Fatal("legacy store admitted a new marker", err)
	}
	currentStore := targetDeletePromotedStore(t, true)
	newScope, absent, newBody := absentMarkerFixture(t, currentStore, "issued-by-current-schema", filepath.Join(t.TempDir(), "candidate"))
	currentClaim, err := currentStore.AdmitTargetMarker(newScope, absent, nil, newBody)
	if err != nil {
		t.Fatal("current schema fixture did not admit a claim", err)
	}
	prepared, _ := targetMarkerEffectFixture(t, currentClaim, 0)
	if permit, err := control.BeginTargetMarkerDispatch(prepared); !errors.Is(err, ErrTargetMarkerClaim) || permit != nil {
		t.Fatal("legacy store authorized a current-protocol preparation", err)
	}

	badClock := control.now
	control.now = func() int64 { return -1 }
	clockTx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	clockErr := control.migrateToV23Tx(clockTx)
	rollbackErr := clockTx.Rollback()
	control.now = badClock
	if !errors.Is(clockErr, ErrWriterFenceHeld) || rollbackErr != nil {
		t.Fatal("migration used a bad clock or failed to roll back", clockErr, rollbackErr)
	}

	for _, fault := range []inventoryReadFault{
		{query: "CREATE TRIGGER control_target_marker_advance", exec: true, armed: true},
		{query: "INSERT INTO schema_migrations VALUES(23", exec: true, armed: true},
		{query: "UPDATE control_store_meta SET schema_version=23", exec: true, armed: true},
		{query: "PRAGMA user_version=23", exec: true, armed: true},
		{query: "SELECT sql FROM sqlite_schema WHERE name=? AND type=?", armAfterExec: "PRAGMA user_version=23"},
	} {
		installInventoryReadFault(t, control, &fault)
		attempt, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		migrationErr := control.migrateToV23Tx(attempt)
		if err := attempt.Rollback(); err != nil {
			t.Fatal(err)
		}
		control.schema = SchemaV22
		if !fault.injected || (!errors.Is(migrationErr, errInventoryReadInterrupted) && !errors.Is(migrationErr, ErrForeignRuntimeStore)) {
			t.Fatal("v23 migration interruption was hidden", fault.query, migrationErr)
		}
		check, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		schemaErr := verifySchemaTx(check, SchemaV22)
		if err := check.Rollback(); err != nil {
			t.Fatal(err)
		}
		if schemaErr != nil {
			t.Fatal("rollback did not restore original schema", schemaErr)
		}
	}
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	upgraded := reopenTargetDeleteStore(t, path, func() int64 { return 1000 })
	if upgraded.SchemaVersion() != CurrentSchema {
		t.Fatal("reopening did not restore new schema")
	}
	current, _, found, err := upgraded.ReplayPublicBackupTargetRegistration(scope.operationID, scope.actor, body)
	if err != nil || !found || !reflect.DeepEqual(current, original) {
		t.Fatal("original registration bytes did not survive schema upgrade", found, err)
	}
	var count int
	if err := upgraded.db.QueryRow("SELECT COUNT(*) FROM control_target_marker_operations").Scan(&count); err != nil || count != 0 {
		t.Fatal("upgrade invented marker effect history", count, err)
	}
}
