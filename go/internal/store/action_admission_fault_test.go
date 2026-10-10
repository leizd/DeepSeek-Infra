package store

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

// admittedAction returns an open store and an action it has already claimed, which is the
// starting point for the faults below: every one of them breaks exactly one thing the
// admission or renewal path has to re-derive before it may extend a lease.
func admittedAction(t *testing.T, actionID string, epoch uint64) (*Control, AdmissionResult) {
	t.Helper()
	control := openControlAt(t, 1000)
	if err := control.Put(Record{
		Domain: "action", ID: actionID, Revision: 1, ExecutionEpoch: epoch, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":{"policyId":"p1","destTargetId":"t1"}}`),
	}); err != nil {
		t.Fatal(err)
	}
	admitted, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: actionID, Owner: "worker-a", LeaseSeconds: 60, ResourceKeys: []string{"res-1", "res-2"},
	})
	if err != nil {
		t.Fatal(err)
	}
	return control, admitted
}

func renew(control *Control, admitted AdmissionResult) (ActionLease, error) {
	return control.RenewActionLease(ActionLeaseRenewal{
		ActionID:     admitted.Lease.ActionID,
		Epoch:        admitted.Lease.Epoch,
		ClaimToken:   admitted.Lease.ClaimToken,
		Owner:        admitted.Lease.Owner,
		LeaseSeconds: 60,
	})
}

// A lease may only be extended by the holder of the epoch it was granted against; anything
// else reads as stale, never as renewable.
func TestLeaseRenewalRefusesClaimsItCannotReDerive(t *testing.T) {
	t.Run("wrong epoch", func(t *testing.T) {
		control, admitted := admittedAction(t, "renew-epoch-act", 1)
		defer control.Close()
		if _, err := control.RenewActionLease(ActionLeaseRenewal{
			ActionID:     admitted.Lease.ActionID,
			Epoch:        admitted.Lease.Epoch + 1,
			ClaimToken:   admitted.Lease.ClaimToken,
			Owner:        admitted.Lease.Owner,
			LeaseSeconds: 60,
		}); !errors.Is(err, ErrActionLeaseStale) {
			t.Fatalf("a renewal carrying another epoch must be stale: %v", err)
		}
	})

	t.Run("wrong claim token", func(t *testing.T) {
		control, admitted := admittedAction(t, "renew-token-act", 1)
		defer control.Close()
		if _, err := control.RenewActionLease(ActionLeaseRenewal{
			ActionID:     admitted.Lease.ActionID,
			Epoch:        admitted.Lease.Epoch,
			ClaimToken:   strings.Repeat("0", len(admitted.Lease.ClaimToken)),
			Owner:        admitted.Lease.Owner,
			LeaseSeconds: 60,
		}); !errors.Is(err, ErrInvalidClaimToken) {
			t.Fatalf("a renewal carrying another claim token must be refused: %v", err)
		}
	})

	t.Run("the holder may renew", func(t *testing.T) {
		control, admitted := admittedAction(t, "renew-ok-act", 1)
		defer control.Close()
		renewed, err := renew(control, admitted)
		if err != nil {
			t.Fatal(err)
		}
		// The clock is pinned by the fixture, so the deadline cannot move forward here; what
		// this asserts is that the holder's own renewal is accepted at all, which is what
		// makes the refusals above refusals of the right thing.
		if renewed.LeaseUntil < admitted.Lease.LeaseUntil {
			t.Fatalf("renewal must not shorten the deadline: %d -> %d", admitted.Lease.LeaseUntil, renewed.LeaseUntil)
		}
		if renewed.Epoch != admitted.Lease.Epoch || renewed.ClaimToken != admitted.Lease.ClaimToken {
			t.Fatalf("renewal must keep the claim identity: %+v", renewed)
		}
	})
}

// The lease ledger is what a lease is re-derived from, so it is append-only: the schema
// raises on any UPDATE or DELETE. That is also why the re-derivation checks in
// `validateActionLeaseResourcesTx` read as defensive — a manifest that no longer matches
// cannot be produced through SQL at all.
func TestActionLeaseLedgerIsAppendOnly(t *testing.T) {
	control, admitted := admittedAction(t, "ledger-act", 1)
	defer control.Close()

	for _, statement := range []string{
		"UPDATE action_lease_events SET resource_keys_json='[]' WHERE action_id=?",
		"UPDATE action_lease_events SET owner='someone-else' WHERE action_id=?",
		"DELETE FROM action_lease_events WHERE action_id=?",
	} {
		if _, err := control.db.Exec(statement, admitted.Lease.ActionID); err == nil {
			t.Fatalf("the lease ledger must refuse %q", statement)
		}
	}
}

