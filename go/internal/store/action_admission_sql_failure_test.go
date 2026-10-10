package store

import (
	"fmt"
	"reflect"
	"strings"
	"testing"
)

// Real SQLite statement failures, distinct from killing a process or proving a
// remote effect. Check the injected error so an earlier guard cannot fake PASS.
func TestActionAdmissionSQLFailuresRollbackEveryWrite(t *testing.T) {
	for _, tc := range []struct{ operation, trigger string }{
		{"admit", "BEFORE INSERT ON action_resource_leases"},
		{"admit", "BEFORE INSERT ON action_leases"},
		{"admit", "BEFORE INSERT ON action_lease_events"},
		{"admit", "BEFORE UPDATE ON action_journal"},
		{"admit", "BEFORE INSERT ON control_events WHEN NEW.revision=2"},
		{"takeover", "BEFORE UPDATE ON action_resource_leases"},
		{"takeover", "BEFORE UPDATE ON action_leases"},
		{"renew", "BEFORE UPDATE ON action_leases"},
		{"renew", "BEFORE UPDATE ON action_resource_leases"},
		{"renew", "BEFORE INSERT ON action_lease_events"},
		{"settle", "BEFORE UPDATE ON action_journal"},
		{"settle", "BEFORE UPDATE ON action_leases"},
		{"settle", "BEFORE DELETE ON action_resource_leases"},
		{"settle", "BEFORE INSERT ON action_lease_events"},
	} {
		t.Run(tc.operation+"/"+tc.trigger, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()
			if err := control.Put(Record{Domain: "action", ID: "sql-fault", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
				Payload: []byte(`{"parameters":{"targetId":"fault-target"}}`)}); err != nil {
				t.Fatal(err)
			}
			var claim AdmissionResult
			var err error
			if tc.operation != "admit" {
				claim, err = control.AdmitAndClaimAction(AdmissionRequest{ActionID: "sql-fault", LeaseSeconds: 10})
				if err != nil {
					t.Fatal(err)
				}
			}
			before, _, err := control.Get("action", "sql-fault")
			if err != nil {
				t.Fatal(err)
			}
			writer := control.Writer()
			resources, err := control.GetResourceLeases("sql-fault")
			if err != nil {
				t.Fatal(err)
			}
			until := int64(1001)
			if tc.operation == "takeover" {
				until = 1010
			}
			control.now = func() int64 { return until }
			statement := fmt.Sprintf("CREATE TRIGGER admission_sql_fault %s BEGIN SELECT RAISE(ABORT,'admission injected SQL failure'); END", tc.trigger)
			if _, err := control.db.Exec(statement); err != nil {
				t.Fatal(err)
			}
			switch tc.operation {
			case "admit", "takeover":
				_, err = control.AdmitAndClaimAction(AdmissionRequest{ActionID: "sql-fault", LeaseSeconds: 10})
			case "renew":
				_, err = control.RenewActionLease(renewalFor(claim.Lease))
			case "settle":
				_, err = control.FailAction("sql-fault", claim.Lease.Epoch, claim.Lease.ClaimToken, nil)
			}
			if err == nil || !strings.Contains(err.Error(), "admission injected SQL failure") {
				t.Fatalf("did not reach SQLite fault: %v", err)
			}
			if _, err := control.db.Exec("DROP TRIGGER admission_sql_fault"); err != nil {
				t.Fatal(err)
			}
			after, exists, err := control.Get("action", "sql-fault")
			if err != nil || !exists || !sameControlRecord(after, before) {
				t.Fatalf("record changed: %v", err)
			}
			afterResources, err := control.GetResourceLeases("sql-fault")
			if err != nil || !reflect.DeepEqual(resources, afterResources) {
				t.Fatalf("reservations changed: %v", err)
			}
			if tc.operation != "admit" {
				assertBoundaryLeaseUnchanged(t, control, claim.Lease, writer)
			} else {
				if _, exists, err := control.GetActionLease("sql-fault"); err != nil || exists {
					t.Fatal("rejected claim persisted")
				}
				assertControlTransactionUnchanged(t, control, writer, 1)
			}
		})
	}
}

func TestActionAdmissionUnavailableDatabaseFailsClosed(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	if err := control.db.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Record.ID}); err == nil {
		t.Fatal("admitted on closed database")
	}
	if _, err := control.RenewActionLease(renewalFor(claim.Lease)); err == nil {
		t.Fatal("renewed on closed database")
	}
	if _, err := control.FailAction(claim.Record.ID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil); err == nil {
		t.Fatal("settled on closed database")
	}
	if _, _, err := control.GetActionLease(claim.Record.ID); err == nil {
		t.Fatal("read unavailable lease")
	}
	if _, err := control.GetResourceLeases(claim.Record.ID); err == nil {
		t.Fatal("read unavailable resources")
	}
}
