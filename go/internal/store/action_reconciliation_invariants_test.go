package store

import (
	"errors"
	"testing"
)

func TestReconcilingInvariantTakeoverNeverMintsWriteIdentity(t *testing.T) {
	control, originalClaim := executingLeasedAction(t)
	original, bound, err := control.GetStorageDispatch(originalClaim.Lease.ActionID, originalClaim.Lease.Epoch)
	if err != nil || !bound {
		t.Fatalf("missing original dispatch: bound=%v err=%v", bound, err)
	}
	resources, err := control.GetResourceLeases(originalClaim.Lease.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	claim := originalClaim
	for i := 0; i < 3; i++ {
		now := claim.Lease.LeaseUntil + 1
		control.now = func() int64 { return now }
		next, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID, Owner: "successor", LeaseSeconds: 10})
		if err != nil {
			t.Fatal(err)
		}
		if next.Record.State != "RECONCILING" || next.Lease.Epoch != claim.Lease.Epoch+1 || next.Lease.ClaimToken == claim.Lease.ClaimToken {
			t.Fatalf("takeover %d minted a new claim identity incorrectly: state=%s epoch=%d", i, next.Record.State, next.Lease.Epoch)
		}
		if next.Record.ExecutionEpoch == original.Intent.ExecutionEpoch {
			t.Fatal("current claim epoch collapsed onto original dispatch epoch")
		}
		got, bound, err := control.GetLeasedStorageDispatch(next.Lease.ActionID, next.Lease.Epoch, next.Lease.ClaimToken)
		if err != nil || !bound || got != original || got.Intent.ExecutionEpoch != original.Intent.ExecutionEpoch || got.Intent.OperationID != original.Intent.OperationID {
			t.Fatalf("takeover %d lost original write identity: bound=%v err=%v", i, bound, err)
		}
		if _, bound, err := control.GetStorageDispatch(next.Lease.ActionID, next.Lease.Epoch); err != nil || bound {
			t.Fatalf("current claim epoch accepted as dispatch key: bound=%v err=%v", bound, err)
		}
		if _, bound, err := control.GetLeasedStorageDispatch(next.Lease.ActionID, original.Intent.ExecutionEpoch, next.Lease.ClaimToken); !errors.Is(err, ErrActionLeaseStale) || bound {
			t.Fatalf("original dispatch epoch accepted as current claim key: bound=%v err=%v", bound, err)
		}
		claim = next
	}
	afterJournal, _, err := control.Get("action", claim.Lease.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	afterResources, err := control.GetResourceLeases(claim.Lease.ActionID)
	if err != nil || len(afterResources) != len(resources) {
		t.Fatalf("takeover released reservations: %v", err)
	}
	lookupWriter := control.Writer()
	beforeLookup := afterJournal
	got, bound, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken)
	if err != nil || !bound || got != original {
		t.Fatalf("read-only lookup lost original dispatch: bound=%v err=%v", bound, err)
	}
	afterLookup, _, err := control.Get("action", claim.Lease.ActionID)
	if err != nil || !sameControlRecord(beforeLookup, afterLookup) || lookupWriter != control.Writer() {
		t.Fatal("read-only lookup mutated journal or writer")
	}
	if _, err := control.RenewActionLease(renewalFor(originalClaim.Lease)); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expired predecessor renewed: %v", err)
	}
	control.now = func() int64 { return claim.Lease.LeaseUntil }
	if _, err := control.RenewActionLease(renewalFor(claim.Lease)); !errors.Is(err, ErrActionLeaseExpired) {
		t.Fatalf("expired current lease renewed: %v", err)
	}
}

func TestReconcilingInvariantBudgetLocksAndUnknownRetention(t *testing.T) {
	control, claim := executingLeasedAction(t)
	now := claim.Lease.LeaseUntil + 1
	control.now = func() int64 { return now }
	claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID, LeaseSeconds: 10})
	if err != nil || claim.Record.State != "RECONCILING" {
		t.Fatalf("takeover: %v", err)
	}
	if err := control.Put(Record{Domain: "action", ID: "peer-action", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "peer-action", Policy: AdmissionPolicy{MaxConcurrentActions: 1}}); !errors.Is(err, ErrBudgetExceeded) {
		t.Fatalf("RECONCILING dropped out of budget: %v", err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "peer-action", ResourceKeys: claim.ResourceKeys}); !errors.Is(err, ErrResourceConflict) {
		t.Fatalf("RECONCILING reservations stolen: %v", err)
	}
	unknown, err := control.MarkActionEffectUnknown(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken)
	if err != nil || unknown.State != "EFFECT_UNKNOWN" || unknown.ExecutionEpoch != claim.Lease.Epoch {
		t.Fatalf("uncertainty changed epoch: %+v %v", unknown, err)
	}
	locks, err := control.GetResourceLeases(claim.Lease.ActionID)
	if err != nil || len(locks) != len(claim.ResourceKeys) {
		t.Fatalf("EFFECT_UNKNOWN released reservations: count=%d err=%v", len(locks), err)
	}
	lease, exists, err := control.GetActionLease(claim.Lease.ActionID)
	if err != nil || !exists || lease.ClaimToken != claim.Lease.ClaimToken {
		t.Fatalf("EFFECT_UNKNOWN dropped live claim: %v", err)
	}
}

