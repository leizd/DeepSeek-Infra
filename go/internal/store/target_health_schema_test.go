package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"testing"
)

// Historical fixtures must remove every later schema object before claiming
// an older version. Otherwise the store correctly refuses a forged identity.
func dropTargetHealthFixtureTables(t *testing.T, tx *sql.Tx) {
	t.Helper()
	for _, table := range []string{"control_operator_mutations", "backup_target_health", "control_target_health_imports"} {
		if _, err := tx.Exec("DROP TABLE IF EXISTS " + table); err != nil {
			_ = tx.Rollback()
			t.Fatal(err)
		}
	}
}

func materializeV13HealthMigrationSource(t *testing.T, control *Control) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	dropTargetHealthFixtureTables(t, tx)
	for _, statement := range []string{
		`CREATE TABLE meta_v13_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,
			mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 13),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO meta_v13_fixture SELECT singleton,runtime,mode,13,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta", "ALTER TABLE meta_v13_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=14", "PRAGMA user_version=13",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV13
}

func TestV14MigrationPreservesOlderRecordsWithoutInventingHealth(t *testing.T) {
	control := openShadow(t)
	if err := control.Put(Record{Domain: "policy", ID: "old-policy", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{"policyId":"old-policy","policyRevision":1}`)}); err != nil {
		t.Fatal(err)
	}
	materializeV13HealthMigrationSource(t, control)
	path := control.path
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "v14-successor"})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("v13 was not migrated: %d", reopened.SchemaVersion())
	}
	if record, exists, err := reopened.Get("policy", "old-policy"); err != nil || !exists || record.Revision != 1 {
		t.Fatalf("v14 migration lost old data: %+v %v %v", record, exists, err)
	}
	var rows int
	if err := reopened.db.QueryRow("SELECT (SELECT COUNT(*) FROM backup_target_health)+(SELECT COUNT(*) FROM control_target_health_imports)").Scan(&rows); err != nil || rows != 0 {
		t.Fatalf("v14 invented health evidence: %d %v", rows, err)
	}
}

func TestV14TargetHealthSchemaAndSnapshotsCannotBeSilentlyChanged(t *testing.T) {
	control, _, _, _ := importedHealthStore(t, false)
	defer control.Close()
	for _, statement := range []string{
		"UPDATE backup_target_health SET status='healthy'",
		"DELETE FROM backup_target_health",
		"INSERT OR REPLACE INTO backup_target_health SELECT * FROM backup_target_health",
		"UPDATE control_target_health_imports SET row_count=0",
		"DELETE FROM control_target_health_imports",
		"INSERT OR REPLACE INTO control_target_health_imports SELECT * FROM control_target_health_imports",
	} {
		if _, err := control.db.Exec(statement); err == nil {
			t.Fatalf("immutable health snapshot changed: %s", statement)
		}
	}
	if _, err := control.db.Exec("DROP TRIGGER backup_target_health_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.GetCutover("target"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing health guard accepted as a valid schema: %v", err)
	}
}
