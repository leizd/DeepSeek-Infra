package store

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

// Only construct historical layouts in isolated test databases, never runtime
// state. Retained control, dispatch, and claim events stay byte-for-byte intact.
func prepareVerificationV6Fixture(t *testing.T, control *Control) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, statement := range []string{
		"DROP TABLE action_verification_boundary",
		`CREATE TABLE control_meta_v6_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 6),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v6_fixture SELECT singleton,runtime,mode,6,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta", "ALTER TABLE control_meta_v6_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=7", "PRAGMA user_version=6",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV6); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV6
}

func TestActionVerificationV6UpgradeIsAtomicAndPreservesRecovery(t *testing.T) {
	control, claim := executingLeasedAction(t)
	original, _, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken)
	if err != nil {
		t.Fatal(err)
	}
	resources, err := control.GetResourceLeases(claim.Lease.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	prepareVerificationV6Fixture(t, control)
	before, err := control.ExportSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	calls := 0
	failed, err := OpenControl(OpenOptions{Path: control.path, Owner: "failed", Now: func() int64 {
		calls++
		if calls == 1 {
			return 1001
		}
		return -1
	}})
	if !errors.Is(err, ErrWriterFenceHeld) {
		if failed != nil {
			_ = failed.Close()
		}
		t.Fatalf("migration failed to roll back: %v", err)
	}
	if _, err := validateExistingControlMarker(control.DatabasePath()); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: control.path, Owner: "successor", Now: func() int64 { return 1011 }})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.Writer().FencingToken != 2 {
		t.Fatal("failed migration advanced writer token")
	}
	after, err := reopened.ExportSnapshot()
	if err != nil || after.SchemaVersion != CurrentSchema || !reflect.DeepEqual(before.Records, after.Records) {
		t.Fatalf("upgrade rewrote history: %v", err)
	}
	lease, exists, err := reopened.GetActionLease(claim.Lease.ActionID)
	if err != nil || !exists || lease != claim.Lease {
		t.Fatalf("upgrade changed claim: %v", err)
	}
	gotResources, err := reopened.GetResourceLeases(claim.Lease.ActionID)
	if err != nil || !reflect.DeepEqual(resources, gotResources) {
		t.Fatalf("upgrade changed resources: %v", err)
	}
	var previous, boundary int64
	if err := reopened.db.QueryRow(`SELECT (SELECT last_legacy_event_id FROM action_reconciliation_boundary),last_legacy_event_id FROM action_verification_boundary`).Scan(&previous, &boundary); err != nil || previous != 0 || boundary != 3 {
		t.Fatalf("migration changed boundaries: v6=%d v7=%d %v", previous, boundary, err)
	}
	next, err := reopened.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := reopened.MarkActionVerifying(next.Lease.ActionID, next.Lease.Epoch, next.Lease.ClaimToken, nil); err != nil {
		t.Fatal(err)
	}
	got, bound, err := reopened.GetLeasedStorageDispatch(next.Lease.ActionID, next.Lease.Epoch, next.Lease.ClaimToken)
	if err != nil || !bound || got != original {
		t.Fatalf("upgrade lost original dispatch: %v", err)
	}
	if err := reopened.Rollback(0); !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatalf("rollback erased phase history: %v", err)
	}
}

func TestActionVerificationUpgradeDoesNotLegalizePreV7Phases(t *testing.T) {
	control, claim := executingLeasedAction(t)
	if _, err := control.MarkActionVerifying(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil); err != nil {
		t.Fatal(err)
	}
	// Deliberately corrupt pre-v7 history, not a provider-effect fixture.
	prepareVerificationV6Fixture(t, control)
	if _, _, err := control.Get("action", claim.Lease.ActionID); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("v6 accepted phase: %v", err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: control.path, Owner: "reader", Now: func() int64 { return 1001 }})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if _, _, err := reopened.Get("action", claim.Lease.ActionID); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("upgrade legalized corrupt phase: %v", err)
	}
}

func TestActionVerificationBoundaryIsImmutableAndRequired(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	for _, statement := range []string{
		"UPDATE action_verification_boundary SET last_legacy_event_id=99",
		"DELETE FROM action_verification_boundary",
		"INSERT OR REPLACE INTO action_verification_boundary VALUES(1,99)",
	} {
		if _, err := control.db.Exec(statement); err == nil || !strings.Contains(err.Error(), "VERIFICATION_BOUNDARY_IMMUTABLE") {
			t.Fatalf("boundary mutable: %v", err)
		}
	}
	for _, fault := range []string{"missing row", "future boundary", "before v6 boundary", "missing trigger"} {
		t.Run(fault, func(t *testing.T) {
			broken := openControlAt(t, 1000)
			defer broken.Close()
			var statements []string
			switch fault {
			case "missing row":
				statements = []string{"DROP TRIGGER action_verification_boundary_no_delete", "DELETE FROM action_verification_boundary", actionVerificationSchemaObjects["action_verification_boundary_no_delete"]}
			case "future boundary":
				statements = []string{"DROP TRIGGER action_verification_boundary_no_update", "UPDATE action_verification_boundary SET last_legacy_event_id=99", actionVerificationSchemaObjects["action_verification_boundary_no_update"]}
			case "before v6 boundary":
				if err := broken.Put(Record{Domain: "action", ID: "fixture", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
					t.Fatal(err)
				}
				statements = []string{"DROP TRIGGER action_reconciliation_boundary_no_update", "UPDATE action_reconciliation_boundary SET last_legacy_event_id=1", actionReconciliationSchemaObjects["action_reconciliation_boundary_no_update"]}
			case "missing trigger":
				statements = []string{"DROP TRIGGER action_verification_boundary_no_replace"}
			}
			for _, statement := range statements {
				if _, err := broken.db.Exec(statement); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := broken.ExportSnapshot(); !errors.Is(err, ErrForeignRuntimeStore) {
				t.Fatalf("invalid boundary accepted: %v", err)
			}
		})
	}
}