func TestReconcilingInvariantPredecessorWalkFailsClosed(t *testing.T) {
	for _, fault := range []string{"admitted missing", "admitted became takeover", "admitted points at executing", "admitted claim_revision past predecessor"} {
		t.Run(fault, func(t *testing.T) {
			control, claim := executingLeasedAction(t)
			originalEpoch := claim.Lease.Epoch
			now := claim.Lease.LeaseUntil + 1
			control.now = func() int64 { return now }
			claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID, LeaseSeconds: 10})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec("DROP TRIGGER action_lease_events_no_update"); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec("DROP TRIGGER action_lease_events_no_delete"); err != nil {
				t.Fatal(err)
			}
			switch fault {
			case "admitted missing":
				if _, err := control.db.Exec("DELETE FROM action_lease_events WHERE event_type='ADMITTED'"); err != nil {
					t.Fatal(err)
				}
			case "admitted became takeover":
				if _, err := control.db.Exec("UPDATE action_lease_events SET event_type='TAKEOVER' WHERE event_type='ADMITTED'"); err != nil {
					t.Fatal(err)
				}
			case "admitted points at executing":
				if _, err := control.db.Exec("UPDATE action_lease_events SET claim_revision=3 WHERE event_type='ADMITTED'"); err != nil {
					t.Fatal(err)
				}
			case "admitted claim_revision past predecessor":
				if _, err := control.db.Exec("UPDATE action_lease_events SET claim_revision=99 WHERE event_type='ADMITTED'"); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := control.db.Exec(actionAdmissionSchemaObjects["action_lease_events_no_update"]); err != nil {
				t.Fatal(err)
			}
			if _, err := control.db.Exec(actionAdmissionSchemaObjects["action_lease_events_no_delete"]); err != nil {
				t.Fatal(err)
			}
			writer := control.Writer()
			before, _, err := control.Get("action", claim.Lease.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			if _, bound, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken); !errors.Is(err, ErrActionLeaseStale) || bound {
				t.Fatalf("broken predecessor recovered: bound=%v err=%v", bound, err)
			}
			after, _, err := control.Get("action", claim.Lease.ActionID)
			if err != nil || !sameControlRecord(before, after) || writer != control.Writer() {
				t.Fatal("failed walk mutated state")
			}
			if _, bound, err := control.GetStorageDispatch(claim.Lease.ActionID, originalEpoch); err != nil || !bound {
				t.Fatalf("fail-closed walk deleted original dispatch: bound=%v err=%v", bound, err)
			}
		})
	}
}

func TestReconcilingInvariantLookupRejectsLostJournalWriterAndClaimClock(t *testing.T) {
	for _, fault := range []string{"missing journal", "acquired_at mismatch", "corrupt current takeover", "writer lost during read", "schema objects"} {
		t.Run(fault, func(t *testing.T) {
			control := openControlAt(t, 1000)
			t.Cleanup(func() { _ = control.Close() })
			if err := control.Put(Record{Domain: "action", ID: "lookup-fault", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
				Payload: []byte(`{"parameters":{"targetId":"lookup-target"}}`)}); err != nil {
				t.Fatal(err)
			}
			claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "lookup-fault", Owner: "action-owner", LeaseSeconds: 20})
			if err != nil {
				t.Fatal(err)
			}
			record, intent := boundaryDispatch(claim)
			if err := control.ClaimLeasedStorageDispatch(record, intent, claim.Lease.ClaimToken); err != nil {
				t.Fatal(err)
			}
			takeoverAt := claim.Lease.LeaseUntil + 1
			control.now = func() int64 { return takeoverAt }
			claim, err = control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID, Owner: "successor", LeaseSeconds: 60})
			if err != nil {
				t.Fatal(err)
			}
			want := ErrActionLeaseStale
			switch fault {
			case "missing journal":
				if _, err := control.db.Exec("DELETE FROM action_journal WHERE id=?", claim.Lease.ActionID); err != nil {
					t.Fatal(err)
				}
				want = ErrCorruptRecord
			case "acquired_at mismatch":
				if _, err := control.db.Exec("UPDATE action_leases SET acquired_at=acquired_at-1 WHERE action_id=?", claim.Lease.ActionID); err != nil {
					t.Fatal(err)
				}
			case "corrupt current takeover":
				if _, err := control.db.Exec("DROP TRIGGER action_lease_events_no_update"); err != nil {
					t.Fatal(err)
				}
				if _, err := control.db.Exec("UPDATE action_lease_events SET recorded_at=999 WHERE event_type='TAKEOVER'"); err != nil {
					t.Fatal(err)
				}
				if _, err := control.db.Exec(actionAdmissionSchemaObjects["action_lease_events_no_update"]); err != nil {
					t.Fatal(err)
				}
			case "writer lost during read":
				deadline := control.Writer().LeaseUntil
				calls := 0
				control.now = func() int64 {
					calls++
					if calls == 1 {
						return takeoverAt
					}
					return deadline
				}
				want = ErrWriterFenceHeld
			case "schema objects":
				if _, err := control.db.Exec("DROP TRIGGER action_reconciliation_boundary_no_replace"); err != nil {
					t.Fatal(err)
				}
				want = ErrForeignRuntimeStore
			}
			writer := control.Writer()
			if _, bound, err := control.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken); !errors.Is(err, want) || bound {
				t.Fatalf("unsafe lookup: bound=%v err=%v want=%v", bound, err, want)
			}
			if writer != control.Writer() {
				t.Fatal("failed lookup renewed writer")
			}
		})
	}
}

