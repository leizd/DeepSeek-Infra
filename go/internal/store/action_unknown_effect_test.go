package store

import (
	"errors"
	"reflect"
	"testing"
)

func executingLeasedAction(t *testing.T) (*Control, AdmissionResult) {
	t.Helper()
	control, claim := admittedBoundaryAction(t)
	record, intent := boundaryDispatch(claim)
	if err := control.ClaimLeasedStorageDispatch(record, intent, claim.Lease.ClaimToken); err != nil {
		t.Fatal(err)
	}
	return control, claim
}

func TestUnknownActionEffectRetainsLeaseResourcesAndDispatch(t *testing.T) {
	control, claim := executingLeasedAction(t)
	resources, err := control.GetResourceLeases(claim.Record.ID)
	if err != nil {
		t.Fatal(err)
	}
	beforeDispatch, _, err := control.GetStorageDispatch(claim.Record.ID, claim.Lease.Epoch)
	if err != nil {
		t.Fatal(err)
	}
	unknown, err := control.MarkActionEffectUnknown(claim.Record.ID, claim.Lease.Epoch, claim.Lease.ClaimToken)
	if err != nil {
		t.Fatal(err)
	}
	if unknown.State != "EFFECT_UNKNOWN" || unknown.Revision != 4 || unknown.ExecutionEpoch != claim.Lease.Epoch {
		t.Fatalf("incorrect unknown transition: %+v", unknown)
	}
	lease, exists, err := control.GetActionLease(claim.Record.ID)
	if err != nil || !exists || lease != claim.Lease {
		t.Fatalf("unknown effect released or changed lease: %v", err)
	}
	afterResources, err := control.GetResourceLeases(claim.Record.ID)
	if err != nil || !reflect.DeepEqual(resources, afterResources) {
		t.Fatalf("unknown effect changed resources: %v", err)
	}
	afterDispatch, bound, err := control.GetStorageDispatch(claim.Record.ID, claim.Lease.Epoch)
	if err != nil || !bound || afterDispatch != beforeDispatch {
		t.Fatalf("unknown effect lost dispatch: %v", err)
	}
	control.now = func() int64 { return 1001 }
	if _, err := control.RenewActionLease(renewalFor(claim.Lease)); err != nil {
		t.Fatalf("uncertain action cannot renew: %v", err)
	}
}

func TestUnknownActionEffectRejectsLostClaimAndLateCommit(t *testing.T) {
	for _, fault := range []string{"wrong token", "stale epoch", "expired", "expires at commit", "missing resource"} {
		t.Run(fault, func(t *testing.T) {
			control, claim := executingLeasedAction(t)
			writer := control.Writer()
			token, epoch := claim.Lease.ClaimToken, claim.Lease.Epoch
			want := ErrActionLeaseExpired
			switch fault {
			case "wrong token":
				token, want = "wrong-claim", ErrInvalidClaimToken
			case "stale epoch":
				epoch, want = epoch+1, ErrActionLeaseStale
			case "expired":
				control.now = func() int64 { return 1010 }
			case "expires at commit":
				calls := 0
				control.now = func() int64 {
					calls++
					if calls == 1 {
						return 1001
					}
					return 1010
				}
			case "missing resource":
				if _, err := control.db.Exec("DELETE FROM action_resource_leases"); err != nil {
					t.Fatal(err)
				}
				want = ErrActionLeaseStale
			}
			if _, err := control.MarkActionEffectUnknown(claim.Record.ID, epoch, token); !errors.Is(err, want) {
				t.Fatalf("unknown transition error=%v want=%v", err, want)
			}
			assertControlTransactionUnchanged(t, control, writer, 3)
			current, _, err := control.Get("action", claim.Record.ID)
			if err != nil || current.State != "EXECUTING" || current.Revision != 3 {
				t.Fatalf("failed transition changed journal: %v", err)
			}
		})
	}
}
