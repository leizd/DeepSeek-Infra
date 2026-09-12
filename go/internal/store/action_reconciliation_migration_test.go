package store

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

// Historical layout construction is confined to t.TempDir databases. It is not
// a downgrade API: real retained lease/dispatch history may never be discarded.
func prepareReconciliationV5Fixture(t *testing.T, control *Control) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	metadata := `CREATE TABLE control_meta_v5_fixture (
		singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
		mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 5),
		unique_writer TEXT NOT NULL) STRICT`
	for _, statement := range []string{
		"DROP TABLE action_reconciliation_boundary", metadata,
		"INSERT INTO control_meta_v5_fixture SELECT singleton,runtime,mode,5,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta", "ALTER TABLE control_meta_v5_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=6", "PRAGMA user_version=5",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV5); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV5
}

func TestActionReconciliationV5UpgradePreservesHistory(t *testing.T) {
	control, claim := executingLeasedAction(t)
	original, _, err := control.GetStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch)
	if err != nil {
		t.Fatal(err)
	}
	resources, err := control.GetResourceLeases(claim.Lease.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	prepareReconciliationV5Fixture(t, control)
	before, err := control.ExportSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	// Failure occurs after DDL and the boundary INSERT, before migration commit.
	calls := 0
	if failed, err := OpenControl(OpenOptions{Path: control.path, Owner: "failed", Now: func() int64 {
		calls++
		if calls == 1 {
			return 1001
		}
		return -1
	}}); !errors.Is(err, ErrWriterFenceHeld) {
		if failed != nil {
			_ = failed.Close()
		}
		t.Fatalf("migration did not fail atomically: %v", err)
	}
	// The read-only identity check also rejects unexpected partial v6 objects.
	if _, err := validateExistingControlMarker(control.DatabasePath()); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: control.path, Owner: "successor", Now: func() int64 { return 1011 }})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.Writer().FencingToken != 2 {
		t.Fatal("failed migration advanced persisted writer token")
	}
	after, err := reopened.ExportSnapshot()
	if err != nil || after.SchemaVersion != SchemaV6 || !reflect.DeepEqual(before.Records, after.Records) {
		t.Fatalf("history rewritten by upgrade: %v", err)
	}
	lease, exists, err := reopened.GetActionLease(claim.Lease.ActionID)
	if err != nil || !exists || lease != claim.Lease {
		t.Fatalf("upgrade changed claim: %v", err)
	}
	gotResources, err := reopened.GetResourceLeases(claim.Lease.ActionID)
	if err != nil || !reflect.DeepEqual(resources, gotResources) {
		t.Fatalf("upgrade changed reservations: %v", err)
	}
	var boundary int64
	if err := reopened.db.QueryRow("SELECT last_legacy_event_id FROM action_reconciliation_boundary").Scan(&boundary); err != nil || boundary != 3 {
		t.Fatalf("wrong migration boundary: %d %v", boundary, err)
	}
	next, err := reopened.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID})
	if err != nil || next.Record.State != "RECONCILING" {
		t.Fatalf("upgraded claim cannot recover: %v", err)
	}
	got, bound, err := reopened.GetLeasedStorageDispatch(next.Lease.ActionID, next.Lease.Epoch, next.Lease.ClaimToken)
	if err != nil || !bound || got != original {
		t.Fatalf("upgrade lost original operation: %v", err)
	}
	if err := reopened.Rollback(0); !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatalf("rollback destroyed recovery history: %v", err)
	}
}

func TestActionReconciliationBoundaryIsImmutableAndRequired(t *testing.T) {
	for _, statement := range []string{
		"UPDATE action_reconciliation_boundary SET last_legacy_event_id=99",
		"DELETE FROM action_reconciliation_boundary",
		"INSERT OR REPLACE INTO action_reconciliation_boundary VALUES(1,99)",
	} {
		t.Run(statement, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()
			if _, err := control.db.Exec(statement); err == nil || !strings.Contains(err.Error(), "RECONCILIATION_BOUNDARY_IMMUTABLE") {
				t.Fatalf("boundary mutable: %v", err)
			}
		})
	}
	for _, fault := range []string{"missing row", "future boundary", "missing trigger"} {
		t.Run(fault, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()
			var statements []string
			switch fault {
			case "missing row":
				statements = []string{"DROP TRIGGER action_reconciliation_boundary_no_delete", "DELETE FROM action_reconciliation_boundary", actionReconciliationSchemaObjects["action_reconciliation_boundary_no_delete"]}
			case "future boundary":
				statements = []string{"DROP TRIGGER action_reconciliation_boundary_no_update", "UPDATE action_reconciliation_boundary SET last_legacy_event_id=99", actionReconciliationSchemaObjects["action_reconciliation_boundary_no_update"]}
			case "missing trigger":
				statements = []string{"DROP TRIGGER action_reconciliation_boundary_no_replace"}
			}
			for _, statement := range statements {
				if _, err := control.db.Exec(statement); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := control.ExportSnapshot(); !errors.Is(err, ErrForeignRuntimeStore) {
				t.Fatalf("invalid version boundary accepted: %v", err)
			}
		})
	}
}
