package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"sync"
	"testing"
)

func TestAdmitAndClaimActionSuccess(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	// Seed an action in PENDING state
	pending := Record{
		Domain:         "action",
		ID:             "action-1",
		Revision:       1,
		ExecutionEpoch: 1,
		State:          "PENDING",
		Payload:        json.RawMessage(`{"parameters":{"policyId":"pol-1","destTargetId":"tgt-1"}}`),
	}
	if err := control.Put(pending); err != nil {
		t.Fatalf("failed to seed pending action: %v", err)
	}

	req := AdmissionRequest{
		ActionID:     "action-1",
		Owner:        "worker-alpha",
		LeaseSeconds: 60,
		ResourceKeys: []string{"target:tgt-1", "policy:pol-1"},
	}

	result, err := control.AdmitAndClaimAction(req)
	if err != nil {
		t.Fatalf("AdmitAndClaimAction failed: %v", err)
	}

	if result.Lease.ActionID != "action-1" {
		t.Fatalf("unexpected lease action id: %s", result.Lease.ActionID)
	}
	if result.Lease.Owner != "worker-alpha" {
		t.Fatalf("unexpected lease owner: %s", result.Lease.Owner)
	}
	if result.Lease.Epoch != 1 {
		t.Fatalf("unexpected lease epoch: %d", result.Lease.Epoch)
	}
	if len(result.Lease.ClaimToken) < 16 {
		t.Fatalf("claim token too short: %s", result.Lease.ClaimToken)
	}
	if result.Lease.LeaseUntil != 1060 {
		t.Fatalf("unexpected lease_until: %d, expected 1060", result.Lease.LeaseUntil)
	}

	// Verify action_journal is now CLAIMED with revision 2
	got, exists, err := control.Get("action", "action-1")
	if err != nil || !exists {
		t.Fatalf("failed to fetch action: %v", err)
	}
	if got.State != "CLAIMED" || got.Revision != 2 || got.ExecutionEpoch != 1 {
		t.Fatalf("unexpected action state: %+v", got)
	}

	// Verify resource leases exist
	resLeases, err := control.GetResourceLeases("action-1")
	if err != nil {
		t.Fatalf("failed to fetch resource leases: %v", err)
	}
	if len(resLeases) != 2 {
		t.Fatalf("expected 2 resource leases, got %d", len(resLeases))
	}
}

func TestConcurrentClaimSameAction(t *testing.T) {
	control1 := openControlAt(t, 1000)
	defer control1.Close()

	// Seed pending action
	pending := Record{
		Domain:         "action",
		ID:             "race-action",
		Revision:       1,
		ExecutionEpoch: 1,
		State:          "PENDING",
		Payload:        json.RawMessage(`{}`),
	}
	if err := control1.Put(pending); err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	wg.Add(2)
	results := make([]error, 2)

	go func() {
		defer wg.Done()
		_, results[0] = control1.AdmitAndClaimAction(AdmissionRequest{
			ActionID:     "race-action",
			Owner:        "worker-1",
			LeaseSeconds: 60,
		})
	}()

	go func() {
		defer wg.Done()
		_, results[1] = control1.AdmitAndClaimAction(AdmissionRequest{
			ActionID:     "race-action",
			Owner:        "worker-2",
			LeaseSeconds: 60,
		})
	}()

	wg.Wait()

	successCount := 0
	for _, r := range results {
		if r == nil {
			successCount++
		}
	}
	if successCount != 1 {
		t.Fatalf("expected exactly 1 success in concurrent claim of same action, got %d (errs: %v, %v)",
			successCount, results[0], results[1])
	}

	// Verify only 1 valid lease exists in database
	lease, exists, err := control1.GetActionLease("race-action")
	if err != nil || !exists {
		t.Fatalf("expected 1 active lease: exists=%v err=%v", exists, err)
	}
	if lease.Epoch != 1 {
		t.Fatalf("unexpected epoch: %d", lease.Epoch)
	}
}

func TestResourceConflictBetweenDifferentActions(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	// Seed two pending actions
	for _, id := range []string{"act-a", "act-b"} {
		if err := control.Put(Record{
			Domain: "action", ID: id, Revision: 1, ExecutionEpoch: 1, State: "PENDING",
			Payload: json.RawMessage(`{}`),
		}); err != nil {
			t.Fatal(err)
		}
	}

	// Action A claims resource "shared-target"
	_, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-a",
		Owner:        "worker-a",
		LeaseSeconds: 60,
		ResourceKeys: []string{"shared-target"},
	})
	if err != nil {
		t.Fatalf("Action A admission failed: %v", err)
	}

	// Action B attempts to claim the same resource -> must be rejected
	_, errB := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-b",
		Owner:        "worker-b",
		LeaseSeconds: 60,
		ResourceKeys: []string{"shared-target"},
	})
	if !errors.Is(errB, ErrResourceConflict) {
		t.Fatalf("expected ErrResourceConflict for action B, got: %v", errB)
	}

	// Action B must NOT have left an action lease
	_, exists, err := control.GetActionLease("act-b")
	if err != nil {
		t.Fatal(err)
	}
	if exists {
		t.Fatal("loser action B must not leave an action lease")
	}

	// Action B in action_journal must still be PENDING
	recB, exists, err := control.Get("action", "act-b")
	if err != nil || !exists || recB.State != "PENDING" {
		t.Fatalf("action B state corrupted: %+v", recB)
	}
}

func TestMultiResourcePartialFailureRollback(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	// Pre-claim resource C by blocker action
	if err := control.Put(Record{
		Domain: "action", ID: "blocker", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}
	_, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "blocker",
		Owner:        "worker-blocker",
		LeaseSeconds: 120,
		ResourceKeys: []string{"resource-C"},
	})
	if err != nil {
		t.Fatalf("blocker claim failed: %v", err)
	}

	// Now try to admit candidate needing A, B, and C
	if err := control.Put(Record{
		Domain: "action", ID: "candidate", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}

	_, candErr := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "candidate",
		Owner:        "worker-cand",
		LeaseSeconds: 60,
		ResourceKeys: []string{"resource-A", "resource-B", "resource-C"},
	})
	if !errors.Is(candErr, ErrResourceConflict) {
		t.Fatalf("expected ErrResourceConflict, got: %v", candErr)
	}

	// Verify resource-A and resource-B were NOT left behind as orphan rows!
	for _, resKey := range []string{"resource-A", "resource-B"} {
		var holder string
		rowErr := control.db.QueryRow("SELECT action_id FROM action_resource_leases WHERE resource_key=?", resKey).Scan(&holder)
		if !errors.Is(rowErr, errors.New("sql: no rows in result set")) && !strings.Contains(fmt.Sprint(rowErr), "no rows") {
			t.Fatalf("orphan resource row found for %s: held by %s (err: %v)", resKey, holder, rowErr)
		}
	}

	// Candidate action lease must not exist
	_, leaseExists, _ := control.GetActionLease("candidate")
	if leaseExists {
		t.Fatal("candidate must not leave action lease")
	}

	// Candidate state must remain PENDING
	candRec, _, _ := control.Get("action", "candidate")
	if candRec.State != "PENDING" {
		t.Fatalf("candidate state corrupted: %+v", candRec)
	}
}

