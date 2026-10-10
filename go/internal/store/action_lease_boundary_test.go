package store

import (
	"errors"
	"math"
	"reflect"
	"strings"
	"testing"
)

func admittedBoundaryAction(t *testing.T) (*Control, AdmissionResult) {
	t.Helper()
	control := openControlAt(t, 1000)
	t.Cleanup(func() { _ = control.Close() })
	if err := control.Put(Record{Domain: "action", ID: "lease-boundary", Revision: 1, ExecutionEpoch: 1,
		State: "PENDING", Payload: []byte(`{"parameters":{"targetId":"target-boundary"}}`)}); err != nil {
		t.Fatal(err)
	}
	claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "lease-boundary", Owner: "action-owner", LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	return control, claim
}

func renewalFor(lease ActionLease) ActionLeaseRenewal {
	return ActionLeaseRenewal{ActionID: lease.ActionID, Epoch: lease.Epoch, Owner: lease.Owner, ClaimToken: lease.ClaimToken, LeaseSeconds: 20}
}

func assertBoundaryLeaseUnchanged(t *testing.T, control *Control, before ActionLease, writer WriterLease) {
	t.Helper()
	got, exists, err := control.GetActionLease(before.ActionID)
	if err != nil || !exists || got != before {
		t.Fatalf("rejected operation changed action lease: exists=%v err=%v", exists, err)
	}
	var events int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM action_lease_events WHERE action_id=?", before.ActionID).Scan(&events); err != nil || events != 1 {
		t.Fatalf("rejected operation changed lease events: count=%d err=%v", events, err)
	}
	assertControlTransactionUnchanged(t, control, writer, 2)
}

func TestActionLeaseDeadlineIsExclusive(t *testing.T) {
	for _, operation := range []string{"renew", "settle"} {
		t.Run(operation, func(t *testing.T) {
			control, claim := admittedBoundaryAction(t)
			before := control.Writer()
			control.now = func() int64 { return claim.Lease.LeaseUntil }
			var err error
			if operation == "renew" {
				_, err = control.RenewActionLease(renewalFor(claim.Lease))
			} else {
				_, err = control.FailAction(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil)
			}
			if !errors.Is(err, ErrActionLeaseExpired) {
				t.Fatalf("deadline must already be expired: %v", err)
			}
			assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
		})
	}
}

func TestSuccessorWriterCannotReusePriorActionClaim(t *testing.T) {
	for _, operation := range []string{"renew", "settle"} {
		t.Run(operation, func(t *testing.T) {
			original, claim := admittedBoundaryAction(t)
			if err := original.Close(); err != nil {
				t.Fatal(err)
			}
			successor, err := OpenControl(OpenOptions{Path: original.path, Owner: "successor", Now: func() int64 { return 1001 }})
			if err != nil {
				t.Fatal(err)
			}
			defer successor.Close()
			before := successor.Writer()
			if operation == "renew" {
				_, err = successor.RenewActionLease(renewalFor(claim.Lease))
			} else {
				_, err = successor.FailAction(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil)
			}
			if !errors.Is(err, ErrActionLeaseStale) {
				t.Fatalf("new writer adopted old action claim: %v", err)
			}
			assertBoundaryLeaseUnchanged(t, successor, claim.Lease, before)
		})
	}
}

func TestActionSettlementUsesCanonicalPayloadValidation(t *testing.T) {
	for _, tc := range []struct {
		name    string
		payload []byte
		want    error
	}{
		{"secret field", []byte(`{"privateKey":"not-a-real-key"}`), ErrSecretDetected},
		{"oversized", []byte(`{"result":"` + strings.Repeat("x", maximumPayloadBytes) + `"}`), ErrInvalidPayload},
	} {
		t.Run(tc.name, func(t *testing.T) {
			control, claim := admittedBoundaryAction(t)
			before := control.Writer()
			if _, err := control.FailAction(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, tc.payload); !errors.Is(err, tc.want) {
				t.Fatalf("invalid settlement accepted: %v", err)
			}
			assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
		})
	}
}

