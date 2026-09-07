package action

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestExecutingActionCannotDispatchAgain(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "control", Now: func() int64 { return 500 }})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	record := store.Record{Domain: "action", ID: "once", ExecutionEpoch: 1, Payload: []byte(`{}`)}
	for index, state := range []string{"PENDING", "CLAIMED", "EXECUTING"} {
		record.State, record.Revision = state, int64(index+1)
		if err := control.Put(record); err != nil {
			t.Fatal(err)
		}
	}
	worker := &fakeWorkerClient{onExecute: func() { t.Error("EXECUTING action was dispatched again") }}
	_, err = NewCoordinator(control, worker, WithNow(func() int64 { return 500 })).ExecuteStorageAction(context.Background(), "once", &actionv1.StorageMutationRequest{OperationId: "op"})
	if !errors.Is(err, ErrStorageMutationUncertain) {
		t.Fatalf("expected reconciliation requirement: %v", err)
	}
	current, _, err := control.Get("action", "once")
	if err != nil || current.State != "EXECUTING" || current.Revision != 3 {
		t.Fatalf("retry changed durable state: %+v %v", current, err)
	}
}

func TestQueryFailureCannotProveNoRemoteEffect(t *testing.T) {
	for _, code := range []string{"DISK_CORRUPT", "NEW_QUERY_ERROR", "PRECONDITION_REJECTED", "Failed"} {
		t.Run(code, func(t *testing.T) {
			control := newFakeStore()
			control.records["action:a"] = store.Record{Domain: "action", ID: "a", ExecutionEpoch: 1, Revision: 3, State: "EFFECT_UNKNOWN"}
			worker := &fakeWorkerClient{queryResp: &actionv1.StorageMutationResponse{
				Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED,
				State:  commonv1.EffectState_EFFECT_STATE_UNKNOWN,
				Fence:  &commonv1.ActionFence{ActionId: "a", ExecutionEpoch: 1}, OperationId: "op",
				Error: &commonv1.ErrorDetail{Code: code},
			}}
			_, err := NewCoordinator(control, worker, WithNow(func() int64 { return 500 })).ReconcileStorageAction(context.Background(), "a", "op")
			record, _, _ := control.Get("action", "a")
			if err == nil || record.State != "EFFECT_UNKNOWN" || record.Revision != 3 {
				t.Fatalf("query failure became terminal outcome: %+v %v", record, err)
			}
		})
	}
}

func TestTwoCoordinatorsShareOneDurableDispatchClaim(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "control", Now: func() int64 { return 500 }})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	if err := control.Put(store.Record{Domain: "action", ID: "one-dispatch", ExecutionEpoch: 1, Revision: 1, State: "PENDING", Payload: []byte(`{}`)}); err != nil {
		t.Fatal(err)
	}
	started, release, done := make(chan struct{}), make(chan struct{}), make(chan error, 1)
	var calls atomic.Int64
	worker := &fakeWorkerClient{executeErr: context.DeadlineExceeded, onExecute: func() {
		if calls.Add(1) == 1 {
			close(started)
			<-release
		}
	}}
	first := NewCoordinator(control, worker, WithNow(func() int64 { return 500 }))
	second := NewCoordinator(control, worker, WithNow(func() int64 { return 500 }))
	go func() {
		_, err := first.ExecuteStorageAction(context.Background(), "one-dispatch", &actionv1.StorageMutationRequest{OperationId: "op"})
		done <- err
	}()
	defer func() {
		close(release)
		select {
		case err := <-done:
			if !errors.Is(err, ErrStorageMutationUncertain) {
				t.Errorf("first dispatch outcome: %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Error("first dispatch did not finish after release")
		}
	}()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("first dispatch did not reach worker")
	}
	_, err = second.ExecuteStorageAction(context.Background(), "one-dispatch", &actionv1.StorageMutationRequest{OperationId: "op"})
	if !errors.Is(err, ErrStorageMutationUncertain) || calls.Load() != 1 {
		t.Fatalf("concurrent coordinator bypassed the dispatch claim: calls=%d error=%v", calls.Load(), err)
	}
}