func TestBudgetRaceMaxConcurrentActions(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	policy := AdmissionPolicy{
		MaxConcurrentActions: 2,
		MaxActionsPerHour:    100,
	}

	// Seed 3 pending actions
	for _, id := range []string{"act-1", "act-2", "act-3"} {
		if err := control.Put(Record{
			Domain: "action", ID: id, Revision: 1, ExecutionEpoch: 1, State: "PENDING",
			Payload: json.RawMessage(`{}`),
		}); err != nil {
			t.Fatal(err)
		}
	}

	// Claim action 1
	_, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: "act-1", Owner: "w1", LeaseSeconds: 60, Policy: policy,
	})
	if err != nil {
		t.Fatalf("act-1 claim failed: %v", err)
	}

	// Now act-2 and act-3 race for the 2nd and final slot
	var wg sync.WaitGroup
	wg.Add(2)
	errs := make([]error, 2)
	actions := []string{"act-2", "act-3"}

	for i := 0; i < 2; i++ {
		idx := i
		go func() {
			defer wg.Done()
			_, errs[idx] = control.AdmitAndClaimAction(AdmissionRequest{
				ActionID: actions[idx], Owner: fmt.Sprintf("w-%d", idx), LeaseSeconds: 60, Policy: policy,
			})
		}()
	}
	wg.Wait()

	successes := 0
	budgetRejections := 0
	for _, e := range errs {
		if e == nil {
			successes++
		} else if errors.Is(e, ErrBudgetExceeded) {
			budgetRejections++
		}
	}

	if successes != 1 || budgetRejections != 1 {
		t.Fatalf("expected 1 success and 1 budget rejection in race for last slot, got %d successes and %d rejections (errs: %v)",
			successes, budgetRejections, errs)
	}

	// Verify count of active actions in database is exactly 2
	var activeCount int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM action_journal WHERE state IN ('CLAIMED', 'EXECUTING', 'EFFECT_UNKNOWN')").Scan(&activeCount); err != nil {
		t.Fatal(err)
	}
	if activeCount != 2 {
		t.Fatalf("expected active count = 2, got %d", activeCount)
	}
}

func TestWriterLeaseCommitFenceRejectsExpiredWriterDuringAdmission(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	if err := control.Put(Record{
		Domain: "action", ID: "act-fence", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}

	// Mock clock so writer expires before commit
	calls := 0
	control.now = func() int64 {
		calls++
		if calls == 1 {
			return 1001 // assertWriterTx sees valid writer
		}
		return 1035 // commitNow is past writer lease deadline (1030)
	}

	_, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-fence",
		Owner:        "worker-fence",
		LeaseSeconds: 60,
		ResourceKeys: []string{"res-fence"},
	})
	if !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld on writer expiry at commit, got: %v", err)
	}

	// Must roll back everything:
	// 1. Action lease must NOT exist
	_, leaseExists, _ := control.GetActionLease("act-fence")
	if leaseExists {
		t.Fatal("action lease must not exist after rollback")
	}

	// 2. Resource reservation must NOT exist
	var resCount int
	_ = control.db.QueryRow("SELECT COUNT(*) FROM action_resource_leases WHERE resource_key='res-fence'").Scan(&resCount)
	if resCount != 0 {
		t.Fatal("resource lease must not exist after rollback")
	}

	// 3. Action record must still be PENDING
	rec, _, _ := control.Get("action", "act-fence")
	if rec.State != "PENDING" {
		t.Fatalf("action state must remain PENDING: %+v", rec)
	}
}

func TestTakeoverExpiryAndStaleEpochFencing(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()
	control.leaseSeconds = 300
	if err := control.RenewWriter(context.Background()); err != nil {
		t.Fatal(err)
	}

	if err := control.Put(Record{
		Domain: "action", ID: "act-takeover", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}

	// Worker A claims action at t=1000 with 60s lease (expires at 1060)
	admitA, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-takeover",
		Owner:        "worker-A",
		LeaseSeconds: 60,
		ResourceKeys: []string{"res-shared"},
	})
	if err != nil {
		t.Fatalf("worker A admission failed: %v", err)
	}
	if admitA.Lease.Epoch != 1 {
		t.Fatalf("expected epoch 1, got %d", admitA.Lease.Epoch)
	}
	tokenA := admitA.Lease.ClaimToken

	// 1. Worker B attempts takeover at t=1030 (before lease expires at 1060) -> REJECTED!
	control.now = func() int64 { return 1030 }
	_, errEarly := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-takeover",
		Owner:        "worker-B",
		LeaseSeconds: 60,
	})
	if !errors.Is(errEarly, ErrActionLeaseActive) {
		t.Fatalf("expected ErrActionLeaseActive for unexpired takeover, got: %v", errEarly)
	}

	// 2. Advance time past expiry: t=1070. Worker B takes over -> ALLOWED!
	control.now = func() int64 { return 1070 }
	admitB, errTakeover := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-takeover",
		Owner:        "worker-B",
		LeaseSeconds: 60,
		ResourceKeys: []string{"res-shared"},
	})
	if errTakeover != nil {
		t.Fatalf("takeover after expiry failed: %v", errTakeover)
	}
	if admitB.Lease.Epoch != 2 {
		t.Fatalf("expected epoch 2 after takeover, got %d", admitB.Lease.Epoch)
	}
	bResLeases, err := control.GetResourceLeases("act-takeover")
	if err != nil || len(bResLeases) != 1 || bResLeases[0].Epoch != 2 || bResLeases[0].Owner != "worker-B" {
		t.Fatalf("takeover resource lease rebind failed: %+v %v", bResLeases, err)
	}
	if admitB.Lease.ClaimToken == tokenA {
		t.Fatalf("claim token must change on takeover")
	}
	tokenB := admitB.Lease.ClaimToken

	// 3. Stale Worker A wakes up and attempts renew with old epoch 1 and tokenA -> REJECTED!
	_, errStaleRenew := control.RenewActionLease(ActionLeaseRenewal{
		ActionID:   "act-takeover",
		Epoch:      1,
		ClaimToken: tokenA,
	})
	if !errors.Is(errStaleRenew, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for stale epoch renew, got: %v", errStaleRenew)
	}

	// 4. Stale Worker A attempts CompleteAction with old epoch 1 -> REJECTED!
	_, errStaleComplete := control.CompleteAction("act-takeover", 1, tokenA, json.RawMessage(`{}`))
	if !errors.Is(errStaleComplete, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for stale complete, got: %v", errStaleComplete)
	}

	// 5. Stale Worker A attempts FailAction with old epoch 1 -> REJECTED!
	_, errStaleFail := control.FailAction("act-takeover", 1, tokenA, json.RawMessage(`{}`))
	if !errors.Is(errStaleFail, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for stale fail, got: %v", errStaleFail)
	}

	// 6. Test wrong claim token with correct epoch 2 -> REJECTED!
	_, errWrongToken := control.RenewActionLease(ActionLeaseRenewal{
		ActionID:   "act-takeover",
		Epoch:      2,
		ClaimToken: "wrong-token-value-12345",
	})
	if !errors.Is(errWrongToken, ErrInvalidClaimToken) {
		t.Fatalf("expected ErrInvalidClaimToken, got: %v", errWrongToken)
	}

	// 7. Legitimate Worker B completes action with epoch 2 and tokenB -> SUCCEEDS!
	completed, errValidComplete := control.CompleteAction("act-takeover", 2, tokenB, json.RawMessage(`{"result":"ok"}`))
	if errValidComplete != nil {
		t.Fatalf("valid complete failed: %v", errValidComplete)
	}
	if completed.State != "SUCCEEDED" {
		t.Fatalf("expected state SUCCEEDED, got %s", completed.State)
	}

	// 8. Completed action releases resource leases
	resLeases, err := control.GetResourceLeases("act-takeover")
	if err != nil || len(resLeases) != 0 {
		t.Fatalf("resource leases must be released on terminal complete: count=%d err=%v", len(resLeases), err)
	}
}

func TestDirectPutBypassRejection(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	if err := control.Put(Record{
		Domain: "action", ID: "act-bypass", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}

	// Admitting action binds it to an action lease
	admit, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: "act-bypass", Owner: "legit-worker", LeaseSeconds: 60,
	})
	if err != nil {
		t.Fatal(err)
	}

	// Attempting to bypass the action lease via generic Put must fail!
	bypassRecord := Record{
		Domain:         "action",
		ID:             "act-bypass",
		Revision:       admit.Record.Revision + 1,
		ExecutionEpoch: admit.Lease.Epoch,
		State:          "SUCCEEDED",
		Payload:        json.RawMessage(`{"bypass":true}`),
	}
	errBypass := control.Put(bypassRecord)
	if !errors.Is(errBypass, ErrActionLeaseRequired) {
		t.Fatalf("expected ErrActionLeaseRequired on direct Put bypass, got: %v", errBypass)
	}
}

