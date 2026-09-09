package store

import (
	"errors"
	"reflect"
	"testing"
)

func TestLeasedStorageDispatchResolvesOriginalEpochAfterTakeover(t *testing.T) {
	control, original := executingLeasedAction(t)
	originalDispatch, _, err := control.GetStorageDispatch(original.Lease.ActionID, original.Lease.Epoch)
	if err != nil {
		t.Fatal(err)
	}
	for _, takeover := range []bool{false, true} {
		claim := original
		if takeover {
			control.now = func() int64 { return 1010 }
			claim, err = control.AdmitAndClaimAction(AdmissionRequest{ActionID: original.Lease.ActionID, Owner: "successor", LeaseSeconds: 20})
			if err != nil || claim.Lease.Epoch != original.Lease.Epoch+1 {
				t.Fatalf("takeover: %v", err)
			}
		}
		writer := control.Writer()
		before, _, _ := control.Get("action", claim.Lease.ActionID)
		resources, _ := control.GetResourceLeases(claim.Lease.ActionID)
		dispatch, bound, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken)
		if err != nil || !bound || dispatch != originalDispatch {
			t.Fatalf("takeover=%v did not retain original dispatch: bound=%v err=%v", takeover, bound, err)
		}
		after, _, _ := control.Get("action", claim.Lease.ActionID)
		afterResources, _ := control.GetResourceLeases(claim.Lease.ActionID)
		if !reflect.DeepEqual(before, after) || !reflect.DeepEqual(resources, afterResources) || writer != control.Writer() {
			t.Fatal("read-only recovery lookup changed ownership or reservations")
		}
		if takeover {
			if _, bound, err := control.GetLeasedStorageDispatch(original.Lease.ActionID, original.Lease.Epoch, original.Lease.ClaimToken); !errors.Is(err, ErrActionLeaseStale) || bound {
				t.Fatalf("old claim recovered after takeover: %v", err)
			}
			if _, bound, err := control.GetStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch); err != nil || bound {
				t.Fatalf("lookup fabricated a replacement dispatch: %v", err)
			}
		}
	}
}

func TestLeasedStorageDispatchNeverInventsMissingDispatch(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	for _, takeover := range []bool{false, true} {
		if takeover {
			control.now = func() int64 { return 1010 }
			var err error
			claim, err = control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID})
			if err != nil {
				t.Fatal(err)
			}
		}
		if _, bound, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken); err != nil || bound {
			t.Fatalf("takeover=%v fabricated dispatch: %v", takeover, err)
		}
	}
}

func TestLeasedStorageDispatchRejectsInvalidOrLostOwnership(t *testing.T) {
	for _, fault := range []string{"empty id", "zero epoch", "wrong epoch", "empty token", "wrong token", "expired", "clock rollback", "writer lost", "resource missing", "terminal", "closed", "inactive schema", "expires during read"} {
		t.Run(fault, func(t *testing.T) {
			control, claim := executingLeasedAction(t)
			id, epoch, token := claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken
			want := ErrActionLeaseStale
			switch fault {
			case "empty id":
				id, want = "", ErrInvalidStorageIntent
			case "zero epoch":
				epoch, want = 0, ErrInvalidStorageIntent
			case "wrong epoch":
				epoch++
			case "empty token":
				token, want = "", ErrInvalidClaimToken
			case "wrong token":
				token, want = "not-the-claim", ErrInvalidClaimToken
			case "expired":
				control.now = func() int64 { return 1010 }
				want = ErrActionLeaseExpired
			case "clock rollback":
				control.now = func() int64 { return 999 }
				want = ErrActionLeaseExpired
			case "writer lost":
				deadline := control.Writer().LeaseUntil
				control.now = func() int64 { return deadline }
				want = ErrWriterFenceHeld
			case "resource missing":
				if _, err := control.db.Exec("DELETE FROM action_resource_leases"); err != nil {
					t.Fatal(err)
				}
			case "terminal":
				if _, err := control.CompleteAction(id, epoch, token, nil); err != nil {
					t.Fatal(err)
				}
			case "closed":
				if err := control.Close(); err != nil {
					t.Fatal(err)
				}
				want = ErrWriterFenceHeld
			case "inactive schema":
				control.schema, want = SchemaV4, ErrSchemaInactive
			case "expires during read":
				calls := 0
				control.now = func() int64 {
					calls++
					if calls == 1 {
						return 1001
					}
					return 1010
				}
				want = ErrActionLeaseExpired
			}
			writer := control.Writer()
			if _, bound, err := control.GetLeasedStorageDispatch(id, epoch, token); !errors.Is(err, want) || bound {
				t.Fatalf("unsafe lookup: bound=%v err=%v want=%v", bound, err, want)
			}
			if writer != control.Writer() {
				t.Fatal("failed lookup renewed writer")
			}
		})
	}
}

func TestLeasedStorageDispatchRejectsCorruptOriginalIntent(t *testing.T) {
	for _, update := range []string{"operation_id='substituted'", "claim_revision=2", "writer_fencing_token=99", "intent_json=intent_json||' '"} {
		t.Run(update, func(t *testing.T) {
			control, claim := executingLeasedAction(t)
			control.now = func() int64 { return 1010 }
			claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID})
			if err != nil {
				t.Fatal(err)
			}
			// Deliberate local corruption, never a successful-effect seed.
			if _, err := control.db.Exec("DROP TRIGGER storage_dispatches_no_update"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec("UPDATE storage_dispatches SET " + update); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec(storageDispatchSchemaObjects["storage_dispatches_no_update"]); err != nil {
				t.Fatal(err)
			}
			if _, bound, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken); !errors.Is(err, ErrCorruptRecord) || bound {
				t.Fatalf("corrupt original intent recovered: %v", err)
			}
		})
	}
}

func TestLeasedStorageDispatchReadDoesNotRenewSQLiteLeases(t *testing.T) {
	control, claim := executingLeasedAction(t)
	writer := control.Writer()
	control.now = func() int64 { return 1001 }
	if _, bound, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken); err != nil || !bound {
		t.Fatal(err)
	}
	var persistedUntil int64
	if err := control.db.QueryRow("SELECT lease_until FROM control_writer").Scan(&persistedUntil); err != nil || persistedUntil != writer.LeaseUntil {
		t.Fatalf("read extended persisted writer deadline: %d %v", persistedUntil, err)
	}
	lease, _, _ := control.GetActionLease(claim.Lease.ActionID)
	if lease != claim.Lease {
		t.Fatal("read renewed action lease")
	}
}