func TestRecordedNoEffectRequiresBoundTerminalEvidence(t *testing.T) {
	for _, rejected := range []bool{false, true} {
		status, code := actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_FAILED, "Failed"
		if rejected {
			status, code = actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED, "Rejected"
		}
		for _, valid := range []bool{false, true} {
			control := newFakeStore()
			control.records["action:a"] = store.Record{Domain: "action", ID: "a", ExecutionEpoch: 1, Revision: 3, State: "EFFECT_UNKNOWN"}
			response := &actionv1.StorageMutationResponse{Status: status, State: commonv1.EffectState_EFFECT_STATE_NOT_APPLIED,
				Fence: &commonv1.ActionFence{ActionId: "a", ExecutionEpoch: 1}, OperationId: "op", Error: &commonv1.ErrorDetail{Code: code}}
			if !valid {
				response.OperationId = "another-operation"
			}
			_, err := NewCoordinator(control, &fakeWorkerClient{queryResp: response}, WithNow(func() int64 { return 500 })).ReconcileStorageAction(context.Background(), "a", "op")
			record, _, _ := control.Get("action", "a")
			if valid && (err != nil || record.State != "FAILED_BEFORE_EFFECT") {
				t.Fatalf("recorded no-effect result lost: %+v %v", record, err)
			}
			if !valid && (err == nil || record.State != "EFFECT_UNKNOWN") {
				t.Fatalf("unbound no-effect result settled: %+v %v", record, err)
			}
		}
	}
}

func TestUnboundConfirmationCannotSettleAction(t *testing.T) {
	for _, query := range []bool{false, true} {
		control := newFakeStore()
		state := "PENDING"
		if query {
			state = "EFFECT_UNKNOWN"
		}
		control.records["action:a"] = store.Record{Domain: "action", ID: "a", ExecutionEpoch: 1, Revision: 1, State: state}
		response := &actionv1.StorageMutationResponse{Status: actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_CONFIRMED,
			State: commonv1.EffectState_EFFECT_STATE_APPLIED, Fence: &commonv1.ActionFence{ActionId: "a", ExecutionEpoch: 1}, OperationId: "other-operation"}
		coordinator := NewCoordinator(control, &fakeWorkerClient{executeResp: response, queryResp: response}, WithNow(func() int64 { return 500 }))
		var err error
		if query {
			_, err = coordinator.ReconcileStorageAction(context.Background(), "a", "op")
		} else {
			_, err = coordinator.ExecuteStorageAction(context.Background(), "a", &actionv1.StorageMutationRequest{OperationId: "op"})
		}
		record, _, _ := control.Get("action", "a")
		if !errors.Is(err, ErrStorageMutationUncertain) || record.State != "EFFECT_UNKNOWN" {
			t.Fatalf("unbound confirmation settled action: %+v %v", record, err)
		}
	}
}

func TestReplayRejectionMayDescribeAnAlreadyCommittedEffect(t *testing.T) {
	control := newFakeStore()
	control.records["action:a"] = store.Record{Domain: "action", ID: "a", ExecutionEpoch: 1, Revision: 1, State: "PENDING"}
	worker := &fakeWorkerClient{executeErr: internalprotocol.ErrStorageReplayRejected}
	_, err := NewCoordinator(control, worker, WithNow(func() int64 { return 500 })).ExecuteStorageAction(context.Background(), "a", &actionv1.StorageMutationRequest{OperationId: "op"})
	record, _, _ := control.Get("action", "a")
	if !errors.Is(err, ErrStorageMutationUncertain) || record.State != "EFFECT_UNKNOWN" {
		t.Fatalf("replay rejection incorrectly proved no effect: %+v %v", record, err)
	}
}

func TestAuthoritativeReconciliationRemainsDenied(t *testing.T) {
	control := newFakeStore()
	control.getErr = errors.New("must not read or modify state before cutover approval")
	_, err := NewCoordinator(control, &fakeWorkerClient{}, WithAuthoritative(true)).ReconcileStorageAction(context.Background(), "a", "op")
	if !errors.Is(err, store.ErrCutoverNotAuthorized) {
		t.Fatalf("reconciliation bypassed cutover gate: %v", err)
	}
}