func TestRenewActionLeaseComprehensive(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	if err := control.Put(Record{
		Domain: "action", ID: "act-renew", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}

	admit, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-renew",
		Owner:        "worker-1",
		LeaseSeconds: 60,
		ResourceKeys: []string{"res-1", "res-2"},
	})
	if err != nil {
		t.Fatal(err)
	}

	// 1. Successful renewal
	control.now = func() int64 { return 1020 }
	renewed, err := control.RenewActionLease(ActionLeaseRenewal{
		ActionID:     "act-renew",
		Epoch:        1,
		ClaimToken:   admit.Lease.ClaimToken,
		Owner:        "worker-1",
		LeaseSeconds: 120,
	})
	if err != nil {
		t.Fatalf("renewal failed: %v", err)
	}
	if renewed.LeaseUntil != 1140 {
		t.Fatalf("unexpected renewed lease until: %d, expected 1140", renewed.LeaseUntil)
	}

	// Verify all resource leases were extended
	resLeases, err := control.GetResourceLeases("act-renew")
	if err != nil || len(resLeases) != 2 {
		t.Fatalf("expected 2 resource leases, got: %v %v", len(resLeases), err)
	}
	for _, rl := range resLeases {
		if rl.LeaseUntil != 1140 {
			t.Fatalf("resource lease %s not extended: %d", rl.ResourceKey, rl.LeaseUntil)
		}
	}

	// 2. Renewal with invalid inputs
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: ""}); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-renew", Epoch: 0}); !errors.Is(err, ErrEpochOutOfRange) {
		t.Fatalf("expected ErrEpochOutOfRange, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-renew", Epoch: 1, ClaimToken: ""}); !errors.Is(err, ErrInvalidClaimToken) {
		t.Fatalf("expected ErrInvalidClaimToken, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "nonexistent", Epoch: 1, ClaimToken: "token"}); !errors.Is(err, ErrActionLeaseNotFound) {
		t.Fatalf("expected ErrActionLeaseNotFound, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-renew", Epoch: 99, ClaimToken: admit.Lease.ClaimToken}); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale on wrong epoch, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-renew", Epoch: 1, ClaimToken: "wrong", Owner: "worker-1"}); !errors.Is(err, ErrInvalidClaimToken) {
		t.Fatalf("expected ErrInvalidClaimToken, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-renew", Epoch: 1, ClaimToken: admit.Lease.ClaimToken, Owner: "wrong-worker"}); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale on wrong owner, got: %v", err)
	}

	// 3. Renewal after expiry (with writer still valid)
	control.leaseSeconds = 2000
	_ = control.RenewWriter(context.Background())
	control.now = func() int64 { return 1200 }
	if _, err := control.RenewActionLease(ActionLeaseRenewal{
		ActionID:   "act-renew",
		Epoch:      1,
		ClaimToken: admit.Lease.ClaimToken,
	}); !errors.Is(err, ErrActionLeaseExpired) {
		t.Fatalf("expected ErrActionLeaseExpired, got: %v", err)
	}

	// 4. Renewal on closed control
	_ = control.Close()
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-renew", Epoch: 1, ClaimToken: "token"}); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld on closed, got: %v", err)
	}
}

func TestFailActionAndTerminalSettlementComprehensive(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	if err := control.Put(Record{
		Domain: "action", ID: "act-fail", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}

	admit, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-fail",
		Owner:        "worker-f",
		LeaseSeconds: 60,
		ResourceKeys: []string{"res-fail-1"},
	})
	if err != nil {
		t.Fatal(err)
	}

	// Input validations
	if _, err := control.FailAction("", 1, admit.Lease.ClaimToken, nil); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}
	if _, err := control.FailAction("act-fail", 0, admit.Lease.ClaimToken, nil); !errors.Is(err, ErrEpochOutOfRange) {
		t.Fatalf("expected ErrEpochOutOfRange, got: %v", err)
	}
	if _, err := control.FailAction("act-fail", 1, "", nil); !errors.Is(err, ErrInvalidClaimToken) {
		t.Fatalf("expected ErrInvalidClaimToken, got: %v", err)
	}
	if _, err := control.FailAction("nonexistent", 1, admit.Lease.ClaimToken, nil); !errors.Is(err, ErrActionLeaseNotFound) {
		t.Fatalf("expected ErrActionLeaseNotFound, got: %v", err)
	}
	if _, err := control.FailAction("act-fail", 99, admit.Lease.ClaimToken, nil); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale on wrong epoch, got: %v", err)
	}
	if _, err := control.FailAction("act-fail", 1, "bad-token", nil); !errors.Is(err, ErrInvalidClaimToken) {
		t.Fatalf("expected ErrInvalidClaimToken, got: %v", err)
	}

	// Test expiry during settlement (with writer still valid)
	control.leaseSeconds = 2000
	_ = control.RenewWriter(context.Background())
	control.now = func() int64 { return 1100 }
	if _, err := control.FailAction("act-fail", 1, admit.Lease.ClaimToken, nil); !errors.Is(err, ErrActionLeaseExpired) {
		t.Fatalf("expected ErrActionLeaseExpired, got: %v", err)
	}

	// Reset clock to valid lease window
	control.now = func() int64 { return 1020 }

	// Successful FailAction transitions CLAIMED -> FAILED_BEFORE_EFFECT
	failedRec, err := control.FailAction("act-fail", 1, admit.Lease.ClaimToken, json.RawMessage(`{"failed":true}`))
	if err != nil {
		t.Fatalf("FailAction failed: %v", err)
	}
	if failedRec.State != "FAILED_BEFORE_EFFECT" {
		t.Fatalf("unexpected state: %s", failedRec.State)
	}

	// Resources released
	resList, err := control.GetResourceLeases("act-fail")
	if err != nil || len(resList) != 0 {
		t.Fatalf("resources not released on FailAction: %d %v", len(resList), err)
	}

	// Subsequent settlement on already-terminal action is rejected
	if _, err := control.CompleteAction("act-fail", 1, admit.Lease.ClaimToken, nil); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for already terminal action, got: %v", err)
	}

	// Closed control rejection
	_ = control.Close()
	if _, err := control.CompleteAction("act-fail", 1, admit.Lease.ClaimToken, nil); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld on closed, got: %v", err)
	}
}