func TestReconcilingInvariantSameEpochSettlementDoesNotRedispatch(t *testing.T) {
	control, claim := executingLeasedAction(t)
	original, _, err := control.GetStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch)
	if err != nil {
		t.Fatal(err)
	}
	now := claim.Lease.LeaseUntil + 1
	control.now = func() int64 { return now }
	claim, err = control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	record, err := control.CompleteAction(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken, []byte(`{"reconciled":true}`))
	if err != nil || record.State != "SUCCEEDED" || record.ExecutionEpoch != claim.Lease.Epoch {
		t.Fatalf("same-epoch success settlement: %+v %v", record, err)
	}
	got, bound, err := control.GetStorageDispatch(claim.Lease.ActionID, original.Intent.ExecutionEpoch)
	if err != nil || !bound || got != original {
		t.Fatalf("settlement rebound dispatch: bound=%v err=%v", bound, err)
	}
	if _, bound, err := control.GetStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch); err != nil || bound {
		t.Fatalf("settlement claimed current epoch: bound=%v err=%v", bound, err)
	}
	locks, err := control.GetResourceLeases(claim.Lease.ActionID)
	if err != nil || len(locks) != 0 {
		t.Fatalf("terminal success kept reservations: %v", err)
	}
}

func TestReconcilingInvariantV5IllegalEdgeStaysIllegal(t *testing.T) {
	control, claim := executingLeasedAction(t)
	control.now = func() int64 { return 1011 }
	claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: claim.Lease.ActionID})
	if err != nil {
		t.Fatal(err)
	}
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
		t.Fatalf("upgrade bleached illegal v5 history: %v", err)
	}
	if LegalTransition("action", "EXECUTING", "RECONCILING") {
		t.Fatal("generic graph expanded")
	}
}

func TestReconcilingInvariantMigrateToV6TxFailsClosed(t *testing.T) {
	for _, fault := range []string{"duplicate v6 meta table", "duplicate schema migration"} {
		t.Run(fault, func(t *testing.T) {
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
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			switch fault {
			case "duplicate v6 meta table":
				if _, err := tx.Exec("CREATE TABLE control_store_meta_v6 (singleton INTEGER PRIMARY KEY)"); err != nil {
					t.Fatal(err)
				}
			case "duplicate schema migration":
				if _, err := tx.Exec("INSERT INTO schema_migrations VALUES(6,1,'injected')"); err != nil {
					t.Fatal(err)
				}
			}
			if err := control.migrateToV6Tx(tx); err == nil {
				_ = tx.Commit()
				t.Fatal("v6 migration committed over a conflicting catalog")
			}
			if err := tx.Rollback(); err != nil {
				t.Fatal(err)
			}
			if control.schema != SchemaV5 {
				t.Fatalf("failed v6 migration mutated in-memory schema: %d", control.schema)
			}
			var version int
			if err := control.db.QueryRow("PRAGMA user_version").Scan(&version); err != nil || version != SchemaV5 {
				t.Fatalf("failed v6 migration persisted schema %d: %v", version, err)
			}
			var operation string
			var epoch int64
			if err := control.db.QueryRow("SELECT operation_id, execution_epoch FROM storage_dispatches WHERE action_id=?", claim.Lease.ActionID).Scan(&operation, &epoch); err != nil || operation != original.Intent.OperationID || uint64(epoch) != original.Intent.ExecutionEpoch {
				t.Fatalf("failed v6 migration lost original dispatch: op=%s epoch=%d err=%v", operation, epoch, err)
			}
			var retained int
			if err := control.db.QueryRow("SELECT COUNT(*) FROM action_resource_leases WHERE action_id=?", claim.Lease.ActionID).Scan(&retained); err != nil || retained != len(resources) {
				t.Fatalf("failed v6 migration changed reservations: count=%d err=%v", retained, err)
			}
		})
	}
}
