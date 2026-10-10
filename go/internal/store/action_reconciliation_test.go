package store

import (
	"errors"
	"reflect"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func TestActionReconciliationRepeatedTakeoverPreservesOriginalDispatch(t *testing.T) {
	for _, uncertain := range []bool{false, true} {
		t.Run(map[bool]string{false: "executing", true: "effect_unknown"}[uncertain], func(t *testing.T) {
			control, claim := executingLeasedAction(t)
			original, _, err := control.GetStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch)
			if err != nil {
				t.Fatal(err)
			}
			if uncertain {
				if _, err := control.MarkActionEffectUnknown(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken); err != nil {
					t.Fatal(err)
				}
			}
			for i := 0; i < 3; i++ {
				previous := claim
				now := claim.Lease.LeaseUntil + 1
				control.now = func() int64 { return now }
				claim, err = control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID, Owner: "successor", LeaseSeconds: 10})
				if err != nil {
					t.Fatal(err)
				}
				if claim.Record.State != "RECONCILING" || claim.Lease.Epoch != previous.Lease.Epoch+1 || claim.Lease.ClaimToken == previous.Lease.ClaimToken || !reflect.DeepEqual(claim.ResourceKeys, previous.ResourceKeys) {
					t.Fatalf("takeover %d did not preserve reconciliation authority: state=%s epoch=%d", i, claim.Record.State, claim.Lease.Epoch)
				}
				if _, err := control.RenewActionLease(renewalFor(previous.Lease)); !errors.Is(err, ErrActionLeaseStale) {
					t.Fatalf("old owner renewed: %v", err)
				}
				if _, err := control.RenewActionLease(renewalFor(claim.Lease)); err != nil {
					t.Fatal(err)
				}
				// Renewal changes only deadlines, not claim identity.
				claim.Lease, _, err = control.GetActionLease(claim.Lease.ActionID)
				if err != nil {
					t.Fatal(err)
				}
				got, bound, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken)
				if err != nil || !bound || got != original {
					t.Fatalf("original dispatch lost at takeover %d: bound=%v err=%v", i, bound, err)
				}
				if _, bound, err := control.GetStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch); err != nil || bound {
					t.Fatalf("replacement dispatch fabricated: %v", err)
				}
			}
			if _, err := control.MarkActionEffectUnknown(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken); err != nil {
				t.Fatal(err)
			}
			locks, err := control.GetResourceLeases(claim.Lease.ActionID)
			if err != nil || len(locks) != len(claim.ResourceKeys) {
				t.Fatalf("uncertainty released reservations: %v", err)
			}
		})
	}
}

func TestActionReconciliationRetainsBudgetsAndCannotRedispatch(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	for i := 0; i < 2; i++ {
		now := claim.Lease.LeaseUntil + 1
		control.now = func() int64 { return now }
		var err error
		claim, err = control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID, LeaseSeconds: 10})
		if err != nil {
			t.Fatal(err)
		}
		if _, bound, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken); err != nil || bound {
			t.Fatalf("missing original dispatch fabricated: %v", err)
		}
	}
	if err := control.Put(Record{Domain: "action", ID: "other", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "other", Policy: AdmissionPolicy{MaxConcurrentActions: 1}}); !errors.Is(err, ErrBudgetExceeded) {
		t.Fatalf("reconciliation not counted in budget: %v", err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "other", ResourceKeys: claim.ResourceKeys}); !errors.Is(err, ErrResourceConflict) {
		t.Fatalf("reconciliation reservations stolen: %v", err)
	}
	record, intent := boundaryDispatch(claim)
	record.Revision = claim.Record.Revision + 1
	if err := control.ClaimLeasedStorageDispatch(record, intent, claim.Lease.ClaimToken); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("reconciliation dispatched a new mutation: %v", err)
	}
	if err := control.Put(Record{Domain: "action", ID: "other", Revision: 2, ExecutionEpoch: 1, State: "CLAIMED"}); err != nil {
		t.Fatal(err)
	}
	if err := control.Put(Record{Domain: "action", ID: "other", Revision: 3, ExecutionEpoch: 2, State: "RECONCILING"}); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("generic writer bypassed native claim: %v", err)
	}
	if LegalTransition("action", "CLAIMED", "RECONCILING") {
		t.Fatal("legacy graph silently expanded")
	}
}

// Change a journal state with a coherent record digest solely to test historical
// validation. This never seeds provider effects or successful execution evidence.
func rewriteReconciliationFixtureState(t *testing.T, control *Control, record Record, state string) {
	t.Helper()
	record.State = state
	digest, err := protocol.Digest(record)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE control_events SET state=?,record_digest=? WHERE domain='action' AND record_id=? AND revision=?", state, digest, record.ID, record.Revision); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE action_journal SET state=?,record_digest=? WHERE id=? AND revision=?", state, digest, record.ID, record.Revision); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(controlEventImmutabilityTriggers[0]); err != nil {
		t.Fatal(err)
	}
}

func TestActionReconciliationMigrationDoesNotLegalizeOldStateEdges(t *testing.T) {
	control, claim := executingLeasedAction(t)
	control.now = func() int64 { return 1011 }
	claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID})
	if err != nil {
		t.Fatal(err)
	}
	// Leave the otherwise valid new edge in a historical-v5-shaped corrupt fixture.
	prepareReconciliationV5Fixture(t, control)
	if _, _, err := control.Get("action", claim.Lease.ActionID); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("v5 accepted new edge: %v", err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: control.path, Owner: "new-reader", Now: func() int64 { return 1012 }})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if _, _, err := reopened.Get("action", claim.Lease.ActionID); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("upgrade legalized old corrupt history: %v", err)
	}
}

func TestActionReconciliationV5UnknownTakeoverStillResolves(t *testing.T) {
	control, originalClaim := executingLeasedAction(t)
	original, _, err := control.GetStorageDispatch(originalClaim.Lease.ActionID, originalClaim.Lease.Epoch)
	if err != nil {
		t.Fatal(err)
	}
	control.now = func() int64 { return 1011 }
	claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: originalClaim.Lease.ActionID, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	rewriteReconciliationFixtureState(t, control, claim.Record, "EFFECT_UNKNOWN")
	prepareReconciliationV5Fixture(t, control)
	if _, _, err := control.Get("action", claim.Lease.ActionID); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: control.path, Owner: "new-reader", Now: func() int64 { return 1022 }})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	next, err := reopened.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID})
	if err != nil {
		t.Fatal(err)
	}
	got, bound, err := reopened.GetLeasedStorageDispatch(next.Lease.ActionID, next.Lease.Epoch, next.Lease.ClaimToken)
	if err != nil || !bound || got != original {
		t.Fatalf("v5 takeover lost original dispatch across v6 takeover: %v", err)
	}
}

func TestActionReconciliationRejectsUnboundOrCorruptClaimHistory(t *testing.T) {
	for _, update := range []string{"claim_revision=3", "writer_fencing_token=99", "recorded_at=999", "event_type='RENEWED'"} {
		t.Run(update, func(t *testing.T) {
			control, claim := executingLeasedAction(t)
			control.now = func() int64 { return 1011 }
			claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec("DROP TRIGGER action_lease_events_no_update"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec("UPDATE action_lease_events SET " + update + " WHERE event_type='TAKEOVER'"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec(actionAdmissionSchemaObjects["action_lease_events_no_update"]); err != nil {
				t.Fatal(err)
			}
			if _, _, err := control.Get("action", claim.Lease.ActionID); !errors.Is(err, ErrCorruptRecord) {
				t.Fatalf("unbound reconciliation history accepted: %v", err)
			}
		})
	}
}