func TestAdmissionBudgetDimensions(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	// 1. MaxActionsPerHour
	policy := AdmissionPolicy{
		MaxConcurrentActions:                 10,
		MaxActionsPerHour:                    2,
		MaxConcurrentPerTarget:               10,
		MaxConcurrentPerPolicy:               10,
		MaxSimultaneousFailureDomainsTouched: 5,
	}
	for i := 1; i <= 3; i++ {
		id := fmt.Sprintf("hourly-%d", i)
		if err := control.Put(Record{Domain: "action", ID: id, Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "hourly-1", Owner: "w", LeaseSeconds: 60, Policy: policy}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "hourly-2", Owner: "w", LeaseSeconds: 60, Policy: policy}); err != nil {
		t.Fatal(err)
	}
	// 3rd action exceeds hourly limit of 2
	_, errHourly := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "hourly-3", Owner: "w", LeaseSeconds: 60, Policy: policy})
	if !errors.Is(errHourly, ErrBudgetExceeded) {
		t.Fatalf("expected ErrBudgetExceeded for hourly limit, got: %v", errHourly)
	}

	// 2. MaxConcurrentPerTarget
	control2 := openControlAt(t, 1000)
	defer control2.Close()
	targetPolicy := AdmissionPolicy{
		MaxConcurrentActions:   10,
		MaxActionsPerHour:      100,
		MaxConcurrentPerTarget: 1,
	}
	for _, id := range []string{"tgt-act-1", "tgt-act-2"} {
		payload := fmt.Sprintf(`{"parameters":{"targetId":"shared-target"}}`)
		if err := control2.Put(Record{Domain: "action", ID: id, Revision: 1, ExecutionEpoch: 1, State: "PENDING", Payload: json.RawMessage(payload)}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := control2.AdmitAndClaimAction(AdmissionRequest{ActionID: "tgt-act-1", Owner: "w", LeaseSeconds: 60, Policy: targetPolicy}); err != nil {
		t.Fatal(err)
	}
	// tgt-act-2 targets the same target, max is 1 -> rejected!
	_, errTarget := control2.AdmitAndClaimAction(AdmissionRequest{ActionID: "tgt-act-2", Owner: "w", LeaseSeconds: 60, Policy: targetPolicy})
	if !errors.Is(errTarget, ErrBudgetExceeded) {
		t.Fatalf("expected ErrBudgetExceeded for target limit, got: %v", errTarget)
	}

	// 3. MaxConcurrentPerPolicy
	control3 := openControlAt(t, 1000)
	defer control3.Close()
	policyLimit := AdmissionPolicy{
		MaxConcurrentActions:   10,
		MaxActionsPerHour:      100,
		MaxConcurrentPerPolicy: 1,
	}
	for _, id := range []string{"pol-act-1", "pol-act-2"} {
		payload := fmt.Sprintf(`{"parameters":{"policyId":"shared-pol"}}`)
		if err := control3.Put(Record{Domain: "action", ID: id, Revision: 1, ExecutionEpoch: 1, State: "PENDING", Payload: json.RawMessage(payload)}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := control3.AdmitAndClaimAction(AdmissionRequest{ActionID: "pol-act-1", Owner: "w", LeaseSeconds: 60, Policy: policyLimit}); err != nil {
		t.Fatal(err)
	}
	_, errPolicy := control3.AdmitAndClaimAction(AdmissionRequest{ActionID: "pol-act-2", Owner: "w", LeaseSeconds: 60, Policy: policyLimit})
	if !errors.Is(errPolicy, ErrBudgetExceeded) {
		t.Fatalf("expected ErrBudgetExceeded for policy limit, got: %v", errPolicy)
	}

	// 4. MaxSimultaneousFailureDomainsTouched
	control4 := openControlAt(t, 1000)
	defer control4.Close()
	domainPolicy := AdmissionPolicy{
		MaxConcurrentActions:                 10,
		MaxActionsPerHour:                    100,
		MaxSimultaneousFailureDomainsTouched: 1,
	}
	if err := control4.Put(Record{
		Domain: "action", ID: "dom-act-1", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":{"failureDomain":"zone-east-1"}}`),
	}); err != nil {
		t.Fatal(err)
	}
	if err := control4.Put(Record{
		Domain: "action", ID: "dom-act-2", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":{"failureDomain":"zone-west-2"}}`),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control4.AdmitAndClaimAction(AdmissionRequest{ActionID: "dom-act-1", Owner: "w", LeaseSeconds: 60, Policy: domainPolicy}); err != nil {
		t.Fatal(err)
	}
	// Different domain zone-west-2 exceeds max 1 failure domain
	_, errDomain := control4.AdmitAndClaimAction(AdmissionRequest{ActionID: "dom-act-2", Owner: "w", LeaseSeconds: 60, Policy: domainPolicy})
	if !errors.Is(errDomain, ErrBudgetExceeded) {
		t.Fatalf("expected ErrBudgetExceeded for failure domain limit, got: %v", errDomain)
	}
}

func TestAdmissionPayloadAndDerivationEdges(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	// 1. Complex payload with parameters
	complexPayload := `{
		"policyId": "p-top",
		"backupId": "b-top",
		"source": "s-top",
		"destination": "d-top",
		"parameters": {
			"policyId": "p-sub",
			"backupId": "b-sub",
			"sourceTargetId": "src-target-1",
			"destTargetId": "dst-target-2",
			"failureDomain": "domain-a"
		}
	}`
	if err := control.Put(Record{
		Domain: "action", ID: "act-complex", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(complexPayload),
	}); err != nil {
		t.Fatal(err)
	}

	admit, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-complex",
		Owner:        "worker-cx",
		LeaseSeconds: -1, // should default to 60
	})
	if err != nil {
		t.Fatalf("admit complex failed: %v", err)
	}
	if admit.Lease.LeaseUntil != 1060 {
		t.Fatalf("expected default 60s lease, got: %d", admit.Lease.LeaseUntil)
	}
	// Verify derived keys from payload
	resKeys, err := control.GetResourceLeases("act-complex")
	if err != nil {
		t.Fatal(err)
	}
	hasSrc := false
	hasDst := false
	hasBackup := false
	for _, k := range resKeys {
		if k.ResourceKey == "target:src-target-1" {
			hasSrc = true
		}
		if k.ResourceKey == "target:dst-target-2" {
			hasDst = true
		}
		if k.ResourceKey == "backup:p-sub:b-sub" {
			hasBackup = true
		}
	}
	if !hasSrc || !hasDst || !hasBackup {
		t.Fatalf("missing expected resource keys in derived set: %+v", resKeys)
	}

	// 2. RiskSubject fallback
	control = openControlAt(t, 1000)
	defer control.Close()
	riskPayload := `{
		"riskSubject": {
			"policyId": "risk-pol",
			"failureDomain": "risk-domain"
		}
	}`
	if err := control.Put(Record{
		Domain: "action", ID: "act-risk", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(riskPayload),
	}); err != nil {
		t.Fatal(err)
	}
	admitRisk, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: "act-risk",
		Owner:    "worker-rx",
	})
	if err != nil {
		t.Fatal(err)
	}
	riskKeys, _ := control.GetResourceLeases("act-risk")
	hasRiskPol := false
	for _, k := range riskKeys {
		if k.ResourceKey == "policy:risk-pol" {
			hasRiskPol = true
		}
	}
	if !hasRiskPol {
		t.Fatalf("missing policy:risk-pol in %+v", riskKeys)
	}
	_ = admitRisk

	// 3. Admission input validations
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: ""}); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "foo/bar"}); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID for invalid ActionID, got: %v", err)
	}
	if err := control.Put(Record{
		Domain: "action", ID: "act-invalid-key", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-invalid-key", ResourceKeys: []string{string([]byte{0})}}); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("expected ErrInvalidPayload for null byte in resource key, got: %v", err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "nonexistent", Owner: "w"}); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("expected ErrActionNotClaimable, got: %v", err)
	}

	// 4. Action in non-action domain cannot be claimed
	if err := control.Put(Record{
		Domain: "policy", ID: "pol-test", Revision: 1, State: "ACTIVE",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "pol-test", Owner: "w"}); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("expected ErrActionNotClaimable for non-action domain, got: %v", err)
	}

	// 5. Closed control
	_ = control.Close()
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-risk", Owner: "w"}); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld on closed, got: %v", err)
	}
}

func TestGetLeasesAndQueryEdges(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	// Non-existent action lease
	lease, exists, err := control.GetActionLease("does-not-exist")
	if err != nil || exists || lease.ActionID != "" {
		t.Fatalf("expected false, nil for non-existent lease, got: %v, %v, %v", lease, exists, err)
	}

	// Invalid action ID
	if _, _, err := control.GetActionLease(""); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}
	if _, err := control.GetResourceLeases(""); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}

	// Non-existent resource lease returns empty slice
	leases, err := control.GetResourceLeases("does-not-exist")
	if err != nil || len(leases) != 0 {
		t.Fatalf("expected empty slice for non-existent resource leases, got: %v, %v", leases, err)
	}

	// Closed control
	_ = control.Close()
	if _, _, err := control.GetActionLease("a"); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}
	if _, err := control.GetResourceLeases("a"); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}
}

func TestRetireEmptyActionAdmissionHistoryRetained(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	if err := control.Put(Record{
		Domain: "action", ID: "act-hist", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-hist", Owner: "w"}); err != nil {
		t.Fatal(err)
	}

	// Rollback(0) must fail with ErrAdmissionHistoryRetained
	err := control.Rollback(0)
	if !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatalf("expected ErrAdmissionHistoryRetained on rollback with active action lease, got: %v", err)
	}
}

func TestValidateStorageDispatchIntentDirect(t *testing.T) {
	valid := dispatchIntent()
	if err := ValidateStorageDispatchIntent(valid); err != nil {
		t.Fatalf("valid intent failed validation: %v", err)
	}

	invalid := valid
	invalid.Schema = "bad-schema"
	if err := ValidateStorageDispatchIntent(invalid); !errors.Is(err, ErrInvalidStorageIntent) {
		t.Fatalf("expected ErrInvalidStorageIntent, got: %v", err)
	}
}

