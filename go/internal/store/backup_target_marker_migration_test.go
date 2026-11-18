package store

import (
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

	fault := inventoryReadFault{query: "CREATE TRIGGER control_target_marker_advance", exec: true, armed: true}
	installInventoryReadFault(t, control, &fault)
	attempt, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer attempt.Rollback()
	if err := control.migrateToV23Tx(attempt); !errors.Is(err, errInventoryReadInterrupted) || !fault.injected {
		t.Fatal("v23 migration interruption was hidden", err)
	}
	if err := attempt.Rollback(); err != nil {
		t.Fatal(err)
	}
	check, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer check.Rollback()
	if err := verifySchemaTx(check, SchemaV22); err != nil {
		t.Fatal("rollback did not restore original schema", err)
	}
	if err := check.Rollback(); err != nil {
		t.Fatal(err)
	}
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	upgraded := reopenTargetDeleteStore(t, path, func() int64 { return 1000 })
	if upgraded.SchemaVersion() != SchemaV23 {
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