// An action that carries no execution epoch is refused at the write boundary, so the lease
// epoch a fresh claim would bump from zero can never be reached.
func TestActionsWithoutAnExecutionEpochAreRefusedAtTheWriteBoundary(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	if err := control.Put(Record{
		Domain: "action", ID: "zero-epoch-act", Revision: 1, ExecutionEpoch: 0, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":{"policyId":"p1","destTargetId":"t1"}}`),
	}); !errors.Is(err, internalprotocol.ErrZeroEpoch) {
		t.Fatalf("an action with no execution epoch must be refused: %v", err)
	}
	if _, exists, err := control.Get("action", "zero-epoch-act"); err != nil || exists {
		t.Fatalf("a refused action must not be recorded: exists=%v err=%v", exists, err)
	}
}

// Unrepresentable resource keys are refused before anything is reserved for them.
func TestFreshClaimsRefuseUnrepresentableResourceKeys(t *testing.T) {
	for _, testCase := range []struct {
		name string
		key  string
	}{
		{"a key over the 1024-byte bound", strings.Repeat("k", 1025)},
		{"a key carrying a NUL", "res\x001"},
		{"a key that is not valid UTF-8", "res\xff1"},
		{"an empty key", ""},
	} {
		t.Run("the resource key "+testCase.name, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()
			if err := control.Put(Record{
				Domain: "action", ID: "bad-key-act", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
				Payload: json.RawMessage(`{"parameters":{"policyId":"p1","destTargetId":"t1"}}`),
			}); err != nil {
				t.Fatal(err)
			}
			if _, err := control.AdmitAndClaimAction(AdmissionRequest{
				ActionID: "bad-key-act", Owner: "worker-a", LeaseSeconds: 60, ResourceKeys: []string{testCase.key},
			}); !errors.Is(err, ErrInvalidPayload) {
				t.Fatalf("an unrepresentable resource key must be refused: %v", err)
			}
		})
	}
}

// The journal is not authoritative on its own: admission re-reads it against the event log
// and against the lease ledger, and refuses when the three disagree. A rewound journal, a
// lease row left behind by a claim the journal no longer shows, and a retained lease event
// with no live lease are all states a crash can leave, and none of them may be claimed.
func TestAdmissionRefusesAJournalThatDisagreesWithTheLedger(t *testing.T) {
	t.Run("a journal rewound without its event log", func(t *testing.T) {
		control, admitted := admittedAction(t, "journal-rewind-act", 1)
		defer control.Close()
		if _, err := control.db.Exec(
			"UPDATE action_journal SET state='PENDING' WHERE id=?", admitted.Lease.ActionID,
		); err != nil {
			t.Fatal(err)
		}
		// Admission reads the record before it looks at the lease, so the disagreement is
		// refused as a corrupt record rather than as a stale lease.
		if _, err := control.AdmitAndClaimAction(AdmissionRequest{
			ActionID: admitted.Lease.ActionID, Owner: "worker-b", LeaseSeconds: 60, ResourceKeys: []string{"res-1"},
		}); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("a journal that disagrees with its event log must be refused: %v", err)
		}
	})

	t.Run("a pending action that still holds a lease row", func(t *testing.T) {
		control := openControlAt(t, 1000)
		defer control.Close()
		if err := control.Put(Record{
			Domain: "action", ID: "leftover-lease-act", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
			Payload: json.RawMessage(`{"parameters":{"policyId":"p1","destTargetId":"t1"}}`),
		}); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(`INSERT INTO action_leases(
			action_id, owner, epoch, claim_token, lease_until, acquired_at, updated_at,
			claim_revision, writer_fencing_token) VALUES(?, 'ghost', 1, ?, 2000, 1000, 1000, 1, ?)`,
			"leftover-lease-act", strings.Repeat("a", 32), control.token); err != nil {
			t.Fatal(err)
		}
		if _, err := control.AdmitAndClaimAction(AdmissionRequest{
			ActionID: "leftover-lease-act", Owner: "worker-a", LeaseSeconds: 60, ResourceKeys: []string{"res-1"},
		}); !errors.Is(err, ErrActionLeaseStale) {
			t.Fatalf("a pending action with a leftover lease row must be stale: %v", err)
		}
	})

	t.Run("a retained lease event with no live lease", func(t *testing.T) {
		control := openControlAt(t, 1000)
		defer control.Close()
		if err := control.Put(Record{
			Domain: "action", ID: "retained-event-act", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
			Payload: json.RawMessage(`{"parameters":{"policyId":"p1","destTargetId":"t1"}}`),
		}); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(`INSERT INTO action_lease_events(
			action_id, event_type, owner, epoch, claim_token, lease_until, claim_revision,
			writer_fencing_token, recorded_at, resource_keys_json)
			VALUES(?, 'TERMINATED', 'ghost', 1, ?, 2000, 1, ?, 1000, '[]')`,
			"retained-event-act", strings.Repeat("a", 32), control.token); err != nil {
			t.Fatal(err)
		}
		if _, err := control.AdmitAndClaimAction(AdmissionRequest{
			ActionID: "retained-event-act", Owner: "worker-a", LeaseSeconds: 60, ResourceKeys: []string{"res-1"},
		}); !errors.Is(err, ErrActionLeaseStale) {
			t.Fatalf("a retained lease event must block a fresh claim: %v", err)
		}
	})
}