func TestAdditionalAdmissionCoverage(t *testing.T) {
	// 1. effectivePolicy defaults when negative/zero
	p := effectivePolicy(AdmissionPolicy{
		MaxConcurrentActions:                 -1,
		MaxActionsPerHour:                    0,
		MaxConcurrentPerTarget:               -2,
		MaxConcurrentPerPolicy:               0,
		MaxSimultaneousFailureDomainsTouched: -3,
	})
	def := defaultAdmissionPolicy()
	if p != def {
		t.Fatalf("expected effectivePolicy to equal default, got: %+v", p)
	}

	// 2. parsePayloadMetadata with invalid JSON
	if _, err := parsePayloadMetadata([]byte("{invalid-json")); err == nil {
		t.Fatal("expected error on invalid json payload metadata")
	}

	control := openControlAt(t, 1000)
	defer control.Close()

	// 3. SchemaInactive on AdmitAndClaimAction, RenewActionLease, CompleteAction, GetActionLease, GetResourceLeases
	control.schema = SchemaV4
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act"}); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive on Admit, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act", Epoch: 1, ClaimToken: "t"}); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive on Renew, got: %v", err)
	}
	if _, err := control.CompleteAction("act", 1, "t", nil); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive on Complete, got: %v", err)
	}
	if _, _, err := control.GetActionLease("act"); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive on GetActionLease, got: %v", err)
	}
	if _, err := control.GetResourceLeases("act"); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive on GetResourceLeases, got: %v", err)
	}
	control.schema = SchemaV5

	// 4. Resource key length > 1024
	longKey := strings.Repeat("a", 1025)
	if err := control.Put(Record{Domain: "action", ID: "act-long", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-long", ResourceKeys: []string{longKey}}); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("expected ErrInvalidPayload for oversize resource key, got: %v", err)
	}

	// 5. Pre-commit clock expiry on RenewActionLease and settleActionTerminalTx
	if err := control.Put(Record{Domain: "action", ID: "act-clock", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	admitClock, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-clock", Owner: "w", LeaseSeconds: 60})
	if err != nil {
		t.Fatal(err)
	}

	// Clock expiry on renewal
	calls := 0
	control.now = func() int64 {
		calls++
		if calls == 1 {
			return 1005
		}
		return 1050 // expires writer lease (1030) before commit
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{
		ActionID:   "act-clock",
		Epoch:      1,
		ClaimToken: admitClock.Lease.ClaimToken,
	}); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld on renew pre-commit expiry, got: %v", err)
	}

	// Reset clock to valid time
	control.now = func() int64 { return 1005 }

	// Test illegal transition (CompleteAction directly from CLAIMED is not allowed)
	if _, err := control.CompleteAction("act-clock", 1, admitClock.Lease.ClaimToken, nil); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("expected ErrIllegalTransition on complete directly from CLAIMED, got: %v", err)
	}

	// Clock expiry on settlement (using legal FailAction from CLAIMED)
	calls = 0
	control.now = func() int64 {
		calls++
		if calls == 1 {
			return 1005
		}
		return 1050 // expires writer lease
	}
	if _, err := control.FailAction("act-clock", 1, admitClock.Lease.ClaimToken, nil); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld on fail pre-commit expiry, got: %v", err)
	}

	// Reset clock
	control.now = func() int64 { return 1005 }
	control.leaseSeconds = 300
	_ = control.RenewWriter(context.Background())

	// Fail successfully to test GetActionLease on terminal state
	if _, err := control.FailAction("act-clock", 1, admitClock.Lease.ClaimToken, nil); err != nil {
		t.Fatal(err)
	}
	if lease, exists, err := control.GetActionLease("act-clock"); err != nil || exists || lease.ActionID != "" {
		t.Fatalf("expected terminal lease to return false, nil; got lease=%+v exists=%v err=%v", lease, exists, err)
	}

	// 6. Test retireEmptyActionAdmissionTx in isolated control
	controlIso := openControlAt(t, 1000)
	defer controlIso.Close()
	txIso, err := controlIso.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer txIso.Rollback()

	// Case A: insert action_lease_events -> check events
	if _, err := txIso.Exec(`INSERT INTO action_lease_events(
		action_id, event_type, owner, epoch, claim_token, lease_until, claim_revision, writer_fencing_token, recorded_at
	) VALUES('a', 'ADMITTED', 'o', 1, '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', 100, 1, 1, 10)`); err != nil {
		t.Fatal(err)
	}
	if err := retireEmptyActionAdmissionTx(txIso); !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatalf("expected ErrAdmissionHistoryRetained when events exist, got: %v", err)
	}
	_ = txIso.Rollback()

	// Case B: insert action_resource_leases in fresh tx
	txIso2, err := controlIso.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer txIso2.Rollback()
	if _, err := txIso2.Exec("INSERT INTO action_resource_leases(resource_key, action_id, owner, epoch, acquired_at, lease_until, writer_fencing_token) VALUES('k', 'a', 'o', 1, 10, 20, 1)"); err != nil {
		t.Fatal(err)
	}
	if err := retireEmptyActionAdmissionTx(txIso2); !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatalf("expected ErrAdmissionHistoryRetained when resource leases exist, got: %v", err)
	}

	// 7. Test verifyActionAdmissionSchemaTx failure when table schema altered
	if _, err := txIso2.Exec("DROP TABLE action_leases"); err != nil {
		t.Fatal(err)
	}
	if err := verifyActionAdmissionSchemaTx(txIso2); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("expected ErrForeignRuntimeStore on missing/altered schema object, got: %v", err)
	}

	// 8. Empty payload metadata
	if p, err := parsePayloadMetadata(nil); err != nil || p.PolicyID != "" {
		t.Fatal(err)
	}
	if p, err := parsePayloadMetadata([]byte{}); err != nil || p.PolicyID != "" {
		t.Fatal(err)
	}

	// 9. Admit terminal action -> ErrActionNotClaimable
	if err := control.Put(Record{Domain: "action", ID: "act-terminal", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	if err := control.Put(Record{Domain: "action", ID: "act-terminal", Revision: 2, ExecutionEpoch: 1, State: "FAILED_BEFORE_EFFECT"}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-terminal"}); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("expected ErrActionNotClaimable for terminal action, got: %v", err)
	}

	// 10. A corrupted lease cannot choose a higher action epoch.
	if err := control.Put(Record{Domain: "action", ID: "act-higher-epoch", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-higher-epoch", Owner: "w", LeaseSeconds: 10}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE action_leases SET epoch=5, lease_until=1005 WHERE action_id='act-higher-epoch'"); err != nil {
		t.Fatal(err)
	}
	control.now = func() int64 { return 1010 }
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-higher-epoch", Owner: "w2", LeaseSeconds: 60}); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("corrupt epoch was adopted: %v", err)
	}

	// 11. RenewActionLease when action_leases is terminal or action_journal state is not active
	if _, err := control.RenewActionLease(ActionLeaseRenewal{
		ActionID:   "act-clock",
		Epoch:      1,
		ClaimToken: admitClock.Lease.ClaimToken,
	}); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for terminal action renewal, got: %v", err)
	}
	// Corruption fixtures are independent; never repair one implicitly to admit another.
	control = openControlAt(t, 1010)
	defer control.Close()

	if err := control.Put(Record{Domain: "action", ID: "act-state-change", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	admitState, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-state-change", Owner: "w", LeaseSeconds: 60})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE action_journal SET state='FAILED' WHERE id='act-state-change'"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{
		ActionID:   "act-state-change",
		Epoch:      1,
		ClaimToken: admitState.Lease.ClaimToken,
	}); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("expected ErrCorruptRecord after tampering with action state, got: %v", err)
	}

	// 12. Corrupt payload JSON in peer active action during budget check
	control = openControlAt(t, 1010)
	defer control.Close()
	if err := control.Put(Record{Domain: "action", ID: "act-corrupt-json", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-corrupt-json", Owner: "w", LeaseSeconds: 60}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`UPDATE action_journal SET payload_json='"string-not-an-object"' WHERE id='act-corrupt-json'`); err != nil {
		t.Fatal(err)
	}
	if err := control.Put(Record{Domain: "action", ID: "act-after-corrupt", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-after-corrupt", Owner: "w", LeaseSeconds: 60, Policy: AdmissionPolicy{MaxConcurrentActions: 10}}); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt active peer did not fail closed: %v", err)
	}

	// 13. An orphan lease has no immutable claim to authorize settlement.
	if _, err := control.db.Exec(`INSERT INTO action_leases(action_id, owner, epoch, claim_token, lease_until, acquired_at, updated_at, claim_revision, writer_fencing_token)
		VALUES('orphan-action', 'w', 1, '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', 2000, 1000, 1000, 1, 1)`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.FailAction("orphan-action", 1, "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", nil); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for missing claim history, got: %v", err)
	}

	// 14. Journal epoch mismatch during settlement -> ErrActionLeaseStale
	control = openControlAt(t, 1010)
	defer control.Close()
	if err := control.Put(Record{Domain: "action", ID: "act-epoch-mismatch", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	admitMismatch, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-epoch-mismatch", Owner: "w", LeaseSeconds: 60})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE action_journal SET execution_epoch=99 WHERE id='act-epoch-mismatch'"); err != nil {
		t.Fatal(err)
	}
	// 14. Tampered journal row during settlement -> ErrCorruptRecord
	if _, err := control.FailAction("act-epoch-mismatch", 1, admitMismatch.Lease.ClaimToken, nil); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("expected ErrCorruptRecord for tampered journal epoch, got: %v", err)
	}

	// 15. Resource lease epoch mismatch during renewal -> ErrActionLeaseStale
	control = openControlAt(t, 1010)
	defer control.Close()
	if err := control.Put(Record{Domain: "action", ID: "act-mismatch-locks", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	admitMis, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-mismatch-locks", Owner: "w", LeaseSeconds: 60, ResourceKeys: []string{"res-m1", "res-m2"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE action_resource_leases SET epoch=99 WHERE resource_key='res-m1'"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{
		ActionID: "act-mismatch-locks", Epoch: 1, ClaimToken: admitMis.Lease.ClaimToken,
	}); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale on mismatched resource lease epoch, got: %v", err)
	}
}

func TestAdmitAndClaimActionDefaultOwnerAndDuplicateKeys(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	if err := control.Put(Record{Domain: "action", ID: "act-owner-dup", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}

	// 1. Owner empty defaults to store.owner, duplicate resource keys get deduped
	res, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-owner-dup",
		Owner:        "", // defaults to control.owner
		LeaseSeconds: 60,
		ResourceKeys: []string{"res-a", "res-a", "res-b"},
	})
	if err != nil {
		t.Fatalf("AdmitAndClaimAction failed: %v", err)
	}
	if res.Lease.Owner != control.owner {
		t.Fatalf("expected owner %q, got %q", control.owner, res.Lease.Owner)
	}

	locks, err := control.GetResourceLeases("act-owner-dup")
	if err != nil {
		t.Fatal(err)
	}
	if len(locks) != 2 {
		t.Fatalf("expected 2 unique locks, got %d", len(locks))
	}

	// 2. Invalid resource keys
	invalidKeys := [][]string{
		{""},
		{strings.Repeat("k", 1025)},
		{"nul\x00key"},
		{string([]byte{0xff, 0xfe})},
	}
	for idx, ik := range invalidKeys {
		id := fmt.Sprintf("act-inv-k-%d", idx)
		if err := control.Put(Record{Domain: "action", ID: id, Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
			t.Fatal(err)
		}
		if _, err := control.AdmitAndClaimAction(AdmissionRequest{
			ActionID: id, Owner: "w", LeaseSeconds: 60, ResourceKeys: ik,
		}); !errors.Is(err, ErrInvalidPayload) {
			t.Fatalf("expected ErrInvalidPayload for invalid keys %v, got: %v", ik, err)
		}
	}
}

func TestAdmitAndClaimActionFailureDomainReuse(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	// Seed action 1 in zone-a
	if err := control.Put(Record{
		Domain: "action", ID: "act-dom-1", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":{"failureDomain":"zone-a"}}`),
	}); err != nil {
		t.Fatal(err)
	}
	policy := AdmissionPolicy{
		MaxConcurrentActions:                 10,
		MaxActionsPerHour:                    100,
		MaxSimultaneousFailureDomainsTouched: 1,
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: "act-dom-1", Owner: "w", LeaseSeconds: 60, Policy: policy,
	}); err != nil {
		t.Fatal(err)
	}

	// Seed action 2 also in zone-a: should SUCCEED because zone-a is already active
	if err := control.Put(Record{
		Domain: "action", ID: "act-dom-2", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":{"failureDomain":"zone-a"}}`),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: "act-dom-2", Owner: "w", LeaseSeconds: 60, Policy: policy,
	}); err != nil {
		t.Fatalf("expected success for already-active failure domain, got: %v", err)
	}

	// Seed action 3 in zone-b: should FAIL with ErrBudgetExceeded
	if err := control.Put(Record{
		Domain: "action", ID: "act-dom-3", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":{"failureDomain":"zone-b"}}`),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: "act-dom-3", Owner: "w", LeaseSeconds: 60, Policy: policy,
	}); !errors.Is(err, ErrBudgetExceeded) {
		t.Fatalf("expected ErrBudgetExceeded for second failure domain, got: %v", err)
	}
}