func TestActionRenewalCannotShortenLease(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	control.now = func() int64 { return 1001 }
	req := renewalFor(claim.Lease)
	req.LeaseSeconds = 1
	renewed, err := control.RenewActionLease(req)
	if err != nil || renewed.LeaseUntil != claim.Lease.LeaseUntil {
		t.Fatalf("renewal shortened deadline: until=%d err=%v", renewed.LeaseUntil, err)
	}
	resources, err := control.GetResourceLeases(claim.Lease.ActionID)
	if err != nil || len(resources) != 1 || resources[0].LeaseUntil != renewed.LeaseUntil {
		t.Fatalf("resource deadline changed independently: count=%d err=%v", len(resources), err)
	}
}

func TestActionRenewalDurationOverflowFailsBeforeMutation(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	before := control.Writer()
	req := renewalFor(claim.Lease)
	req.LeaseSeconds = math.MaxInt64
	if _, err := control.RenewActionLease(req); !errors.Is(err, ErrActionLeaseExpired) {
		t.Fatalf("duration overflow must fail before SQL: %v", err)
	}
	assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
}

func TestAdmissionExtraResourcesCannotOmitDerivedReservations(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	if err := control.Put(Record{Domain: "action", ID: "derived-scope", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: []byte(`{"parameters":{"targetId":"required-target"}}`)}); err != nil {
		t.Fatal(err)
	}
	claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "derived-scope", ResourceKeys: []string{"additional-resource"}})
	if err != nil || !reflect.DeepEqual(claim.ResourceKeys, []string{"additional-resource", "target:required-target"}) {
		t.Fatalf("caller omitted persisted resource scope: keys=%v err=%v", claim.ResourceKeys, err)
	}
}

func TestActionLeaseRenewalRejectsChangedResourceSet(t *testing.T) {
	for _, fault := range []string{
		"DELETE FROM action_resource_leases",
		"UPDATE action_resource_leases SET owner='different-owner'",
		"UPDATE action_resource_leases SET lease_until=lease_until+1",
		"UPDATE action_resource_leases SET resource_key='substituted-resource'",
	} {
		t.Run(fault, func(t *testing.T) {
			control, claim := admittedBoundaryAction(t)
			before := control.Writer()
			// Deliberate Go-local corruption, not a provider effect or crash fixture.
			if _, err := control.db.Exec(fault); err != nil {
				t.Fatal(err)
			}
			resources, err := control.GetResourceLeases(claim.Lease.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			control.now = func() int64 { return 1001 }
			if _, err := control.RenewActionLease(renewalFor(claim.Lease)); !errors.Is(err, ErrActionLeaseStale) {
				t.Fatalf("corrupt reservations were renewed: %v", err)
			}
			assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
			after, err := control.GetResourceLeases(claim.Lease.ActionID)
			if err != nil || !reflect.DeepEqual(after, resources) {
				t.Fatalf("rejection mutated resources: %v", err)
			}
		})
	}
}

func TestActionLeaseDeadlineMustRemainLiveAtCommit(t *testing.T) {
	for _, operation := range []string{"renew", "settle"} {
		t.Run(operation, func(t *testing.T) {
			control, claim := admittedBoundaryAction(t)
			before := control.Writer()
			calls := 0
			deadline := int64(1010)
			if operation == "renew" {
				deadline = 1021
			}
			control.now = func() int64 {
				calls++
				if calls == 1 {
					return 1001
				}
				return deadline
			}
			var err error
			if operation == "renew" {
				_, err = control.RenewActionLease(renewalFor(claim.Lease))
			} else {
				_, err = control.FailAction(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil)
			}
			if !errors.Is(err, ErrActionLeaseExpired) {
				t.Fatalf("action lease expired before commit: %v", err)
			}
			assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
		})
	}
}

func TestActionAdmissionCannotCommitAnExpiredLease(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	if err := control.Put(Record{Domain: "action", ID: "expire-during-claim", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	before := control.Writer()
	calls := 0
	control.now = func() int64 {
		calls++
		if calls == 1 {
			return 1000
		}
		return 1001
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "expire-during-claim", LeaseSeconds: 1}); !errors.Is(err, ErrActionLeaseExpired) {
		t.Fatalf("expired claim committed: %v", err)
	}
	if _, exists, err := control.GetActionLease("expire-during-claim"); err != nil || exists {
		t.Fatalf("claim survived rollback: %v", err)
	}
	assertControlTransactionUnchanged(t, control, before, 1)
}

func boundaryDispatch(claim AdmissionResult) (Record, StorageDispatchIntent) {
	record := claim.Record
	record.Revision++
	record.State = "EXECUTING"
	intent := dispatchIntent()
	intent.ActionID, intent.ExecutionEpoch = record.ID, record.ExecutionEpoch
	return record, intent
}

func TestAnonymousStorageDispatchCannotBypassActionClaim(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	before := control.Writer()
	record, intent := boundaryDispatch(claim)
	if err := control.ClaimStorageDispatch(record, intent); !errors.Is(err, ErrActionLeaseRequired) {
		t.Fatalf("anonymous dispatch bypassed claim token: %v", err)
	}
	assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
	if _, exists, err := control.GetStorageDispatch(record.ID, record.ExecutionEpoch); err != nil || exists {
		t.Fatalf("rejected dispatch persisted intent: exists=%v err=%v", exists, err)
	}
}

func TestTakeoverCannotAdoptMissingOrCorruptLease(t *testing.T) {
	for _, fault := range []string{
		"DELETE FROM action_leases",
		"UPDATE action_leases SET epoch=5",
		"DELETE FROM action_resource_leases",
	} {
		t.Run(fault, func(t *testing.T) {
			control, claim := admittedBoundaryAction(t)
			if _, err := control.db.Exec(fault); err != nil {
				t.Fatal(err)
			}
			before := control.Writer()
			control.now = func() int64 { return 1011 }
			if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Record.ID}); err == nil {
				t.Fatal("takeover silently repaired missing or corrupt authority")
			}
			assertControlTransactionUnchanged(t, control, before, 2)
		})
	}
}

