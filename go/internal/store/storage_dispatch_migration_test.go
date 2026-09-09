package store

import (
	"database/sql"
	"errors"
	"strings"
	"testing"

	modernsqlite "modernc.org/sqlite"
)

// Construct the historical shape only in a new isolated test database. This is
// not a downgrade API and never deletes real execution/replay history.
func prepareDispatchV3Fixture(t *testing.T, control *Control) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	oldMetadata := strings.Replace(bootstrapSchemaStatements[0], "CREATE TABLE IF NOT EXISTS control_store_meta", "CREATE TABLE control_meta_v3_fixture", 1)
	for _, statement := range []string{
		"DROP TABLE IF EXISTS action_resource_leases",
		"DROP TABLE IF EXISTS action_lease_events",
		"DROP TABLE IF EXISTS action_leases",
		"DROP TABLE IF EXISTS storage_dispatches",
		oldMetadata,
		"INSERT INTO control_meta_v3_fixture SELECT singleton,runtime,mode,3,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_meta_v3_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version >= 4",
		"PRAGMA user_version=3",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV3); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV3
}

func TestStorageDispatchV3UpgradePreservesHistoryAndFailedClaim(t *testing.T) {
	control := openControlAt(t, 1000)
	path, databasePath := control.path, control.DatabasePath()
	claimedAction(t, control)
	before, ok, err := control.Get("action", "a")
	if err != nil || !ok {
		t.Fatal(err)
	}
	prepareDispatchV3Fixture(t, control)
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	clockCalls := 0
	if _, err := OpenControl(OpenOptions{Path: path, Owner: "failed-successor", Now: func() int64 {
		clockCalls++
		if clockCalls == 1 {
			return 1000
		}
		return -1
	}}); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("late migration failure: %v", err)
	}
	connector, err := modernsqlite.NewConnector(controlDatabaseDSN(databasePath))
	if err != nil {
		t.Fatal(err)
	}
	db := sql.OpenDB(connector)
	t.Cleanup(func() { _ = db.Close() })
	var version, objects, events int
	var token, lease int64
	if err := db.QueryRow("PRAGMA user_version").Scan(&version); err != nil || version != 3 {
		t.Fatalf("version=%d err=%v", version, err)
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'storage_dispatches%' OR name='control_store_meta_v4' OR name LIKE 'action_leases%' OR name LIKE 'action_resource_leases%'").Scan(&objects); err != nil || objects != 0 {
		t.Fatalf("partial objects=%d err=%v", objects, err)
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM control_events").Scan(&events); err != nil || events != 2 {
		t.Fatalf("history=%d err=%v", events, err)
	}
	if err := db.QueryRow("SELECT fencing_token,lease_until FROM control_writer").Scan(&token, &lease); err != nil || token != 1 || lease != 1000 {
		t.Fatalf("failed claim token=%d lease=%d err=%v", token, lease, err)
	}
	if _, err := db.Exec("UPDATE control_store_meta SET schema_version=4"); err == nil {
		t.Fatal("old metadata ceiling must survive failed upgrade")
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "successor", Now: func() int64 { return 1000 }})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	got, ok, err := reopened.Get("action", "a")
	if err != nil || !ok || !sameControlRecord(got, before) {
		t.Fatalf("preserved record=%+v ok=%v err=%v", got, ok, err)
	}
	if reopened.SchemaVersion() != CurrentSchema || reopened.Writer().FencingToken != 2 {
		t.Fatal("migration/claim did not advance exactly once")
	}
	if _, exists, err := reopened.GetStorageDispatch("a", 1); err != nil || exists {
		t.Fatalf("legacy intent must not be inferred: %v %v", exists, err)
	}
}