func TestRenewActionLeaseDefaultsAndChecks(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	if err := control.Put(Record{Domain: "action", ID: "act-ren-chk", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	admit, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: "act-ren-chk", Owner: "worker-1", LeaseSeconds: 60,
	})
	if err != nil {
		t.Fatal(err)
	}

	// 1. LeaseSeconds <= 0 defaults to 60
	renewed, err := control.RenewActionLease(ActionLeaseRenewal{
		ActionID: "act-ren-chk", Epoch: 1, ClaimToken: admit.Lease.ClaimToken, LeaseSeconds: 0,
	})
	if err != nil {
		t.Fatalf("RenewActionLease failed: %v", err)
	}
	if renewed.LeaseUntil != 1060 {
		t.Fatalf("expected lease until 1060, got %d", renewed.LeaseUntil)
	}

	// 2. Owner mismatch returns ErrActionLeaseStale
	if _, err := control.RenewActionLease(ActionLeaseRenewal{
		ActionID: "act-ren-chk", Owner: "wrong-worker", Epoch: 1, ClaimToken: admit.Lease.ClaimToken, LeaseSeconds: 60,
	}); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for owner mismatch, got: %v", err)
	}
}

func TestCompleteActionPayloadNilAndIllegalTransitions(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	initialPayload := json.RawMessage(`{"key":"value1"}`)
	if err := control.Put(Record{
		Domain: "action", ID: "act-settle", Revision: 1, ExecutionEpoch: 1, State: "PENDING", Payload: initialPayload,
	}); err != nil {
		t.Fatal(err)
	}
	admit, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: "act-settle", Owner: "w", LeaseSeconds: 10,
	})
	if err != nil {
		t.Fatal(err)
	}

	// 1. CompleteAction from CLAIMED state is illegal (CLAIMED -> SUCCEEDED is invalid)
	if _, err := control.CompleteAction("act-settle", 1, admit.Lease.ClaimToken, nil); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("expected ErrIllegalTransition when completing directly from CLAIMED, got: %v", err)
	}

	// 2. FailAction from CLAIMED state is legal (CLAIMED -> FAILED_BEFORE_EFFECT), and payload == nil preserves existing payload
	failed, err := control.FailAction("act-settle", 1, admit.Lease.ClaimToken, nil)
	if err != nil {
		t.Fatalf("FailAction failed: %v", err)
	}
	if string(failed.Payload) != string(initialPayload) {
		t.Fatalf("expected payload preserved %s, got %s", string(initialPayload), string(failed.Payload))
	}
	if failed.State != "FAILED_BEFORE_EFFECT" {
		t.Fatalf("expected state FAILED_BEFORE_EFFECT, got %s", failed.State)
	}

	// 3. Takeover transitions action to EFFECT_UNKNOWN, then CompleteAction transitions EFFECT_UNKNOWN -> SUCCEEDED
	if err := control.Put(Record{
		Domain: "action", ID: "act-takeover-complete", Revision: 1, ExecutionEpoch: 1, State: "PENDING", Payload: initialPayload,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: "act-takeover-complete", Owner: "w1", LeaseSeconds: 10,
	}); err != nil {
		t.Fatal(err)
	}
	// Advance clock past lease expiry
	control.now = func() int64 { return 1020 }
	// Takeover moves it to EFFECT_UNKNOWN
	takeover, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID: "act-takeover-complete", Owner: "w2", LeaseSeconds: 60,
	})
	if err != nil {
		t.Fatalf("takeover failed: %v", err)
	}
	// From EFFECT_UNKNOWN, CompleteAction is legal and transitions to SUCCEEDED
	completed, err := control.CompleteAction("act-takeover-complete", takeover.Lease.Epoch, takeover.Lease.ClaimToken, nil)
	if err != nil {
		t.Fatalf("CompleteAction failed from EFFECT_UNKNOWN: %v", err)
	}
	if completed.State != "SUCCEEDED" {
		t.Fatalf("expected state SUCCEEDED, got %s", completed.State)
	}
	if string(completed.Payload) != string(initialPayload) {
		t.Fatalf("expected payload preserved %s, got %s", string(initialPayload), string(completed.Payload))
	}

	// 4. GetResourceLeases for non-existent action returns empty slice
	emptyLocks, err := control.GetResourceLeases("no-such-action")
	if err != nil || len(emptyLocks) != 0 {
		t.Fatalf("expected empty slice for non-existent action, got %v %v", emptyLocks, err)
	}
}