func TestTakeoverAtDeadlineRetainsExactResources(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	if err := control.Put(Record{Domain: "action", ID: "extra-scope", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	first, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "extra-scope", LeaseSeconds: 10, ResourceKeys: []string{"custom-reservation"}})
	if err != nil {
		t.Fatal(err)
	}
	control.now = func() int64 { return 1010 }
	second, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "extra-scope", Owner: "successor", LeaseSeconds: 20})
	if err != nil {
		t.Fatal(err)
	}
	if second.Lease.Epoch != first.Lease.Epoch+1 || second.Record.State != "RECONCILING" || !reflect.DeepEqual(first.ResourceKeys, second.ResourceKeys) {
		t.Fatal("takeover lost scope or did not advance the frozen action fence")
	}
	got, exists, err := control.GetActionLease("extra-scope")
	if err != nil || !exists || got != second.Lease {
		t.Fatal("returned lease differs from persisted takeover")
	}
	if _, err := control.RenewActionLease(renewalFor(second.Lease)); err != nil {
		t.Fatalf("takeover resources cannot renew together: %v", err)
	}
}

func TestLeasedDispatchFencesAndCommitBoundary(t *testing.T) {
	for _, mode := range []string{"success", "empty token", "wrong token", "missing resource", "expired", "expires before commit"} {
		t.Run(mode, func(t *testing.T) {
			control, claim := admittedBoundaryAction(t)
			before := control.Writer()
			record, intent := boundaryDispatch(claim)
			token := claim.Lease.ClaimToken
			var want error
			switch mode {
			case "empty token":
				token, want = "", ErrInvalidClaimToken
			case "wrong token":
				token, want = "not-the-claim-token", ErrInvalidClaimToken
			case "missing resource":
				if _, err := control.db.Exec("DELETE FROM action_resource_leases"); err != nil {
					t.Fatal(err)
				}
				want = ErrActionLeaseStale
			case "expired":
				control.now = func() int64 { return 1010 }
				want = ErrActionLeaseExpired
			case "expires before commit":
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
			err := control.ClaimLeasedStorageDispatch(record, intent, token)
			if !errors.Is(err, want) {
				t.Fatalf("dispatch result=%v want=%v", err, want)
			}
			_, exists, readErr := control.GetStorageDispatch(record.ID, record.ExecutionEpoch)
			if readErr != nil || exists != (want == nil) {
				t.Fatalf("unexpected intent persistence: exists=%v err=%v", exists, readErr)
			}
			if want != nil {
				assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
			}
		})
	}
}

func TestDefaultAdmissionCannotDisableBudgets(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	for _, id := range []string{"default-1", "default-2", "default-3", "default-4"} {
		if err := control.Put(Record{Domain: "action", ID: id, Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
			t.Fatal(err)
		}
		_, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: id})
		if id == "default-4" {
			if !errors.Is(err, ErrBudgetExceeded) {
				t.Fatalf("omitted policy disabled budgets: %v", err)
			}
		} else if err != nil {
			t.Fatal(err)
		}
	}
}

