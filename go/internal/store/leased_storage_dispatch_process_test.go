package store

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"testing"
	"time"
)

type leasedDispatchCheckpoint struct {
	Lease    ActionLease
	Dispatch StorageDispatch
	Writer   WriterLease
}

// Actual forced process death and Go-owned SQLite takeover, not a provider or
// deepseekd-main test. No worker is called and no applied effect is synthesized.
func TestLeasedStorageDispatchSurvivesKilledOwner(t *testing.T) {
	const childKey = "DEEPSEEK_TEST_LEASED_DISPATCH_CHILD"
	if path := os.Getenv(childKey); path != "" {
		control, err := OpenControl(OpenOptions{Path: path, Owner: "killed-owner", LeaseSeconds: 3})
		if err != nil {
			t.Fatal(err)
		}
		defer control.Close()
		if err := control.Put(Record{Domain: "action", ID: "killed-dispatch", Revision: 1, ExecutionEpoch: 1, State: "PENDING"}); err != nil {
			t.Fatal(err)
		}
		claim, err := control.AdmitAndClaimAction(AdmissionRequest{ActionID: "killed-dispatch", LeaseSeconds: 3, ResourceKeys: []string{"reserved-target"}})
		if err != nil {
			t.Fatal(err)
		}
		record := claim.Record
		record.Revision++
		record.State = "EXECUTING"
		intent := dispatchIntent()
		intent.ActionID, intent.OperationID = record.ID, " original operation after kill "
		if err := control.ClaimLeasedStorageDispatch(record, intent, claim.Lease.ClaimToken); err != nil {
			t.Fatal(err)
		}
		dispatch, bound, err := control.GetLeasedStorageDispatch(record.ID, claim.Lease.Epoch, claim.Lease.ClaimToken)
		if err != nil || !bound {
			t.Fatalf("dispatch checkpoint: %v", err)
		}
		if err := json.NewEncoder(os.Stdout).Encode(leasedDispatchCheckpoint{claim.Lease, dispatch, control.Writer()}); err != nil {
			t.Fatal(err)
		}
		<-time.After(time.Minute)
		t.Fatal("owner was not killed")
	}
	path := t.TempDir()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestLeasedStorageDispatchSurvivesKilledOwner$")
	command.Env = append(os.Environ(), childKey+"="+path)
	command.Stderr = os.Stderr
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	waited := false
	defer func() {
		if !waited {
			_ = command.Process.Kill()
			_ = command.Wait()
		}
	}()
	var checkpoint leasedDispatchCheckpoint
	ready := make(chan error, 1)
	go func() { ready <- json.NewDecoder(io.LimitReader(stdout, 64*1024)).Decode(&checkpoint) }()
	select {
	case err := <-ready:
		if err != nil {
			t.Fatalf("child checkpoint: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("child did not reach durable dispatch")
	}
	if checkpoint.Lease.Epoch != 1 || checkpoint.Dispatch.Intent.OperationID != " original operation after kill " {
		t.Fatal("incorrect child checkpoint")
	}
	if err := command.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	err = command.Wait()
	waited = true
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Fatalf("owner did not exit by force: %v", err)
	}
	until := time.Until(time.Unix(max(checkpoint.Lease.LeaseUntil, checkpoint.Writer.LeaseUntil), 0))
	if until > 4*time.Second {
		t.Fatal("unexpected lease horizon")
	}
	if until > 0 {
		<-time.After(until + 50*time.Millisecond)
	}
	successor, err := OpenControl(OpenOptions{Path: path, Owner: "takeover-owner"})
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
	claim, err := successor.AdmitAndClaimAction(AdmissionRequest{ActionID: checkpoint.Lease.ActionID})
	if err != nil || claim.Lease.Epoch != 2 || successor.Writer().FencingToken != checkpoint.Writer.FencingToken+1 {
		t.Fatalf("takeover failed: %v", err)
	}
	dispatch, bound, err := successor.GetLeasedStorageDispatch(claim.Lease.ActionID, claim.Lease.Epoch, claim.Lease.ClaimToken)
	if err != nil || !bound || dispatch != checkpoint.Dispatch {
		t.Fatalf("killed owner lost original operation: %v", err)
	}
	resources, err := successor.GetResourceLeases(claim.Lease.ActionID)
	if err != nil || len(resources) != 1 || resources[0].Epoch != claim.Lease.Epoch || claim.Record.State != "RECONCILING" {
		t.Fatalf("takeover released uncertain resources: %v", err)
	}
}