func TestAdmissionFaultInjectionRollbacks(t *testing.T) {
	faultStages := []string{
		"after_budget_check",
		"after_first_resource",
		"mid_resources",
		"after_action_lease_insert",
		"after_control_event_write",
		"before_final_writer_fence",
		"at_commit",
	}

	for _, stage := range faultStages {
		t.Run(stage, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()

			actionID := "fault-act-" + stage
			if err := control.Put(Record{
				Domain: "action", ID: actionID, Revision: 1, ExecutionEpoch: 1, State: "PENDING",
				Payload: json.RawMessage(`{"parameters":{"policyId":"p1","destTargetId":"t1"}}`),
			}); err != nil {
				t.Fatal(err)
			}

			// Hook injection point
			control.admissionFaultStage = stage

			req := AdmissionRequest{
				ActionID:     actionID,
				Owner:        "worker-fault",
				LeaseSeconds: 60,
				ResourceKeys: []string{"res-1", "res-2", "res-3"},
			}

			_, err := control.AdmitAndClaimAction(req)
			if err == nil {
				t.Fatalf("expected error for fault stage %s, got nil", stage)
			}

			// Verify invariant 1: Zero orphan resource leases
			for _, resKey := range []string{"res-1", "res-2", "res-3"} {
				var count int
				if err := control.db.QueryRow("SELECT COUNT(*) FROM action_resource_leases WHERE resource_key=?", resKey).Scan(&count); err != nil {
					t.Fatal(err)
				}
				if count != 0 {
					t.Fatalf("stage %s left orphan resource lease for %s", stage, resKey)
				}
			}

			// Verify invariant 2: Zero orphan action leases
			var leaseCount int
			if err := control.db.QueryRow("SELECT COUNT(*) FROM action_leases WHERE action_id=?", actionID).Scan(&leaseCount); err != nil {
				t.Fatal(err)
			}
			if leaseCount != 0 {
				t.Fatalf("stage %s left orphan action lease", stage)
			}

			// Verify invariant 3: Zero action lease events
			var eventCount int
			if err := control.db.QueryRow("SELECT COUNT(*) FROM action_lease_events WHERE action_id=?", actionID).Scan(&eventCount); err != nil {
				t.Fatal(err)
			}
			if eventCount != 0 {
				t.Fatalf("stage %s left orphan action lease events", stage)
			}

			// Verify invariant 4: Action record remains in PENDING with revision 1
			rec, exists, err := control.Get("action", actionID)
			if err != nil || !exists {
				t.Fatalf("failed to fetch action: %v", err)
			}
			if rec.State != "PENDING" || rec.Revision != 1 {
				t.Fatalf("stage %s corrupted action record: %+v", stage, rec)
			}

			// Verify invariant 5: No orphan control_events for revision > 1
			var ctrlEventCount int
			if err := control.db.QueryRow("SELECT COUNT(*) FROM control_events WHERE domain='action' AND record_id=? AND revision>1", actionID).Scan(&ctrlEventCount); err != nil {
				t.Fatal(err)
			}
			if ctrlEventCount != 0 {
				t.Fatalf("stage %s left orphan control event", stage)
			}
		})
	}
}