func TestCorruptActivePeerCannotBeIgnoredByAdmission(t *testing.T) {
	control, _ := admittedBoundaryAction(t)
	if _, err := control.db.Exec(`UPDATE action_journal SET payload_json='"not an object"'`); err != nil {
		t.Fatal(err)
	}
	if err := control.Put(Record{Domain: "action", ID: "after-corruption", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	before := control.Writer()
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "after-corruption", Policy: AdmissionPolicy{MaxConcurrentActions: 10}}); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt active peer was ignored: %v", err)
	}
	assertControlTransactionUnchanged(t, control, before, 3)
}

func TestRenewalRejectsCorruptActionPayload(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	before := control.Writer()
	if _, err := control.db.Exec(`UPDATE action_journal SET payload_json='{}'`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.RenewActionLease(renewalFor(claim.Lease)); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("renewal trusted a corrupt journal: %v", err)
	}
	assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
}

func TestRenewalCannotResurrectClaimAtPriorDeadline(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	before := control.Writer()
	calls := 0
	control.now = func() int64 {
		calls++
		if calls == 1 {
			return 1009
		}
		return 1010
	}
	if _, err := control.RenewActionLease(renewalFor(claim.Lease)); !errors.Is(err, ErrActionLeaseExpired) {
		t.Fatalf("renewal committed after prior claim expired: %v", err)
	}
	assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
}

func TestLeasedDispatchCannotRewriteAdmittedScope(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	before := control.Writer()
	record, intent := boundaryDispatch(claim)
	record.Payload = []byte(`{"parameters":{"targetId":"different-target"}}`)
	if err := control.ClaimLeasedStorageDispatch(record, intent, claim.Lease.ClaimToken); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("dispatch rewrote admitted scope: %v", err)
	}
	assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
}

func TestSettlementCannotReleaseChangedResources(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	before := control.Writer()
	if _, err := control.db.Exec("UPDATE action_resource_leases SET owner='different-owner'"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.FailAction(claim.Record.ID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("settlement released another owner's reservation: %v", err)
	}
	assertBoundaryLeaseUnchanged(t, control, claim.Lease, before)
}

func TestActionClaimManifestCannotBeReplaced(t *testing.T) {
	control, claim := admittedBoundaryAction(t)
	for _, statement := range []string{
		`INSERT OR REPLACE INTO action_lease_events SELECT event_id, action_id, event_type, owner, epoch, claim_token,
		lease_until, claim_revision, writer_fencing_token, recorded_at, '[]' FROM action_lease_events`,
		`INSERT OR REPLACE INTO action_lease_events SELECT event_id+1, action_id, event_type, owner, epoch, claim_token,
		lease_until, claim_revision, writer_fencing_token, recorded_at, '[]' FROM action_lease_events`,
	} {
		if _, err := control.db.Exec(statement); err == nil {
			t.Fatal("immutable claim manifest replaced")
		}
	}
	if _, err := control.RenewActionLease(renewalFor(claim.Lease)); err != nil {
		t.Fatal(err)
	}
}

func TestAdmissionReservesEveryTargetAlias(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	if err := control.Put(Record{Domain: "action", ID: "target-aliases", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: []byte(`{"target":"root","parameters":{"source":"source-alias","sourceTargetId":"source-id","destination":"destination-alias","target":"target-alias","targetId":"target-id"}}`)}); err != nil {
		t.Fatal(err)
	}
	claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "target-aliases"})
	want := []string{"target:destination-alias", "target:root", "target:source-alias", "target:source-id", "target:target-alias", "target:target-id"}
	if err != nil || !reflect.DeepEqual(claim.ResourceKeys, want) {
		t.Fatalf("persisted target aliases escaped reservations: keys=%v err=%v", claim.ResourceKeys, err)
	}
}