func TestActionAdmissionInputValidationAndClosedStore(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	// 1. store.closed checks
	closedControl := openControlAt(t, 1000)
	_ = closedControl.Close()

	if _, err := closedControl.AdmitAndClaimAction(AdmissionRequest{ActionID: "a"}); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}
	if _, err := closedControl.RenewActionLease(ActionLeaseRenewal{ActionID: "a", Epoch: 1, ClaimToken: "tok"}); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}
	if _, err := closedControl.CompleteAction("a", 1, "tok", nil); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}
	if _, err := closedControl.FailAction("a", 1, "tok", nil); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}
	if _, _, err := closedControl.GetActionLease("a"); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}
	if _, err := closedControl.GetResourceLeases("a"); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}

	// 2. store.schema != CurrentSchema
	v4Control := openControlAt(t, 1000)
	defer v4Control.Close()
	v4Control.schema = SchemaV4

	if _, err := v4Control.AdmitAndClaimAction(AdmissionRequest{ActionID: "a"}); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive, got: %v", err)
	}
	if _, err := v4Control.RenewActionLease(ActionLeaseRenewal{ActionID: "a", Epoch: 1, ClaimToken: "tok"}); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive, got: %v", err)
	}
	if _, err := v4Control.CompleteAction("a", 1, "tok", nil); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive, got: %v", err)
	}
	if _, err := v4Control.FailAction("a", 1, "tok", nil); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive, got: %v", err)
	}
	if _, _, err := v4Control.GetActionLease("a"); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive, got: %v", err)
	}
	if _, err := v4Control.GetResourceLeases("a"); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive, got: %v", err)
	}

	// 3. Input validation: empty record IDs and out of range epochs
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: ""}); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: ""}); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act", Epoch: 0}); !errors.Is(err, ErrEpochOutOfRange) {
		t.Fatalf("expected ErrEpochOutOfRange, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act", Epoch: math.MaxUint64}); !errors.Is(err, ErrEpochOutOfRange) {
		t.Fatalf("expected ErrEpochOutOfRange, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act", Epoch: 1, ClaimToken: ""}); !errors.Is(err, ErrInvalidClaimToken) {
		t.Fatalf("expected ErrInvalidClaimToken, got: %v", err)
	}
	if _, err := control.CompleteAction("", 1, "tok", nil); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}
	if _, err := control.CompleteAction("act", 0, "tok", nil); !errors.Is(err, ErrEpochOutOfRange) {
		t.Fatalf("expected ErrEpochOutOfRange, got: %v", err)
	}
	if _, err := control.CompleteAction("act", math.MaxUint64, "tok", nil); !errors.Is(err, ErrEpochOutOfRange) {
		t.Fatalf("expected ErrEpochOutOfRange, got: %v", err)
	}
	if _, err := control.CompleteAction("act", 1, "", nil); !errors.Is(err, ErrInvalidClaimToken) {
		t.Fatalf("expected ErrInvalidClaimToken, got: %v", err)
	}
	if _, err := control.FailAction("", 1, "tok", nil); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}
	if _, err := control.FailAction("act", 0, "tok", nil); !errors.Is(err, ErrEpochOutOfRange) {
		t.Fatalf("expected ErrEpochOutOfRange, got: %v", err)
	}
	if _, err := control.FailAction("act", math.MaxUint64, "tok", nil); !errors.Is(err, ErrEpochOutOfRange) {
		t.Fatalf("expected ErrEpochOutOfRange, got: %v", err)
	}
	if _, err := control.FailAction("act", 1, "", nil); !errors.Is(err, ErrInvalidClaimToken) {
		t.Fatalf("expected ErrInvalidClaimToken, got: %v", err)
	}
	if _, _, err := control.GetActionLease(""); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}
	if _, exists, err := control.GetActionLease("non-existent"); err != nil || exists {
		t.Fatalf("expected exists=false, err=nil for non-existent lease, got exists=%v err=%v", exists, err)
	}
	if _, err := control.GetResourceLeases(""); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}

	// 4. Non-existent action journal row for AdmitAndClaimAction -> ErrActionNotClaimable
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-non-existent"}); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("expected ErrActionNotClaimable, got: %v", err)
	}

	// 5. Non-claimable terminal action journal row -> ErrActionNotClaimable
	if err := control.Put(Record{Domain: "action", ID: "act-term", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	admitTerm, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-term", Owner: "w", LeaseSeconds: 60})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.FailAction("act-term", 1, admitTerm.Lease.ClaimToken, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-term", Owner: "w", LeaseSeconds: 60}); !errors.Is(err, ErrActionNotClaimable) {
		t.Fatalf("expected ErrActionNotClaimable for terminal action, got: %v", err)
	}
}

