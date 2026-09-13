package store

import (
	"database/sql"
	"errors"
	"testing"

	modernsqlite "modernc.org/sqlite"
)

func prepareActionV4Fixture(t *testing.T, control *Control) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()

	// Construct the historical shape only in this isolated fixture.
	for _, table := range []string{"action_verification_boundary", "action_reconciliation_boundary", "action_resource_leases", "action_lease_events", "action_leases"} {
		if _, err := tx.Exec("DROP TABLE IF EXISTS " + table); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := tx.Exec("DELETE FROM schema_migrations WHERE version>=5"); err != nil {
		t.Fatal(err)
	}

	// Set control_store_meta ceiling back to 4
	oldMetadata := `CREATE TABLE control_meta_v4_fixture (
		singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
		runtime TEXT NOT NULL,
		mode TEXT NOT NULL,
		schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 4),
		unique_writer TEXT NOT NULL
	) STRICT`
	for _, statement := range []string{
		oldMetadata,
		"INSERT INTO control_meta_v4_fixture SELECT singleton,runtime,mode,4,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_meta_v4_fixture RENAME TO control_store_meta",
		"PRAGMA user_version=4",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}

	if err := verifySchemaTx(tx, SchemaV4); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV4
}

func TestActionAdmissionV4UpgradePreservesHistoryAndRollsBackOnFailure(t *testing.T) {
	control := openControlAt(t, 1000)
	path, databasePath := control.path, control.DatabasePath()
	claimedAction(t, control)
	before, ok, err := control.Get("action", "a")
	if err != nil || !ok {
		t.Fatal(err)
	}

	prepareActionV4Fixture(t, control)
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}

	// 1. Simulate migration failure due to clock failure during migration
	clockCalls := 0
	if _, err := OpenControl(OpenOptions{Path: path, Owner: "failed-successor", Now: func() int64 {
		clockCalls++
		if clockCalls == 1 {
			return 1000
		}
		return -1
	}}); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("late migration failure expected ErrWriterFenceHeld, got: %v", err)
	}

	// Verify database is still V4 and no partial V5 objects exist
	connector, err := modernsqlite.NewConnector(controlDatabaseDSN(databasePath))
	if err != nil {
		t.Fatal(err)
	}
	db := sql.OpenDB(connector)
	t.Cleanup(func() { _ = db.Close() })

	var version, objects, events int
	var token, lease int64
	if err := db.QueryRow("PRAGMA user_version").Scan(&version); err != nil || version != 4 {
		t.Fatalf("version=%d err=%v", version, err)
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'action_leases%' OR name LIKE 'action_resource_leases%'").Scan(&objects); err != nil || objects != 0 {
		t.Fatalf("partial objects=%d err=%v", objects, err)
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM control_events").Scan(&events); err != nil || events != 2 {
		t.Fatalf("history=%d err=%v", events, err)
	}
	if err := db.QueryRow("SELECT fencing_token,lease_until FROM control_writer").Scan(&token, &lease); err != nil || token != 1 || lease != 1000 {
		t.Fatalf("failed claim token=%d lease=%d err=%v", token, lease, err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	// 2. Successful upgrade to V5
	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "successor", Now: func() int64 { return 1000 }})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()

	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("expected current schema, got %d", reopened.SchemaVersion())
	}

	got, ok, err := reopened.Get("action", "a")
	if err != nil || !ok || !sameControlRecord(got, before) {
		t.Fatalf("preserved record=%+v ok=%v err=%v", got, ok, err)
	}
}

func TestActionAdmissionRollbackProtection(t *testing.T) {
	// 1. action_leases retention
	c1 := openControlAt(t, 1000)
	if _, err := c1.db.Exec(`INSERT INTO action_leases(action_id, owner, epoch, claim_token, lease_until, acquired_at, updated_at, claim_revision, writer_fencing_token)
		VALUES('act-rb-1', 'w', 1, '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', 2000, 1000, 1000, 1, 1)`); err != nil {
		t.Fatal(err)
	}
	if err := c1.Rollback(0); !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatalf("expected ErrAdmissionHistoryRetained for action_leases, got: %v", err)
	}
	c1.Close()

	// 2. action_lease_events retention
	c2 := openControlAt(t, 1000)
	if _, err := c2.db.Exec(`INSERT INTO action_lease_events(action_id, event_type, owner, epoch, claim_token, lease_until, claim_revision, writer_fencing_token, recorded_at)
		VALUES('act-rb-2', 'ADMITTED', 'w', 1, '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', 2000, 1, 1, 1000)`); err != nil {
		t.Fatal(err)
	}
	if err := c2.Rollback(0); !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatalf("expected ErrAdmissionHistoryRetained for action_lease_events, got: %v", err)
	}
	c2.Close()

	// 3. action_resource_leases retention
	c3 := openControlAt(t, 1000)
	if _, err := c3.db.Exec(`INSERT INTO action_resource_leases(resource_key, action_id, owner, epoch, acquired_at, lease_until, writer_fencing_token)
		VALUES('res-rb-3', 'act-rb-3', 'w', 1, 1000, 2000, 1)`); err != nil {
		t.Fatal(err)
	}
	if err := c3.Rollback(0); !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatalf("expected ErrAdmissionHistoryRetained for action_resource_leases, got: %v", err)
	}
	c3.Close()

	// 4. Empty admission tables rollback to 0 successfully
	c4 := openControlAt(t, 1000)
	defer c4.Close()
	if err := c4.Rollback(0); err != nil {
		t.Fatalf("expected clean rollback to 0, got: %v", err)
	}
	if c4.SchemaVersion() != 0 {
		t.Fatalf("expected schema 0, got: %d", c4.SchemaVersion())
	}
}