func TestAdmissionRejectsUnqualifiedActiveHistory(t *testing.T) {
	for _, fault := range []string{"legacy", "lease epoch mismatch", "resource missing"} {
		t.Run(fault, func(t *testing.T) {
			control := openControlAt(t, 1000)
			defer control.Close()
			for _, id := range []string{"active-peer", "candidate"} {
				if err := control.Put(Record{Domain: "action", ID: id, Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
					t.Fatal(err)
				}
			}
			if fault == "legacy" {
				if err := control.Put(Record{Domain: "action", ID: "active-peer", Revision: 2, ExecutionEpoch: 1, State: "CLAIMED"}); err != nil {
					t.Fatal(err)
				}
			} else {
				if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "active-peer", ResourceKeys: []string{"peer-resource"}}); err != nil {
					t.Fatal(err)
				}
				statement := "UPDATE action_leases SET epoch=2"
				if fault == "resource missing" {
					statement = "DELETE FROM action_resource_leases"
				}
				if _, err := control.db.Exec(statement); err != nil {
					t.Fatal(err)
				}
			}
			writer := control.Writer()
			if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "candidate"}); err == nil {
				t.Fatal("unqualified active history permitted admission")
			}
			assertControlTransactionUnchanged(t, control, writer, 3)
		})
	}
}

func TestTakeoverScopeAndEpochBounds(t *testing.T) {
	t.Run("scope expansion", func(t *testing.T) {
		control, claim := admittedBoundaryAction(t)
		writer := control.Writer()
		control.now = func() int64 { return 1010 }
		if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Record.ID, ResourceKeys: []string{"new-resource"}}); !errors.Is(err, ErrActionLeaseStale) {
			t.Fatalf("takeover changed scope: %v", err)
		}
		assertBoundaryLeaseUnchanged(t, control, claim.Lease, writer)
	})
	t.Run("epoch overflow", func(t *testing.T) {
		control := openControlAt(t, 1000)
		defer control.Close()
		if err := control.Put(Record{Domain: "action", ID: "epoch-limit", Revision: 1, ExecutionEpoch: math.MaxInt64, State: "PENDING"}); err != nil {
			t.Fatal(err)
		}
		claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "epoch-limit", LeaseSeconds: 10})
		if err != nil {
			t.Fatal(err)
		}
		writer := control.Writer()
		control.now = func() int64 { return 1010 }
		if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "epoch-limit"}); !errors.Is(err, ErrEpochOutOfRange) {
			t.Fatalf("epoch overflow accepted: %v", err)
		}
		assertBoundaryLeaseUnchanged(t, control, claim.Lease, writer)
	})
}

func TestAdmissionSchemaGuardRejectsEveryLeaseMutation(t *testing.T) {
	for _, operation := range []string{"admit", "renew", "settle"} {
		t.Run(operation, func(t *testing.T) {
			control, claim := admittedBoundaryAction(t)
			writer := control.Writer()
			if _, err := control.db.Exec("DROP TRIGGER action_lease_events_no_replace"); err != nil {
				t.Fatal(err)
			}
			var err error
			switch operation {
			case "admit":
				_, err = control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Record.ID})
			case "renew":
				_, err = control.RenewActionLease(renewalFor(claim.Lease))
			case "settle":
				_, err = control.FailAction(claim.Record.ID, claim.Lease.Epoch, claim.Lease.ClaimToken, nil)
			}
			if !errors.Is(err, ErrForeignRuntimeStore) {
				t.Fatalf("changed schema allowed mutation: %v", err)
			}
			assertBoundaryLeaseUnchanged(t, control, claim.Lease, writer)
		})
	}
}
