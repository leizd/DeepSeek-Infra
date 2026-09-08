package action

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"testing"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

type dispatchCrashCheckpoint struct {
	Dispatch store.StorageDispatch
	Writer   store.WriterLease
}

// Executes the actual coordinator and SQLite owner in a child Go test process,
// then force-kills it at the worker boundary. This is not a deepseekd-main or
// provider test; the worker double deliberately cannot claim an applied effect.
func TestCoordinatorDispatchSurvivesKilledProcess(t *testing.T) {
	const childKey = "DEEPSEEK_TEST_STORAGE_DISPATCH_CHILD"
	if path := os.Getenv(childKey); path != "" {
		control, err := store.OpenControl(store.OpenOptions{Path: path, Owner: "killed-controller"})
		if err != nil {
			t.Fatal(err)
		}
		defer control.Close()
		pendingStorageAction(t, control)
		worker := &inspectingWorker{execute: func(*actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
			dispatch, exists, err := control.GetStorageDispatch("durable", 1)
			if err != nil || !exists {
				t.Fatalf("missing committed intent: %v", err)
			}
			if err := json.NewEncoder(os.Stdout).Encode(dispatchCrashCheckpoint{dispatch, control.Writer()}); err != nil {
				t.Fatal(err)
			}
			time.Sleep(time.Minute)
			return nil, context.DeadlineExceeded
		}}
		_, err = NewCoordinator(control, worker).ExecuteStorageAction(context.Background(), "durable", storageRequest(" persisted across kill "))
		t.Fatalf("child was not killed during dispatch: %v", err)
	}
	path := t.TempDir()
	ctx, cancel := context.WithTimeout(context.Background(), 75*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestCoordinatorDispatchSurvivesKilledProcess$")
	command.Env = append(os.Environ(), childKey+"="+path)
	var stderr bytes.Buffer
	command.Stderr = &stderr
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
	var checkpoint dispatchCrashCheckpoint
	ready := make(chan error, 1)
	go func() { ready <- json.NewDecoder(io.LimitReader(stdout, 64*1024)).Decode(&checkpoint) }()
	select {
	case err := <-ready:
		if err != nil {
			t.Fatalf("child checkpoint: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("child did not reach committed dispatch barrier")
	}
	if checkpoint.Dispatch.Intent.OperationID != " persisted across kill " || checkpoint.Dispatch.ClaimRevision != 3 || checkpoint.Writer.FencingToken != 1 {
		t.Fatalf("unexpected durable checkpoint: %+v", checkpoint)
	}
	if err := command.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	err = command.Wait()
	waited = true
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Fatalf("child did not exit from forced kill: %v %s", err, stderr.String())
	}
	premature, err := store.OpenControl(store.OpenOptions{Path: path, Owner: "premature-successor"})
	if premature != nil {
		_ = premature.Close()
	}
	if !errors.Is(err, store.ErrWriterFenceHeld) {
		t.Fatalf("successor bypassed killed writer lease: %v", err)
	}
	remaining := time.Until(time.Unix(checkpoint.Writer.LeaseUntil, 0))
	if remaining <= 0 || remaining > 35*time.Second {
		t.Fatalf("unexpected real lease horizon: %v", remaining)
	}
	time.Sleep(remaining + 100*time.Millisecond)
	successor, err := store.OpenControl(store.OpenOptions{Path: path, Owner: "recovery-controller"})
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
	if successor.Writer().FencingToken != checkpoint.Writer.FencingToken+1 {
		t.Fatal("recovery writer fence did not advance")
	}
	retained, exists, err := successor.GetStorageDispatch("durable", 1)
	if err != nil || !exists || retained != checkpoint.Dispatch {
		t.Fatalf("kill lost or rebound dispatch: %+v %v", retained, err)
	}
	record, exists, err := successor.Get("action", "durable")
	if err != nil || !exists || record.State != "EXECUTING" || record.Revision != 3 {
		t.Fatalf("kill lost claimed revision: %+v %v", record, err)
	}
	queries := 0
	worker := &inspectingWorker{execute: func(*actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
		t.Error("successor blindly redispatched an uncertain mutation")
		return nil, context.DeadlineExceeded
	}, query: func(fence *commonv1.ActionFence, operation string) (*actionv1.StorageMutationResponse, error) {
		queries++
		if fence.ActionId != retained.Intent.ActionID || fence.ExecutionEpoch != retained.Intent.ExecutionEpoch || operation != retained.Intent.OperationID {
			t.Error("recovery query substituted intent")
		}
		return nil, context.DeadlineExceeded
	}}
	coordinator := NewCoordinator(successor, worker)
	if _, err := coordinator.ExecuteStorageAction(context.Background(), "durable", storageRequest("replacement")); !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatal(err)
	}
	if _, err := coordinator.ReconcileStorageAction(context.Background(), "durable", ""); !errors.Is(err, context.DeadlineExceeded) || queries != 1 {
		t.Fatalf("recovery query: count=%d err=%v", queries, err)
	}
	record, _, err = successor.Get("action", "durable")
	if err != nil || record.State != "EFFECT_UNKNOWN" || record.Revision != 4 {
		t.Fatalf("unknown worker result became definitive: %+v %v", record, err)
	}
}
