package store

import (
	"encoding/json"
	"errors"
	"testing"
)

func TestActionAdmissionGracefulReopenLeaseExpiry(t *testing.T) {
	// Step 1: Open control store, seed pending action and claim it
	tNow := int64(2000)
	control := openControlAt(t, tNow)
	t.Cleanup(func() { _ = control.Close() })
	path := control.path

	actionID := "crash-act-1"
	record := Record{
		Domain:         "action",
		ID:             actionID,
		Revision:       1,
		ExecutionEpoch: 1,
		State:          "PENDING",
		Payload:        json.RawMessage(`{"parameters":{"targetId":"t-crash"}}`),
	}
	if err := control.Put(record); err != nil {
		t.Fatal(err)
	}

	admitRes, err := control.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     actionID,
		Owner:        "original-controller",
		LeaseSeconds: 60, // expires at 2060
		ResourceKeys: []string{"target:t-crash"},
	})
	if err != nil {
		t.Fatalf("failed to admit action: %v", err)
	}
	originalToken := admitRes.Lease.ClaimToken

	// Dispatch storage mutation intent to test operation binding preservation
	dispatchRecord := Record{
		Domain:         "action",
		ID:             actionID,
		Revision:       2,
		ExecutionEpoch: 1,
		State:          "CLAIMED",
		Payload:        admitRes.Record.Payload,
	}
	dispatchRecord.Revision = 3
	dispatchRecord.State = "EXECUTING"
	intent := dispatchIntent()
	intent.ActionID = actionID
	intent.ExecutionEpoch = 1
	intent.OperationID = "op-crash-preservation-123"

	if err := control.ClaimLeasedStorageDispatch(dispatchRecord, intent, originalToken); err != nil {
		t.Fatalf("failed to claim dispatch: %v", err)
	}

	// Step 2: Graceful reopen persistence only, not a process-kill or provider proof.
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}

	// Step 3: Reopen database before lease expiry (at t=2030 < 2060)
	earlyReopen, err := OpenControl(OpenOptions{
		Path:         path,
		Owner:        "takeover-controller",
		Now:          func() int64 { return 2030 },
		LeaseSeconds: 30,
	})
	if err != nil {
		t.Fatalf("failed to reopen database: %v", err)
	}
	defer earlyReopen.Close()

	// Step 4: Verify takeover is rejected before lease expiry!
	_, earlyTakeoverErr := earlyReopen.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     actionID,
		Owner:        "takeover-controller",
		LeaseSeconds: 60,
	})
	if !errors.Is(earlyTakeoverErr, ErrActionLeaseActive) {
		t.Fatalf("expected ErrActionLeaseActive before lease expiry, got: %v", earlyTakeoverErr)
	}

	// Close earlyReopen
	if err := earlyReopen.Close(); err != nil {
		t.Fatal(err)
	}

	// Step 5: Advance clock past lease expiry (t=2070 > 2060) and reopen
	expiredReopen, err := OpenControl(OpenOptions{
		Path:         path,
		Owner:        "takeover-controller",
		Now:          func() int64 { return 2070 },
		LeaseSeconds: 30,
	})
	if err != nil {
		t.Fatalf("failed to reopen database: %v", err)
	}
	defer expiredReopen.Close()

	// Takeover should now be ALLOWED!
	takeoverRes, err := expiredReopen.AdmitAndClaimAction(AdmissionRequest{
		ActionID:     actionID,
		Owner:        "takeover-controller",
		LeaseSeconds: 60,
		ResourceKeys: []string{"target:t-crash"},
	})
	if err != nil {
		t.Fatalf("takeover after lease expiry failed: %v", err)
	}

	if takeoverRes.Lease.Epoch != 2 {
		t.Fatalf("expected epoch 2 after takeover, got %d", takeoverRes.Lease.Epoch)
	}
	if takeoverRes.Lease.ClaimToken == originalToken {
		t.Fatal("claim token must be renewed on takeover")
	}

	// Step 6: Verify original storage dispatch operation binding is retained!
	disp, bound, err := expiredReopen.GetStorageDispatch(actionID, 1)
	if err != nil || !bound {
		t.Fatalf("original dispatch binding must be retained across reopen and takeover: bound=%v err=%v", bound, err)
	}
	if disp.Intent.OperationID != "op-crash-preservation-123" {
		t.Fatalf("operation ID was not preserved: got %s", disp.Intent.OperationID)
	}
}