func TestActionAdmissionDeepErrorBranches(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	// 1. AdmitAndClaimAction with corrupt digest in journal (line 260)
	if err := control.Put(Record{Domain: "action", ID: "act-tamper-j", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE action_journal SET record_digest=? WHERE id='act-tamper-j'", strings.Repeat("a", 64)); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-tamper-j", Owner: "w", LeaseSeconds: 60}); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("expected ErrCorruptRecord for tampered journal, got: %v", err)
	}

	// 2. AdmitAndClaimAction with non-object valid JSON payload (line 316)
	if err := control.Put(Record{Domain: "action", ID: "act-num-payload", Revision: 1, ExecutionEpoch: 1, State: "PENDING", Payload: json.RawMessage("123")}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-num-payload", Owner: "w", LeaseSeconds: 60}); !errors.Is(err, ErrInvalidPayload) {
		t.Fatalf("expected ErrInvalidPayload for numeric payload, got: %v", err)
	}

	// 3. Seed an active admitted action
	if err := control.Put(Record{Domain: "action", ID: "act-deep-settle", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	admit, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-deep-settle", Owner: "w", LeaseSeconds: 60})
	if err != nil {
		t.Fatal(err)
	}

	// 4. RenewActionLease wrong epoch (line 714)
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-deep-settle", Epoch: 99, ClaimToken: admit.Lease.ClaimToken, LeaseSeconds: 60}); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for wrong epoch, got: %v", err)
	}

	// 5. RenewActionLease wrong token (line 717)
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-deep-settle", Epoch: 1, ClaimToken: "wrong-token-value", LeaseSeconds: 60}); !errors.Is(err, ErrInvalidClaimToken) {
		t.Fatalf("expected ErrInvalidClaimToken for wrong token, got: %v", err)
	}

	// 6. Settle wrong epoch (line 863)
	if _, err := control.FailAction("act-deep-settle", 99, admit.Lease.ClaimToken, nil); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for wrong epoch, got: %v", err)
	}

	// 7. Settle wrong token (line 866)
	if _, err := control.FailAction("act-deep-settle", 1, "wrong-token-value", nil); !errors.Is(err, ErrInvalidClaimToken) {
		t.Fatalf("expected ErrInvalidClaimToken for wrong token, got: %v", err)
	}

	// 8. Settle on already-terminal lease (line 860) and Renew on terminal lease (line 711)
	if _, err := control.FailAction("act-deep-settle", 1, admit.Lease.ClaimToken, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := control.FailAction("act-deep-settle", 1, admit.Lease.ClaimToken, nil); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for already-terminal lease, got: %v", err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-deep-settle", Epoch: 1, ClaimToken: admit.Lease.ClaimToken, LeaseSeconds: 60}); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for terminal lease, got: %v", err)
	}

	// 9. Settle and Renew on expired lease (lines 723, 869)
	if err := control.Put(Record{Domain: "action", ID: "act-exp-settle", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	admitExp, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-exp-settle", Owner: "w", LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	// Advance clock to 1020: action lease expired at 1010, but writer lease is valid until 1030
	control.now = func() int64 { return 1020 }
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-exp-settle", Epoch: 1, ClaimToken: admitExp.Lease.ClaimToken, LeaseSeconds: 60}); !errors.Is(err, ErrActionLeaseExpired) {
		t.Fatalf("expected ErrActionLeaseExpired, got: %v", err)
	}
	if _, err := control.FailAction("act-exp-settle", 1, admitExp.Lease.ClaimToken, nil); !errors.Is(err, ErrActionLeaseExpired) {
		t.Fatalf("expected ErrActionLeaseExpired, got: %v", err)
	}
}

func TestTakeoverEpochBumpAndActiveLeaseChecks(t *testing.T) {
	control := openControlAt(t, 1000)
	defer control.Close()

	if err := control.Put(Record{Domain: "action", ID: "act-takeover-bump", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-takeover-bump", Owner: "w1", LeaseSeconds: 10}); err != nil {
		t.Fatal(err)
	}

	// 1. Calling AdmitAndClaimAction while lease is active (now=1000 <= 1010) returns ErrActionLeaseActive (line 297)
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-takeover-bump", Owner: "w2", LeaseSeconds: 60}); !errors.Is(err, ErrActionLeaseActive) {
		t.Fatalf("expected ErrActionLeaseActive, got: %v", err)
	}

	// 2. Tampering cannot advance the authoritative journal's epoch.
	if _, err := control.db.Exec("UPDATE action_leases SET epoch=5 WHERE action_id='act-takeover-bump'"); err != nil {
		t.Fatal(err)
	}
	// Advance time past expiry
	control.now = func() int64 { return 1020 }
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-takeover-bump", Owner: "w2", LeaseSeconds: 60}); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("corrupt lease was adopted: %v", err)
	}

	// 3. RenewActionLease when action state in action_journal is inactive (line 736)
	control = openControlAt(t, 1000)
	defer control.Close()
	if err := control.Put(Record{Domain: "action", ID: "act-takeover-bump", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-takeover-bump"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE action_journal SET state='SUCCEEDED' WHERE id='act-takeover-bump'"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{ActionID: "act-takeover-bump", Epoch: 1, ClaimToken: claim.Lease.ClaimToken, LeaseSeconds: 60}); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("expected ErrCorruptRecord for tampered journal state, got: %v", err)
	}

	// 4. Settle when existing.ExecutionEpoch != epoch (line 887)
	if err := control.Put(Record{Domain: "action", ID: "act-settle-epoch-mismatch", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	admitSettle, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-settle-epoch-mismatch", Owner: "w", LeaseSeconds: 60})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE action_leases SET epoch=99 WHERE action_id='act-settle-epoch-mismatch'"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.FailAction("act-settle-epoch-mismatch", 99, admitSettle.Lease.ClaimToken, nil); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for journal epoch mismatch, got: %v", err)
	}

	// 5. Illegal transition on CompleteAction directly on CLAIMED state (line 890)
	control = openControlAt(t, 1000)
	defer control.Close()
	if err := control.Put(Record{Domain: "action", ID: "act-illegal-complete", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	resClaimed, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-illegal-complete", Owner: "w", LeaseSeconds: 60})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.CompleteAction("act-illegal-complete", 1, resClaimed.Lease.ClaimToken, nil); !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("expected ErrIllegalTransition on CLAIMED -> SUCCEEDED, got: %v", err)
	}

	// 7. rowsAffected != expectedLocks in RenewActionLease (line 766)
	if err := control.Put(Record{Domain: "action", ID: "act-resource-mismatch", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
		t.Fatal(err)
	}
	resMismatch, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     "act-resource-mismatch",
		Owner:        "w",
		LeaseSeconds: 60,
		ResourceKeys: []string{"res-edge-a", "res-edge-b"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE action_resource_leases SET epoch=99 WHERE resource_key='res-edge-a'"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.RenewActionLease(ActionLeaseRenewal{
		ActionID:     "act-resource-mismatch",
		Epoch:        1,
		ClaimToken:   resMismatch.Lease.ClaimToken,
		LeaseSeconds: 60,
	}); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale on rowsAffected != expectedLocks, got: %v", err)
	}

	// 8. GetActionLease and GetResourceLeases input validation
	if _, _, err := control.GetActionLease(""); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}
	if _, err := control.GetResourceLeases(""); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}

	// 9. Closed control rejections
	_ = control.Close()
	if _, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-epoch-zero"}); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}
	if _, _, err := control.GetActionLease("act-epoch-zero"); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}
	if _, err := control.GetResourceLeases("act-epoch-zero"); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}

	// 10. settleActionTerminalTx when action row does not exist in action_journal (line 881)
	c2 := openControlAt(t, 1000)
	defer c2.Close()
	orphanToken := strings.Repeat("a", 64)
	if _, err := c2.db.Exec(`INSERT INTO action_leases(action_id, owner, epoch, claim_token, lease_until, acquired_at, updated_at, claim_revision, writer_fencing_token)
		VALUES('act-orphaned-lease', 'w', 1, ?, 2000, 1000, 1000, 1, 1)`, orphanToken); err != nil {
		t.Fatal(err)
	}
	if _, err := c2.FailAction("act-orphaned-lease", 1, orphanToken, nil); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("expected ErrActionLeaseStale for orphaned lease, got: %v", err)
	}

	// 11. RenewActionLease when action row does not exist in action_journal (line 732)
	if _, err := c2.RenewActionLease(ActionLeaseRenewal{
		ActionID:   "act-orphaned-lease",
		Epoch:      1,
		ClaimToken: orphanToken,
	}); err == nil {
		t.Fatal("expected error on orphaned lease renewal")
	}

	// 12. destTargetId matching in admission budget check (lines 539, 549)
	targetPolicy := AdmissionPolicy{
		MaxConcurrentActions:   10,
		MaxActionsPerHour:      100,
		MaxConcurrentPerTarget: 1,
	}
	if err := c2.Put(Record{
		Domain: "action", ID: "act-dest-1", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":{"destTargetId":"shared-dest"}}`),
	}); err != nil {
		t.Fatal(err)
	}
	if err := c2.Put(Record{
		Domain: "action", ID: "act-dest-2", Revision: 1, ExecutionEpoch: 1, State: "PENDING",
		Payload: json.RawMessage(`{"parameters":{"destTargetId":"shared-dest"}}`),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := c2.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-dest-1", Owner: "w", LeaseSeconds: 60, Policy: targetPolicy}); err != nil {
		t.Fatal(err)
	}
	if _, err := c2.AdmitAndClaimAction(AdmissionRequest{ActionID: "act-dest-2", Owner: "w", LeaseSeconds: 60, Policy: targetPolicy}); !errors.Is(err, ErrBudgetExceeded) {
		t.Fatalf("expected ErrBudgetExceeded on destTargetId conflict, got: %v", err)
	}
}
